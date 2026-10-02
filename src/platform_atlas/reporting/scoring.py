"""
Platform Atlas Severity-Weighted Scoring

Shared severity-weight constants and the weighted-score calculation, used by
``report_renderer.calculate_stats`` (CLI score panel + ``session diff``) and
``reporting_engine._build_summary`` (Customer360/Salesforce JSON export +
WebUI viewmodel -> report.html hero gauge), so every surface agrees on the
same formula and weight values.

The original (unweighted) pass-rate calculation in both of those modules is
untouched — this module only adds the new severity-weighted number alongside
it, on request, for posterity/comparison.
"""

from typing import Any

# Critical failures count 5x as much as info failures, warnings 2x. Chosen so
# a single critical failure visibly outweighs a single info failure while a
# single warning failure lands close to the old unweighted score. Unknown or
# missing severities weigh like "info" rather than inflating the score.
SEVERITY_WEIGHTS: dict[str, float] = {"critical": 5, "warning": 2, "info": 1}
_DEFAULT_WEIGHT = 1

_PASS_STATUSES = {"PASS"}
_SKIP_STATUSES = {"SKIP", "SKIPPED", "N/A", "NA"}


def weighted_pass_percent(rows: list[dict[str, Any]], *, status_column: str = "status") -> float:
    """Severity-weighted equivalent of the plain pass rate.

    Same shape as the unweighted calculation — passed weight over evaluated
    weight, skips excluded from the denominator — but each row counts by its
    severity weight instead of 1, so a critical failure costs more than an
    info failure.
    """
    total_weight = 0.0
    pass_weight = 0.0
    for row in rows:
        status = str(row.get(status_column, "")).upper()
        if status in _SKIP_STATUSES:
            continue
        weight = SEVERITY_WEIGHTS.get(str(row.get("severity", "")).lower(), _DEFAULT_WEIGHT)
        total_weight += weight
        if status in _PASS_STATUSES:
            pass_weight += weight
    return round((pass_weight / total_weight * 100), 1) if total_weight else 0.0


def rating_for(percent: float) -> str:
    """Qualitative health rating for a percent score.

    Shared thresholds for both the unweighted and severity-weighted scores:
    >=95 Excellent, >=85 Good, >=70 Needs Attention, >=50 Poor, else Critical.
    """
    if percent >= 95:
        return "Excellent"
    if percent >= 85:
        return "Good"
    if percent >= 70:
        return "Needs Attention"
    if percent >= 50:
        return "Poor"
    return "Critical"
