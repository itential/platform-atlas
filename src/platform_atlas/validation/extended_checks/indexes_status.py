"""Database Index Status — flags collections with missing indexes."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check(
    "indexes_status",
    name="Database Index Status",
    category=CheckCategory.HEALTH,
    group=CheckGroup.PLATFORM_CORE,
    requires=("platform.indexes_status",),
)
def check_indexes_status(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check if any database collecctions have missing indexes"""
    indexes = chk.require(data, "platform.indexes_status", "index status")

    def _inspect(_collection: str, info: dict) -> str | None:
        missing = info.get("missing", [])
        if missing:
            return f"{len(missing)} missing index(es)"
        return None

    issues = chk.scan(indexes, _inspect)
    return chk.report(
        issues,
        pass_msg="All database collections are properly indexed",
        warn_msg=f"{len(issues)} collection(s) with missing indexes",
        total=len(indexes),
        fail_threshold=0.3,
        remediation=(
            "Missing indexes can significantly degrade query performance "
            "and increase database load. Run the platform's index rebuild "
            "in Admin Essentials to resolve this."
        ),
    )
