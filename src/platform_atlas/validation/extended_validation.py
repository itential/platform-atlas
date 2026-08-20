"""
ATLAS // Extended Validation

Automatically runs validation checks outside the standard ruleset structure.
No user interaction required - just register checks and they'll run automatically.

Architecture
────────────
This module is the plugin framework only: the @check decorator, the
CheckContext builder, ExtendedCheckResult, and the registry. The checks
themselves live one-per-file in validation/extended_checks/, which is
auto-discovered on import (see the trigger import below) — dropping a new
file into that package registers a check, no index to maintain here.

Adding a new check
──────────────────
Create validation/extended_checks/<check_id>.py:

    from platform_atlas.validation.extended_validation import (
        check, CheckContext, ExtendedCheckResult, CheckCategory, CheckGroup,
    )

    @check("my_check_id", name="My Check Name", category=CheckCategory.HEALTH,
           group=CheckGroup.PLATFORM_CORE)
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
from enum import Enum, auto

from rich.console import Console

from platform_atlas.core import ui

logger = logging.getLogger(__name__)

ExtendedStatus = Literal["PASS", "WARN", "FAIL", "INFO", "SKIP"]

_OFFLINE_STATES = frozenset({"STOPPED", "OFFLINE", "DEAD"})

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


class CheckGroup(Enum):
    """Subsystem groupings for the `config edit` "Additional Validation
    Modules" picker (and its WebUI equivalent) — a different axis than
    CheckCategory. CheckCategory tags what KIND of check it is (health,
    performance, ...); CheckGroup tags what subsystem it's ABOUT, which is
    what a user turning checks on/off actually thinks in terms of. E.g. the
    9 "adapter_*" checks share this one group despite being scattered across
    5 different CheckCategory values.

    Declaration order here is the display order in the picker.
    """
    ADAPTERS = "Platform Adapters"
    APPLICATIONS = "Platform Applications"
    PLATFORM_CORE = "Platform Core"
    LOGGING = "Logging"
    GATEWAY = "Itential Gateway"
    MONGODB = "MongoDB"
    REDIS = "Redis"


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
    # True when this SKIP exists because the user turned the check off via
    # `config edit` (Advanced > Additional Validation Modules) / the WebUI
    # equivalent — distinguishes "user deactivated" from "couldn't connect"
    # or "not applicable to this tier" in the report.
    deactivated: bool = False

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
            "deactivated": self.deactivated,
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
    group: CheckGroup
    requires: tuple[str, ...] = ()

    # ── Result builders ────────────────────────────────────────

    def result(
        self,
        status: ExtendedStatus,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
        deactivated: bool = False,
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
            deactivated=deactivated,
        )

    def skip(self, message: str, deactivated: bool = False) -> ExtendedCheckResult:
        """Build a SKIP result (check not applicable / no data / user-deactivated)."""
        return self.result("SKIP", message, deactivated=deactivated)

    def passed(self, message: str, details: dict[str, Any] | None = None) -> ExtendedCheckResult:
        """Build a PASS result with an optional details payload."""
        return self.result("PASS", f"✓ {message}", details)

    def info(self, message: str, details: dict[str, Any] | None = None) -> ExtendedCheckResult:
        """Build an informational (INFO) result — neither pass nor fail."""
        return self.result("INFO", message, details)

    def warn(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        """Build a WARN result with optional details and remediation guidance."""
        return self.result("WARN", message, details, remediation)

    def fail(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str = "",
    ) -> ExtendedCheckResult:
        """Build a FAIL result with optional details and remediation guidance."""
        return self.result("FAIL", message, details, remediation)

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

        return self.result(status, message, issues, remediation)


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
        group: CheckGroup,
        requires: tuple[str, ...] = (),
    ) -> Callable[[CheckFn], CheckFn]:
        """
        Decorator to register an extended validation check.

        The decorated function receives (data, chk) where chk is a
        pre-built CheckContext carrying the check's metadata. ``group`` has
        no default — every check must declare its subsystem grouping
        explicitly (see CheckGroup) so the `config edit` picker never has an
        uncategorized check to fall back on as the check count grows.
        """
        display_name = name or check_id.replace("_", " ").title()
        chk = CheckContext(
            check_id=check_id,
            name=display_name,
            category=category,
            group=group,
            requires=requires,
        )

        def decorator(func: CheckFn) -> CheckFn:
            self._checks[check_id] = (func, chk)
            return func
        return decorator

    def execute_all(
        self,
        data: dict,
        *,
        tier: str = "extended",
        headless: bool = False,
        skip_checks: set[str] | None = None,
        deactivated_checks: set[str] | None = None,
    ) -> list[ExtendedCheckResult]:
        """
        Execute all registered checks, catching exceptions per-check.

        Tier-aware behavior for checks whose ``requires`` paths aren't
        satisfied by the capture data:

        - **Standard**: silent continue — out-of-tier checks don't exist.
          They never appear in the report total or the skipped UI.
        - **Extended**: surface as SKIP — the data was expected but missing
          (e.g., Mongo URI configured but pymongo couldn't connect).

        ``skip_checks`` is an optional set of check IDs to omit entirely
        (no result emitted — they are treated as if they don't exist).

        ``deactivated_checks`` is a separate optional set of check IDs the
        user explicitly turned off via `config edit` / the WebUI equivalent.
        Unlike ``skip_checks``, these DO emit a result — a SKIP tagged
        ``deactivated=True`` — so the report can show "Module Deactivated"
        instead of the check silently vanishing.
        """
        results: list[ExtendedCheckResult] = []
        _skip = skip_checks or set()
        _deactivated = deactivated_checks or set()

        for check_id, (check_func, chk) in self._checks.items():
            if check_id in _skip:
                logger.debug("Skipping extended check '%s' (explicitly excluded)", check_id)
                continue
            if check_id in _deactivated:
                logger.debug("Extended check '%s' deactivated by user config", check_id)
                results.append(chk.skip(
                    "Module deactivated by user in Atlas config", deactivated=True
                ))
                continue
            if chk.requires and not self._requirements_met(data, chk.requires):
                if tier == "standard":
                    # Standard: out-of-tier check, silently filter
                    logger.debug(
                        "Standard tier: '%s' filtered (requires=%s)",
                        check_id, chk.requires,
                    )
                    continue
                # Extended: data expected but missing — emit SKIP
                logger.debug(
                    "Extended tier: '%s' skipped (requires=%s, data missing)",
                    check_id, chk.requires,
                )
                results.append(chk.skip(
                    f"Required capture data not present: {', '.join(chk.requires)}"
                ))
                continue

            if not headless:
                console.print(f"  ▶ {chk.name}...", style=f"bold {theme.secondary}")
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
        """Return the ids of every registered check, in registration order."""
        return list(self._checks.keys())

    def list_checks(self) -> list[tuple[str, str, "CheckCategory"]]:
        """Return (check_id, name, category) for every registered check.

        Registration order — the same order ``check_ids`` returns. Used by
        the CLI's ``config edit`` Advanced menu and the WebUI's AVC-modules
        page to render a labeled, categorized checkbox list without either
        surface hardcoding check names.
        """
        return [(check_id, chk.name, chk.category) for check_id, (_, chk) in self._checks.items()]

    def list_checks_grouped(self) -> list[tuple[str, str, "CheckGroup"]]:
        """Return (check_id, name, group) for every registered check,
        ordered by CheckGroup declaration order and then by registration
        order within each group.

        Used by the CLI's `config edit` Advanced menu (and the WebUI's AVC-
        modules page) to render the collapsible, grouped checkbox picker.
        Separate from ``list_checks()`` (which stays category-based, for
        back-compat with any existing caller of that exact shape).
        """
        order = {group: i for i, group in enumerate(CheckGroup)}
        return sorted(
            ((check_id, chk.name, chk.group) for check_id, (_, chk) in self._checks.items()),
            key=lambda item: order[item[2]],
        )

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
    group: CheckGroup,
    requires: tuple[str, ...] = (),
) -> Callable[[CheckFn], CheckFn]:
    """
    Module-level shortcut for @_registry.register(...).

    Usage:
        @check("adapter_versions", name="Adapter Version Check",
               category=CheckCategory.VERSION, group=CheckGroup.ADAPTERS)
        def check_adapter_versions(data: dict, chk: CheckContext) -> ExtendedCheckResult:
            ...
    """
    return _registry.register(
        check_id, name=name, category=category, group=group, requires=requires,
    )


def get_registry() -> ExtendedValidationRegistry:
    """Return the global extended-validation registry singleton."""
    return _registry


# =================================================
# Shared inspectors (used by more than one check module — single-use
# inspectors live alongside their one check in extended_checks/)
# =================================================

def _inspect_health_state(_name: str, state_info: dict) -> str | None:
    """Flag items whose state or connection_state is in _OFFLINE_STATES."""
    bad = [
        str(state_info.get(k, "")).upper()
        for k in ("state", "connection_state")
        if str(state_info.get(k, "")).upper() in _OFFLINE_STATES
    ]
    return ", ".join(bad) if bad else None


# Auto-discovers and imports every check module in extended_checks/ — each
# self-registers via @check as an import side effect. A bare `import` (not
# `from ... import name`) so this is safe even if some other module happens
# to import a check first, which would re-enter this module while it's only
# partially initialized (see extended_checks/__init__.py for the discovery
# loop that triggers that path).
import platform_atlas.validation.extended_checks  # noqa: E402,F401  pylint: disable=wrong-import-position,unused-import


# Main Entrypoint
def run_extended_validation(
    capture_data: dict,
    *,
    headless: bool = False,
    skip_adapter_check: bool = False,
) -> list[ExtendedCheckResult]:
    """Execute all registered extended validation checks.

    The active tier is read from the capture metadata when present (so
    re-running validation against an old capture preserves the original
    tier semantics), falling back to the live config tier.

    Returns no results under SaaS: every AVC inspects Platform/MongoDB/Redis
    architecture (adapters, applications, infra health) that a single-gateway
    SaaS audit never collects, so they are not applicable there. Short-circuiting
    here covers every caller (validation, report/viewmodel re-runs).
    """
    tier = (
        capture_data.get("_atlas", {}).get("metadata", {}).get("tier")
        or _resolve_active_tier()
    )
    if tier == "saas":
        return []
    skip_checks = {"adapter_versions"} if skip_adapter_check else set()
    deactivated_checks: set[str] = set()
    # RBAC used to have its own standalone suppression here (invisible
    # skip_checks entry when enable_rbac_collection was off). It's now just
    # another entry in disabled_extended_checks like every other AVC module,
    # so it flows through deactivated_checks below and — unlike the old
    # behavior — shows as "Module Deactivated" in the report instead of
    # vanishing with no explanation.
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if is_config_loaded():
            deactivated_checks = set(get_config().disabled_extended_checks)
        else:
            # Config unavailable — fail safe on the privacy-sensitive check
            # rather than assuming it's enabled, matching the old
            # enable_rbac_collection fallback.
            deactivated_checks = {"rbac_authorization"}
    except Exception:
        deactivated_checks = {"rbac_authorization"}
    return get_registry().execute_all(
        capture_data, tier=tier, headless=headless,
        skip_checks=skip_checks, deactivated_checks=deactivated_checks,
    )


def _resolve_active_tier() -> str:
    """Best-effort lookup for the live tier; defaults to 'extended'."""
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if is_config_loaded():
            return get_config().tier
    except Exception:
        pass
    return "extended"
