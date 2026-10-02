"""Renderer for report.html.

report.html combines the Compliance, Operational, and Architecture reports
into a single standalone HTML file whose three pages are switched from the
persistent top bar. Almost all rendering happens *client-side*: the page is
driven entirely by the viewmodel JSON embedded in ``#atlas-viewmodel``.

This module therefore does only three things:

1. Serialize the viewmodel (the same dict ``build_webui_viewmodel`` produces,
   which also powers the WebUI's report view — so the numbers stay in
   lockstep with the WebUI).
2. Inject it into the template's ``{{ATLAS_VIEWMODEL_JSON}}`` placeholder.
3. Inject the embedded fonts and the vendored Motion animation library.

Uses literal ``str.replace`` rather than a regex so the (potentially large)
JSON payload is never interpreted as a replacement pattern.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from pathlib import Path
from typing import Any

from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css
from platform_atlas.reporting.assets.motion_lib import get_motion_js as _get_motion_js


def _report_title(viewmodel: dict[str, Any]) -> str:
    """Derive the document ``<title>`` from the viewmodel session block."""
    session = viewmodel.get("session") or {}
    org = str(session.get("organization_name") or "Platform Atlas")
    # SaaS is Platform-anchored now (limited OAuth pull), so every tier produces
    # a Platform Health Report rather than the old SaaS gateway-only title.
    return f"Platform Health Report — {org}"


def render_unified_report(
        viewmodel: dict[str, Any],
        template_path: str | Path,
        output_path: str | Path,
        *,
        title: str | None = None,
) -> str:
    """Render the single-file report.html.

    Args:
        viewmodel: The merged report viewmodel (output of
            ``webui_viewmodel.build_webui_viewmodel``) with ``session``,
            ``compliance``, ``operational``, and ``architecture`` blocks.
        template_path: Path to ``report.html`` (the ``REPORT_TEMPLATE``).
        output_path: Where to write the rendered report.
        title: Optional ``<title>`` override; derived from the viewmodel
            when omitted.

    Returns:
        The rendered HTML string (also written to ``output_path``).
    """
    template = Path(template_path).read_text(encoding="utf-8")

    # ``ensure_ascii=False`` per project convention (em dashes in rule
    # messages). The ``</`` → ``<\/`` rewrite prevents any string value
    # containing ``</script>`` from prematurely closing the data island; it is
    # invisible to ``JSON.parse`` because ``\/`` is a valid JSON escape for
    # ``/``. This is the standard "JSON in a <script> tag" hardening.
    # ``<!--`` becomes ``\u003c!--`` (a valid JSON escape) so ``<!--<script``
    # cannot flip the HTML parser into the script-data double-escaped state
    # and swallow the closing ``</script>``.
    payload = json.dumps(viewmodel, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "\\u003c!--")

    doc_title = title if title is not None else _report_title(viewmodel)

    # Single-pass substitution: replacement text (which may contain captured
    # data such as ``{{MOTION_JS}}``) is never re-scanned for placeholders.
    values = {
        "TITLE": html_mod.escape(str(doc_title)),
        "ATLAS_VIEWMODEL_JSON": payload,
        "EMBEDDED_FONTS": _get_font_css(),
        "MOTION_JS": _get_motion_js(),
    }
    html = re.sub(
        r"\{\{(TITLE|ATLAS_VIEWMODEL_JSON|EMBEDDED_FONTS|MOTION_JS)\}\}",
        lambda m: values[m.group(1)],
        template,
    )

    output_path = Path(output_path)
    output_path.write_text(html, encoding="utf-8")
    if os.name == "posix":
        os.chmod(output_path, 0o600)

    return html
