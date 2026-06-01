"""Continuous-audit log watcher — Extended mode only.

For each enabled ``LogWatchEntry`` in the active continuous-audit settings,
this module:
1. Reads recent log lines from the appropriate source(s) via SSH.
2. Applies the entry's regex pattern.
3. Checks the configured threshold mode (any / count / window).
4. Returns a list of ``LogWatchAlert`` objects for entries whose threshold
   was exceeded.

The alerts are then folded into the engine's ``alerts.json`` aggregate and
``events.ndjson`` timeline alongside drift events.

TODO: Log file watching — deferred to a later version of Atlas.
      This entire module is preserved but not active. Re-enable by:
      1. Uncommenting the engine.py log-watch block
      2. Uncommenting the CLI handlers in handlers/continuous.py
      3. Uncommenting the cli.py log-watch subparser
      4. Uncommenting the WebUI routes and template section
"""

# TODO: Log file watching — implement in a later version of Atlas.
# The implementation below is complete but not wired in. See module docstring.

# from __future__ import annotations
#
# import hashlib
# import logging
# import re
# from datetime import datetime, timedelta, timezone
# from typing import Any
#
# from platform_atlas.continuous.models import (
#     LOG_WATCH_ANY,
#     LOG_WATCH_COUNT,
#     LOG_WATCH_WINDOW,
#     LogWatchEntry,
#     LogWatchAlert,
# )
#
# logger = logging.getLogger(__name__)
#
# # Maximum raw log lines to scan per watch per source.
# _MAX_LINES = 5000
#
#
# def _now_iso() -> str:
#     return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
#
#
# def _collect_log_lines(
#     log_source: str,
#     since: datetime,
#     *,
#     target_dict: dict,
# ) -> list[str]:
#     try:
#         from platform_atlas.core.transport import transport_from_config
#         from platform_atlas.capture.collectors.filesystem import FileSystemInfoCollector
#         from platform_atlas.capture.log_parser import ParserConfig, set_parser_config
#
#         transport = transport_from_config(target_dict)
#         try:
#             set_parser_config(ParserConfig(since=since))
#             fs = FileSystemInfoCollector(transport=transport)
#
#             sources: list[str] = (
#                 ["platform", "webserver", "mongodb"]
#                 if log_source == "any"
#                 else [log_source]
#             )
#             all_lines: list[str] = []
#             for src in sources:
#                 try:
#                     if src == "platform":
#                         data = fs.get_platform_logs(since=since)
#                     elif src == "webserver":
#                         data = fs.get_webserver_logs(since=since)
#                     elif src == "mongodb":
#                         data = fs.get_mongo_logs(since=since)
#                     else:
#                         continue
#                     raw = _extract_raw_lines(data)
#                     all_lines.extend(raw)
#                 except Exception as exc:
#                     logger.debug("Log-watch source '%s' collection failed: %s", src, exc)
#
#             return all_lines[:_MAX_LINES]
#         finally:
#             try:
#                 transport.close()
#             except Exception:
#                 pass
#     except Exception as exc:
#         logger.debug("Log-watch transport setup failed: %s", exc)
#         return []
#
#
# def _extract_raw_lines(log_data: Any) -> list[str]:
#     lines: list[str] = []
#     if isinstance(log_data, str):
#         lines.extend(log_data.splitlines())
#     elif isinstance(log_data, dict):
#         for v in log_data.values():
#             lines.extend(_extract_raw_lines(v))
#     elif isinstance(log_data, list):
#         for item in log_data:
#             if isinstance(item, str):
#                 lines.append(item)
#             elif isinstance(item, dict):
#                 msg = item.get("message") or item.get("msg") or item.get("line") or ""
#                 if msg:
#                     lines.append(str(msg))
#     return lines
#
#
# def _count_matches(lines: list[str], pattern: str) -> tuple[int, list[str]]:
#     try:
#         rx = re.compile(pattern, re.IGNORECASE)
#     except re.error as exc:
#         logger.warning("Log-watch pattern %r is invalid regex: %s — falling back to literal", pattern, exc)
#         rx = re.compile(re.escape(pattern), re.IGNORECASE)
#
#     matching: list[str] = []
#     for line in lines:
#         if rx.search(line):
#             matching.append(line)
#
#     samples = matching[:3]
#     return len(matching), samples
#
#
# def _is_within_window(line: str, cutoff: datetime) -> bool:
#     m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
#     if not m:
#         return True
#     try:
#         ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
#         return ts >= cutoff
#     except ValueError:
#         return True
#
#
# def _threshold_exceeded(
#     entry: LogWatchEntry,
#     lines: list[str],
# ) -> tuple[bool, int, list[str]]:
#     if entry.threshold_mode == LOG_WATCH_ANY:
#         count, samples = _count_matches(lines, entry.pattern)
#         return count > 0, count, samples
#
#     if entry.threshold_mode == LOG_WATCH_COUNT:
#         count, samples = _count_matches(lines, entry.pattern)
#         return count >= entry.threshold_count, count, samples
#
#     if entry.threshold_mode == LOG_WATCH_WINDOW:
#         cutoff = datetime.now(timezone.utc) - timedelta(minutes=entry.threshold_window_minutes)
#         windowed = [l for l in lines if _is_within_window(l, cutoff)]
#         count, samples = _count_matches(windowed, entry.pattern)
#         return count >= entry.threshold_count, count, samples
#
#     count, samples = _count_matches(lines, entry.pattern)
#     return count > 0, count, samples
#
#
# def run_log_watches(
#     settings_log_watches: tuple[LogWatchEntry, ...],
#     *,
#     target_dict: dict | None,
# ) -> list[LogWatchAlert]:
#     if not settings_log_watches or target_dict is None:
#         return []
#
#     max_window = max(
#         (e.threshold_window_minutes for e in settings_log_watches if e.enabled),
#         default=60,
#     )
#     since = datetime.now(timezone.utc) - timedelta(minutes=max(max_window, 60))
#
#     source_groups: dict[str, list[LogWatchEntry]] = {}
#     for entry in settings_log_watches:
#         if not entry.enabled or not entry.pattern:
#             continue
#         source_groups.setdefault(entry.log_source, []).append(entry)
#
#     alerts: list[LogWatchAlert] = []
#     now = _now_iso()
#
#     for log_source, entries in source_groups.items():
#         lines = _collect_log_lines(log_source, since, target_dict=target_dict)
#         if not lines:
#             logger.debug("Log-watch: no lines collected for source '%s'", log_source)
#
#         for entry in entries:
#             exceeded, count, samples = _threshold_exceeded(entry, lines)
#             if exceeded:
#                 alerts.append(LogWatchAlert(
#                     watch_id=entry.id,
#                     watch_name=entry.name,
#                     pattern=entry.pattern,
#                     log_source=log_source,
#                     severity=entry.severity,
#                     threshold_mode=entry.threshold_mode,
#                     threshold_count=entry.threshold_count,
#                     match_count=count,
#                     sample_lines=samples,
#                     detected_at=now,
#                 ))
#                 logger.info(
#                     "Log-watch '%s' triggered: %d match(es) of %r in %s logs",
#                     entry.name, count, entry.pattern, log_source,
#                 )
#
#     return alerts
#
#
# def log_watch_alert_to_drift_event(alert: LogWatchAlert, run_id: str) -> dict[str, Any]:
#     alert_id = "lw-" + hashlib.md5(
#         f"{alert.watch_id}:{alert.log_source}".encode()
#     ).hexdigest()[:12]
#
#     return {
#         "type": "log_watch",
#         "alert_id": alert_id,
#         "rule_number": f"LW-{alert.watch_id[:8].upper()}",
#         "rule_name": alert.watch_name,
#         "severity": alert.severity,
#         "path": f"logs/{alert.log_source}",
#         "previous": None,
#         "current": f"{alert.match_count} match(es) of '{alert.pattern}'",
#         "previous_run_id": "",
#         "current_run_id": run_id,
#         "detected_at": alert.detected_at,
#         "log_source": alert.log_source,
#         "pattern": alert.pattern,
#         "threshold_mode": alert.threshold_mode,
#         "match_count": alert.match_count,
#         "sample_lines": alert.sample_lines,
#     }
