"""
ATLAS // Pipeline Cards

Shared live display for the Capture / Validate / Report stages — three cards,
left to right, filling top-to-bottom as data streams in, one screen, no scroll.
Replaces the old per-stage UIs (``CaptureUI``'s "LIVE PREVIEW" JSON panel, the
flat "X/Y rules processed" counter, and Report's plain scrolling prints).

Design carried over from ``design/mockups/pipeline-cards-mockup.py`` (verified
there against several terminal sizes before this real implementation), revised
after the first real look at it:

  * CAPTURE always lists every module individually under its category header
    — no collapsing to a summary line. Errors show inline with their real
    message. ``_fit_rows`` is the only height guard (truncates with "+N more"
    on a genuinely short terminal), which is fine — a capture rarely runs
    more than ~20 modules.
  * VALIDATE deliberately avoids ✓/✗ per category: a rule FAILING is a
    compliance finding, not a bug, and a cross/check pair reads as "this is
    broken" — the wrong signal for an audit tool. Each category gets a small
    compliance meter (like the card's own overall one) plus its pass/warn/
    fail/skip counts and rate, all on one line — a done category never costs
    more than a single row, which is what makes room for what's below it.
    Additional Validation Checks run inside the same card, in their own
    "ADDITIONAL CHECKS" section, grouped by CheckGroup (``_group_avc``/
    ``_avc_group_rows``). Binary, not judged, same reasoning as rule
    categories above: a check either "ran" (PASS/WARN/INFO/FAIL — it
    executed; what it found is a report.html concern) or was "skipped"
    (deactivated, tier doesn't apply, required data missing — it never ran
    at all). A group is only expanded to per-check rows while something in
    it is actually in flight; a group nothing has touched yet collapses to
    "pending (N)" and a fully finished one collapses to "N ran" — so the
    section's steady-state height tracks the number of GROUPS (a handful),
    not the number of checks (19 today, maybe 50+ later), with no arbitrary
    threshold. Used to print as a plain scrolling list after the card closed
    — and any caller that re-runs checks without wiring
    ``on_check_start``/``on_check_done`` (see ``run_extended_validation``)
    still falls back to that raw print, which is exactly why the report-side
    re-run callers pass ``headless=True``: that print path must never fire
    outside this card. MongoDB Operational Pipelines (a static recap of a
    prior capture-time run, not something live-checking during Validate)
    shows above AVC only until AVC starts — once AVC needs the room,
    Pipelines disappears for the rest of the card's life (its results still
    land in report.html regardless).
  * REPORT shows the 4 steps that genuinely exist today (build the
    viewmodel, render report.html, write the WebUI viewmodel, tally the
    Audit Score) — no fabricated per-page progress.
  * ``_fit_rows`` measures each row's REAL rendered line count (Rich's
    fixed-height ``Panel`` clips overflow silently rather than growing) and
    the Report card's finished payoff (score/path/browser line) is reserved
    space before the step list, so it's never the part that gets clipped.
  * Cards are capped well short of the full terminal height (see
    ``_card_height``) — a card stretched to the very last row fights with
    whatever prints below it once the ``Live`` block closes (AVC used to do
    this badly), producing a visible "bounce" as the terminal scrolls.

Used in two shapes:
  * ``mode="solo"`` — a single centered card (standalone ``session run
    capture``/``validate``/``report``).
  * ``mode="all"`` — all three cards side by side (``session run all``);
    a phase that already finished renders in its collapsed "done" state
    alongside whichever phase is currently live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from platform_atlas.core import ui
from platform_atlas.capture.models import CaptureState, ModuleResult, ModuleStatus
from platform_atlas.capture.modules_registry import ModuleCategory, get_module_info
from platform_atlas.reporting.operational_engine import OperationalReport

theme = ui.theme
glyph = ui.glyph

SOLO_CARD_WIDTH = 96

# Real registered module keys that carry no MODULE_DEFINITIONS entry (they're
# assembled directly in modules_registry's builder functions, not declared
# alongside the rest). Extend here if a future collector adds another one.
_CATEGORY_OVERRIDES: dict[str, ModuleCategory] = {
    "gateway4_api": ModuleCategory.GATEWAY,
}
_LABEL_OVERRIDES: dict[str, str] = {
    "gateway4_api": "Gateway4 API",
}
# transport_type values capture_engine.py's protocol-verify step registers a
# conf module under when its primary protocol source came back empty and an
# SSH/K8s fallback filled in instead (see capture_engine.py's "VERIFY
# PROTOCOL-PRIMARY CONFIG DATA" section) — surfaced on that module's row.
_FALLBACK_TRANSPORT_LABELS: dict[str, str] = {
    "ssh/fallback": "SSH FALLBACK",
    "k8s/fallback": "K8S FALLBACK",
}

VALIDATE_CATEGORY_LABELS: dict[str, str] = {
    "platform": "Platform",
    "gateway4": "Gateway4",
    "gateway5": "Gateway5",
    "redis": "Redis",
    "mongo": "MongoDB",
    "kubernetes": "Kubernetes",
}

REPORT_STEP_LABELS = ("Building viewmodel", "Rendering report.html", "WebUI viewmodel", "Audit Score")


# ════════════════════════════════════════════════════════════════════════════════
#  DATA MODEL
# ════════════════════════════════════════════════════════════════════════════════
@dataclass
class ValidateCategoryState:
    """Live per-category tally for the Validate card — no analog in
    validation_engine.py before this, which only tracked a flat
    processed/pass/fail counter. Shared by both the main ruleset's rule
    categories and Additional Validation Checks' CheckGroups — ``warned``
    is always 0 for the former (rules are PASS/FAIL/SKIP only)."""
    key: str
    label: str
    total: int
    processed: int = 0
    passed: int = 0
    failed: int = 0
    warned: int = 0
    skipped: int = 0
    status: str = "pending"       # pending | active | done


@dataclass
class AvcCheckState:
    """Live state of one Additional Validation Check. Rendered per-check
    (like a Capture module) only while its CheckGroup is actively in
    flight — see ``_avc_group_rows`` — so watching a run doesn't mean
    staring at ~19 (or, later, 50+) rows at once. ``result_status`` still
    carries the real PASS/FAIL/WARN/INFO/SKIP verdict (needed for
    report.html and for telling a real SKIP apart from "ran"), but the
    card itself only ever displays the binary: ran vs skipped."""
    check_id: str
    name: str
    group: str                    # CheckGroup.value, for the section's mini-headers
    status: str = "pending"       # pending | active | done
    result_status: str = ""       # PASS | FAIL | WARN | INFO | SKIP, once done
    message: str = ""


@dataclass
class ReportStepState:
    """One of the 4 real Report steps (build viewmodel / render / write
    WebUI viewmodel / audit score)."""
    label: str
    status: str = "pending"       # pending | running | done
    detail: str = ""


@dataclass
class PipelineFrame:
    """Everything one render tick needs. Callers mutate this in place and
    re-render — nothing here owns the Rich ``Live`` context itself.

    Each phase (capture/validate/report) gets its own short-lived
    ``PipelineFrame`` and its own ``Live`` segment — see the module
    docstring — so ``phase`` never itself progresses through all three
    stages within one frame's lifetime; it just names which phase THIS
    frame is live for. ``done`` is the flag that phase's own caller flips
    to True on its final render, which is what actually reveals a Report
    card's finished-state payoff (or a solo card's completed border) —
    without it every card would look "active" forever."""
    mode: str                      # "solo" | "all"
    phase: str                     # "capture" | "validate" | "report"
    done: bool = False             # True once THIS frame's own phase has finished
    tier: str = ""
    env_name: str = ""
    start_time: float = field(default_factory=time)
    capture: CaptureState | None = None
    validate: dict[str, ValidateCategoryState] | None = None
    avc: list[AvcCheckState] | None = None                # Additional Validation Checks, one row each
    pipelines: OperationalReport | None = None            # MongoDB operational pipelines, if the user ran any
    report_steps: list[ReportStepState] | None = None
    report_score: dict[str, Any] | None = None   # {passed, failed, skipped, pct}
    report_path: str = ""
    report_notes: list[str] = field(default_factory=list)
    report_opened_browser: bool | None = None


def new_report_steps() -> list[ReportStepState]:
    """A fresh, all-pending step list for a Report card."""
    return [ReportStepState(label=lbl) for lbl in REPORT_STEP_LABELS]


# ════════════════════════════════════════════════════════════════════════════════
#  RECONSTRUCTION — turn already-persisted data back into the same types the
#  live path uses, so a finished phase renders through the exact same card
#  code as a live one (mode="all" showing an earlier phase's final state).
# ════════════════════════════════════════════════════════════════════════════════
# Top-level capture JSON keys that are genuine capture *sections*
# (CAPTURE_STRUCTURE's destination roots) — as opposed to sections written
# by the extended/manual captures (e.g. "checks", "adapters",
# "applications") that aren't module-shaped and would show up as
# nonsensical fake "modules" in the reconstruction below.
_KNOWN_CAPTURE_SECTIONS = frozenset({
    "system", "mongo", "redis", "authorization", "platform",
    "gateway4", "gateway5", "kubernetes",
})


def capture_state_from_json(captured_data: dict[str, Any]) -> CaptureState:
    """Rebuild a display-only ``CaptureState`` from a capture JSON document,
    for showing a finished Capture card alongside a later live phase (mode
    "all") — Cody wants that card to stay exactly as detailed as it was
    while capturing, not collapse to a summary once the phase ends.

    Prefers ``_atlas.metadata.module_manifest`` (every module's real name/
    status/duration/error/transport, persisted at capture time — see
    ``CaptureState.module_manifest``), which reproduces the live card
    exactly. Falls back to a coarser section-level reconstruction for older
    capture files with no manifest, restricted to ``_KNOWN_CAPTURE_SECTIONS``
    so extended/manual capture data (e.g. "checks", "adapters",
    "applications") doesn't masquerade as capture modules — those don't
    carry real per-module names, so this path shows sections, not modules,
    with durations left at 0."""
    state = CaptureState()
    atlas_meta = (captured_data.get("_atlas") or {}).get("metadata") or {}

    manifest = atlas_meta.get("module_manifest") or []
    if manifest:
        for entry in manifest:
            name = entry.get("name")
            if not name:
                continue
            try:
                status = ModuleStatus[entry.get("status", "SUCCESS")]
            except KeyError:
                status = ModuleStatus.SUCCESS
            state.modules[name] = ModuleResult(
                name=name,
                status=status,
                error_message=entry.get("error_message") or None,
                duration_ms=entry.get("duration_ms"),
                transport_type=entry.get("transport_type") or "local",
            )
        return state

    failed = {
        m.get("name"): (m.get("error_message") or "")
        for m in (atlas_meta.get("failed_modules") or [])
        if isinstance(m, dict) and m.get("name")
    }
    for name in captured_data:
        if name == "_atlas" or name in failed or name not in _KNOWN_CAPTURE_SECTIONS:
            continue
        state.register_module(name)
        state.complete_module(name, duration_ms=0)
    for name, err in failed.items():
        state.register_module(name)
        state.start_module(name)
        state.fail_module(name, err, duration_ms=0)
    return state


def validate_categories_from_results(rows: list[dict]) -> dict[str, ValidateCategoryState]:
    """Rebuild the per-category tallies from already-computed validation rows
    (``ValidationResults.rows``) — used to show a finished Validate card
    alongside a live Report card."""
    cats: dict[str, ValidateCategoryState] = {}
    for row in rows:
        key = row.get("category") or "other"
        cat = cats.setdefault(key, ValidateCategoryState(
            key=key, label=VALIDATE_CATEGORY_LABELS.get(key, key.title()), total=0,
        ))
        cat.total += 1
        cat.processed += 1
        status = str(row.get("status", "")).upper()
        if status == "PASS":
            cat.passed += 1
        elif status == "FAIL":
            cat.failed += 1
        else:
            cat.skipped += 1
    for cat in cats.values():
        cat.status = "done"
    return cats


def avc_from_results(extended_results: list[dict]) -> list[AvcCheckState]:
    """Rebuild the finished per-check AVC list from already-computed extended
    check dicts (``ValidationResults.metadata["extended_results"]``, each one
    ``ExtendedCheckResult.to_dict()`` — carries ``group`` alongside
    ``category``) — used to show a finished Validate card's Additional
    Checks section alongside a live Report card, the same way
    ``validate_categories_from_results`` does for rule categories."""
    return [
        AvcCheckState(
            check_id=r.get("check_id", ""), name=r.get("name", r.get("check_id", "")),
            group=r.get("group", ""), status="done",
            result_status=str(r.get("status", "")).upper(), message=r.get("message", ""),
        )
        for r in extended_results
    ]


# ════════════════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ════════════════════════════════════════════════════════════════════════════════
def _fmt_ms(ms: float) -> str:
    ms = ms or 0
    if ms < 1000:
        return f"{ms:.0f}ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s) // 60}m{int(s) % 60:02d}s"


def _fmt_clock(elapsed: float) -> str:
    m, s = divmod(int(max(0, elapsed)), 60)
    return f"{m:02d}:{s:02d}"


def _row(g: str, gc: str, label: str, ls: str, right: str = "", rs: str = "") -> Table:
    """One aligned row: glyph | label (flex, ellipsizes) | right-aligned status."""
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(width=2)
    row.add_column(ratio=1)
    row.add_column(justify="right")
    row.add_row(
        Text(g, style=f"bold {gc}"),
        Text(label, style=ls, no_wrap=True, overflow="ellipsis"),
        Text(right, style=rs),
    )
    return row


def _spinner_row(label: str, right: str = "") -> Table:
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(width=2)
    row.add_column(ratio=1)
    row.add_column(justify="right")
    spinner_name = "line" if ui.is_plain_mode() else "dots"
    row.add_row(
        Spinner(spinner_name, style=f"bold {theme.primary_glow}"),
        Text(label, style=f"bold {theme.primary_glow}", no_wrap=True, overflow="ellipsis"),
        Text(right, style=theme.text_dim),
    )
    return row


def _fit_rows(console: Console, rows: list, width: int, budget: int) -> list:
    """Safety-net truncation — the collapse logic already keeps each card
    small, but if a terminal is unusually short this guarantees a hard cap
    instead of spilling past the panel. Measures each row's REAL rendered
    line count (a wrapped note or a 2-line error detail costs more than 1
    line) rather than counting list items — Rich's fixed-height Panel clips
    overflow silently rather than growing, so an item-count budget can
    under-count and let the tail of a card get clipped without warning."""
    if budget <= 0:
        return []
    out: list = []
    used = 0
    for i, r in enumerate(rows):
        h = len(console.render_lines(r, console.options.update(width=width)))
        if used + h > budget:
            remaining = len(rows) - i
            if remaining > 0 and budget - used >= 1:
                out.append(Text(f"… +{remaining} more", style=theme.text_ghost))
            return out
        out.append(r)
        used += h
    return out


def _meter(passed: int, failed: int, skipped: int, width: int = 22, warned: int = 0) -> Text:
    """A compliance meter, not a health/status light — segments are
    proportional counts, not a pass/fail verdict, which is the whole point
    (see the module docstring on why VALIDATE avoids ✓/✗)."""
    segments = [(passed, theme.success), (warned, theme.warning), (failed, theme.error)]
    total = passed + failed + skipped + warned
    out = Text()
    if total == 0:
        out.append("─" * width, style=theme.text_ghost)
        return out
    used = 0
    for count, color in segments:
        if count <= 0:
            continue
        w = round(count / total * width)
        if w:
            out.append("█" * w, style=color)
            used += w
    remaining = max(0, width - used)
    if remaining:
        out.append("░" * remaining, style=theme.text_ghost)
    return out


def _gradient(text: str, *stops: str) -> Text:
    def hx(h):
        return tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    out = Text()
    chars = list(text)
    rgb = [hx(c) for c in stops]
    n, segs = len(chars), len(rgb) - 1
    for i, ch in enumerate(chars):
        if ch == " " or n == 1:
            out.append(ch, style=f"bold {stops[0]}")
            continue
        t = i / (n - 1)
        seg = min(int(t * segs), segs - 1)
        loc = t * segs - seg
        a, b = rgb[seg], rgb[seg + 1]
        col = tuple(round(a[k] + (b[k] - a[k]) * loc) for k in range(3))
        out.append(ch, style=f"bold #{col[0]:02x}{col[1]:02x}{col[2]:02x}")
    return out


def _module_category(name: str) -> str:
    info = get_module_info(name)
    if info is not None:
        return info.category.name
    return _CATEGORY_OVERRIDES.get(name, ModuleCategory.PLATFORM).name


def _module_label(name: str) -> str:
    info = get_module_info(name)
    if info is not None:
        return info.name
    return _LABEL_OVERRIDES.get(name, name.replace("_", " ").title())


# ════════════════════════════════════════════════════════════════════════════════
#  CAPTURE CARD
# ════════════════════════════════════════════════════════════════════════════════
def _module_row(name: str, m: ModuleResult):
    pad = "  "
    label = f"{pad}{_module_label(name)}"
    if m.status == ModuleStatus.PENDING:
        return _row(glyph("pending"), theme.text_ghost, label, theme.text_muted, "", "")
    if m.status == ModuleStatus.RUNNING:
        return _spinner_row(label, m.transport_type.upper())
    if m.status == ModuleStatus.SUCCESS:
        # A conf module that fell back to SSH/K8s after its primary protocol
        # source came back empty — flagged distinctly (same accent color the
        # old standalone print used) rather than a plain duration, since
        # "the primary source was unavailable" is worth knowing at a glance.
        fallback_tag = _FALLBACK_TRANSPORT_LABELS.get(m.transport_type)
        if fallback_tag:
            return _row(glyph("success"), theme.success_glow, label, theme.text_secondary,
                        fallback_tag, theme.accent)
        return _row(glyph("success"), theme.success_glow, label, theme.text_secondary,
                    _fmt_ms(m.duration_ms), theme.text_dim)
    if m.status == ModuleStatus.FAILED:
        main = _row(glyph("error"), theme.error_glow, label, f"bold {theme.error_glow}", "failed", theme.error_glow)
        detail = Text(f"{pad}    {m.error_message or 'unknown error'}", style=theme.error, overflow="fold")
        return Group(main, detail)
    # skipped / deferred (not currently produced by any collector, but the
    # capture UI has always rendered them — see ModuleStatus docstring)
    main = _row(glyph("skip"), theme.warning_dim, label, theme.text_dim, "skipped", theme.warning_dim)
    if m.error_message:
        detail = Text(f"{pad}    {m.error_message}", style=theme.text_dim, overflow="fold")
        return Group(main, detail)
    return main


def _capture_category_block(cat: str, mods: list[tuple[str, ModuleResult]]) -> list:
    """Every module in this category, always — Cody wants the individual
    captures visible, not collapsed into a summary line. A category header
    (no icon — module rows already carry their own status) plus one row
    per module."""
    block: list = [Text(cat, style=f"bold {theme.text_secondary}")]
    block.extend(_module_row(n, m) for n, m in mods)
    return block


def build_capture_card(capture: CaptureState, width: int, height: int, console: Console,
                        *, active: bool, done: bool) -> Panel:
    """The CAPTURE card — every module, grouped by category, never scrolls."""
    by_cat: dict[str, list[tuple[str, ModuleResult]]] = {}
    order: list[str] = []
    for name, m in capture.modules.items():
        cat = _module_category(name)
        if cat not in by_cat:
            by_cat[cat] = []
            order.append(cat)
        by_cat[cat].append((name, m))

    rows: list = []
    for cat in order:
        rows.extend(_capture_category_block(cat, by_cat[cat]))

    n_total = capture.total_count
    n_done = capture.completed_count
    n_failed = capture.failed_count
    n_skipped = sum(1 for m in capture.modules.values() if m.status in (ModuleStatus.SKIPPED, ModuleStatus.DEFERRED))

    footer = Table.grid(expand=True)
    footer.add_column(ratio=1)
    footer.add_column(justify="right")
    footer_right = Text()
    if n_failed:
        footer_right.append(f"{n_failed} failed", style=f"bold {theme.error}")
    if n_skipped:
        if n_failed:
            footer_right.append("  ·  ", style=theme.text_ghost)
        footer_right.append(f"{n_skipped} skipped", style=theme.warning_dim)
    footer.add_row(Text(f"{n_done}/{n_total} modules", style=theme.text_muted), footer_right)

    color = theme.primary if active else (theme.success if done else theme.text_ghost)
    title = Text(" CAPTURE ", style=f"bold {theme.bg_primary} on {color}")

    inner_w = width - 6
    inner_h = height - 4
    body_rows = _fit_rows(console, rows, inner_w, max(1, inner_h - 2))
    content = Group(*body_rows, Rule(style=theme.border_ghost), footer) if rows else Group(footer)

    return Panel(content, title=title, title_align="left", box=box.ROUNDED, border_style=color,
                 style=f"on {theme.tint_neutral}", padding=(1, 2), width=width, height=height)


# ════════════════════════════════════════════════════════════════════════════════
#  VALIDATE CARD
# ════════════════════════════════════════════════════════════════════════════════
def _category_row(c: ValidateCategoryState, meter_width: int = 14):
    """A rule FAILING is a compliance finding, not a bug — so a done
    category never gets a ✓/✗ icon (that reads as "broken"). Instead: a
    small meter (proportional, like the card's own overall one), the real
    pass/warn/fail/skip counts, and the rate — all on one row. A done
    category used to cost 2 lines (meter row + a counts line below it);
    collapsing to 1 is what makes room for Additional Checks below. On a
    narrow card (``meter_width`` at its floor) the counts are dropped
    rather than squeezing the label — the meter alone still carries the
    proportions."""
    label = c.label.upper()
    if c.status == "pending":
        return _row(glyph("pending"), theme.text_ghost, label, theme.text_ghost, f"pending ({c.total})", theme.text_ghost)
    if c.status == "active":
        right = f"{c.processed}/{c.total}"
        if c.passed or c.failed or c.warned:
            right = f"{c.processed}/{c.total} · {c.passed}p {c.failed}f"
        return _spinner_row(label, right)
    if c.passed == 0 and c.failed == 0 and c.warned == 0 and c.skipped:
        # Nothing was actually evaluated (e.g. its capture failed upstream) —
        # a meter reading 0% would misreport this as a failing category.
        return _row(glyph("skip"), theme.warning_dim, label, theme.text_dim, "not evaluated", theme.warning_dim)

    evaluated = c.passed + c.failed + c.warned
    rate = (c.passed / evaluated * 100) if evaluated else 0.0
    rate_color = theme.success if rate >= 90 else theme.warning if rate >= 70 else theme.error

    counts = f"{c.passed}p"
    if c.warned:
        counts += f" {c.warned}w"
    if c.failed:
        counts += f" {c.failed}f"
    if c.skipped:
        counts += f" {c.skipped}s"

    show_counts = meter_width > 8
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(ratio=1)
    row.add_column(width=meter_width)
    if show_counts:
        row.add_column(width=14, justify="right")
    row.add_column(width=6, justify="right")
    cells = [
        Text(label, style=f"bold {theme.text_secondary}", no_wrap=True, overflow="ellipsis"),
        _meter(c.passed, c.failed, c.skipped, width=meter_width, warned=c.warned),
    ]
    if show_counts:
        cells.append(Text(counts, style=theme.text_dim, no_wrap=True, overflow="crop"))
    cells.append(Text(f"{rate:.0f}%", style=f"bold {rate_color}"))
    row.add_row(*cells)
    return row


def _avc_check_row(c: AvcCheckState):
    """One Additional Validation Check, same row shape as ``_module_row`` on
    the Capture card — but binary, not judged: PASS/WARN/INFO/FAIL all read
    as "ran" (the check executed; whether it found something is a report.html
    concern, same reasoning ``_category_row`` already applies to rule
    categories). Only a real SKIP (deactivated, tier doesn't apply, required
    data missing) reads as "skipped" — the check never actually ran."""
    pad = "  "
    label = f"{pad}{c.name}"
    if c.status == "pending":
        return _row(glyph("pending"), theme.text_ghost, label, theme.text_muted, "", "")
    if c.status == "active":
        return _spinner_row(label)
    if c.result_status == "SKIP":
        return _row(glyph("skip"), theme.warning_dim, label, theme.text_dim, "skipped", theme.warning_dim)
    return _row(glyph("bullet"), theme.primary_dim, label, theme.text_secondary, "ran", theme.text_dim)


def _group_avc(avc: list[AvcCheckState]) -> list[tuple[str, list[AvcCheckState]]]:
    """Bucket the flat check list into contiguous (group, checks) runs —
    ``list_checks_grouped()`` (and the report-side reconstruction, which
    walks results in that same original order) already emit one group at a
    time, so a simple run-length grouping is enough; no sorting needed."""
    groups: list[tuple[str, list[AvcCheckState]]] = []
    for c in avc:
        if groups and groups[-1][0] == c.group:
            groups[-1][1].append(c)
        else:
            groups.append((c.group, [c]))
    return groups


def _avc_group_header(group: str, suffix: str, style: str) -> Text:
    """A bulleted CheckGroup line — the "•" gives each group its own visual
    anchor in a section that's otherwise all text, in any of its three
    states (pending count / running / finished summary)."""
    line = Text(f"  {glyph('bullet')} ", style=theme.primary_dim)
    line.append(f"{group} — {suffix}", style=style)
    return line


def _avc_group_rows(group: str, items: list[AvcCheckState]) -> list:
    """One CheckGroup's contribution to the Additional Checks section.

    This is what keeps the section's height bounded as the check count grows
    (19 today, maybe 50+ later) without any arbitrary threshold: at any
    moment, only the group(s) actually in flight are expanded to per-check
    rows. A group nothing has touched yet, or one that's fully finished,
    collapses to a single line — so the steady-state height tracks the
    number of GROUPS (a handful, growing slowly) rather than the number of
    checks (growing however fast new checks get added)."""
    if all(c.status == "pending" for c in items):
        return [_avc_group_header(group, f"pending ({len(items)})", theme.text_ghost)]
    if all(c.status == "done" for c in items):
        skipped = sum(1 for c in items if c.result_status == "SKIP")
        ran = len(items) - skipped
        parts = [f"{ran} ran"] if ran else []
        if skipped:
            parts.append(f"{skipped} skipped")
        return [_avc_group_header(group, " · ".join(parts), theme.text_dim)]
    rows: list = [_avc_group_header(group, "running…", theme.text_dim)]
    rows.extend(_avc_check_row(c) for c in items)
    return rows


def _pipeline_row(r) -> Group:
    """One MongoDB operational pipeline's row — a real technical success/
    failure (did the aggregation run), not a compliance finding, so ✓/✗ is
    the correct signal here (unlike a rule category above)."""
    if r.succeeded:
        main = _row(glyph("success"), theme.success_glow, r.name, theme.text_secondary,
                    f"{r.row_count:,} rows · {r.duration_ms / 1000:.1f}s", theme.text_dim)
        return Group(main)
    main = _row(glyph("error"), theme.error_glow, r.name, f"bold {theme.error_glow}", "failed", theme.error_glow)
    detail = Text(f"    {r.error or 'unknown error'}", style=theme.error, overflow="fold")
    return Group(main, detail)


def build_validate_card(categories: dict[str, ValidateCategoryState] | None, width: int, height: int,
                         console: Console, *, active: bool, done: bool, started: bool,
                         avc: list[AvcCheckState] | None = None,
                         pipelines: OperationalReport | None = None) -> Panel:
    """The VALIDATE card — one block per rule category, live meter while in
    flight, plus Additional Validation Checks (if this tier runs them, one
    row per check grouped by CheckGroup) and MongoDB operational pipelines
    (if the user ran any) in their own sections below."""
    color = theme.primary if active else (theme.success if done else theme.text_ghost)
    title = Text(" VALIDATE ", style=f"bold {theme.bg_primary} on {color}")

    if not started or not categories:
        body = Align.center(Text("waiting for capture to finish…", style=theme.text_ghost), vertical="middle")
        return Panel(body, title=title, title_align="left", box=box.ROUNDED, border_style=color,
                     style=f"on {theme.tint_neutral}", padding=(1, 2), width=width, height=height)

    meter_width = max(8, min(18, width // 5))
    rows = [_category_row(c, meter_width) for c in categories.values()]
    # Pipelines is a static recap (already finished during capture), not a
    # live check — it only occupies space while AVC hasn't asked for the
    # room yet. Once AVC starts (frame.avc gets populated), Pipelines is
    # dropped for the rest of this card's life so AVC has space to render;
    # its data isn't lost, just no longer shown here (see report.html).
    if pipelines and pipelines.pipeline_count and not avc:
        rows.append(Rule(style=theme.border_ghost))
        rows.append(Text("OPERATIONAL PIPELINES", style=f"bold {theme.text_secondary}"))
        rows.extend(_pipeline_row(r) for r in pipelines.results)
    if avc:
        rows.append(Rule(style=theme.border_ghost))
        rows.append(Text("ADDITIONAL CHECKS", style=f"bold {theme.text_secondary}"))
        for group, items in _group_avc(avc):
            rows.extend(_avc_group_rows(group, items))

    total_pass = sum(c.passed for c in categories.values())
    total_fail = sum(c.failed for c in categories.values())
    total_skip = sum(c.skipped for c in categories.values())
    evaluated = total_pass + total_fail
    rate = (total_pass / evaluated * 100) if evaluated else 0.0
    rate_color = theme.success if rate >= 90 else theme.warning if rate >= 70 else theme.error

    meter_row = Table.grid(expand=True)
    meter_row.add_column(ratio=1)
    meter_row.add_column(justify="right")
    meter_row.add_row(_meter(total_pass, total_fail, total_skip, width=max(10, width - 20)),
                       Text(f"{rate:.1f}%", style=f"bold {rate_color}"))
    footer_line = Text(f"{total_pass} pass · {total_fail} fail · {total_skip} skip", style=theme.text_dim)

    inner_w = width - 6
    inner_h = height - 4
    body_rows = _fit_rows(console, rows, inner_w, max(1, inner_h - 3))
    content = Group(*body_rows, Rule(style=theme.border_ghost), meter_row, footer_line)

    return Panel(content, title=title, title_align="left", box=box.ROUNDED, border_style=color,
                 style=f"on {theme.tint_neutral}", padding=(1, 2), width=width, height=height)


# ════════════════════════════════════════════════════════════════════════════════
#  REPORT CARD
# ════════════════════════════════════════════════════════════════════════════════
def _step_row(s: ReportStepState):
    if s.status == "pending":
        return _row(glyph("pending"), theme.text_ghost, s.label, theme.text_ghost, "", "")
    if s.status == "running":
        return _spinner_row(s.label)
    main = _row(glyph("success"), theme.success_glow, s.label, theme.text_secondary, "", "")
    if not s.detail:
        return main
    return Group(main, Text(f"    {s.detail}", style=theme.text_dim, no_wrap=True, overflow="ellipsis"))


def build_report_card(steps: list[ReportStepState] | None, width: int, height: int, console: Console,
                       *, active: bool, done: bool, started: bool,
                       score: dict[str, Any] | None = None, report_path: str = "",
                       notes: list[str] | None = None, opened_browser: bool | None = None) -> Panel:
    """The REPORT card — the 4 real steps, then the finished-state payoff
    (score/path/browser outcome), which is reserved space and never
    truncated even on a short terminal."""
    color = theme.primary if active else (theme.success if done else theme.text_ghost)
    title = Text(" REPORT ", style=f"bold {theme.bg_primary} on {color}")

    if not started or not steps:
        body = Align.center(Text("waiting for validation to finish…", style=theme.text_ghost), vertical="middle")
        return Panel(body, title=title, title_align="left", box=box.ROUNDED, border_style=color,
                     style=f"on {theme.tint_neutral}", padding=(1, 2), width=width, height=height)

    rows: list = [_step_row(s) for s in steps]
    for note in (notes or []):
        rows.append(Text(f"  – {note}", style=theme.text_dim, overflow="fold"))

    inner_w = width - 6
    inner_h = height - 4

    # The finished-state payoff (score + file path + "opened in browser") is
    # more important than the step-by-step list above it, so it's reserved
    # space FIRST and never truncated — only the step list gets clipped if
    # the terminal is too short for both.
    tail: list = []
    if done and score is not None:
        passed = score.get("passed", 0)
        failed = score.get("failed", 0)
        skipped = score.get("skipped", 0)
        pct = score.get("pct", 0.0)
        rate_color = theme.success if pct >= 90 else theme.warning if pct >= 70 else theme.error

        stat = Table.grid(expand=True, padding=(0, 1))
        stat.add_column(ratio=1)
        stat.add_column(justify="right")
        stat.add_row(Text("Compliant", style=theme.text_dim), Text(str(passed), style=f"bold {theme.success}"))
        stat.add_row(Text("Non-Compliant", style=theme.text_dim), Text(str(failed), style=f"bold {theme.error}"))
        stat.add_row(Text("Skipped", style=theme.text_dim), Text(str(skipped), style=f"bold {theme.text_secondary}"))
        stat.add_row(Text("Score", style=theme.text_dim), Text(f"{pct:.1f}%", style=f"bold {rate_color}"))

        tail = [Rule(style=theme.border_ghost), stat]
        if report_path:
            tail.append(Text(""))
            tail.append(Text(report_path, style=theme.primary_dim, no_wrap=True, overflow="ellipsis"))
        if opened_browser is True:
            tail.append(Text("↗ opened in browser", style=f"bold {theme.success}"))
        elif opened_browser is False:
            tail.append(Text("server environment — open the file manually", style=theme.text_dim))

    tail_h = sum(len(console.render_lines(r, console.options.update(width=inner_w))) for r in tail)
    body_rows = _fit_rows(console, rows, inner_w, max(1, inner_h - tail_h))
    return Panel(Group(*body_rows, *tail), title=title, title_align="left", box=box.ROUNDED, border_style=color,
                 style=f"on {theme.tint_neutral}", padding=(1, 2), width=width, height=height)


# ════════════════════════════════════════════════════════════════════════════════
#  HEADER
# ════════════════════════════════════════════════════════════════════════════════
def _phase_chain(phase: str, done: bool) -> Text:
    """The C→V→R chain in the header (mode="all" only). ``phase`` is this
    frame's own live phase; ``done`` is whether it has finished — the
    pipeline is only fully complete once the REPORT frame reports done."""
    order = ["capture", "validate", "report"]
    labels = ["CAPTURE", "VALIDATE", "REPORT"]
    all_done = phase == "report" and done
    idx = len(order) if all_done else order.index(phase)
    out = Text()
    for i, label in enumerate(labels):
        completed = all_done or i < idx
        is_current = not completed and i == idx
        if i > 0:
            link_done = all_done or i <= idx
            out.append(" ━━ " if link_done else " ── ",
                       style=theme.success if (all_done or i < idx) else theme.text_ghost)
        if completed:
            out.append(glyph("node_done") + " ", style=f"bold {theme.success}")
        elif is_current:
            out.append(glyph("node_current") + " ", style=f"bold {theme.primary}")
        else:
            out.append(glyph("node_pending") + " ", style=theme.text_ghost)
        out.append(label, style=f"bold {theme.text_secondary}" if (completed or is_current) else theme.text_ghost)
    return out


_TIER_LABEL_COLOR = {
    "standard": ("Standard", "tier_standard"),
    "saas": ("SaaS", "tier_saas"),
}


def build_header(frame: PipelineFrame) -> tuple[Panel, int]:
    """Wordmark + elapsed clock + tier/env chips, plus the C→V→R phase chain
    in "all" mode. Returns (panel, rendered_height) so callers can size the
    cards below to fill exactly what's left of the terminal."""
    left = Text()
    left.append("⬢ ", style=f"bold {theme.accent}")
    left.append(_gradient("PLATFORM ATLAS", theme.primary_glow, theme.primary, theme.secondary))

    right = Text(_fmt_clock(time() - frame.start_time), style=f"bold {theme.text_secondary}")

    top = Table.grid(expand=True)
    top.add_column(ratio=1)
    top.add_column(justify="right")
    top.add_row(left, right)

    tier_label, tier_attr = _TIER_LABEL_COLOR.get((frame.tier or "").lower(), ("Extended", "tier_extended"))
    tier_color = getattr(theme, tier_attr)
    ctx_line = Text()
    ctx_line.append(tier_label, style=f"bold {tier_color}")
    if frame.env_name:
        ctx_line.append("  ·  ", style=theme.text_ghost)
        ctx_line.append(frame.env_name, style=theme.primary)
        ctx_line.append("  env", style=theme.text_ghost)
    if frame.mode == "all":
        ctx_line.append("    ", style=theme.text_ghost)
        ctx_line.append(_phase_chain(frame.phase, frame.done))

    return Panel(Group(top, ctx_line), box=box.HEAVY, border_style=theme.banner_rule,
                 style=f"on {theme.banner_bg}", padding=(0, 2)), 4


# ════════════════════════════════════════════════════════════════════════════════
#  LAYOUT
# ════════════════════════════════════════════════════════════════════════════════
# Cards are capped well short of the terminal's full height and always leave
# a real margin below them. A card stretched to the last row fights with
# whatever prints once the Live block closes (a "Saved to:", the next-step
# panel) — each such print forces the terminal to scroll, and a Live region
# touching the bottom row visibly "bounces" as that happens. The cap also
# keeps a card from looking mostly-empty on a tall terminal now that Capture
# and Validate both show real, non-collapsed content.
CARD_MIN_HEIGHT = 14
CARD_MAX_HEIGHT = 34
CARD_BOTTOM_MARGIN = 10


def _card_height(console: Console, header_h: int) -> int:
    return max(CARD_MIN_HEIGHT, min(CARD_MAX_HEIGHT, console.height - header_h - CARD_BOTTOM_MARGIN))


def render_frame(console: Console, frame: PipelineFrame) -> Group:
    """Build the full renderable for one Live tick — a header plus either a
    single centered card (``mode="solo"``) or all three side by side
    (``mode="all"``)."""
    header, header_h = build_header(frame)

    if frame.mode == "solo":
        card_h = _card_height(console, header_h)
        card_w = min(SOLO_CARD_WIDTH, max(40, console.width - 4))
        active, done = not frame.done, frame.done
        if frame.phase == "capture":
            card = build_capture_card(frame.capture or CaptureState(), card_w, card_h, console,
                                       active=active, done=done)
        elif frame.phase == "validate":
            card = build_validate_card(frame.validate, card_w, card_h, console,
                                        active=active, done=done, started=True,
                                        avc=frame.avc, pipelines=frame.pipelines)
        else:
            card = build_report_card(frame.report_steps, card_w, card_h, console,
                                      active=active, done=done, started=True,
                                      score=frame.report_score, report_path=frame.report_path,
                                      notes=frame.report_notes, opened_browser=frame.report_opened_browser)
        return Group(header, Text(""), Align.center(card))

    card_h = _card_height(console, header_h)
    card_w = max(34, (console.width - 8) // 3)

    # Each phase's own frame is fixed at its own `phase` value for its whole
    # life (see PipelineFrame's docstring) — an earlier phase in the C→V→R
    # order is always "done" (its capture/validate data, when present, is
    # already a finished snapshot); the CURRENT phase is "done" only once
    # this frame's own `done` flag flips.
    capture_done = frame.phase != "capture" or frame.done
    capture_card = build_capture_card(frame.capture or CaptureState(), card_w, card_h, console,
                                       active=frame.phase == "capture" and not frame.done, done=capture_done)

    validate_active = frame.phase == "validate" and not frame.done
    validate_done = frame.phase == "report" or (frame.phase == "validate" and frame.done)
    validate_started = frame.phase != "capture"
    validate_card = build_validate_card(frame.validate, card_w, card_h, console,
                                         active=validate_active, done=validate_done, started=validate_started,
                                         avc=frame.avc, pipelines=frame.pipelines)

    report_active = frame.phase == "report" and not frame.done
    report_done = frame.phase == "report" and frame.done
    report_started = frame.phase == "report"
    report_card = build_report_card(frame.report_steps, card_w, card_h, console,
                                     active=report_active, done=report_done, started=report_started,
                                     score=frame.report_score, report_path=frame.report_path,
                                     notes=frame.report_notes, opened_browser=frame.report_opened_browser)

    row = Table.grid(padding=(0, 1), expand=False)
    row.add_column()
    row.add_column()
    row.add_column()
    row.add_row(capture_card, validate_card, report_card)
    return Group(header, Text(""), row)
