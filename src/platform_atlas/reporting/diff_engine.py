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

import pandas as pd

from platform_atlas.core._version import __version__
from platform_atlas.core.utils import secure_mkdir
from platform_atlas.reporting.report_renderer import calculate_stats

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

    for _, row in merged.iterrows():
        rule_number = row[join_on]
        presence = row["_merge"] # "left-only", "right-only", "both"

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
    diff_df.attrs["latest_ruleset_version"] = latest.attrs.get("ruleset_version", "")
    diff_df.attrs["baseline_modules_ran"] = baseline.attrs.get("modules_ran", "")
    diff_df.attrs["latest_modules_ran"] = latest.attrs.get("modules_ran", "")

    return diff_df

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Diff-Specific Report Rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _generate_diff_actions(diff_df: pd.DataFrame, max_actions: int = 5) -> tuple[str, int]:
    """Build priority-actions HTML from a diff DataFrame"""
    # Regressions are the highest priority in a diff context
    regressions = diff_df[diff_df["change_type"] == str(ChangeType.REGRESSED)].copy()
    remaining_fails = diff_df[
        (diff_df["latest_status"].str.upper() == "FAIL")
        & (diff_df["change_type"] != str(ChangeType.REGRESSED))
    ].copy()

    candidates = pd.concat([regressions, remaining_fails]).head(max_actions)

    if candidates.empty:
        html = '''
        <div class="no-actions">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" stroke-width="2">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            <p>No regressions detected!<br/>Looking good.</p>
        </div>
        '''
        return html, 0

    actions_html = []
    for _, row in candidates.iterrows():
        severity = str(row.get("severity", "info")).lower()
        rule_number = row.get("rule_number", "")
        name = row.get("name", "Unknown rule")
        change = row.get("change_type", "")

        if change == str(ChangeType.REGRESSED):
            detail = "Regressed - was passing, now failing"
        else:
            detail = str(row.get("recommendations", ""))

        # Escape values before inserting into HTML
        safe_severity = html_mod.escape(str(severity))
        safe_name = html_mod.escape(str(name))
        safe_detail = html_mod.escape(str(detail))
        safe_rule_number = html_mod.escape(str(rule_number))

        actions_html.append(f'''
        <div class="action-item">
            <div class="severity-dot {safe_severity}"></div>
            <div class="action-content">
                <p class="action-title">{safe_name}</p>
                <p class="action-detail">{safe_detail}</p>
            </div>
            <span class="action-rule">{safe_rule_number}</span>
        </div>
        ''')

    return "\n".join(actions_html), len(candidates)

def render_diff_report(
        diff_df: pd.DataFrame,
        template_path: str | Path,
        output_path: str | Path | None = None,
        *,
        title: str = "Configuration Diff Report",
        subtitle: str = "",
) -> str:
    """Render a diff DataFrame through the existing Atlas HTML template"""
    summary = summarize_diff(diff_df)

    # Auto-generate subtitle from metadata
    display_df = diff_df[[
        "rule_number", "name", "category", "severity",
        "baseline_status", "latest_status", "change_type",
        "baseline_actual", "latest_actual", "recommendations",
    ]].copy()

    # Rename for cleaner table headers
    display_df = display_df.rename(columns={
        "rule_number": "rule_number",
        "name": "name",
        "category": "category",
        "severity": "severity",
        "baseline_status": "baseline status",
        "latest_status": "latest status",
        "change_type": "change type",
        "baseline_actual": "baseline actual",
        "latest_actual": "latest actual",
        "recommendations": "recommendations",
    })

    # Use the latest pass percentage as the "score" in the ring
    # and latest stats for the stat cards
    latest_pct = summary.latest_pass_pct

    # Count latest statuses for the stat cards
    latest_statuses = diff_df["latest_status"].str.upper()
    pass_count = int((latest_statuses == "PASS").sum())
    fail_count = int((latest_statuses == "FAIL").sum())
    skip_count = int(latest_statuses.isin(["SKIP", "N/A", "-"]).sum())

    # Count baseline statuses for delta computation
    baseline_statuses = diff_df["baseline_status"].str.upper()
    b_pass = int((baseline_statuses == "PASS").sum())
    b_fail = int((baseline_statuses == "FAIL").sum())
    b_skip = int(baseline_statuses.isin(["SKIP", "N/A", "-"]).sum())

    # Changed count: everything that isn't Unchanged
    changed_count = int((diff_df["change_type"] != str(ChangeType.UNCHANGED)).sum())

    # Build diff-specific priority actions
    priority_html, action_count = _generate_diff_actions(diff_df)

    # System info shows the comparison context
    system_info = [
        f"Baseline: {summary.baseline_pass_pct:.0f}% compliant",
        f"Latest: {summary.latest_pass_pct:.0f}% compliant",
        f"Delta: {_format_delta(summary.delta_pct)}",
    ]

    # Build the score rating from the diff delta
    score_rating = summary.rating

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    footer = (
        f"Diff generated {timestamp} • "
        f"{summary.fixed} fixed • {summary.regressed} regressed • "
        f"{summary.unchanged} unchanged"
    )

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

    # --- Render through the existing template ---
    template = Path(template_path).read_text(encoding="utf-8")

    # Build table HTML from display DataFrame
    table_html = display_df.to_html(
        index=False,
        classes=["dataframe"],
        table_id="report-table",
        border=0,
        escape=True,
    )

    # Generate modules footer text
    modules_text, is_partial = _generate_modules_footer(modules_ran)

    # Add obelisk to score if partial
    score_obelisk = "†" if is_partial else ""
    modules_footnote = f"† Score based on partial data collection ({modules_text})"

    # ===== CRITICAL: Escape ONLY user-controlled values =====
    # These are the values that could contain XSS
    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    safe_footer = html_mod.escape(footer)
    safe_target_system = html_mod.escape(target_system)
    safe_ruleset_version = html_mod.escape(ruleset_ver)
    safe_timestamp = html_mod.escape(timestamp)
    safe_org_name = html_mod.escape(str(diff_df.attrs.get("organization_name", "")))
    safe_baseline_name = html_mod.escape(str(diff_df.attrs.get("baseline_name", "Baseline")))
    safe_current_name = html_mod.escape(str(diff_df.attrs.get("current_name", "Current")))
    safe_baseline_date = html_mod.escape(str(diff_df.attrs.get("baseline_date", "")))
    safe_current_date = html_mod.escape(str(diff_df.attrs.get("current_date", "")))

    replacements = {
        "{{TITLE}}": safe_title,
        "{{SUBTITLE}}": safe_subtitle,
        "{{TABLE}}": table_html,
        "{{FOOTER}}": safe_footer,
        "{{MODULES_FOOTER}}": modules_text,
        "{{PASS_COUNT}}": str(pass_count),
        "{{FAIL_COUNT}}": str(fail_count),
        "{{SKIP_COUNT}}": str(skip_count),
        "{{PASS_PERCENT}}": str(int(latest_pct)),
        "{{SCORE_OBELISK}}": score_obelisk,
        "{{MODULES_FOOTNOTE}}": modules_footnote,
        "{{SCORE_RATING}}": score_rating,
        "{{SYSTEM_INFO_1}}": system_info[0],
        "{{SYSTEM_INFO_2}}": system_info[1],
        "{{SYSTEM_INFO_3}}": system_info[2],
        "{{TIMESTAMP}}": safe_timestamp,
        "{{RULESET_VERSION}}": safe_ruleset_version,
        "{{TARGET_SYSTEM}}": safe_target_system,
        "{{ATLAS_VERSION}}": __version__,
        "{{PRIORITY_ACTIONS}}": priority_html,
        "{{ACTION_COUNT}}": str(action_count),
        # Diff-specific placeholders
        "{{ORGANIZATION_NAME}}": safe_org_name,
        "{{BASELINE_NAME}}": safe_baseline_name,
        "{{BASELINE_DATE}}": safe_baseline_date,
        "{{CURRENT_NAME}}": safe_current_name,
        "{{CURRENT_DATE}}": safe_current_date,
        "{{CHANGED_COUNT}}": str(changed_count),
        "{{PASS_DELTA}}": _format_stat_delta(pass_count - b_pass),
        "{{FAIL_DELTA}}": _format_stat_delta(fail_count - b_fail, invert=True),
        "{{SKIP_DELTA}}": _format_stat_delta(skip_count - b_skip),
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    # Inject diff-specific CSS and JS before </head>
    diff_styles = _build_diff_styles()
    html = html.replace("</head>", f"{diff_styles}\n</head>")

    diff_script = _build_diff_script()
    html = html.replace("</body>", f"{diff_script}\n</body>")

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

def _format_delta(delta: float) -> str:
    """Format a percentage-point delta with sign and arrow"""
    if delta > 0:
        return f"+{delta:.1f}pp ▲"
    if delta < 0:
        return f"{delta:.1f}pp ▼"
    return "0pp -"

def _format_stat_delta(delta: int, *, invert: bool = False) -> str:
    """
    Build an HTML delta chip for stat cards.

    Args:
        delta:  Numeric change (latest - baseline)
        invert: If True, positive delta is bad (used for fail count
                where more failures = regression)
    """
    if delta == 0:
        return '<div class="stat-delta neutral">0 —</div>'

    if delta > 0:
        css = "regressed" if invert else "improved"
        return f'<div class="stat-delta {css}">+{delta} ▲</div>'

    css = "improved" if invert else "regressed"
    return f'<div class="stat-delta {css}">{delta} ▼</div>'

def _generate_modules_footer(modules_ran: list[str] | None) -> tuple[str, bool]:
    """Generate a simple string showing which modules ran"""
    if modules_ran is None:
        return "Modules: Unknown", False

    if modules_ran == ["all"]:
        return "Modules: All default modules collected", False

    # Join the list into a readable string
    return f"Modules: {', '.join(modules_ran)}", True

def _build_diff_styles() -> str:
    """Additional CSS injected into the template for diff-specific pills"""
    return """
<style>
  /* Diff change-type pills */
  .pill.fixed {
    color: #1b5e20;
    background: #c8e6c9;
    border-color: #a5d6a7;
  }
  .pill.regressed {
    color: #b71c1c;
    background: #ffcdd2;
    border-color: #ef9a9a;
  }
  .pill.unchanged {
    color: #616161;
    background: #f5f5f5;
    border-color: #e0e0e0;
  }
  .pill.new-rule {
    color: #0d47a1;
    background: #bbdefb;
    border-color: #90caf9;
  }
  .pill.removed {
    color: #4a148c;
    background: #e1bee7;
    border-color: #ce93d8;
  }
  .pill.changed {
    color: #e65100;
    background: #ffe0b2;
    border-color: #ffcc80;
  }
  .pill.dash {
    color: #9e9e9e;
    background: #fafafa;
    border-color: #eeeeee;
  }
</style>
"""

def _build_diff_script() -> str:
    """Javascript injected after the existing pull highlighter to handle
    diff-specific values in the 'change' column and dash placeholders
    """
    return """
    <script>
    (function () {
    const diffMap = {
        "FIXED": "fixed",
        "REGRESSED": "regressed",
        "UNCHANGED": "unchanged",
        "NEW RULE": "new-rule",
        "REMOVED": "removed",
        "CHANGED": "changed",
        "SKIPPED": "skip",
        "—": "dash",
    };

    const headers = Array.from(document.querySelectorAll("table thead th"))
        .map(th => th.textContent.trim().toLowerCase());

    const diffColumns = ["change", "baseline status", "latest status"];

    document.querySelectorAll("table tbody tr").forEach(row => {
        row.querySelectorAll("td").forEach((td, colIndex) => {
        const header = headers[colIndex];
        const raw = td.textContent.trim();
        const key = raw.toUpperCase();

        // Handle the 'change' column
        if (header === "change" && diffMap[key]) {
            td.innerHTML = '<span class="pill ' + diffMap[key] + '">' + raw + '</span>';
            return;
        }

        // Handle dash placeholders in status columns
        if (diffColumns.includes(header) && raw === "—") {
            td.innerHTML = '<span class="pill dash">—</span>';
            return;
        }
        });
    });
    })();
    </script>
    """
