# pylint: disable=line-too-long
"""
Dispatch Handler ::: Fleet

Subcommands:
    fleet status   — multi-environment compliance overview from local cache.

Read-only — never triggers a capture or network I/O.
"""

from __future__ import annotations

import json
import logging
from argparse import Namespace

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box

from platform_atlas.core import ui
from platform_atlas.core.fleet import collect_fleet
from platform_atlas.core.registry import registry

logger = logging.getLogger(__name__)
theme = ui.theme
console = Console()


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _score_color(pct: float) -> str:
    if pct >= 85:
        return theme.success
    if pct >= 75:
        return theme.warning
    return theme.error


def _heat_bar(pct: float | None, passed: int, failed: int, skipped: int) -> Text:
    """Color heat bar + pass/fail/skip counts, matching session trend style."""
    if pct is None:
        return Text("  —", style=theme.text_ghost)
    bar_len = 6
    filled = int(round(pct / 100 * bar_len))
    bar = "█" * filled + "░" * (bar_len - filled)
    color = _score_color(pct)
    t = Text()
    t.append(f"{bar} ", style=color)
    t.append(f"{pct:.1f}%  ", style=f"bold {color}")
    t.append(f"{passed}✓ ", style=theme.success)
    t.append(f"{failed}✗ ", style=theme.error)
    t.append(f"{skipped}–", style=theme.text_dim)
    return t


@registry.register("fleet", "status",
                   description="Multi-environment compliance overview from local cache")
def handle_fleet_status(args: Namespace) -> int:
    entries, summary = collect_fleet()

    if getattr(args, "as_json", False):
        payload = {
            "summary": summary.to_dict(),
            "environments": [e.to_dict() for e in entries],
        }
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return 0

    if not entries:
        console.print(
            f"[{theme.text_dim}]No environments configured. Create one with "
            f"[bold]platform-atlas env new[/bold].[/{theme.text_dim}]"
        )
        return 0

    # Header summary
    fleet_pct = summary.fleet_pass_rate_pct
    if fleet_pct is not None:
        fleet_color = _score_color(fleet_pct)
        fleet_rate_str = f"[bold {fleet_color}]{fleet_pct:.1f}%[/bold {fleet_color}]"
    else:
        fleet_rate_str = f"[{theme.text_dim}]—[/{theme.text_dim}]"

    console.print(
        f"\n[bold]Fleet · {summary.total_envs} environment{'s' if summary.total_envs != 1 else ''}[/bold]  "
        f"[{theme.text_dim}]│[/{theme.text_dim}]  "
        f"fleet pass rate: {fleet_rate_str}\n"
    )

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style=f"bold {theme.primary}",
        padding=(0, 1),
    )
    table.add_column("Environment", no_wrap=True, min_width=14)
    table.add_column("Tier", no_wrap=True, min_width=10)
    table.add_column("Last Session", no_wrap=True, min_width=16)
    table.add_column("Age", no_wrap=True, justify="right", min_width=6)
    table.add_column("Compliance", no_wrap=True, min_width=30)

    for entry in entries:
        name_cell = Text()
        if entry.is_active:
            name_cell.append("● ", style=theme.accent)
        else:
            name_cell.append("  ")
        name_cell.append(entry.name, style="bold")

        tier_label = {
            "standard": "Standard",
            "extended": "Extended",
            "saas":     "SaaS",
        }.get((entry.tier or "").lower(), entry.tier or "")
        tier_cell = Text(tier_label or "—", style=theme.text_dim if not tier_label else "")

        last_session = Text()
        if entry.last_session_name:
            last_session.append(entry.last_session_name)
            if entry.last_session_status:
                last_session.append(f"  {entry.last_session_status}", style=theme.text_dim)
        else:
            last_session.append("—", style=theme.text_ghost)

        table.add_row(
            name_cell,
            tier_cell,
            last_session,
            Text(_format_age(entry.last_session_age_seconds), style=theme.text_dim),
            _heat_bar(entry.pass_rate_pct, entry.pass_count, entry.fail_count, entry.skip_count),
        )

    console.print(table)
    console.print(
        f"  [{theme.text_dim}]Color key: "
        f"[{theme.success}]≥85%[/{theme.success}]  "
        f"[{theme.warning}]75–84%[/{theme.warning}]  "
        f"[{theme.error}]<75%[/{theme.error}]  "
        f"· Read-only snapshot — captures are not triggered by this command.[/{theme.text_dim}]\n"
    )
    return 0
