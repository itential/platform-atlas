"""
ATLAS // What's New

Shows a one-time "What's New" notice after an upgrade:
    1. Prints a brief CLI summary with bullet points (always visible)
    2. Opens a detailed HTML page in the default browser (if available)

The HTML template lives in reporting/assets/templates/whats-new-{version}.html.
Brand assets (logos, images) live in reporting/assets/images/ and are
base64-encoded into the HTML at runtime so the output is fully self-contained.

Tracking:
    ~/.atlas/.seen_version stores the last version whose update was shown.
"""

from __future__ import annotations

import base64
import logging
import tempfile
import webbrowser
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

from platform_atlas.core._version import __version__
from platform_atlas.core.paths import ATLAS_HOME, PROJECT_TEMPLATES
from platform_atlas.core import ui

logger = logging.getLogger(__name__)

SEEN_VERSION_FILE = ATLAS_HOME / ".seen_version"
ASSETS_IMAGES_DIR = PROJECT_TEMPLATES.parent / "images"

# Versions that have a What's New page
WHATS_NEW_VERSIONS = {"1.5"}


# ── Version comparison ────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def _get_seen_version() -> str | None:
    if not SEEN_VERSION_FILE.is_file():
        return None
    try:
        return SEEN_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _mark_seen(version: str) -> None:
    try:
        SEEN_VERSION_FILE.write_text(version, encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write seen version file: %s", e)


def _should_show() -> bool:
    if not ATLAS_HOME.is_dir():
        return False
    if __version__ not in WHATS_NEW_VERSIONS:
        return False
    seen = _get_seen_version()
    if seen is None:
        return True
    return _parse_version(__version__) > _parse_version(seen)


# ── Asset helpers ─────────────────────────────────────────────────

def _load_image_data_uri(filename: str) -> str:
    """
    Read an image from the assets/images directory and return it as a
    base64 data URI suitable for an <img> src attribute.

    Returns an empty string if the file doesn't exist (the HTML template
    should handle this gracefully with CSS fallbacks).
    """
    path = ASSETS_IMAGES_DIR / filename
    if not path.is_file():
        logger.debug("Asset image not found: %s", path)
        return ""

    suffix = path.suffix.lower()
    mime_map = {
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    mime = mime_map.get(suffix, "application/octet-stream")

    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_html(version: str) -> str | None:
    """
    Read the HTML template for a version and inject any asset placeholders.

    Supported placeholders:
        {{ITENTIAL_LOGO}}  — base64 data URI for itential-logo-dark.svg
    """
    template_path = PROJECT_TEMPLATES / f"whats-new-{version}.html"
    if not template_path.is_file():
        logger.debug("What's New template not found: %s", template_path)
        return None

    html = template_path.read_text(encoding="utf-8")

    # Inject brand assets as data URIs
    logo_uri = _load_image_data_uri("itential-logo-dark.svg")
    html = html.replace("{{ITENTIAL_LOGO}}", logo_uri)

    return html


# ── CLI Summary (bullet points in terminal) ──────────────────────

_CLI_BULLETS: dict[str, list[str]] = {
    "1.5": [
        "Sessions now bind an environment, ruleset, and profile together",
        "Switching sessions restores the full context automatically",
        "Each environment carries its own organization name",
        "Redesigned dashboard shows your active session at a glance",
        "Gateway4 direct API collection (SSH is now a fallback)",
        "JSON/Markdown exports include environment in metadata",
        "Knowledge Base remediation steps shown by default in reports",
        "Run [bold]session repair[/bold] to backfill pre-1.5 session metadata",
    ],
}


def _show_cli_summary(version: str) -> None:
    """Print a concise CLI summary with bullet points."""
    theme = ui.theme
    console = Console()

    bullets = _CLI_BULLETS.get(version, [])
    if not bullets:
        return

    lines = []
    for item in bullets:
        lines.append(f"  [{theme.text_dim}]•[/{theme.text_dim}]  {item}")

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold {theme.primary}]🎉 What's New in v{version}[/bold {theme.primary}]",
        title_align="left",
        border_style=theme.primary,
        box=box.ROUNDED,
        style=f"on {theme.tint_primary}",
        padding=(1, 2),
        expand=True,
    ))

    console.print(
        f"  [{theme.text_dim}]View the full update page anytime with"
        f" [bold {theme.primary}]--whats-new[/bold {theme.primary}][/{theme.text_dim}]"
    )
    console.print()


def _wait_and_clear() -> None:
    """Prompt the user to press any key, then clear the screen for the dashboard."""
    theme = ui.theme
    console = Console()
    console.print(
        f"  [{theme.text_ghost}]Press Enter to continue...[/{theme.text_ghost}]"
    )
    try:
        console.input("")
    except (EOFError, KeyboardInterrupt):
        pass
    console.clear()


# ── HTML Page (opens in browser) ──────────────────────────────────

def _open_html_page(version: str) -> None:
    """Build the self-contained HTML page and open it in the browser."""
    html = _build_html(version)
    if html is None:
        return

    # Write to ~/.atlas so the browser can read it reliably
    page_path = ATLAS_HOME / f"whats-new-v{version}.html"
    try:
        page_path.write_text(html, encoding="utf-8")
    except OSError:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".html", prefix=f"atlas-whats-new-{version}-",
            delete=False,
        )
        tmp.write(html.encode("utf-8"))
        tmp.close()
        page_path = Path(tmp.name)

    try:
        webbrowser.open(f"file://{page_path.absolute()}")
    except Exception as e:
        logger.debug("Could not open browser for What's New page: %s", e)


# ── Public API ────────────────────────────────────────────────────

def maybe_show_whats_new(*, force: bool = False) -> None:
    """
    Show the what's-new notice if appropriate.

    Prints CLI bullet points to the terminal (always visible, even over SSH),
    then opens the detailed HTML page in the browser (if available).
    """
    version = __version__

    if force:
        if version in WHATS_NEW_VERSIONS:
            _show_cli_summary(version)
            _open_html_page(version)
            _mark_seen(version)
            _wait_and_clear()
        return

    if _should_show():
        _show_cli_summary(version)
        _open_html_page(version)
        _mark_seen(version)
        _wait_and_clear()
