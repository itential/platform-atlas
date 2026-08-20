"""MongoDB Log Analysis — surfaces error/warning patterns from mongod logs."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)


@check(
    "mongo_log_analysis",
    name="MongoDB Log Analysis",
    category=CheckCategory.LOGS,
    group=CheckGroup.LOGGING,
    requires=("mongo.log_analysis",),
)
def check_mongo_log_analysis(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Analyze MongoDB log error/warning patterns from captured log data.

    Produces two distinct report sections:
      1. Top Repeated Messages — most frequent error/warning log lines
      2. Heuristic Keyword Scan — lines matching known-bad patterns
    """
    log_data = chk.require(data, "mongo.log_analysis", "MongoDB log analysis")

    lines_read = log_data.get("lines_read", 0)
    lines_matched = log_data.get("lines_matched", 0)

    if lines_read == 0:
        return chk.skip("No MongoDB log data available")

    if lines_matched == 0:
        return chk.passed(
            f"No errors or warnings detected in MongoDB logs ({lines_read:,} lines scanned)",
            details={
                "files_parsed": 1,
                "total_lines": lines_read,
                "total_matched": 0,
                "error_groups": [],
                "heuristic_groups": [],
            },
        )

    # ── Top repeated messages → error_groups format ──
    top_messages = log_data.get("top_messages", [])
    error_groups: list[dict] = []

    if top_messages:
        error_groups.append({
            "name": "mongod",
            "matched": lines_matched,
            "parsed": lines_read,
            "top_messages": [
                {
                    "level": (m["message"].split("]")[0].lstrip("[")
                              if m["message"].startswith("[") else ""),
                    "message": m["message"][:200],
                    "count": m["count"],
                }
                for m in top_messages[:10]
            ],
        })

    # ── Heuristic keyword matches → heuristic_groups format ──
    heuristic_matches = log_data.get("heuristic_matches", [])
    heuristic_groups: list[dict] = []

    if heuristic_matches:
        keyword_counts: dict[str, int] = {}
        keyword_examples: dict[str, list[str]] = {}

        for hit in heuristic_matches:
            for kw in hit.get("keywords", []):
                keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
                if len(keyword_examples.get(kw, [])) < 2:
                    keyword_examples.setdefault(kw, []).append(
                        hit.get("message", "")[:200]
                    )

        heuristic_groups.append({
            "name": "mongod",
            "total_hits": len(heuristic_matches),
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

    details = {
        "files_parsed": 1,
        "total_lines": lines_read,
        "total_matched": lines_matched,
        "error_groups": error_groups,
        "heuristic_groups": heuristic_groups,
    }

    message = f"{lines_matched:,} error/warning entries in MongoDB logs"

    error_rate = (lines_matched / lines_read * 100) if lines_read > 0 else 0.0

    if error_rate > 5.0:
        return chk.fail(
            message,
            details=details,
            remediation=(
                "High error rate detected in MongoDB logs. "
                "Review the top messages above — particularly any "
                "replication, storage, or authentication errors that "
                "may indicate cluster health issues."
            ),
        )

    return chk.warn(
        message,
        details=details,
        remediation=(
            "Errors and warnings were found in MongoDB logs. "
            "Review the messages above for recurring patterns such as "
            "slow queries, replication lag, or connection issues."
        ),
    )
