# pylint: disable=line-too-long
"""
ATLAS // WebUI Viewmodel Builder

Builds the typed JSON contract (``06_webui_viewmodel.json``) that the WebUI
frontend consumes to render the unified Compliance / Operational / Architecture
view as a single tabbed experience.

The viewmodel is data-only. The WebUI applies its own theme uniformly across
all routes — no theme/branding is embedded here. The standalone HTML reports
(03/04/05) keep their own self-contained branding for portability and export;
this file is the parallel surface that powers the in-WebUI experience.

Two entry points share a single builder:

* ``write_webui_viewmodel`` — called at report-generation time from
  ``handle_session_run_report`` in ``core/handlers/session.py``. Inputs are
  already in scope (validation DataFrame, extended results, architecture
  data, optional operational report). Writes the file atomically.

* ``load_or_build_viewmodel`` — called at request time by the WebUI's
  viewmodel route. Returns the cached file if present and current; otherwise
  rebuilds from ``01_capture.json`` + ``02_validation.parquet`` so sessions
  captured before this feature lands still render correctly.

Bump ``SCHEMA_VERSION`` whenever the shape changes. ``load_or_build_viewmodel``
treats a cached file with a stale ``schema_version`` as missing and rebuilds.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from platform_atlas.core._version import __version__
from platform_atlas.reporting.reporting_engine import (
    _build_metadata,
    _build_summary,
    _clean_architecture,
    _ARCH_LABELS,
    _EXCLUDED_CHECK_IDS,
)

logger = logging.getLogger(__name__)


SCHEMA_VERSION = "1.2"

# Status normalization — mirrors the value sets used by report_renderer.calculate_stats
# and the chart data generators so per-category / per-severity counts agree across
# every surface the user sees.
_PASS_VALUES = frozenset({"PASS", "COMPLIANT", "OK", "SUCCESS", "TRUE"})
_FAIL_VALUES = frozenset({"FAIL", "NON-COMPLIANT", "FALSE", "CRITICAL"})
_SKIP_VALUES = frozenset({"SKIP", "SKIPPED", "N/A", "NA"})
_ERROR_VALUES = frozenset({"ERROR"})

# Severity ranking — matches the order used by generate_priority_actions.
# Unknown severities sort last (rank 99) so they never crowd out classified ones.
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "high": 1, "medium": 2, "info": 2, "low": 3}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_webui_viewmodel(
    df: pd.DataFrame,
    *,
    extended_results: list[dict] | None = None,
    architecture_data: dict[str, Any] | None = None,
    operational_report: Any = None,
    rbac_data: dict | None = None,
    session_name: str = "",
    modules_ran: list[str] | None = None,
    tier: str = "extended",
    platform_uri: str = "",
    deployment_mode: str = "",
) -> dict[str, Any]:
    """Assemble the full viewmodel from in-memory data.

    Pure function — no I/O. Used by both the write-at-report-time path and
    the on-the-fly fallback. Both callers share the same builder so the
    fallback is guaranteed to produce a payload with the same shape.

    Args:
        df: Validation results DataFrame. ``df.attrs`` should already carry
            the metadata block populated by ``validate_from_files``; if it
            doesn't, callers must rehydrate first via
            ``session_manager.rehydrate_validation_attrs``.
        extended_results: Extended validation check dicts (output of
            ``ExtendedCheckResult.to_dict``). Log-analysis checks land in the
            ``operational`` block; everything else lands in ``architecture``.
        architecture_data: Architecture overview dict from
            ``ArchitectureProgress.completed`` or the capture-file fallback.
            Always emitted with every known section key — absent sections are
            ``null`` for schema consistency.
        operational_report: An ``OperationalReport`` (MongoDB pipelines), or
            ``None`` for Standard tier / sessions without operational data.
        session_name: Session name string. Falls back to ``df.attrs`` if blank.
        modules_ran: List of capture module names that ran. Falls back to
            ``df.attrs`` if not provided.
        tier: ``"standard"`` or ``"extended"``. Persisted under ``session.tier``.
    """
    # Reuse the existing reporting_engine builders so the viewmodel mirrors
    # the JSON/Markdown export contract for fields they share. Different
    # surfaces, same numbers — avoids the "two reports, two truths" trap.
    meta = _build_metadata(df, session_name=session_name, modules_ran=modules_ran)
    summary = _build_summary(df)

    # Override tier if passed explicitly (the report handler resolves it from
    # session metadata, which can differ from df.attrs after activate-from-disk).
    if tier:
        meta["tier"] = tier

    return {
        "schema_version": SCHEMA_VERSION,
        "atlas_version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session": _build_session_block(meta, session_name, platform_uri=platform_uri, deployment_mode=deployment_mode),
        "compliance": _build_compliance_block(df, summary),
        "operational": _build_operational_block(extended_results or [], operational_report),
        "architecture": _build_architecture_block(extended_results or [], architecture_data or {}),
        "rbac": rbac_data or {},
    }


def _atomic_write_viewmodel(output_path: Path, viewmodel: dict[str, Any]) -> Path:
    """Atomically write ``viewmodel`` JSON to ``output_path``.

    Temp-file + ``os.replace`` so a crash mid-write cannot leave a
    half-written ``06_webui_viewmodel.json`` on disk that the WebUI
    would then fail to parse. Used by both the report-generation path
    and the on-the-fly rebuild path so a single request's rebuild
    becomes durable for every subsequent request.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_",
        suffix="_" + output_path.name,
        dir=str(output_path.parent),
    )
    os.close(fd)
    try:
        Path(tmp_path).write_text(
            json.dumps(viewmodel, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return output_path


def write_webui_viewmodel(
    output_path: Path,
    df: pd.DataFrame,
    *,
    extended_results: list[dict] | None = None,
    architecture_data: dict[str, Any] | None = None,
    operational_report: Any = None,
    rbac_data: dict | None = None,
    session_name: str = "",
    modules_ran: list[str] | None = None,
    tier: str = "extended",
    platform_uri: str = "",
    deployment_mode: str = "",
) -> Path:
    """Build the viewmodel and write it atomically to ``output_path``.

    Atomic via temp-file + ``os.replace`` so a crash mid-write cannot leave
    a half-written ``06_webui_viewmodel.json`` on disk that the WebUI would
    then fail to parse. Mirrors the parquet write pattern in
    ``handle_session_run_validate``.
    """
    viewmodel = build_webui_viewmodel(
        df,
        extended_results=extended_results,
        architecture_data=architecture_data,
        operational_report=operational_report,
        rbac_data=rbac_data,
        session_name=session_name,
        modules_ran=modules_ran,
        tier=tier,
        platform_uri=platform_uri,
        deployment_mode=deployment_mode,
    )
    return _atomic_write_viewmodel(output_path, viewmodel)


def load_or_build_viewmodel(session: Any, *, force_rebuild: bool = False) -> dict[str, Any]:
    """Return the WebUI viewmodel for ``session``.

    Resolution order:
      1. If ``06_webui_viewmodel.json`` exists with the current
         ``SCHEMA_VERSION`` (and ``force_rebuild`` is False), return it
         as-is. **No validation runs in this path.**
      2. Otherwise rebuild from ``01_capture.json`` +
         ``02_validation.parquet`` on the fly, **persist the result to
         disk**, and return it. Used for sessions captured before this
         feature shipped, sessions whose cache was deleted, schema
         upgrades, and the explicit ``?refresh=1`` user-initiated path.

    The persistence step is the important fix: without it, every
    request hits the rebuild path and re-runs extended validation,
    which calls out to GitLab for IAG/IAP version data. Writing the
    viewmodel after rebuild means the next request — and every one
    after — gets the cache hit and runs nothing.

    Raises ``FileNotFoundError`` if neither the cached viewmodel nor
    the underlying parquet exists — the caller (typically a FastAPI
    route) should translate this to a 404.
    """
    cached_path = session.directory / "06_webui_viewmodel.json"
    if cached_path.exists() and not force_rebuild:
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            if cached.get("schema_version") == SCHEMA_VERSION:
                return cached
            logger.info(
                "WebUI viewmodel for session '%s' has stale schema (cached=%s, current=%s) — rebuilding",
                session.name, cached.get("schema_version"), SCHEMA_VERSION,
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "WebUI viewmodel cache unreadable for session '%s' (%s) — rebuilding",
                session.name, exc,
            )

    if not session.validation_file.exists():
        raise FileNotFoundError(
            f"Session '{session.name}' has no validation parquet — cannot build viewmodel."
        )

    if force_rebuild:
        logger.info("Rebuilding WebUI viewmodel for session '%s' (user-requested refresh)", session.name)

    df = pd.read_parquet(session.validation_file, engine="pyarrow")

    # Parquet round-trip drops df.attrs. Rehydrate from session metadata so
    # the meta/session block carries org/tier/ruleset/etc. — see CLAUDE.md
    # hard-won lesson #3.
    from platform_atlas.core.session_manager import rehydrate_validation_attrs
    rehydrate_validation_attrs(df, session)

    extended_results = _load_extended_results_for_fallback(df, session)
    architecture_data = _load_architecture_data_for_fallback(session)
    operational_report = _load_operational_report_for_fallback(session)

    # Load RBAC data when available (opt-in; returns {} when not collected)
    rbac_data: dict = {}
    try:
        from platform_atlas.core.handlers.session import _load_rbac_data
        rbac_data = _load_rbac_data(getattr(session, "capture_file", None))
    except Exception as _rbac_exc:
        logger.debug("Could not load RBAC data for session '%s': %s", session.name, _rbac_exc)

    # Read platform_uri and deployment_mode from the session's bound environment
    # file so the WebUI can build deep-links and run spec comparisons.
    _platform_uri = ""
    _deployment_mode = ""
    _env_name = getattr(session.metadata, "environment", "") or ""
    if _env_name:
        from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
        _env_file = ATLAS_ENVIRONMENTS_DIR / f"{_env_name}.json"
        try:
            _env_data = json.loads(_env_file.read_text(encoding="utf-8"))
            _platform_uri = _env_data.get("platform_uri", "")
            _deployment_mode = (_env_data.get("deployment") or {}).get("mode", "")
        except (OSError, json.JSONDecodeError):
            pass

    viewmodel = build_webui_viewmodel(
        df,
        extended_results=extended_results,
        architecture_data=architecture_data,
        operational_report=operational_report,
        rbac_data=rbac_data,
        session_name=session.name,
        modules_ran=session.metadata.modules_ran,
        tier=getattr(session.metadata, "tier", None) or df.attrs.get("tier") or "extended",
        platform_uri=_platform_uri,
        deployment_mode=_deployment_mode,
    )

    # Persist so subsequent requests skip the rebuild path entirely
    # (which is what avoids re-running extended validation + the
    # GitLab call). Failure here is non-fatal — the user still gets
    # the data we just built; we just don't get the cache benefit
    # next time.
    try:
        _atomic_write_viewmodel(cached_path, viewmodel)
        logger.info("Persisted WebUI viewmodel cache for session '%s'", session.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not persist WebUI viewmodel cache for session '%s' (%s) — "
            "will rebuild on next request",
            session.name, exc,
        )

    return viewmodel


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Section builders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_session_block(meta: dict[str, Any], session_name: str, *, platform_uri: str = "", deployment_mode: str = "") -> dict[str, Any]:
    """Restructure the flat ``_build_metadata`` output into a nested session block.

    The viewmodel collapses ``ruleset_id``/``ruleset_version``/``ruleset_profile``
    into a single ``ruleset`` object so the frontend can render
    ``{{ ruleset.id }} v{{ ruleset.version }} ({{ ruleset.profile }})`` without
    composing strings from three loose fields.
    """
    return {
        "name": session_name or meta.get("session", ""),
        "tier": meta.get("tier", "extended"),
        "environment": meta.get("environment", ""),
        "organization_name": meta.get("organization", "Unknown"),
        "platform_version": meta.get("platform_version", "Unknown"),
        "hostname": meta.get("hostname", "Unknown"),
        "captured_at": meta.get("captured_at", ""),
        "ruleset": {
            "id": meta.get("ruleset_id", ""),
            "version": meta.get("ruleset_version", ""),
            "profile": meta.get("ruleset_profile", ""),
        },
        "modules_ran": meta.get("modules_ran") or [],
        "platform_uri": platform_uri,
        "deployment_mode": deployment_mode,
    }


def _build_compliance_block(df: pd.DataFrame, summary: dict[str, Any]) -> dict[str, Any]:
    """Build the compliance block: summary, by_category, by_severity,
    priority_actions, the full rule list, and the per-failing-rule fix
    knowledgebase entries."""
    return {
        "summary": summary,
        "by_category": _group_counts(df, "category"),
        "by_severity": _group_counts(df, "severity", order=_SEVERITY_ORDER),
        "priority_actions": _priority_actions(df, max_actions=5),
        "rules": _rule_rows(df),
        "fixes": _fixes_for_failures(df),
    }


def _build_operational_block(
    extended_results: list[dict],
    operational_report: Any,
) -> dict[str, Any]:
    """Build the operational block: log-analysis checks + MongoDB pipelines.

    Log checks are identified by the same ``_EXCLUDED_CHECK_IDS`` set the
    JSON/Markdown exporters use to *exclude* them — we just reverse the
    filter so log checks land here and non-log checks land in architecture.
    """
    log_sections = [
        _normalize_extended_check(c)
        for c in extended_results
        if isinstance(c, dict) and c.get("check_id") in _EXCLUDED_CHECK_IDS
    ]

    pipelines: list[dict[str, Any]] = []
    mongodb_summary: dict[str, Any] = {
        "pipeline_count": 0,
        "success_count": 0,
        "error_count": 0,
        "total_rows": 0,
        "generated_at": "",
    }
    if operational_report is not None:
        for result in getattr(operational_report, "results", []) or []:
            pipelines.append(_normalize_pipeline_result(result))
        mongodb_summary = {
            "pipeline_count": getattr(operational_report, "pipeline_count", len(pipelines)),
            "success_count": getattr(operational_report, "success_count", 0),
            "error_count": getattr(operational_report, "error_count", 0),
            "total_rows": getattr(operational_report, "total_rows", 0),
            "generated_at": getattr(operational_report, "generated_at", ""),
        }

    return {
        "log_sections": log_sections,
        "mongodb_pipelines": pipelines,
        "mongodb_summary": mongodb_summary,
    }


def _build_architecture_block(
    extended_results: list[dict],
    architecture_data: dict[str, Any],
) -> dict[str, Any]:
    """Build the architecture block: non-log extended checks + raw section data.

    Sections pass through unchanged via ``_clean_architecture`` — every key in
    ``_ARCH_LABELS`` is emitted (null when absent) so the frontend can render
    a stable nav without conditional-ness on key presence.
    """
    extended_checks = [
        _normalize_extended_check(c)
        for c in extended_results
        if isinstance(c, dict) and c.get("check_id") not in _EXCLUDED_CHECK_IDS
    ]

    sections = _clean_architecture(architecture_data)
    section_labels = {key: _ARCH_LABELS.get(key, key.replace("_", " ").title()) for key in sections}

    return {
        "extended_checks": extended_checks,
        "sections": sections,
        "section_labels": section_labels,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Row / group helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _group_counts(
    df: pd.DataFrame,
    column: str,
    order: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Group the DataFrame by ``column`` and emit per-group status counts.

    Returns a list (not a dict) because order matters for charts: the
    frontend reads it left-to-right. When ``order`` is supplied, groups
    sort by that ranking; otherwise they sort alphabetically by name.
    """
    if column not in df.columns:
        return []

    status_upper = df["status"].astype(str).str.upper()
    out: list[dict[str, Any]] = []
    for name, group in df.groupby(column, sort=False):
        sub = status_upper.loc[group.index]
        out.append({
            "name": str(name),
            "compliant": int(sub.isin(_PASS_VALUES).sum()),
            "non_compliant": int(sub.isin(_FAIL_VALUES).sum()),
            "skipped": int(sub.isin(_SKIP_VALUES).sum()),
            "errors": int(sub.isin(_ERROR_VALUES).sum()),
            "total": len(group),
        })

    if order:
        out.sort(key=lambda row: order.get(row["name"].lower(), 99))
    else:
        out.sort(key=lambda row: row["name"].lower())
    return out


def _priority_actions(df: pd.DataFrame, max_actions: int = 5) -> list[dict[str, Any]]:
    """Return the top-N failing rules ordered by severity.

    Ordering matches ``generate_priority_actions`` — critical first, then
    warning, then info, with unknown severities last. Used to render the
    "what to fix first" panel on the Compliance tab.
    """
    if "status" not in df.columns:
        return []

    failures = df[df["status"].astype(str).str.upper() == "FAIL"].copy()
    if failures.empty:
        return []

    if "severity" in failures.columns:
        failures["_sev_rank"] = (
            failures["severity"].astype(str).str.lower().map(_SEVERITY_ORDER).fillna(99)
        )
        failures = failures.sort_values("_sev_rank")

    rows: list[dict[str, Any]] = []
    for record in failures.head(max_actions).to_dict(orient="records"):
        rows.append({
            "rule_number": _safe_str(record.get("rule_number")),
            "name": _safe_str(record.get("name")),
            "category": _safe_str(record.get("category")),
            "severity": _safe_str(record.get("severity")).lower() or "info",
            "path": _safe_str(record.get("path")),
            "expected": _safe_str(record.get("expected")),
            "actual": _safe_str(record.get("actual")),
            "recommendations": _safe_str(record.get("recommendations")),
        })
    return rows


def _fixes_for_failures(df: pd.DataFrame) -> dict[str, dict[str, str]]:
    """Return knowledgebase fix entries keyed by rule_number for FAIL rules.

    Mirrors ``fixes_for_modal`` in ``report_renderer.py`` — only Non-Compliant
    rules get an entry so the JSON payload stays small. The WebUI slide-over
    looks the rule up by ``rule_number`` when it opens the detail panel for a
    failing row.

    A missing or unparseable ``RULES_KNOWLEDGEBASE.md`` is non-fatal: we log
    and return an empty dict so the rest of the report still renders.
    """
    if "status" not in df.columns or "rule_number" not in df.columns:
        return {}

    try:
        from platform_atlas.core.knowledgebase import load_knowledgebase
        kb = load_knowledgebase()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not load RULES_KNOWLEDGEBASE.md (%s) — fix instructions will be absent in WebUI viewmodel",
            exc,
        )
        return {}
    if not kb:
        return {}

    out: dict[str, dict[str, str]] = {}
    fail_mask = df["status"].astype(str).str.upper().isin(_FAIL_VALUES)
    for rule_id in df.loc[fail_mask, "rule_number"].dropna():
        rule_id = _safe_str(rule_id)
        if not rule_id:
            continue
        fix = kb.get(rule_id)
        if not fix:
            continue
        out[rule_id] = {
            "title": fix.title,
            "purpose": fix.purpose,
            "how_to_fix": fix.how_to_fix,
        }
    return out


def _rule_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert every DataFrame row to a JSON-safe dict for the WebUI rules table.

    Includes every column the validator emits so the WebUI can render rich
    detail (path, operator, expected/actual, message, recommendations) without
    going back to the parquet. Missing columns are silently skipped — the
    frontend tolerates absent keys.
    """
    candidate_cols = [
        "rule_number", "name", "category", "severity", "status",
        "path", "operator", "expected", "actual", "message", "recommendations",
        # Drives the WebUI's color-coded skip callout: "unreachable" /
        # "no_data" / "conditional". None for non-skip rows.
        "skip_kind",
    ]
    available = [c for c in candidate_cols if c in df.columns]

    # Pre-compute the suppressed mask vectorially to avoid per-row pd.isna() calls.
    if "user_suppressed" in df.columns:
        suppressed_series = df["user_suppressed"].fillna(False).astype(bool)
    else:
        suppressed_series = pd.Series(False, index=df.index, dtype=bool)

    out: list[dict[str, Any]] = []
    for record, is_suppressed in zip(df[available].to_dict(orient="records"), suppressed_series):
        record = {col: _json_safe(val) for col, val in record.items()}
        # Normalize status to upper so the frontend never has to guess casing.
        if "status" in record and isinstance(record["status"], str):
            record["status"] = record["status"].upper()
        # skip_kind drives the WebUI's color-coded skip callout. Only the three
        # string kinds are valid: collapse everything else to null — NaN from
        # non-skip rows (the column round-trips through parquet as float NaN,
        # which _json_safe leaves untouched) and user-suppressed rows (the
        # WebUI has no suppression UI yet, so they stay plain skips, matching
        # the standalone report which excludes them from its skip map).
        sk = record.get("skip_kind")
        record["skip_kind"] = sk if (isinstance(sk, str) and sk and not is_suppressed) else None
        out.append(record)
    return out


def _normalize_extended_check(check: dict[str, Any]) -> dict[str, Any]:
    """Return an extended check dict with a stable shape.

    Matches the export contract from ``export_json_report`` so consumers
    parsing both surfaces see identical fields per check.
    """
    return {
        "check_id": check.get("check_id", ""),
        "name": check.get("name", ""),
        "category": check.get("category", ""),
        "status": check.get("status", ""),
        "message": check.get("message", ""),
        "remediation": check.get("remediation", ""),
        "details": _json_safe(check.get("details", {})),
    }


def _normalize_pipeline_result(result: Any) -> dict[str, Any]:
    """Normalize a ``PipelineResult`` to a JSON-safe dict.

    Accepts either a ``PipelineResult`` dataclass (live operational report)
    or a plain dict (deserialized from ``04_operational.json``).
    """
    if isinstance(result, dict):
        rows = result.get("rows") or []
        columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        return {
            "name": result.get("name", ""),
            "description": result.get("description", ""),
            "collection": result.get("collection", ""),
            "row_count": result.get("row_count", len(rows)),
            "duration_ms": result.get("duration_ms", 0.0),
            "error": result.get("error"),
            "status": "error" if result.get("error") else "success",
            "columns": columns,
            "rows": _json_safe(rows),
        }

    rows = list(getattr(result, "rows", []) or [])
    return {
        "name": getattr(result, "name", ""),
        "description": getattr(result, "description", ""),
        "collection": getattr(result, "collection", ""),
        "row_count": getattr(result, "row_count", len(rows)),
        "duration_ms": getattr(result, "duration_ms", 0.0),
        "error": getattr(result, "error", None),
        "status": "success" if getattr(result, "succeeded", result is not None) else "error",
        "columns": getattr(result, "columns", None) or (list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []),
        "rows": _json_safe(rows),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fallback loaders (on-the-fly viewmodel construction)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_extended_results_for_fallback(df: pd.DataFrame, session: Any) -> list[dict]:
    """Re-run extended validation against the captured data.

    The validation parquet round-trip drops ``df.attrs['extended_results']``,
    so for sessions without a cached viewmodel we have to recompute. Extended
    checks are pure functions of the capture JSON (no network), so re-running
    is cheap and deterministic.
    """
    cached = df.attrs.get("extended_results", []) if df.attrs else []
    if cached:
        return cached

    if not session.capture_file.exists():
        return []

    try:
        from platform_atlas.validation.extended_validation import run_extended_validation
        from platform_atlas.core.json_utils import load_json

        capture_data = load_json(session.capture_file)

        # Mirror the log-analysis merge that validate_from_files does, so
        # log checks see the same data they did at validation time.
        if session.logs_file.exists():
            try:
                logs_data = json.loads(session.logs_file.read_text(encoding="utf-8"))
                platform = capture_data.setdefault("platform", {})
                if "log_analysis" in logs_data:
                    platform["log_analysis"] = logs_data["log_analysis"]
                if "webserver_logs" in logs_data:
                    platform["webserver_logs"] = logs_data["webserver_logs"]
                if "mongo_log_analysis" in logs_data:
                    mongo = capture_data.setdefault("mongo", {})
                    mongo["log_analysis"] = logs_data["mongo_log_analysis"]
            except Exception as exc:  # noqa: BLE001
                logger.debug("Logs merge failed during viewmodel fallback: %s", exc)

        check_results = run_extended_validation(capture_data)
        return [r.to_dict() for r in check_results]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Extended validation fallback failed for session '%s': %s", session.name, exc)
        return []


def _load_architecture_data_for_fallback(session: Any) -> dict[str, Any]:
    """Load the architecture overview for the session's environment.

    Resolution order matches ``_load_architecture_data`` in the session
    handler: per-env architecture store first (authoritative), capture-file
    fallback second.
    """
    environment = (session.metadata.environment or "").strip()
    try:
        from platform_atlas.capture.collectors.manual import ArchitectureProgress
        progress = ArchitectureProgress.load(environment)
        if progress.completed:
            return progress.completed
    except Exception as exc:  # noqa: BLE001
        logger.debug("Architecture store load failed for env=%s: %s", environment or "_default", exc)

    if session.capture_file.exists():
        try:
            cap = json.loads(session.capture_file.read_text(encoding="utf-8"))
            arch = cap.get("checks", {}).get("architecture_validation")
            if arch:
                return arch
        except Exception as exc:  # noqa: BLE001
            logger.debug("Architecture capture-file fallback failed: %s", exc)

    return {}


def _load_operational_report_for_fallback(session: Any) -> Any:
    """Load the saved operational report (MongoDB pipelines), or None."""
    op_path = session.operational_data_file
    if not op_path.exists():
        return None
    try:
        from platform_atlas.reporting.operational_engine import OperationalReport
        return OperationalReport.from_json(op_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Operational report load failed for session '%s': %s", session.name, exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Coercion helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _safe_str(value: Any) -> str:
    """Coerce any value (including pandas NaN) to a string, with empty-string for null."""
    if value is None:
        return ""
    try:
        if pd.isna(value):  # pylint: disable=no-member
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _json_safe(value: Any) -> Any:
    """Recursively coerce a value to JSON-serializable form.

    Handles pandas/numpy scalars, datetimes, sets, and arbitrary objects
    by falling through to ``str()``. Mirrors ``reporting_engine._json_safe``
    so nested structures serialize the same way across both surfaces.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return [_json_safe(v) for v in sorted(value, key=str)]
    try:
        if pd.isna(value):  # pylint: disable=no-member
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(value)
