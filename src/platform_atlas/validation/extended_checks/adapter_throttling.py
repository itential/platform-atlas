"""Adapter Throttling — flags adapters with request throttling enabled."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_throttling", name="Adapter Throttling", category=CheckCategory.PERFORMANCE,
       group=CheckGroup.ADAPTERS)
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
