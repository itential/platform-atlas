"""Application States — flags applications that are stopped or offline."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    _inspect_health_state,
    check,
)


@check("application_states", name="Application States", category=CheckCategory.HEALTH,
       group=CheckGroup.APPLICATIONS)
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
