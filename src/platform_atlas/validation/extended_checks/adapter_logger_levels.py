"""Adapter Logger Levels — flags adapters left in verbose (debug/trace) logging."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)

_VERBOSE_LOG_LEVELS = frozenset({"debug", "trace"})


def _inspect_verbose_logging(_name: str, config: dict) -> str | None:
    """Flag adapters with debug/trace logging on console or file."""
    bad = [
        str(config.get(k, "")).lower()
        for k in ("console", "file")
        if str(config.get(k, "")).lower() in _VERBOSE_LOG_LEVELS
    ]
    return ", ".join(bad) if bad else None


@check("adapter_logger_levels", name="Adapter Logger Levels", category=CheckCategory.CONFIGURATION,
       group=CheckGroup.ADAPTERS)
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
