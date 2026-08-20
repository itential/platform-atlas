"""MongoDB WiredTiger Cache Analysis — reports cache utilization and pressure signals."""
from __future__ import annotations

from platform_atlas.core.utils import human_readable_bytes
from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check(
    "mongo_wiredtiger_cache",
    name="MongoDB WiredTiger Cache Analysis",
    category=CheckCategory.PERFORMANCE,
    group=CheckGroup.MONGODB,
    requires=("mongo.wiredtiger_cache",),
)
def check_mongo_wiredtiger_cache(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Report WiredTiger cache usage and pressure signals (informational only).

    Every value here is a cumulative counter since mongod last restarted, not
    a live reading, so no pass/fail threshold is applied — just awareness of
    cache utilization, dirty fill, miss ratio, and eviction pressure.
    """
    cache = chk.require(data, "mongo.wiredtiger_cache", "WiredTiger cache")

    def _fmt_bytes(value: int | None) -> str | None:
        if value is None:
            return None
        return f"{value} bytes ({human_readable_bytes(value)})"

    def _fmt_pct(value: float | None) -> str | None:
        if value is None:
            return None
        return f"{value:.2f}%"

    details = {
        "cache_bytes_used": _fmt_bytes(cache.get("cache_bytes_used")),
        "cache_bytes_max": _fmt_bytes(cache.get("cache_bytes_max")),
        "cache_utilization_pct": _fmt_pct(cache.get("cache_utilization_pct")),
        "dirty_fill_pct": _fmt_pct(cache.get("dirty_fill_pct")),
        "cache_miss_ratio_pct": _fmt_pct(cache.get("cache_miss_ratio_pct")),
        "app_thread_evictions": cache.get("app_thread_evictions"),
    }

    parts = []
    if cache.get("cache_utilization_pct") is not None:
        parts.append(f"{cache['cache_utilization_pct']:.2f}% cache utilization")
    if cache.get("cache_miss_ratio_pct") is not None:
        parts.append(f"{cache['cache_miss_ratio_pct']:.2f}% miss ratio")
    if cache.get("dirty_fill_pct") is not None:
        parts.append(f"{cache['dirty_fill_pct']:.2f}% dirty")
    if cache.get("app_thread_evictions") is not None:
        parts.append(f"{cache['app_thread_evictions']:,} app-thread evictions")

    message = "WiredTiger cache — " + ", ".join(parts) if parts else "WiredTiger cache data captured"
    return chk.info(message, details=details)
