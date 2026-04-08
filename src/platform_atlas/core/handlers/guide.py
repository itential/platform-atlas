# pylint: disable=line-too-long
"""
Dispatch Handler ::: Config
"""

from __future__ import annotations

import platform
from argparse import Namespace

from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich.console import Console, Group
from rich.markdown import Markdown

from platform_atlas.core._version import __version__
from platform_atlas.core.paths import ATLAS_USER_GUIDE
from platform_atlas.core import ui
from platform_atlas.core.registry import registry

console = Console()
theme = ui.theme

@registry.register("guide", description="Show README guide to use in Rich Markdown viewer")
def handle_view_guide(args: Namespace) -> int:
    """README Markdown File Viewer"""

    style = platform.system() in ('Darwin', 'Windows')

    with open(ATLAS_USER_GUIDE, 'r', encoding='utf-8') as f:
        markdown_data = f.read()

    md = Markdown(markdown_data, code_theme="nord")
    max_width = min(console.width - 4, 88)

    help_bar = Panel(
        Text("Scroll ↑/↓  Page: PgUp/PgDn  Search: / (less)  Quit: q", style=theme.primary_glow),
        padding=(0, 2),
        width=max_width,
        border_style=theme.primary_dim,
        title="Guide"
    )
    md_panel = Panel(
            md,
            padding=(1, 2),
            width=max_width,
            border_style=theme.text_dim,
    )
    full_guide = Align.center(
        Group(help_bar, md_panel),
        vertical="top",
    )
    with console.pager(styles=style):
        console.print(full_guide)
    return 0
