"""Renderer for the opt-in unified report (``--unified``).

The unified report combines the Compliance, Operational, and Architecture
reports into a single standalone HTML file whose three pages are switched from
the persistent top bar. Unlike the classic renderers, almost all rendering
happens *client-side*: the page is driven entirely by the viewmodel JSON
embedded in ``#atlas-viewmodel``.

This module therefore does only two things:

1. Serialize the viewmodel (the same dict ``build_webui_viewmodel`` produces,
   which also powers the WebUI's unified report — so the numbers stay in
   lockstep with the classic reports and the WebUI).
2. Inject it into the template's ``{{ATLAS_VIEWMODEL_JSON}}`` placeholder.

Substitution mirrors :func:`report_renderer.render_html_report`'s
``{{PLACEHOLDER}}`` convention, but uses literal ``str.replace`` rather than a
regex so the (potentially large) JSON payload is never interpreted as a
replacement pattern.
"""

from __future__ import annotations

import html as html_mod
import json
import os
from pathlib import Path
from typing import Any

from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css


def _report_title(viewmodel: dict[str, Any]) -> str:
    """Derive the document ``<title>`` from the viewmodel session block."""
    session = viewmodel.get("session") or {}
    org = str(session.get("organization_name") or "Platform Atlas")
    tier = str(session.get("tier") or "extended").lower()
    kind = "Gateway Health Report" if tier == "saas" else "Platform Health Report"
    return f"{kind} — {org}"


def render_unified_report(
        viewmodel: dict[str, Any],
        template_path: str | Path,
        output_path: str | Path,
        *,
        title: str | None = None,
) -> str:
    """Render the unified single-file report.

    Args:
        viewmodel: The merged report viewmodel (output of
            ``webui_viewmodel.build_webui_viewmodel``) with ``session``,
            ``compliance``, ``operational``, and ``architecture`` blocks.
        template_path: Path to ``report_unified.html``.
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
    payload = json.dumps(viewmodel, ensure_ascii=False).replace("</", "<\\/")

    doc_title = title if title is not None else _report_title(viewmodel)

    html = template.replace("{{TITLE}}", html_mod.escape(str(doc_title)))
    html = html.replace("{{ATLAS_VIEWMODEL_JSON}}", payload)
    html = html.replace("{{EMBEDDED_FONTS}}", _get_font_css())

    output_path = Path(output_path)
    output_path.write_text(html, encoding="utf-8")
    if os.name == "posix":
        os.chmod(output_path, 0o600)

    return html
