"""
ATLAS // Extended Capture Engine

Addtional capture functions paried with Extended Validation.
Extracts adapter and application metadata from Platform capture data
"""
from __future__ import annotations

from typing import Iterator

# Package ID prefixes and short-forms
_OPENSOURCE_PREFIX = "@itentialopensource/"
_ITENTIAL_PREFIX = "@itential/"

def _iter_results(
        capture_data: dict,
        status_key: str,
        prefix: str,
) -> Iterator[tuple[str, dict]]:
    """
    Yield (clean_name, item_dict) for every result matching
    the given prefix under platform.<status_key>.results.
    """
    results = (
        capture_data
        .get("platform", {})
        .get(status_key, {})
        .get("results", [])
    )
    for item in results:
        # Normalize: unwrap metadata/data wrapper if present
        if "data" in item and "metadata" in item:
            item = item["data"]

        # adapter_status uses "package_id", adapter_props uses "model"
        package_id = item.get("package_id") or item.get("model", "")
        if package_id.startswith(prefix):
            yield package_id.removeprefix(prefix), item

def deep_get(data: dict, *keys, default="unknown"):
    """Safely traverse nested dicts"""
    current = data
    for key in keys:
        if current is None:
            return default

        # Direct index into a list
        if isinstance(current, list) and isinstance(key, int):
            try:
                current = current[key]
            except IndexError:
                return default

        # Fan-out: apply a string key across every dict in a list
        elif isinstance(current, list) and isinstance(key, str):
            results = []
            for item in current:
                if isinstance(item, dict) and key in item:
                    results.append(item[key])
            current = results if results else default
            if current is default:
                return default

        # Normal dict traversal
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return default

    return current if current is not None else default

def strip_unknowns(data: dict, sentinel: str = "unknown") -> dict:
    """Remove entries with the sentinel value, recursively for nested dicts"""
    cleaned = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested = strip_unknowns(value, sentinel)
            if nested: # only keep non-empty dicts
                cleaned[key] = nested
        elif value != sentinel:
            cleaned[key] = value
    return cleaned

def capture_adapter_versions(capture_data: dict) -> dict[str, str]:
    """Map adater name -> installed version for open-source adapters"""
    return {
        name: item.get("version", "unknown")
        for name, item in _iter_results(capture_data, "adapter_status", _OPENSOURCE_PREFIX)
    }

def capture_adapter_loggers(capture_data: dict) -> dict[str, dict[str, str]]:
    """Map adapter name -> {console, file} log levels for open-source adapters"""
    loggers = {}
    for name, item in _iter_results(capture_data, "adapter_status", _OPENSOURCE_PREFIX):
        logger_config = item.get("logger")
        if logger_config:
            loggers[name] = {
                "console": logger_config.get("console", "unknown"),
                "file": logger_config.get("file", "unknown")
            }
    return loggers

def capture_adapter_states(capture_data: dict) -> dict[str, dict[str, str]]:
    """Map adapter name -> {state, connection_state} for open-source adapters"""
    states = {}
    for name, item in _iter_results(capture_data, "adapter_status", _OPENSOURCE_PREFIX):
        connection = item.get("connection")
        if connection:
            states[name] = {
                "state": item.get("state", "unknown"),
                "connection_state": connection.get("state", "unknown")
            }
    return states

def capture_application_states(capture_data: dict) -> dict[str, dict[str, str]]:
    """Map application name -> {state} for Itential applications"""
    return {
        name: {"state": item.get("state", "unknown")}
        for name, item in _iter_results(capture_data, "application_status", _ITENTIAL_PREFIX)
    }

def capture_all_adapter_data(capture_data: dict) -> dict[str, dict]:
    versions, loggers, states, filedata, healthdata = {}, {}, {}, {}, {}
    throttledata, requestdata, adapter_brokers = {}, {}, {}
    adapter_limit_errors = {}

    # Search in adapter_status
    for name, item in _iter_results(capture_data, "adapter_status", _OPENSOURCE_PREFIX):
        # Version Data
        versions[name] = deep_get(item, "version")

        loggers[name] = {
            "console": deep_get(item, "logger", "console"),
            "file": deep_get(item, "logger", "file"),
        }

        states[name] = {
            "state": deep_get(item, "state"),
            "connection_state": deep_get(item, "connection", "state"),
        }
    # Search in adapter_props (@itentialopensource)
    for name, item in _iter_results(capture_data, "adapter_props", _OPENSOURCE_PREFIX):
        filedata[name] = {
            "filename": deep_get(item, "loggerProps", "log_filename"),
            "filesize": deep_get(item, "loggerProps", "log_max_file_size"),
        }
        healthdata[name] = {
            "healthcheck_type": deep_get(item, "properties", "properties", "healthcheck", "type"),
            "healthcheck_frequency": deep_get(item, "properties", "properties", "healthcheck", "frequency"),
        }
        throttledata[name] = {
            "throttle_enabled": deep_get(item, "properties", "properties", "throttle", "throttle_enabled"),
        }
        requestdata[name] = {
            "attempt_timeout": deep_get(item, "properties", "properties", "request", "attempt_timeout"),
        }
        adapter_brokers[name] = {
            "brokers": deep_get(item, "properties", "brokers", default=[]),
        }
        adapter_limit_errors[name] = {
            "limit_retry_error": deep_get(item, "properties", "properties", "request", "limit_retry_error"),
        }
    # Search in adapter_props (@itential)
    for name, item in _iter_results(capture_data, "adapter_props", _ITENTIAL_PREFIX):
        throttledata[name] = {
            "throttle_enabled": deep_get(item, "properties", "properties", "throttle", "throttle_enabled"),
        }
        adapter_brokers[name] = {
            "brokers": deep_get(item, "properties", "brokers", default=[]),
        }
        adapter_limit_errors[name] = {
            "limit_retry_error": deep_get(item, "properties", "properties", "request", "limit_retry_error"),
        }

    raw = {
        "versions": versions,
        "loggers": loggers,
        "states": states,
        "filedata": filedata,
        "healthdata": healthdata,
        "throttledata": throttledata,
        "requestdata": requestdata,
        "adapter_brokers": adapter_brokers,
        "adapter_limit_errors": adapter_limit_errors,
    }
    return strip_unknowns(raw)

def capture_indexes_status(capture_data: dict) -> dict[str, dict]:
    """Extract per-collection index status from Platform API data"""
    return capture_data.get("platform", {}).get("indexes_status", {})

def capture_iag4_default_paths(capture_data: dict) -> dict[str, list[str]]:
    """Extract configured paths from Gateway4 SQLite config data"""
    config_rows = (
        capture_data
        .get("gateway4", {})
        .get("db_config", {})
        .get("config", [])
    )
    if not config_rows:
        return {}

    # Take first row - config table typically has one entry
    config = config_rows[0] if isinstance(config_rows, list) else config_rows

    # Only extract the path categories we need
    path_keys = ("module_path", "collection_path", "role_path", "playbook_path")
    paths: dict[str, list[str]] = {}

    for key in path_keys:
        value = config.get(key, [])
        # Normalize
        if isinstance(value, str):
            value = [value]
        if value:
            paths[key] = value

    return paths
