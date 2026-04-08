"""
Filesystem Collector - Lightweight, read-only data collection for files

This module provides a simple way to capture file data for Platform Atlas

Example:
    >>> collector = FileSystemInfoCollector(transport=transport)
"""

from __future__ import annotations

import time
from typing import Any
import shlex
import logging
import re
import json
import yaml
logger = logging.getLogger(__name__)

from platform_atlas.core.context import ctx
from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.transport import Transport, LocalTransport
from platform_atlas.core.exceptions import CollectorError
from platform_atlas.core.paths import (
    CONF_FILE_MONGO,
    CONF_FILE_GATEWAY4,
    CONF_FILE_REDIS,
    CONF_FILE_SENTINEL,
    CONF_FILE_PLATFORM,
    PLATFORM6_AGMANAGER_PRONGHORN,
    PLATFORM6_LOG_PATH_ROOT,
    PLATFORM6_WEBSERVER_LOG_PATH,
    IAP_AGMANAGER_PRONGHORN,
    GATEWAY4_DB_MAIN,
    GATEWAY4_DB_AUDIT,
    GATEWAY4_DB_EXEC_HISTORY,
    MONGO_LOG_PATH,
)

COMPOUND_CONFIG_KEYS = frozenset({
    "client-output-buffer-limit",
    "save",
    "rename-command"
})

# Sentinel directives that include a master name as the first argument
# eg: "sentinel monitor mymaster 127.0.0.1 6379 2"
# vs. global directives like "sentinel deny-scripts-reconfig yes"
SENTINEL_MASTER_DIRECTIVES = frozenset({
    "monitor",
    "down-after-milliseconds",
    "failover-timeout",
    "parallel-syncs",
    "auth-pass",
    "auth-user",
    "notification-script",
    "client-reconfig-script",
    "known-replica",
    "known-sentinel",
})

MAX_LOG_FILES = 100
MAX_LOG_COLLECTION_SECONDS = 120
MAX_SSH_WORKERS = 2 # Don't set this too high, be kind to the SSH server

class FileSystemInfoCollector:
    """Simple local filesystem system collector"""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport or LocalTransport()
        logger.debug(
            "FileSystemInfoCollector initialized with transport: %s",
            type(self._transport).__name__
        )

    def get_mongo_conf(self) -> dict[str, Any]:
        """Mongo Configuration Reader"""
        mongo_conf = CONF_FILE_MONGO
        logger.debug("Reading mongo config: %s via %s", mongo_conf, type(self._transport).__name__)

        if not self._transport.is_exists(str(mongo_conf)):
            raise FileNotFoundError(f"Mongo config not found: {mongo_conf}")

        content = self._transport.read_file(str(mongo_conf))
        config = yaml.safe_load(content)

        # Validate that we got a dict
        if config is None:
            return {}
        if not isinstance(config, dict):
            raise ValueError(f"Expected dict from {mongo_conf}, got {type(config).__name__}")
        return config

    def get_gateway4_conf(self) -> dict[str, Any]:
        """Gateway4 Configuration Reader"""
        gateway4_conf = CONF_FILE_GATEWAY4

        if not self._transport.is_exists(str(gateway4_conf)):
            raise FileNotFoundError(f"Gateway4 config not found: {gateway4_conf}")

        content = self._transport.read_file(str(gateway4_conf))
        config = yaml.safe_load(content)

        # Validate that we got a dict
        if config is None:
            return {}
        if not isinstance(config, dict):
            raise ValueError(f"Expected dict from {gateway4_conf}, got {type(config).__name__}")
        return config

    def check_agmanager_size(self) -> int:
        """Check Filesize for pronghorn.json for AGManager"""
        config = ctx().config
        if config.legacy_profile:
            agmanager_pronghorn = IAP_AGMANAGER_PRONGHORN
        else:
            agmanager_pronghorn = PLATFORM6_AGMANAGER_PRONGHORN

        if not self._transport.is_exists(str(agmanager_pronghorn)):
            raise FileNotFoundError(f"AGManager pronghorn.json not found: {agmanager_pronghorn}")

        agmanager_pronghorn_size = self._transport.file_size(str(agmanager_pronghorn))

        if agmanager_pronghorn_size is None:
            return 0
        if not isinstance(agmanager_pronghorn_size, int):
            raise ValueError(f"Expected int from {agmanager_pronghorn}, got {type(agmanager_pronghorn_size).__name__}")
        return agmanager_pronghorn_size

    def check_gateway4_db_size(self) -> dict[str, Any]:
        """Check Filesize for Gateway4 DB"""
        db_files = {
            "main": GATEWAY4_DB_MAIN,
            "audit": GATEWAY4_DB_AUDIT,
            "exec_history": GATEWAY4_DB_EXEC_HISTORY
        }

        sizes = {}
        for name, db_path in db_files.items():
            size= self._transport.file_size(str(db_path))
            if size is None:
                logger.debug("Could not get size for %s: %s", name, db_path)
                sizes[name] = 0
            elif not isinstance(size, int):
                raise ValueError(
                    f"Expected int from {db_path}, got {type(size).__name__}"
                )
            else:
                sizes[name] = size

        return {
            "gateway4_main_db_size": sizes["main"],
            "gateway4_audit_db_size": sizes["audit"],
            "gateway4_exec_history_db_size": sizes["exec_history"],
        }

    def get_python_version(self) -> dict[str, Any]:
        """Gets the python and pip version from the Platform server"""

        checks = {
            "3.9": self._transport.run_command("command -v python3.9").ok,
            "3.11": self._transport.run_command("command -v python3.11").ok
        }

        # Find highest available python version
        available_versions = [v for v, installed in checks.items() if installed]

        if available_versions:
            from packaging.version import Version
            max_version = max(available_versions, key=Version)
        else:
            max_version = None

        return {
            "python39": checks["3.9"],
            "python311": checks["3.11"],
            "max_version": max_version,
            "available_versions": available_versions
        }

    def get_platform_logs(self) -> dict[str, Any]:
        """
        Collect and parse IAP platform logs over the transport.

        Discovers .log files under /var/log/itential/platform, reads each
        file via the transport (SSH or local), then feeds the content to
        LogParser for error/warning frequency analysis.

        Returns a dict suitable for the capture JSON under
        "platform.log_analysis".
        """
        from platform_atlas.capture.log_parser import LogParser, ParserConfig

        log_dir = str(PLATFORM6_LOG_PATH_ROOT)

        if not self._transport.is_exists(log_dir):
            raise FileNotFoundError(
                f"Platform log directory not found: {log_dir}"
            )

        # Discover .log files via shell find (works over SSH and local)
        # Find all log files in current dir under 3MB within the past week
        result = self._transport.run_command(
            f"find {shlex.quote(log_dir)} -maxdepth 1 -name '*.log' -type f -size -3M -mtime -7"
        )
        if result.return_code != 0 or not result.stdout.strip():
            raise CollectorError(f"No .log files found in {log_dir}")

        log_files = [
            f.strip() for f in result.stdout.strip().splitlines()
            if f.strip()
        ]
        logger.debug("Found %d log files in %s", len(log_files), log_dir)

        log_files = log_files[:MAX_LOG_FILES]

        file_contents: dict[str, str] = {}

        # Read log files in parallel over the same SSH connection.
        # SSH multiplexes channels, so multiple cat commands run
        # simultaneously without opening new TCP connections.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _read_one(filepath: str) -> tuple[str, str | None]:
            try:
                result = self._transport.run_command(
                    f"cat {shlex.quote(filepath)}", timeout=10
                )
                if result.ok and result.stdout:
                    return filepath, result.stdout
            except Exception as e:
                logger.debug("Skipping unreadable log file %s: %s", filepath, e)
            return filepath, None

        with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as pool:
            futures = {pool.submit(_read_one, fp): fp for fp in log_files}
            for future in as_completed(futures):
                filepath, content = future.result()
                if content:
                    file_contents[filepath] = content

        if not file_contents:
            raise CollectorError(
                f"Could not read any log files from {log_dir}"
            )

        # Parse with default Atlas config (error + warn, top-25)
        from platform_atlas.capture.log_parser import get_parser_config
        parser = LogParser(get_parser_config())
        results = parser.parse_from_text(file_contents)

        # Serialize for capture JSON
        return {
            "log_directory": log_dir,
            "files_found": len(log_files),
            "files_parsed": len(file_contents),
            "groups": {
                name: group.to_dict()
                for name, group in results.items()
            },
        }

    def get_webserver_logs(self) -> dict[str, Any]:
        """Collect and parse webserver acces logs from Platform"""
        log_path = str(PLATFORM6_WEBSERVER_LOG_PATH)

        if not self._transport.is_exists(log_path):
            raise FileNotFoundError(
                f"Webserver log not found: {log_path}"
            )

        if not self._transport.is_readable(log_path):
            raise PermissionError(
                f"Webserver log not readable: {log_path}"
            )

        result = self._transport.run_command(
            f"tail -n 50000 {shlex.quote(log_path)}", timeout=15
        )
        logger.debug(
            "webserver_logs: return_code=%s, stdout_len=%s, stderr=%r",
            result.return_code,
            len(result.stdout) if result.stdout else 0,
            (result.stderr or "")[:200],
        )
        if result.return_code != 0:
            raise CollectorError(f"Could not read webserver log: {log_path}")

        if not result.stdout.strip():
            return {"entries": []}

        entries = []
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                entries.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue

        return {"entries": entries}

    # MongoDB severity levels to capture (Fatal, Error, Warning)
    _MONGO_SEVERITY_FILTER: frozenset[str] = frozenset({"F", "E", "W"})

    # MongoDB-specific heuristic keywords for issue detection
    _MONGO_HEURISTIC_KEYWORDS: tuple[str, ...] = (
        # Replication
        "replSet", "ROLLBACK", "stepdown", "election", "heartbeat failed",
        "oplog", "repl state", "vote", "priority takeover", "catchup",
        # Storage / WiredTiger
        "WiredTiger", "corruption", "WT_PANIC", "data file", "repair",
        "out of disk", "journal", "checkpoint", "cache full", "eviction",
        # Auth / Security
        "authentication failed", "unauthorized", "access control",
        "SCRAM", "X509", "auth fail", "sasl",
        # TLS / OCSP
        "OCSP", "OCSPCertificateStatusUnknown", "Could not staple OCSP",
        "certificate", "SSL", "TLS handshake",
        # Connections / Network
        "connection refused", "socket exception", "connection reset",
        "connection accepted", "listener", "too many open",
        "connection pool", "exhausted",
        "ConnectionPoolExpired", "Dropping all pooled connections",
        "Ending connection due to bad connection status",
        "Bad HTTP response",
        # Performance
        "slow query", "COLLSCAN", "planSummary", "getmore",
        "exceeded time limit", "flow control",
        # Memory / Resources
        "OOM", "out of memory", "rlimit", "vm.max_map_count",
        "cache pressure", "dirty bytes",
        # General errors
        "crash", "fatal assertion", "fassert", "invariant",
        "exception", "assert", "stack trace", "segfault",
        "startup warnings", "Timeout was reached",
    )

    # False positives to exclude from mongo heuristic matches
    _MONGO_FALSE_POSITIVES: tuple[str, ...] = (
        "initandlisten",
        "Refreshing cluster metadata",
        "Successfully set",
    )

    def get_mongo_logs(self) -> dict[str, Any]:
        """
        Collect and parse MongoDB logs from the mongo server.

        Reads /var/log/mongodb/mongod.log via SSH, parses JSON log entries,
        filters by severity (Fatal/Error/Warning), runs keyword heuristics,
        and produces a top-10 message frequency ranking.

        Returns a dict suitable for the capture JSON under
        "mongo.log_analysis".
        """
        log_path = str(MONGO_LOG_PATH)

        if not self._transport.is_exists(log_path):
            raise FileNotFoundError(f"MongoDB log not found: {log_path}")

        if not self._transport.is_readable(log_path):
            raise PermissionError(f"MongoDB log not readable: {log_path}")

        # Read last 50,000 lines — enough for recent history without
        # pulling enormous files over SSH
        result = self._transport.run_command(
            f"tail -n 50000 {shlex.quote(log_path)}", timeout=15
        )

        # If the atlas user can't read the file directly, try sudo.
        # MongoDB logs are often owned by the mongod user with restricted
        # permissions. The transport already caches the sudo availability check.
        if result.return_code != 0 and hasattr(self._transport, "has_passwordless_sudo"):
            if self._transport.has_passwordless_sudo():
                logger.debug("Retrying MongoDB log read with sudo")
                result = self._transport._sudo_command(
                    f"tail -n 50000 {shlex.quote(log_path)}", timeout=15
                )

        if result.return_code != 0:
            raise CollectorError(f"Could not read MongoDB log: {log_path}")

        if not result.stdout.strip():
            return {
                "log_path": log_path,
                "lines_read": 0,
                "lines_matched": 0,
                "top_messages": [],
                "heuristic_matches": [],
            }

        # Build compiled patterns for heuristic scanning
        positive_re = re.compile(
            "|".join(re.escape(k) for k in self._MONGO_HEURISTIC_KEYWORDS),
            re.IGNORECASE,
        )
        negative_re = re.compile(
            "|".join(re.escape(k) for k in self._MONGO_FALSE_POSITIVES),
            re.IGNORECASE,
        )

        from collections import Counter

        lines_read = 0
        lines_matched = 0
        message_counter: Counter[str] = Counter()
        heuristic_matches: list[dict[str, Any]] = []
        earliest_ts: str | None = None
        latest_ts: str | None = None

        for raw_line in result.stdout.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue

            lines_read += 1

            # Extract severity — mongod uses single-letter codes
            severity = entry.get("s", "")
            if severity not in self._MONGO_SEVERITY_FILTER:
                continue

            lines_matched += 1

            # Extract timestamp
            ts_obj = entry.get("t", {})
            timestamp = ts_obj.get("$date") if isinstance(ts_obj, dict) else None
            if timestamp:
                if earliest_ts is None or timestamp < earliest_ts:
                    earliest_ts = timestamp
                if latest_ts is None or timestamp > latest_ts:
                    latest_ts = timestamp

            # Build a normalized message key for frequency counting
            component = entry.get("c", "")
            msg = entry.get("msg", "")
            msg_key = f"[{severity}] [{component}] {msg}"

            message_counter[msg_key] += 1

            # Heuristic keyword scan on the full message + attributes
            scan_text = msg
            attr = entry.get("attr")
            if attr:
                scan_text = f"{msg} {json.dumps(attr, default=str)}"

            if negative_re.search(scan_text):
                continue

            found = positive_re.findall(scan_text)
            if found:
                heuristic_matches.append({
                    "keywords": sorted(set(found), key=str.lower),
                    "severity": severity,
                    "component": component,
                    "message": msg[:500],
                    "timestamp": timestamp,
                    "log_id": entry.get("id"),
                })

        # Top 10 most frequent error/warning messages
        top_messages = [
            {"message": msg, "count": count}
            for msg, count in message_counter.most_common(10)
        ]

        # Deduplicate heuristic matches by message (keep first occurrence)
        seen_msgs: set[str] = set()
        unique_heuristics: list[dict[str, Any]] = []
        for match in heuristic_matches:
            if match["message"] not in seen_msgs:
                seen_msgs.add(match["message"])
                unique_heuristics.append(match)

        return {
            "log_path": log_path,
            "lines_read": lines_read,
            "lines_matched": lines_matched,
            "time_range": {
                "earliest": earliest_ts,
                "latest": latest_ts,
            },
            "top_messages": top_messages,
            "heuristic_matches": unique_heuristics,
        }

    def get_iagctl_checks(self) -> dict[str, Any]:
        """Gets iagctl version and registry info"""

        # iagctl version checks
        version_result = self._transport.run_command("iagctl version")
        version_result.check()

        raw = version_result.stdout
        if isinstance(raw, list):
            first_line = str(raw[0].strip())
        else:
            first_line = raw.strip().split("\n")[0]
        version = first_line.split(": ", 1)[1] if ": " in first_line else first_line

        # iagctl get registries
        registry_result = self._transport.run_command("iagctl get registries --raw")
        registry_result.check()

        try:
            data = json.loads(registry_result.stdout)
        except json.JSONDecodeError as e:
            raise CollectorError(
                "Invalid JSON from iagctl registries",
                details={"error": str(e)},
            )

        custom_registries = [
            r for r in data.get("registries", [])
            if not r["name"].startswith("default-")
        ]

        return {
            "version": version,
            "custom_registries": len(custom_registries)
        }

    def _coerce_value(self, val: str) -> Any:
        """Attempt to coerce a string value to its appropriate Python type"""
        if val.lower() in ("yes", "true", "on"):
            return True
        if val.lower() in ("no", "false", "off"):
            return False

        # Handle integers
        try:
            return int(val)
        except ValueError:
            pass

        # Handle floats
        try:
            return float(val)
        except ValueError:
            pass

        # Return as string if nothing else matches
        return val

    def _normalize_tokens(self, tokens: list[str]) -> Any:
        """Convert a list of string tokens to appropriate value structure"""
        match tokens:
            case []:
                return None
            case [single]:
                return self._coerce_value(single)
            case multiple:
                return [self._coerce_value(t) for t in multiple]

    def get_unformatted_config(self, service_name: str) -> dict[str, Any]:
        """Reads and Parses Unformatted Configuration Files into JSON"""
        config: dict[str, Any] = {}

        if service_name == "redis":
            config_file = CONF_FILE_REDIS
        elif service_name == "sentinel":
            config_file = CONF_FILE_SENTINEL
        elif service_name == "platform":
            config_file = CONF_FILE_PLATFORM
        else:
            return config

        if not self._transport.is_exists(str(config_file)):
            raise FileNotFoundError(
                f"{service_name.capitalize()} config file not found: {config_file}"
            )

        content = self._transport.read_file(str(config_file))
        for lineno, line in enumerate(content.splitlines(), start=1):
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            # Parse the line into key and tokens
            try:
                if "=" in line:
                    # Handle '=' delimiter (eg: "mongo_url=mongodb://...")
                    key, _, rest = line.partition("=")
                    key = key.strip()
                    rest = rest.strip()
                    tokens = shlex.split(rest) if rest else []
                else:
                    # Handle space-delimited (eq "maxmemory 1gb")
                    parts = shlex.split(line)
                    if not parts:
                        continue
                    key = parts[0]
                    tokens = parts[1:]
            except ValueError as e:
                raise ValueError(f"Parse error on line {lineno}: {e}")

            # Handle compound keys (eg: "client-output-buffer-limit normal 0 0 0")
            if key in COMPOUND_CONFIG_KEYS and len(tokens) >= 1:
                sub_key = tokens[0]
                sub_values = self._normalize_tokens(tokens[1:])

                # Initialize as dict if first occurrence
                if key not in config:
                    config[key] = {}

                # Ensure it's a dict (in case of malformed config)
                if isinstance(config[key], dict):
                    config[key][sub_key] = sub_values
                else:
                    # Key exists but isn't a dict - wrap existing and add new
                    existing = config[key]
                    config[key] = {"_default": existing, sub_key: sub_values}

            # Handle sentinel directives
            # Master-specific:  "sentinel monitor mymaster 127.0.0.1 6379 2"
            # Global:           "sentinel deny-scripts-reconfig yes"
            elif key == "sentinel" and len(tokens) >= 1:
                directive = tokens[0]

                if directive in SENTINEL_MASTER_DIRECTIVES and len(tokens) >= 2:
                    master_name = tokens[1]
                    values = self._normalize_tokens(tokens[2:])

                    config.setdefault("sentinel", {})
                    config["sentinel"].setdefault(master_name, {})

                    # Handle repeated directives (eg: multiple known-replica)
                    if directive in config["sentinel"][master_name]:
                        existing = config["sentinel"][master_name][directive]
                        if not isinstance(existing, list):
                            config["sentinel"][master_name][directive] = [existing]
                        config["sentinel"][master_name][directive].append(values)
                    else:
                        config["sentinel"][master_name][directive] = values
                else:
                    # Global sentinel directive (no master name)
                    values = self._normalize_tokens(tokens[1:])
                    config.setdefault("sentinel", {})
                    config["sentinel"].setdefault("_global", {})
                    config["sentinel"]["_global"][directive] = values

            else:
                # Standard key-value handling
                value = self._normalize_tokens(tokens)

                # Handle repeated keys by converting to list
                if key in config:
                    existing = config[key]
                    if not isinstance(existing, list):
                        config[key] = [existing]
                    config[key].append(value)
                else:
                    config[key] = value
        return config

    def preflight(self) -> CheckResult:
        """Check if configuration files are accessible"""
        service_name = "Config Files"

        files = {
            "MongoDB": CONF_FILE_MONGO,
            "Redis": CONF_FILE_REDIS,
            "Sentinel": CONF_FILE_SENTINEL,
            "Platform": CONF_FILE_PLATFORM,
            "Gateway4": CONF_FILE_GATEWAY4
        }

        missing = []
        unreadable = []

        for name, path in files.items():
            if not self._transport.is_exists(str(path)):
                missing.append(name)
            elif not self._transport.is_readable(str(path)):
                unreadable.append(name)

        if unreadable:
            return CheckResult.fail(
                service_name,
                f"Cannot read: {', '.join(unreadable)}",
                "Check file permissions"
            )

        if missing:
            # Missing files might be OK depending on environment
            return CheckResult.warn(
                service_name,
                f"Not found: {', '.join(missing)}",
                "Some collectors may be skipped"
            )

        return CheckResult.ok(service_name, f"All {len(files)} config files accessible")

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
