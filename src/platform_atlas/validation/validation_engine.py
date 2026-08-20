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
from platform_atlas.core.utils import split_path
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

# Skip-kind families — set on a SKIP result's ``skip_kind`` so the report can
# color-code *why* a rule was skipped (see report.html skip callout):
#   unreachable — the data was never collected (service down / file unreadable).
#                 Enriched with the collector's real error from failed_modules.
#   no_data     — the section WAS collected, but this specific setting was absent.
#   conditional — skipped on purpose by a rule dependency or version gate.
SKIP_UNREACHABLE = "unreachable"
SKIP_NO_DATA = "no_data"
SKIP_CONDITIONAL = "conditional"

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
    # For SKIP rows only: which family of skip this is, so the report can
    # color-code it — "unreachable" (data not collected / service down),
    # "no_data" (collected but the setting was absent), or "conditional"
    # (skipped by a rule dependency / version gate). None for PASS/FAIL.
    skip_kind: str | None = None
    # True only for PASS/FAIL rows where the setting itself was never found in
    # the capture and the rule's documented ``default_value`` was substituted
    # in its place — distinct from a SKIP, where we have no data to assume
    # anything from. Lets the report explain why a result may look surprising
    # (e.g. "PASS" when the operator was never explicitly configured).
    used_default: bool = False

    @classmethod
    def from_rule(
        cls,
        rule: dict,
        *,
        status: ValidationStatus,
        expected: Any = None,
        actual: Any = None,
        recommendations: str = "",
        skip_kind: str | None = None,
        used_default: bool = False,
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
            skip_kind=skip_kind,
            used_default=used_default,
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
    """Backwards-compatible alias for the canonical splitter in core.utils."""
    return split_path(path)

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
                    # Coerce to str — captured payloads occasionally have non-string
                    # names (None, ints) that would crash .replace().
                    item_name = str(item.get("name") or "")
                    # Direct name match (top-level)
                    if item_name == key or item_name.replace(" ", "_") == key:
                        found = item
                        break
                    # Unwrapped data.name match
                    item_data = item.get("data")
                    if isinstance(item_data, dict):
                        data_name = str(item_data.get("name") or "")
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

    A Platform API endpoint that errors mid-capture (see
    ``platform.py::_fetch_endpoint``) leaves its sentinel
    ``{"error": ..., "status": "failed"}`` in place of real data — that's a
    non-None dict, but it means the section was NOT actually captured, so
    it must not be treated as "leaf genuinely absent" for default purposes.
    """
    keys = _split_path(path)
    if not keys:
        return False

    def _is_failed_endpoint(value: Any) -> bool:
        return isinstance(value, dict) and value.get("status") == "failed"

    # For single-segment rule paths (e.g. "checks") the "parent" is the
    # capture root itself — treat the section as captured iff that single
    # top-level key resolved to a non-None value. Refusing to honor defaults
    # for those rules forced rule authors to invent dummy parent keys.
    if len(keys) == 1:
        value = data.get(keys[0]) if isinstance(data, dict) else None
        return value is not None and not _is_failed_endpoint(value)

    # Walk the first two segments
    current = data
    for key in keys[:2]:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
        if current is None or _is_failed_endpoint(current):
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
    "gateway5.image_tag":           "Gateway 5 Helm chart image tag",
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


def _k8s_skip_message(data_source: str, data: dict) -> str:
    """Build a contextual skip message for Kubernetes rules that declare a data_source.

    Inspects the capture to determine which sources actually ran so the message
    reflects whether the gap is a missing values.yaml, missing kubectl access,
    or a field simply not configured in the Helm chart / not returned by kubectl.
    """
    k8s_section = (data.get("system") or {}).get("kubernetes") or {}

    # system.kubernetes is passthrough-preserved in finalize_capture, so the
    # full dict (including values.yaml-derived keys like replica_count) is
    # present in the filtered capture. Key presence — not value truthiness —
    # distinguishes "values.yaml was configured" from "it was not": the key
    # exists with None when configured-but-unset, and is absent when no
    # values.yaml was provided at all.
    has_values_yaml = "replica_count" in k8s_section
    # kubectl always writes max_restart_count (even as 0) when it runs.
    has_kubectl = "max_restart_count" in k8s_section

    if data_source == "values_yaml":
        if not has_values_yaml:
            return (
                "Rule skipped — this check reads from the Helm values.yaml file, "
                "but no values.yaml was configured for this environment. "
                "Add the path to your values.yaml in the environment settings to enable this check."
            )
        return (
            "Rule skipped — this field was not found in the provided values.yaml. "
            "The setting may not be explicitly configured in your Helm chart values "
            "(the Kubernetes default will apply at runtime)."
        )

    if data_source == "kubectl":
        if not has_kubectl:
            return (
                "Rule skipped — this check requires live cluster data from kubectl, "
                "but kubectl was unavailable or did not run during capture. "
                "Ensure kubectl is configured in the environment settings and the cluster is reachable."
            )
        return (
            "Rule skipped — kubectl ran during capture but did not return data for this field. "
            "The resource may not exist in the cluster "
            "(e.g. no HPA is configured, or the resource type is not present)."
        )

    return (
        "Rule skipped — this setting was not found in the captured data "
        "and no default value is defined for this rule."
    )


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

    # Platform masks sensitive config values with "*" or "****".
    # On the primary path (API response) the key only exists when the setting is
    # configured, so a masked value still means the setting is present → PASS.
    # On the alt path (properties file) we cannot determine the real value → SKIP.
    if isinstance(actual, str) and actual and actual == "*" * len(actual):
        if used_path == rule.get("path"):
            return ValidationResult.from_rule(
                rule, status=ValidationStatus.PASS, expected=expected, actual=actual,
                recommendations=rule["messages"]["pass"]
            ).to_dict()
        return ValidationResult.from_rule(
            rule, status=ValidationStatus.SKIP, expected=expected,
            skip_kind=SKIP_NO_DATA,
            recommendations=(
                "Rule skipped because the captured value was masked by the platform "
                "during collection. Capture this value from the source configuration "
                "file to evaluate this rule."
            )
        ).to_dict()

    # Handle missing values
    if actual is None:
        # Only trust a missing value once we know the parent section was
        # actually captured. If the section exists but this leaf is missing,
        # the value genuinely isn't set. If the section doesn't exist at all,
        # we never collected that data and can't assume anything — not even
        # for "exists" checks, which would otherwise treat "never collected"
        # the same as "confirmed absent" and report a false FAIL.
        section_captured = (
            _parent_section_exists(data, rule["path"])
            or (rule.get("alt_path") and _parent_section_exists(data, rule["alt_path"]))
        )

        if not section_captured:
            label = _section_label(rule["path"])
            return ValidationResult.from_rule(
                rule, status=ValidationStatus.SKIP, expected=expected,
                skip_kind=SKIP_UNREACHABLE,
                recommendations=(
                    f"Rule skipped because the {label} data was not collected. "
                    f"This usually means the configuration file could not be read "
                    f"or the service was unreachable during capture."
                )
            ).to_dict()

        default = rule.get("default_value")

        if default is not None:
            # A documented default applies even to "exists" rules: if the
            # platform falls back to a built-in value when the setting is
            # unset, that value effectively "exists" and should be evaluated
            # like any other captured value (e.g. IAG-028's Gateway Connect
            # certificate path).
            actual = default
            used_default = True
        elif validation["operator"] == "exists":
            passed = not expected # exists: false would pass
        else:
            data_source = rule.get("data_source")
            skip_msg = (
                _k8s_skip_message(data_source, data)
                if data_source
                else (
                    "Rule skipped because this setting was not found in the "
                    "captured data and no default value is defined for this rule"
                )
            )
            return ValidationResult.from_rule(
                rule, status=ValidationStatus.SKIP, expected=expected,
                skip_kind=SKIP_NO_DATA,
                recommendations=skip_msg
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
        recommendations=message, used_default=used_default
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

    # A user-suppressed parent should never satisfy a dependency condition —
    # the result isn't real data, it's an intentional override.
    if dep_result.get("user_suppressed"):
        return False, f'Rule skipped because "{dep_name}" was suppressed by user'

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

def create_skip_result(rule: dict, reason: str, *, kind: str = SKIP_CONDITIONAL) -> dict:
    """Create a SKIP result for a rule that wasn't executed.

    ``kind`` defaults to "conditional" because this path is used for
    dependency / version-gate skips (and user-suppression, which the report
    relabels to "Suppressed" anyway).
    """
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
        "skip_kind": kind,
        "used_default": False,
    }
### END RULE-CHAINING FUNCTIONS ###

def validate(ruleset: dict, captured_data: dict, *, headless: bool = False) -> pd.DataFrame:
    """Validate captured data against a ruleset"""
    console = Console(quiet=headless)  # noqa: F841 — shadow module-level console when headless
    results = {} # rule_number -> result dict

    # Get enabled rules
    enabled_rules = [r for r in ruleset.get("rules", []) if r.get("enabled", True)]

    # If modules_ran metadata exists, only keep rules whose category was captured.
    # Capture writes this under _atlas.metadata (see capture_engine.finalize_capture).
    # "all" means every registered module ran successfully — still check kubernetes.
    # "gateway4_api" is the protocol collector name; rules use category "gateway4".
    # "kubernetes_helm" maps to the "kubernetes" rule category.
    _MODULE_TO_CATEGORY: dict[str, str] = {
        "gateway4_api": "gateway4",
        "kubernetes_helm": "kubernetes",
        # "system" module on a Kubernetes node also produces kubernetes data —
        # map it so that a kubernetes_helm failure doesn't silently drop all
        # KBS rules when the system module still ran and collected k8s facts.
        "system": "kubernetes",
    }
    modules_ran = set(
        captured_data.get("_atlas", {}).get("metadata", {}).get("modules_ran", [])
    )

    # Detect kubernetes data by presence of system.kubernetes in the capture.
    # system.kubernetes is always passed through by finalize_capture, so for a
    # Kubernetes environment the key exists even when the dict is empty (no
    # values.yaml, kubectl unavailable). Use `is not None` not `bool()` so that
    # an empty dict does not suppress kubernetes rules.
    has_k8s_data = (captured_data.get("system") or {}).get("kubernetes") is not None

    # Map each attempted-and-FAILED module to its rule category and remember the
    # collector's real failure reason. This lets an unreachable subsystem show
    # up as color-coded "skipped: couldn't connect" rows (enriched after the
    # evaluation loop) instead of silently vanishing from the report. Only
    # modules that were actually tried appear here, so out-of-tier/never-run
    # categories stay filtered out.
    failed_modules = (
        captured_data.get("_atlas", {}).get("metadata", {}).get("failed_modules", []) or []
    )
    category_errors: dict[str, str] = {}
    for _fm in failed_modules:
        if not isinstance(_fm, dict):
            continue
        _cat = _MODULE_TO_CATEGORY.get(_fm.get("name", ""), _fm.get("name", ""))
        _msg = (_fm.get("error_message") or "").strip()
        if _cat and _msg and _cat not in category_errors:
            category_errors[_cat] = _msg
    failed_categories = set(category_errors)

    if modules_ran and "all" not in modules_ran:
        resolved_categories = {_MODULE_TO_CATEGORY.get(m, m) for m in modules_ran}
        # Non-kubernetes captures also run the "system" module, so we need the
        # k8s data check here too — "system" alone is not proof of kubernetes.
        if not has_k8s_data:
            resolved_categories.discard("kubernetes")
        # Keep rules for categories whose collector was tried but FAILED, so the
        # subsystem outage surfaces per-rule (as unreachable skips) below.
        resolved_categories |= failed_categories
        # SaaS hard boundary: the gateway-category allowlist always wins.
        # The system→kubernetes mapping (or any future mapping) must never
        # pull a non-gateway category into a SaaS run. The loaded ruleset
        # is already tier-filtered to gateway rules; this keeps the
        # boundary even for hand-loaded rulesets.
        try:
            if ctx().is_saas:
                resolved_categories &= {"gateway4", "gateway5"}
        except Exception:
            pass
        enabled_rules = [
            r for r in enabled_rules
            if r.get("category", "") in resolved_categories
        ]
    elif "all" in modules_ran:
        # All registered modules ran, but kubernetes modules are only registered
        # for Kubernetes environments. Filter kubernetes rules when no k8s data
        # was collected so non-Kubernetes captures don't produce spurious results.
        if not has_k8s_data:
            enabled_rules = [
                r for r in enabled_rules
                if r.get("category") != "kubernetes"
            ]

    # Check for user-skipped rules from config — keyed by rule_number for O(1) lookup
    skip_rules_map: dict[str, dict] = {}
    try:
        config = ctx().config
        skip_rules_map = {r["rule_number"]: r for r in (config.skip_rules or []) if isinstance(r, dict)}
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
            if rule["rule_number"] in skip_rules_map:
                result = create_skip_result(rule, "Suppressed by user")
                result["user_suppressed"] = True
                result["suppression_reason"] = skip_rules_map[rule["rule_number"]].get("reason", "")
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
            if rule["rule_number"] in skip_rules_map:
                result = create_skip_result(rule, "Suppressed by user")
                result["user_suppressed"] = True
                result["suppression_reason"] = skip_rules_map[rule["rule_number"]].get("reason", "")
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

    # Enrich "unreachable" skips with the collector's real failure reason, so a
    # rule skipped because its subsystem was down can name *why* (e.g. "auth
    # failed at host:port") instead of only "the service was unreachable".
    if category_errors:
        for result in results.values():
            if (
                result.get("status") == "SKIP"
                and result.get("skip_kind") == SKIP_UNREACHABLE
            ):
                err = category_errors.get(result.get("category", ""))
                if err:
                    base = (result.get("recommendations") or "").rstrip()
                    result["recommendations"] = f"{base} Reported error: {err}".strip()

    # Convert to DataFrame
    df = pd.DataFrame(list(results.values()))

    if 'expected' in df.columns:
        df['expected'] = df['expected'].fillna('').astype(str)

    if 'actual' in df.columns:
        df['actual'] = df['actual'].fillna('').astype(str)

    if 'used_default' in df.columns:
        df['used_default'] = df['used_default'].fillna(False).astype(bool)

    # Low-cardinality string columns benefit from Categorical dtype (lower memory,
    # faster groupby/isin). These values are stable after construction and survive
    # the write-to-parquet boundary as dictionary-encoded Arrow data.
    for _cat_col in ("category", "severity", "skip_kind"):
        if _cat_col in df.columns:
            df[_cat_col] = pd.Categorical(df[_cat_col])

    # Convert to DataFrame
    return df


def _extended_checks_enabled() -> bool:
    """Whether the Additional Validation Checks (AVC) should run.

    They run only in the platform-anchored tiers. A SaaS audit is a single
    gateway with no Platform/MongoDB/Redis, and every AVC inspects exactly that
    architecture (adapters, applications, Mongo/Redis health, …) — so under SaaS
    they would only ever SKIP, and the SaaS report omits them entirely. Disable
    them there so they don't run at all.
    """
    return bool(ctx().config.extended_validation_checks) and not ctx().is_saas


# MAIN ENTRYPOINT
def validate_from_files(data_path: str | Path, *, headless: bool = False, skip_adapter_check: bool = False) -> pd.DataFrame:
    """Load ruleset and data from files, then validate"""
    console = Console(quiet=headless)  # noqa: F841 — shadow module-level console when headless
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
    df = validate(rules, captured_data, headless=headless)

    # EXTENDED VALIDATION CHECKS (AVC) — platform-anchored tiers only; never SaaS.
    extended_results = []
    if _extended_checks_enabled():
        console.print("\n◉ Running Additional Validation Checks", style=f"bold {theme.primary}")
        try:
            extended_results = run_extended_validation(captured_data, headless=headless, skip_adapter_check=skip_adapter_check)
        except Exception as exc:
            logger.error("Extended validation checks failed unexpectedly: %s", exc)
            console.print(
                f"  [{theme.warning}]⚠ Additional checks skipped due to unexpected error: {exc}[/{theme.warning}]"
            )

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
    # Tier — preserved from the capture metadata so the report renderer
    # can stamp the Mode badge without re-resolving the active tier
    # (which may have changed since the capture was taken).
    df.attrs["tier"] = user_metadata.get("tier", config.tier)

    if extended_results:
        # Attach extended results as metadata to be used in the Reporting Engine
        df.attrs["extended_results"] = [result.to_dict() for result in extended_results]

    return df


# Rule categories carrying data an additional Kubernetes namespace can
# actually produce — see capture/modules_registry.py's per-target Kubernetes
# wiring: an extra namespace only ever registers "kubernetes" (+ "gateway5"
# for an IAG-role namespace), never Mongo/Redis/Platform (those stay a
# single global connection, not namespace-scoped).
_NAMESPACE_BASE_CATEGORIES = frozenset({"kubernetes"})
_NAMESPACE_ROLE_CATEGORIES = {"iag": frozenset({"gateway5"})}


def validate_multi_target_namespaces(
    ruleset: dict,
    structured_data: dict,
) -> dict[str, dict[str, Any]]:
    """
    Validate each additional Kubernetes namespace captured alongside the
    primary environment (see ``capture_engine._resolve_modules`` — same-role
    or explicitly-namespaced targets are demoted into
    ``structured_data["_multi_target"]`` instead of overwriting the
    canonical capture path). Reuses ``validate()`` unchanged, scoped per
    namespace to just the rule categories that namespace could have
    produced data for.

    Returns ``{}`` when there are no additional namespaces — the default,
    common case. Otherwise keyed by target label:
        {label: {"role", "namespace", "context", "pass_count",
                 "fail_count", "failed_rules": [{"rule_number", "name",
                 "severity", "message"}]}}
    """
    multi_target = structured_data.get("_multi_target") or {}
    if not multi_target:
        return {}

    all_rules = ruleset.get("rules", [])
    results: dict[str, dict[str, Any]] = {}

    for label, entry in multi_target.items():
        role = entry.get("role", "")
        categories = _NAMESPACE_BASE_CATEGORIES | _NAMESPACE_ROLE_CATEGORIES.get(role, frozenset())

        subset_rules = [r for r in all_rules if r.get("category", "") in categories]
        if not subset_rules:
            continue

        subset_ruleset = {**ruleset, "rules": subset_rules}
        df = validate(subset_ruleset, entry.get("data", {}) or {}, headless=True)

        if df.empty or "status" not in df.columns:
            pass_count = fail_count = 0
            failed_rules: list[dict[str, str]] = []
        else:
            pass_count = int((df["status"] == ValidationStatus.PASS).sum())
            fail_count = int((df["status"] == ValidationStatus.FAIL).sum())
            failed_rules = [
                {
                    "rule_number": row.get("rule_number", ""),
                    "name": row.get("name", ""),
                    "severity": row.get("severity", ""),
                    "message": row.get("recommendations", ""),
                }
                for row in df[df["status"] == ValidationStatus.FAIL].to_dict("records")
            ]

        results[label] = {
            "role": role,
            "namespace": entry.get("namespace", ""),
            "context": entry.get("context", ""),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "failed_rules": failed_rules,
        }

    return results
