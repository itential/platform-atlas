"""
Continuous-audit engine — capture (Platform-only) → reshape → finalize → validate
→ diff against previous run → write JSON report + drift events.

The same ``run_once()`` is invoked by the CLI subcommand and by the WebUI
scheduler. It does NOT go through the normal ``run_capture()`` orchestrator
because that would build SSH/Mongo/Redis collectors via the modules registry.
Instead, we drive ``PlatformCollector`` directly with a planned endpoint set.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from socket import gethostname
from typing import Any

from platform_atlas.core._version import __version__
from platform_atlas.core.context import ctx
from platform_atlas.capture.capture_engine import reshape_capture, finalize_capture
from platform_atlas.capture.collectors.platform import PlatformCollector

from platform_atlas.continuous import storage
from platform_atlas.continuous.scope import platform_only_scope
from platform_atlas.continuous.endpoint_planner import required_endpoints, needs_index_status
from platform_atlas.continuous.models import ContinuousSettings, RunResult, RunSummary
from platform_atlas.continuous.drift import compute_drift
from platform_atlas.continuous.alerts import update_alert_state
from platform_atlas.continuous.policy import filter_drift_events_for_alerts

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_ruleset_for_settings(settings: ContinuousSettings, context: Any) -> Any:
    """Return a Ruleset for the given continuous-audit settings.

    If ``settings.ruleset_id`` is empty, returns the context's active ruleset.
    Otherwise loads the configured ruleset file (+ optional profile overlay)
    directly — without mutating global state — and returns a local Ruleset.
    """
    if not settings.ruleset_id:
        return context.ruleset

    import json as _json
    from platform_atlas.core.rules import Ruleset, _filter_rules_for_tier, _resolve_tier_for_filter
    from platform_atlas.core.ruleset_manager import get_ruleset_manager

    mgr = get_ruleset_manager()
    ruleset_path = mgr._resolve_ruleset_path(settings.ruleset_id)
    if ruleset_path is None:
        raise FileNotFoundError(f"Configured continuous-audit ruleset not found: {settings.ruleset_id}")

    with open(ruleset_path, "r", encoding="utf-8") as fh:
        data = _json.load(fh)

    if settings.profile_id:
        # pylint: disable=protected-access
        data = mgr._apply_profile(data, settings.profile_id)

    tier = _resolve_tier_for_filter()
    filtered = _filter_rules_for_tier(data["rules"], tier)
    return Ruleset(schema=data.get("$schema"), ruleset=data["ruleset"], rules=filtered)


def _capture_platform_only(ruleset: dict) -> dict[str, Any]:
    """Run ONE Platform OAuth fetch, narrowed to endpoints the ruleset uses.

    Returns a flat collector-output dict shaped like ``run_capture()`` would
    produce for the platform module — the reshape step then lifts it into
    the nested ``platform.*`` hierarchy validation expects.
    """
    endpoints = required_endpoints(ruleset)
    flat: dict[str, Any] = {}
    with platform_only_scope():
        with PlatformCollector.from_config() as collector:
            # ``get_platform_info`` already does parallel fetch + per-endpoint
            # error tolerance, so a single dead endpoint won't tank the run.
            platform_data = collector.get_platform_info(
                endpoints=endpoints,
                max_workers=4,
            )
            if not needs_index_status(ruleset):
                platform_data.pop("indexes_status", None)
    flat["platform"] = platform_data
    return flat


def _build_results(
    df_records: list[dict[str, Any]],
    drift_by_rule: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], RunSummary]:
    """Convert validation DataFrame records → result dicts + summary roll-up.

    ``drift_by_rule`` maps rule_number → drift sub-dict (may be empty); we
    merge those onto the corresponding result so external alerters see drift
    inline with the rule context.
    """
    summary = RunSummary()
    out: list[dict[str, Any]] = []

    for rec in df_records:
        status = str(rec.get("status", "")).upper()
        if status == "PASS":
            summary.pass_count += 1
        elif status == "FAIL":
            summary.fail_count += 1
        elif status == "SKIP":
            summary.skip_count += 1
        elif status == "ERROR":
            summary.error_count += 1

        rule_number = str(rec.get("rule_number", ""))
        result = {
            "rule_number": rule_number,
            "name": rec.get("name", ""),
            "category": rec.get("category", ""),
            "severity": rec.get("severity", ""),
            "status": status,
            "path": rec.get("path", ""),
            "operator": rec.get("operator", ""),
            "expected": rec.get("expected"),
            "actual": rec.get("actual"),
            "message": rec.get("recommendations", ""),
        }
        drift = drift_by_rule.get(rule_number)
        if drift:
            result["drift"] = drift
            summary.drifted_count += 1
        out.append(result)

    return out, summary


def run_once(*, environment: str | None = None) -> RunResult:
    """Execute one continuous-audit cycle and return the run report.

    Holds an exclusive ``storage.env_lock`` for the entire run so concurrent
    invocations from the WebUI scheduler, OS timer (systemd / launchd), and
    manual CLI ``run-once`` cannot collide on the OAuth fetch, alerts.json,
    or latest.json. Cross-process safe — the flock is on a real file in the
    env directory.

    Side effects:
        - writes the run JSON to ``~/.atlas/continuous/<env>/runs/<run_id>.json``
        - refreshes the ``latest.json`` pointer
        - appends drift events (if any) to ``events.ndjson``
        - updates ``alerts.json`` aggregate state
        - updates the scheduler heartbeat in ``status.json``
        - prunes old runs per ``retain_runs``
    """
    context = ctx()
    config = context.config
    env_name = environment or context.active_environment or ""

    with storage.env_lock(env_name):
        return _run_once_locked(env_name=env_name, context=context, config=config)


def _run_once_locked(*, env_name: str, context: Any, config: Any) -> RunResult:
    """Body of run_once executed while ``storage.env_lock`` is held."""

    # Load the configured ruleset (may differ from the globally-active one).
    from platform_atlas.continuous.runtime import read_settings
    ca_settings = read_settings(env_name)
    ruleset_obj = _load_ruleset_for_settings(ca_settings, context)
    # ruleset_obj.ruleset is the metadata block only (id, version, …).
    # Both required_endpoints() and validate() need a top-level "rules" key,
    # so merge the metadata with the tier-filtered rules list.
    ruleset_dict = {**ruleset_obj.ruleset, "rules": ruleset_obj.rules}
    started_iso = _now_iso()
    started_ts = time.time()
    run_id = storage.make_run_id(env_name)

    logger.info("Continuous audit starting: env=%s run_id=%s", env_name or "_default", run_id)

    # ── Capture (narrow, Platform-only) ──────────────────────────────
    capture_error: str | None = None
    structured: dict[str, Any] = {}
    try:
        flat = _capture_platform_only(ruleset_dict)
        structured = reshape_capture(flat)
    except Exception as exc:  # noqa: BLE001 — surface as capture_error in the report
        capture_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Continuous audit capture failed (env=%s): %s", env_name, capture_error)

    # ── Validate (against the active ruleset) ────────────────────────
    df_records: list[dict[str, Any]] = []
    if not capture_error:
        try:
            limited = finalize_capture(
                structured_data=structured,
                rules=ruleset_obj.as_rules_dict(),
                ruleset=ruleset_obj,
                config=config,
                modules_ran=["platform"],
            )
            from platform_atlas.validation.validation_engine import validate
            df = validate(ruleset_dict, limited, headless=True)
            df_records = df.to_dict(orient="records")
        except Exception as exc:  # noqa: BLE001
            capture_error = f"validation failure: {type(exc).__name__}: {exc}"
            logger.warning("Continuous audit validation failed (env=%s): %s", env_name, capture_error)

    # ── Drift detection vs previous run ──────────────────────────────
    drift_by_rule: dict[str, dict[str, Any]] = {}
    drift_events: list[dict[str, Any]] = []
    previous_unreadable = False
    if df_records:
        previous = storage.read_latest(env_name)
        if previous is None and storage.list_runs(env_name):
            # Files exist on disk but neither latest.json nor the newest run
            # JSON could be parsed — surface the gap rather than silently
            # treating this as the first run.
            previous_unreadable = True
            logger.warning(
                "Continuous audit (env=%s): previous run unreadable; drift detection skipped this cycle",
                env_name or "_default",
            )
        drift_by_rule, drift_events = compute_drift(
            current_records=df_records,
            previous_run=previous,
            current_run_id=run_id,
            detected_at=_now_iso(),
        )

    # ── Build the run report ─────────────────────────────────────────
    results, summary = _build_results(df_records, drift_by_rule)
    finished_iso = _now_iso()
    duration_ms = int((time.time() - started_ts) * 1000)

    run = RunResult(
        run_id=run_id,
        environment=env_name,
        started_at=started_iso,
        finished_at=finished_iso,
        duration_ms=duration_ms,
        ruleset_id=ruleset_dict.get("id", ""),
        ruleset_version=str(ruleset_dict.get("version", "")),
        summary=summary,
        results=results,
        capture_error=capture_error,
        previous_unreadable=previous_unreadable,
    )
    run_dict = run.to_dict()

    # ── Persist ──────────────────────────────────────────────────────
    storage.write_run(env_name, run_dict)
    if drift_events:
        # events.ndjson is the audit-grade timeline — it always carries the
        # full set, regardless of policy/watchlist. Filtering only affects
        # what surfaces as alerts and outbound notifications below.
        storage.append_events(env_name, drift_events)

    # Apply per-environment alert policy + rule watchlist before alert state
    # is updated. Default settings (policy=any, watchlist=()) are a no-op so
    # existing installs see identical behavior on upgrade.
    alertable_events = filter_drift_events_for_alerts(
        drift_events,
        policy=ca_settings.alert_policy,
        watchlist=ca_settings.watchlist,
    )
    if drift_events and len(alertable_events) != len(drift_events):
        logger.info(
            "Continuous audit (env=%s): %d/%d drift events kept after policy=%s, watchlist=%d rules",
            env_name or "_default",
            len(alertable_events), len(drift_events),
            ca_settings.alert_policy, len(ca_settings.watchlist),
        )
    transitions = update_alert_state(env_name, alertable_events)

    # TODO: Log file watching — deferred to a later version of Atlas.
    # Re-enable by uncommenting this block and the lw_transitions merge below.
    # lw_transitions: list[Any] = []
    # if ca_settings.log_watch_enabled and ca_settings.log_watches:
    #     try:
    #         from platform_atlas.continuous.log_watcher import (
    #             run_log_watches,
    #             log_watch_alert_to_drift_event,
    #         )
    #         _lw_target: dict | None = None
    #         for _t in (config.targets or []):
    #             if _t.get("transport") in ("ssh", "paramiko"):
    #                 _lw_target = _t
    #                 break
    #         lw_alerts = run_log_watches(ca_settings.log_watches, target_dict=_lw_target)
    #         if lw_alerts:
    #             lw_events = [log_watch_alert_to_drift_event(a, run_id) for a in lw_alerts]
    #             storage.append_events(env_name, lw_events)
    #             lw_transitions = update_alert_state(env_name, lw_events)
    #             logger.info("Log-watch (env=%s): %d alert(s) triggered", env_name or "_default", len(lw_alerts))
    #     except Exception as exc:  # noqa: BLE001
    #         logger.warning("Log-watch run failed (env=%s): %s", env_name, exc)

    # ── Outbound notifications (only on alert-state transitions) ─────
    all_transitions = list(transitions)  # + lw_transitions when log-watch is re-enabled
    if all_transitions:
        try:
            from platform_atlas.continuous import notifications
            notifications.fire_drift_alerts(env_name, all_transitions)
        except Exception as exc:  # noqa: BLE001 — notification failure must not fail the run
            logger.warning("Continuous audit notification dispatch failed (env=%s): %s", env_name, exc)

    # Heartbeat for the always-on banner / topbar pill.
    storage.write_status(env_name, {
        "last_run_id": run_id,
        "last_started_at": started_iso,
        "last_finished_at": finished_iso,
        "last_duration_ms": duration_ms,
        "last_status": "error" if capture_error else "ok",
        "last_error": capture_error,
        "last_summary": summary.to_dict(),
        "previous_unreadable": previous_unreadable,
        "atlas_version": __version__,
        "host": gethostname(),
    })

    # Retention pruning. Read settings for the env we just ran against.
    try:
        from platform_atlas.continuous.runtime import read_settings
        settings = read_settings(env_name)
        storage.prune_runs(env_name, retain=settings.retain_runs)
    except Exception as exc:  # noqa: BLE001 — pruning errors must not fail the run
        logger.debug("Run pruning skipped: %s", exc)

    logger.info(
        "Continuous audit complete: env=%s pass=%d fail=%d skip=%d drift=%d duration=%dms",
        env_name or "_default",
        summary.pass_count, summary.fail_count, summary.skip_count, summary.drifted_count,
        duration_ms,
    )
    return run
