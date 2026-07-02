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
import webbrowser
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

from platform_atlas.core._version import __version__
from platform_atlas.core.paths import ATLAS_HOME, PROJECT_TEMPLATES
from platform_atlas.core import ui

logger = logging.getLogger(__name__)

TEMPLATE_PATH   = PROJECT_TEMPLATES / "whats-new.html"
CACHED_PAGE     = ATLAS_HOME / "whats-new.html"
HASH_FILE       = ATLAS_HOME / ".whats_new_hash"
ASSETS_IMAGES   = PROJECT_TEMPLATES.parent / "images"

_CLI_BULLETS = [
    "[bold]ControlMaster hardened[/bold] — shorter socket paths, primary-only HA2 sockets, non-blocking pre-capture check, auto-open MFA prompt, and the new [bold]env sockets[/bold] command; no more hard blocks on jump hosts or CyberArk PSMP environments",
    "[bold]session trend[/bold] — compliance heat matrix: category pass rates across sessions over time, with --env / --all-envs / --limit",
    "[bold]horizon-atlas theme[/bold] — new default CLI theme for fresh installs; deep ocean dark background with bioluminescent blue-green primary",
    "[bold]Password-based SSH[/bold] — choose key or password per node in the wizard or env edit; password goes to the credential backend",
    "[bold]Environment drafts[/bold] — wizard saves after the name is confirmed; Ctrl-C no longer loses progress; env list marks incomplete envs ⚠",
    "[bold]GW4 + GW5 together[/bold] — pair a Gateway 4 API target with a Gateway 5 SSH/file node in one Extended or SaaS audit",
    "122 rules total — plus the Pandas validation pipeline is fully vectorized for faster session runs",
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

    # Remove any leftover versioned files from the old naming scheme
    for stale in ATLAS_HOME.glob("whats-new-v*.html"):
        try:
            stale.unlink()
            logger.debug("Removed stale What's New file: %s", stale)
        except OSError:
            pass

    try:
        CACHED_PAGE.write_text(html, encoding="utf-8")
        page_path = CACHED_PAGE
    except OSError:
        tmp = tempfile.NamedTemporaryFile(suffix=".html", prefix="atlas-whats-new-", delete=False)
        tmp.write(html.encode("utf-8"))
        tmp.close()
        page_path = Path(tmp.name)

    try:
        webbrowser.open(page_path.as_uri())
    except Exception as e:
        logger.debug("Could not open browser for What's New page: %s", e)


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
