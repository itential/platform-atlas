"""Architecture warning analysis — latency, availability, and security concerns."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _safe_int(value: Any, default: int = 0) -> int:
    """Cast *value* to int, returning *default* on any failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _known_int(section: dict[str, Any], key: str) -> int | None:
    """The integer answer for ``key``, or ``None`` when it is missing or answered "Unknown".

    A customer who answers "Unknown" to a count must never be read as zero —
    that would raise a false "no high availability" alert. Unknown means "we
    can't say", so every rule that needs the number simply doesn't fire.
    """
    if key in (section.get("unknown") or []):
        return None
    value = section.get(key)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ArchWarning:
    category: str   # "latency" | "availability" | "security"
    severity: str   # "critical" | "warning" | "info"
    component: str  # e.g. "MongoDB", "Gateway4", "Redis"
    message: str
    detail: str = ""


def compute_arch_warnings(arch_data: dict[str, Any]) -> list[ArchWarning]:
    """Analyze architecture overview data and return a list of ArchWarning objects.

    ``arch_data`` is the ``completed`` section of the architecture JSON — the dict
    that maps section names (``"environment"``, ``"platform"``, ``"mongodb"``,
    ``"redis"``, ``"gateway4"``, ``"gateway5"``) to their collected key/value data.

    All field accesses use ``.get()`` so missing keys are silently skipped rather
    than raising KeyError.
    """
    warnings: list[ArchWarning] = []

    env = arch_data.get("environment") or {}
    platform = arch_data.get("platform") or {}
    mongodb = arch_data.get("mongodb") or {}
    redis = arch_data.get("redis") or {}
    gateway4 = arch_data.get("gateway4") or {}
    gateway5 = arch_data.get("gateway5") or {}

    # ── Latency warnings ─────────────────────────────────────────────────────

    # Platform nodes span multiple datacenters
    if platform.get("all_in_same_datacenter") is False:
        warnings.append(ArchWarning(
            category="latency",
            severity="warning",
            component="Platform",
            message="Platform nodes span multiple datacenters — latency between nodes may degrade performance",
            detail=platform.get("datacenter_details", ""),
        ))

    # MongoDB in a different datacenter
    if mongodb.get("same_datacenter_as_platform") is False:
        warnings.append(ArchWarning(
            category="latency",
            severity="warning",
            component="MongoDB",
            message="MongoDB is in a different datacenter from Platform — write latency may be significant",
        ))

    # Redis in a different datacenter
    if redis.get("same_datacenter_as_platform") is False:
        warnings.append(ArchWarning(
            category="latency",
            severity="warning",
            component="Redis",
            message="Redis is in a different datacenter from Platform — cache latency may be significant",
        ))

    # MongoDB replica set spans datacenters (informational)
    if mongodb.get("replicas_across_datacenters") is True:
        warnings.append(ArchWarning(
            category="latency",
            severity="info",
            component="MongoDB",
            message=(
                "MongoDB replica set spans datacenters — this provides geographic "
                "redundancy but increases write latency"
            ),
            detail=mongodb.get("datacenter_distribution", ""),
        ))

    # Gateway4 in a different datacenter
    if gateway4.get("present") is True and gateway4.get("same_datacenter_as_platform") is False:
        warnings.append(ArchWarning(
            category="latency",
            severity="warning",
            component="Gateway4",
            message="Gateway4 is in a different datacenter from Platform — API call latency will be elevated",
        ))

    # Gateway5 in a different datacenter
    if gateway5.get("present") is True and gateway5.get("same_datacenter_as_platform") is False:
        warnings.append(ArchWarning(
            category="latency",
            severity="warning",
            component="Gateway5",
            message="Gateway5 is in a different datacenter from Platform — API call latency will be elevated",
        ))

    # ── Availability warnings ─────────────────────────────────────────────────

    # Platform single-node (no HA) — use active + standby counts.
    # The collector stores active_instance_count and standby_instance_count; there
    # is no explicit ha_enabled field on the platform section. Total <= 1 means
    # no redundancy.
    active_count = _known_int(platform, "active_instance_count")
    standby_known = _known_int(platform, "standby_instance_count")
    standby_unknown = "standby_instance_count" in (platform.get("unknown") or [])
    # An unknown standby count could still be redundancy — don't claim "no HA".
    if active_count is not None and not standby_unknown:
        total_platform = active_count + (standby_known or 0)
        if total_platform <= 1:
            warnings.append(ArchWarning(
                category="availability",
                severity="critical",
                component="Platform",
                message="Platform has no high availability — a single node failure will cause downtime",
            ))

    # MongoDB no replica set / too few members for automatic-failover quorum
    # (fewer than 3 total nodes) — only warn when MongoDB is actually deployed.
    # mongo_node_count (total servers, including the primary) is the current
    # field; replica_count (additional members beyond the primary) is the
    # pre-3.x field for environments answered before the MongoDB section was
    # rewritten to match Redis's total-count pattern — read as a fallback,
    # converted to a total, so old data still warns correctly rather than
    # silently going quiet.
    node_count = _known_int(mongodb, "mongo_node_count")
    node_count_unknown = "mongo_node_count" in (mongodb.get("unknown") or [])
    if node_count is None and mongodb.get("replica_count") is not None:
        node_count = _safe_int(mongodb["replica_count"]) + 1
    if mongodb.get("present") is not False and not node_count_unknown:
        if node_count is not None:
            if node_count <= 2:
                warnings.append(ArchWarning(
                    category="availability",
                    severity="critical",
                    component="MongoDB",
                    message="MongoDB has no replica set configured — a node failure will cause data unavailability",
                ))
        elif "standalone" in str(mongodb.get("deployment_type") or "").lower():
            # No node count, but the customer said Standalone. A section that is
            # merely unanswered says nothing — the report's completeness
            # banner covers that, not a false availability alert.
            warnings.append(ArchWarning(
                category="availability",
                severity="critical",
                component="MongoDB",
                message="MongoDB has no replica set configured — a node failure will cause data unavailability",
            ))

    # Redis standalone
    redis_deploy = (redis.get("deployment_type") or "").lower()
    redis_node_count = _known_int(redis, "redis_node_count")
    is_redis_standalone = (
        "single" in redis_deploy
        or (redis_node_count is not None and redis_node_count <= 1)
    )
    if redis and is_redis_standalone:
        warnings.append(ArchWarning(
            category="availability",
            severity="warning",
            component="Redis",
            message="Redis is running in standalone mode — no failover available",
        ))

    # Gateway4 no HA (informational, only when present and ha_enabled field is set)
    if gateway4.get("present") is True and "ha_enabled" in gateway4:
        if gateway4.get("ha_enabled") is False:
            warnings.append(ArchWarning(
                category="availability",
                severity="info",
                component="Gateway4",
                message="Gateway4 has no high availability configured",
            ))

    # ── Security warnings ─────────────────────────────────────────────────────

    hosting = (env.get("hosting_provider") or "").lower()
    cloud_keywords = ("cloud", "aws", "azure", "gcp")
    is_cloud = any(kw in hosting for kw in cloud_keywords)

    if is_cloud and mongodb.get("same_datacenter_as_platform") is False:
        warnings.append(ArchWarning(
            category="security",
            severity="warning",
            component="MongoDB",
            message=(
                "Cross-datacenter traffic to cloud MongoDB may traverse public internet "
                "— verify private connectivity"
            ),
        ))

    return warnings
