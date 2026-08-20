"""Adapter Health Data — flags misconfigured adapter healthcheck settings."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check("adapter_health_data", name="Adapter Health Data", category=CheckCategory.HEALTH,
       group=CheckGroup.ADAPTERS)
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
