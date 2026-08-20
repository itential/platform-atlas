"""
ATLAS // What's New

Shows a "What's New" notice after any upgrade that ships a changed page:
    1. Prints a brief CLI summary with bullet points (always visible)
    2. Opens the self-contained HTML page in the default browser

A single universal template lives at:
    reporting/assets/templates/whats-new.html

It is updated in-place each release — no versioned copies accumulate.
Staleness is detected by SHA-256 hashing the bundled template and comparing
it against ~/.atlas/.whats_new_hash. Any change to the template (content,
fixes, typos) will re-trigger the notice for all users on next run.

Force-show at any time with: platform-atlas --whats-new
"""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

from platform_atlas.core._version import __version__
from platform_atlas.core.paths import ATLAS_HOME, ATLAS_HOME_GUIDES, PROJECT_TEMPLATES
from platform_atlas.core import ui

logger = logging.getLogger(__name__)

TEMPLATE_PATH   = PROJECT_TEMPLATES / "whats-new.html"
CACHED_PAGE     = ATLAS_HOME_GUIDES / "whats-new.html"
HASH_FILE       = ATLAS_HOME / ".whats_new_hash"
ASSETS_IMAGES   = PROJECT_TEMPLATES.parent / "images"

_CLI_BULLETS = [
    "[bold]Every page Atlas generates now shares one design[/bold] — report.html, session diff, the export splash page, and all three browser wizards moved to one warm, paper-toned look",
    "[bold]session run report now builds one report.html[/bold] — Compliance, Operational, and Architecture as pages in a single file; the old three-file structure and `--unified` flag are gone",
    "[bold]MongoDB/Redis through a jumphost[/bold] — Extended-tier deployments that can't connect directly can now tunnel through a bastion host, with a live connectivity test before saving",
    "[bold]Kubernetes multi-namespace capture[/bold] — an opt-in setting captures and validates a second Platform or Gateway5 deployment in its own namespace or cluster",
    "[bold]Turn off individual validation checks[/bold] — `config edit` lists every extended check with a checkbox; a disabled check shows \"Module Deactivated\" instead of failing",
    "[bold]preflight, now one tree[/bold] — the stacked per-phase tables are replaced by one grouped tree with per-branch tallies and a single progress spinner",
    "Basic TLS toggle for MongoDB/Redis, organization name set once in `config edit`, and `config doctor` now reports `~/.atlas` disk usage",
]


# ── Hash helpers ──────────────────────────────────────────────────

def _get_template_hash() -> str | None:
    """SHA-256 of the bundled whats-new.html template, or None if absent."""
    if not TEMPLATE_PATH.is_file():
        return None
    return hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest()


def _get_stored_hash() -> str | None:
    if not HASH_FILE.is_file():
        return None
    try:
        return HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _store_hash(h: str) -> None:
    try:
        HASH_FILE.write_text(h, encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write whats-new hash file: %s", e)


def _should_show() -> bool:
    if not ATLAS_HOME.is_dir():
        return False
    template_hash = _get_template_hash()
    if template_hash is None:
        return False
    return _get_stored_hash() != template_hash


# ── Asset helpers ─────────────────────────────────────────────────

def _load_image_data_uri(filename: str) -> str:
    path = ASSETS_IMAGES / filename
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
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_html() -> str | None:
    if not TEMPLATE_PATH.is_file():
        logger.debug("What's New template not found: %s", TEMPLATE_PATH)
        return None
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("{{ITENTIAL_LOGO}}", _load_image_data_uri("itential-logo-dark.svg"))
    html = html.replace("{{ITENTIAL_ICON}}", _load_image_data_uri("itential-icon.png"))
    from platform_atlas.reporting.assets.fonts import get_font_css
    html = html.replace("{{EMBEDDED_FONTS}}", get_font_css())
    return html


# ── CLI summary ───────────────────────────────────────────────────

def _show_cli_summary() -> None:
    theme = ui.theme
    console = Console()

    series = ".".join(__version__.split(".")[:2])
    lines = [
        f"  [{theme.text_dim}]•[/{theme.text_dim}]  {item}"
        for item in _CLI_BULLETS
    ]

    import os as _os
    prefix = "" if _os.environ.get("NO_COLOR") else "🎉 "

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold {theme.primary}]{prefix}What's New in v{series}[/bold {theme.primary}]",
        title_align="left",
        border_style=theme.primary,
        box=box.ROUNDED,
        style=f"on {theme.tint_primary}",
        padding=(1, 2),
        expand=True,
    ))

    console.print(
        f"  [{theme.text_dim}]View the full update page any time with"
        f" [bold {theme.primary}]--whats-new[/bold {theme.primary}][/{theme.text_dim}]"
    )
    console.print()


def _wait_and_clear() -> None:
    theme = ui.theme
    console = Console()
    console.print(f"  [{theme.text_ghost}]Press Enter to continue...[/{theme.text_ghost}]")
    try:
        console.input("")
    except (EOFError, KeyboardInterrupt):
        pass
    console.clear()


# ── HTML page ─────────────────────────────────────────────────────

def _open_html_page() -> None:
    html = _build_html()
    if html is None:
        return

    # Remove any leftover versioned files from the old naming scheme, plus the
    # pre-guides-folder copy that used to live directly under ~/.atlas.
    for stale in [*ATLAS_HOME.glob("whats-new-v*.html"), ATLAS_HOME / "whats-new.html"]:
        try:
            stale.unlink()
            logger.debug("Removed stale What's New file: %s", stale)
        except OSError:
            pass

    try:
        ATLAS_HOME_GUIDES.mkdir(mode=0o700, parents=True, exist_ok=True)
        CACHED_PAGE.write_text(html, encoding="utf-8")
        page_path = CACHED_PAGE
    except OSError:
        tmp = tempfile.NamedTemporaryFile(suffix=".html", prefix="atlas-whats-new-", delete=False)
        tmp.write(html.encode("utf-8"))
        tmp.close()
        page_path = Path(tmp.name)

    if not ui.maybe_open_html(page_path.as_uri()):
        ui.print_skip(f"Server environment detected — open manually: {page_path}")


# ── Public API ────────────────────────────────────────────────────

def maybe_show_whats_new(*, force: bool = False) -> None:
    """
    Show the what's-new notice if appropriate.

    Auto mode: fires when the bundled template's SHA-256 differs from the
    stored hash — i.e., a new or updated page shipped with this version.
    Force mode (--whats-new): always shows regardless of hash state.

    The notice is shown at most once per template version (hash stored
    after display). A fresh install marks the hash without showing anything
    so only upgrades trigger the auto notice.
    """
    template_hash = _get_template_hash()
    if template_hash is None:
        return

    if force or _should_show():
        _show_cli_summary()
        _open_html_page()
        _store_hash(template_hash)
        _wait_and_clear()


def mark_seen_fresh_install() -> None:
    """
    Record the current template hash on a fresh install so the what's-new
    screen doesn't fire — it's for upgrades only.
    """
    h = _get_template_hash()
    if h:
        _store_hash(h)
