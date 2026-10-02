"""
ATLAS // Diff Engine
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
import html as html_mod
import json
import re

from platform_atlas.core._version import __version__
from platform_atlas.core.utils import secure_mkdir
from platform_atlas.reporting.scoring import weighted_pass_percent
from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css
from platform_atlas.validation.results import ValidationResults

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Change Classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ChangeType(str, Enum):
    """Describes what happened to a rule between two captures"""

    FIXED = "Fixed"
    REGRESSED = "Regressed"
    UNCHANGED = "Unchanged"
    NEW_RULE = "New Rule"
    REMOVED = "Removed"
    CHANGED = "Changed"
    SKIPPED = "Skipped"

    def __str__(self) -> str:
        return self.value

_FAILING = frozenset({"FAIL", "ERROR", "NON-COMPLIANT"})
_PASSING = frozenset({"PASS", "COMPLIANT"})
_SKIPPED = frozenset({"SKIP", "SKIPPED", "N/A", "NA"})

def classify_change(baseline_status: str | None, latest_status: str | None) -> ChangeType:
    """Determine the type of change between two statuses"""
    if baseline_status is None:
        return ChangeType.NEW_RULE
    if latest_status is None:
        return ChangeType.REMOVED

    b = baseline_status.upper()
    l = latest_status.upper()

    if b == l:
        return ChangeType.UNCHANGED

    # Either side is skip -> treat as Skipped
    if b in _SKIPPED or l in _SKIPPED:
        return ChangeType.SKIPPED

    # Fail -> Pass = Fixed
    if b in _FAILING and l in _PASSING:
        return ChangeType.FIXED

    # Pass -> Fail = Regressed
    if b in _PASSING and l in _FAILING:
        return ChangeType.REGRESSED

    # Anything else that actually changed
    return ChangeType.CHANGED

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Diff Summary Statistics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True, slots=True)
class DiffSummary:
    """Aggregate counts for a diff comparison"""

    total_rules: int
    fixed: int
    regressed: int
    unchanged: int
    new_rules: int
    removed: int
    changed: int
    skipped: int

    # Score deltas
    baseline_pass_pct: float
    latest_pass_pct: float

    @property
    def delta_pct(self) -> float:
        """Percentage-point improvement (positive = better)"""
        return round(self.latest_pass_pct - self.baseline_pass_pct, 1)

    @property
    def rating(self) -> str:
        """Human-readable assessment of the delta"""
        d = self.delta_pct
        if d > 10:
            return "Significant Improvement"
        if d > 0:
            return "Improved"
        if d == 0:
            return "No Change"
        if d > -10:
            return "Declined"
        return "Significant Decline"

def _pass_percent(results: ValidationResults, col: str = "status") -> float:
    """Severity-weighted pass percentage for a capture.

    Uses the shared ``weighted_pass_percent`` (critical 5x / warning 2x / info 1x)
    so the diff's baseline, current and delta agree with the severity-weighted
    score shown on each report's hero gauge — the plain unweighted rate read a
    couple of points different (e.g. 92% vs. the report's 95%)."""
    if results.empty:
        return 0.0
    return weighted_pass_percent(results.rows, status_column=col)

def summarize_diff(diff_result: ValidationResults) -> DiffSummary:
    """Build a DiffSummary from a completed diff result"""
    change_types = [row.get("change_type") for row in diff_result.rows]
    return DiffSummary(
        total_rules=len(diff_result),
        fixed=change_types.count(str(ChangeType.FIXED)),
        regressed=change_types.count(str(ChangeType.REGRESSED)),
        unchanged=change_types.count(str(ChangeType.UNCHANGED)),
        new_rules=change_types.count(str(ChangeType.NEW_RULE)),
        removed=change_types.count(str(ChangeType.REMOVED)),
        changed=change_types.count(str(ChangeType.CHANGED)),
        skipped=change_types.count(str(ChangeType.SKIPPED)),
        baseline_pass_pct=diff_result.metadata.get("baseline_pass_pct", 0.0),
        latest_pass_pct=diff_result.metadata.get("latest_pass_pct", 0.0),
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core Diff Logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _normalize_statuses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shallow-copy rows with ``status`` upper-cased and legacy compliance
    labels normalized to PASS/FAIL."""
    normalized = []
    for row in rows:
        row = dict(row)
        if row.get("status") is not None:
            status = str(row["status"]).upper()
            row["status"] = {"COMPLIANT": "PASS", "NON-COMPLIANT": "FAIL"}.get(status, status)
        normalized.append(row)
    return normalized

def _outer_join_by_key(baseline_rows, latest_rows, key):
    """Union two row lists by ``key``, baseline-first ordering (mirrors a
    pandas outer join without needing pandas for 123-row tables)."""
    b_by_key = {r[key]: r for r in baseline_rows}
    l_by_key = {r[key]: r for r in latest_rows}
    ordered_keys = list(b_by_key) + [k for k in l_by_key if k not in b_by_key]
    for k in ordered_keys:
        yield k, b_by_key.get(k), l_by_key.get(k)

def diff_reports(
        baseline: ValidationResults,
        latest: ValidationResults,
        *,
        join_on: str = "rule_number",
) -> ValidationResults:
    """Compare two validation results and return a diff result"""
    b_rows = _normalize_statuses(baseline.rows)
    l_rows = _normalize_statuses(latest.rows)

    rows: list[dict[str, Any]] = []

    for rule_number, b_row, l_row in _outer_join_by_key(b_rows, l_rows, join_on):
        b = b_row or {}
        l = l_row or {}

        b_status = b.get("status") if b_row is not None else None
        l_status = l.get("status") if l_row is not None else None

        change = classify_change(
            str(b_status) if b_status is not None else None,
            str(l_status) if l_status is not None else None,
        )

        # Pick the best available value for display columns
        name = _coalesce(l.get("name"), b.get("name"))
        category = _coalesce(l.get("category"), b.get("category"))
        severity = _coalesce(l.get("severity"), b.get("severity"))
        path = _coalesce(l.get("path"), b.get("path"))

        b_actual = b.get("actual") if b_row is not None else None
        l_actual = l.get("actual") if l_row is not None else None

        b_rec = b.get("recommendations") if b_row is not None else None
        l_rec = l.get("recommendations") if l_row is not None else None

        rows.append({
            "rule_number": rule_number,
            "name": name,
            "category": category,
            "severity": severity,
            "baseline_status": _display_status(b_status),
            "latest_status": _display_status(l_status),
            "change_type": str(change),
            "path": path,
            "baseline_actual": _safe_str(b_actual),
            "latest_actual": _safe_str(l_actual),
            "recommendations": l_rec or b_rec or "",
        })

    # Sort: regressions first, then fixed, then the rest
    change_sort_order = {
        str(ChangeType.REGRESSED): 0,
        str(ChangeType.FIXED): 1,
        str(ChangeType.CHANGED): 2,
        str(ChangeType.NEW_RULE): 3,
        str(ChangeType.UNCHANGED): 4,
        str(ChangeType.SKIPPED): 5,
        str(ChangeType.REMOVED): 6,
    }
    rows.sort(key=lambda r: (change_sort_order.get(r["change_type"], 99), r["rule_number"]))

    diff_result = ValidationResults(rows=rows)

    # Attach metadata for downstream reporting
    diff_result.metadata["baseline_pass_pct"] = _pass_percent(ValidationResults(rows=b_rows))
    diff_result.metadata["latest_pass_pct"] = _pass_percent(ValidationResults(rows=l_rows))
    diff_result.metadata["baseline_hostname"] = baseline.metadata.get("hostname", "Unknown")
    diff_result.metadata["latest_hostname"] = latest.metadata.get("hostname", "Unknown")
    diff_result.metadata["baseline_ruleset_id"] = baseline.metadata.get("ruleset_id", "")
    diff_result.metadata["latest_ruleset_id"] = latest.metadata.get("ruleset_id", "")
    diff_result.metadata["baseline_ruleset_version"] = baseline.metadata.get("ruleset_version", "")
    # Tier propagation — surface a cross-tier notice in the diff renderer
    # when comparing a Standard capture against an Extended one.
    diff_result.metadata["baseline_tier"] = baseline.metadata.get("tier", "extended")
    diff_result.metadata["latest_tier"] = latest.metadata.get("tier", "extended")
    diff_result.metadata["cross_tier"] = (
        diff_result.metadata["baseline_tier"] != diff_result.metadata["latest_tier"]
    )
    diff_result.metadata["latest_ruleset_version"] = latest.metadata.get("ruleset_version", "")
    diff_result.metadata["baseline_modules_ran"] = baseline.metadata.get("modules_ran", "")
    diff_result.metadata["latest_modules_ran"] = latest.metadata.get("modules_ran", "")

    return diff_result

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Diff-Specific Report Rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_diff_rows(diff_result: ValidationResults) -> list[dict[str, Any]]:
    """Build the row data consumed client-side by the findings table"""
    rows: list[dict[str, Any]] = []
    for row in diff_result.rows:
        rows.append({
            "rule_number": row.get("rule_number", ""),
            "name": row.get("name", ""),
            "category": row.get("category", ""),
            "severity": str(row.get("severity") or "info").lower(),
            "baseline_status": row.get("baseline_status", "-"),
            "latest_status": row.get("latest_status", "-"),
            "change_type": row.get("change_type", ""),
            "baseline_actual": row.get("baseline_actual", "-"),
            "latest_actual": row.get("latest_actual", "-"),
            "recommendations": row.get("recommendations") or "",
        })
    return rows

def _build_priority_list(diff_result: ValidationResults, max_items: int = 8) -> list[dict[str, Any]]:
    """Build the Priority Regressions list — regressions first, then any
    remaining open failures not already surfaced as a regression"""
    regressions = [
        row for row in diff_result.rows if row.get("change_type") == str(ChangeType.REGRESSED)
    ]
    remaining_fails = [
        row for row in diff_result.rows
        if str(row.get("latest_status", "")).upper() == "FAIL"
        and row.get("change_type") != str(ChangeType.REGRESSED)
    ]

    candidates = (regressions + remaining_fails)[:max_items]

    items: list[dict[str, Any]] = []
    for row in candidates:
        change = row.get("change_type", "")
        if change == str(ChangeType.REGRESSED):
            detail = "Regressed — was passing, now failing"
        else:
            detail = str(row.get("recommendations") or "Still failing since baseline")

        items.append({
            "rule_number": row.get("rule_number", ""),
            "name": row.get("name", "Unknown rule"),
            "change_type": change,
            "detail": detail,
        })

    return items

def render_diff_report(
        diff_result: ValidationResults,
        template_path: str | Path,
        output_path: str | Path | None = None,
        *,
        title: str = "Configuration Diff Report",
        subtitle: str = "",
) -> str:
    """Render a diff result through diff.html.

    diff.html shares report.html's rendering model: almost everything is
    driven client-side from a single viewmodel JSON embedded in the page,
    so this function's job is just to assemble that viewmodel and inject it
    (mirrors ``unified_renderer.render_unified_report``).
    """
    summary = summarize_diff(diff_result)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    ruleset_ver = (
        diff_result.metadata.get("latest_ruleset_version")
        or diff_result.metadata.get("baseline_ruleset_version")
        or "Unknown"
    )
    modules_ran = (
        diff_result.metadata.get("latest_modules_ran")
        or diff_result.metadata.get("baseline_modules_ran")
        or "Unknown"
    )
    target_system = diff_result.metadata.get("latest_hostname", "Unknown")
    modules_text, _is_partial = _generate_modules_footer(modules_ran)

    # ── Tier badge + cross-tier notice ──────────────────────────
    baseline_tier = (diff_result.metadata.get("baseline_tier") or "extended").lower()
    latest_tier = (diff_result.metadata.get("latest_tier") or "extended").lower()
    cross_tier = bool(diff_result.metadata.get("cross_tier"))

    if latest_tier == "standard":
        tier_label, tier_color, cover_kind = "STANDARD", "#1B93D2", "Application Audit"
    elif latest_tier == "saas":
        tier_label, tier_color, cover_kind = "SAAS", "#C5258F", "Gateway Audit"
    else:
        tier_label, tier_color, cover_kind = "EXTENDED", "#FF6633", "Infrastructure Audit"

    tier_note: str | None = None
    if cross_tier:
        b_label = html_mod.escape(baseline_tier.capitalize())
        l_label = html_mod.escape(latest_tier.capitalize())
        tier_note = (
            "<strong>Cross-tier diff:</strong> baseline was captured in "
            f"<strong>{b_label}</strong> mode, latest in <strong>{l_label}</strong>. "
            "Rules outside the narrower tier appear as SKIP — only rules common "
            "to both tiers are directly comparable."
        )
    elif latest_tier == "standard":
        tier_note = (
            "Want deeper validation? Itential&#39;s Extended Mode adds MongoDB, "
            "Redis, IG5 and system-layer audits. Contact your Itential CSM, or "
            "run <code>platform-atlas tier upgrade</code>."
        )

    viewmodel = {
        "title": title,
        "subtitle": subtitle,
        "organization_name": str(diff_result.metadata.get("organization_name", "") or ""),
        "atlas_version": __version__,
        "generated_at": timestamp,
        "ruleset_version": str(ruleset_ver),
        "target_system": str(target_system),
        "modules_footer": modules_text,
        "baseline": {
            "name": str(diff_result.metadata.get("baseline_name", "Baseline")),
            "date": str(diff_result.metadata.get("baseline_date", "")),
        },
        "current": {
            "name": str(diff_result.metadata.get("current_name", "Current")),
            "date": str(diff_result.metadata.get("current_date", "")),
        },
        "tier": {
            "label": tier_label,
            "color": tier_color,
            "cover_kind": cover_kind,
            "baseline_tier": baseline_tier,
            "latest_tier": latest_tier,
            "cross_tier": cross_tier,
        },
        "tier_note": tier_note,
        "summary": {
            "total_rules": summary.total_rules,
            "fixed": summary.fixed,
            "regressed": summary.regressed,
            "unchanged": summary.unchanged,
            "new_rules": summary.new_rules,
            "removed": summary.removed,
            "changed": summary.changed,
            "skipped": summary.skipped,
            "baseline_pass_pct": summary.baseline_pass_pct,
            "latest_pass_pct": summary.latest_pass_pct,
            "delta_pct": summary.delta_pct,
            "rating": summary.rating,
        },
        "priority": _build_priority_list(diff_result),
        "rows": _build_diff_rows(diff_result),
    }

    template = Path(template_path).read_text(encoding="utf-8")

    # ``</`` → ``<\/`` prevents a string value containing ``</script>`` from
    # closing the data island early — same hardening as unified_renderer.py.
    # ``<!--`` is neutralised too so ``<!--<script`` cannot break the island.
    payload = json.dumps(viewmodel, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "\\u003c!--")

    # Single-pass substitution so data can never be re-scanned for placeholders.
    values = {
        "TITLE": html_mod.escape(title),
        "DIFF_VIEWMODEL_JSON": payload,
        "EMBEDDED_FONTS": _get_font_css(),
    }
    html = re.sub(
        r"\{\{(TITLE|DIFF_VIEWMODEL_JSON|EMBEDDED_FONTS)\}\}",
        lambda m: values[m.group(1)],
        template,
    )

    if output_path:
        out = Path(output_path)
        secure_mkdir(out.parent)
        out.write_text(html, encoding="utf-8")

    return html

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _coalesce(*values: Any) -> str:
    """Return the first non-null, non-empty value, stringified"""
    for val in values:
        if val is not None and str(val).strip():
            return str(val)
    return ""

def _safe_str(value: Any) -> str:
    """Convert a value to a string, handling None gracefully"""
    if value is None:
        return "-"
    return str(value)

def _display_status(status: Any) -> str:
    """Normalize a status for display, handling None"""
    if status is None:
        return "-"
    return str(status).upper().replace("COMPLIANT", "PASS").replace("NON-COMPLIANT", "FAIL")

def _generate_modules_footer(modules_ran: list[str] | None) -> tuple[str, bool]:
    """Generate a simple string showing which modules ran"""
    if modules_ran is None:
        return "Modules: Unknown", False

    if modules_ran == ["all"]:
        return "Modules: All default modules collected", False

    # Join the list into a readable string
    return f"Modules: {', '.join(modules_ran)}", True
