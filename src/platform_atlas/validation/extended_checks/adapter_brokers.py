"""Adapter Brokers — flags use of LocalAAA authentication."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_brokers", name="Adapter Brokers", category=CheckCategory.AUTHENTICATION,
       group=CheckGroup.ADAPTERS)
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
