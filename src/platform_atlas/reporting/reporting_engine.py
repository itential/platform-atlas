# pylint: disable=line-too-long
"""
ATLAS // Reporting Engine

Handles all non-HTML report generation: JSON, Markdown, and CSV exports.
JSON and Markdown formats include the full report data: metadata, validation
results, extended validation checks, and architecture overview.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Any

import pandas as pd

from platform_atlas.core.context import ctx
from platform_atlas.reporting.report_renderer import render_html_report, calculate_stats
from platform_atlas.core.paths import (
    REPORT_TEMPLATE,
)
from platform_atlas.core._version import __version__

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
    "network_security": "Network & Security",
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


class ExportFormat(Enum):
    """Supported non-HTML export formats"""
    CSV = "csv"
    JSON = "json"
    MD = "md"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shared helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_metadata(df: pd.DataFrame, session_name: str = "", modules_ran: list[str] | None = None) -> dict[str, Any]:
    """Build the metadata block shared by JSON and Markdown exports."""
    return {
        "atlas_version": __version__,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session": session_name,
        "organization": df.attrs.get("organization_name", "Unknown"),
        "environment": df.attrs.get("environment", ""),
        "hostname": df.attrs.get("hostname", "Unknown"),
        "platform_version": df.attrs.get("platform_ver", "Unknown"),
        "ruleset_id": df.attrs.get("ruleset_id", "Unknown"),
        "ruleset_version": df.attrs.get("ruleset_version", "Unknown"),
        "ruleset_profile": df.attrs.get("ruleset_profile", ""),
        "captured_at": df.attrs.get("captured_at", "Unknown"),
        "modules_ran": modules_ran or df.attrs.get("modules_ran", []),
    }


def _build_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Build the summary statistics block.

    Pass rate excludes skipped rules (matches HTML report calculation).
    """
    total = len(df)
    passed = int((df["status"] == "PASS").sum())
    failed = int((df["status"] == "FAIL").sum())
    skipped = int((df["status"].isin(["SKIP", "SKIPPED", "N/A", "NA"])).sum())
    errored = int((df["status"] == "ERROR").sum())

    # Exclude skipped from denominator (matches calculate_stats in report_renderer)
    evaluated = passed + failed + errored
    pass_rate = round((passed / evaluated * 100), 1) if evaluated > 0 else 0.0

    # Rating thresholds match HTML report exactly
    if pass_rate >= 95:
        rating = "Excellent"
    elif pass_rate >= 85:
        rating = "Good"
    elif pass_rate >= 70:
        rating = "Needs Attention"
    elif pass_rate >= 50:
        rating = "Poor"
    else:
        rating = "Critical"

    return {
        "total_rules": total,
        "evaluated": evaluated,
        "compliant": passed,
        "non_compliant": failed,
        "skipped": skipped,
        "errors": errored,
        "pass_rate": pass_rate,
        "health_rating": rating,
    }


def _clean_architecture(arch_data: dict[str, Any]) -> dict[str, Any]:
    """Clean architecture data — remove empty/skipped sections and excluded keys."""
    if not arch_data:
        return {}

    cleaned = {}
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

    return cleaned


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JSON Export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def export_json_report(
    df: pd.DataFrame,
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
    available_cols = [c for c in export_cols if c in df.columns]
    df_export = df[available_cols].copy()

    # Group validation results by category for structured output
    validation_by_category: dict[str, list[dict]] = {}
    for _, row in df_export.iterrows():
        cat = row.get("category", "other")
        rule_dict = {k: _json_safe(v) for k, v in row.to_dict().items()}
        validation_by_category.setdefault(cat, []).append(rule_dict)

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
            })

    # Assemble the full report
    report = {
        "report": {
            "metadata": _build_metadata(df, session_name, modules_ran),
            "summary": _build_summary(df),
            "validation": validation_by_category,
            "extended_checks": extended,
            "architecture": _clean_architecture(architecture_data or {}),
        }
    }

    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return output_path


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

def export_markdown_report(
    df: pd.DataFrame,
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
    meta = _build_metadata(df, session_name, modules_ran)
    summary = _build_summary(df)

    export_cols = [
        "rule_number", "name", "category", "severity",
        "status", "expected", "actual",
    ]
    available_cols = [c for c in export_cols if c in df.columns]
    df_export = df[available_cols].copy()

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
    fail_df = df_export[df_export["status"] == "FAIL"]
    if not fail_df.empty:
        lines.extend([
            f"## Non-Compliant ({len(fail_df)})",
            "",
            fail_df.to_markdown(index=False),
            "",
        ])

    # ── Errors ────────────────────────────────────────────────
    error_df = df_export[df_export["status"] == "ERROR"]
    if not error_df.empty:
        lines.extend([
            f"## Errors ({len(error_df)})",
            "",
            error_df.to_markdown(index=False),
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
    skip_df = df_export[df_export["status"] == "SKIP"]
    if not skip_df.empty:
        lines.extend([
            f"## Skipped ({len(skip_df)})",
            "",
            skip_df.to_markdown(index=False),
            "",
        ])

    # ── Compliant (last — least urgent) ───────────────────────
    pass_df = df_export[df_export["status"] == "PASS"]
    if not pass_df.empty:
        lines.extend([
            f"## Compliant ({len(pass_df)})",
            "",
            pass_df.to_markdown(index=False),
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Legacy / HTML helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def report(
    df: pd.DataFrame,
    report_name: str,
    output_path: str,
) -> None:
    """Generates HTML report.

    Args:
        df: Validation results DataFrame.
        report_name: Title for the report.
        output_path: Destination file path.
    """

    # Set Report Template Path
    active_template = REPORT_TEMPLATE

    # Calculate stats from DataFrame
    calculate_stats(df, status_column="status")

    # Metadata
    hostname = df.attrs.get('hostname', 'Unknown')
    platform_ver = df.attrs.get('platform_ver', 'Unknown')
    ruleset_id = df.attrs.get('ruleset_id', '')
    ruleset_ver = df.attrs.get('ruleset_version', '')
    ruleset_profile = df.attrs.get('ruleset_profile', '')
    modules_ran = df.attrs.get('modules_ran', [])

    extended_results = df.attrs.get('extended_results', [])

    df = df.sort_values(by='rule_number')

    render_html_report(
        df,
        active_template,
        output_path=output_path,
        title=report_name,
        subtitle=f"Configuration validation for {hostname}",
        system_info=[
            f"Host: {hostname}",
            f"Platform Version: {platform_ver}",
            f"Ruleset: {ruleset_id} v{ruleset_ver}",
        ],
        ruleset_version=f"{ruleset_ver} ({ruleset_profile})" if ruleset_profile else f"{ruleset_ver}",
        target_system=f"{hostname}",
        modules_ran=modules_ran,
        extended_results=extended_results,
    )


def export_report(
        parquet_path: Path,
        output_path: Path,
        fmt: ExportFormat,
        *,
        orient: str = "records",
) -> Path:
    """Export a parquet report to CSV, JSON, or Markdown."""
    df = pd.read_parquet(parquet_path)

    # Enforce correct extension
    output_path = output_path.with_suffix(f".{fmt.value}")

    match fmt:
        case ExportFormat.CSV:
            df.to_csv(output_path, index=False)
        case ExportFormat.JSON:
            export_json_report(df, output_path)
        case ExportFormat.MD:
            export_markdown_report(df, output_path)

    return output_path
