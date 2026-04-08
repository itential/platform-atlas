"""
ATLAS // Log Parser

Heuristic keyword analysis, top-N frequency ranking, and time-range
filtering for IAP JSON log files.

Collection over SSH is handled by FileSystemInfoCollector.get_platform_logs(),
which reads files via the transport and feeds raw text here.
"""

from __future__ import annotations

import json
import logging
import re
import functools
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ======================================================================
# Heuristic Keyword Sets
# ======================================================================

DEFAULT_KEYWORDS: list[str] = [
    # Connection / Infrastructure
    "ECONNREFUSED", "ECONNRESET", "ENOTFOUND", "ETIMEDOUT", "EPIPE",
    "unreachable", "offline", "unavailable",
    # Severity
    "fail", "fatal", "exception", "traceback", "panic",
    "critical", "segfault", "sigabrt", "crashed",
    # Auth / Permissions
    "denied", "forbidden", "unauthorized",
    # General
    "timeout", "invalid", "unexpected", "unknown",
    "could not", "missing",
    # Services
    "Channel closed", "ERROR RETURN", "maxmemory",
    "NOREPLICAS", "QueueDeclare", "ConnBlockedError",
    # Mongo
    "MongoError", "MongoServerError", "topology was destroyed",
]

DEFAULT_FALSE_POSITIVES: list[str] = [
    "received topics", "mock data", "broker array", "Adding service",
    "Passed in cache properties", "callback to sender", "ansible_facts",
    "HEALTH CHECK", "failure_fraction", "FULL STUB REQUEST", "mockdatafiles",
    "Schema failed validation", "Alarms module", "enum", "OPTIONS",
    "Ansible Playbook", "failure status of the workflow",
]


# ======================================================================
# Data Classes
# ======================================================================

@dataclass
class ParserConfig:
    """All tunables in one place."""
    levels: list[str] = field(default_factory=lambda: ["error", "warn"])
    search_type: str = "top"                      # "top" | "heuristics"
    top_n: int = 25
    max_message_length: int = 500
    heuristics_limit: int = 1_000_000
    since: datetime | None = None
    until: datetime | None = None
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    false_positives: list[str] = field(default_factory=lambda: list(DEFAULT_FALSE_POSITIVES))
    include_pattern: str | None = None
    exclude_pattern: str | None = None


@dataclass
class HeuristicMatch:
    """A single heuristic hit: which keywords fired and the raw line."""
    keywords: list[str]
    line: str

    def to_dict(self) -> dict[str, Any]:
        return {"keywords": self.keywords, "line": self.line}


@dataclass
class LogGroupResult:
    """Parsed results for one logical group of log files."""
    group_name: str
    file_count: int
    total_lines_parsed: int
    total_matched: int
    top_messages: list[tuple[str, int]]
    heuristic_matches: list[HeuristicMatch]
    earliest_timestamp: str | None = None
    latest_timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for capture JSON."""
        return {
            "group": self.group_name,
            "file_count": self.file_count,
            "total_lines_parsed": self.total_lines_parsed,
            "total_matched": self.total_matched,
            "time_range": {
                "earliest": self.earliest_timestamp,
                "latest": self.latest_timestamp,
            },
            "top_messages": [
                {"message": m, "count": c} for m, c in self.top_messages
            ],
            "heuristic_matches": [
                h.to_dict() for h in self.heuristic_matches
            ],
        }

@functools.lru_cache(maxsize=8)
def _compile_keyword_re(keywords: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)

@functools.lru_cache(maxsize=8)
def _compile_pattern_re(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)

# ======================================================================
# Core Parser
# ======================================================================

class LogParser:
    """
    Stateless log parser. Accepts pre-read text content so it works
    transparently with any transport (local, SSH).

    Usage:
        parser = LogParser(ParserConfig(levels=["error", "warn"]))
        results = parser.parse_from_text({"platform.log": content, ...})
    """

    def __init__(self, config: ParserConfig | None = None) -> None:
        self.cfg = config or ParserConfig()
        # Reuse cached compiled patterns when keywords haven't changed
        self._positive_re = _compile_keyword_re(tuple(self.cfg.keywords))
        self._negative_re = _compile_keyword_re(tuple(self.cfg.false_positives))
        # Optional single patterns — guard for None before compiling
        self._include_re = _compile_pattern_re(self.cfg.include_pattern) if self.cfg.include_pattern else None
        self._exclude_re = _compile_pattern_re(self.cfg.exclude_pattern) if self.cfg.exclude_pattern else None

    def parse_from_text(self, files: dict[str, str]) -> dict[str, LogGroupResult]:
        """
        Parse log content already read into memory via transport.

        Parameters:
            files: Mapping of {filename: raw_text_content}

        Returns:
            Grouped results keyed by group name.
        """
        grouped: dict[str, Counter] = defaultdict(Counter)
        meta: dict[str, dict] = defaultdict(lambda: {
            "file_count": 0, "total_lines": 0,
            "earliest": None, "latest": None,
        })

        for filename, content in files.items():
            gkey = self._group_key_from_name(filename)
            meta[gkey]["file_count"] += 1

            for raw_line in content.splitlines():
                obj = self._safe_json(raw_line)
                if obj is None:
                    continue

                meta[gkey]["total_lines"] += 1
                level = self._extract_level(obj)
                if level not in self.cfg.levels:
                    continue

                timestamp = self._extract_timestamp(obj)
                if not self._in_time_range(timestamp):
                    continue
                self._update_time_bounds(meta[gkey], timestamp)

                message = self._extract_message(obj, level)
                if self._include_re and not self._include_re.search(message):
                    continue
                if self._exclude_re and self._exclude_re.search(message):
                    continue

                grouped[gkey][message] += 1

        return self._build_results(grouped, meta)

    # -- Internal methods --------------------------------------------------

    @staticmethod
    def _group_key_from_name(filename: str) -> str:
        """Derive a group key from a filename (strip trailing numbers)."""
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        stem = stem.rsplit("/", 1)[-1]
        return re.sub(r"[-_]*\d+$", "", stem)

    def _build_results(self, grouped, meta) -> dict[str, LogGroupResult]:
        results: dict[str, LogGroupResult] = {}
        for gkey, counter in grouped.items():
            m = meta[gkey]

            # Always produce plain frequency ranking (top N)
            top_messages = counter.most_common(self.cfg.top_n)

            # Always run heuristic keyword scan across all messages
            heuristic_matches: list[HeuristicMatch] = []
            for msg, _ in counter.most_common(self.cfg.heuristics_limit):
                heuristic_matches.extend(self._scan_heuristics(msg))

            results[gkey] = LogGroupResult(
                group_name=gkey,
                file_count=m["file_count"],
                total_lines_parsed=m["total_lines"],
                total_matched=sum(counter.values()),
                top_messages=top_messages,
                heuristic_matches=heuristic_matches,
                earliest_timestamp=m["earliest"],
                latest_timestamp=m["latest"],
            )
        # Groups with files but zero matching lines
        for gkey, m in meta.items():
            if gkey not in results:
                results[gkey] = LogGroupResult(
                    group_name=gkey, file_count=m["file_count"],
                    total_lines_parsed=m["total_lines"], total_matched=0,
                    top_messages=[], heuristic_matches=[],
                )
        return results

    def _scan_heuristics(self, text: str) -> list[HeuristicMatch]:
        matches = []
        for line in text.splitlines():
            if self._negative_re and self._negative_re.search(line):
                continue
            found = self._positive_re.findall(line) if self._positive_re else []
            if found:
                matches.append(HeuristicMatch(
                    keywords=sorted(set(found), key=str.lower),
                    line=line.strip(),
                ))
        return matches

    @staticmethod
    def _safe_json(raw: str) -> dict | None:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _extract_level(obj: dict) -> str | None:
        try:
            return obj["@fields"]["@level"]
        except (KeyError, TypeError):
            pass
        for key in ("level", "severity", "log_level", "loglevel"):
            val = obj.get(key)
            if isinstance(val, str):
                return val.lower()
        return None

    @staticmethod
    def _extract_timestamp(obj: dict) -> str | None:
        try:
            return obj["@timestamp"]
        except KeyError:
            pass
        for key in ("timestamp", "time", "ts", "@fields"):
            val = obj.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                ts = val.get("@timestamp") or val.get("timestamp")
                if ts:
                    return ts
        return None

    def _in_time_range(self, ts_str: str | None) -> bool:
        if not self.cfg.since and not self.cfg.until:
            return True
        if ts_str is None:
            return True
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return True
        if self.cfg.since and ts < self.cfg.since:
            return False
        if self.cfg.until and ts >= self.cfg.until:
            return False
        return True

    @staticmethod
    def _update_time_bounds(meta_entry: dict, ts_str: str | None) -> None:
        if ts_str is None:
            return
        if meta_entry["earliest"] is None or ts_str < meta_entry["earliest"]:
            meta_entry["earliest"] = ts_str
        if meta_entry["latest"] is None or ts_str > meta_entry["latest"]:
            meta_entry["latest"] = ts_str

    @staticmethod
    def _extract_message(obj: dict, level: str) -> str:
        raw = ""
        try:
            raw = obj["@message"]
        except KeyError:
            raw = obj.get("message", obj.get("msg", str(obj)))
        cleaned = raw.replace("\\n", "\n").replace('\\"', "'")
        cleaned = " ".join(cleaned.split())
        cleaned = cleaned.strip('"').strip("[]")
        return f"[{level}] {cleaned}"

_active_config: ParserConfig | None = None

def set_parser_config(config: ParserConfig) -> None:
    """Set the active parser config before capture runs"""
    global _active_config
    _active_config = config

def get_parser_config() -> ParserConfig:
    """Get the active config, falling back to defaults"""
    return _active_config or ParserConfig()
