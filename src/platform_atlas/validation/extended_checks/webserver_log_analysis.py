"""Webserver Log Analysis — surfaces performance and auth anomalies from access logs."""
from __future__ import annotations

from urllib.parse import urlparse

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    ExtendedStatus,
    check,
)


@check(
    "webserver_log_analysis",
    name="Webserver Log Analysis",
    category=CheckCategory.LOGS,
    group=CheckGroup.LOGGING,
    requires=("platform.webserver_logs",),
)
def check_webserver_logs(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze webserver access logs for performance and auth anomalies."""
    log_data = chk.require(data, "platform.webserver_logs", "webserver logs")

    entries: list[dict] = log_data.get("entries", [])
    if not entries:
        return chk.skip("No webserver log entries to analyze")

    slow_threshold_ms = 5000.0
    error_codes = {"400", "401", "403", "404", "500", "502", "503", "504"}

    slow_requests: list[dict] = []
    error_requests: list[dict] = []
    anonymous_requests: list[dict] = []
    status_counts: dict[str, int] = {}
    total_time = 0.0

    # ── Per-endpoint volume tracking ──
    endpoint_volume: dict[str, dict] = {}

    for entry in entries:
        status = str(entry.get("status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1

        try:
            elapsed = float(entry.get("total_time_ms", 0))
        except (ValueError, TypeError):
            elapsed = 0.0
        total_time += elapsed

        # Track call volume by base path
        full_url = entry.get("url", "unknown")
        base_path = urlparse(full_url).path
        method = entry.get("method", "?")

        if base_path not in endpoint_volume:
            endpoint_volume[base_path] = {
                "count": 0,
                "methods": {},
                "total_ms": 0.0,
                "error_count": 0,
            }
        ep = endpoint_volume[base_path]
        ep["count"] += 1
        ep["methods"][method] = ep["methods"].get(method, 0) + 1
        ep["total_ms"] += elapsed
        if status in error_codes:
            ep["error_count"] += 1

        if elapsed >= slow_threshold_ms:
            slow_requests.append({
                "url": full_url,
                "method": method,
                "total_time_ms": elapsed,
                "status": status,
            })

        if status in error_codes:
            error_requests.append({
                "url": full_url,
                "method": method,
                "status": status,
            })

        user = entry.get("remote_user", "")
        if user.lower() in ("anonymous", "", "-"):
            anonymous_requests.append({
                "url": full_url,
                "method": method,
            })

    total = len(entries)
    avg_time = total_time / total if total else 0.0

    # ── Top-10 endpoints by call volume ──
    top_endpoints = sorted(
        endpoint_volume.items(),
        key=lambda x: x[1]["count"],
        reverse=True,
    )[:10]
    top_endpoints_list = [
        {
            "path": path,
            "count": info["count"],
            "methods": info["methods"],
            "avg_ms": round(info["total_ms"] / info["count"], 1) if info["count"] else 0,
            "error_count": info["error_count"],
        }
        for path, info in top_endpoints
    ]

    # ── Slow endpoints grouped by base path ──
    slow_by_path: dict[str, dict] = {}

    for req in slow_requests:
        full_url = req["url"]
        base_path = urlparse(full_url).path

        if base_path not in slow_by_path:
            slow_by_path[base_path] = {
                "worst_ms": req["total_time_ms"],
                "count": 0,
                "examples": [],
            }

        group = slow_by_path[base_path]
        group["count"] += 1
        group["worst_ms"] = max(group["worst_ms"], req["total_time_ms"])

        if len(group["examples"]) < 3:
            group["examples"].append({
                "url": full_url,
                "total_time_ms": req["total_time_ms"],
                "method": req["method"],
            })

    details = {
        "total_requests": total,
        "avg_response_ms": round(avg_time, 2),
        "status_distribution": status_counts,
        "top_endpoints": top_endpoints_list,
        "slow_requests_count": len(slow_requests),
        "slow_endpoints": dict(sorted(
            slow_by_path.items(), key=lambda x: x[1]["worst_ms"], reverse=True
        )[:10]),
        "error_count": len(error_requests),
        "anonymous_count": len(anonymous_requests),
    }

    issues: list[str] = []

    error_rate = len(error_requests) / total * 100 if total else 0
    if error_rate > 5.0:
        issues.append(f"High error rate: {error_rate:.1f}%")

    if len(slow_requests) > total * 0.1:
        issues.append(
            f"{len(slow_requests)} requests exceeded {slow_threshold_ms}ms "
            f"({len(slow_requests)/total*100:.1f}%)"
        )

    if not issues:
        return chk.passed(
            f"Webserver healthy — {total} requests, avg {avg_time:.1f}ms",
            details=details,
        )

    severity: ExtendedStatus = "FAIL" if error_rate > 15.0 else "WARN"
    return chk.result(
        severity,
        f"{len(issues)} issue(s): {'; '.join(issues)}",
        details=details,
        remediation=(
            "Review slow endpoints for optimization opportunities. "
            "High error rates may indicate misconfigured routes or "
            "upstream service issues."
        ),
    )
