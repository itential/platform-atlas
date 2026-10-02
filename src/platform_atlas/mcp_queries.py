"""
Cross-environment / cross-session analytical queries for the Atlas MCP server.

Everything in this module exists solely to back read-only tool-call queries
exposed by ``platform-atlas-webui --mcp-server`` (see that repo's
``mcp/tools.py``) — deliberately kept out of ``core/fleet.py``, which powers
the CLI ``fleet status`` table and the WebUI ``/fleet`` page and has no need
for per-rule, per-session, or per-category drill-down. ``core/fleet.py``
still owns ``collect_fleet()``/``FleetEntry``/``FleetSummary`` (the
per-environment snapshot) and ``latest_sessions_by_environment()`` (session
grouping shared by both modules); everything here reads that snapshot or
walks raw validation rows on top of it.

All functions are pure local-disk reads — no ``ctx()``/``init_context()``
required, safe to call from a concurrent MCP request handler. One bad/
unreadable session file logs a warning and is skipped rather than sinking
the whole query, matching the rest of Atlas's "partial failure is still
success" convention.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

from platform_atlas.core.environment import get_environment_manager
from platform_atlas.core.session_manager import get_session_manager
from platform_atlas.core.fleet import collect_fleet, latest_sessions_by_environment

logger = logging.getLogger(__name__)

# Severity ordering for rule-severity sorting — a separate concept from (and
# coincidentally identical to) core/fleet.py's alert-severity ordering, kept
# as its own small constant here rather than importing that module's private
# one, so this module has no coupling to fleet.py's internals.
_SEVERITY_ORDER = ("critical", "high", "warning", "medium", "info", "low")


def _severity_rank(severity: str) -> int:
    try:
        return _SEVERITY_ORDER.index((severity or "").lower())
    except ValueError:
        return len(_SEVERITY_ORDER)


def _normalize_query(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", (value or "").strip().lower())


def _first_validated_session(sessions_for_env: list):
    """First (newest) session in *sessions_for_env* with a completed
    validation run, or None."""
    for session in sessions_for_env:
        md = session.metadata
        if md.validation_completed and md.total_rules and (md.pass_count + md.fail_count):
            return session
    return None


def _validated_sessions(sessions_for_env: list, limit: int | None = None) -> list:
    """All (not just the first) validated sessions in *sessions_for_env*,
    newest-first, optionally capped at *limit*."""
    validated = [
        s for s in sessions_for_env
        if s.metadata.validation_completed and s.metadata.total_rules
        and (s.metadata.pass_count + s.metadata.fail_count)
    ]
    return validated[:limit] if limit is not None else validated


# ── Rule-impact ranking (backs fleet_top_fix) ───────────────────────────

@dataclass
class RuleImpact:
    """One rule's cross-environment failure footprint.

    Ranked by breadth (how many distinct environments fail it), not raw fail
    count — a rule failing once in every environment outranks one failing
    repeatedly within a single environment.
    """
    rule_number: str
    name: str
    category: str
    severity: str
    recommendation: str
    envs_failing: list[str]
    envs_evaluated: int

    @property
    def impact_pct(self) -> float:
        if not self.envs_evaluated:
            return 0.0
        return round((len(self.envs_failing) / self.envs_evaluated) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["impact_pct"] = self.impact_pct
        return d


def latest_validated_environment() -> str | None:
    """Name of whichever environment's latest validated session is the most
    recently updated across the whole fleet, or None if none exist.

    Used by tools that accept an optional ``environment`` argument and fall
    back to "whatever was most recently audited" when it's omitted.
    """
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))
    candidates: list[tuple[datetime, str]] = []
    for name, sessions in sessions_by_env.items():
        session = _first_validated_session(sessions)
        if session is not None:
            candidates.append((session.metadata.updated_at, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def fleet_rule_impact(top_n: int = 5) -> list[RuleImpact]:
    """Rank rule failures by how many environments they recur in.

    For every environment with a latest *validated* session, loads that
    session's validation rows and tallies FAIL rows per ``rule_number``,
    counting distinct environments affected.
    """
    from platform_atlas.validation.results import load_validation_results

    names = get_environment_manager().list_names()
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    envs_evaluated = 0
    by_rule: dict[str, dict[str, Any]] = {}
    for name in names:
        session = _first_validated_session(sessions_by_env.get(name, []))
        if session is None:
            continue
        envs_evaluated += 1
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the fleet view
            logger.warning("fleet_rule_impact: could not read %s: %s", session.validation_file, exc)
            continue
        for row in results.rows:
            if row.get("status") != "FAIL":
                continue
            rule_number = row.get("rule_number") or ""
            if not rule_number:
                continue
            entry = by_rule.setdefault(rule_number, {
                "rule_number": rule_number,
                "name": row.get("name") or "",
                "category": row.get("category") or "",
                "severity": row.get("severity") or "",
                "recommendation": row.get("recommendations") or "",
                "envs_failing": set(),
            })
            entry["envs_failing"].add(name)

    impacts = [
        RuleImpact(
            rule_number=e["rule_number"],
            name=e["name"],
            category=e["category"],
            severity=e["severity"],
            recommendation=e["recommendation"],
            envs_failing=sorted(e["envs_failing"]),
            envs_evaluated=envs_evaluated,
        )
        for e in by_rule.values()
    ]
    impacts.sort(key=lambda r: (-len(r.envs_failing), _severity_rank(r.severity), r.rule_number))
    return impacts[:top_n]


# ── Rule lookup (backs explain_rule / rule_fleet_distribution) ─────────

@dataclass
class RuleMatch:
    """One rule lookup result, scoped to a single environment's latest
    validated session."""
    environment: str
    session_name: str
    rule_number: str
    name: str
    category: str
    severity: str
    path: str
    status: str
    expected: Any
    actual: Any
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rule_matches_in_rows(
    rows: list[dict[str, Any]], needle: str, *, env_name: str, session_name: str,
) -> list[RuleMatch]:
    """``RuleMatch`` rows within one session whose path/name/rule_number
    contain *needle* (already normalized). Shared by ``find_rule`` (one
    environment) and ``find_rule_across_fleet`` (every environment)."""
    matches: list[RuleMatch] = []
    for row in rows:
        haystacks = (
            _normalize_query(str(row.get("path") or "")),
            _normalize_query(str(row.get("name") or "")),
            _normalize_query(str(row.get("rule_number") or "")),
        )
        if not any(needle in h for h in haystacks):
            continue
        matches.append(RuleMatch(
            environment=env_name,
            session_name=session_name,
            rule_number=row.get("rule_number") or "",
            name=row.get("name") or "",
            category=row.get("category") or "",
            severity=row.get("severity") or "",
            path=row.get("path") or "",
            status=row.get("status") or "",
            expected=row.get("expected"),
            actual=row.get("actual"),
            recommendation=row.get("recommendations") or "",
        ))
    return matches


def find_rule(query: str, environment: str | None = None) -> list[RuleMatch]:
    """Case-insensitive substring lookup against one environment's latest
    validated session.

    Matches *query* against each rule's ``path``, ``name``, and
    ``rule_number`` (hyphens/underscores/spaces normalized, so
    ``"maxmemory-policy"`` / ``"maxmemory policy"`` / ``"RDS-002"`` all hit
    the same rule). If *environment* is omitted, scopes to whichever
    environment's latest validated session is the most recently updated
    across the whole fleet.
    """
    from platform_atlas.validation.results import load_validation_results

    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    if environment:
        candidate_envs = [environment]
    else:
        candidates: list[tuple[datetime, str]] = []
        for name, sessions in sessions_by_env.items():
            session = _first_validated_session(sessions)
            if session is not None:
                candidates.append((session.metadata.updated_at, name))
        if not candidates:
            return []
        candidates.sort(reverse=True)
        candidate_envs = [candidates[0][1]]

    needle = _normalize_query(query)
    matches: list[RuleMatch] = []
    for env_name in candidate_envs:
        session = _first_validated_session(sessions_by_env.get(env_name, []))
        if session is None:
            continue
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("find_rule: could not read %s: %s", session.validation_file, exc)
            continue
        matches.extend(_rule_matches_in_rows(
            results.rows, needle, env_name=env_name, session_name=session.metadata.name,
        ))
    return matches


def find_rule_across_fleet(query: str) -> list[RuleMatch]:
    """Same lookup as ``find_rule``, but across *every* environment's latest
    validated session rather than just the most recently audited one.

    Distinguishes a systemic policy gap (the rule fails everywhere) from a
    one-off misconfiguration (it fails in exactly one environment) — the
    kind of fleet-wide framing ``find_rule`` can't give since it only ever
    looks at one environment at a time.
    """
    from platform_atlas.validation.results import load_validation_results

    names = get_environment_manager().list_names()
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    needle = _normalize_query(query)
    matches: list[RuleMatch] = []
    for env_name in names:
        session = _first_validated_session(sessions_by_env.get(env_name, []))
        if session is None:
            continue
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("find_rule_across_fleet: could not read %s: %s", session.validation_file, exc)
            continue
        matches.extend(_rule_matches_in_rows(
            results.rows, needle, env_name=env_name, session_name=session.metadata.name,
        ))
    return matches


# ── Severity / category health ──────────────────────────────────────────

@dataclass
class SeverityBreakdown:
    """FAIL-row counts by severity, one environment or fleet-wide."""
    scope: str  # an environment name, or "fleet"
    envs_evaluated: int
    total_failures: int
    by_severity: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fleet_severity_breakdown(environment: str = "") -> SeverityBreakdown:
    """Tally FAIL rows by severity — one environment, or the whole fleet.

    Answers "what's most urgent right now" without paging through every
    environment's report by hand.
    """
    from platform_atlas.validation.results import load_validation_results

    names = [environment] if environment else get_environment_manager().list_names()
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    envs_evaluated = 0
    by_severity: dict[str, int] = {}
    for name in names:
        session = _first_validated_session(sessions_by_env.get(name, []))
        if session is None:
            continue
        envs_evaluated += 1
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fleet_severity_breakdown: could not read %s: %s", session.validation_file, exc)
            continue
        for row in results.rows:
            if row.get("status") != "FAIL":
                continue
            sev = (row.get("severity") or "unspecified").lower()
            by_severity[sev] = by_severity.get(sev, 0) + 1

    ordered = {sev: by_severity[sev] for sev in _SEVERITY_ORDER if sev in by_severity}
    ordered.update({sev: n for sev, n in by_severity.items() if sev not in ordered})

    return SeverityBreakdown(
        scope=environment or "fleet",
        envs_evaluated=envs_evaluated,
        total_failures=sum(by_severity.values()),
        by_severity=ordered,
    )


@dataclass
class CategoryHealth:
    """Pass/fail/skip tally for one rule category."""
    category: str
    pass_count: int
    fail_count: int
    skip_count: int

    @property
    def pass_rate_pct(self) -> float:
        evaluated = self.pass_count + self.fail_count
        return round((self.pass_count / evaluated) * 100, 1) if evaluated else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pass_rate_pct"] = self.pass_rate_pct
        return d


def rule_category_health(environment: str = "") -> list[CategoryHealth]:
    """Pass/fail/skip counts grouped by rule category (gateway4, mongo_conf,
    redis_conf, ...) — one environment, or aggregated across the fleet.

    Sorted worst-pass-rate-first, so the weakest subsystem surfaces
    immediately.
    """
    from platform_atlas.validation.results import load_validation_results

    names = [environment] if environment else get_environment_manager().list_names()
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    by_category: dict[str, dict[str, int]] = {}
    for name in names:
        session = _first_validated_session(sessions_by_env.get(name, []))
        if session is None:
            continue
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rule_category_health: could not read %s: %s", session.validation_file, exc)
            continue
        for row in results.rows:
            status = row.get("status")
            if status not in ("PASS", "FAIL", "SKIP"):
                continue
            category = row.get("category") or "uncategorized"
            counts = by_category.setdefault(category, {"pass": 0, "fail": 0, "skip": 0})
            counts[status.lower()] += 1

    health = [
        CategoryHealth(category=cat, pass_count=c["pass"], fail_count=c["fail"], skip_count=c["skip"])
        for cat, c in by_category.items()
    ]
    health.sort(key=lambda h: (h.pass_rate_pct, -h.fail_count, h.category))
    return health


# ── Audit-coverage gaps ──────────────────────────────────────────────────

@dataclass
class StaleEnvironment:
    """One environment whose last session is older than the requested
    threshold, or has never been audited at all."""
    name: str
    last_session_name: str
    last_session_at: str
    age_days: float | None  # None => never audited

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stale_environments(days: int = 30) -> list[StaleEnvironment]:
    """Environments whose last session is older than *days*, or that have
    never been audited — surfaces audit-coverage gaps, not compliance
    failures. Never-audited environments sort first (most actionable),
    then oldest-first.
    """
    entries, _summary = collect_fleet()
    threshold_seconds = days * 86400

    stale: list[StaleEnvironment] = []
    for e in entries:
        if e.last_session_age_seconds is None:
            stale.append(StaleEnvironment(name=e.name, last_session_name="", last_session_at="", age_days=None))
        elif e.last_session_age_seconds >= threshold_seconds:
            stale.append(StaleEnvironment(
                name=e.name,
                last_session_name=e.last_session_name,
                last_session_at=e.last_session_at,
                age_days=round(e.last_session_age_seconds / 86400, 1),
            ))
    stale.sort(key=lambda s: (s.age_days is not None, -(s.age_days or 0)))
    return stale


# ── Trend / stability over time (single environment) ────────────────────

@dataclass
class TrendPoint:
    session_name: str
    updated_at: str
    pass_rate_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionHistoryTrend:
    environment: str
    history: list[TrendPoint]  # oldest -> newest
    trend: str  # "improving" | "degrading" | "flat" | "insufficient_data"
    delta_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TREND_FLAT_THRESHOLD_PCT = 2.0


def session_history_trend(environment: str, limit: int = 10) -> SessionHistoryTrend:
    """Pass-rate trajectory across the last *limit* validated sessions in
    one environment, with a simple improving/degrading/flat classification.

    Answers "is this environment getting better or worse over time" —
    something ``diff_sessions`` can't (it only ever compares two sessions).
    """
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))
    validated = _validated_sessions(sessions_by_env.get(environment, []), limit=limit)

    points = [
        TrendPoint(
            session_name=s.metadata.name,
            updated_at=s.metadata.updated_at.isoformat(),
            pass_rate_pct=round((s.metadata.pass_count / (s.metadata.pass_count + s.metadata.fail_count)) * 100, 1),
        )
        for s in reversed(validated)  # oldest -> newest
    ]

    if len(points) < 2:
        trend, delta = "insufficient_data", None
    else:
        delta = round(points[-1].pass_rate_pct - points[0].pass_rate_pct, 1)
        if delta > _TREND_FLAT_THRESHOLD_PCT:
            trend = "improving"
        elif delta < -_TREND_FLAT_THRESHOLD_PCT:
            trend = "degrading"
        else:
            trend = "flat"

    return SessionHistoryTrend(environment=environment, history=points, trend=trend, delta_pct=delta)


@dataclass
class FlakyRule:
    """A rule whose PASS/FAIL status flipped more than once across recent
    sessions in one environment — config drift or an unstable check, not a
    single clean regression."""
    rule_number: str
    name: str
    category: str
    severity: str
    status_sequence: list[str]  # oldest -> newest
    flip_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def flaky_rules(environment: str, lookback: int = 5) -> list[FlakyRule]:
    """Rules that flip PASS/FAIL more than once across the last *lookback*
    validated sessions in one environment.

    A single flip is an ordinary regression or fix; two or more means the
    rule keeps flapping — worth investigating as instability rather than
    fixing once and moving on.
    """
    from platform_atlas.validation.results import load_validation_results

    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))
    validated = list(reversed(_validated_sessions(sessions_by_env.get(environment, []), limit=lookback)))

    by_rule: dict[str, dict[str, Any]] = {}
    for session in validated:
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("flaky_rules: could not read %s: %s", session.validation_file, exc)
            continue
        for row in results.rows:
            status = row.get("status")
            if status not in ("PASS", "FAIL"):
                continue
            rule_number = row.get("rule_number") or ""
            if not rule_number:
                continue
            entry = by_rule.setdefault(rule_number, {
                "rule_number": rule_number,
                "name": row.get("name") or "",
                "category": row.get("category") or "",
                "severity": row.get("severity") or "",
                "sequence": [],
            })
            entry["sequence"].append(status)

    flaky = []
    for entry in by_rule.values():
        seq = entry["sequence"]
        flips = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
        if flips >= 2:
            flaky.append(FlakyRule(
                rule_number=entry["rule_number"], name=entry["name"],
                category=entry["category"], severity=entry["severity"],
                status_sequence=seq, flip_count=flips,
            ))
    flaky.sort(key=lambda f: (-f.flip_count, _severity_rank(f.severity), f.rule_number))
    return flaky


# ── Cross-environment comparison ────────────────────────────────────────

@dataclass
class EnvironmentDivergence:
    rule_number: str
    name: str
    category: str
    severity: str
    status_a: str
    status_b: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnvironmentComparison:
    environment_a: str
    environment_b: str
    session_a: str
    session_b: str
    rules_compared: int
    divergences: list[EnvironmentDivergence]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_environments(environment_a: str, environment_b: str) -> EnvironmentComparison | None:
    """Rules where two environments' latest validated sessions disagree
    (one passes, the other fails) on the same rule number.

    Returns None if either environment has no validated session — the
    caller (MCP tool layer) turns that into a user-facing error message.
    """
    from platform_atlas.validation.results import load_validation_results

    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))
    session_a = _first_validated_session(sessions_by_env.get(environment_a, []))
    session_b = _first_validated_session(sessions_by_env.get(environment_b, []))
    if session_a is None or session_b is None:
        return None

    try:
        results_a = load_validation_results(session_a.validation_file)
        results_b = load_validation_results(session_b.validation_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning("compare_environments: could not read validation data: %s", exc)
        return None

    rows_a = {r.get("rule_number"): r for r in results_a.rows if r.get("rule_number")}
    rows_b = {r.get("rule_number"): r for r in results_b.rows if r.get("rule_number")}
    shared = sorted(set(rows_a) & set(rows_b))

    divergences = []
    for rule_number in shared:
        ra, rb = rows_a[rule_number], rows_b[rule_number]
        sa, sb = ra.get("status"), rb.get("status")
        if sa != sb and "SKIP" not in (sa, sb):
            divergences.append(EnvironmentDivergence(
                rule_number=rule_number, name=ra.get("name") or "",
                category=ra.get("category") or "", severity=ra.get("severity") or "",
                status_a=sa or "", status_b=sb or "",
            ))
    divergences.sort(key=lambda d: (_severity_rank(d.severity), d.rule_number))

    return EnvironmentComparison(
        environment_a=environment_a, environment_b=environment_b,
        session_a=session_a.metadata.name, session_b=session_b.metadata.name,
        rules_compared=len(shared), divergences=divergences,
    )


# ── Skip-reason visibility ───────────────────────────────────────────────

@dataclass
class SkipReasonBreakdown:
    """SKIP rows grouped by ``skip_kind`` — distinguishes a deliberate
    suppression ("conditional") from data that was simply never collected
    ("unreachable"), which is easy to miss reading one report by eye."""
    scope: str
    envs_evaluated: int
    total_skips: int
    by_reason: dict[str, int]
    unreachable_rules: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def skip_reason_breakdown(environment: str = "") -> SkipReasonBreakdown:
    from platform_atlas.validation.results import load_validation_results

    names = [environment] if environment else get_environment_manager().list_names()
    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    envs_evaluated = 0
    by_reason: dict[str, int] = {}
    unreachable: list[str] = []
    for name in names:
        session = _first_validated_session(sessions_by_env.get(name, []))
        if session is None:
            continue
        envs_evaluated += 1
        try:
            results = load_validation_results(session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip_reason_breakdown: could not read %s: %s", session.validation_file, exc)
            continue
        for row in results.rows:
            if row.get("status") != "SKIP":
                continue
            reason = row.get("skip_kind") or "unspecified"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if reason == "unreachable":
                rule_number = row.get("rule_number")
                if rule_number:
                    unreachable.append(rule_number if environment else f"{name}:{rule_number}")

    return SkipReasonBreakdown(
        scope=environment or "fleet",
        envs_evaluated=envs_evaluated,
        total_skips=sum(by_reason.values()),
        by_reason=by_reason,
        unreachable_rules=unreachable[:25],
    )


# ── Tier coverage ─────────────────────────────────────────────────────────

@dataclass
class TierCoverage:
    tier: str
    env_count: int
    envs: list[str]
    avg_pass_rate_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fleet_tier_coverage() -> list[TierCoverage]:
    """Environment counts by tier (standard/extended/saas), cross-referenced
    with average pass rate per tier — useful context for tier-migration
    planning (e.g. "are our SaaS environments passing at a different rate
    than Extended ones").
    """
    from platform_atlas.core.config import environment_effective_tier

    entries, _summary = collect_fleet()
    by_tier: dict[str, list] = {}
    for e in entries:
        tier = environment_effective_tier(e.name) or "unknown"
        by_tier.setdefault(tier, []).append(e)

    coverage = []
    for tier, envs in by_tier.items():
        rates = [e.pass_rate_pct for e in envs if e.pass_rate_pct is not None]
        avg = round(sum(rates) / len(rates), 1) if rates else None
        coverage.append(TierCoverage(
            tier=tier, env_count=len(envs), envs=sorted(e.name for e in envs), avg_pass_rate_pct=avg,
        ))
    coverage.sort(key=lambda t: t.tier)
    return coverage


# ── Fleet-wide regressions ────────────────────────────────────────────────

@dataclass
class FleetRegression:
    """One rule that regressed (PASS -> FAIL) between an environment's two
    most recent validated sessions."""
    environment: str
    rule_number: str
    name: str
    category: str
    severity: str
    baseline_session: str
    latest_session: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fleet_regressions_since_last_audit(limit: int = 10) -> list[FleetRegression]:
    """For every environment with at least two validated sessions, diff the
    latest against the previous one and collect every regression — ranked
    by severity across the whole fleet.

    Answers "what just got worse, anywhere, since we last looked" without
    running ``diff_sessions`` by hand against every environment.
    """
    from platform_atlas.validation.results import load_validation_results
    from platform_atlas.reporting.diff_engine import diff_reports, ChangeType

    sessions_by_env = latest_sessions_by_environment(get_session_manager().list(sort_by="updated_at"))

    regressions: list[FleetRegression] = []
    for name, sessions_for_env in sessions_by_env.items():
        validated = _validated_sessions(sessions_for_env, limit=2)
        if len(validated) < 2:
            continue
        latest_session, baseline_session = validated[0], validated[1]
        try:
            latest_results = load_validation_results(latest_session.validation_file)
            baseline_results = load_validation_results(baseline_session.validation_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fleet_regressions_since_last_audit: could not read %s: %s", name, exc)
            continue
        diff_result = diff_reports(baseline_results, latest_results)
        for row in diff_result.rows:
            if row.get("change_type") != str(ChangeType.REGRESSED):
                continue
            regressions.append(FleetRegression(
                environment=name,
                rule_number=row.get("rule_number") or "",
                name=row.get("name") or "",
                category=row.get("category") or "",
                severity=row.get("severity") or "",
                baseline_session=baseline_session.metadata.name,
                latest_session=latest_session.metadata.name,
            ))

    regressions.sort(key=lambda r: (_severity_rank(r.severity), r.environment, r.rule_number))
    return regressions[:limit]
