# pylint: disable=line-too-long
"""
ATLAS // Dashboard

Redesigned in v1.5 to show session bindings as a visual relationship:
    Environment ──▸ Session ◂── Ruleset + Profile

The active session hero panel displays the full context at a glance.
The sessions table shows recent sessions with their bindings, pipeline
progress, and results.
"""

import datetime
import json
from rich import box
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.rule import Rule
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.padding import Padding

from platform_atlas.core._version import __version__
from platform_atlas.core.context import ctx
from platform_atlas.core import ui
from platform_atlas.core.session_manager import get_session_manager, NoActiveSessionError
from platform_atlas.core.ruleset_manager import get_ruleset_manager
from platform_atlas.core.exceptions import ConfigError

theme = ui.theme
console = Console()

# ── Helpers ──────────────────────────────────────────────────────

STATUS_COLORS = {
    "created": "text_dim",
    "capturing": "primary",
    "captured": "info",
    "validating": "warning",
    "validated": "success",
    "reported": "success_glow",
    "failed": "error",
}

def _sc(status: str) -> str:
    """Get the theme color string for a session status."""
    return getattr(theme, STATUS_COLORS.get(status, "text_dim"))


def _dot(done: bool) -> str:
    return f"[{theme.success}]●[/{theme.success}]" if done else f"[{theme.text_ghost}]○[/{theme.text_ghost}]"


def _pipeline(meta) -> str:
    """Build the C → V → R pipeline string from session metadata."""
    return (
        f"{_dot(meta.capture_completed)} C  "
        f"[{theme.text_ghost}]→[/{theme.text_ghost}]  "
        f"{_dot(meta.validation_completed)} V  "
        f"[{theme.text_ghost}]→[/{theme.text_ghost}]  "
        f"{_dot(meta.report_completed)} R"
    )


def _time_ago(dt: datetime.datetime) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = now - dt
    if delta.days > 0:
        return f"{delta.days}d ago"
    if delta.seconds > 3600:
        return f"{delta.seconds // 3600}h ago"
    if delta.seconds > 60:
        return f"{delta.seconds // 60}m ago"
    return "just now"


def _next_step(meta) -> tuple[str, str]:
    """Return (description, command) for the next pipeline step."""
    status = str(meta.status)
    next_map = {
        "created":    ("Run data capture",        "session run capture"),
        "capturing":  ("Resume capture",           "session run capture"),
        "captured":   ("Run validation",           "session run validate"),
        "validating": ("Resume validation",        "session run validate"),
        "validated":  ("Generate report",          "session run report"),
        "reported":   ("View report or export",    f"session show {meta.name}"),
        "failed":     ("Review errors",            f"session show {meta.name}"),
    }
    return next_map.get(status, ("Continue", "session --help"))


# ── Main dashboard ───────────────────────────────────────────────

def show_dashboard():
    """Show Atlas info dashboard when no arguments provided"""

    console.clear()

    session_mgr = get_session_manager()
    ruleset_mgr = get_ruleset_manager()

    # ═══════════════════════════════════════════════════════════════
    # HEADER BANNER
    # ═══════════════════════════════════════════════════════════════
    banner_text = Text(justify="center")
    banner_text.append("⬡ ", style=f"bold {theme.primary_glow}")
    banner_text.append("Platform Atlas", style=f"bold {theme.banner_fg}")
    banner_text.append(f"  v{__version__}", style=theme.text_muted)

    subtitle_text = Text(
        "Itential Platform Configuration Auditing & Validation",
        style=theme.text_dim,
        justify="center",
    )

    banner = Panel(
        Group(
            Align.center(banner_text),
            Align.center(subtitle_text),
        ),
        box=box.HEAVY,
        border_style=theme.banner_rule,
        style=f"on {theme.banner_bg}",
        padding=(1, 2),
        expand=True,
    )
    console.print(banner)

    # ═══════════════════════════════════════════════════════════════
    # ACTIVE SESSION HERO
    # ═══════════════════════════════════════════════════════════════

    try:
        active_session = session_mgr.get_active()
    except NoActiveSessionError:
        active_session = None

    if active_session:
        meta = active_session.metadata
        sc = _sc(str(meta.status))

        # ── Build a clean key-value details table ─────────────────
        details = Table(
            box=None,
            show_header=False,
            padding=(0, 2),
            expand=False,
        )
        details.add_column("label", style=theme.text_ghost, min_width=14)
        details.add_column("value")

        details.add_row(
            "Status",
            f"[{sc} bold]{meta.status}[/{sc} bold]    {_pipeline(meta)}"
        )

        if meta.organization_name:
            details.add_row(
                "Organization",
                f"[bold]{meta.organization_name}[/bold]"
            )

        if meta.environment:
            details.add_row(
                "Environment",
                f"[{theme.primary}]{meta.environment}[/{theme.primary}]"
            )

        if meta.ruleset_id:
            rs_display = f"[{theme.secondary}]{meta.ruleset_id}[/{theme.secondary}]"
            if meta.ruleset_profile:
                rs_display += f"  [{theme.text_ghost}]+[/{theme.text_ghost}]  [{theme.warning}]{meta.ruleset_profile}[/{theme.warning}]"
            details.add_row("Ruleset", rs_display)

        # Results row (only if validated)
        if meta.validation_completed and meta.total_rules > 0:
            evaluated = meta.pass_count + meta.fail_count
            rate = round(meta.pass_count / evaluated * 100, 1) if evaluated else 0
            details.add_row(
                "Results",
                f"[{theme.success} bold]{meta.pass_count}[/{theme.success} bold] "
                f"[{theme.text_dim}]pass[/{theme.text_dim}]  "
                f"[{theme.error} bold]{meta.fail_count}[/{theme.error} bold] "
                f"[{theme.text_dim}]fail[/{theme.text_dim}]  "
                f"[{theme.text_ghost}]{meta.skip_count}[/{theme.text_ghost}] "
                f"[{theme.text_dim}]skip[/{theme.text_dim}]  "
                f"[{theme.text_ghost}]·[/{theme.text_ghost}]  "
                f"[bold]{rate}%[/bold] [{theme.text_dim}]compliance[/{theme.text_dim}]"
            )

        # Next step row
        label, cmd = _next_step(meta)
        details.add_row(
            "Next",
            f"[{theme.accent}]→[/{theme.accent}] {label}:  "
            f"[bold {theme.primary}]{cmd}[/bold {theme.primary}]"
        )

        console.print(Panel(
            details,
            title=(
                f"[bold {theme.primary}]Active Session[/bold {theme.primary}]"
                f"  [{theme.text_ghost}]·[/{theme.text_ghost}]  "
                f"[bold]{meta.name}[/bold]"
            ),
            title_align="left",
            border_style=f"{theme.primary}",
            box=box.ROUNDED,
            style=f"on {theme.tint_primary}",
            padding=(1, 2),
            expand=True,
        ))

    # ═══════════════════════════════════════════════════════════════
    # NO SESSION — GETTING STARTED
    # ═══════════════════════════════════════════════════════════════

    if not active_session:
        all_sessions = session_mgr.list()

        if all_sessions:
            body = (
                f"  [{theme.text_dim}]No active session.[/{theme.text_dim}]\n"
                f"  Switch to an existing session or create a new one:\n\n"
                f"    [bold {theme.primary}]session switch[/bold {theme.primary}]"
                f"        [{theme.text_dim}]Pick from existing sessions[/{theme.text_dim}]\n"
                f"    [bold {theme.primary}]session create <n>[/bold {theme.primary}]"
                f"   [{theme.text_dim}]Start a new audit[/{theme.text_dim}]"
            )
        else:
            body = (
                f"  [{theme.text_dim}]No sessions yet. Create one to get started:[/{theme.text_dim}]\n\n"
                f"    [{theme.text_dim}]1.[/{theme.text_dim}]  "
                f"[bold {theme.primary}]session create <n>[/bold {theme.primary}]"
                f"   [{theme.text_dim}]Create a session (selects env + ruleset)[/{theme.text_dim}]\n"
                f"    [{theme.text_dim}]2.[/{theme.text_dim}]  "
                f"[bold {theme.primary}]session run all[/bold {theme.primary}]"
                f"        [{theme.text_dim}]Run the full pipeline[/{theme.text_dim}]"
            )

        console.print(Panel(
            body,
            title=f"[bold {theme.primary}]Getting Started[/bold {theme.primary}]",
            title_align="left",
            border_style=theme.primary,
            box=box.ROUNDED,
            style=f"on {theme.tint_primary}",
            padding=(1, 2),
            expand=True,
        ))

    # ═══════════════════════════════════════════════════════════════
    # MISMATCH WARNINGS
    # ═══════════════════════════════════════════════════════════════

    if active_session and active_session.capture_file.exists():
        active_ruleset = ruleset_mgr.get_active_ruleset_id()
        active_profile = ruleset_mgr.get_active_profile_id()
        env_name = ctx().active_environment
        warnings = []

        capture_meta = {}
        try:
            with open(active_session.capture_file, encoding="utf-8") as f:
                capture_data = json.load(f)
            capture_meta = capture_data.get("_atlas", {}).get("metadata", {})
        except Exception:
            pass

        session_ruleset = getattr(active_session.metadata, "ruleset_id", None)
        if session_ruleset and active_ruleset and session_ruleset != active_ruleset:
            warnings.append(
                f"[{theme.warning}]⚠[/{theme.warning}]  Session was captured with ruleset "
                f"[bold]{session_ruleset}[/bold] but [{theme.accent}]{active_ruleset}[/{theme.accent}] is now loaded"
            )

        capture_profile = capture_meta.get("ruleset_profile", "")
        if capture_profile and active_profile and capture_profile != active_profile:
            warnings.append(
                f"[{theme.warning}]⚠[/{theme.warning}]  Session was captured with profile "
                f"[bold]{capture_profile}[/bold] but [{theme.accent}]{active_profile}[/{theme.accent}] is now active"
            )

        capture_env = capture_meta.get("environment") or capture_meta.get("active_environment")
        if capture_env and env_name and capture_env != env_name:
            warnings.append(
                f"[{theme.warning}]⚠[/{theme.warning}]  Session was captured under environment "
                f"[bold]{capture_env}[/bold] but [{theme.accent}]{env_name}[/{theme.accent}] is now active"
            )

        if warnings:
            warn_content = "\n".join(f"  {w}" for w in warnings)
            console.print(Panel(
                warn_content,
                border_style=theme.warning,
                box=box.ROUNDED,
                style=f"on {theme.tint_warning}",
                padding=(0, 1),
                expand=True,
            ))

    # ═══════════════════════════════════════════════════════════════
    # RECENT SESSIONS TABLE (5 most recent)
    # ═══════════════════════════════════════════════════════════════
    all_sessions = session_mgr.list()

    if all_sessions:
        recent = sorted(
            all_sessions,
            key=lambda s: s.metadata.updated_at,
            reverse=True,
        )[:5]

        sessions_table = Table(
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style=f"bold {theme.text_dim}",
            padding=(0, 1),
            expand=True,
        )
        sessions_table.add_column("", width=2)
        sessions_table.add_column("Session", style=theme.text_primary, ratio=3)
        sessions_table.add_column("Environment", ratio=2)
        sessions_table.add_column("Organization", ratio=2)
        sessions_table.add_column("Profile", ratio=2)
        sessions_table.add_column("Status", justify="center", ratio=1)
        sessions_table.add_column("Pipeline", justify="center", ratio=2)
        sessions_table.add_column("Results", justify="right", ratio=2)
        sessions_table.add_column("Updated", justify="right", ratio=1)

        active_name = session_mgr.get_active_session_name()

        for sess in recent:
            is_active = sess.name == active_name
            m = sess.metadata

            # Marker
            marker = f"[{theme.success}]▸[/{theme.success}]" if is_active else ""

            # Name
            name_text = (
                f"[bold {theme.accent}]{m.name}[/bold {theme.accent}]"
                if is_active
                else m.name
            )

            # Environment
            env_text = (
                f"[{theme.primary}]{m.environment}[/{theme.primary}]"
                if m.environment
                else f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            )

            # Organization
            org_text = m.organization_name or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"

            # Profile
            if m.ruleset_profile:
                rs_text = f"[{theme.secondary}]{m.ruleset_profile}[/{theme.secondary}]"
            else:
                rs_text = f"[{theme.text_ghost}]—[/{theme.text_ghost}]"

            # Status
            sc = _sc(str(m.status))
            status_text = f"[{sc}]{m.status}[/{sc}]"

            # Pipeline
            pipe = (
                f"{_dot(m.capture_completed)} C "
                f"{_dot(m.validation_completed)} V "
                f"{_dot(m.report_completed)} R"
            )

            # Results
            if m.validation_completed:
                results = (
                    f"[{theme.success}]{m.pass_count}[/{theme.success}]✓ "
                    f"[{theme.error}]{m.fail_count}[/{theme.error}]✗"
                )
            else:
                results = f"[{theme.text_ghost}]—[/{theme.text_ghost}]"

            # Updated
            updated = f"[{theme.text_ghost}]{_time_ago(m.updated_at)}[/{theme.text_ghost}]"

            sessions_table.add_row(
                marker, name_text, env_text, org_text,
                rs_text, status_text, pipe, results, updated,
            )

        # Title with count
        total = len(all_sessions)
        title_suffix = f"  [{theme.text_ghost}]({total} total — showing 5 most recent)[/{theme.text_ghost}]" if total > 5 else ""

        console.print(Panel(
            Group(
                Text.from_markup(
                    f"  [bold {theme.text_secondary}]Sessions[/bold {theme.text_secondary}]"
                    f"{title_suffix}"
                ),
                sessions_table,
            ),
            box=box.ROUNDED,
            border_style=theme.border_dim,
            style=f"on {theme.tint_neutral}",
            padding=(1, 0),
            expand=True,
        ))

    # ═══════════════════════════════════════════════════════════════
    # QUICK SWITCH & HELP FOOTER
    # ═══════════════════════════════════════════════════════════════
    sep = f"  [{theme.text_ghost}]│[/{theme.text_ghost}]  "

    switch_cmds = sep.join([
        f"[{theme.primary}]session switch[/{theme.primary}]",
        f"[{theme.primary}]session create[/{theme.primary}]",
        f"[{theme.primary}]session edit[/{theme.primary}]",
        f"[{theme.primary}]preflight[/{theme.primary}]",
    ])

    help_cmds = sep.join([
        f"[{theme.primary}]--help[/{theme.primary}]",
        f"[{theme.primary}]session --help[/{theme.primary}]",
        f"[{theme.primary}]env --help[/{theme.primary}]",
        f"[{theme.primary}]guide[/{theme.primary}]",
    ])

    footer_text = (
        f"[{theme.text_dim}]Quick:[/{theme.text_dim}]   {switch_cmds}\n"
        f"[{theme.text_dim}]Help:[/{theme.text_dim}]    {help_cmds}"
    )

    console.print(Panel(
        footer_text,
        box=box.SIMPLE,
        style=f"on {theme.tint_neutral}",
        padding=(0, 2),
        expand=True,
    ))
    console.print()
