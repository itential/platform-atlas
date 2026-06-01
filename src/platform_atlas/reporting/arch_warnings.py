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
    active_count = platform.get("active_instance_count")
    standby_count = platform.get("standby_instance_count", 0)
    if active_count is not None:
        total_platform = _safe_int(active_count) + _safe_int(standby_count or 0)
        if total_platform <= 1:
            warnings.append(ArchWarning(
                category="availability",
                severity="critical",
                component="Platform",
                message="Platform has no high availability — a single node failure will cause downtime",
            ))

    # MongoDB no replica set — only warn when MongoDB is actually deployed
    replica_count = mongodb.get("replica_count")
    if mongodb.get("present") is not False:
        if replica_count is not None:
            if _safe_int(replica_count) <= 1:
                warnings.append(ArchWarning(
                    category="availability",
                    severity="critical",
                    component="MongoDB",
                    message="MongoDB has no replica set configured — a node failure will cause data unavailability",
                ))
        elif mongodb:
            # Section present but field missing — treat as single node
            warnings.append(ArchWarning(
                category="availability",
                severity="critical",
                component="MongoDB",
                message="MongoDB has no replica set configured — a node failure will cause data unavailability",
            ))

    # Redis standalone
    redis_deploy = (redis.get("deployment_type") or "").lower()
    redis_node_count = redis.get("redis_node_count")
    is_redis_standalone = (
        "single" in redis_deploy
        or (redis_node_count is not None and _safe_int(redis_node_count) <= 1)
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
