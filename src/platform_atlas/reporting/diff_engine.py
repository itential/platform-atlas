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

import pandas as pd

from platform_atlas.core._version import __version__
from platform_atlas.core.utils import secure_mkdir
from platform_atlas.reporting.report_renderer import calculate_stats
from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css

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

def _pass_percent(df: pd.DataFrame, col: str = "status") -> float:
    """Calculate pass-percentage from a validation dataframe"""
    if df.empty:
        return 0.0
    stats = calculate_stats(df, status_column=col)
    return stats["pass_percent"]

def summarize_diff(diff_df: pd.DataFrame) -> DiffSummary:
    """Build a DiffSummary from a completed diff DataFrame"""
    change_col = diff_df["change_type"]
    return DiffSummary(
        total_rules=len(diff_df),
        fixed=int((change_col == str(ChangeType.FIXED)).sum()),
        regressed=int((change_col == str(ChangeType.REGRESSED)).sum()),
        unchanged=int((change_col == str(ChangeType.UNCHANGED)).sum()),
        new_rules=int((change_col == str(ChangeType.NEW_RULE)).sum()),
        removed=int((change_col == str(ChangeType.REMOVED)).sum()),
        changed=int((change_col == str(ChangeType.CHANGED)).sum()),
        skipped=int((change_col == str(ChangeType.SKIPPED)).sum()),
        baseline_pass_pct=diff_df.attrs.get("baseline_pass_pct", 0.0),
        latest_pass_pct=diff_df.attrs.get("latest_pass_pct", 0.0),
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core Diff Logic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def diff_reports(
        baseline: pd.DataFrame,
        latest: pd.DataFrame,
        *,
        join_on: str = "rule_number",
) -> pd.DataFrame:
    """Compare two validation DataFrames and return a diff DataFrame"""
    b = baseline.copy()
    l = latest.copy()

    for df in (b, l):
        if "status" in df.columns:
            df["status"] = df["status"].str.upper().replace({
                "COMPLIANT": "PASS",
                "NON-COMPLIANT": "FAIL",
            })

    # Outer-join on the rule key so we can capture adds/removes
    merged = pd.merge(
        b, l,
        on=join_on,
        how="outer",
        suffixes=("_baseline", "_latest"),
        indicator=True,
    )

    rows: list[dict[str, Any]] = []

    for row in merged.to_dict(orient="records"):
        rule_number = row[join_on]
        presence = row["_merge"]  # "left_only", "right_only", "both"

        b_status = row.get("status_baseline") if presence != "right_only" else None
        l_status = row.get("status_latest") if presence != "left_only" else None

        change = classify_change(
            str(b_status) if pd.notna(b_status) else None,
            str(l_status) if pd.notna(l_status) else None,
        )

        # Pick the best available value for display columns
        name = _coalesce(row, "name_latest", "name_baseline")
        category = _coalesce(row, "category_latest", "category_baseline")
        severity = _coalesce(row, "severity_latest", "severity_baseline")
        path = _coalesce(row, "path_latest", "path_baseline")

        b_actual = row.get("actual_baseline") if presence != "right_only" else None
        l_actual = row.get("actual_latest") if presence != "left_only" else None

        b_rec = row.get("recommendations_baseline") if presence != "right_only" else None
        l_rec = row.get("recommendations_latest") if presence != "left_only" else None

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

    diff_df = pd.DataFrame(rows)

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
    diff_df["_sort"] = diff_df["change_type"].map(change_sort_order).fillna(99)
    diff_df = diff_df.sort_values(["_sort", "rule_number"]).drop(columns=["_sort"])
    diff_df = diff_df.reset_index(drop=True)

    # Attach metadata for downstream reporting
    diff_df.attrs["baseline_pass_pct"] = _pass_percent(b)
    diff_df.attrs["latest_pass_pct"] = _pass_percent(l)
    diff_df.attrs["baseline_hostname"] = baseline.attrs.get("hostname", "Unknown")
    diff_df.attrs["latest_hostname"] = latest.attrs.get("hostname", "Unknown")
    diff_df.attrs["baseline_ruleset_id"] = baseline.attrs.get("ruleset_id", "")
    diff_df.attrs["latest_ruleset_id"] = latest.attrs.get("ruleset_id", "")
    diff_df.attrs["baseline_ruleset_version"] = baseline.attrs.get("ruleset_version", "")
    # Tier propagation — surface a cross-tier notice in the diff renderer
    # when comparing a Standard capture against an Extended one.
    diff_df.attrs["baseline_tier"] = baseline.attrs.get("tier", "extended")
    diff_df.attrs["latest_tier"] = latest.attrs.get("tier", "extended")
    diff_df.attrs["cross_tier"] = (
        diff_df.attrs["baseline_tier"] != diff_df.attrs["latest_tier"]
    )
    diff_df.attrs["latest_ruleset_version"] = latest.attrs.get("ruleset_version", "")
    diff_df.attrs["baseline_modules_ran"] = baseline.attrs.get("modules_ran", "")
    diff_df.attrs["latest_modules_ran"] = latest.attrs.get("modules_ran", "")

    return diff_df

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Diff-Specific Report Rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_diff_rows(diff_df: pd.DataFrame) -> list[dict[str, Any]]:
    """Build the row data consumed client-side by the findings table"""
    rows: list[dict[str, Any]] = []
    for _, row in diff_df.iterrows():
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

def _build_priority_list(diff_df: pd.DataFrame, max_items: int = 8) -> list[dict[str, Any]]:
    """Build the Priority Regressions list — regressions first, then any
    remaining open failures not already surfaced as a regression"""
    regressions = diff_df[diff_df["change_type"] == str(ChangeType.REGRESSED)].copy()
    remaining_fails = diff_df[
        (diff_df["latest_status"].str.upper() == "FAIL")
        & (diff_df["change_type"] != str(ChangeType.REGRESSED))
    ].copy()

    candidates = pd.concat([regressions, remaining_fails]).head(max_items)

    items: list[dict[str, Any]] = []
    for _, row in candidates.iterrows():
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
        diff_df: pd.DataFrame,
        template_path: str | Path,
        output_path: str | Path | None = None,
        *,
        title: str = "Configuration Diff Report",
        subtitle: str = "",
) -> str:
    """Render a diff DataFrame through diff.html.

    diff.html shares report.html's rendering model: almost everything is
    driven client-side from a single viewmodel JSON embedded in the page,
    so this function's job is just to assemble that viewmodel and inject it
    (mirrors ``unified_renderer.render_unified_report``).
    """
    summary = summarize_diff(diff_df)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    ruleset_ver = (
        diff_df.attrs.get("latest_ruleset_version")
        or diff_df.attrs.get("baseline_ruleset_version")
        or "Unknown"
    )
    modules_ran = (
        diff_df.attrs.get("latest_modules_ran")
        or diff_df.attrs.get("baseline_modules_ran")
        or "Unknown"
    )
    target_system = diff_df.attrs.get("latest_hostname", "Unknown")
    modules_text, _is_partial = _generate_modules_footer(modules_ran)

    # ── Tier badge + cross-tier notice ──────────────────────────
    baseline_tier = (diff_df.attrs.get("baseline_tier") or "extended").lower()
    latest_tier = (diff_df.attrs.get("latest_tier") or "extended").lower()
    cross_tier = bool(diff_df.attrs.get("cross_tier"))

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
            "Redis, IAG5 and system-layer audits. Contact your Itential CSM, or "
            "run <code>platform-atlas tier upgrade</code>."
        )

    viewmodel = {
        "title": title,
        "subtitle": subtitle,
        "organization_name": str(diff_df.attrs.get("organization_name", "") or ""),
        "atlas_version": __version__,
        "generated_at": timestamp,
        "ruleset_version": str(ruleset_ver),
        "target_system": str(target_system),
        "modules_footer": modules_text,
        "baseline": {
            "name": str(diff_df.attrs.get("baseline_name", "Baseline")),
            "date": str(diff_df.attrs.get("baseline_date", "")),
        },
        "current": {
            "name": str(diff_df.attrs.get("current_name", "Current")),
            "date": str(diff_df.attrs.get("current_date", "")),
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
        "priority": _build_priority_list(diff_df),
        "rows": _build_diff_rows(diff_df),
    }

    template = Path(template_path).read_text(encoding="utf-8")

    # ``</`` → ``<\/`` prevents a string value containing ``</script>`` from
    # closing the data island early — same hardening as unified_renderer.py.
    payload = json.dumps(viewmodel, ensure_ascii=False).replace("</", "<\\/")

    html = template.replace("{{TITLE}}", html_mod.escape(title))
    html = html.replace("{{DIFF_VIEWMODEL_JSON}}", payload)
    html = html.replace("{{EMBEDDED_FONTS}}", _get_font_css())

    if output_path:
        out = Path(output_path)
        secure_mkdir(out.parent)
        out.write_text(html, encoding="utf-8")

    return html

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _coalesce(row: pd.Series, *keys: str) -> str:
    """Return the first non-null, non-empty value from the row"""
    for key in keys:
        val = row.get(key)
        if pd.notna(val) and str(val).strip():
            return str(val)
    return ""

def _safe_str(value: Any) -> str:
    """Convert a value to a string, handling None/NaN gracefully"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return str(value)

def _display_status(status: Any) -> str:
    """Normalize a status for display, handling None"""
    if status is None or (isinstance(status, float) and pd.isna(status)):
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
