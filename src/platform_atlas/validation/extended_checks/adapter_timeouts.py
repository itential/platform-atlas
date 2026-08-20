"""Adapter Timeouts — flags adapters with too-aggressive request timeouts."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_timeouts", name="Adapter Timeouts", category=CheckCategory.PERFORMANCE,
       group=CheckGroup.ADAPTERS)
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
