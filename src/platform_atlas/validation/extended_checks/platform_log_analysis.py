"""Platform Log Analysis — surfaces error/warning patterns from platform logs."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check(
    "platform_log_analysis",
    name="Platform Log Analysis",
    category=CheckCategory.LOGS,
    group=CheckGroup.LOGGING,
    requires=("platform.log_analysis",),
)
def check_platform_log_analysis(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze platform log error/warning patterns from captured log data.

    Produces two distinct report sections:
      1. Top Repeated Messages — most frequent error/warning log lines
      2. Heuristic Keyword Scan — lines matching known-bad patterns
    """
    log_data = chk.require(data, "platform.log_analysis", "platform log analysis")

    files_parsed = log_data.get("files_parsed", 0)
    groups: dict = log_data.get("groups", {})

    if not groups:
        return chk.passed(
            f"No errors or warnings detected across {files_parsed} log files",
            details={
                "files_parsed": files_parsed,
                "total_lines": 0,
                "total_matched": 0,
                "error_groups": [],
                "heuristic_groups": [],
            },
        )

    total_matched = 0
    total_parsed = 0
    error_groups: list[dict] = []
    heuristic_groups: list[dict] = []

    for group_name, group_info in groups.items():
        matched = group_info.get("total_matched", 0)
        parsed = group_info.get("total_lines_parsed", 0)
        total_matched += matched
        total_parsed += parsed

        # ── Top repeated messages (frequency-ranked) ──
        top = group_info.get("top_messages", [])
        if top:
            error_groups.append({
                "name": group_name,
                "matched": matched,
                "parsed": parsed,
                "top_messages": [
                    {
                        "level": (m["message"].split("]")[0].lstrip("[")
                                  if m["message"].startswith("[") else ""),
                        "message": m["message"][:200],
                        "count": m["count"],
                    }
                    for m in top[:10]
                ],
            })

        # ── Heuristic keyword matches ──
        heuristics = group_info.get("heuristic_matches", [])
        if heuristics:
            # Aggregate by keyword for cleaner presentation
            keyword_counts: dict[str, int] = {}
            keyword_examples: dict[str, list[str]] = {}
            for hit in heuristics:
                for kw in hit.get("keywords", []):
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                    if len(keyword_examples.get(kw, [])) < 2:
                        keyword_examples.setdefault(kw, []).append(
                            hit.get("line", "")[:200]
                        )

            heuristic_groups.append({
                "name": group_name,
                "total_hits": len(heuristics),
                "keywords": [
                    {
                        "keyword": kw,
                        "count": count,
                        "examples": keyword_examples.get(kw, []),
                    }
                    for kw, count in sorted(
                        keyword_counts.items(),
                        key=lambda x: x[1],
                        reverse=True,
                    )[:15]
                ],
            })

    # Worst offenders first
    error_groups.sort(key=lambda g: g["matched"], reverse=True)
    heuristic_groups.sort(key=lambda g: g["total_hits"], reverse=True)

    details = {
        "files_parsed": files_parsed,
        "total_lines": total_parsed,
        "total_matched": total_matched,
        "error_groups": error_groups,
        "heuristic_groups": heuristic_groups,
    }

    group_count = len(error_groups)

    if total_matched == 0:
        return chk.passed(
            f"No errors or warnings detected across {files_parsed} log files",
            details=details,
        )

    message = (
        f"{total_matched:,} error/warning entries across "
        f"{group_count} log group{'s' if group_count != 1 else ''}"
    )

    error_rate = (total_matched / total_parsed * 100) if total_parsed > 0 else 0.0

    if error_rate > 5.0:
        return chk.fail(
            message,
            details=details,
            remediation=(
                "High error rate detected in platform logs. "
                "Review the top error groups above and address recurring "
                "issues — particularly any adapter connectivity or "
                "workflow validation errors."
            ),
        )

    return chk.warn(
        message,
        details=details,
        remediation=(
            "Errors and warnings were found in platform logs. "
            "Review the groups above for recurring patterns that "
            "may indicate configuration or connectivity issues."
        ),
    )
