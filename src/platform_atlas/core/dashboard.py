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
from rich.align import Align
from rich.console import Console, Group
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
    from platform_atlas.core.ui import glyph
    return glyph("node_done") if done else glyph("node_pending")


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
        "created":    ("Run data capture",         "platform-atlas session run capture"),
        "capturing":  ("Resume capture",           "platform-atlas session run capture"),
        "captured":   ("Run validation",           "platform-atlas session run validate"),
        "validating": ("Resume validation",        "platform-atlas session run validate"),
        "validated":  ("Generate report",          "platform-atlas session run report"),
        "reported":   ("View report or export",    f"platform-atlas session show {meta.name}"),
        "failed":     ("Review errors",            f"platform-atlas session show {meta.name}"),
    }
    return next_map.get(status, ("Continue", "platform-atlas session --help"))


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
    tint_map = {"high": "#C5258F", "medium": "#FDD058", "low": "#99CA3C"}
    banner_border = tint_map.get(_env_tint or "", theme.banner_rule)

    parts: list[str] = []
    if tier_name:
        tier_color = {
            "standard": theme.tier_standard,
            "saas": theme.tier_saas,
        }.get(tier_name, theme.tier_extended)
        # Proper brand casing — "SaaS", not .capitalize()'s "Saas" — and matches
        # the Mode label used by the pipeline tracker below.
        tier_label = {
            "standard": "Standard",
            "saas": "SaaS",
        }.get(tier_name, "Extended")
        parts.append(
            f"[{tier_color}]{tier_label}[/{tier_color}] "
            f"[{theme.text_ghost}]mode[/{theme.text_ghost}]"
        )
    if env_name:
        parts.append(f"[{theme.primary}]{env_name}[/{theme.primary}] [{theme.text_ghost}]env[/{theme.text_ghost}]")
    else:
        parts.append(f"[{theme.text_ghost}]no environment[/{theme.text_ghost}]")

    # Loud (but fully guarded) indicator when the encrypted local file credential
    # store is active — so a support engineer sees at a glance that the keyring
    # fallback engaged. active_secret_store() never connects to Vault, so this is
    # safe to call during banner render.
    try:
        from platform_atlas.core.credentials import active_secret_store as _ass
        if _ass().is_file:
            parts.append(f"[#FF6633]local file store[/#FF6633] [{theme.text_ghost}]creds[/{theme.text_ghost}]")
    except Exception:
        pass
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
# ACTIVE SESSION — PIPELINE TRACKER
# ══════════════════════════════════════════════════════════════════
#
# The active session renders as a horizontal Capture → Validate → Report
# tracker: three stage cards joined by connectors that light green only when
# both adjacent stages are complete, so a half-run pipeline reads at a glance.
# Everything below is theme-driven (theme.* resolves to the active preset) and
# guarded for sparse / mid-pipeline / failed sessions.

# done / current / pending / error  →  theme attribute names (never hardcoded
# hex, so a theme switch re-skins the tracker for free).
_STATE_COLOR = {
    "done":    "success",
    "current": "primary",
    "pending": "text_ghost",
    "error":   "error",
}
_STATE_TINT = {
    "done":    "tint_success",
    "current": "tint_primary",
    "pending": "tint_neutral",
    "error":   "tint_error",
}
_ERROR_STATUSES = {"failed", "aborted"}
_STAGE_TITLES = ("CAPTURE", "VALIDATE", "REPORT")


def _state_color(state: str) -> str:
    return getattr(theme, _STATE_COLOR.get(state, "text_ghost"))


def _state_tint(state: str) -> str:
    return getattr(theme, _STATE_TINT.get(state, "tint_neutral"))


def _stage_states(meta) -> list[str]:
    """Resolve each pipeline stage to done / current / pending / error.

    The first not-yet-complete stage is "current" — or "error" when the session
    is failed/aborted (that's where it stopped). Stages after it are "pending".
    Works for every status, including a fully-reported session (all "done") and
    a brand-new "created" one (capture "current", the rest "pending").
    """
    done = [
        bool(getattr(meta, "capture_completed", False)),
        bool(getattr(meta, "validation_completed", False)),
        bool(getattr(meta, "report_completed", False)),
    ]
    status = str(meta.status)
    states: list[str] = []
    marked_active = False
    for is_done in done:
        if is_done:
            states.append("done")
        elif not marked_active:
            states.append("error" if status in _ERROR_STATUSES else "current")
            marked_active = True
        else:
            states.append("pending")
    return states


def _node_glyph(state: str) -> str:
    """Pipeline-node glyph from the canonical ``ui`` registry (ASCII fallbacks
    handled there under NO_COLOR / compatibility mode)."""
    from platform_atlas.core.ui import glyph
    return glyph({
        "error": "error",
        "pending": "node_pending",
        "current": "node_current",
    }.get(state, "node_done"))


def _compliance_rate(meta) -> tuple[float | None, str]:
    """(rate, color) for a validated session, or (None, ghost) when there's
    nothing to rate yet. Guards the divide-by-zero when every rule skipped."""
    evaluated = meta.pass_count + meta.fail_count
    if not (meta.validation_completed and evaluated > 0):
        return None, theme.text_ghost
    rate = round(meta.pass_count / evaluated * 100, 1)
    color = (
        theme.success if rate >= 90
        else theme.warning if rate >= 70
        else theme.error
    )
    return rate, color


def _stage_lines(meta, index: int, state: str) -> list[str]:
    """The 1–3 sub-stat lines under a stage node. Every value comes from real
    session metadata and is guarded for the not-run / empty / partial cases."""
    status = str(meta.status)
    if index == 0:  # Capture
        if state == "done":
            mods = list(getattr(meta, "modules_ran", None) or [])
            head = f"{len(mods)} module{'s' if len(mods) != 1 else ''}" if mods else "captured"
            return [head, "complete"]
        if state == "error":
            return ["did not finish"]
        if state == "current":
            return ["in progress" if status == "capturing" else "ready to run"]
        return ["pending"]
    if index == 1:  # Validate
        if state == "done":
            total = meta.total_rules or 0
            if total > 0:
                return [
                    f"{meta.pass_count} pass · {meta.fail_count} fail",
                    f"{meta.skip_count} skipped",
                    f"of {total} rules",
                ]
            return ["validated", "complete"]
        if state == "error":
            return ["did not finish"]
        if state == "current":
            return ["in progress" if status == "validating" else "ready to run"]
        return ["pending"]
    # index == 2: Report
    if state == "done":
        lines = ["report.html"]
        rate, _ = _compliance_rate(meta)
        if rate is not None:
            lines.append(f"{rate:.1f}% compliant")
        lines.append("ready to export")
        return lines
    if state == "error":
        return ["did not finish"]
    if state == "current":
        return ["ready to run"]
    return ["pending"]


def _stage_node(index: int, state: str, lines: list[str]) -> Panel:
    """One stage of the pipeline tracker, rendered as a small centered card.

    `lines` is pre-computed and pre-padded by the caller so all three cards
    share a height and the row reads as a clean rectangle.
    """
    color = _state_color(state)
    body = Text(justify="center")
    body.append(_node_glyph(state), style=f"bold {color}")
    body.append("\n")
    body.append(_STAGE_TITLES[index], style=f"bold {color}")
    for line in lines:
        body.append("\n")
        body.append(line, style=theme.text_dim)
    return Panel(
        body,
        box=box.ROUNDED,
        border_style=color,
        style=f"on {_state_tint(state)}",
        padding=(1, 1),
        expand=True,
    )


def _connector(linked: bool) -> Align:
    """Arrow between two stage cards, vertically centered to sit level with the
    glyphs. Green only when both adjacent stages are complete."""
    from platform_atlas.core.ui import is_plain_mode
    arrow = "-->" if is_plain_mode() else "━━▶"
    color = theme.success if linked else theme.text_ghost
    return Align.center(Text(arrow, style=f"bold {color}"), vertical="middle")


def _build_pipeline_tracker(active_session) -> Panel:
    """The active session as a horizontal Capture → Validate → Report tracker,
    with a context line above and the next actionable command below."""
    meta = active_session.metadata
    sc = _sc(str(meta.status))
    states = _stage_states(meta)

    # Context line — org · mode · env · ruleset (+ profile). Each part is added
    # only when present, so a freshly-created session with nothing bound yet
    # still renders cleanly (it falls back to just the Mode chip).
    session_tier = (getattr(meta, "tier", None) or "extended").lower()
    tier_label, tier_color = {
        "standard": ("Standard", theme.tier_standard),
        "saas": ("SaaS", theme.tier_saas),
    }.get(session_tier, ("Extended", theme.tier_extended))

    parts: list[Text] = []
    if meta.organization_name:
        parts.append(Text(meta.organization_name, style="bold"))
    parts.append(Text(tier_label, style=f"bold {tier_color}"))
    if meta.environment:
        parts.append(Text(meta.environment, style=theme.primary))
    if meta.ruleset_id:
        rs = Text()
        rs.append(meta.ruleset_id, style=theme.secondary)
        if meta.ruleset_profile:
            rs.append("  +  ", style=theme.text_ghost)
            rs.append(meta.ruleset_profile, style=theme.warning)
        parts.append(rs)

    context_line = Text()
    for i, part in enumerate(parts):
        if i:
            context_line.append("  ·  ", style=theme.text_ghost)
        context_line.append(part)

    # Pre-compute each node's sub-lines and pad them all to the tallest, so the
    # three cards share a height and the row reads as a clean rectangle.
    line_lists = [_stage_lines(meta, i, states[i]) for i in range(3)]
    height = max(len(lst) for lst in line_lists)
    line_lists = [lst + [""] * (height - len(lst)) for lst in line_lists]

    # The track: node ━▶ node ━▶ node. Nodes get the room (ratio 4), connectors
    # a thin lane (ratio 1). Links mirror the chain semantics.
    track = Table.grid(expand=True)
    for ratio in (4, 1, 4, 1, 4):
        track.add_column(ratio=ratio)
    track.add_row(
        _stage_node(0, states[0], line_lists[0]),
        _connector(meta.capture_completed and meta.validation_completed),
        _stage_node(1, states[1], line_lists[1]),
        _connector(meta.validation_completed and meta.report_completed),
        _stage_node(2, states[2], line_lists[2]),
    )

    # Next step — the single command to run next.
    label, cmd = _next_step(meta)
    next_block = Text()
    next_block.append("→ ", style=f"bold {theme.accent}")
    next_block.append(label, style=theme.text_primary)
    next_block.append("    ")
    next_block.append("▎ ", style=f"bold {theme.accent}")
    next_block.append(f"$ {cmd}", style=f"bold {theme.primary}")

    title = Text()
    title.append(" ACTIVE SESSION ", style=f"bold {theme.bg_primary} on {theme.primary}")
    title.append("  ")
    title.append(meta.name, style=f"bold {theme.text_primary}")
    title.append("  ")
    title.append(f" {meta.status} ", style=f"bold {theme.bg_primary} on {sc}")

    return Panel(
        Group(context_line, Text(""), track, Text(""), next_block),
        title=title,
        title_align="left",
        border_style=theme.primary,
        # Heavy border marks the primary hero — a deliberate second weight
        # tier (shared with the identity banner) above the ROUNDED secondaries.
        box=box.HEAVY,
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
        # Primary hero (no active session) — heavy border, same tier as the
        # active-session tracker and the identity banner.
        box=box.HEAVY,
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
# RECENT ACTIVITY FEED
# ══════════════════════════════════════════════════════════════════

def _trunc(value: str, width: int) -> str:
    """Truncate to `width` cells with an ellipsis so the feed's columns stay
    aligned even when a session name or environment is very long."""
    s = str(value)
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def _build_activity_feed(all_sessions, active_name: str | None) -> Panel:
    """The 5 most-recent sessions as a vertical timeline — a status dot per
    session on a connecting rail, with a pass/fail sub-line once validated.

    Reads as "what's been happening" rather than a flat table, reinforcing the
    pipeline metaphor. Fully guarded: long names truncate, a missing
    environment shows a dash, an unparseable timestamp degrades to blank, and
    an empty list prints a friendly placeholder.
    """
    from platform_atlas.core.ui import is_plain_mode
    plain = is_plain_mode()

    recent = sorted(
        all_sessions,
        key=lambda s: s.metadata.updated_at,
        reverse=True,
    )[:5]

    feed = Text()
    count = len(recent)
    rail_char = "|" if plain else "│"

    for i, sess in enumerate(recent):
        m = sess.metadata
        status = str(m.status)
        color = _sc(status)
        is_active = sess.name == active_name
        validated = bool(m.validation_completed)

        from platform_atlas.core.ui import glyph
        dot = glyph("active") if validated else glyph("pending")
        feed.append(f"  {dot}  ", style=f"bold {color}")
        feed.append(
            _trunc(m.name, 26).ljust(27),
            style=f"bold {theme.accent}" if is_active else f"bold {theme.text_primary}",
        )
        feed.append(_trunc(status, 11).ljust(12), style=color)
        if m.environment:
            feed.append(_trunc(m.environment, 14).ljust(15), style=theme.primary)
        else:
            feed.append("—".ljust(15), style=theme.text_ghost)
        try:
            ago = _time_ago(m.updated_at)
        except Exception:
            ago = ""
        feed.append(ago, style=theme.text_ghost)
        feed.append("\n")

        # Rail down to the next dot, plus the result sub-line once validated.
        is_last = i == count - 1
        rail = "     " if is_last else f"  {rail_char}  "
        if validated:
            feed.append(rail, style=theme.border_dim)
            feed.append(f"{m.pass_count}", style=theme.success)
            feed.append(" pass · ", style=theme.text_dim)
            feed.append(f"{m.fail_count}", style=theme.error)
            feed.append(" fail", style=theme.text_dim)
            feed.append("\n")
        elif not is_last:
            feed.append(rail + "\n", style=theme.border_dim)

    if count == 0:
        feed.append("  No sessions yet — create one to get started.", style=theme.text_dim)

    total = len(all_sessions)
    title = Text()
    title.append(" RECENT ACTIVITY ", style=f"bold {theme.bg_primary} on {theme.text_secondary}")
    if total > 5:
        title.append(f"   {total} total · 5 most recent", style=theme.text_ghost)

    return Panel(
        feed,
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style=theme.border_dim,
        style=f"on {theme.tint_neutral}",
        padding=(1, 1),
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
        console.print(_build_pipeline_tracker(active_session))
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
        console.print(_build_activity_feed(all_sessions, active_name))

    # Ruleset update notice (shown if user previously declined an available update)
    update_notice = _build_ruleset_update_notice()
    if update_notice is not None:
        console.print(update_notice)

    # Footer
    console.print(_build_footer())
    console.print()
