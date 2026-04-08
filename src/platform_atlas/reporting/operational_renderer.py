# pylint: disable=line-too-long
"""
ATLAS // Operational Report Renderer

Renders an OperationalReport to a styled HTML file using the operational
report templates.

Each PipelineResult becomes a collapsible section with:
  - Pipeline name + description header
  - Status pill (success / error)
  - Auto-generated data table (columns derived from result dict keys)
  - Row count + execution time footer
"""

from __future__ import annotations

import os
import re
import html as html_mod
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from platform_atlas.reporting.operational_engine import OperationalReport, PipelineResult
from platform_atlas.core._version import __version__

logger = logging.getLogger(__name__)

def _render_pipeline_table(result: PipelineResult) -> str:
    """Render an HTML data table from a pipeline result's row data."""
    if not result.rows:
        return '<p class="no-data">No data returned</p>'

    columns = result.columns
    parts: list[str] = []

    parts.append('<div class="table-scroll">')
    parts.append('<table class="pipeline-table">')

    # Header row
    parts.append("<thead><tr>")
    for col in columns:
        safe_col = html_mod.escape(str(col))
        parts.append(f"<th>{safe_col}</th>")
    parts.append("</tr></thead>")

    # Data rows
    parts.append("<tbody>")
    for row in result.rows:
        parts.append("<tr>")
        for col in columns:
            val = row.get(col, "")
            safe_val = html_mod.escape(str(val))
            parts.append(f"<td>{safe_val}</td>")
        parts.append("</tr>")
    parts.append("</tbody>")

    parts.append("</table>")
    parts.append("</div>")

    return "\n".join(parts)

def _render_pipeline_section(result: PipelineResult) -> str:
    """Render a single pipeline result as a collapsible section."""
    safe_name = html_mod.escape(result.name)
    safe_desc = html_mod.escape(result.description)
    status_class = "pass" if result.succeeded else "fail"
    status_label = "Success" if result.succeeded else "Error"

    parts: list[str] = []

    parts.append('<div class="collapsible-section">')

    # Header bar
    parts.append(f'''
    <div class="collapsible-header">
        <h3>
            <span>{safe_name}</span>
            <span class="count-badge">{result.row_count} rows</span>
        </h3>
        <div class="header-right">
            <span class="pill {status_class}">{status_label}</span>
            <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
        </div>
    </div>''')

    # Collapsible content
    parts.append('<div class="collapsible-content">')

    if safe_desc:
        parts.append(f'<p class="pipeline-desc">{safe_desc}</p>')

    if result.succeeded:
        parts.append(_render_pipeline_table(result))
    else:
        safe_error = html_mod.escape(result.error or "Unknown error")
        parts.append(f'''
        <div class="pipeline-error">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
            </svg>
            <span>{safe_error}</span>
        </div>''')

    # Footer with timing
    parts.append(f'''
    <div class="pipeline-footer">
        <span>Collection: <code>{html_mod.escape(result.collection)}</code></span>
        <span>{result.duration_ms:.0f} ms</span>
    </div>''')

    parts.append("</div>")  # collapsible-content
    parts.append("</div>")  # collapsible-section

    return "\n".join(parts)

def generate_pipeline_sections(report: OperationalReport) -> str:
    """Generate all pipeline sections HTML."""
    if not report.results:
        return '''
        <div class="no-pipelines">
            <p>No operational pipelines were found.</p>
            <p>Place pipeline JSON files in <code>~/.atlas/pipelines/</code> to get started.</p>
        </div>'''

    return "\n".join(_render_pipeline_section(r) for r in report.results)

def render_operational_report(
    report: OperationalReport,
    template_path: str | Path,
    output_path: str | Path,
    *,
    title: str = "Operational Metrics Report",
    subtitle: str = "",
    organization_name: str = "Unknown Organization",
    hostname: str = "Unknown",
    atlas_version: str = __version__,
) -> str:
    """Render an OperationalReport to a styled HTML file"""
    template_path = Path(template_path)
    template = template_path.read_text(encoding="utf-8")

    # Generate timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Generate pipeline sections
    pipeline_sections_html = generate_pipeline_sections(report)

    # Escape user-controlled values
    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    safe_organization_name = html_mod.escape(organization_name)
    safe_hostname = html_mod.escape(hostname)
    safe_atlas_version = html_mod.escape(atlas_version)
    safe_timestamp = html_mod.escape(timestamp)

    replacements = {
        "{{TITLE}}": safe_title,
        "{{SUBTITLE}}": safe_subtitle,
        "{{ORGANIZATION_NAME}}": safe_organization_name,
        "{{HOSTNAME}}": safe_hostname,
        "{{TIMESTAMP}}": safe_timestamp,
        "{{ATLAS_VERSION}}": safe_atlas_version,
        "{{PIPELINE_COUNT}}": str(report.pipeline_count),
        "{{SUCCESS_COUNT}}": str(report.success_count),
        "{{ERROR_COUNT}}": str(report.error_count),
        "{{TOTAL_ROWS}}": str(report.total_rows),
        "{{PIPELINE_SECTIONS}}": pipeline_sections_html,
    }

    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], template)

    # Write to file
    if output_path:
        output_path = Path(output_path)
        output_path.write_text(html, encoding="utf-8")
        os.chmod(output_path, 0o600)

    return html
