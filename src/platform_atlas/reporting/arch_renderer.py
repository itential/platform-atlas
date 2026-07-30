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
from platform_atlas.reporting.arch_warnings import ArchWarning, compute_arch_warnings
from platform_atlas.core._version import __version__
from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css

logger = logging.getLogger(__name__)

# ── Brand colors used for severity badges (hardcoded hex — no CSS vars) ─── #
_SEVERITY_COLORS = {
    "critical": "#FF6633",  # Itential orange
    "warning":  "#1B93D2",  # Itential blue
    "info":     "#99CA3C",  # Itential green
}

# Text that contrasts on those backgrounds
_SEVERITY_TEXT = {
    "critical": "#fff",
    "warning":  "#fff",
    "info":     "#101625",
}

_CATEGORY_LABELS = {
    "latency":      "LATENCY",
    "availability": "AVAILABILITY",
    "security":     "SECURITY",
}


def _render_arch_warnings_html(warnings: list[ArchWarning]) -> str:
    """Return an HTML block listing all arch warnings, or '' when the list is empty."""
    if not warnings:
        return ""

    cards = []
    for w in warnings:
        badge_bg   = _SEVERITY_COLORS.get(w.severity, "#1B93D2")
        badge_text = _SEVERITY_TEXT.get(w.severity, "#fff")
        cat_label  = _CATEGORY_LABELS.get(w.category, w.category.upper())
        detail_html = (
            f'<span class="aw-detail">{html_mod.escape(w.detail)}</span>'
            if w.detail else ""
        )
        cards.append(
            f'<div class="arch-warning-card {html_mod.escape(w.severity)}">'
            f'  <span class="aw-badge" style="background:{badge_bg};color:{badge_text};">'
            f'    {html_mod.escape(cat_label)}'
            f'  </span>'
            f'  <span class="aw-component">{html_mod.escape(w.component)}</span>'
            f'  <span class="aw-message">{html_mod.escape(w.message)}</span>'
            f'  {detail_html}'
            f'</div>'
        )

    cards_html = "\n".join(cards)
    return (
        '<div class="arch-warnings">'
        '<style>'
        '.arch-warnings{margin:1rem 0 1.25rem;}'
        '.arch-warnings h3{font-size:13px;font-weight:700;text-transform:uppercase;'
        '  letter-spacing:.07em;margin:0 0 .6rem;color:var(--text-secondary,#a8bdd0);}'
        '.arch-warning-card{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 8px;'
        '  padding:8px 12px;margin-bottom:6px;border-radius:6px;font-size:12px;'
        '  background:rgba(27,147,210,.06);border:1px solid rgba(27,147,210,.15);}'
        '.arch-warning-card.critical{background:rgba(255,102,51,.07);border-color:rgba(255,102,51,.25);}'
        '.arch-warning-card.warning{background:rgba(27,147,210,.06);border-color:rgba(27,147,210,.20);}'
        '.arch-warning-card.info{background:rgba(153,202,60,.06);border-color:rgba(153,202,60,.20);}'
        '.aw-badge{display:inline-block;font-size:9px;font-weight:800;letter-spacing:.08em;'
        '  padding:2px 6px;border-radius:3px;flex-shrink:0;}'
        '.aw-component{font-weight:700;color:var(--text-primary,#f0f4f8);flex-shrink:0;}'
        '.aw-message{color:var(--text-secondary,#a8bdd0);flex:1;min-width:0;}'
        '.aw-detail{font-size:11px;color:var(--text-muted,#607590);width:100%;padding-left:2px;}'
        '</style>'
        '<h3>Architecture Warnings</h3>'
        + cards_html
        + '</div>'
    )


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
    tier: str = "extended",
) -> str:
    """Render the Architecture & Maintenance report to a styled HTML file.

    In Standard mode the architecture section collapses to a tier notice
    — the architecture form is Extended-only, so there is nothing to render.
    """
    template_path = Path(template_path)
    template = template_path.read_text(encoding="utf-8")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    tier_normalized = (tier or "extended").strip().lower()
    is_standard = tier_normalized == "standard"

    if is_standard:
        tier_label = "STANDARD"
        tier_color = "#1B93D2"
        extended_html = (
            '<div class="tier-notice">'
            '<p><strong>Architecture &amp; maintenance audit is part of '
            'Extended Mode.</strong></p>'
            '<p>Standard Mode validates Platform configuration via OAuth only. '
            'Run <code>platform-atlas tier upgrade</code> to enable the full '
            'architecture review.</p>'
            '</div>'
        )
        architecture_html = ""
        arch_warnings_html = ""
        arch_warning_count = "0"
    else:
        tier_label = "EXTENDED"
        tier_color = "#FF6633"
        extended_html = generate_nonlog_extended_html(extended_results or [])
        architecture_html = _render_architecture_section(architecture_data or {})
        arch_warnings = compute_arch_warnings(architecture_data or {})
        arch_warnings_html = _render_arch_warnings_html(arch_warnings)
        arch_warning_count = str(len(arch_warnings))

    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    safe_org = html_mod.escape(organization_name)
    safe_version = html_mod.escape(atlas_version)
    safe_timestamp = html_mod.escape(timestamp)
    safe_tier_label = html_mod.escape(tier_label)
    safe_tier_color = html_mod.escape(tier_color)

    replacements = {
        "{{TITLE}}": safe_title,
        "{{SUBTITLE}}": safe_subtitle,
        "{{ORGANIZATION_NAME}}": safe_org,
        "{{TIMESTAMP}}": safe_timestamp,
        "{{ATLAS_VERSION}}": safe_version,
        "{{EXTENDED_SECTION}}": extended_html,
        "{{ARCH_WARNINGS}}": arch_warnings_html,
        "{{ARCH_WARNING_COUNT}}": arch_warning_count,
        "{{ARCHITECTURE_SECTION}}": architecture_html,
        "{{TIER_LABEL}}": safe_tier_label,
        "{{TIER_COLOR}}": safe_tier_color,
        "{{EMBEDDED_FONTS}}": _get_font_css(),
    }

    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], template)

    if output_path:
        output_path = Path(output_path)
        output_path.write_text(html, encoding="utf-8")
        if os.name == "posix":
            os.chmod(output_path, 0o600)

    return html
