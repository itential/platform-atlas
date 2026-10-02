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
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from platform_atlas.core._version import __version__, __build__
from platform_atlas.core import ui
from platform_atlas.core.context import ctx
from platform_atlas.core.environment import get_environment_manager, Environment, ENVIRONMENT_TYPE_LABELS
from platform_atlas.core.exceptions import ConfigError
from platform_atlas.core.paths import ATLAS_RULESET_UPDATE_STATE
from platform_atlas.core.session_manager import get_session_manager, NoActiveSessionError
from platform_atlas.core.ruleset_manager import get_ruleset_manager
from platform_atlas.core.topology import role_display_label

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


# Environment tint → hex, shared by the banner border and the environment
# card's tint row. Brand-fixed (not theme.*) so it reads consistently as a
# risk cue regardless of the active theme.
TINT_COLOR_MAP = {"high": "#C5258F", "medium": "#FDD058", "low": "#99CA3C"}

# Tier label + theme color, shared by the banner, the environment card, and
# the organization card. Standard/SaaS are branded; anything else (including
# a missing/unrecognized value) reads as Extended.
_TIER_LABEL_COLOR = {
    "standard": ("Standard", "tier_standard"),
    "saas": ("SaaS", "tier_saas"),
}


def _tier_label_color(tier: str | None) -> tuple[str, str]:
    label, attr = _TIER_LABEL_COLOR.get((tier or "").lower(), ("Extended", "tier_extended"))
    return label, getattr(theme, attr)


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
        "created":    ("Run the full audit",       "platform-atlas session run all"),
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

    # Resolve environment_type for the banner border
    _env_obj = _load_env_safe(env_name) if active_ctx else None
    _env_tint = _env_obj.environment_type if _env_obj else None
    banner_border = TINT_COLOR_MAP.get(_env_tint or "", theme.banner_rule)

    parts: list[str] = []
    if tier_name:
        tier_label, tier_color = _tier_label_color(tier_name)
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
# ACTIVE SESSION — TRIPTYCH (center CTA card + ENVIRONMENT / ORGANIZATION)
# ══════════════════════════════════════════════════════════════════
#
# The active session renders as three cards: a fixed-width CENTER card
# (identity, one CTA, 3 rule-outcome stats, recent sessions, shortcuts)
# flanked by two same-width companion cards — ENVIRONMENT (deployment,
# topology, credentials, this session's capture detail) and ORGANIZATION
# (sessions, environments, ruleset, installed build). Every field traces to
# a real data source (Environment/Config/SessionMetadata/RulesetManager) —
# nothing here is decorative filler.
#
# The side cards are padded with blank lines to exactly MATCH the center
# card's rendered height (measured, not guessed) — real content varies by
# tier and by session (Standard has no topology, SaaS has no SSH key, a
# session with no ruleset bound yet has no RULESET rows, etc.), so a fixed
# row count can't guarantee parity the way it could for one fixed mock
# session. Below the width the triptych needs, only the center card shows
# (centered) — the side cards are a bonus, never a requirement to read the
# dashboard.

SIDE_CARD_WIDTH = 36
CENTER_CARD_WIDTH = 78
_TRIPTYCH_MIN_WIDTH = SIDE_CARD_WIDTH * 2 + CENTER_CARD_WIDTH + 8


def _load_env_safe(env_name: str | None) -> Environment | None:
    """Load an Environment by name, or None on any failure — mirrors the
    guarded exists()-then-load() pattern used throughout this module. Every
    caller treats the result as optional/cosmetic, never worth crashing the
    dashboard over."""
    if not env_name:
        return None
    try:
        mgr = get_environment_manager()
        if mgr.exists(env_name):
            return mgr.load(env_name)
    except Exception:
        pass
    return None


def _measure_height(renderable, width: int) -> int:
    """Rendered line count of `renderable` at a given width — used to pad
    the side cards to match the center card's real height without
    printing anything."""
    return len(console.render_lines(renderable, console.options.update(width=width)))


def _label_value_row(label: str, value, color: str | None = None) -> Table:
    """A quiet two-column row: label left (its own natural width, dim),
    value right (flexible, bold). The label column has no ratio, so it
    never shrinks below its own text — real values (a long ruleset id, an
    environment name) crop against the flexible value column instead, so a
    long real value can never smear into the label with no gap between
    them the way a ratio=1 label column allowed."""
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column()
    row.add_column(ratio=1, justify="right")
    row.add_row(
        Text(str(label), style=theme.text_dim, no_wrap=True, overflow="crop"),
        Text(str(value), style=f"bold {color or theme.text_primary}", no_wrap=True, overflow="crop"),
    )
    return row


def _stat_chip(label: str, value, color: str) -> Panel:
    body = Text(justify="center")
    body.append(f"{value}\n", style=f"bold {color}")
    # text_dim (not text_ghost) — ghost is too low-contrast against a dark
    # theme's tinted chip background to read as a real label.
    body.append(label.upper(), style=f"bold {theme.text_dim}")
    return Panel(body, box=box.ROUNDED, border_style=color, style=f"on {theme.tint_neutral}", padding=(0, 1))


def _assemble_side_sections(sections: list[tuple[str | None, list]]) -> list:
    """Flatten (label, rows) section pairs into one renderable list — a rule
    + small header divides sections after the first. A section with no rows
    (e.g. TOPOLOGY on a Standard-tier session) is skipped entirely rather
    than showing an empty header."""
    parts: list = []
    for label, rows in sections:
        if not rows:
            continue
        if parts:
            parts.append(Text(""))
            parts.append(Rule(style=theme.border_ghost))
        if label:
            # text_secondary, not text_ghost — ghost reads as barely-visible
            # against a dark theme. Secondary sits one tier below the bold
            # text_primary values in each row, so headers stay clearly
            # readable without outshining the actual data.
            parts.append(Text(label, style=f"bold {theme.text_secondary}"))
        parts.extend(rows)
    return parts


def _side_card(title: str, color: str, parts: list) -> Panel:
    title_text = Text(f" {title} ", style=f"bold {theme.bg_primary} on {color}")
    return Panel(
        Group(*parts) if parts else Text(""),
        title=title_text,
        title_align="left",
        box=box.ROUNDED,
        border_style=color,
        style=f"on {theme.tint_neutral}",
        padding=(1, 2),
        width=SIDE_CARD_WIDTH,
    )


def _compliance_headline(meta) -> Text:
    """The one number that ties COMPLIANT/NON-COMPLIANT/SKIPPED together —
    shown nowhere else in the hero now that the stat chips are raw counts.
    Severity-weighted, matching the HTML report and CLI score panel — a
    critical failure pulls this down more than a warning or info one does.
    Sessions validated before weighted_score existed fall back to the plain
    pass rate rather than misreporting 0% compliant."""
    evaluated = meta.pass_count + meta.fail_count
    if evaluated == 0:
        return Text("no rules evaluated yet", style=theme.text_ghost)
    if meta.weighted_score is None:
        rate = round(meta.pass_count / evaluated * 100, 1)
    else:
        rate = meta.weighted_score
    color = theme.success if rate >= 90 else theme.warning if rate >= 70 else theme.error
    return Text(f"{round(rate)}% compliant", style=f"bold {color}")


def _build_center_body(active_session) -> Group:
    """The center card's content — a headline, one CTA, 3 rule-outcome
    stats, recent sessions, shortcuts. Fixed shape across every tier. The
    "PLATFORM ATLAS vX" brand mark lives in the panel's own title now (see
    _build_hero), not in this body."""
    from platform_atlas.core.ui import glyph

    meta = active_session.metadata
    label, cmd = _next_step(meta)

    cta = Group(
        Align.center(Text(label.upper(), style=f"bold {theme.text_secondary}")),
        Align.center(Text(f"$ {cmd}", style=f"bold {theme.primary}")),
    )

    stats = Table.grid(expand=True, padding=(0, 1))
    for _ in range(3):
        stats.add_column(ratio=1)
    stats.add_row(
        _stat_chip("COMPLIANT", meta.pass_count, theme.success),
        _stat_chip("NON-COMPLIANT", meta.fail_count, theme.error),
        _stat_chip("SKIPPED", meta.skip_count, theme.text_secondary),
    )

    recent = sorted(get_session_manager().list(), key=lambda s: s.metadata.updated_at, reverse=True)[:4]
    recent_rows: list = []
    for s in recent:
        m = s.metadata
        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column(justify="right")
        name_style = f"bold {theme.accent}" if m.name == meta.name else theme.text_secondary
        dot = glyph("active" if m.validation_completed else "pending")
        row.add_row(
            Text(f"{dot} {m.name}", style=name_style, no_wrap=True, overflow="ellipsis"),
            Text(str(m.status), style=_sc(str(m.status))),
        )
        recent_rows.append(row)
    if not recent_rows:
        recent_rows = [Text("No sessions yet.", style=theme.text_dim)]

    shortcut_rows: list = [
        Text(lbl, style=theme.text_dim)
        for lbl in ("session switch", "session create", "preflight", "guide")
    ]

    return Group(
        Align.center(_compliance_headline(meta)),
        Text(""),
        Rule(style=theme.border_ghost),
        Text(""),
        cta,
        Text(""),
        stats,
        Text(""),
        Rule(style=theme.border_ghost),
        Text("▸ RECENT", style=f"bold {theme.text_secondary}"),
        *recent_rows,
        Text(""),
        Text("▸ SHORTCUTS", style=f"bold {theme.text_secondary}"),
        *shortcut_rows,
    )


def _build_environment_sections(active_session) -> list[tuple[str | None, list]]:
    """Everything real about the bound environment: deployment/gateway/tint,
    topology (guarded — Standard/SaaS frequently have none), credentials,
    and this session's own capture detail."""
    meta = active_session.metadata
    active_ctx = _ctx_safe()
    config = active_ctx.config if active_ctx else None
    # The environment the next capture will actually run against (--env / ATLAS_ENV /
    # config.json), not the session's bound one — the topology/gateway rows below come
    # from that same config overlay. Session-vs-active drift is flagged in _build_warnings.
    env_name = config.active_environment if config is not None else None
    env = _load_env_safe(env_name)

    primary_rows: list = [_label_value_row("name", env_name or "—")]
    if config is not None:
        try:
            primary_rows.append(_label_value_row("deployment", config.topology.mode.value))
        except ConfigError:
            pass
        gateway = (config.saas_gateway_kind if config.tier == "saas" else config.gateway_kind) or None
        if gateway:
            primary_rows.append(_label_value_row("gateway", gateway))
    if env is not None and env.environment_type:
        primary_rows.append(
            _label_value_row(
                "type",
                ENVIRONMENT_TYPE_LABELS.get(env.environment_type, env.environment_type),
                TINT_COLOR_MAP.get(env.environment_type, theme.text_primary),
            )
        )

    topology_rows: list = []
    if config is not None:
        try:
            targets = config.all_targets
        except Exception:
            targets = ()
        role_counts: dict[str, int] = {}
        for t in targets:
            role = t.get("role")
            if role:
                role_counts[role] = role_counts.get(role, 0) + 1
        for role, count in role_counts.items():
            topology_rows.append(
                _label_value_row(role_display_label(role), f"{count} node" + ("" if count == 1 else "s"))
            )
        if len(role_counts) > 1:
            topology_rows.append(_label_value_row("total", f"{sum(role_counts.values())} nodes"))

    cred_rows: list = []
    if config is not None:
        backend_map = {"keyring": "OS Keyring", "vault": "Vault", "file": "Encrypted File"}
        backend = config.credential_backend or "keyring"
        cred_rows.append(_label_value_row(
            "backend", backend_map.get(backend, backend),
            theme.warning if backend == "file" else None,
        ))
    if env is not None and env.ssh_key:
        cred_rows.append(_label_value_row("ssh key", env.ssh_key))

    scope_map = {"primary_only": "primary only", "all_nodes": "all nodes"}
    scope = scope_map.get(config.capture_scope, config.capture_scope) if config is not None else "—"
    tier_label, tier_color = _tier_label_color(meta.tier)
    capture_rows = [
        _label_value_row("last capture", _time_ago(meta.updated_at)),
        _label_value_row("modules run", len(meta.modules_ran or [])),
        _label_value_row("scope", scope),
        _label_value_row("status", meta.status, _sc(str(meta.status))),
        _label_value_row("tier", tier_label, tier_color),
    ]

    return [
        (None, primary_rows),
        ("TOPOLOGY", topology_rows),
        ("CREDENTIALS", cred_rows),
        ("CAPTURE", capture_rows),
    ]


def _build_organization_sections(active_session) -> list[tuple[str | None, list]]:
    """Aggregate org-level facts — total sessions/environments, the active
    ruleset, and the installed Atlas build. Deliberately no licensing or
    contact fields: nothing like that exists anywhere in this codebase."""
    meta = active_session.metadata
    active_ctx = _ctx_safe()
    config = active_ctx.config if active_ctx else None
    tier_label, tier_color = _tier_label_color(meta.tier)

    identity_rows: list = [
        Align.center(Text(meta.organization_name or "Platform Atlas", style=f"bold {theme.text_primary}")),
        Align.center(Text(f"{tier_label} tier", style=f"bold {tier_color}")),
    ]

    try:
        total_sessions = len(get_session_manager().list())
    except Exception:
        total_sessions = None
    session_rows: list = []
    if total_sessions is not None:
        session_rows.append(_label_value_row("total sessions", total_sessions))
    session_rows.append(_label_value_row("active", meta.name))

    try:
        total_envs = len(get_environment_manager().list_names())
    except Exception:
        total_envs = None
    env_rows: list = []
    if total_envs is not None:
        env_rows.append(_label_value_row("total environments", total_envs))
    if config is not None:
        env_rows.append(_label_value_row("active environment", config.active_environment or "—"))
        default_label, default_color = _tier_label_color(config.tier)
        env_rows.append(_label_value_row("default tier", default_label, default_color))

    # profile used to show only in the center card's context line — now that
    # that line is gone (replaced by the compliance headline), this is the
    # only place it's shown.
    ruleset_rows: list = []
    if meta.ruleset_id:
        ruleset_rows.append(_label_value_row("active ruleset", meta.ruleset_id))
    if meta.ruleset_profile:
        ruleset_rows.append(_label_value_row("profile", meta.ruleset_profile, theme.warning))
    if meta.ruleset_version:
        ruleset_rows.append(_label_value_row("version", meta.ruleset_version))
    if meta.total_rules:
        ruleset_rows.append(_label_value_row("rules", meta.total_rules))

    system_rows = [
        _label_value_row("atlas version", __version__),
        _label_value_row("build", __build__),
        _label_value_row("theme", config.theme if config is not None else "—"),
    ]

    return [
        (None, identity_rows),
        ("SESSIONS", session_rows),
        ("ENVIRONMENTS", env_rows),
        ("RULESET", ruleset_rows),
        ("SYSTEM", system_rows),
    ]


def _build_hero(active_session) -> tuple[Align, int]:
    """The active-session hero: an ENVIRONMENT / center / ORGANIZATION
    triptych on a wide terminal, or just the center card (centered) when
    there isn't room for the side cards. Returns (renderable, content_width)
    so callers — the binding-drift warning, in particular — can size
    themselves to match instead of stretching edge to edge."""
    center_body = _build_center_body(active_session)
    center_inner_width = CENTER_CARD_WIDTH - 2 - 6  # border(2) + padding(1,3) horizontal(6)
    # Same title-chip treatment as ENVIRONMENT/ORGANIZATION (so the brand
    # mark stands out in the border instead of sitting in the body), but
    # centered — the hero card's title is the one thing that should read as
    # different from its two neutral companions either side of it.
    center_title = Text(f"Platform Atlas {__version__}", style=f"bold {theme.bg_primary} on {theme.primary}")

    if console.width < _TRIPTYCH_MIN_WIDTH:
        center_panel = Panel(
            center_body, title=center_title, title_align="center", box=box.ROUNDED,
            border_style=theme.primary, style=f"on {theme.bg_secondary}", padding=(1, 3),
            width=CENTER_CARD_WIDTH,
        )
        return Align.center(center_panel), CENTER_CARD_WIDTH

    side_inner_width = SIDE_CARD_WIDTH - 2 - 4  # border(2) + padding(1,2) horizontal(4)

    env_parts = _assemble_side_sections(_build_environment_sections(active_session))
    org_parts = _assemble_side_sections(_build_organization_sections(active_session))
    center_h = _measure_height(center_body, center_inner_width)
    env_h = _measure_height(Group(*env_parts) if env_parts else Text(""), side_inner_width)
    org_h = _measure_height(Group(*org_parts) if org_parts else Text(""), side_inner_width)

    # Whichever of the three ends up tallest sets the target — content
    # varies by tier/session on every card, including the center one, so
    # nobody gets to assume they're always the tallest.
    target_height = max(center_h, env_h, org_h)
    if center_h < target_height:
        center_body = Group(center_body, *([Text("")] * (target_height - center_h)))
    for parts, current in ((env_parts, env_h), (org_parts, org_h)):
        if current < target_height:
            parts.extend([Text("")] * (target_height - current))

    center_panel = Panel(
        center_body, title=center_title, title_align="center", box=box.ROUNDED,
        border_style=theme.primary, style=f"on {theme.bg_secondary}", padding=(1, 3),
        width=CENTER_CARD_WIDTH,
    )
    left = _side_card("ENVIRONMENT", theme.secondary, env_parts)
    right = _side_card("ORGANIZATION", theme.info, org_parts)

    row = Table.grid(padding=(0, 2))
    row.add_column()
    row.add_column()
    row.add_column()
    row.add_row(left, center_panel, right)
    return Align.center(row), console.measure(row).maximum


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


def _build_warnings(active_session, width: int) -> Align | None:
    warnings: list[str] = []

    # Runs before the capture-file check so a mid-capture session still gets flagged.
    bound_env = getattr(active_session.metadata, "environment", None)
    active_env = ctx().active_environment
    if bound_env and active_env and bound_env != active_env:
        warnings.append(
            f"  [{theme.warning}]⚠[/{theme.warning}]  Session is bound to environment "
            f"[bold]{bound_env}[/bold] but [{theme.accent}]{active_env}[/{theme.accent}] is active"
            f" — run [bold]session switch[/bold] or [bold]env switch {bound_env}[/bold]"
        )

    if not active_session.capture_file.exists():
        return _render_drift_panel(warnings, width)

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

    return _render_drift_panel(warnings, width)


def _render_drift_panel(warnings: list[str], width: int) -> Align | None:
    if not warnings:
        return None

    title = Text()
    title.append(" BINDING DRIFT ", style=f"bold {theme.bg_primary} on {theme.warning}")

    panel = Panel(
        "\n".join(warnings),
        title=title,
        title_align="left",
        border_style=theme.warning,
        box=box.ROUNDED,
        style=f"on {theme.tint_warning}",
        padding=(0, 1),
        width=width,
    )
    return Align.center(panel)


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
    console.print()

    session_mgr = get_session_manager()

    # Active session hero (a triptych — its center card carries its own
    # branding, so the standalone banner is skipped here), or the banner +
    # getting-started panel when there's nothing active yet.
    try:
        active_session = session_mgr.get_active()
    except NoActiveSessionError:
        active_session = None

    if active_session:
        # The triptych's center card already carries its own RECENT and
        # SHORTCUTS sections — the activity feed and footer below would
        # just repeat that same information, so both are skipped here.
        hero, hero_width = _build_hero(active_session)
        console.print(hero)
        warning_panel = _build_warnings(active_session, hero_width)
        if warning_panel is not None:
            console.print(warning_panel)
    else:
        console.print(_build_banner())
        all_sessions_for_gs = session_mgr.list()
        console.print(_build_getting_started(has_sessions=bool(all_sessions_for_gs)))

        # Recent sessions table — only shown here; the active-session hero
        # above already has its own compact recent-sessions section.
        all_sessions = session_mgr.list()
        if all_sessions:
            active_name = session_mgr.get_active_session_name()
            console.print(_build_activity_feed(all_sessions, active_name))

        # Footer — only shown here; the active-session hero above already
        # has its own SHORTCUTS section.
        console.print(_build_footer())

    # Ruleset update notice (shown if user previously declined an available
    # update) — real, actionable content, not a duplicate of anything in the
    # hero, so it's shown in both cases.
    update_notice = _build_ruleset_update_notice()
    if update_notice is not None:
        console.print(update_notice)

    console.print()
