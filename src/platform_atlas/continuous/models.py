"""Data models for the continuous-audit subsystem."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# Default schedule: hourly. Configurable per-environment.
DEFAULT_INTERVAL_SECONDS = 3600

# Default retention: 7 days at hourly cadence = 168 runs.
DEFAULT_RETAIN_RUNS = 168

# Alert policy values. ``any`` = current behavior (alert on any drifted rule);
# ``regression`` = alert only when a rule transitions from PASS → FAIL. Both
# operate on top of the watchlist when one is set. Defaults preserve pre-1.7.x
# behavior on upgrade — empty watchlist + ``any`` policy = identical to before.
ALERT_POLICY_ANY = "any"
ALERT_POLICY_REGRESSION = "regression"
VALID_ALERT_POLICIES = (ALERT_POLICY_ANY, ALERT_POLICY_REGRESSION)
DEFAULT_ALERT_POLICY = ALERT_POLICY_ANY

# Log-watch threshold modes
LOG_WATCH_ANY = "any"           # alert on any match
LOG_WATCH_COUNT = "count"       # alert when matches >= threshold_count in this run
LOG_WATCH_WINDOW = "window"     # alert when matches >= threshold_count in last N minutes
VALID_LOG_WATCH_THRESHOLDS = (LOG_WATCH_ANY, LOG_WATCH_COUNT, LOG_WATCH_WINDOW)

# Log sources available for watching
LOG_WATCH_SOURCES = ("platform", "webserver", "mongodb", "any")


def _coerce_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_alert_policy(raw: Any) -> str:
    """Normalize an alert-policy value, falling back to the default on garbage."""
    if not isinstance(raw, str):
        return DEFAULT_ALERT_POLICY
    candidate = raw.strip().lower()
    if candidate in VALID_ALERT_POLICIES:
        return candidate
    logger.warning(
        "Unknown continuous_audit.alert_policy %r — falling back to %r",
        raw, DEFAULT_ALERT_POLICY,
    )
    return DEFAULT_ALERT_POLICY


def _coerce_log_watches(raw: Any) -> tuple["LogWatchEntry", ...]:
    """Normalize a list of log-watch entry dicts into ``LogWatchEntry`` objects."""
    if not raw or not isinstance(raw, (list, tuple)):
        return ()
    out: list[LogWatchEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(LogWatchEntry.from_dict(item))
        except Exception as exc:
            logger.warning("Skipping malformed log-watch entry: %s — %s", item, exc)
    return tuple(out)


def _coerce_watchlist(raw: Any) -> tuple[str, ...]:
    """Normalize a watchlist value into a deduped tuple of uppercase rule numbers.

    Accepts a list/tuple of strings on disk. Trims whitespace, uppercases
    (rule numbers in this codebase are conventionally upper-case), drops
    empties, dedupes while preserving first-seen order.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        # Tolerate a single string for forgiveness — comma-or-space split.
        items = [t for t in raw.replace(",", " ").split() if t]
    elif isinstance(raw, (list, tuple)):
        items = [str(t) for t in raw]
    else:
        logger.warning(
            "continuous_audit.watchlist is not a list (got %s); ignoring",
            type(raw).__name__,
        )
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        norm = item.strip().upper()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return tuple(out)


@dataclass(frozen=True)
class LogWatchEntry:
    """One log-watching rule for the continuous-audit engine.

    Persisted under ``continuous_audit.log_watches`` in the environment
    overlay file.
    """
    id: str                             # Stable ID (user-provided or auto-generated)
    name: str                           # Human label ("OOM killer events", etc.)
    pattern: str                        # Regex or literal keyword to match
    log_source: str = "any"             # "platform" | "webserver" | "mongodb" | "any"
    severity: str = "warning"           # "critical" | "warning" | "info"
    threshold_mode: str = LOG_WATCH_ANY # "any" | "count" | "window"
    threshold_count: int = 1            # Minimum match count to trigger (count/window modes)
    threshold_window_minutes: int = 60  # Look-back window in minutes (window mode)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogWatchEntry":
        mode = str(data.get("threshold_mode") or LOG_WATCH_ANY)
        if mode not in VALID_LOG_WATCH_THRESHOLDS:
            mode = LOG_WATCH_ANY
        source = str(data.get("log_source") or "any")
        if source not in LOG_WATCH_SOURCES:
            source = "any"
        sev = str(data.get("severity") or "warning").lower()
        if sev not in ("critical", "warning", "info"):
            sev = "warning"
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            pattern=str(data.get("pattern") or ""),
            log_source=source,
            severity=sev,
            threshold_mode=mode,
            threshold_count=_coerce_int(data.get("threshold_count"), 1),
            threshold_window_minutes=_coerce_int(data.get("threshold_window_minutes"), 60),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class LogWatchAlert:
    """One triggered log-watch event from the current continuous-audit run."""
    watch_id: str
    watch_name: str
    pattern: str
    log_source: str
    severity: str
    threshold_mode: str
    threshold_count: int
    match_count: int
    sample_lines: list[str]     # Up to 3 representative matching lines
    detected_at: str            # ISO 8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContinuousSettings:
    """Per-environment continuous-audit configuration.

    Persisted on the environment overlay file under the key
    ``continuous_audit``. Reading is centralized in ``runtime.read_settings``
    so callers never parse the JSON themselves.
    """
    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    retain_runs: int = DEFAULT_RETAIN_RUNS
    ruleset_id: str = ""
    profile_id: str = ""
    # Alert policy: which drift events should produce an alert / notification.
    # Storage of the underlying drift history is unaffected (events.ndjson
    # always contains everything detected). See continuous/policy.py.
    alert_policy: str = DEFAULT_ALERT_POLICY
    # Optional rule-number allowlist. When non-empty, only drift events
    # whose ``rule_number`` matches one of these (case-insensitive) trigger
    # an alert. Empty = no filter (all rules considered, current behavior).
    watchlist: tuple[str, ...] = ()
    # Log watching (Extended mode only). When enabled, each run also tails
    # Platform/webserver/MongoDB logs and generates alerts when patterns match
    # the configured threshold.
    log_watch_enabled: bool = False
    log_watches: tuple[LogWatchEntry, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # Persist watchlist and log_watches as lists (JSON has no tuple type).
        out["watchlist"] = list(self.watchlist)
        out["log_watches"] = [e.to_dict() for e in self.log_watches]
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "ContinuousSettings":
        if not data:
            return cls()
        if not isinstance(data, dict):
            logger.warning("continuous_audit settings is not a JSON object (got %s); using defaults",
                           type(data).__name__)
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            interval_seconds=_coerce_int(data.get("interval_seconds"), DEFAULT_INTERVAL_SECONDS),
            retain_runs=_coerce_int(data.get("retain_runs"), DEFAULT_RETAIN_RUNS),
            ruleset_id=str(data.get("ruleset_id") or ""),
            profile_id=str(data.get("profile_id") or ""),
            alert_policy=_coerce_alert_policy(data.get("alert_policy")),
            watchlist=_coerce_watchlist(data.get("watchlist")),
            log_watch_enabled=bool(data.get("log_watch_enabled", False)),
            log_watches=_coerce_log_watches(data.get("log_watches")),
        )


@dataclass
class RunSummary:
    """Roll-up of one continuous-audit run's outcome."""
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    error_count: int = 0
    drifted_count: int = 0

    @property
    def total(self) -> int:
        return self.pass_count + self.fail_count + self.skip_count + self.error_count

    def to_dict(self) -> dict[str, int]:
        out = asdict(self)
        out["total"] = self.total
        return out


@dataclass
class RunResult:
    """One continuous-audit run, ready to write to disk as JSON."""
    run_id: str                         # ISO timestamp + env, e.g. 2026-05-04T18-00-00Z-prod
    environment: str                    # env name, "" if no active env
    started_at: str                     # ISO 8601 UTC
    finished_at: str                    # ISO 8601 UTC
    duration_ms: int
    ruleset_id: str
    ruleset_version: str
    summary: RunSummary = field(default_factory=RunSummary)
    # Each result carries: rule_number, name, severity, status, path, expected, actual, message,
    # plus a "drift" sub-dict when the value changed from the previous run.
    results: list[dict[str, Any]] = field(default_factory=list)
    capture_error: str | None = None    # Set when capture itself failed (no validation possible).
    # True when a previous run existed on disk but could not be read (corrupt
    # JSON, broken symlink, permission flake). Drift detection skipped this run.
    previous_unreadable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "ruleset": {"id": self.ruleset_id, "version": self.ruleset_version},
            "summary": self.summary.to_dict(),
            "results": self.results,
            "capture_error": self.capture_error,
            "previous_unreadable": self.previous_unreadable,
        }


class AlertStatus(str, Enum):
    UNACKED = "unacked"
    ACKED = "acked"


@dataclass
class DriftEvent:
    """A single observed-value change between two consecutive runs."""
    rule_number: str
    rule_name: str
    severity: str
    path: str
    previous: Any
    current: Any
    previous_run_id: str
    current_run_id: str
    detected_at: str  # ISO 8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Alert:
    """An aggregated, user-facing alert backed by one or more drift events.

    One alert per (rule_number, path). Repeated drift on the same rule path
    bumps ``occurrence_count`` and ``last_seen`` rather than creating a new
    alert — the timeline is in events.ndjson; this file is the dashboard view.
    """
    alert_id: str               # Stable: hash of rule_number + path
    rule_number: str
    rule_name: str
    severity: str
    path: str
    first_seen: str             # ISO 8601 UTC
    last_seen: str              # ISO 8601 UTC
    occurrence_count: int
    latest_previous: Any
    latest_current: Any
    status: AlertStatus = AlertStatus.UNACKED
    acked_at: str | None = None
    acked_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["status"] = self.status.value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alert":
        status = AlertStatus(data.get("status", AlertStatus.UNACKED.value))
        return cls(
            alert_id=data["alert_id"],
            rule_number=data["rule_number"],
            rule_name=data["rule_name"],
            severity=data["severity"],
            path=data["path"],
            first_seen=data["first_seen"],
            last_seen=data["last_seen"],
            occurrence_count=int(data.get("occurrence_count", 1)),
            latest_previous=data.get("latest_previous"),
            latest_current=data.get("latest_current"),
            status=status,
            acked_at=data.get("acked_at"),
            acked_by=data.get("acked_by"),
        )
