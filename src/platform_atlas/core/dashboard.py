# pylint: disable=line-too-long
"""
ATLAS // Dashboard

The dashboard is the user's first impression of Platform Atlas — the welcome
screen, the status board, and the wayfinder. Three goals shape the design:

    1. Make the active session immediately legible at a glance.
    2. Show the C → V → R pipeline state visually, not just textually.
    3. Surface the next action so the user always knows what to run next.

Layout (top to bottom):
    Banner ........ wordmark + honeycomb mark + context strip
    Hero .......... active session card with compliance bar + next-step chip
    Warnings ...... mismatch banners (env / ruleset / profile drift)
    Sessions ...... 6-column table of the 5 most recent sessions
    Footer ........ quick-switch + help command lanes
"""

import datetime
import json

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from platform_atlas.core._version import __version__
from platform_atlas.core import ui
from platform_atlas.core.context import ctx
from platform_atlas.core.paths import ATLAS_RULESET_UPDATE_STATE
from platform_atlas.core.session_manager import get_session_manager, NoActiveSessionError
from platform_atlas.core.ruleset_manager import get_ruleset_manager

theme = ui.theme
console = Console()


# ── Status palette ────────────────────────────────────────────────

STATUS_COLORS = {
    "created":    "text_dim",
    "capturing":  "primary",
    "captured":   "info",
    "validating": "warning",
    "validated":  "success",
    "reported":   "success_glow",
    "failed":     "error",
}


def _sc(status: str) -> str:
    """Theme color string for a session status."""
    return getattr(theme, STATUS_COLORS.get(status, "text_dim"))


# ── Color helpers ─────────────────────────────────────────────────

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


def _gradient(text: str, *stops: str, bold: bool = True) -> Text:
    """Render text with a smooth color gradient across the given hex stops.

    Used for the wordmark — three color stops let us fade from primary_glow
    through primary into secondary, giving the title a subtle depth without
    looking gimmicky.
    """
    out = Text()
    chars = list(text)
    if not chars or not stops:
        out.append(text)
        return out
    if len(stops) == 1 or len(chars) == 1:
        out.append(text, style=f"bold {stops[0]}" if bold else stops[0])
        return out

    rgb_stops = [_hex_to_rgb(c) for c in stops]
    n_chars = len(chars)
    n_segs = len(rgb_stops) - 1

    for i, ch in enumerate(chars):
        if ch == " ":
            out.append(" ")
            continue
        t = i / (n_chars - 1)
        seg = min(int(t * n_segs), n_segs - 1)
        local = (t * n_segs) - seg
        a = rgb_stops[seg]
        b = rgb_stops[seg + 1]
        c = tuple(round(a[k] + (b[k] - a[k]) * local) for k in range(3))
        out.append(ch, style=f"bold {_rgb_to_hex(c)}" if bold else _rgb_to_hex(c))
    return out


# ── Pipeline glyphs ───────────────────────────────────────────────

def _stage_color(done: bool) -> str:
    return theme.success if done else theme.text_ghost


def _stage_glyph(done: bool) -> str:
    from platform_atlas.core.ui import is_plain_mode
    if is_plain_mode():
        return "*" if done else "o"
    return "◉" if done else "◯"


def _pipeline_chain(meta) -> Text:
    """Render the C━V━R pipeline as a connected chain.

    Connectors between stages light up green only when both endpoints are
    complete — so a half-done pipeline reads at a glance: filled, green link,
    filled, dim link, hollow.
    """
    from platform_atlas.core.ui import is_plain_mode
    plain = is_plain_mode()
    connector = "---" if plain else "━━━"
    out = Text()
    stages = [
        meta.capture_completed,
        meta.validation_completed,
        meta.report_completed,
    ]
    labels = ["C", "V", "R"]
    for i, done in enumerate(stages):
        if i > 0:
            link_done = stages[i - 1] and done
            out.append(connector, style=theme.success if link_done else theme.text_ghost)
        out.append(_stage_glyph(done), style=f"bold {_stage_color(done)}")
        out.append(f" {labels[i]}", style=theme.text_secondary if done else theme.text_ghost)
    return out


def _pipeline_compact(meta) -> Text:
    """Tight 3-glyph pipeline chain for the sessions table — no labels."""
    from platform_atlas.core.ui import is_plain_mode
    plain = is_plain_mode()
    out = Text()
    stages = [
        meta.capture_completed,
        meta.validation_completed,
        meta.report_completed,
    ]
    for i, done in enumerate(stages):
        if i > 0:
            link_done = stages[i - 1] and done
            out.append("-" if plain else "─", style=theme.success if link_done else theme.text_ghost)
        out.append(_stage_glyph(done), style=f"bold {_stage_color(done)}")
    return out


# ── Compliance bar ────────────────────────────────────────────────

def _compliance_bar(passed: int, failed: int, skipped: int, width: int = 22) -> Text:
    """A horizontal segmented bar — pass green, fail red, skip ghost.

    The bar is exactly `width` cells. Uses round() per segment then squeezes
    the skip segment to absorb rounding error, so the bar always sums to width.
    """
    total = passed + failed + skipped
    out_bar = Text()
    if total == 0:
        out_bar.append("─" * width, style=theme.text_ghost)
        return out_bar

    p_w = round(passed / total * width)
    f_w = round(failed / total * width)
    s_w = max(0, width - p_w - f_w)

    if p_w:
        out_bar.append("█" * p_w, style=theme.success)
    if f_w:
        out_bar.append("█" * f_w, style=theme.error)
    if s_w:
        out_bar.append("░" * s_w, style=theme.text_ghost)
    return out_bar


# ── Time formatting ───────────────────────────────────────────────

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
    """(description, command) for the next pipeline step."""
    status = str(meta.status)
    next_map = {
        "created":    ("Run data capture",         "session run capture"),
        "capturing":  ("Resume capture",           "session run capture"),
        "captured":   ("Run validation",           "session run validate"),
        "validating": ("Resume validation",        "session run validate"),
        "validated":  ("Generate report",          "session run report"),
        "reported":   ("View report or export",    f"session show {meta.name}"),
        "failed":     ("Review errors",            f"session show {meta.name}"),
    }
    return next_map.get(status, ("Continue", "session --help"))


# ══════════════════════════════════════════════════════════════════
# BANNER
# ══════════════════════════════════════════════════════════════════

def _build_banner() -> Panel:
    """Three-line stylized banner: honeycomb hex mark + wordmark + context strip."""

    # Honeycomb — 3 rows of unicode hex glyphs.
    # Outer hexes use the glow color, inner ring uses primary, center uses accent.
    # The result reads as a tight 7-hex cluster that recalls a network/grid motif.
    hex_mark = Text()
    hex_mark.append(" ⬢ ⬢", style=f"bold {theme.primary_glow}")
    hex_mark.append("\n")
    hex_mark.append("⬢ ", style=f"bold {theme.primary}")
    hex_mark.append("⬢", style=f"bold {theme.accent}")
    hex_mark.append(" ⬢", style=f"bold {theme.primary}")
    hex_mark.append("\n")
    hex_mark.append(" ⬢ ⬢", style=f"bold {theme.primary_glow}")

    # Wordmark with a 3-stop gradient: glow → primary → secondary
    wordmark = _gradient(
        "PLATFORM ATLAS",
        theme.primary_glow,
        theme.primary,
        theme.secondary,
    )

    # Right column: title row, tagline, context strip
    right = Text()
    right.append(wordmark)
    right.append("    ")
    right.append(f"v{__version__}", style=theme.text_muted)
    right.append("\n")
    right.append(
        "Itential Platform — Configuration Audit & Validation",
        style=theme.text_dim,
    )
    right.append("\n")

    # Context strip — mode • env • theme • now
    active_ctx = _ctx_safe()
    env_name = active_ctx.active_environment if active_ctx else None
    theme_id = active_ctx.config.theme if active_ctx else None
    tier_name = active_ctx.tier if active_ctx else None
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Resolve env_tint for the banner border
    _env_tint: str | None = None
    if active_ctx and env_name:
        try:
            from platform_atlas.core.environment import get_environment_manager as _gem
            _mgr = _gem()
            if _mgr.exists(env_name):
                _env_obj = _mgr.load(env_name)
                _env_tint = getattr(_env_obj, "env_tint", None)
        except Exception:
            pass
    _TINT_MAP = {"high": "#C5258F", "medium": "#FDD058", "low": "#99CA3C"}
    banner_border = _TINT_MAP.get(_env_tint or "", theme.banner_rule)

    parts: list[str] = []
    if tier_name:
        tier_color = theme.tier_standard if tier_name == "standard" else theme.tier_extended
        parts.append(
            f"[{tier_color}]{tier_name.capitalize()}[/{tier_color}] "
            f"[{theme.text_ghost}]mode[/{theme.text_ghost}]"
        )
    if env_name:
        parts.append(f"[{theme.primary}]{env_name}[/{theme.primary}] [{theme.text_ghost}]env[/{theme.text_ghost}]")
    else:
        parts.append(f"[{theme.text_ghost}]no environment[/{theme.text_ghost}]")
    if theme_id:
        parts.append(f"[{theme.text_dim}]{theme_id}[/{theme.text_dim}] [{theme.text_ghost}]theme[/{theme.text_ghost}]")
    parts.append(f"[{theme.text_dim}]{now}[/{theme.text_dim}]")

    sep = f"  [{theme.text_ghost}]·[/{theme.text_ghost}]  "
    right.append(Text.from_markup(sep.join(parts)))

    # Side-by-side layout: hex mark | wordmark+tagline+strip
    layout = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    layout.add_column(width=8, no_wrap=True)
    layout.add_column(width=2)  # spacer
    layout.add_column(ratio=1)
    layout.add_row(hex_mark, "", right)

    return Panel(
        layout,
        box=box.HEAVY,
        border_style=banner_border,
        style=f"on {theme.banner_bg}",
        padding=(1, 2),
        expand=True,
    )


def _ctx_safe():
    """Return ctx() or None if context isn't initialized."""
    try:
        return ctx()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# ACTIVE SESSION HERO
# ══════════════════════════════════════════════════════════════════

def _build_hero(active_session) -> Panel:
    """The hero card: full session context at a glance."""
    meta = active_session.metadata
    sc = _sc(str(meta.status))

    details = Table(
        box=None,
        show_header=False,
        padding=(0, 2),
        expand=True,
    )
    details.add_column("label", style=theme.text_ghost, min_width=14, no_wrap=True)
    details.add_column("value", ratio=1)

    # Status row — status text, then chained pipeline glyph
    status_line = Text()
    status_line.append(str(meta.status), style=f"bold {sc}")
    status_line.append("    ")
    status_line.append(_pipeline_chain(meta))
    details.add_row("Status", status_line)

    if meta.organization_name:
        details.add_row(
            "Organization",
            Text(meta.organization_name, style="bold"),
        )

    # Session tier — Standard (blue) vs Extended (orange).
    # Pre-1.7 sessions don't carry a tier field — fall back to Extended.
    session_tier = (getattr(meta, "tier", None) or "extended").lower()
    if session_tier == "standard":
        tier_label = "Standard"
        tier_style = f"bold {theme.tier_standard}"
    else:
        tier_label = "Extended"
        tier_style = f"bold {theme.tier_extended}"
    details.add_row("Mode", Text(tier_label, style=tier_style))

    if meta.environment:
        details.add_row(
            "Environment",
            Text(meta.environment, style=f"bold {theme.primary}"),
        )

    if meta.ruleset_id:
        rs = Text()
        rs.append(meta.ruleset_id, style=theme.secondary)
        if meta.ruleset_profile:
            rs.append("  +  ", style=theme.text_ghost)
            rs.append(meta.ruleset_profile, style=theme.warning)
        details.add_row("Ruleset", rs)

    # Compliance bar — visceral pass/fail/skip distribution.
    # Two-line layout so it never wraps: bar + percentage on row 1, counts on row 2.
    if meta.validation_completed and meta.total_rules > 0:
        evaluated = meta.pass_count + meta.fail_count
        rate = round(meta.pass_count / evaluated * 100, 1) if evaluated else 0.0
        rate_color = (
            theme.success if rate >= 90
            else theme.warning if rate >= 70
            else theme.error
        )

        compliance = Text()
        compliance.append(_compliance_bar(meta.pass_count, meta.fail_count, meta.skip_count, width=24))
        compliance.append("   ")
        compliance.append(f"{rate:>5.1f}%", style=f"bold {rate_color}")
        compliance.append("\n")
        compliance.append(f"{meta.pass_count}", style=f"bold {theme.success}")
        compliance.append(" pass", style=theme.text_dim)
        compliance.append("  ·  ", style=theme.text_ghost)
        compliance.append(f"{meta.fail_count}", style=f"bold {theme.error}")
        compliance.append(" fail", style=theme.text_dim)
        compliance.append("  ·  ", style=theme.text_ghost)
        compliance.append(f"{meta.skip_count}", style=theme.text_ghost)
        compliance.append(" skip", style=theme.text_dim)
        details.add_row("Compliance", compliance)

    # Spacer row before the next-step chip — gives the chip room to breathe
    details.add_row("", "")

    # Next-step — description on one line, command on its own indented line
    label, cmd = _next_step(meta)
    next_block = Text()
    next_block.append("→ ", style=f"bold {theme.accent}")
    next_block.append(label, style=theme.text_primary)
    next_block.append("\n  ")
    next_block.append("▎ ", style=f"bold {theme.accent}")
    next_block.append(f"$ {cmd}", style=f"bold {theme.primary}")
    details.add_row("Next", next_block)

    # Title combines static label with session name + status pill
    title = Text()
    title.append(" ACTIVE SESSION ", style=f"bold {theme.bg_primary} on {theme.primary}")
    title.append("  ")
    title.append(meta.name, style=f"bold {theme.text_primary}")
    title.append("  ")
    title.append(f" {meta.status} ", style=f"bold {theme.bg_primary} on {sc}")

    return Panel(
        details,
        title=title,
        title_align="left",
        border_style=theme.primary,
        box=box.ROUNDED,
        style=f"on {theme.tint_primary}",
        padding=(1, 2),
        expand=True,
    )


# ══════════════════════════════════════════════════════════════════
# GETTING STARTED (no active session)
# ══════════════════════════════════════════════════════════════════

def _build_getting_started(has_sessions: bool) -> Panel:
    if has_sessions:
        body = (
            f"  [{theme.text_dim}]No active session.[/{theme.text_dim}]\n"
            f"  Switch to an existing session or create a new one:\n\n"
            f"    [{theme.accent}]▎[/{theme.accent}] [bold {theme.primary}]session switch[/bold {theme.primary}]"
            f"        [{theme.text_dim}]Pick from existing sessions[/{theme.text_dim}]\n"
            f"    [{theme.accent}]▎[/{theme.accent}] [bold {theme.primary}]session create <name>[/bold {theme.primary}]"
            f"   [{theme.text_dim}]Start a new audit[/{theme.text_dim}]"
        )
    else:
        body = (
            f"  [{theme.text_dim}]No sessions yet. Create one to get started:[/{theme.text_dim}]\n\n"
            f"    [{theme.text_dim}]1.[/{theme.text_dim}]  "
            f"[bold {theme.primary}]session create <name>[/bold {theme.primary}]"
            f"   [{theme.text_dim}]Create a session (selects env + ruleset)[/{theme.text_dim}]\n"
            f"    [{theme.text_dim}]2.[/{theme.text_dim}]  "
            f"[bold {theme.primary}]session run all[/bold {theme.primary}]"
            f"        [{theme.text_dim}]Run the full pipeline[/{theme.text_dim}]"
        )

    title = Text()
    title.append(" GETTING STARTED ", style=f"bold {theme.bg_primary} on {theme.primary}")

    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=theme.primary,
        box=box.ROUNDED,
        style=f"on {theme.tint_primary}",
        padding=(1, 2),
        expand=True,
    )


# ══════════════════════════════════════════════════════════════════
# MISMATCH WARNINGS
# ══════════════════════════════════════════════════════════════════

def _build_ruleset_update_notice() -> Panel | None:
    """Return an info panel if a declined ruleset update is pending, else None."""
    try:
        if not ATLAS_RULESET_UPDATE_STATE.is_file():
            return None
        with open(ATLAS_RULESET_UPDATE_STATE, encoding="utf-8") as f:
            state = json.load(f)
        updates = state.get("updates", [])
        if not updates:
            return None
    except Exception:
        return None

    lines = []
    for u in updates:
        lines.append(
            f"  [{theme.info}]↑[/{theme.info}]  [bold]{u.get('id', '?')}[/bold]  "
            f"[dim]{u.get('current_version', '?')}[/dim] → [{theme.success}]{u.get('available_version', '?')}[/{theme.success}]"
        )
    lines.append(
        f"\n  [{theme.text_ghost}]Run[/{theme.text_ghost}]  "
        f"[{theme.primary}]platform-atlas ruleset update[/{theme.primary}]  "
        f"[{theme.text_ghost}]to apply[/{theme.text_ghost}]"
    )

    title = Text()
    title.append(" RULESET UPDATE AVAILABLE ", style=f"bold {theme.bg_primary} on {theme.info}")

    return Panel(
        "\n".join(lines),
        title=title,
        title_align="left",
        border_style=theme.info,
        box=box.ROUNDED,
        style=f"on {theme.tint_neutral}",
        padding=(0, 1),
        expand=True,
    )


def _build_warnings(active_session) -> Panel | None:
    if not active_session.capture_file.exists():
        return None

    ruleset_mgr = get_ruleset_manager()
    active_ruleset = ruleset_mgr.get_active_ruleset_id()
    active_profile = ruleset_mgr.get_active_profile_id()
    env_name = ctx().active_environment

    capture_meta = {}
    try:
        with open(active_session.capture_file, encoding="utf-8") as f:
            capture_data = json.load(f)
        capture_meta = capture_data.get("_atlas", {}).get("metadata", {})
    except Exception:
        pass

    warnings: list[str] = []

    session_ruleset = getattr(active_session.metadata, "ruleset_id", None)
    if session_ruleset and active_ruleset and session_ruleset != active_ruleset:
        warnings.append(
            f"  [{theme.warning}]⚠[/{theme.warning}]  Session was captured with ruleset "
            f"[bold]{session_ruleset}[/bold] but [{theme.accent}]{active_ruleset}[/{theme.accent}] is now loaded"
        )

    capture_profile = capture_meta.get("ruleset_profile", "")
    if capture_profile and active_profile and capture_profile != active_profile:
        warnings.append(
            f"  [{theme.warning}]⚠[/{theme.warning}]  Session was captured with profile "
            f"[bold]{capture_profile}[/bold] but [{theme.accent}]{active_profile}[/{theme.accent}] is now active"
        )

    capture_env = capture_meta.get("environment") or capture_meta.get("active_environment")
    if capture_env and env_name and capture_env != env_name:
        warnings.append(
            f"  [{theme.warning}]⚠[/{theme.warning}]  Session was captured under environment "
            f"[bold]{capture_env}[/bold] but [{theme.accent}]{env_name}[/{theme.accent}] is now active"
        )

    if not warnings:
        return None

    title = Text()
    title.append(" BINDING DRIFT ", style=f"bold {theme.bg_primary} on {theme.warning}")

    return Panel(
        "\n".join(warnings),
        title=title,
        title_align="left",
        border_style=theme.warning,
        box=box.ROUNDED,
        style=f"on {theme.tint_warning}",
        padding=(0, 1),
        expand=True,
    )


# ══════════════════════════════════════════════════════════════════
# SESSIONS TABLE
# ══════════════════════════════════════════════════════════════════

def _build_sessions_panel(all_sessions, active_name: str | None) -> Panel:
    recent = sorted(
        all_sessions,
        key=lambda s: s.metadata.updated_at,
        reverse=True,
    )[:5]

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style=f"bold {theme.text_dim}",
        padding=(0, 1),
        expand=True,
        row_styles=["", f"on {theme.bg_secondary}"],
    )
    # 6 columns. Dropped Organization (in hero) and the standalone marker column —
    # the active row's accent bar is rendered inline with the session name. Every
    # column has a fixed width that fits its header at 80-col terminal widths.
    table.add_column("Session", min_width=14, no_wrap=True)
    table.add_column("Environment", min_width=11, no_wrap=True)
    table.add_column("State", min_width=10, no_wrap=True)
    table.add_column("Pipeline", justify="center", min_width=8, no_wrap=True)
    table.add_column("Results", justify="right", min_width=8, no_wrap=True)
    table.add_column("Last", justify="right", min_width=7, no_wrap=True)

    for sess in recent:
        is_active = sess.name == active_name
        m = sess.metadata

        # Session — accent bar + name, bold + accent color when active
        name_text = Text()
        if is_active:
            name_text.append("▎", style=f"bold {theme.accent}")
            name_text.append(m.name, style=f"bold {theme.accent}")
        else:
            name_text.append(" ", style=theme.text_ghost)
            name_text.append(m.name, style=theme.text_primary)

        # Environment
        env_text = (
            Text(m.environment, style=theme.primary)
            if m.environment
            else Text("—", style=theme.text_ghost)
        )

        # State
        sc = _sc(str(m.status))
        status_text = Text(str(m.status), style=sc)

        # Pipeline (compact chain)
        pipe = _pipeline_compact(m)

        # Results
        if m.validation_completed:
            results = Text()
            results.append(f"{m.pass_count}", style=f"bold {theme.success}")
            results.append("✓ ", style=theme.success)
            results.append(f"{m.fail_count}", style=f"bold {theme.error}")
            results.append("✗", style=theme.error)
        else:
            results = Text("—", style=theme.text_ghost)

        # Updated
        updated = Text(_time_ago(m.updated_at), style=theme.text_ghost)

        table.add_row(
            name_text, env_text, status_text,
            pipe, results, updated,
        )

    total = len(all_sessions)

    title = Text()
    title.append(" SESSIONS ", style=f"bold {theme.bg_primary} on {theme.text_secondary}")
    if total > 5:
        title.append(f"   {total} total · 5 most recent", style=theme.text_ghost)

    return Panel(
        table,
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style=theme.border_dim,
        style=f"on {theme.tint_neutral}",
        padding=(0, 1),
        expand=True,
    )


# ══════════════════════════════════════════════════════════════════
# FOOTER
# ══════════════════════════════════════════════════════════════════

def _build_footer() -> Panel:
    """Quick-switch and help command lanes, separated by light dot bullets."""
    sep = f"  [{theme.text_ghost}]·[/{theme.text_ghost}]  "

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

    body = (
        f"[{theme.text_ghost}]quick[/{theme.text_ghost}]   {switch_cmds}\n"
        f"[{theme.text_ghost}]help[/{theme.text_ghost}]    {help_cmds}"
    )

    return Panel(
        body,
        box=box.SIMPLE,
        border_style=theme.border_ghost,
        style=f"on {theme.tint_neutral}",
        padding=(0, 2),
        expand=True,
    )


# ══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def show_dashboard():
    """Show Atlas info dashboard when no arguments provided."""
    console.clear()

    session_mgr = get_session_manager()

    # Banner
    console.print(_build_banner())

    # Active session hero (or getting-started panel)
    try:
        active_session = session_mgr.get_active()
    except NoActiveSessionError:
        active_session = None

    if active_session:
        console.print(_build_hero(active_session))
        warning_panel = _build_warnings(active_session)
        if warning_panel is not None:
            console.print(warning_panel)
    else:
        all_sessions_for_gs = session_mgr.list()
        console.print(_build_getting_started(has_sessions=bool(all_sessions_for_gs)))

    # Recent sessions table
    all_sessions = session_mgr.list()
    if all_sessions:
        active_name = session_mgr.get_active_session_name()
        console.print(_build_sessions_panel(all_sessions, active_name))

    # Ruleset update notice (shown if user previously declined an available update)
    update_notice = _build_ruleset_update_notice()
    if update_notice is not None:
        console.print(update_notice)

    # Footer
    console.print(_build_footer())
    console.print()
