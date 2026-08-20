"""
Shared UI instances for Platform Atlas
"""

from __future__ import annotations

import logging
import os
import sys

from rich import box
from rich.console import Console
from rich.panel import Panel

console = Console()
logger = logging.getLogger(__name__)

class _ThemeProxy:
    """Module-level theme that delegates to the active context"""
    _resolved = None

    def __getattr__(self, name: str):
        if self._resolved is not None:
            return getattr(self._resolved, name)

        # Fallback before context is initialized
        from platform_atlas.core.theme import ATLAS_HORIZON_DARK
        return getattr(ATLAS_HORIZON_DARK, name)

    def __repr__(self) -> str:
        if self._resolved is not None:
            return f"<ThemeProxy resolved={type(self._resolved).__name__}>"
        return "<ThemeProxy unresolved>"

theme = _ThemeProxy()


def is_plain_mode() -> bool:
    """Return True when plain/compatibility mode is active."""
    try:
        from platform_atlas.core.context import ctx as _ctx
        return _ctx().config.compatibility_mode
    except Exception:
        return bool(os.environ.get("NO_COLOR"))


def _is_headless_environment() -> bool:
    """Best-effort heuristic: no display available to open a browser on.

    Only Linux is checked — a bare SSH session onto a server is the case
    this exists for. macOS/Windows are always treated as having a display.
    """
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def should_open_browser() -> bool:
    """Resolve the effective ``browser_mode`` setting to a yes/no decision.

    "always"/"never" force the outcome; "auto" (default) falls back to the
    headless heuristic. Fails open to "auto" if config isn't loaded yet.
    """
    try:
        from platform_atlas.core.context import ctx as _ctx
        mode = _ctx().config.browser_mode
    except Exception:
        mode = "auto"

    if mode == "always":
        return True
    if mode == "never":
        return False
    return not _is_headless_environment()


def maybe_open_html(target: str) -> bool:
    """Open ``target`` (a ``file://`` URI or URL) in a browser, honoring browser_mode.

    Returns True if a browser was launched, False if skipped (server/never)
    or if ``webbrowser.open`` raised — callers should tell the user the file
    path to open manually in the False case.
    """
    if not should_open_browser():
        return False

    import webbrowser
    try:
        webbrowser.open(target)
        return True
    except Exception as e:
        logger.debug("Could not open browser for %s: %s", target, e)
        return False


# ── Canonical status glyphs ──────────────────────────────────────
# One icon vocabulary for the whole CLI so "success", "failure",
# "warning" and "skipped" look identical wherever they appear. Plain /
# compatibility mode swaps to ASCII so output stays legible on terminals
# that cannot render the Unicode marks.
_GLYPHS = {
    "success": "✓",
    "error": "✗",
    "warning": "⚠",
    "skip": "⊘",
    "info": "›",
    "arrow": "→",
    "bullet": "•",
    "active": "●",
    "inactive": "·",
    # Progress + flow-node marks — capture module rows and the dashboard
    # Capture ▶ Validate ▶ Report pipeline. Owned here so the whole CLI draws
    # "pending / running / done" the same way (and gets ASCII fallbacks).
    "pending": "○",
    "running": "●",
    "node_done": "◉",
    "node_current": "◉",
    "node_pending": "◯",
}
_GLYPHS_PLAIN = {
    "success": "OK",
    "error": "X",
    "warning": "!",
    "skip": "-",
    "info": ">",
    "arrow": "->",
    "bullet": "*",
    "active": "*",
    "inactive": ".",
    "pending": "o",
    "running": "*",
    "node_done": "*",
    "node_current": ">",
    "node_pending": "o",
}


def glyph(name: str) -> str:
    """Return the canonical status glyph for ``name`` (ASCII in plain mode)."""
    table = _GLYPHS_PLAIN if is_plain_mode() else _GLYPHS
    return table.get(name, _GLYPHS.get(name, ""))


# ── Status message helpers ───────────────────────────────────────
# The single owner of the glyph + semantic color + indent for the
# ubiquitous "  ✓ message" status line. Callers pass Rich markup in the
# message; the helper supplies the colored glyph and leading indent, so
# the success/error/warning/skip vocabulary can never drift again.

def _status_line(
    kind: str,
    color: str,
    message: str,
    *,
    indent: int = 2,
    con: Console | None = None,
) -> None:
    (con or console).print(f"{' ' * indent}[{color}]{glyph(kind)}[/{color}] {message}")


def print_success(message: str, *, indent: int = 2, con: Console | None = None) -> None:
    """Print a ``✓`` line in the success color."""
    _status_line("success", theme.success, message, indent=indent, con=con)


def print_error(message: str, *, indent: int = 2, con: Console | None = None) -> None:
    """Print a ``✗`` line in the error color."""
    _status_line("error", theme.error, message, indent=indent, con=con)


def print_warning(message: str, *, indent: int = 2, con: Console | None = None) -> None:
    """Print a ``⚠`` line in the warning color."""
    _status_line("warning", theme.warning, message, indent=indent, con=con)


def print_skip(message: str, *, indent: int = 2, con: Console | None = None) -> None:
    """Print a ``⊘`` line in the dim text color."""
    _status_line("skip", theme.text_dim, message, indent=indent, con=con)


def print_info(message: str, *, indent: int = 2, con: Console | None = None) -> None:
    """Print a ``›`` info/step line in the primary color."""
    _status_line("info", theme.primary, message, indent=indent, con=con)


# ── Reusable styled panels ───────────────────────────────────────

def next_step(command: str, label: str = "Next Step") -> None:
    """Display a tinted panel prompting the user with the next command to run.

    Args:
        command: The CLI command string (e.g. "session run validate").
        label: Panel title — defaults to "Next Step".
    """
    body = (
        f"  [{theme.accent}]→[/{theme.accent}] "
        f"[bold {theme.primary}]{command}[/bold {theme.primary}]"
    )
    console.print(Panel(
        body,
        title=f"[bold {theme.accent}]{label}[/bold {theme.accent}]",
        title_align="left",
        border_style=theme.accent,
        box=box.ROUNDED,
        style=f"on {theme.tint_accent}",
        padding=(0, 2),
        expand=True,
    ))


def hint_panel(
    message: str,
    *,
    title: str = "Hint",
    style: str | None = None,
) -> None:
    """Display a tinted hint/suggestion panel.

    Args:
        message: Rich-markup body text.
        title: Panel title.
        style: Override accent color (defaults to theme.info).
    """
    color = style or theme.info
    # Map color to tint
    _tint_map = {
        theme.primary: theme.tint_primary,
        theme.secondary: theme.tint_secondary,
        theme.accent: theme.tint_accent,
        theme.success: theme.tint_success,
        theme.warning: theme.tint_warning,
        theme.error: theme.tint_error,
        theme.info: theme.tint_info,
    }
    tint = _tint_map.get(color, theme.tint_neutral)

    console.print(Panel(
        f"  {message}",
        title=f"[bold {color}]{title}[/bold {color}]",
        title_align="left",
        border_style=color,
        box=box.ROUNDED,
        style=f"on {tint}",
        padding=(0, 2),
        expand=True,
    ))
