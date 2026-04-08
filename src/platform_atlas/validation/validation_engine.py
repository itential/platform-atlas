"""
ATLAS // Validation Engine
"""

import logging
import re
from pathlib import Path
from enum import Enum
from typing import Any
from dataclasses import dataclass, asdict

import pandas as pd
from rich.console import Console
from rich.live import Live
from rich.text import Text

# ATLAS Imports
from platform_atlas.core.context import ctx
from platform_atlas.validation.operators import OPERATORS
from platform_atlas.core.json_utils import load_json
from platform_atlas.core import ui
from platform_atlas.validation.extended_validation import run_extended_validation
from platform_atlas.core.exceptions import AtlasError

logger = logging.getLogger(__name__)

# FORBIDDEN KEYS: Superset - additionally blocked during object attribute access
FORBIDDEN_KEYS = frozenset({
    '__class__', '__bases__', "__mro__", '__subclasses__',
    '__globals', '__code__', '__builtins__', '__import__',
    '__init__', '__new__', '__del__', '__repr__', '__str__',
    '__dict__', '__doc__', '__module__', '__weakref__',
    '__func__', '__self__', '__loader__', '__spec__',
})

# EXECUTION KEYS: Always blocked in any context
_EXECUTION_KEYS = frozenset({
    '__class__', '__bases__', "__mro__", '__subclasses__',
    '__globals', '__code__', '__builtins__', '__import__',
})

_MAX_PATH_DEPTH = 20

theme = ui.theme
console = Console()

# ── URI credential redaction ──
# Matches scheme://user:pass@ or scheme://user@ in connection strings
_URI_CREDENTIAL_PATTERN = re.compile(r'(://)[^/@]+(?::[^/@]+)?@')

def _redact_uri_credentials(value: Any) -> Any:
    """Redact userinfo (user:pass) from URI strings before they hit reports."""
    if not isinstance(value, str):
        return value
    return _URI_CREDENTIAL_PATTERN.sub(r'\1******:******@', value)


class ValidationStatus(str, Enum):
    """Validation rule result status"""
    PASS = "PASS" # nosec B105
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"

@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of evaluating a single rule"""
    rule_number: str
    name: str
    category: str
    severity: str
    status: ValidationStatus
    path: str
    expected: Any
    actual: Any
    operator: str
    recommendations: str

    @classmethod
    def from_rule(
        cls,
        rule: dict,
        *,
        status: ValidationStatus,
        expected: Any = None,
        actual: Any = None,
        recommendations: str = "",
    ) -> "ValidationResult":
        """Create a result from a rule dictionary"""
        validation = rule.get("validation", {})
        return cls(
            rule_number=rule["rule_number"],
            name=rule["name"],
            category=rule.get("category", ""),
            severity=rule.get("severity", "warning"),
            status=status,
            path=rule.get("path", ""),
            expected=expected,
            actual=actual,
            operator=validation.get("operator", ""),
            recommendations=recommendations,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

# Core Functions
def validate_path_key(key: str, *, context: str = "dict") -> None:
    """Validate that a path key. Only reject dangerous keys on object access"""
    if context == "object" and key in FORBIDDEN_KEYS:
        raise ValueError(f"Forbidden path key: {key}")
    if key in _EXECUTION_KEYS:
        raise ValueError(f"Blocked execution key: {key}")

def _split_path(path: str) -> list[str]:
    """Split a dot-noation path, respecting quoted segments for names with spaces"""
    keys: list[str] = []
    current: list[str] = []
    in_quotes = False

    for char in path:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            keys.append(''.join(current))
            current = []
        else:
            current.append(char)
    if current:
        keys.append(''.join(current))
    return keys

def extract_value(data: dict, path: str) -> Any:
    """
    Extract a value from nested dict using dot-notation path

    List traversal supports:
        - Numeric index: "results.0.name"
        - Name-based lookup: "results.MyAdapter.properties" (matches item["name"])
        - Type-based lookup: "results.NSO.properties" (matches item["data"]["type"])
        - ID-based lookup: "results.GatewayManager.version (matches item["id"])"
    """
    #keys = path.split(".")
    keys = _split_path(path)
    if len(keys) > _MAX_PATH_DEPTH:
        raise ValueError(f"Path too deep ({len(keys)} levels, max {_MAX_PATH_DEPTH}): {path}")
    current = data

    for key in keys:
        validate_path_key(key) # Validate each key first

        if current is None:
            return None

        if isinstance(current, dict):
            current = current.get(key)
            continue

        if isinstance(current, list):
            # numeric index
            if key.isdigit():
                idx = int(key)
                if idx >= len(current):
                    return None
                current = current[idx]
                continue

            # Search list items by name, type, or id
            found = None
            for item in current:
                if isinstance(item, dict):
                    item_name = item.get("name", "")
                    # Direct name match (top-level)
                    if item_name == key or item_name.replace(" ", "_") == key:
                        found = item
                        break
                    # Unwrapped data.name match
                    item_data = item.get("data")
                    if isinstance(item_data, dict):
                        data_name = item_data.get("name", "")
                        if data_name == key or data_name.replace(" ", "_") == key:
                            found = item_data
                            break
                    # Type-based match (nested in data envelope)
                    if item.get("data", {}).get("properties", {}).get("type") == key:
                        found = item.get("data")
                        break
                    # ID-based match (same-level)
                    if item.get("id") == key:
                        found = item
                        break
            current = found
            continue

        return None

    return current

def _parent_section_exists(data: dict, path: str) -> bool:
    """Check whether the parent data section for a rule path was captured.

    Paths follow the structure: section.collector.leaf (e.g.,
    "platform.config.log_level.value").  If the first two segments
    resolve to a non-None value, the section was successfully
    collected — the specific leaf just isn't set.  If the first two
    segments don't exist, the section was never captured at all.

    This distinction matters for default_value handling:
      • Section exists, leaf missing  → safe to apply defaults
      • Section missing entirely      → SKIP (we can't assume anything)
    """
    keys = _split_path(path)
    if len(keys) < 2:
        # Single-segment path — can't determine a parent section
        return False

    # Walk the first two segments
    current = data
    for key in keys[:2]:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
        if current is None:
            return False
    return True


def resolve_expected(expected: Any, data: dict) -> Any:
    """Resolve expected value, handling computed references"""
    if not isinstance(expected, dict) or "ref" not in expected:
        return expected

    # Get the referenced value
    base_value = extract_value(data, expected["ref"])
    if base_value is None:
        raise ValueError(f"Reference path not found: {expected['ref']}")

    result = float(base_value)

    # Apply multiplier
    if "multiply" in expected:
        result *= expected["multiply"]

    # Apply offset
    if "add" in expected:
        result += expected["add"]

    # Apply bounds
    if "min" in expected:
        result = max(result, expected["min"])
    if "max" in expected:
        result = min(result, expected["max"])

    # Return as int if its a whole number
    return int(result) if result == int(result) else result

def extract_value_with_fallback(data: dict, rule: dict) -> tuple[Any, str, str | None]:
    """Extract value trying primary path first, then alt_path"""
    primary_path = rule["path"]
    alt_path = rule.get("alt_path")

    # Build the "paths tried" message for potential error reporting
    if alt_path:
        paths_tried_msg = f"Primary: {primary_path}, Alt: {alt_path}"
    else:
        paths_tried_msg = primary_path

    # Try primary path first
    value = extract_value(data, primary_path)
    if value is not None:
        return value, primary_path, paths_tried_msg

    # Try fallback if available
    if alt_path:
        value = extract_value(data, alt_path)
        if value is not None:
            return value, alt_path, paths_tried_msg

    # Neither worked
    return None, primary_path, paths_tried_msg

# ── Human-readable section names for skip messages ──
_SECTION_LABELS: dict[str, str] = {
    "platform.config":              "Platform server configuration",
    "platform.config_file":         "Platform properties file",
    "platform.health_server":       "Platform API health data",
    "platform.health_status":       "Platform API status",
    "platform.adapter_props":       "Platform adapter properties",
    "platform.adapter_status":      "Platform adapter status",
    "platform.application_status":  "Platform application status",
    "platform.profile":             "Platform profile",
    "platform.agmanager_size":      "AGManager pronghorn data",
    "mongo.server_status":          "MongoDB server status",
    "mongo.config_file":            "MongoDB configuration file (/etc/mongod.conf)",
    "mongo.build_info":             "MongoDB build info",
    "mongo.db_stats":               "MongoDB database statistics",
    "mongo.repl_set_votes":         "MongoDB replica set data",
    "mongo.repl_set_healthy":       "MongoDB replica set health",
    "redis.info":                   "Redis server info",
    "redis.config_file":            "Redis configuration file (/etc/redis/redis.conf)",
    "redis.runtime_config":         "Redis runtime config (CONFIG GET)",
    "redis.acl_users":              "Redis ACL user list",
    "redis.sentinel_config":        "Redis Sentinel configuration file",
    "redis.sentinel_runtime":       "Sentinel runtime config (SENTINEL MASTERS)",
    "gateway4.config_file":         "Gateway 4 configuration file (properties.yml)",
    "gateway4.runtime_config":      "Gateway 4 runtime config (API GET /config)",
    "gateway4.api_status":          "Gateway 4 server status (API GET /status)",
    "gateway4.packages":            "Gateway 4 installed packages",
    "gateway4.sync_config":         "Gateway 4 sync configuration",
    "gateway4.db_sizes":            "Gateway 4 database sizes",
    "gateway4.db_config":           "Gateway 4 database configuration",
    "gateway5.variables":           "Gateway 5 environment variables",
    "gateway5.iagctl":              "Gateway 5 iagctl data",
    "checks.python_version":        "Python version check",
    "checks.architecture_validation": "Architecture validation data",
}

def _section_label(path: str) -> str:
    """Return a human-readable label for the parent section of a rule path."""
    keys = _split_path(path)
    if len(keys) >= 2:
        section_key = f"{keys[0]}.{keys[1]}"
        label = _SECTION_LABELS.get(section_key)
        if label:
            return label
        # Fallback: titlecase the dotted path
        return section_key.replace("_", " ").replace(".", " > ").title()
    return path


def evaluate_rule(rule: dict, data: dict) -> dict:
    """Evaluate a single rule against captured data"""

    validation = rule["validation"]
    passed = False
    used_default = False

    try:
        expected = resolve_expected(validation["expected"], data)
    except ValueError as e:
        return ValidationResult.from_rule(
            rule, status=ValidationStatus.ERROR, expected=validation["expected"],
            recommendations=str(e)
        ).to_dict()

    # Extract actual value from data (with fallback support)
    actual, used_path, paths_tried = extract_value_with_fallback(data, rule)
    actual = _redact_uri_credentials(actual)

    # Handle missing values
    if actual is None:
        if validation["operator"] == "exists":
            passed = not expected # exists: false would pass
        else:
            default = rule.get("default_value")

            # Only apply defaults when the parent section was captured.
            # If the section exists but this leaf is missing, the value
            # genuinely isn't set -- the default is a safe assumption.
            # If the section doesn't exist at all, we never collected
            # that data and can't assume anything.
            section_captured = (
                _parent_section_exists(data, rule["path"])
                or (rule.get("alt_path") and _parent_section_exists(data, rule["alt_path"]))
            )

            if default is not None and section_captured:
                actual = default
                used_default = True
            elif not section_captured:
                label = _section_label(rule["path"])
                return ValidationResult.from_rule(
                    rule, status=ValidationStatus.SKIP, expected=expected,
                    recommendations=(
                        f"Rule skipped because the {label} data was not collected. "
                        f"This usually means the configuration file could not be read "
                        f"or the service was unreachable during capture."
                    )
                ).to_dict()
            else:
                return ValidationResult.from_rule(
                    rule, status=ValidationStatus.SKIP, expected=expected,
                    recommendations=(
                        f"Rule skipped because this setting was not found in the "
                        f"captured data and no default value is defined for this rule"
                    )
                ).to_dict()

    # Run the operator (actual is guaranteed non-None here, or exists already set passed)
    if actual is not None:
        # Look up and run the operator
        op_key = (validation["type"], validation["operator"])
        if op_key not in OPERATORS:
            return ValidationResult.from_rule(
                rule, status=ValidationStatus.ERROR, expected=expected, actual=actual,
                recommendations=f"Unknown operator: {op_key}"
            ).to_dict()

        try:
            passed = OPERATORS[op_key](actual, expected)
        except Exception as e:
            return ValidationResult.from_rule(
                rule, status=ValidationStatus.ERROR, expected=expected, actual=actual,
                recommendations=f"Evaluation error: {e}"
            ).to_dict()

    # Build result
    status = ValidationStatus.PASS if passed else ValidationStatus.FAIL
    message = rule["messages"]["pass" if passed else "fail"]

    # Append note when we fell back to a default value
    if used_default:
        default = rule.get("default_value")
        message += (
            f" (Note: This value was not explicitly set in the configuration"
            f" -- the default value of {default!r} was used for this check)"
        )

    return ValidationResult.from_rule(
        rule, status=status, expected=expected, actual=actual,
        recommendations=message
    ).to_dict()

### START RULE-CHAINING FUNCTIONS ###
def partition_rules(rules: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate rules into independent/dependent based on 'depends_on'"""
    independent = []
    dependent = []

    for rule in rules:
        if "depends_on" in rule:
            dependent.append(rule)
        else:
            independent.append(rule)
    return independent, dependent

def should_execute_rule(
    rule: dict,
    results: dict[str, dict],
    rule_names: dict[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Check if a rule should execute based on its dependencies.

    Args:
        rule: The rule being evaluated.
        results: Already-evaluated rule results keyed by rule_number.
        rule_names: Optional lookup of rule_number -> rule name for
                    better skip messages when a dependency hasn't been
                    evaluated yet.
    """
    if "depends_on" not in rule:
        return True, None

    dep = rule["depends_on"]
    dep_rule_number = dep.get("rule")
    dep_status = dep.get("when_status", "PASS")

    # Check if dependency rule was evaluated
    if dep_rule_number not in results:
        # Use the human-readable rule name when available
        dep_label = (rule_names or {}).get(dep_rule_number, dep_rule_number)
        return False, f'Rule skipped because the required dependency "{dep_label}" was not evaluated'

    dep_result = results[dep_rule_number]
    dep_name = dep_result.get("name", dep_rule_number)
    actual_status = dep_result["status"]

    # ── Version-gated condition ──────────────────────────────────
    # "when_version_below": "6.1.2" → only run this rule when the
    # dependency's actual value parses to a version *below* the
    # threshold.  If the version is at or above, skip.
    version_below = dep.get("when_version_below")
    if version_below:
        from platform_atlas.validation.operators import parse_version

        dep_actual = dep_result.get("actual", "")
        try:
            actual_ver = parse_version(str(dep_actual))
            threshold = parse_version(version_below)
        except (ValueError, TypeError):
            # Can't parse — safe to skip (we can't verify)
            return False, (
                f'Rule skipped because the version from "{dep_name}" '
                f'could not be determined (got: {dep_actual!r})'
            )

        if actual_ver >= threshold:
            return False, (
                f'Rule skipped because "{dep_name}" reported version '
                f'{actual_ver} (≥ {threshold}) — this check only applies '
                f'to versions below {version_below}'
            )
        # Version is below the threshold — allow execution
        return True, None

    # Check if dependency rule has the expected status
    if actual_status == dep_status:
        return True, None

    # Build a human-readable explanation of why the dependency wasn't met
    if dep_status == "FAIL" and actual_status == "PASS":
        reason = f'Rule skipped because "{dep_name}" passed (this rule only applies when that check fails)'
    elif dep_status == "PASS" and actual_status == "FAIL":
        reason = f'Rule skipped because "{dep_name}" failed (this rule requires that check to pass first)'
    elif actual_status == "SKIP":
        reason = f'Rule skipped because "{dep_name}" was also skipped'
    else:
        reason = f'Rule skipped because "{dep_name}" status was {actual_status} (expected {dep_status})'

    return False, reason

def create_skip_result(rule: dict, reason: str) -> dict:
    """Create a SKIP result for a rule that wasn't executed"""
    validation = rule.get("validation", {})
    return {
        "rule_number": rule["rule_number"],
        "name": rule["name"],
        "category": rule.get("category", ""),
        "severity": rule.get("severity", "warning"),
        "status": "SKIP",
        "path": rule.get("path", ""),
        "expected": None,
        "actual": None,
        "operator": validation.get("operator", ""),
        "recommendations": reason,
    }
### END RULE-CHAINING FUNCTIONS ###

def validate(ruleset: dict, captured_data: dict) -> pd.DataFrame:
    """Validate captured data against a ruleset"""
    results = {} # rule_number -> result dict

    # Get enabled rules
    enabled_rules = [r for r in ruleset.get("rules", []) if r.get("enabled", True)]

    # If modules_ran metadata exists, only keep rules whose category was captured
    modules_ran = set(captured_data.get("metadata", {}).get("modules_ran", []))
    if modules_ran:
        enabled_rules = [
            r for r in enabled_rules
            if r.get("category", "") in modules_ran
        ]

    # Check for user-skipped rules from config
    skip_rules: set[str] = set()
    try:
        config = ctx().config
        skip_rules = set(config.skip_rules or [])
    except Exception:
        logger.debug("Could not load skip_rules from config", exc_info=True)

    # Separate independent and dependent rules
    independent_rules, dependent_rules = partition_rules(enabled_rules)

    # Build a rule_number -> name lookup for human-readable skip messages.
    # Uses the FULL ruleset (before filtering) so dependency names resolve
    # even when the dependency rule was filtered out or disabled.
    rule_names: dict[str, str] = {
        r["rule_number"]: r.get("name", r["rule_number"])
        for r in ruleset.get("rules", [])
    }

    total_rules = len(enabled_rules)
    processed = 0
    pass_count = 0
    fail_count = 0

    console.print("◉ Running Primary Validation Checks", style=f"bold {theme.primary}")
    def make_status_text() -> Text:
        text = Text()
        text.append(f"  ▶ {processed}", style=f"bold {theme.secondary}")
        text.append(f"/{total_rules}", style=theme.secondary_dim)
        text.append(f" rules processed", style=theme.warning)
        return text

    with Live(make_status_text(), console=console, refresh_per_second=10, transient=False) as live:
        # Evaluate independent rules first
        for rule in independent_rules:
            if rule["rule_number"] in skip_rules:
                result = create_skip_result(rule, "Skipped by user (config: skip_rules)")
            else:
                result = evaluate_rule(rule, captured_data)
            results[rule["rule_number"]] = result
            processed += 1
            if result["status"] == "PASS":
                pass_count += 1
            elif result["status"] == "FAIL":
                fail_count += 1
            live.update(make_status_text())

        # Evaluate dependent rules, checking prerequisites
        for rule in dependent_rules:
            if rule["rule_number"] in skip_rules:
                result = create_skip_result(rule, "Skipped by user (config: skip_rules)")
            else:
                should_execute, skip_reason = should_execute_rule(rule, results, rule_names)
                if should_execute:
                    result = evaluate_rule(rule, captured_data)
                else:
                    result = create_skip_result(rule, skip_reason)
            results[rule["rule_number"]] = result
            processed += 1
            if result["status"] == "PASS":
                pass_count += 1
            elif result["status"] == "FAIL":
                fail_count += 1
            live.update(make_status_text())

    # Convert to DataFrame
    df = pd.DataFrame(list(results.values()))

    if 'expected' in df.columns:
        df['expected'] = [str(x) if x is not None else '' for x in df['expected']]

    if 'actual' in df.columns:
        df['actual'] = [str(x) if x is not None else '' for x in df['actual']]

    # Convert to DataFrame
    return df

# MAIN ENTRYPOINT
def validate_from_files(data_path: str | Path) -> pd.DataFrame:
    """Load ruleset and data from files, then validate"""
    rules = ctx().rules
    config = ctx().config

    # Load user data
    try:
        captured_data = load_json(data_path)
    except AtlasError as e:
        console.print(f"[bold {theme.error}][Validation Engine ERROR][/bold {theme.error}] {e.message}")
        raise SystemExit()

    # Merge separate log analysis file if it exists
    logs_path = Path(data_path).parent / "01_logs.json"
    if logs_path.is_file():
        try:
            logs_data = load_json(logs_path)
            platform = captured_data.setdefault("platform", {})
            if "log_analysis" in logs_data:
                platform["log_analysis"] = logs_data["log_analysis"]
            if "webserver_logs" in logs_data:
                platform["webserver_logs"] = logs_data["webserver_logs"]
            if "mongo_log_analysis" in logs_data:
                mongo = captured_data.setdefault("mongo", {})
                mongo["log_analysis"] = logs_data["mongo_log_analysis"]
            logger.debug("Merged log analysis from %s", logs_path)
        except Exception as e:
            logger.warning("Failed to load log analysis file: %s", e)

    # Validate Rules and Load into DataFrame
    df = validate(rules, captured_data)

    # EXTENDED VALIDATION CHECKS
    if config.extended_validation_checks:
        console.print("\n◉ Running Additional Validation Checks", style=f"bold {theme.primary}")
        extended_results = run_extended_validation(captured_data)

    # Add Metadata to standard results
    atlas_internal = captured_data.get("_atlas", {})
    user_metadata = atlas_internal.get("metadata", {})
    user_system_facts = atlas_internal.get("system_facts", {})
    platform_data = captured_data.get("platform", {})
    user_platform = platform_data.get("health_server", {}) if isinstance(platform_data, dict) else {}

    # Add Metadata into the dataframe
    df.attrs["hostname"] = user_system_facts.get("hostname", "Unknown")
    df.attrs["platform_ver"] = user_platform.get("version", "Unknown")
    df.attrs["organization_name"] = user_metadata.get("organization_name", "")
    df.attrs["environment"] = user_metadata.get("environment", "")
    df.attrs["ruleset_id"] = user_metadata.get("ruleset_id", "")
    df.attrs["ruleset_version"] = user_metadata.get("ruleset_version", "")
    df.attrs["ruleset_profile"] = user_metadata.get("ruleset_profile", "")
    df.attrs["modules_ran"] = user_metadata.get("modules_ran", "")
    df.attrs["captured_at"] = user_metadata.get("captured_at", "")

    if config.extended_validation_checks:
        # Attach extended results as metadata to be used in the Reporting Engine
        df.attrs["extended_results"] = [result.to_dict() for result in extended_results]

    return df
