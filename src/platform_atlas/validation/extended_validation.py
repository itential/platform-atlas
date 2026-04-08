"""
ATLAS // Extended Validation

Automatically runs validation checks outside the standard ruleset structure.
No user interaction required - just register checks and they'll run automatically.

Architecture
────────────
Every check function is decorated with @check(...) which registers it and
adds a CheckContext as the second argument. The context carries the
check's metadata (id, name, category) and exposes builder methods
(.skip, .passed, .warn, .fail, .info) so individual checks never have to
manually construct ExtendedCheckResult objects.

Adding a new check
──────────────────
    @check("my_check_id", name="My Check Name", category=CheckCategory.HEALTH)
    def check_something(data: dict, chk: CheckContext) -> ExtendedCheckResult:
        items = chk.require(data, "some_key", "widget")  # returns data or raises _Skip
        issues = chk.scan(items, inspector_func)          # common scan pattern
        return chk.report(issues, pass_msg="All widgets healthy",
                          remediation="Fix broken widgets")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum, auto

from urllib.parse import urlparse

from packaging.version import Version

from rich.console import Console

from platform_atlas.core import ui
from platform_atlas.validation.utils import get_latest_version

logger = logging.getLogger(__name__)

ExtendedStatus = Literal["PASS", "WARN", "FAIL", "INFO", "SKIP"]

_OFFLINE_STATES = frozenset({"STOPPED", "OFFLINE", "DEAD"})
_VERBOSE_LOG_LEVELS = frozenset({"debug", "trace"})
_IAG4_DEFAULT_PATHS: dict[str, list[str]] = {
    "module_path": [
        "/usr/local/lib/python3.9/site-packages/ansible/modules/network",
        "/usr/local/lib/python3.9/site-packages/ansible_collections",
        "/home/itential/.local/lib/python3.9/site-packages/ansible/modules/network",
        "/home/itential/.local/lib/python3.9/site-packages/ansible_collections",
        "/home/itential/automation-gateway/lib/python3.9/site-packages/ansible/modules/network",
        "/home/itential/automation-gateway/lib/python3.9/site-packages/ansible_collections",
        "/usr/share/automation-gateway/ansible/modules",
    ],
    "collection_path": [
        "/opt/automation-gateway/.ansible/collections",
        "/usr/share/automation-gateway/ansible/collections",
    ],
    "role_path": [
        "/opt/automation-gateway/.ansible/roles",
        "/usr/share/automation-gateway/ansible/roles",
    ],
}

console = Console()
theme = ui.theme

class CheckCategory(Enum):
    """Categories for extended validation checks."""
    VERSION = auto()
    DEPENDENCY = auto()
    SECURITY = auto()
    PERFORMANCE = auto()
    CONFIGURATION = auto()
    AUTHENTICATION = auto()
    HEALTH = auto()
    LOGS = auto()
    CUSTOM = auto()


@dataclass(frozen=True, slots=True)
class ExtendedCheckResult:
    """Immutable result of a single extended validation check."""
    check_id: str
    name: str
    category: CheckCategory
    status: ExtendedStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for DataFrame/reporting (Parquet-safe)."""
        return {
            "check_id": self.check_id,
            "name": self.name,
            "category": str(self.category.name.lower()),
            "status": str(self.status),
            "message": self.message,
            "details": self.details,
            "remediation": self.remediation,
        }


class _SkipCheck(Exception):
    """Internal signal: check should be skipped (not an error)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

@dataclass(frozen=True, slots=True)
class CheckContext:
    """
    Builder added into every check function by the @check decorator.

    Carries the check's metadata so individual functions never have to
    repeat their check_id, name, or category in return statements.

    Usage inside a check function:
        items = chk.require(data, "adapters.state", "adapter state")
        return chk.passed("All adapters healthy")
        return chk.warn("3 adapters offline", details={...}, remediation="...")
    """
    check_id: str
    name: str
    category: CheckCategory
    requires: tuple[str, ...] = ()

    # ── Result builders ────────────────────────────────────────

    def _result(
        self,
        status: ExtendedStatus,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        """Base builder — all public methods delegate here."""
        return ExtendedCheckResult(
            check_id=self.check_id,
            name=self.name,
            category=self.category,
            status=status,
            message=message,
            details=details or {},
            remediation=remediation,
        )

    def skip(self, message: str) -> ExtendedCheckResult:
        return self._result("SKIP", message)

    def passed(self, message: str, details: dict[str, Any] | None = None) -> ExtendedCheckResult:
        return self._result("PASS", f"✓ {message}", details)

    def info(self, message: str, details: dict[str, Any] | None = None) -> ExtendedCheckResult:
        return self._result("INFO", message, details)

    def warn(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        return self._result("WARN", message, details, remediation)

    def fail(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        return self._result("FAIL", message, details, remediation)

    # ── Data helpers ───────────────────────────────────────────

    def require(self, data: dict, key: str, label: str) -> dict | list:
        """
        Extract data by dotted key path or raise _SkipCheck if missing/empty.

        Supports dotted paths like "adapters.versions" to navigate the
        nested capture hierarchy. Single keys like "platform" still work.
        """
        value = data
        for part in key.split("."):
            if not isinstance(value, dict):
                raise _SkipCheck(f"No {label} data found in capture data")
            value = value.get(part)
            if value is None:
                raise _SkipCheck(f"No {label} data found in capture data")
        if not value:
            raise _SkipCheck(f"No {label} data found in capture data")
        return value

    def scan(
        self,
        items: dict[str, Any],
        inspector: Callable[[str, Any], str | None],
    ) -> dict[str, str]:
        """
        Run *inspector(name, item_data)* on each item in the dict.

        The inspector returns a problem description string if there's
        an issue, or None if the item is fine. Returns a dict of
        {name: problem} for all items that had issues.

        This replaces the repetitive for-loop + issues dict pattern
        that currently appears in most check functions.
        """
        issues: dict[str, str] = {}
        for name, item_data in items.items():
            problem = inspector(name, item_data)
            if problem:
                issues[name] = problem
        return issues

    def report(
        self,
        issues: dict[str, str],
        *,
        pass_msg: str,
        warn_msg: str | None = None,
        fail_threshold: float | None = None,
        total: int | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        """
        Convert a scan result into a PASS / WARN / FAIL result.

        Parameters:
            issues:         Dict of {name: problem_description} from scan()
            pass_msg:       Message when no issues found
            warn_msg:       Message template when issues found. If None, auto-
                            generates "{count} item(s) with issues".
            fail_threshold: If set, issues exceeding this fraction of total
                            escalate from WARN to FAIL (e.g., 0.5 = 50%)
            total:          Total item count for threshold calc (defaults to
                            len(issues) which makes threshold meaningless,
                            so pass the real total when using fail_threshold)
            remediation:    Remediation text attached to WARN/FAIL results
        """
        if not issues:
            return self.passed(pass_msg)

        count = len(issues)
        message = warn_msg or f"{count} item(s) with issues"

        status: ExtendedStatus = "WARN"
        if fail_threshold is not None and total:
            if count >= total * fail_threshold:
                status = "FAIL"

        return self._result(status, message, issues, remediation)


# =================================================
# Registry + @check decorator
# =================================================

# Type alias for the raw check function signature
CheckFn = Callable[[dict, CheckContext], ExtendedCheckResult]


@dataclass
class ExtendedValidationRegistry:
    """Registry of extended validation checks."""
    _checks: dict[str, tuple[CheckFn, CheckContext]] = field(
        default_factory=dict,
    )

    def register(
        self,
        check_id: str,
        *,
        name: str | None = None,
        category: CheckCategory = CheckCategory.CUSTOM,
        requires: tuple[str, ...] = (),
    ) -> Callable[[CheckFn], CheckFn]:
        """
        Decorator to register an extended validation check.

        The decorated function receives (data, chk) where chk is a
        pre-built CheckContext carrying the check's metadata.
        """
        display_name = name or check_id.replace("_", " ").title()
        chk = CheckContext(
            check_id=check_id,
            name=display_name,
            category=category,
            requires=requires,
        )

        def decorator(func: CheckFn) -> CheckFn:
            self._checks[check_id] = (func, chk)
            return func
        return decorator

    def execute_all(self, data: dict) -> list[ExtendedCheckResult]:
        """Execute all registered checks, catching exceptions per-check."""
        results: list[ExtendedCheckResult] = []

        for check_id, (check_func, chk) in self._checks.items():
            # Silently skip checks whose data requirements aren't met
            if chk.requires and not self._requirements_met(data, chk.requires):
                logger.debug("Skipping '%s': required data not present", check_id)
                continue

            label = check_id.replace("_", " ").title()
            console.print(f"  ▶ {label} Check...", style=f"bold {theme.secondary}")
            try:
                results.append(check_func(data, chk))
            except _SkipCheck as skip:
                results.append(chk.skip(skip.message))
            except Exception as exc:
                logger.error("Extended check '%s' failed: %s", check_id, exc)
                results.append(chk.fail(
                    f"Check error: {type(exc).__name__}: {exc}",
                    remediation="This check encountered an unexpected error. Review the Atlas log for details."
                ))
        return results

    @staticmethod
    def _requirements_met(data: dict, requires: tuple[str, ...]) -> bool:
        """Check that all required dotted paths exists and are non-empty"""
        for path in requires:
            current = data
            for part in path.split("."):
                if not isinstance(current, dict):
                    return False
                current = current.get(part)
                if current is None:
                    return False
            if not current:
                return False
        return True

    @property
    def check_ids(self) -> list[str]:
        return list(self._checks.keys())

    def __len__(self) -> int:
        return len(self._checks)

    def __repr__(self) -> str:
        return f"<ExtendedValidationRegistry checks={len(self)}>"


# Global registry instance
_registry = ExtendedValidationRegistry()


def check(
    check_id: str,
    *,
    name: str | None = None,
    category: CheckCategory = CheckCategory.CUSTOM,
    requires: tuple[str, ...] = (),
) -> Callable[[CheckFn], CheckFn]:
    """
    Module-level shortcut for @_registry.register(...).

    Usage:
        @check("adapter_versions", name="Adapter Version Check", category=CheckCategory.VERSION)
        def check_adapter_versions(data: dict, chk: CheckContext) -> ExtendedCheckResult:
            ...
    """
    return _registry.register(
        check_id, name=name, category=category, requires=requires,
    )


def get_registry() -> ExtendedValidationRegistry:
    return _registry


# =================================================
# Shared inspectors (reusable lambdas / small functions for scan())
# =================================================

def _inspect_health_state(_name: str, state_info: dict) -> str | None:
    """Flag items whose state or connection_state is in _OFFLINE_STATES."""
    bad = [
        str(state_info.get(k, "")).upper()
        for k in ("state", "connection_state")
        if str(state_info.get(k, "")).upper() in _OFFLINE_STATES
    ]
    return ", ".join(bad) if bad else None


def _inspect_verbose_logging(_name: str, config: dict) -> str | None:
    """Flag adapters with debug/trace logging on console or file."""
    bad = [
        str(config.get(k, "")).lower()
        for k in ("console", "file")
        if str(config.get(k, "")).lower() in _VERBOSE_LOG_LEVELS
    ]
    return ", ".join(bad) if bad else None


# =================================================
# Built-in checks
# =================================================

@check("adapter_versions", name="Adapter Version Check", category=CheckCategory.VERSION)
def check_adapter_versions(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if installed adapter versions are up-to-date against Gitlab."""
    adapter_versions = chk.require(data, "adapters.versions", "adapter version")
    outdated, up_to_date, failed = [], [], []

    def _check_one(name: str, installed_str: str):
        installed = Version(installed_str)
        latest = Version(get_latest_version(name))
        return name, installed, latest

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_check_one, name, ver): name
            for name, ver in adapter_versions.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                _, installed, latest = future.result()
                if installed < latest:
                    outdated.append({"adapter": name, "installed": str(installed), "latest": str(latest)})
                else:
                    up_to_date.append(name)
            except Exception as e:
                logger.debug("Could not check %s: %s", name, e)
                failed.append(name)

    details = {"outdated": outdated, "up_to_date": up_to_date, "failed": failed}

    if outdated:
        total = len(adapter_versions)
        status: ExtendedStatus = "WARN" if len(outdated) < total / 2 else "FAIL"
        return chk._result(status, f"{len(outdated)} adapter(s) outdated",
                           details, "Update outdated adapters to the latest versions")

    if failed:
        return chk.info(f"Unable to verify {len(failed)} adapter(s)", details)

    return chk.passed("All adapters are up-to-date", details)


@check("adapter_logger_levels", name="Adapter Logger Levels", category=CheckCategory.CONFIGURATION)
def check_adapter_logger_levels(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if any adapters have verbose logging (debug/trace) enabled."""
    log_levels = chk.require(data, "adapters.loggers", "adapter logger")
    issues = chk.scan(log_levels, _inspect_verbose_logging)
    return chk.report(
        issues,
        pass_msg="All adapters using appropriate logging levels",
        warn_msg=f"{len(issues)} adapter(s) using verbose logging",
        remediation=(
            "Verbose logging (debug/trace) can significantly impact "
            "performance and disk space in Production."
        ),
    )


@check("adapter_states", name="Adapter States", category=CheckCategory.HEALTH)
def check_adapter_states(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if any adapters are stopped or offline."""
    states = chk.require(data, "adapters.states", "adapter state")
    issues = chk.scan(states, _inspect_health_state)
    return chk.report(
        issues,
        pass_msg="All adapters running and online",
        warn_msg=f"{len(issues)} adapter(s) stopped or offline",
        remediation="Review stopped and offline adapters in the platform",
    )


@check("application_states", name="Application States", category=CheckCategory.HEALTH)
def check_application_states(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if any applications are stopped or offline."""
    states = chk.require(data, "applications.states", "application state")
    issues = chk.scan(states, _inspect_health_state)
    return chk.report(
        issues,
        pass_msg="All applications running and online",
        warn_msg=f"{len(issues)} application(s) stopped or offline",
        remediation="Review stopped and offline applications in the platform",
    )


@check("adapter_file_data", name="Adapter File Data", category=CheckCategory.CONFIGURATION)
def check_adapter_file_data(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check for duplicate log filenames and undersized log files."""
    filenames = chk.require(data, "adapters.filedata", "adapter file info")
    expected_size = 1_048_576  # 1 MB

    # Build reverse map: filename -> list of adapters sharing it
    owners: dict[str, list[str]] = {}
    for adapter, cfg in filenames.items():
        owners.setdefault(cfg["filename"], []).append(adapter)
    duplicates = {name for name, owning in owners.items() if len(owning) > 1}

    def _inspect(_name: str, cfg: dict) -> str | None:
        problems = []
        if cfg["filename"] in duplicates:
            shared = [a for a in owners[cfg["filename"]] if a != _name]
            problems.append(f"Duplicate filename '{cfg['filename']}' (shared with: {', '.join(shared)})")
        if cfg["filesize"] < expected_size:
            problems.append(f"Log size {cfg['filesize']:,} bytes is below expected {expected_size:,}")
        return "; ".join(problems) if problems else None

    issues = chk.scan(filenames, _inspect)
    return chk.report(
        issues,
        pass_msg="All adapter log files are unique and sizes are correct",
        warn_msg=f"{len(issues)} adapter(s) with log file issues",
        remediation=(
            "Duplicate log filenames can cause adapters to overwrite each "
            "other's logs. Undersized log files may indicate the log rotation "
            "size is set too low, which can result in lost diagnostic data."
        ),
    )


@check("adapter_health_data", name="Adapter Health Data", category=CheckCategory.HEALTH)
def check_adapter_health_data(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check adapter healthcheck configuration."""
    health_data = chk.require(data, "adapters.health", "adapter health info")

    min_freq = 600_000    # 10 minutes in ms
    max_freq = 1_800_000  # 30 minutes in ms

    def _inspect(_name: str, cfg: dict) -> str | None:
        hc_type = cfg.get("healthcheck_type", "none").lower()
        freq = cfg.get("healthcheck_frequency", 0)

        if hc_type == "none":
            return "Healthcheck is disabled"
        if hc_type == "intermittent":
            if freq < min_freq:
                return f"Healthcheck frequency {freq:,}ms is too low (minimum: {min_freq:,}ms / 10 min)"
            if freq > max_freq:
                return f"Healthcheck frequency {freq:,}ms is too high (maximum: {max_freq:,}ms / 30 min)"
        return None

    issues = chk.scan(health_data, _inspect)
    return chk.report(
        issues,
        pass_msg="All adapter healthchecks are properly configured",
        warn_msg=f"{len(issues)} adapter(s) with healthcheck issues",
        remediation=(
            "Adapters with healthchecks disabled will not be monitored for "
            "connectivity issues. Intermittent healthcheck frequency should "
            "be between 10 and 30 minutes (600000-1800000ms) to balance "
            "monitoring coverage with system resource usage."
        ),
    )

@check("adapter_brokers", name="Adapter Brokers", category=CheckCategory.AUTHENTICATION)
def check_adapter_brokers(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check adapter broker configuration."""
    broker_data = chk.require(data, "adapters.brokers", "adapter broker info")

    def _inspect(_name: str, cfg: dict) -> str | None:
        if _name == "adapter-local_aaa" and "aaa" in cfg.get("brokers", []):
            return "LocalAAA adapter is being used for Authentication"
        return None

    issues = chk.scan(broker_data, _inspect)
    return chk.report(
        issues,
        pass_msg="No LocalAAA authentication being used",
        warn_msg="LocalAAA Authentication is being used",
        remediation=(
            "LocalAAA Authentication should not be used in production, and "
            "recommend this be changed to a more secure authentication method."
        ),
    )

@check("adapter_throttling", name="Adapter Throttling", category=CheckCategory.PERFORMANCE)
def check_adapter_throttle(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check adapter throttle configuration."""
    broker_data = chk.require(data, "adapters.throttle", "adapter throttle info")

    def _inspect(_name: str, cfg: dict) -> str | None:
        throttle_enabled = cfg.get('throttle_enabled', False)
        if throttle_enabled:
            return f"Adapter '{_name}' has throttling enabled"
        return None

    issues = chk.scan(broker_data, _inspect)
    return chk.report(
        issues,
        pass_msg="No Adapter Throttling is being used",
        warn_msg=f"{len(issues)} adapter(s) have throttle enabled",
        remediation=(
            "Adapter throttling should be disabled and only ever used "
            "under specific situations that may need it."
        ),
    )

@check("adapter_timeouts", name="Adapter Timeouts", category=CheckCategory.PERFORMANCE)
def check_adapter_timeouts(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check adapter request timeout configuration."""
    timeout_data = chk.require(data, "adapters.requests", "adapter timeout info")

    def _inspect(_name: str, cfg: dict) -> str | None:
        if cfg.get("attempt_timeout", 0) <= 5000:
            return f"Adapter '{_name}' has an attempt_timeout value of 5 seconds or less"
        return None

    issues = chk.scan(timeout_data, _inspect)
    return chk.report(
        issues,
        pass_msg="No Adapter Request Timeout Issues",
        warn_msg=f"{len(issues)} adapter(s) have attempt_timeouts lower than 5 seconds",
        remediation=(
            "Low attempt_timeout values can cause issues with adapters not returning "
            "data and causing frequent timeout issues. Please consider raising this value."
        ),
    )

@check("adapter_limit_errors", name="Adapter Limit Errors", category=CheckCategory.CONFIGURATION)
def check_adapter_limit_errors(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check adapter limit errors configuration."""
    limit_retry_data = chk.require(data, "adapters.limit_errors", "adapter limit errors info")

    def _inspect(_name: str, cfg: dict) -> str | None:
        retry_data = cfg.get("limit_retry_error", [])

        # Normalize to a list of strings for comparison
        if not isinstance(retry_data, list):
            retry_data = [retry_data]
        actual = [str(x) for x in retry_data]

        required = ["500-599", "409", "408", "418"]
        missing = [v for v in required if v not in actual]

        if missing:
            return f"Adapter {_name} is missing required values: {', '.join(missing)} (has: {actual})"
        return None

    issues = chk.scan(limit_retry_data, _inspect)
    return chk.report(
        issues,
        pass_msg="Adapter Limit Retry Errors is good",
        warn_msg=f"{len(issues)} adapter(s) have incorrect settings for limit_retry_error",
        remediation=(
            "Adapters should re-attempt connections for some errors."
        ),
    )

@check("redis_acl", name="Redis ACL", category=CheckCategory.AUTHENTICATION)
def check_redis_acl(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check redis acl configuration."""
    redis_acl_data = chk.require(data, "redis.acl", "redis acl info")

    # Normalize ACL data — the automated collector returns a list of lists
    # (one per user), but manual collection can produce a single flat list
    # with all users' entries concatenated together.
    acl_by_user = _parse_acl_entries(redis_acl_data)

    if not acl_by_user:
        return chk.skip("Could not parse Redis ACL data — unexpected format")

    EXPECTED_ACLS = {
        "itential": {
            "~*", "&*", "-@all", "+@read", "+@write", "+@stream",
            "+@transaction", "+@sortedset", "+@list", "+@hash", "+@string",
            "+@fast", "+@scripting", "+@connection", "+@pubsub",
            "+script|load", "+script|exists", "-script|flush",
            "-flushall", "-flushdb", "-save", "-bgsave",
            "-bgrewriteaof", "-replicaof", "-psync", "-replconf",
            "-shutdown", "-failover", "-cluster", "-asking", "-sync",
            "-readonly", "-readwrite", "+info", "+role",
        },
        "repluser": {
            "&*", "-@all", "+psync", "+replconf", "+ping"
        },
        "sentineluser": {
            "&*", "-@all", "+slaveof", "+ping", "+info", "+role",
            "+publish", "+subscribe", "+psubscribe", "+punsubscribe",
            "+client|setname", "+client|kill", "+multi", "+exec",
            "+replicaof", "+script|kill", "+config|rewrite"
        }
    }

    def _inspect(_name: str, acl_entry: list) -> str | None:
        username = str(acl_entry[0]).lower()
        expected = EXPECTED_ACLS.get(username)

        # Skip users we don't have rules for
        if expected is None:
            return None

        actual_tokens = {str(t) for t in acl_entry}
        missing = expected - actual_tokens

        if missing:
            return f"{username}: missing {', '.join(sorted(missing))}"

    issues = chk.scan(acl_by_user, _inspect)

    return chk.report(
        issues,
        pass_msg="No Redis ACL Issues",
        warn_msg=f"{len(issues)} users(s) have invalid Redis ACL settings",
        remediation=(
            "Please adjust ACL settings for users in Redis"
        ),
    )


# ACL token prefixes — anything starting with these is a permission
# or pattern, not a username.
_ACL_TOKEN_PREFIXES = ("+", "-", "~", "&", "#", ">", "(")

def _parse_acl_entries(acl_data: list) -> dict[str, list]:
    """Normalize Redis ACL data into {username: [tokens...]}.

    Handles two shapes:
      - Proper: [[user1, on, ...], [user2, on, ...]]  (list of lists)
      - Flat:   [user1, true, #hash, &*, +cmd, ..., user2, true, ...]

    In the flat format, booleans (true/false) represent on/off flags,
    and user boundaries are detected by finding string tokens that
    don't start with ACL permission prefixes (+, -, ~, &, #, >).
    """
    if not acl_data:
        return {}

    # Already structured — list of lists
    if isinstance(acl_data[0], (list, tuple)):
        return {
            str(entry[0]).lower(): entry
            for entry in acl_data
            if isinstance(entry, (list, tuple)) and entry
        }

    # Flat list — re-chunk into per-user sub-lists
    users: dict[str, list] = {}
    current_user: str | None = None
    current_tokens: list = []

    for token in acl_data:
        # Booleans are on/off flags — keep them but they're not usernames
        if isinstance(token, bool):
            if current_user is not None:
                current_tokens.append("on" if token else "off")
            continue

        token_str = str(token)

        # If it's a string that doesn't look like a permission/hash/pattern,
        # it's a new username boundary
        if (isinstance(token, str)
                and token_str
                and not token_str.startswith(_ACL_TOKEN_PREFIXES)):
            # Save the previous user
            if current_user is not None:
                users[current_user] = [current_user] + current_tokens
            current_user = token_str.lower()
            current_tokens = []
        else:
            if current_user is not None:
                current_tokens.append(token_str)

    # Don't forget the last user
    if current_user is not None:
        users[current_user] = [current_user] + current_tokens

    return users

@check("indexes_status", name="Database Index Status", category=CheckCategory.HEALTH)
def check_indexes_status(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if any database collecctions have missing indexes"""
    indexes = chk.require(data, "platform.indexes_status", "index status")

    def _inspect(collection: str, info: dict) -> str | None:
        missing = info.get("missing", [])
        if missing:
            return f"{len(missing)} missing index(es)"
        return None

    issues = chk.scan(indexes, _inspect)
    return chk.report(
        issues,
        pass_msg="All database collections are properly indexed",
        warn_msg=f"{len(issues)} collection(s) with missing indexes",
        total=len(indexes),
        fail_threshold=0.3,
        remediation=(
            "Missing indexes can significantly degrade query performance "
            "and increase database load. Run the platform's index rebuild "
            "in Admin Essentials to resolve this."
        ),
    )

@check("iag4_default_paths", name="IAG4 Default Paths", category=CheckCategory.CONFIGURATION, requires=("gateway4.configured_paths",),)
def check_iag4_default_paths(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check for default paths in Automation Gateway 4"""
    configured = chk.require(data, "gateway4.configured_paths", "Gateway4 path config")

    missing: dict[str, list[str]] = {}
    present: dict[str, list[str]] = {}

    for category, defaults in _IAG4_DEFAULT_PATHS.items():
        actual = configured.get(category, [])
        found = [p for p in defaults if p in actual]
        not_found = [p for p in defaults if p not in actual]

        if found:
            present[category] = found
        if not_found:
            missing[category] = not_found

    details: dict[str, Any] = {}
    if present:
        details["present"] = present
    if missing:
        details["missing"] = missing

    if not missing:
        return chk.passed(
            f"All default paths present across {len(_IAG4_DEFAULT_PATHS)} categories",
            details=details,
        )

    total_missing = sum(len(v) for v in missing.values())
    affected = ", ".join(missing.keys())

    return chk.warn(
        f"{total_missing} default path(s) missing in: {affected}",
        details=details,
        remediation=(
            "Missing default paths in the Gateway4 SQLite config may prevent "
            "Ansible from locating modules, collections, or roles. Review the "
            "Configuration Settings and ensure all expected paths are present."
        ),
    )

@check(
    "platform_log_analysis",
    name="Platform Log Analysis",
    category=CheckCategory.LOGS,
    requires=("platform.log_analysis",),
)
def check_platform_log_analysis(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze platform log error/warning patterns from captured log data.

    Produces two distinct report sections:
      1. Top Repeated Messages — most frequent error/warning log lines
      2. Heuristic Keyword Scan — lines matching known-bad patterns
    """
    log_data = chk.require(data, "platform.log_analysis", "platform log analysis")

    files_parsed = log_data.get("files_parsed", 0)
    groups: dict = log_data.get("groups", {})

    if not groups:
        return chk.passed(
            f"No errors or warnings detected across {files_parsed} log files",
            details={
                "files_parsed": files_parsed,
                "total_lines": 0,
                "total_matched": 0,
                "error_groups": [],
                "heuristic_groups": [],
            },
        )

    total_matched = 0
    total_parsed = 0
    error_groups: list[dict] = []
    heuristic_groups: list[dict] = []

    for group_name, group_info in groups.items():
        matched = group_info.get("total_matched", 0)
        parsed = group_info.get("total_lines_parsed", 0)
        total_matched += matched
        total_parsed += parsed

        # ── Top repeated messages (frequency-ranked) ──
        top = group_info.get("top_messages", [])
        if top:
            error_groups.append({
                "name": group_name,
                "matched": matched,
                "parsed": parsed,
                "top_messages": [
                    {
                        "level": (m["message"].split("]")[0].lstrip("[")
                                  if m["message"].startswith("[") else ""),
                        "message": m["message"][:200],
                        "count": m["count"],
                    }
                    for m in top[:10]
                ],
            })

        # ── Heuristic keyword matches ──
        heuristics = group_info.get("heuristic_matches", [])
        if heuristics:
            # Aggregate by keyword for cleaner presentation
            keyword_counts: dict[str, int] = {}
            keyword_examples: dict[str, list[str]] = {}
            for hit in heuristics:
                for kw in hit.get("keywords", []):
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                    if len(keyword_examples.get(kw, [])) < 2:
                        keyword_examples.setdefault(kw, []).append(
                            hit.get("line", "")[:200]
                        )

            heuristic_groups.append({
                "name": group_name,
                "total_hits": len(heuristics),
                "keywords": [
                    {
                        "keyword": kw,
                        "count": count,
                        "examples": keyword_examples.get(kw, []),
                    }
                    for kw, count in sorted(
                        keyword_counts.items(),
                        key=lambda x: x[1],
                        reverse=True,
                    )[:15]
                ],
            })

    # Worst offenders first
    error_groups.sort(key=lambda g: g["matched"], reverse=True)
    heuristic_groups.sort(key=lambda g: g["total_hits"], reverse=True)

    details = {
        "files_parsed": files_parsed,
        "total_lines": total_parsed,
        "total_matched": total_matched,
        "error_groups": error_groups,
        "heuristic_groups": heuristic_groups,
    }

    group_count = len(error_groups)

    if total_matched == 0:
        return chk.passed(
            f"No errors or warnings detected across {files_parsed} log files",
            details=details,
        )

    message = (
        f"{total_matched:,} error/warning entries across "
        f"{group_count} log group{'s' if group_count != 1 else ''}"
    )

    error_rate = (total_matched / total_parsed * 100) if total_parsed > 0 else 0.0

    if error_rate > 5.0:
        return chk.fail(
            message,
            details=details,
            remediation=(
                "High error rate detected in platform logs. "
                "Review the top error groups above and address recurring "
                "issues — particularly any adapter connectivity or "
                "workflow validation errors."
            ),
        )

    return chk.warn(
        message,
        details=details,
        remediation=(
            "Errors and warnings were found in platform logs. "
            "Review the groups above for recurring patterns that "
            "may indicate configuration or connectivity issues."
        ),
    )

@check(
    "webserver_log_analysis",
    name="Webserver Log Analysis",
    category=CheckCategory.LOGS,
    requires=("platform.webserver_logs",),
)
def check_webserver_logs(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze webserver access logs for performance and auth anomalies."""
    log_data = chk.require(data, "platform.webserver_logs", "webserver logs")

    entries: list[dict] = log_data.get("entries", [])
    if not entries:
        return chk.skip("No webserver log entries to analyze")

    slow_threshold_ms = 5000.0
    error_codes = {"400", "401", "403", "404", "500", "502", "503", "504"}

    slow_requests: list[dict] = []
    error_requests: list[dict] = []
    anonymous_requests: list[dict] = []
    status_counts: dict[str, int] = {}
    total_time = 0.0

    # ── Per-endpoint volume tracking ──
    endpoint_volume: dict[str, dict] = {}

    for entry in entries:
        status = str(entry.get("status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1

        try:
            elapsed = float(entry.get("total_time_ms", 0))
        except (ValueError, TypeError):
            elapsed = 0.0
        total_time += elapsed

        # Track call volume by base path
        full_url = entry.get("url", "unknown")
        base_path = urlparse(full_url).path
        method = entry.get("method", "?")

        if base_path not in endpoint_volume:
            endpoint_volume[base_path] = {
                "count": 0,
                "methods": {},
                "total_ms": 0.0,
                "error_count": 0,
            }
        ep = endpoint_volume[base_path]
        ep["count"] += 1
        ep["methods"][method] = ep["methods"].get(method, 0) + 1
        ep["total_ms"] += elapsed
        if status in error_codes:
            ep["error_count"] += 1

        if elapsed >= slow_threshold_ms:
            slow_requests.append({
                "url": full_url,
                "method": method,
                "total_time_ms": elapsed,
                "status": status,
            })

        if status in error_codes:
            error_requests.append({
                "url": full_url,
                "method": method,
                "status": status,
            })

        user = entry.get("remote_user", "")
        if user.lower() in ("anonymous", "", "-"):
            anonymous_requests.append({
                "url": full_url,
                "method": method,
            })

    total = len(entries)
    avg_time = total_time / total if total else 0.0

    # ── Top-10 endpoints by call volume ──
    top_endpoints = sorted(
        endpoint_volume.items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    )[:10]
    top_endpoints_list = [
        {
            "path": path,
            "count": info["count"],
            "methods": info["methods"],
            "avg_ms": round(info["total_ms"] / info["count"], 1) if info["count"] else 0,
            "error_count": info["error_count"],
        }
        for path, info in top_endpoints
    ]

    # ── Slow endpoints grouped by base path ──
    slow_by_path: dict[str, dict] = {}

    for req in slow_requests:
        full_url = req["url"]
        base_path = urlparse(full_url).path

        if base_path not in slow_by_path:
            slow_by_path[base_path] = {
                "worst_ms": req["total_time_ms"],
                "count": 0,
                "examples": [],
            }

        group = slow_by_path[base_path]
        group["count"] += 1
        group["worst_ms"] = max(group["worst_ms"], req["total_time_ms"])

        if len(group["examples"]) < 3:
            group["examples"].append({
                "url": full_url,
                "total_time_ms": req["total_time_ms"],
                "method": req["method"],
            })

    details = {
        "total_requests": total,
        "avg_response_ms": round(avg_time, 2),
        "status_distribution": status_counts,
        "top_endpoints": top_endpoints_list,
        "slow_requests_count": len(slow_requests),
        "slow_endpoints": dict(sorted(
            slow_by_path.items(), key=lambda x: x[1]["worst_ms"], reverse=True
        )[:10]),
        "error_count": len(error_requests),
        "anonymous_count": len(anonymous_requests),
    }

    issues: list[str] = []

    error_rate = len(error_requests) / total * 100 if total else 0
    if error_rate > 5.0:
        issues.append(f"High error rate: {error_rate:.1f}%")

    if len(slow_requests) > total * 0.1:
        issues.append(
            f"{len(slow_requests)} requests exceeded {slow_threshold_ms}ms "
            f"({len(slow_requests)/total*100:.1f}%)"
        )

    if not issues:
        return chk.passed(
            f"Webserver healthy — {total} requests, avg {avg_time:.1f}ms",
            details=details,
        )

    severity: ExtendedStatus = "FAIL" if error_rate > 15.0 else "WARN"
    return chk._result(
        severity,
        f"{len(issues)} issue(s): {'; '.join(issues)}",
        details=details,
        remediation=(
            "Review slow endpoints for optimization opportunities. "
            "High error rates may indicate misconfigured routes or "
            "upstream service issues."
        ),
    )


@check(
    "mongo_log_analysis",
    name="MongoDB Log Analysis",
    category=CheckCategory.LOGS,
    requires=("mongo.log_analysis",),
)
def check_mongo_log_analysis(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze MongoDB log error/warning patterns from captured log data.

    Produces two distinct report sections:
      1. Top Repeated Messages — most frequent error/warning log lines
      2. Heuristic Keyword Scan — lines matching known-bad patterns
    """
    log_data = chk.require(data, "mongo.log_analysis", "MongoDB log analysis")

    lines_read = log_data.get("lines_read", 0)
    lines_matched = log_data.get("lines_matched", 0)

    if lines_read == 0:
        return chk.skip("No MongoDB log data available")

    if lines_matched == 0:
        return chk.passed(
            f"No errors or warnings detected in MongoDB logs ({lines_read:,} lines scanned)",
            details={
                "files_parsed": 1,
                "total_lines": lines_read,
                "total_matched": 0,
                "error_groups": [],
                "heuristic_groups": [],
            },
        )

    # ── Top repeated messages → error_groups format ──
    top_messages = log_data.get("top_messages", [])
    error_groups: list[dict] = []

    if top_messages:
        error_groups.append({
            "name": "mongod",
            "matched": lines_matched,
            "parsed": lines_read,
            "top_messages": [
                {
                    "level": (m["message"].split("]")[0].lstrip("[")
                              if m["message"].startswith("[") else ""),
                    "message": m["message"][:200],
                    "count": m["count"],
                }
                for m in top_messages[:10]
            ],
        })

    # ── Heuristic keyword matches → heuristic_groups format ──
    heuristic_matches = log_data.get("heuristic_matches", [])
    heuristic_groups: list[dict] = []

    if heuristic_matches:
        keyword_counts: dict[str, int] = {}
        keyword_examples: dict[str, list[str]] = {}

        for hit in heuristic_matches:
            for kw in hit.get("keywords", []):
                keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                if len(keyword_examples.get(kw, [])) < 2:
                    keyword_examples.setdefault(kw, []).append(
                        hit.get("message", "")[:200]
                    )

        heuristic_groups.append({
            "name": "mongod",
            "total_hits": len(heuristic_matches),
            "keywords": [
                {
                    "keyword": kw,
                    "count": count,
                    "examples": keyword_examples.get(kw, []),
                }
                for kw, count in sorted(
                    keyword_counts.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )[:15]
            ],
        })

    details = {
        "files_parsed": 1,
        "total_lines": lines_read,
        "total_matched": lines_matched,
        "error_groups": error_groups,
        "heuristic_groups": heuristic_groups,
    }

    message = f"{lines_matched:,} error/warning entries in MongoDB logs"

    error_rate = (lines_matched / lines_read * 100) if lines_read > 0 else 0.0

    if error_rate > 5.0:
        return chk.fail(
            message,
            details=details,
            remediation=(
                "High error rate detected in MongoDB logs. "
                "Review the top messages above — particularly any "
                "replication, storage, or authentication errors that "
                "may indicate cluster health issues."
            ),
        )

    return chk.warn(
        message,
        details=details,
        remediation=(
            "Errors and warnings were found in MongoDB logs. "
            "Review the messages above for recurring patterns such as "
            "slow queries, replication lag, or connection issues."
        ),
    )


# Main Entrypoint
def run_extended_validation(capture_data: dict) -> list[ExtendedCheckResult]:
    """Execute all registered extended validation checks."""
    return get_registry().execute_all(capture_data)
