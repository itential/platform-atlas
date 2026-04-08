# pylint: disable=line-too-long
"""
Platform Atlas Report Renderer

Renders validation results DataFrame to styled HTML report with summary statistics
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
import re
import html as html_mod
import pandas as pd

from platform_atlas.core._version import __version__

def calculate_stats(df: pd.DataFrame, status_column: str = "status") -> dict[str, Any]:
    """Calculate summary statistics from a validation results DataFrame"""
    total = len(df)

    status_upper = df[status_column].str.upper()

    pass_count = len(status_upper[status_upper == "PASS"])
    fail_count = len(status_upper[status_upper == "FAIL"])
    skip_count = len(status_upper[status_upper.isin(["SKIP", "SKIPPED", "N/A", "NA"])])
    error_count = len(status_upper[status_upper == "ERROR"])

    # Calculate pass percentage (excluding skipped)
    evaluated = pass_count + fail_count + error_count
    if evaluated > 0:
        pass_percent = round((pass_count / evaluated * 100), 1)
    else:
        pass_percent = 0.0

    # Determine Score Rating
    if pass_percent >= 95:
        rating = "Excellent"
    elif pass_percent >= 85:
        rating = "Good"
    elif pass_percent >= 70:
        rating = "Needs Attention"
    elif pass_percent >= 50:
        rating = "Poor"
    else:
        rating = "Critical"

    return {
        "total": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "error_count": error_count,
        "pass_percent": pass_percent,
        "rating": rating,
    }

def generate_priority_actions(df: pd.DataFrame, status_column: str = "status",
                              severity_column: str = "severity",
                              max_actions: int = 5) -> tuple[str, int]:
    """
    Generate HTML for priority actions panel from failed rules
    """
    # Filter to failed rules only
    failures = df[df[status_column].str.upper() == "FAIL"].copy()

    if failures.empty:
        # Return "all clear" message
        html = '''
        <div class="no-actions">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            <p>All checks passed!<br/>No actions required</p>
        </div>
        '''
        return html, 0

    # Sort by severity (crit,warn,info)
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    failures["_severity_order"] = failures[severity_column].str.lower().map(severity_order).fillna(3)
    failures = failures.sort_values("_severity_order").head(max_actions)

    # Generate HTML for each action
    actions_html = []
    for _, row in failures.iterrows():
        severity = row.get(severity_column, "info").lower()
        rule_number = row.get("rule_number", "")
        name = row.get("name", "Unknown rule")
        recommendations = row.get("recommendations", "")

        # Properly escape variables before putting them in HTML
        safe_name = html_mod.escape(str(name))
        safe_recommendations = html_mod.escape(str(recommendations))
        safe_rule_number = html_mod.escape(str(rule_number))
        safe_severity = html_mod.escape(str(severity))

        action_html = f'''
        <div class="action-item">
            <div class="severity-dot {safe_severity}"></div>
            <div class="action-content">
                <p class="action-title">{safe_name}</p>
                <p class="action-detail">{safe_recommendations}</p>
            </div>
            <span class="action-rule">{safe_rule_number}</span>
        </div>
        '''
        actions_html.append(action_html)
    return "\n".join(actions_html), len(failures)

def generate_modules_footer(modules_ran: list[str] | None) -> tuple[str, bool]:
    """Generate a simple string showing which modules ran"""
    if modules_ran is None:
        return "Modules: Unknown", False

    if modules_ran == ["all"]:
        return "Modules: All default modules collected", False

    # Join the list into a readable string
    return f"Modules: {', '.join(modules_ran)}", True


# ─────────────── CHART DATA GENERATORS (Premium template) ─────────────── #

def generate_category_chart_data(
    df: pd.DataFrame,
    status_column: str = "status",
    category_column: str = "category",
) -> str:
    """Generate JSON data for the category donut chart.

    Returns a JSON string: [{"name", "pass", "fail", "skip", "total"}, ...]
    Safe to inject directly into a <script> block — contains no user-controlled
    strings (category names are escaped by json.dumps).
    """
    if category_column not in df.columns:
        return "[]"

    pass_values = {"PASS", "COMPLIANT", "OK", "SUCCESS", "TRUE"}
    fail_values = {"FAIL", "NON-COMPLIANT", "FALSE", "CRITICAL"}
    skip_values = {"SKIP", "SKIPPED", "N/A", "NA"}

    chart_data = []
    for category, group in df.groupby(category_column, sort=False):
        status_upper = group[status_column].str.upper()
        chart_data.append({
            "name": str(category),
            "pass": int(status_upper.isin(pass_values).sum()),
            "fail": int(status_upper.isin(fail_values).sum()),
            "skip": int(status_upper.isin(skip_values).sum()),
            "total": len(group),
        })

    return json.dumps(chart_data)


def generate_severity_chart_data(
    df: pd.DataFrame,
    status_column: str = "status",
    severity_column: str = "severity",
) -> str:
    """Generate JSON data for the severity horizontal bar chart.

    Returns a JSON string: [{"severity", "pass", "fail", "total"}, ...]
    Sorted by severity order: critical → warning → info.
    """
    if severity_column not in df.columns:
        return "[]"

    pass_values = {"PASS", "COMPLIANT", "OK", "SUCCESS", "TRUE"}
    severity_order = {"critical": 0, "warning": 1, "info": 2}

    chart_data = []
    for severity, group in df.groupby(severity_column, sort=False):
        status_upper = group[status_column].str.upper()
        passed = int(status_upper.isin(pass_values).sum())
        total = len(group)
        chart_data.append({
            "severity": str(severity).lower(),
            "pass": passed,
            "fail": total - passed,
            "total": total,
        })

    chart_data.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return json.dumps(chart_data)


# ─────────────── EXTENDED VALIDATION CHART DATA ─────────────── #

def generate_extended_chart_data(extended_results: list) -> str:
    """Generate JSON data for extended validation analytics.

    Returns a JSON string with structure:
    {
        "checks": [{"id", "name", "category", "status"}, ...],
        "by_status": {"PASS": n, "WARN": n, "FAIL": n, ...},
        "by_category": [{"name", "checks": n, "pass": n, "warn": n, "fail": n, ...}, ...]
    }
    """
    if not extended_results:
        return '{}'

    result_dicts = [
        r.to_dict() if hasattr(r, 'to_dict') else r
        for r in extended_results
    ]

    if not result_dicts:
        return '{}'

    # Individual check list
    checks = []
    for r in result_dicts:
        checks.append({
            "id": r.get("check_id", r.get("name", "unknown")),
            "name": r.get("name", "Unknown"),
            "category": r.get("category", "other"),
            "status": r.get("status", "INFO").upper(),
        })

    # Aggregate by status
    by_status: dict[str, int] = {}
    for c in checks:
        s = c["status"]
        by_status[s] = by_status.get(s, 0) + 1

    # Aggregate by category
    cat_map: dict[str, dict] = {}
    for c in checks:
        cat = c["category"]
        if cat not in cat_map:
            cat_map[cat] = {"name": cat, "checks": 0, "PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0, "SKIP": 0}
        cat_map[cat]["checks"] += 1
        s = c["status"]
        if s in cat_map[cat]:
            cat_map[cat][s] += 1

    by_category = sorted(cat_map.values(), key=lambda x: x["checks"], reverse=True)

    return json.dumps({
        "checks": checks,
        "by_status": by_status,
        "by_category": by_category,
    })


# ─────────────── EXTENDED SECTION RENDERER ─────────────── #

def generate_extended_section(extended_results: list) -> str:
    """Generate HTML for extended validation section"""
    if not extended_results:
        return ""

    # Convert results to dicts if needed
    result_dicts = [
        r.to_dict() if hasattr(r, 'to_dict') else r
        for r in extended_results
    ]

    if not result_dicts:
        return ""

    active_results = result_dicts

    html_parts = ['<div class="extended-section">']
    html_parts.append('<h2>Additional Validation</h2>')
    html_parts.append('<p class="section-desc">Additional automated checks</p>')

    # Group by category
    categories = {}
    for result in active_results:
        cat = result.get('category', 'other')
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(result)

    for category, checks in categories.items():
        html_parts.append(f'''
        <div class="collapsible-section">
            <div class="collapsible-header">
                <h3>
                    <span>{category.title()}</span>
                    <span class="count-badge">{len(checks)}</span>
                </h3>
                <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
            </div>
            <div class="collapsible-content">
                <div class="extended-checks">
        ''')

        for check in checks:
            status_class = check.get('status', 'info').lower()
            html_parts.append(f'<div class="extended-check {status_class}">')

            # Escape variable data before inserting into HTML
            safe_name = html_mod.escape(str(check.get("name", "Unknown")))
            safe_message = html_mod.escape(str(check.get("message", "")))

            # Header with name and status dot
            html_parts.append(f'<div class="check-header">')
            html_parts.append(f'<span class="check-status-dot {status_class}"></span>')
            html_parts.append(f'<span class="check-name">{safe_name}</span>')
            html_parts.append('</div>')

            # Message
            html_parts.append(f'<p class="check-message">{safe_message}</p>')

            # Details - route to specialized renderer if applicable
            details = check.get('details')
            if details:
                html_parts.append('<div class="check-details">')
                if 'error_groups' in details:
                    html_parts.append(_render_log_analysis_details(details))
                elif 'slow_endpoints' in details:
                    html_parts.append(_render_webserver_log_details(details))
                else:
                    html_parts.append(_render_details_generic(details))
                html_parts.append('</div>')

            # Add remediation if present
            remediation = check.get('remediation')
            if remediation:
                safe_remediation = html_mod.escape(str(remediation))
                html_parts.append(
                    f'<div class="remediation">'
                    f'<strong>Suggestion:</strong> {safe_remediation}'
                    f'</div>'
                )

            html_parts.append('</div>')  # Close extended-check

        # Close collapsible section (after all checks in this category)
        html_parts.append('</div>')  # Close extended-checks
        html_parts.append('</div>')  # Close collapsible-content
        html_parts.append('</div>')  # Close collapsible-section

    html_parts.append('</div>')  # Close extended-section container

    return '\n'.join(html_parts)


def render_summary_cards(
    df: pd.DataFrame,
    status_column: str = "status",
    category_column: str = "category",
) -> str:
    """Build subsystem summary cards HTML from validation results"""
    if category_column not in df.columns:
        return ""

    # Score tier thresholds -> CSS class names
    def _score_tier(pct: float) -> str:
        if pct >= 90:
            return "excellent"
        if pct >= 80:
            return "good"
        if pct >= 70:
            return "fair"
        if pct >= 50:
            return "poor"
        return "critical"

    cards: list[str] = []
    pass_values = {"PASS", "COMPLIANT", "OK", "SUCCESS", "TRUE"}

    for category, group in df.groupby(category_column, sort=False):
        total = len(group)
        skipped = group[status_column].str.upper().eq("SKIP").sum()
        effective = total - skipped
        passed = group[status_column].str.upper().isin(
            {v.upper() for v in pass_values}
        ).sum()
        pct = round((passed / effective) * 100) if effective else 0
        tier = _score_tier(pct)
        safe_name = html_mod.escape(str(category))

        cards.append(
            f'<div class="summary-card">'
            f'  <div class="card-label">{safe_name}</div>'
            f'  <div class="card-value {tier}">{pct}%</div>'
            f'  <div class="card-sub">{passed} of {effective} checks passed ({skipped} skipped)</div>'
            f'</div>'
        )

    if not cards:
        return ""

    return f'<div class="summary-grid">{"".join(cards)}</div>'

def _render_log_analysis_details(details: dict) -> str:
    """Render log analysis details with two sections:
    1. Top Repeated Messages (frequency-ranked)
    2. Heuristic Keyword Scan (known-bad pattern matches)
    """
    parts = []

    files = details.get("files_parsed", 0)
    total = details.get("total_lines", 0)
    matched = details.get("total_matched", 0)

    # ── Summary stats row ──
    parts.append(
        '<div style="display:flex; gap:1.5rem; margin-bottom:0.75rem;">'
    )
    for label, value in [
        ("Files Parsed", f"{files:,}"),
        ("Total Lines", f"{total:,}"),
        ("Total Matched", f"{matched:,}"),
    ]:
        safe_label = html_mod.escape(label)
        safe_value = html_mod.escape(str(value))
        parts.append(
            f'<div>'
            f'<strong style="font-size:0.75rem;">{safe_label}</strong><br>'
            f'<code style="font-size:0.875rem;">{safe_value}</code>'
            f'</div>'
        )
    parts.append('</div>')

    # ═══════════════════════════════════════════════════════════════
    # SECTION 1: Top Repeated Messages
    # ═══════════════════════════════════════════════════════════════
    error_groups = details.get("error_groups", [])

    parts.append(
        '<div style="margin-top:0.75rem; padding:0.5rem 0.75rem; '
        'background:var(--bg-subtle, rgba(0,0,0,0.15)); '
        'border-radius:6px 6px 0 0; border-bottom:2px solid var(--accent, #ff6b9d);">'
        '<strong style="font-size:0.8125rem;">Top Repeated Messages</strong>'
        '<span style="font-size:0.6875rem; color:var(--text-muted); margin-left:0.5rem;">'
        'Most frequent error/warning log lines by occurrence count</span>'
        '</div>'
    )

    if not error_groups:
        parts.append(
            '<div style="padding:0.75rem; border:1px solid var(--border-subtle, #ddd); '
            'border-top:none; border-radius:0 0 6px 6px;">'
            '<p class="check-info" style="margin:0;">✓ No error groups detected</p>'
            '</div>'
        )
    else:
        parts.append(
            '<div style="border:1px solid var(--border-subtle, #ddd); '
            'border-top:none; border-radius:0 0 6px 6px; overflow:hidden;">'
        )
        parts.append(
            '<table class="data-table" style="margin:0;">'
            '<thead><tr>'
            '<th style="width:22%;">Group</th>'
            '<th style="width:10%; text-align:right;">Hits</th>'
            '<th>Top Messages</th>'
            '</tr></thead><tbody>'
        )

        for group in error_groups:
            safe_name = html_mod.escape(str(group.get("name", "Unknown")))
            hit_count = group.get("matched", 0)

            top_msgs = group.get("top_messages", [])
            if top_msgs:
                msg_parts = []
                for msg in top_msgs:
                    safe_msg = html_mod.escape(str(msg.get("message", ""))[:200])
                    count = msg.get("count", 0)
                    level = msg.get("level", "")
                    safe_level = html_mod.escape(str(level).lower())

                    level_html = ""
                    if safe_level:
                        level_class = "sev-critical" if safe_level == "error" else "sev-warning"
                        level_html = (
                            f'<span class="{level_class}" '
                            f'style="font-size:0.6875rem; margin-right:0.25rem;">'
                            f'[{safe_level}]</span>'
                        )
                        safe_msg = re.sub(r'^\[(?:error|warn|info|debug|trace)\]\s*', '', safe_msg, flags=re.IGNORECASE)
                    msg_parts.append(
                        f'<div style="padding:0.1875rem 0; '
                        f'border-bottom:1px solid var(--border-subtle); '
                        f'font-size:0.75rem; line-height:1.4;">'
                        f'{level_html}'
                        f'<span style="color:var(--text-secondary);">{safe_msg}</span> '
                        f'<code style="font-size:0.625rem;">({count}x)</code>'
                        f'</div>'
                    )
                messages_html = "".join(msg_parts)
            else:
                messages_html = (
                    '<span style="color:var(--text-muted); '
                    'font-size:0.75rem;">—</span>'
                )

            parts.append(
                f'<tr>'
                f'<td style="font-weight:600; vertical-align:top;">{safe_name}</td>'
                f'<td style="text-align:right; vertical-align:top;">'
                f'<code>{hit_count}</code></td>'
                f'<td style="white-space:normal;">{messages_html}</td>'
                f'</tr>'
            )

        parts.append('</tbody></table>')
        parts.append('</div>')

    # ═══════════════════════════════════════════════════════════════
    # SECTION 2: Heuristic Keyword Scan
    # ═══════════════════════════════════════════════════════════════
    heuristic_groups = details.get("heuristic_groups", [])

    parts.append(
        '<div style="margin-top:1rem; padding:0.5rem 0.75rem; '
        'background:var(--bg-subtle, rgba(0,0,0,0.15)); '
        'border-radius:6px 6px 0 0; border-bottom:2px solid var(--accent, #ff6b9d);">'
        '<strong style="font-size:0.8125rem;">Heuristic Keyword Scan</strong>'
        '<span style="font-size:0.6875rem; color:var(--text-muted); margin-left:0.5rem;">'
        'Log lines matching known problematic patterns (connection errors, crashes, auth failures)</span>'
        '</div>'
    )

    if not heuristic_groups:
        parts.append(
            '<div style="padding:0.75rem; border:1px solid var(--border-subtle, #ddd); '
            'border-top:none; border-radius:0 0 6px 6px;">'
            '<p class="check-info" style="margin:0;">✓ No keyword matches detected</p>'
            '</div>'
        )
    else:
        parts.append(
            '<div style="border:1px solid var(--border-subtle, #ddd); '
            'border-top:none; border-radius:0 0 6px 6px; overflow:hidden;">'
        )
        parts.append(
            '<table class="data-table" style="margin:0;">'
            '<thead><tr>'
            '<th style="width:22%;">Group</th>'
            '<th style="width:10%; text-align:right;">Hits</th>'
            '<th>Keywords &amp; Examples</th>'
            '</tr></thead><tbody>'
        )

        for hgroup in heuristic_groups:
            safe_name = html_mod.escape(str(hgroup.get("name", "Unknown")))
            hit_count = hgroup.get("total_hits", 0)
            keywords = hgroup.get("keywords", [])

            kw_parts = []
            for kw_info in keywords:
                safe_kw = html_mod.escape(str(kw_info.get("keyword", "")))
                kw_count = kw_info.get("count", 0)
                examples = kw_info.get("examples", [])

                kw_parts.append(
                    f'<div style="padding:0.25rem 0; '
                    f'border-bottom:1px solid var(--border-subtle);">'
                    f'<code style="font-size:0.75rem; font-weight:600; '
                    f'color:var(--status-fail, #e74c3c);">{safe_kw}</code> '
                    f'<code style="font-size:0.625rem;">({kw_count}x)</code>'
                )
                for ex in examples:
                    safe_ex = html_mod.escape(str(ex)[:180])
                    kw_parts.append(
                        f'<div style="font-size:0.6875rem; padding-left:0.75rem; '
                        f'color:var(--text-muted); line-height:1.3; '
                        f'white-space:nowrap; overflow:hidden; text-overflow:ellipsis; '
                        f'max-width:600px;">{safe_ex}</div>'
                    )
                kw_parts.append('</div>')

            kw_html = "".join(kw_parts) if kw_parts else (
                '<span style="color:var(--text-muted); font-size:0.75rem;">—</span>'
            )

            parts.append(
                f'<tr>'
                f'<td style="font-weight:600; vertical-align:top;">{safe_name}</td>'
                f'<td style="text-align:right; vertical-align:top;">'
                f'<code>{hit_count}</code></td>'
                f'<td style="white-space:normal;">{kw_html}</td>'
                f'</tr>'
            )

        parts.append('</tbody></table>')
        parts.append('</div>')

    return "".join(parts)


def _render_webserver_log_details(details: dict) -> str:
    """Render webserver access log analysis as summary stats + endpoint tables."""
    parts = []

    total_requests = details.get("total_requests", 0)
    avg_ms = details.get("avg_response_ms", 0.0)
    slow_count = details.get("slow_requests_count", 0)
    error_count = details.get("error_count", 0)
    anonymous_count = details.get("anonymous_count", 0)

    # ── Summary stats row ──
    parts.append(
        '<div style="display:flex; gap:1.5rem; flex-wrap:wrap; margin-bottom:0.75rem;">'
    )
    for label, value in [
        ("Total Requests", f"{total_requests:,}"),
        ("Avg Response", f"{avg_ms:,.1f}ms"),
        ("Slow Requests", f"{slow_count:,}"),
        ("Errors", f"{error_count:,}"),
        ("Anonymous", f"{anonymous_count:,}"),
    ]:
        safe_label = html_mod.escape(label)
        safe_value = html_mod.escape(str(value))
        parts.append(
            f'<div>'
            f'<strong style="font-size:0.75rem;">{safe_label}</strong><br>'
            f'<code style="font-size:0.875rem;">{safe_value}</code>'
            f'</div>'
        )
    parts.append('</div>')

    # ── Status distribution ──
    status_dist = details.get("status_distribution", {})
    if status_dist:
        parts.append('<strong>Status Code Distribution:</strong>')
        parts.append(
            '<div style="display:flex; gap:0.75rem; flex-wrap:wrap; '
            'margin:0.375rem 0 0.75rem 0;">'
        )
        for code, count in sorted(status_dist.items(), key=lambda x: -x[1]):
            safe_code = html_mod.escape(str(code))
            # Color-code by status class
            if safe_code.startswith("5"):
                color = "var(--status-fail, #e74c3c)"
            elif safe_code.startswith("4"):
                color = "var(--status-warn, #f39c12)"
            elif safe_code.startswith("2"):
                color = "var(--status-pass, #27ae60)"
            else:
                color = "var(--text-secondary, #666)"
            parts.append(
                f'<div style="text-align:center;">'
                f'<code style="font-size:0.875rem; color:{color}; '
                f'font-weight:600;">{safe_code}</code><br>'
                f'<span style="font-size:0.6875rem; '
                f'color:var(--text-muted, #999);">{count:,}</span>'
                f'</div>'
            )
        parts.append('</div>')

    # ── Top Endpoints by Volume ──
    top_endpoints = details.get("top_endpoints", [])
    if top_endpoints:
        parts.append(
            '<div style="margin-top:0.75rem; padding:0.5rem 0.75rem; '
            'background:var(--bg-subtle, rgba(0,0,0,0.15)); '
            'border-radius:6px 6px 0 0; border-bottom:2px solid var(--accent, #ff6b9d);">'
            '<strong style="font-size:0.8125rem;">Top Endpoints by Volume</strong>'
            '<span style="font-size:0.6875rem; color:var(--text-muted); margin-left:0.5rem;">'
            'Most frequently called API paths</span>'
            '</div>'
        )
        parts.append(
            '<div style="border:1px solid var(--border-subtle, #ddd); '
            'border-top:none; border-radius:0 0 6px 6px; overflow:hidden;">'
        )
        parts.append(
            '<table class="data-table" style="margin:0;">'
            '<thead><tr>'
            '<th style="width:50%;">Endpoint</th>'
            '<th style="width:12%; text-align:right;">Calls</th>'
            '<th style="width:15%;">Methods</th>'
            '<th style="width:12%; text-align:right;">Avg Time</th>'
            '<th style="width:11%; text-align:right;">Errors</th>'
            '</tr></thead><tbody>'
        )

        for ep in top_endpoints:
            safe_path = html_mod.escape(str(ep.get("path", "")))
            call_count = ep.get("count", 0)
            avg_ms = ep.get("avg_ms", 0)
            error_count = ep.get("error_count", 0)
            methods = ep.get("methods", {})

            # Format methods as colored badges
            method_badges = []
            for method, mcount in sorted(methods.items(), key=lambda x: -x[1]):
                safe_method = html_mod.escape(str(method))
                method_badges.append(
                    f'<code style="font-size:0.6875rem; font-weight:600; '
                    f'padding:0.0625rem 0.25rem; border-radius:3px; '
                    f'background:var(--bg-subtle, #eef);">'
                    f'{safe_method}</code> '
                    f'<span style="font-size:0.625rem; '
                    f'color:var(--text-muted);">{mcount}</span>'
                )
            methods_html = " ".join(method_badges)

            # Color errors red if any
            error_style = (
                'color:var(--status-fail, #e74c3c); font-weight:600;'
                if error_count > 0
                else 'color:var(--text-muted);'
            )

            parts.append(
                f'<tr>'
                f'<td style="font-family:monospace; font-size:0.75rem; '
                f'word-break:break-all;">{safe_path}</td>'
                f'<td style="text-align:right;"><code style="font-weight:600;">'
                f'{call_count:,}</code></td>'
                f'<td>{methods_html}</td>'
                f'<td style="text-align:right;"><code>{avg_ms:,.1f}ms</code></td>'
                f'<td style="text-align:right;"><code style="{error_style}">'
                f'{error_count:,}</code></td>'
                f'</tr>'
            )

        parts.append('</tbody></table>')
        parts.append('</div>')

    # ── Slow endpoints ──
    slow_endpoints = details.get("slow_endpoints", {})

    # Normalize — may arrive as list of dicts after serialization
    if isinstance(slow_endpoints, list):
        slow_endpoints = {
            entry.get("path", entry.get("url", f"endpoint_{i}")): entry
            for i, entry in enumerate(slow_endpoints)
            if isinstance(entry, dict)
        }

    if not slow_endpoints:
        parts.append(
            '<p class="check-info" style="margin:0.75rem 0 0 0;">✓ No slow endpoints detected</p>'
        )
        return "".join(parts)

    parts.append(
        '<div style="margin-top:1rem; padding:0.5rem 0.75rem; '
        'background:var(--bg-subtle, rgba(0,0,0,0.15)); '
        'border-radius:6px 6px 0 0; border-bottom:2px solid var(--accent, #ff6b9d);">'
        '<strong style="font-size:0.8125rem;">Slow Endpoints</strong>'
        '<span style="font-size:0.6875rem; color:var(--text-muted); margin-left:0.5rem;">'
        f'Requests exceeding 5,000ms response time</span>'
        '</div>'
    )

    for path, info in slow_endpoints.items():
        safe_path = html_mod.escape(str(path))
        worst_ms = info.get("worst_ms", 0)
        hit_count = info.get("count", 0)
        examples = info.get("examples", [])

        # Endpoint header card
        parts.append(
            f'<div style="border:1px solid var(--border-subtle, #ddd); '
            f'border-radius:6px; margin:0.5rem 0; overflow:hidden;">'
            # Header bar
            f'<div style="display:flex; justify-content:space-between; '
            f'align-items:center; padding:0.5rem 0.75rem; '
            f'background:var(--bg-subtle, #f8f9fa); '
            f'border-bottom:1px solid var(--border-subtle, #ddd);">'
            f'<code style="font-weight:600; font-size:0.875rem;">{safe_path}</code>'
            f'<div style="display:flex; gap:1rem; font-size:0.75rem;">'
            f'<span>Worst: <code style="font-weight:600;">{worst_ms:,.0f}ms</code></span>'
            f'<span>Hits: <code style="font-weight:600;">{hit_count}</code></span>'
            f'</div>'
            f'</div>'
        )

        # Example requests
        if examples:
            for ex in examples:
                safe_url = html_mod.escape(str(ex.get("url", "")))
                ex_ms = ex.get("total_time_ms", 0)
                method = html_mod.escape(str(ex.get("method", "?")))

                # Split URL into path and query params for readable formatting
                if "?" in safe_url:
                    url_path, query = safe_url.split("?", 1)
                    # Query is already HTML-escaped so & appears as &amp;
                    params = query.split("&amp;")
                    param_html = "".join(
                        f'<div style="padding-left:1.5rem; '
                        f'word-break:break-all; overflow-wrap:break-word; '
                        f'color:var(--text-secondary, #555);">'
                        f'{"?" if i == 0 else "&amp;"}'
                        f'{p}</div>'
                        for i, p in enumerate(params)
                    )
                    url_html = (
                        f'<div style="word-break:break-all; '
                        f'overflow-wrap:break-word; '
                        f'color:var(--text-primary, #333);">{url_path}</div>'
                        f'{param_html}'
                    )
                else:
                    url_html = (
                        f'<div style="word-break:break-all; '
                        f'overflow-wrap:break-word; '
                        f'color:var(--text-primary, #333);">{safe_url}</div>'
                    )

                parts.append(
                    f'<div style="padding:0.5rem 0.75rem; '
                    f'border-bottom:1px solid var(--border-subtle, #eee);">'
                    f'<div style="display:flex; justify-content:space-between; '
                    f'align-items:baseline; margin-bottom:0.25rem;">'
                    f'<code style="font-size:0.6875rem; font-weight:600; '
                    f'padding:0.125rem 0.375rem; border-radius:3px; '
                    f'background:var(--bg-subtle, #eef);">{method}</code>'
                    f'<code style="font-size:0.6875rem; '
                    f'color:var(--text-muted, #888);">{ex_ms:,.0f}ms</code>'
                    f'</div>'
                    f'<div style="font-family:monospace; font-size:0.75rem; '
                    f'line-height:1.5; max-width:100%; overflow:hidden;">'
                    f'{url_html}'
                    f'</div>'
                    f'</div>'
                )
        else:
            parts.append(
                '<div style="padding:0.375rem 0.75rem; font-size:0.75rem; '
                'color:var(--text-muted, #999);">No examples captured</div>'
            )

        parts.append('</div>')  # Close endpoint card
    return "".join(parts)


def _render_details_generic(details: dict) -> str:
    """Render details dict in a generic, data-agnostic way"""
    html_parts = []
    in_list = False

    for key, value in details.items():
        # Special case: "outdated" key with list of items
        if key == 'outdated' and isinstance(value, list):
            html_parts.append('<strong>Outdated Items:</strong>')
            html_parts.append('<ul>')
            for item in value:
                if isinstance(item, dict):
                    # Render dict items (like adapter versions)
                    html_parts.append(_render_outdated_item(item))
                else:
                    # Simple list item
                    html_parts.append(f'<li>{item}</li>')
            html_parts.append('</ul>')

        # Special case: "up-to-date" count
        elif key == 'up_to_date' and isinstance(value, (int, list)):
            count = len(value) if isinstance(value, list) else value
            html_parts.append(f'<p class="check-info">✓ {count} up-to-date</p>')

        # Generic case: simple key-value pairs
        elif not isinstance(value, (list, dict)):
            if not in_list:
                html_parts.append('<ul>')
                in_list = True
            safe_key = html_mod.escape(_humanize_key(str(key)))
            safe_value = html_mod.escape(str(value))
            html_parts.append(
                f'<li><strong>• {safe_key}:</strong> <code>{safe_value}</code></li>'
            )

        # Nested dict
        elif isinstance(value, dict):
            html_parts.append(f'<strong>{_humanize_key(key)}:</strong>')
            html_parts.append('<ul>')
            for sub_key, sub_value in value.items():
                safe_sub_key = html_mod.escape(_humanize_key(str(sub_key)))
                safe_sub_value = html_mod.escape(str(sub_value))
                html_parts.append(
                    f'<li><strong>{_humanize_key(safe_sub_key)}:</strong> '
                    f'<code>{safe_sub_value}</code></li>'
                )
            html_parts.append('</ul>')

    if in_list:
        html_parts.append('</ul>')

    return ''.join(html_parts)

def _render_outdated_item(item: dict) -> str:
    """Render a single outdated item (like adapter version info)"""
    if 'adapter' in item and 'installed' in item and 'latest' in item:
        safe_adapter = html_mod.escape(str(item["adapter"]))
        safe_installed = html_mod.escape(str(item["installed"]))
        safe_latest = html_mod.escape(str(item["latest"]))
        return (
            f'<li>'
            f'<strong>• {safe_adapter}</strong> '
            f'<span class="version-old">{safe_installed}</span> → '
            f'<span class="version-new">{safe_latest}</span>'
            f'</li>'
        )

    # Generic fallback for unknown dict structure
    parts = [f'{html_mod.escape(str(k))}: {html_mod.escape(str(v))}' for k, v in item.items()]
    return f'<li>{" | ".join(parts)}</li>'

def _humanize_key(key: str) -> str:
    """Convert snake_case to Title Case"""
    return key.replace('_', ' ').title()

# ─────────────── REMEDIATION GUIDE ─────────────── #
# Fixes are rendered client-side inside the rule detail modal by default.
# The knowledgebase is serialized to JSON and injected into the
# template as {{FIXES_KNOWLEDGEBASE}} for the modal JS to consume.
# Disable with --no-fixes on the CLI.

# ─────────────── ARCHITECTURE SECTION ─────────────── #

# Human-readable labels for architecture section keys
_ARCH_SECTION_LABELS = {
    "environment": "Environment",
    "platform": "Platform (IAP)",
    "gateway4": "Automation Gateway 4",
    "gateway5": "Automation Gateway 5",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "load_balancer": "Load Balancer",
    "kubernetes": "Kubernetes",
    "network_security": "Network & Security",
}


def _render_arch_value(value: Any, depth: int = 0) -> str:
    """Recursively render a value as clean HTML."""
    if isinstance(value, dict):
        rows = []
        for k, v in value.items():
            label = html_mod.escape(k.replace("_", " ").title())
            rendered = _render_arch_value(v, depth + 1)
            rows.append(
                f'<div style="display:flex; gap:0.75rem; padding:0.25rem 0; '
                f'border-bottom:1px solid var(--border-subtle); font-size:0.8125rem;">'
                f'<span style="color:var(--accent-3, var(--text-muted)); min-width:11rem; flex-shrink:0; '
                f'font-weight:500;">{label}</span>'
                f'<span style="color:var(--text-secondary);">{rendered}</span>'
                f'</div>'
            )
        return "\n".join(rows)
    elif isinstance(value, list):
        if not value:
            return '<span style="color:var(--text-ghost);">—</span>'
        items = [html_mod.escape(str(v)) for v in value]
        return ", ".join(
            f'<code style="font-family:var(--font-mono); font-size:0.75rem; '
            f'background:var(--accent-ghost); padding:0.0625rem 0.375rem; '
            f'border-radius:var(--r-sm, 4px); color:var(--accent);">{i}</code>'
            for i in items
        )
    elif isinstance(value, bool):
        if value:
            return '<span style="color:var(--status-pass); font-weight:600;">Yes</span>'
        return '<span style="color:var(--text-muted);">No</span>'
    elif value is None or value == "":
        return '<span style="color:var(--text-ghost);">—</span>'
    else:
        return html_mod.escape(str(value))


def _render_architecture_section(arch_data: dict[str, Any]) -> str:
    """Render the architecture overview as a collapsible section.

    Architecture data is a dict of section names (environment, platform,
    mongodb, etc.) to their collected key-value data.
    """
    if not arch_data:
        return ""

    # Build collapsible sub-sections for each architecture component
    subsections = []
    for section_key, section_data in arch_data.items():
        if not isinstance(section_data, dict):
            continue

        label = _ARCH_SECTION_LABELS.get(section_key, section_key.replace("_", " ").title())

        # Skip sections that were marked not present
        if section_data.get("present") is False:
            continue
        if section_data.get("deployed_on_kubernetes") is False:
            continue

        # Render all key-value pairs
        content_html = _render_arch_value(section_data)

        subsections.append(
            f'<div class="collapsible-section collapsed">'
            f'  <div class="collapsible-header">'
            f'    <h3><span>{html_mod.escape(label)}</span></h3>'
            f'    <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>'
            f'  </div>'
            f'  <div class="collapsible-content">'
            f'    <div style="padding:0.5rem 0.75rem;">{content_html}</div>'
            f'  </div>'
            f'</div>'
        )

    if not subsections:
        return ""

    section_count = len(subsections)

    return (
        '<div class="extended-section">'
        '  <div class="collapsible-section">'
        '    <div class="collapsible-header">'
        '      <h3>'
        '        <span>Architecture Overview</span>'
        f'        <span class="count-badge">{section_count} sections</span>'
        '      </h3>'
        '      <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>'
        '    </div>'
        '    <div class="collapsible-content">'
        '      <div style="padding:0.25rem;">'
        + "\n".join(subsections)
        + '      </div>'
        '    </div>'
        '  </div>'
        '</div>'
    )


# ─────────────── HTML REPORT RENDERER ─────────────── #

def render_html_report(
        df: pd.DataFrame,
        template_path: str | Path,
        output_path: str | Path | None = None,
        *,
        title: str = "Configuration Audit",
        subtitle: str = "Validation Results",
        organization_name: str = "Unknown Organization",
        status_column: str = "status",
        footer: str = "",
        system_info: list[str] | None = None,
        ruleset_version: str = "Unknown",
        target_system: str = "Unknown",
        atlas_version: str = __version__,
        table_id: str = "report-table",
        index: bool = False,
        modules_ran: list[str] | None = None,
        extended_results: list = None,
        knowledgebase: dict | None = None,
        architecture_data: dict | None = None,
) -> str:
    """Render a validation DataFrame to a styled HTML report"""

    # Load Template
    template_path = Path(template_path)
    template = template_path.read_text(encoding="utf-8")

    # Calculate statistics
    stats = calculate_stats(df, status_column)

    # Generate chart data BEFORE status renaming (needs original PASS/FAIL values)
    category_chart_json = generate_category_chart_data(df, status_column)
    severity_chart_json = generate_severity_chart_data(df, status_column)

    # Generate priority actions
    priority_actions_html, action_count = generate_priority_actions(df, status_column=status_column)

    # Generate extended section
    # Metadata: Extended Results
    if not extended_results:
        extended_results = df.attrs.get('extended_results', [])
    extended_html = generate_extended_section(extended_results or [])
    extended_chart_json = generate_extended_chart_data(extended_results or [])

    # Build knowledgebase JSON for modal injection (enabled by default, --no-fixes to disable)
    fixes_json = "{}"
    if knowledgebase:
        failed_ids = set(
            row.get("rule_number", "")
            for _, row in df[df[status_column].str.upper() == "FAIL"].iterrows()
        )
        fixes_for_modal = {}
        for rule_id in failed_ids:
            fix = knowledgebase.get(rule_id)
            if fix:
                fixes_for_modal[rule_id] = {
                    "title": fix.title,
                    "purpose": fix.purpose,
                    "how_to_fix": fix.how_to_fix,
                }
        fixes_json = json.dumps(fixes_for_modal)

    # Generate architecture section from manual collection data
    architecture_html = _render_architecture_section(architecture_data or {})

    # Generate timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Default system info
    if system_info is None:
        system_info = [
            f"Total Rules: {stats['total']}",
            f"Evaluated: {stats['pass_count'] + stats['fail_count']}",
            f"Skipped: {stats['skip_count']}",
        ]

    # Ensure we have exactly 3 system info items
    while len(system_info) < 3:
        system_info.append("")

    # Build summary cards
    summary_cards_html = render_summary_cards(df, status_column)

    # Modify Report Language for Added Professionalism
    df = df.copy()
    df["status"] = df["status"].replace({
        "PASS": "Compliant", # nosec B105
        "FAIL": "Non-Compliant",
        "SKIP": "Skipped"
    })

    # Sort DataFrame by Rule Name
    df = df.sort_values(by='rule_number')

    # Write initial dataframe export to file
    table_html = df.to_html(
        index=index,
        classes=["dataframe"],
        table_id=table_id,
        border=0,
        escape=True
    )

    # Build footer if not provided
    if not footer:
        footer = f"Generated {timestamp} • {stats['total']} rules evaluated"

    # Generate modules footer text
    modules_text, is_partial = generate_modules_footer(modules_ran)

    # Add obelisk to score if partial
    score_obelisk = "†" if is_partial else ""
    modules_footnote = f"† Score based on partial data collection ({modules_text})"

    # ===== CRITICAL: Escape ONLY user-controlled values =====
    # These are the values that could contain XSS
    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    safe_organization_name = html_mod.escape(organization_name)
    safe_footer = html_mod.escape(footer)
    safe_target_system = html_mod.escape(target_system)
    safe_ruleset_version = html_mod.escape(ruleset_version)
    safe_atlas_version = html_mod.escape(atlas_version)
    safe_timestamp = html_mod.escape(timestamp)

    # System info might contain user data (e.g., hostname)
    safe_system_info = [html_mod.escape(info) for info in system_info]

    # Replace placeholders
    replacements = {
        "{{TITLE}}": safe_title,
        "{{SUBTITLE}}": safe_subtitle,
        "{{ORGANIZATION_NAME}}": safe_organization_name,
        "{{TABLE}}": table_html,
        "{{FOOTER}}": safe_footer,
        "{{MODULES_FOOTER}}": modules_text,
        "{{PASS_COUNT}}": str(stats["pass_count"]),
        "{{FAIL_COUNT}}": str(stats["fail_count"]),
        "{{SKIP_COUNT}}": str(stats["skip_count"]),
        "{{ERROR_COUNT}}": str(stats["error_count"]),
        "{{TOTAL_RULES}}": str(stats["total"]),
        "{{EVALUATED_COUNT}}": str(stats["pass_count"] + stats["fail_count"] + stats["error_count"]),
        "{{PASS_PERCENT}}": str(int(stats["pass_percent"])),
        "{{SCORE_OBELISK}}": score_obelisk,
        "{{MODULES_FOOTNOTE}}": modules_footnote,
        "{{SCORE_RATING}}": stats["rating"],
        "{{SYSTEM_INFO_1}}": safe_system_info[0],
        "{{SYSTEM_INFO_2}}": safe_system_info[1],
        "{{SYSTEM_INFO_3}}": safe_system_info[2],
        "{{TIMESTAMP}}": safe_timestamp,
        "{{RULESET_VERSION}}": safe_ruleset_version,
        "{{TARGET_SYSTEM}}": safe_target_system,
        "{{ATLAS_VERSION}}": safe_atlas_version,
        "{{PRIORITY_ACTIONS}}": priority_actions_html,
        "{{ACTION_COUNT}}": str(action_count),
        "{{EXTENDED_SECTION}}": extended_html,
        "{{FIXES_KNOWLEDGEBASE}}": fixes_json,
        "{{ARCHITECTURE_SECTION}}": architecture_html,
        "{{SUMMARY_CARDS}}": summary_cards_html,
        "{{CATEGORY_CHART_DATA}}": category_chart_json,
        "{{SEVERITY_CHART_DATA}}": severity_chart_json,
        "{{EXTENDED_CHART_DATA}}": extended_chart_json,
    }

    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    html = pattern.sub(lambda m: replacements[m.group(0)], template)

    # Write to file if output path provided
    if output_path:
        output_path = Path(output_path)
        output_path.write_text(html, encoding="utf-8")
        os.chmod(output_path, 0o600)

    return html
