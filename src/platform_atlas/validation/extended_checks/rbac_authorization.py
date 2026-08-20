"""RBAC Authorization Analysis — summarizes the /authorization/* privilege graph."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check(
    "rbac_authorization",
    name="RBAC Authorization Analysis",
    category=CheckCategory.AUTHENTICATION,
    group=CheckGroup.PLATFORM_CORE,
    requires=("authorization.accounts",),
)
def check_rbac_authorization(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze the RBAC authorization graph captured from /authorization/*.

    Computes the full privilege summary (heatmap tiers, stale accounts, admin
    count) used by the unified report's RBAC tab. Stored in ``details`` so the
    report renderer can pull it directly from extended_results without re-reading
    the capture file.

    Skipped automatically when this module is deactivated in `config edit` >
    Advanced > Additional Validation Modules (no authorization data in capture).
    """
    from platform_atlas.validation.rbac_engine import build_rbac_summary

    auth_data = chk.require(data, "authorization", "authorization graph")
    summary_data = build_rbac_summary(auth_data)
    if not summary_data:
        return chk.skip("Authorization data present but could not be summarized")

    s = summary_data.get("summary", {})
    total = s.get("total_accounts", 0)
    admin_count = s.get("admin_count", 0)
    stale_privileged = s.get("stale_privileged", 0)
    inactive_with_access = s.get("inactive_with_access", 0)

    concerns = []
    if inactive_with_access:
        concerns.append(f"{inactive_with_access} inactive account(s) still hold role assignments")
    if stale_privileged:
        concerns.append(f"{stale_privileged} stale account(s) with elevated privileges")
    if admin_count:
        concerns.append(f"{admin_count} admin-level account(s) detected")

    if inactive_with_access:
        return chk.fail(
            f"{inactive_with_access} inactive account(s) retain role assignments — "
            f"{total} total identities analyzed",
            details=summary_data,
            remediation=(
                "Review and revoke role assignments for inactive accounts via the "
                "Platform Authorization UI or /authorization/accounts API."
            ),
        )
    if concerns:
        return chk.warn(
            f"{total} identities analyzed — " + "; ".join(concerns),
            details=summary_data,
            remediation=(
                "Review stale and admin-level accounts in the RBAC tab of the unified report."
            ),
        )
    return chk.passed(
        f"{total} identities analyzed — no privilege concerns detected",
        details=summary_data,
    )
