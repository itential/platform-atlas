"""Parse RULES_KNOWLEDGEBASE.md into a lookup dict keyed by rule ID."""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass

from platform_atlas.core.paths import KNOWLEDGEBASE_PATH

@dataclass(frozen=True, slots=True)
class RuleFix:
    rule_id: str
    title: str
    purpose: str
    how_to_fix: str


def load_knowledgebase(path: Path | None = None) -> dict[str, RuleFix]:
    """Parse the markdown file into {rule_id: RuleFix} lookup."""
    kb_path = path or KNOWLEDGEBASE_PATH
    if not kb_path.is_file():
        return {}

    text = kb_path.read_text(encoding="utf-8")

    # Split on rule headings: # PLAT-001: Title, # REDIS-001: Title, etc.
    rule_pattern = re.compile(
        r'^# ([A-Z]+-\d+):\s*(.+)$', re.MULTILINE
    )

    fixes: dict[str, RuleFix] = {}
    splits = rule_pattern.split(text)

    # splits[0] is preamble, then groups of (id, title, body)
    for i in range(1, len(splits) - 2, 3):
        rule_id = splits[i].strip()
        title = splits[i + 1].strip()
        body = splits[i + 2]

        purpose = _extract_section(body, "Purpose")
        how_to_fix = _extract_section(body, "How to Fix")

        fixes[rule_id] = RuleFix(
            rule_id=rule_id,
            title=title,
            purpose=purpose,
            how_to_fix=how_to_fix,
        )

    return fixes


def _extract_section(body: str, heading: str) -> str:
    """Extract content under a ## heading until the next ## or end."""
    pattern = re.compile(
        rf'^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)',
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""
