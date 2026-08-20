"""Adapter States — flags adapters that are stopped or offline."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    _inspect_health_state,
    check,
)


@check("adapter_states", name="Adapter States", category=CheckCategory.HEALTH,
       group=CheckGroup.ADAPTERS)
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
