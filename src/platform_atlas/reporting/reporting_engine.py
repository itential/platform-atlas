# pylint: disable=line-too-long
"""
ATLAS // Reporting Engine

Handles all non-HTML report generation: JSON, Markdown, and CSV exports.
JSON and Markdown formats include the full report data: metadata, validation
results, extended validation checks, and architecture overview.
"""

import html as html_mod
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from platform_atlas.core.paths import REPORT_JSON_SCHEMA
from platform_atlas.core._version import __version__
from platform_atlas.reporting.scoring import rating_for, weighted_pass_percent
from platform_atlas.validation.results import ValidationResults

logger = logging.getLogger(__name__)


# Human-readable labels for architecture section keys
_ARCH_LABELS = {
    "environment": "Environment",
    "platform": "Platform (IAP)",
    "gateway4": "Automation Gateway 4",
    "gateway5": "Automation Gateway 5",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "load_balancer": "Load Balancer",
    "kubernetes": "Kubernetes",
    "monitoring": "Monitoring & Health Checks",
    "network_security": "Network & Security",
    "vulnerability_assessments": "Vulnerability Assessments",
    "artificial_intelligence": "Artificial Intelligence",
}

# Fields to exclude from reports (logs, raw data, internal keys)
_EXCLUDED_ARCH_KEYS = frozenset({
    "platform_logs", "webserver_logs", "log_analysis",
    "platform_log_analysis", "webserver_log_analysis",
    "mongo_log_analysis",
})

# Extended check IDs to exclude from JSON/Markdown exports (log analysis)
_EXCLUDED_CHECK_IDS = frozenset({
    "platform_log_analysis",
    "webserver_log_analysis",
    "mongo_log_analysis",
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shared helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_metadata(
    results: ValidationResults, session_name: str = "", modules_ran: list[str] | None = None
) -> dict[str, Any]:
    """Build the metadata block shared by JSON and Markdown exports."""
    meta = results.metadata
    return {
        "atlas_version": __version__,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session": session_name,
        "organization": meta.get("organization_name", "Unknown"),
        "environment": meta.get("environment", ""),
        "tier": meta.get("tier", "extended"),
        "hostname": meta.get("hostname", "Unknown"),
        "platform_version": meta.get("platform_ver", "Unknown"),
        "ruleset_id": meta.get("ruleset_id", "Unknown"),
        "ruleset_version": meta.get("ruleset_version", "Unknown"),
        "ruleset_profile": meta.get("ruleset_profile", ""),
        "captured_at": meta.get("captured_at", "Unknown"),
        "modules_ran": modules_ran or meta.get("modules_ran", []),
    }


def _build_summary(results: ValidationResults) -> dict[str, Any]:
    """Build the summary statistics block.

    Pass rate excludes skipped rules (matches HTML report calculation).
    """
    total = len(results)
    statuses = [row.get("status") for row in results.rows]
    passed = statuses.count("PASS")
    failed = statuses.count("FAIL")
    skipped = sum(1 for s in statuses if s in {"SKIP", "SKIPPED", "N/A", "NA"})
    errored = statuses.count("ERROR")

    # Exclude skipped from denominator (matches calculate_stats in report_renderer)
    evaluated = passed + failed + errored
    pass_rate = round((passed / evaluated * 100), 1) if evaluated > 0 else 0.0

    # Severity-weighted score — same formula, but each rule counts by its
    # severity weight (critical/warning/info) instead of 1. Kept alongside
    # pass_rate above rather than replacing it; this is the number the HTML
    # report's hero gauge leads with, with pass_rate shown on hover.
    weighted_score = weighted_pass_percent(results.rows)

    return {
        "total_rules": total,
        "evaluated": evaluated,
        "compliant": passed,
        "non_compliant": failed,
        "skipped": skipped,
        "errors": errored,
        "pass_rate": pass_rate,
        "health_rating": rating_for(pass_rate),
        "weighted_score": weighted_score,
        "weighted_health_rating": rating_for(weighted_score),
    }


def _clean_architecture(arch_data: dict[str, Any]) -> dict[str, Any]:
    """Clean architecture data for export.

    Always emits every key defined in _ARCH_LABELS so consumers get a consistent
    schema regardless of what was captured. Sections that were excluded, absent,
    or not present in the deployment are set to null rather than omitted.
    """
    cleaned: dict[str, Any] = {}
    for section_key, section_data in arch_data.items():
        if section_key in _EXCLUDED_ARCH_KEYS:
            continue
        if not isinstance(section_data, dict):
            continue
        if section_data.get("present") is False:
            continue
        if section_data.get("deployed_on_kubernetes") is False:
            continue
        cleaned[section_key] = section_data

    # Older architecture-form.html builds wrote the em-dash without surrounding
    # spaces (e.g. "Yes—regularly"); the schema and CLI collector use the spaced
    # form. Coerce the legacy spelling so already-stored data still validates.
    va = cleaned.get("vulnerability_assessments")
    if isinstance(va, dict):
        performs_fix = {
            "Yes—regularly": "Yes — regularly",
            "Yes—ad-hoc / on demand": "Yes — ad-hoc / on demand",
        }
        pa = va.get("performs_assessments")
        if pa in performs_fix:
            cleaned["vulnerability_assessments"] = {**va, "performs_assessments": performs_fix[pa]}

    # Guarantee every known section key is present; null if not captured
    return {key: cleaned.get(key, None) for key in _ARCH_LABELS}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JSON Export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _validate_json_report(report: dict) -> tuple[bool, list[str]]:
    """Validate the assembled report dict against the bundled JSON schema.

    Returns (valid, errors) where errors is an empty list on success.
    Fails gracefully — a missing schema file or import error never aborts export.
    """
    try:
        import jsonschema
        schema = json.loads(REPORT_JSON_SCHEMA.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        errors = [
            f"{' > '.join(str(p) for p in e.absolute_path) or 'root'}: {e.message}"
            for e in validator.iter_errors(report)
        ]
        return (len(errors) == 0, errors)
    except FileNotFoundError:
        logger.debug("Report JSON schema not found at %s — skipping validation", REPORT_JSON_SCHEMA)
        return (True, [])
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("JSON schema validation skipped: %s", exc)
        return (True, [])


def export_json_report(
    results: ValidationResults,
    output_path: Path,
    *,
    extended_results: list[dict] | None = None,
    architecture_data: dict[str, Any] | None = None,
    session_name: str = "",
    modules_ran: list[str] | None = None,
) -> Path:
    """Export a complete Atlas report as structured JSON.

    Designed for ingestion by Customer360, Salesforce, and other systems
    that need to parse Atlas findings programmatically.

    Top-level structure:
        report.metadata        — session identity, versions, timestamps
        report.summary         — pass/fail counts, health rating
        report.validation      — rule results grouped by category
        report.extended_checks — additional validation findings
        report.architecture    — deployment topology and configuration
    """
    export_cols = [
        "rule_number", "name", "category", "severity",
        "status", "expected", "actual", "message",
    ]

    # Group validation results by category for structured output.
    # Always emit all 8 columns so consumers get a consistent key set; null for absent fields.
    validation_by_category: dict[str, list[dict]] = {}
    for row in results.rows:
        cat = row.get("category") or "other"
        validation_by_category.setdefault(cat, []).append(
            {col: _json_safe(row.get(col)) for col in export_cols}
        )

    # Build extended checks array
    extended = []
    for check in (extended_results or []):
        if isinstance(check, dict):
            check_id = check.get("check_id", "")
            if check_id in _EXCLUDED_CHECK_IDS:
                continue
            extended.append({
                "check_id": check.get("check_id", ""),
                "name": check.get("name", ""),
                "category": check.get("category", ""),
                "status": check.get("status", ""),
                "message": check.get("message", ""),
                "remediation": check.get("remediation", ""),
                "details": check.get("details", {}),
                "deactivated": bool(check.get("deactivated", False)),
            })

    # Assemble the full report
    report = {
        "report": {
            "metadata": _build_metadata(results, session_name, modules_ran),
            "summary": _build_summary(results),
            "validation": validation_by_category,
            "extended_checks": extended,
            "architecture": _clean_architecture(architecture_data or {}),
        }
    }

    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    valid, schema_errors = _validate_json_report(report)
    if not valid:
        logger.warning("JSON report schema validation failed (%d issue(s)):", len(schema_errors))
        for err in schema_errors:
            logger.warning("  • %s", err)

    return output_path, valid, schema_errors


def _json_safe(value: Any) -> Any:
    """Ensure a value is JSON-serializable."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Markdown Export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _md_cell(value: Any) -> str:
    """Escape a value for a Markdown table cell (pipes, newlines, HTML-significant chars)."""
    text = html_mod.escape(str(value), quote=False)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _markdown_table(rows: list[dict], columns: list[str]) -> str:
    """Render rows as a Markdown table (replaces pandas' ``to_markdown()``)."""
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(_md_cell(row.get(c, "")) for c in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, sep, *body])


def export_markdown_report(
    results: ValidationResults,
    output_path: Path,
    *,
    extended_results: list[dict] | None = None,
    architecture_data: dict[str, Any] | None = None,
    session_name: str = "",
    modules_ran: list[str] | None = None,
) -> Path:
    """Export a complete Atlas report as Markdown.

    Sections:
        1. Metadata & System Info
        2. Summary
        3. Non-Compliant Rules (failures first — most important)
        4. Extended Validation Checks
        5. Architecture Overview
        6. Errors / Skipped / Compliant Rules
    """
    meta = _build_metadata(results, session_name, modules_ran)
    summary = _build_summary(results)

    export_cols = [
        "rule_number", "name", "category", "severity",
        "status", "expected", "actual",
    ]

    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────
    lines.extend([
        "# Platform Atlas — Validation Report",
        "",
        f"_Generated: {meta['generated_at']} | Atlas v{meta['atlas_version']}_",
        "",
    ])

    # ── Metadata ──────────────────────────────────────────────
    lines.extend([
        "## System Info",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Organization | {meta['organization']} |",
        f"| Session | {meta['session']} |",
        f"| Host | {meta['hostname']} |",
        f"| Platform Version | {meta['platform_version']} |",
        f"| Ruleset | {meta['ruleset_id']} v{meta['ruleset_version']} |",
        f"| Profile | {meta['ruleset_profile'] or '—'} |",
        f"| Captured At | {meta['captured_at']} |",
        "",
    ])

    # ── Summary ───────────────────────────────────────────────
    lines.extend([
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Health Rating | **{summary['health_rating']}** |",
        f"| Pass Rate | {summary['pass_rate']}% |",
        f"| Total Rules | {summary['total_rules']} |",
        f"| Evaluated | {summary['evaluated']} |",
        f"| Compliant | {summary['compliant']} |",
        f"| Non-Compliant | {summary['non_compliant']} |",
        f"| Skipped | {summary['skipped']} |",
        f"| Errors | {summary['errors']} |",
        "",
    ])

    # ── Non-Compliant (first — most important) ────────────────
    fail_rows = [row for row in results.rows if row.get("status") == "FAIL"]
    if fail_rows:
        lines.extend([
            f"## Non-Compliant ({len(fail_rows)})",
            "",
            _markdown_table(fail_rows, export_cols),
            "",
        ])

    # ── Errors ────────────────────────────────────────────────
    error_rows = [row for row in results.rows if row.get("status") == "ERROR"]
    if error_rows:
        lines.extend([
            f"## Errors ({len(error_rows)})",
            "",
            _markdown_table(error_rows, export_cols),
            "",
        ])

    # ── Extended Validation ───────────────────────────────────
    ext = [
        c for c in (extended_results or [])
        if isinstance(c, dict) and c.get("check_id") not in _EXCLUDED_CHECK_IDS
    ]
    if ext:
        lines.extend([
            "## Extended Validation",
            "",
        ])
        for check in ext:
            if not isinstance(check, dict):
                continue
            status = check.get("status", "UNKNOWN")
            name = check.get("name", "Unnamed Check")
            message = check.get("message", "")
            remediation = check.get("remediation", "")
            category = check.get("category", "")

            icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "INFO": "ℹ️", "SKIP": "⏭️"}.get(status, "•")
            lines.append(f"### {icon} {name}")
            lines.append("")
            if category:
                lines.append(f"**Category:** {category}  ")
            lines.append(f"**Status:** {status}  ")
            if message:
                lines.append(f"**Finding:** {message}  ")
            if remediation:
                lines.append(f"**Remediation:** {remediation}  ")

            # Render details if present
            details = check.get("details", {})
            if details and isinstance(details, dict):
                items = details.get("items") or details.get("adapters") or details.get("issues")
                if isinstance(items, list) and items:
                    lines.append("")
                    for item in items[:20]:
                        if isinstance(item, dict):
                            parts = [f"{k}: {v}" for k, v in item.items() if v]
                            lines.append(f"- {', '.join(parts)}")
                        else:
                            lines.append(f"- {item}")
                    if len(items) > 20:
                        lines.append(f"- _...and {len(items) - 20} more_")

            lines.append("")

    # ── Architecture Overview ─────────────────────────────────
    arch = _clean_architecture(architecture_data or {})
    if arch:
        lines.extend([
            "## Architecture Overview",
            "",
        ])
        for section_key, section_data in arch.items():
            label = _ARCH_LABELS.get(section_key, section_key.replace("_", " ").title())
            lines.append(f"### {label}")
            lines.append("")
            lines.extend(_render_arch_md(section_data))
            lines.append("")

    # ── Skipped ───────────────────────────────────────────────
    skip_rows = [row for row in results.rows if row.get("status") == "SKIP"]
    if skip_rows:
        lines.extend([
            f"## Skipped ({len(skip_rows)})",
            "",
            _markdown_table(skip_rows, export_cols),
            "",
        ])

    # ── Compliant (last — least urgent) ───────────────────────
    pass_rows = [row for row in results.rows if row.get("status") == "PASS"]
    if pass_rows:
        lines.extend([
            f"## Compliant ({len(pass_rows)})",
            "",
            _markdown_table(pass_rows, export_cols),
            "",
        ])

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _render_arch_md(data: Any, depth: int = 0) -> list[str]:
    """Recursively render architecture data as Markdown key-value lines."""
    lines: list[str] = []
    indent = "  " * depth

    if isinstance(data, dict):
        for key, value in data.items():
            label = key.replace("_", " ").title()
            if isinstance(value, dict):
                lines.append(f"{indent}**{label}:**")
                lines.extend(_render_arch_md(value, depth + 1))
            elif isinstance(value, list):
                if not value:
                    lines.append(f"{indent}**{label}:** —")
                else:
                    items = ", ".join(f"`{v}`" for v in value)
                    lines.append(f"{indent}**{label}:** {items}")
            elif isinstance(value, bool):
                lines.append(f"{indent}**{label}:** {'Yes' if value else 'No'}")
            elif value is None or value == "":
                lines.append(f"{indent}**{label}:** —")
            else:
                lines.append(f"{indent}**{label}:** {value}")
    return lines
