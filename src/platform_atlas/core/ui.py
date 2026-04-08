"""
Shared UI instances for Platform Atlas
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel

console = Console()

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
