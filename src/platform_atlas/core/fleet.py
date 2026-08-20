"""
Fleet aggregator — multi-environment compliance overview from local cache.

Reads the most recent session metadata, continuous-audit run status, and
alert counts for every configured environment without triggering any
captures or network I/O. Pure local-disk inspection.

Powers both the CLI ``platform-atlas fleet status`` table and the WebUI
``/fleet`` page; the JSON-serializable ``FleetEntry`` dataclass is the
single shape both consumers render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

from platform_atlas.core.environment import EnvironmentManager, get_environment_manager
from platform_atlas.core.session_manager import get_session_manager
from platform_atlas.continuous import alerts as alerts_mod, storage
from platform_atlas.continuous.runtime import read_settings

logger = logging.getLogger(__name__)


# Severity ordering for "worst-case" rollup in the fleet KPI tile.
_SEVERITY_ORDER = ("critical", "high", "warning", "medium", "info", "low")


@dataclass
class FleetEntry:
    """One environment's local-cached compliance snapshot."""
    name: str
    tier: str                       # "standard" | "extended" | ""
    is_active: bool
    organization_name: str
    description: str
    # Latest session for this env (any status).
    last_session_name: str = ""
    last_session_status: str = ""
    last_session_at: str = ""       # ISO 8601, "" if no session
    last_session_age_seconds: int | None = None
    # Pass rate sourced from the latest session that completed validation.
    last_validated_session_name: str = ""
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    total_rules: int = 0
    pass_rate_pct: float | None = None  # None when there's no validated session
    # Trailing pass-rate history (oldest → newest, up to 8 validated sessions);
    # drives the per-tile EKG sparkline on the Fleet wall.
    score_history: list[float] = field(default_factory=list)
    # Health classification used to color the tile + decide sort order.
    # One of: "healthy" | "warn" | "critical" | "cold".
    state: str = "cold"
    # Continuous audit state.
    continuous_enabled: bool = False
    continuous_last_run_at: str = ""
    continuous_last_run_age_seconds: int | None = None
    continuous_last_status: str = ""
    continuous_previous_unreadable: bool = False
    # Alerts roll-up.
    alerts_total: int = 0
    alerts_unacked: int = 0
    alerts_worst_unacked_severity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FleetSummary:
    """Aggregate roll-up across all environments — drives the KPI tiles."""
    total_envs: int
    envs_with_sessions: int
    continuous_enabled_envs: int
    total_unacked_alerts: int
    fleet_pass_rate_pct: float | None    # weighted by total rule count
    last_activity_at: str                # most-recent timestamp seen
    worst_unacked_severity: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(stamp: str) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        # Accept both "Z" and explicit offsets.
        if stamp.endswith("Z"):
            return datetime.fromisoformat(stamp[:-1]).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(stamp)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_seconds(stamp: str) -> int | None:
    parsed = _parse_iso(stamp)
    if parsed is None:
        return None
    return max(0, int((_now_utc() - parsed).total_seconds()))


def _worst_severity(severities: list[str]) -> str:
    rank: dict[str, int] = {sev: i for i, sev in enumerate(_SEVERITY_ORDER)}
    best_rank = len(_SEVERITY_ORDER)
    best = ""
    for raw in severities:
        sev = (raw or "").lower()
        r = rank.get(sev, len(_SEVERITY_ORDER))
        if r < best_rank:
            best_rank = r
            best = sev
    return best


# Tiles older than this read as "stale" and surface to the top of the wall.
_COLD_THRESHOLD_SECONDS = 60 * 24 * 3600


def _classify_state(entry: "FleetEntry") -> str:
    """Tile state for the Fleet wall — cold > critical > warn > healthy."""
    age = entry.last_session_age_seconds
    if not entry.last_session_at or age is None or age >= _COLD_THRESHOLD_SECONDS:
        return "cold"
    pct = entry.pass_rate_pct
    if pct is None:
        return "cold"
    if pct < 80:
        return "critical"
    if pct < 95:
        return "warn"
    return "healthy"


# Sort weight: cold first (most actionable), then critical, warn, healthy.
_STATE_SORT = {"cold": 0, "critical": 1, "warn": 2, "healthy": 3}


def _latest_per_env(sessions) -> dict[str, list]:
    by_env: dict[str, list] = {}
    for session in sessions:
        env = session.metadata.environment or ""
        if not env:
            continue
        by_env.setdefault(env, []).append(session)
    for env_sessions in by_env.values():
        env_sessions.sort(key=lambda s: s.metadata.updated_at, reverse=True)
    return by_env


def _build_entry(
    env_name: str,
    *,
    is_active: bool,
    env_obj,
    sessions_for_env: list,
) -> FleetEntry:
    # Organization name is global (config.json) — the same value for every
    # environment in this install, not a per-env attribute.
    try:
        from platform_atlas.core.context import ctx
        org_name = ctx().config.organization_name or ""
    except Exception:
        org_name = ""

    entry = FleetEntry(
        name=env_name,
        tier=str(getattr(env_obj, "tier", None) or ""),
        is_active=is_active,
        organization_name=org_name,
        description=getattr(env_obj, "description", "") or "",
    )

    if sessions_for_env:
        latest = sessions_for_env[0]
        entry.last_session_name = latest.metadata.name
        entry.last_session_status = str(latest.metadata.status)
        entry.last_session_at = latest.metadata.updated_at.isoformat()
        entry.last_session_age_seconds = _age_seconds(entry.last_session_at)

        # Walk validated sessions (newest → oldest) once, doing two jobs:
        # capture the latest validated pass_rate_pct, and collect up to 8
        # historical scores for the EKG sparkline.
        history: list[float] = []
        first_validated = True
        for session in sessions_for_env:
            md = session.metadata
            if not (md.validation_completed and md.total_rules):
                continue
            evaluated = md.pass_count + md.fail_count
            if not evaluated:
                continue
            score = round((md.pass_count / evaluated) * 100, 1)
            if first_validated:
                entry.last_validated_session_name = md.name
                entry.pass_count = md.pass_count
                entry.fail_count = md.fail_count
                entry.skip_count = md.skip_count
                entry.total_rules = md.total_rules
                entry.pass_rate_pct = score
                first_validated = False
            if len(history) < 8:
                history.append(score)
        # Reverse so the EKG line reads oldest → newest (left → right).
        entry.score_history = list(reversed(history))

    entry.state = _classify_state(entry)

    settings = read_settings(env_name)
    entry.continuous_enabled = settings.enabled
    status = storage.read_status(env_name)
    if status:
        entry.continuous_last_run_at = str(status.get("last_finished_at", "") or "")
        entry.continuous_last_run_age_seconds = _age_seconds(entry.continuous_last_run_at)
        entry.continuous_last_status = str(status.get("last_status", "") or "")
        entry.continuous_previous_unreadable = bool(status.get("previous_unreadable", False))

    counts = alerts_mod.counts(env_name)
    entry.alerts_total = int(counts.get("total", 0))
    entry.alerts_unacked = int(counts.get("unacked", 0))
    if entry.alerts_unacked:
        unacked = [a for a in alerts_mod.list_alerts(env_name, only_unacked=True)]
        entry.alerts_worst_unacked_severity = _worst_severity([a.severity for a in unacked])

    return entry


def collect_fleet() -> tuple[list[FleetEntry], FleetSummary]:
    """Build the full fleet snapshot.

    Returns ``(entries, summary)`` where entries is one ``FleetEntry`` per
    configured environment, sorted alphabetically by name. The active
    environment (if any) is flagged via ``is_active``.
    """
    mgr: EnvironmentManager = get_environment_manager()
    names = mgr.list_names()
    sessions = get_session_manager().list(sort_by="updated_at")
    sessions_by_env = _latest_per_env(sessions)

    # Identify active env without going through ctx() — this module is read
    # by the WebUI which has already loaded the context, but we shouldn't
    # depend on the context being initialized for CLI early-exit paths.
    try:
        from platform_atlas.core.context import ctx
        active_env = ctx().active_environment
    except Exception:  # noqa: BLE001 — context not yet initialized
        active_env = None

    entries: list[FleetEntry] = []
    for name in names:
        try:
            env_obj = mgr.load(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fleet: could not load env %s: %s", name, exc)
            continue
        entries.append(_build_entry(
            name,
            is_active=(name == active_env),
            env_obj=env_obj,
            sessions_for_env=sessions_by_env.get(name, []),
        ))

    # Sort by state (stale + critical surface first), then alphabetical.
    entries.sort(key=lambda e: (_STATE_SORT.get(e.state, 99), e.name))

    summary = _build_summary(entries)
    return entries, summary


def _build_summary(entries: list[FleetEntry]) -> FleetSummary:
    total_envs = len(entries)
    envs_with_sessions = sum(1 for e in entries if e.last_session_at)
    continuous_envs = sum(1 for e in entries if e.continuous_enabled)
    total_unacked = sum(e.alerts_unacked for e in entries)
    last_activity = ""
    weighted_pass = 0
    weighted_total = 0
    severities: list[str] = []
    for e in entries:
        if e.last_session_at and e.last_session_at > last_activity:
            last_activity = e.last_session_at
        if e.continuous_last_run_at and e.continuous_last_run_at > last_activity:
            last_activity = e.continuous_last_run_at
        evaluated = e.pass_count + e.fail_count
        if evaluated:
            weighted_pass += e.pass_count
            weighted_total += evaluated
        if e.alerts_unacked and e.alerts_worst_unacked_severity:
            severities.append(e.alerts_worst_unacked_severity)

    fleet_pass_rate = round((weighted_pass / weighted_total) * 100, 1) if weighted_total else None
    return FleetSummary(
        total_envs=total_envs,
        envs_with_sessions=envs_with_sessions,
        continuous_enabled_envs=continuous_envs,
        total_unacked_alerts=total_unacked,
        fleet_pass_rate_pct=fleet_pass_rate,
        last_activity_at=last_activity,
        worst_unacked_severity=_worst_severity(severities),
    )
