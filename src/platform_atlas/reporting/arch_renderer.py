# pylint: disable=line-too-long
"""
ATLAS // Architecture & Maintenance Report Renderer

Renders the Architecture & Maintenance report (05_arch.html) containing:
  - Additional Validation (non-log extended checks)
  - Architecture Overview (manually collected deployment data)
"""

from __future__ import annotations

import os
import re
import html as html_mod
import logging
from pathlib import Path
from datetime import datetime, timezone

from platform_atlas.reporting.report_renderer import (
    generate_nonlog_extended_html,
    _render_architecture_section,
)
from platform_atlas.core._version import __version__

logger = logging.getLogger(__name__)


def render_arch_report(
    extended_results: list,
    architecture_data: dict,
    template_path: str | Path,
    output_path: str | Path,
    *,
    title: str = "Architecture & Maintenance",
    subtitle: str = "",
    organization_name: str = "Unknown Organization",
    atlas_version: str = __version__,
) -> str:
    """Render the Architecture & Maintenance report to a styled HTML file."""
    template_path = Path(template_path)
    template = template_path.read_text(encoding="utf-8")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    extended_html = generate_nonlog_extended_html(extended_results or [])
    architecture_html = _render_architecture_section(architecture_data or {})

    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    safe_org = html_mod.escape(organization_name)
    safe_version = html_mod.escape(atlas_version)
    safe_timestamp = html_mod.escape(timestamp)

    replacements = {
        "{{TITLE}}": safe_title,
        "{{SUBTITLE}}": safe_subtitle,
        "{{ORGANIZATION_NAME}}": safe_org,
        "{{TIMESTAMP}}": safe_timestamp,
        "{{ATLAS_VERSION}}": safe_version,
        "{{EXTENDED_SECTION}}": extended_html,
        "{{ARCHITECTURE_SECTION}}": architecture_html,
    }

    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], template)

    if output_path:
        output_path = Path(output_path)
        output_path.write_text(html, encoding="utf-8")
        os.chmod(output_path, 0o600)

    return html
