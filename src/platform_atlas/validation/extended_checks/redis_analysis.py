"""Redis Analysis — memory, client, persistence, and replication health from INFO."""
from __future__ import annotations

from platform_atlas.core.utils import human_readable_bytes
from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


def _num(info: dict, key: str, default: float = 0.0) -> float:
    """Coerce an INFO field to float; default for missing/non-numeric values."""
    try:
        return float(info[key])
    except (KeyError, TypeError, ValueError):
        return default


def _replica_offsets(info: dict) -> list[int]:
    """Byte offsets of every connected replica, for master-side lag calc.

    redis-py auto-parses ``slaveN:ip=...,offset=...,...`` lines into a dict
    (any INFO value containing comma-separated ``key=value`` pairs gets this
    treatment), so each ``slaveN`` entry is already a dict by the time it
    reaches us here.
    """
    offsets = []
    for key, value in info.items():
        if key.startswith("slave") and key[5:].isdigit() and isinstance(value, dict):
            offset = value.get("offset")
            if offset is not None:
                try:
                    offsets.append(int(offset))
                except (TypeError, ValueError):
                    pass
    return offsets


@check(
    "redis_analysis",
    name="Redis Analysis",
    category=CheckCategory.HEALTH,
    group=CheckGroup.REDIS,
    requires=("redis.info",),
)
def check_redis_analysis(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze Redis INFO for memory, client, persistence, and replication health.

    Atlas takes one INFO snapshot per session (no two-sample delta), so
    cumulative counters (evicted_keys, rejected_connections, keyspace hit
    ratio) can only be shown as lifetime context — raw count plus an
    average-since-uptime rate — not scored, since a single reading of a
    monotonic counter can't distinguish "one eviction years ago" from
    "evicting right now" (same reasoning as mongo_wiredtiger_cache.py).
    Everything else here is a live gauge or most-recent-event value valid
    to threshold from a single snapshot: memory ceiling, fragmentation
    ratio, client ceiling, persistence status, replication link/lag, and
    fork time.
    """
    info = chk.require(data, "redis.info", "Redis INFO")
    key_count = data.get("redis", {}).get("key_count")

    details: dict[str, object] = {}
    if key_count is not None:
        details["key_count"] = key_count

    critical: dict[str, str] = {}
    warnings: dict[str, str] = {}

    # ── Memory ──────────────────────────────────────────────────
    used = _num(info, "used_memory")
    maxmem = _num(info, "maxmemory")
    peak = _num(info, "used_memory_peak")
    frag_ratio = _num(info, "mem_fragmentation_ratio", default=1.0)

    details["used_memory"] = human_readable_bytes(used)
    details["mem_fragmentation_ratio"] = round(frag_ratio, 2)
    if peak:
        details["used_memory_peak_pct"] = round(used / peak * 100, 1)

    if maxmem > 0:
        mem_pct = used / maxmem * 100
        details["maxmemory"] = human_readable_bytes(maxmem)
        details["mem_used_pct"] = round(mem_pct, 1)
        if mem_pct > 90:
            critical["memory"] = f"{mem_pct:.1f}% of maxmemory used (critical > 90%)"
        elif mem_pct > 80:
            warnings["memory"] = f"{mem_pct:.1f}% of maxmemory used (warn > 80%)"
    else:
        details["maxmemory"] = "unlimited"

    if frag_ratio < 1.0:
        critical["mem_fragmentation"] = (
            f"mem_fragmentation_ratio {frag_ratio:.2f} < 1.0 — Redis is swapping to disk"
        )
    elif frag_ratio > 1.5:
        warnings["mem_fragmentation"] = f"mem_fragmentation_ratio {frag_ratio:.2f} > 1.5"

    # ── Clients ─────────────────────────────────────────────────
    connected = _num(info, "connected_clients")
    blocked = _num(info, "blocked_clients")
    maxclients = _num(info, "maxclients")

    details["connected_clients"] = int(connected)
    details["blocked_clients"] = int(blocked)

    if maxclients > 0:
        conn_pct = connected / maxclients * 100
        details["connected_clients_pct"] = round(conn_pct, 1)
        if conn_pct > 90:
            critical["clients"] = f"{conn_pct:.1f}% of maxclients connected (critical > 90%)"
        elif conn_pct > 80:
            warnings["clients"] = f"{conn_pct:.1f}% of maxclients connected (warn > 80%)"

    # ── Persistence ─────────────────────────────────────────────
    rdb_status = info.get("rdb_last_bgsave_status")
    aof_enabled = _num(info, "aof_enabled") == 1
    aof_status = info.get("aof_last_write_status") if aof_enabled else None

    if rdb_status is not None:
        details["rdb_last_bgsave_status"] = rdb_status
    details["rdb_changes_since_last_save"] = int(_num(info, "rdb_changes_since_last_save"))
    if aof_enabled:
        details["aof_last_write_status"] = aof_status

    if rdb_status is not None and rdb_status != "ok":
        critical["rdb_bgsave"] = f"rdb_last_bgsave_status = {rdb_status}"
    if aof_enabled and aof_status != "ok":
        critical["aof_write"] = f"aof_last_write_status = {aof_status}"

    # ── Replication ─────────────────────────────────────────────
    role = info.get("role")
    if role is not None:
        details["role"] = role

    if role == "master":
        details["connected_slaves"] = int(_num(info, "connected_slaves"))
        offsets = _replica_offsets(info)
        if offsets:
            details["replica_lag_bytes"] = int(_num(info, "master_repl_offset") - min(offsets))
    elif role == "slave":
        link_status = info.get("master_link_status")
        last_io = _num(info, "master_last_io_seconds_ago", default=-1)
        details["master_link_status"] = link_status
        if last_io >= 0:
            details["master_last_io_seconds_ago"] = last_io
        if link_status != "up":
            critical["replication"] = f"master_link_status = {link_status}"
        elif last_io > 10:
            warnings["replication"] = f"master_last_io_seconds_ago = {last_io:.0f} (> 10s)"

    # ── Fork latency ────────────────────────────────────────────
    fork_ms = _num(info, "latest_fork_usec") / 1000.0
    details["latest_fork_ms"] = round(fork_ms, 1)
    if fork_ms > 200:
        warnings["fork_time"] = f"latest fork() took {fork_ms:.0f}ms (warn > 200ms)"

    # ── Lifetime context (cumulative counters — not scored) ───────
    uptime = _num(info, "uptime_in_seconds")
    details["uptime_in_seconds"] = int(uptime)

    evicted = _num(info, "evicted_keys")
    rejected = _num(info, "rejected_connections")
    hits = _num(info, "keyspace_hits")
    misses = _num(info, "keyspace_misses")

    details["evicted_keys_total"] = int(evicted)
    details["rejected_connections_total"] = int(rejected)
    if uptime > 0:
        details["evicted_keys_per_hour_avg"] = round(evicted / uptime * 3600, 2)
        details["rejected_connections_per_hour_avg"] = round(rejected / uptime * 3600, 2)
    if hits + misses > 0:
        details["keyspace_hit_pct_lifetime"] = round(hits / (hits + misses) * 100, 1)

    details["instantaneous_ops_per_sec"] = int(_num(info, "instantaneous_ops_per_sec"))
    details["instantaneous_input_kbps"] = _num(info, "instantaneous_input_kbps")
    details["instantaneous_output_kbps"] = _num(info, "instantaneous_output_kbps")

    # ── Verdict ─────────────────────────────────────────────────
    if critical:
        return chk.fail(
            f"{len(critical)} critical Redis health issue(s): {', '.join(critical.values())}",
            details=details,
            remediation="Review Redis memory, persistence, and replication health above.",
        )
    if warnings:
        return chk.warn(
            f"{len(warnings)} Redis health warning(s): {', '.join(warnings.values())}",
            details=details,
            remediation="Review Redis memory, client, and fork-latency thresholds above.",
        )
    message = f"Redis healthy — {key_count:,} keys" if key_count is not None else "Redis healthy"
    return chk.passed(message, details=details)
