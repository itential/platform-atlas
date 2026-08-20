"""Adapter Limit Errors — flags adapters missing required retry-on-error codes."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_limit_errors", name="Adapter Limit Errors", category=CheckCategory.CONFIGURATION,
       group=CheckGroup.ADAPTERS)
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
