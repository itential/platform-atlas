# pylint: disable=line-too-long
"""
Platform Atlas Report Renderer

Shared helpers used by report generation: summary statistics for the CLI's
score panel, and the export-archive splash / cover page. The report itself
is rendered client-side (see ``reporting/unified_renderer.py``).
"""

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
import re
import html as html_mod
import pandas as pd

from platform_atlas.core._version import __version__
from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css

def calculate_stats(df: pd.DataFrame, status_column: str = "status") -> dict[str, Any]:
    """Calculate summary statistics from a validation results DataFrame"""
    total = len(df)

    status_upper = df[status_column].str.upper()

    pass_count = len(status_upper[status_upper == "PASS"])
    fail_count = len(status_upper[status_upper == "FAIL"])
    skip_count = len(status_upper[status_upper.isin(["SKIP", "SKIPPED", "N/A", "NA"])])
    error_count = len(status_upper[status_upper == "ERROR"])

    # Calculate pass percentage (excluding skipped)
    evaluated = pass_count + fail_count + error_count
    if evaluated > 0:
        pass_percent = round((pass_count / evaluated * 100), 1)
    else:
        pass_percent = 0.0

    # Determine Score Rating
    if pass_percent >= 95:
        rating = "Excellent"
    elif pass_percent >= 85:
        rating = "Good"
    elif pass_percent >= 70:
        rating = "Needs Attention"
    elif pass_percent >= 50:
        rating = "Poor"
    else:
        rating = "Critical"

    return {
        "total": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "error_count": error_count,
        "pass_percent": pass_percent,
        "rating": rating,
    }

def render_splash_page(
        template_path: str | Path,
        output_path: str | Path,
        *,
        organization_name: str = "Unknown Organization",
        session_name: str = "",
        tier: str = "extended",
        report_link: str = "session_files/report.html",
        atlas_version: str = __version__,
        timestamp: str | None = None,
) -> str:
    """Render the export splash / cover page (``REPORT.html``).

    This is the landing page placed at the top level of an exported session
    archive. The report itself lives in a ``session_files/`` subdirectory; the
    splash's single call-to-action links into ``report_link`` (defaults to
    ``session_files/report.html``).

    Substitution is a regex replace (not real Jinja2). The organization and
    session names are HTML-escaped because they originate from user input.
    """
    template = Path(template_path).read_text(encoding="utf-8")

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Human Mode label. The .meta block is text-transform:uppercase in CSS, so
    # casing here is cosmetic — these match the report cover's tier semantics.
    tier_normalized = (tier or "extended").strip().lower()
    tier_label = {"standard": "Standard", "saas": "SaaS"}.get(tier_normalized, "Extended")

    replacements = {
        "{{ORGANIZATION_NAME}}": html_mod.escape(str(organization_name or "Unknown Organization")),
        "{{SESSION_NAME}}": html_mod.escape(str(session_name or "—")),
        "{{TIER_LABEL}}": html_mod.escape(tier_label),
        # quote=True: the link lands in an href="" attribute.
        "{{REPORT_LINK}}": html_mod.escape(str(report_link), quote=True),
        "{{ATLAS_VERSION}}": html_mod.escape(str(atlas_version)),
        "{{TIMESTAMP}}": html_mod.escape(str(timestamp)),
        "{{EMBEDDED_FONTS}}": _get_font_css(),
    }

    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], template)

    output_path = Path(output_path)
    output_path.write_text(html, encoding="utf-8")
    if os.name == "posix":
        os.chmod(output_path, 0o600)

    return html
