"""
Platform Atlas // Capture Engine
"""

from __future__ import annotations

import logging
from time import time
from collections.abc import Mapping
from socket import gethostname
from typing import Any, Callable, TypeVar, Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live

# ATLAS Imports
from platform_atlas.core._version import __version__

from platform_atlas.core.context import ctx
from platform_atlas.core.config import Config
from platform_atlas.capture.models import (
    SystemFacts,
    ResolvedModules,
    CaptureState,
    ModuleStatus
)
from platform_atlas.capture.ui import CaptureUI, WarningCapture
from platform_atlas.core import ui
from platform_atlas.core.topology import COLLECTOR_TRANSPORT
from platform_atlas.core.utils import show_premium_header
from platform_atlas.capture.extended_captures import (
    capture_application_states,
    capture_all_adapter_data,
    capture_indexes_status,
    capture_iag4_default_paths,
)
from platform_atlas.capture.utils import filter_capture_by_rules, normalize_acl_entries

# Capture Modules Registry
from platform_atlas.capture.modules_registry import build_modules_for_target

logger = logging.getLogger(__name__)

T = TypeVar('T')
console = Console()
theme = ui.theme

# =================================================
# Capture JSON Hierarchy
# =================================================
# Maps flat collector output keys to their nested destination
# in the final capture JSON structure.

CAPTURE_STRUCTURE: dict[str, str] = {
    # System
    "system":               "system",

    # MongoDB
    "mongo":                "mongo",
    "mongo_conf":           "mongo.config_file",
    "mongo_repl_status":    "mongo.repl_status",
    "mongo_repl_config":    "mongo.repl_config",
    "mongo_logs":           "mongo.log_analysis",

    # Redis
    "redis":                "redis",
    "redis_conf":           "redis.config_file",
    "redis_sentinel_conf":  "redis.sentinel_config",

    # Platform
    "platform":             "platform",
    "platform_conf":        "platform.config_file",
    "platform_logs":        "platform.log_analysis",
    "webserver_logs":       "platform.webserver_logs",
    "agmanager_size":       "platform.agmanager_size",

    # Gateway 4
    "gateway4":             "gateway4.packages",
    "gateway4_sync_config": "gateway4.sync_config",
    "gateway4_db_config":   "gateway4.db_config",
    "gateway4_db_sizes":    "gateway4.db_sizes",
    "gateway4_conf":        "gateway4.config_file",
    "gateway4_api":         "gateway4",

    # Gateway 5
    "gateway5":             "gateway5",
    "iagctl_checks":        "gateway5.iagctl",

    # Kubernetes
    "kubernetes_helm":      "kubernetes.helm_values",

    # Standalone checks
    "python_version":       "checks.python_version",
}


def reshape_capture(flat_data: dict[str, Any]) -> dict[str, Any]:
    """
    Reshape flat collector output into the nested capture hierarchy.

    Collectors dump results as flat top-level keys (e.g. "mongo_conf",
    "gateway4_db_sizes"). This function restructures them into a clean
    grouped hierarchy (e.g. "mongo.config_file", "gateway4.db_sizes").

    Unknown keys not in CAPTURE_STRUCTURE are preserved at the top level
    to avoid silently dropping data.
    """
    structured: dict[str, Any] = {}

    for flat_key, value in flat_data.items():
        dest_path = CAPTURE_STRUCTURE.get(flat_key)

        if dest_path is None:
            # Unknown key — preserve at top level
            structured[flat_key] = value
            continue

        parts = dest_path.split(".")
        target = structured
        for part in parts[:-1]:
            target = target.setdefault(part, {})

        leaf = parts[-1]
        # Merge dicts when the destination already has content
        # (e.g. "mongo" collector data + "mongo.config_file" from filesystem)
        if leaf in target and isinstance(target[leaf], dict) and isinstance(value, dict):
            target[leaf].update(value)
        else:
            target[leaf] = value

    return structured

def iter_module_functions(modules: dict, prefix: tuple = ()) -> Iterator[tuple[str, Callable]]:
    """Flatten nested module dict into (name, callable) pairs"""
    for name, val in modules.items():
        if isinstance(val, Mapping):
            yield from iter_module_functions(val, prefix + (name,))
        else:
            fullname = "_".join(prefix + (name,))
            yield fullname, val

def call_with_context(func: Callable[[], T]) -> T:
    """Execute a collector function, using context manager if available"""
    if not callable(func):
        raise TypeError(f"Expected callable, got {type(func).__name__}. "
                    f"Ensure the module registry contains only valid collector functions"
                    )

    # Get the bound instance if this is a method
    owner = getattr(func, "__self__", None)

    # Check if the owner implements the context manager protocol properly
    if owner is not None and isinstance(owner, AbstractContextManager):
        with owner:
            return func()
    return func()

def execute_module(
        name: str,
        func: Callable,
        state: CaptureState,
        results: dict[str, Any],
        manifest: dict[str, Any],
        warning_capture: WarningCapture,
        debug: bool = False,
) -> bool:
    """Execute a single capture module and update state"""
    start_time = time()

    try:
        result = call_with_context(func)

        # Treat None OR empty dict as failure
        if result is None or result == {}:
            raise ValueError("Module returned empty result")

        duration_ms = (time() - start_time) * 1000
        results[name] = result
        manifest[name] = "successful"
        state.complete_module(name, duration_ms, result)

        # Process any warnings that occurred during this module
        warning_capture.process_warnings()

        return True
    except Exception as e:
        duration_ms = (time() - start_time) * 1000
        error_msg = f"{type(e).__name__}: {e}"

        results[name] = {}
        manifest[name] = f"failed: {error_msg}"
        state.fail_module(name, error_msg, duration_ms)
        logger.debug("Module %s failed: %s", name, error_msg, exc_info=True)
        return False

def _resolve_modules(
        config: Config,
        user_modules: list[str] | None = None,
        log_since=None,
        log_until=None,
) -> ResolvedModules:
    """Discover targets, build collectors, and filter to user selection"""
    state = CaptureState()

    targets = config.targets or [{"name": "local", "transport": "local"}]
    all_modules: dict[str, Callable] = {}
    transport_map: dict[str, tuple[str, str]] = {}
    all_deferred: list[str] = []
    all_ssh_fallbacks: dict[str, Callable] = {}

    for target in targets:
        target_name = target.get("name", "local")
        target_kind = target.get("transport", "local")
        try:
            target_modules, deferred, ssh_fallbacks = build_modules_for_target(
                target, log_since=log_since, log_until=log_until
            )
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            state.errors.append((target_name, error_msg))
            logger.debug("Skipping target '%s': %s", target_name, error_msg)
            continue

        all_modules.update(target_modules)
        all_deferred.extend(deferred)
        all_ssh_fallbacks.update(ssh_fallbacks)

        for mod_name in target_modules:
            transport_map[mod_name] = (target_kind, target_name)

    if user_modules:
        invalid = set(user_modules) - set(all_modules.keys())
        if invalid:
            raise ValueError(f"Unknown modules: {', '.join(invalid)}")
        modules_to_run = {name: all_modules[name] for name in user_modules}
        is_subset = True
    else:
        modules_to_run = all_modules
        is_subset = False

    return ResolvedModules(
        modules=modules_to_run,
        transport_map=transport_map,
        is_subset=is_subset,
        deferred_ssh_modules=tuple(all_deferred),
        ssh_fallbacks=all_ssh_fallbacks,
    )

def _init_manifest() -> dict[str, Any]:
    """Build the initial manifest metadata dict"""
    return {
        "manifest": {
            "version": "atlas/1.0",
            "atlas": __version__,
            "created": str(round(time()*1000)),
            "hostname": gethostname()
        }
    }

def finalize_capture(
    structured_data: dict[str, Any],
    rules: dict[str, Any],
    ruleset: Any,
    config: Any,
    modules_ran: list[str],
) -> dict[str, Any]:
    """
    Post-process structured capture data into final capture format.

    Takes the reshaped (nested) capture data, filters by ruleset paths,
    then injects Atlas metadata and derived adapter/application data.
    """
    limited = filter_capture_by_rules(structured_data, rules)

    # ── Atlas internal metadata (under _atlas prefix) ─────────────
    system_data = structured_data.get("system", {})
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    limited["_atlas"] = {
        "system_facts": SystemFacts.capture_facts(system_data).to_dict(),
        "metadata": {
            "organization_name": config.organization_name,
            "environment": ctx().active_environment or "",
            "ruleset_id": ruleset.ruleset["id"],
            "ruleset_version": ruleset.ruleset["version"],
            "ruleset_profile": ctx().manager.get_active_profile_id() or "",
            "modules_ran": modules_ran,
            "captured_at": timestamp,
        },
    }

    # ── Derived adapter & application data ────────────────────────
    try:
        extended_adapter_data = capture_all_adapter_data(structured_data)
        adapter_mapping = {
            "versions":     "versions",
            "loggers":      "loggers",
            "states":       "states",
            "filedata":     "filedata",
            "health":       "healthdata",
            "requests":     "requestdata",
            "throttle":     "throttledata",
            "brokers":      "adapter_brokers",
            "limit_errors": "adapter_limit_errors",
        }
        adapters: dict[str, Any] = {}
        for dest_key, src_key in adapter_mapping.items():
            if src_key in extended_adapter_data:
                adapters[dest_key] = extended_adapter_data[src_key]
        if adapters:
            limited["adapters"] = adapters

        # Application states
        app_states = capture_application_states(structured_data)
        if app_states:
            limited["applications"] = {"states": app_states}
    except Exception as e:
        logger.debug("Adapter/application data extraction failed (expected for manual capture): %s", e)

    # ── Platform indexes ──────────────────────────────────────────
    try:
        indexes = capture_indexes_status(structured_data)
        if indexes:
            limited.setdefault("platform", {})["indexes_status"] = indexes
    except Exception as e:
        logger.debug("Index status extraction failed: %s", e)

    # ── Redis ACL (extracted from config_file) ────────────────────
    try:
        redis_config_file = structured_data.get("redis", {}).get("config_file", {})
        if "user" in redis_config_file:
            limited.setdefault("redis", {})["acl"] = redis_config_file["user"]
    except Exception as e:
        logger.debug("Redis ACL extraction failed: %s", e)

    # ── Redis runtime config (CONFIG GET fallback for alt_path) ──
    try:
        runtime_config = structured_data.get("redis", {}).get("runtime_config")
        if runtime_config:
            limited.setdefault("redis", {})["runtime_config"] = runtime_config
    except Exception as e:
        logger.debug("Redis runtime config passthrough failed: %s", e)

    # ── Sentinel runtime config (SENTINEL MASTERS fallback) ──────
    try:
        sentinel_runtime = structured_data.get("redis", {}).get("sentinel_runtime")
        if sentinel_runtime:
            limited.setdefault("redis", {})["sentinel_runtime"] = sentinel_runtime
    except Exception as e:
        logger.debug("Sentinel runtime config passthrough failed: %s", e)

    # ── Gateway4 API runtime config (ipsdk fallback for alt_path) ─
    try:
        gw4_runtime = structured_data.get("gateway4", {}).get("runtime_config")
        if gw4_runtime:
            limited.setdefault("gateway4", {})["runtime_config"] = gw4_runtime
        gw4_status = structured_data.get("gateway4", {}).get("api_status")
        if gw4_status:
            limited.setdefault("gateway4", {})["api_status"] = gw4_status
    except Exception as e:
        logger.debug("Gateway4 API passthrough failed: %s", e)

    # ── Replica set derivation (manual capture path) ──────────────
    try:
        mongo_data = limited.get("mongo", {})
        repl_config = structured_data.get("mongo", {}).get("repl_config")
        repl_status = structured_data.get("mongo", {}).get("repl_status")

        if repl_config and "repl_set_votes" not in mongo_data:
            # replSetGetConfig wraps in {"config": {...}}, rs.conf() does not
            members = (
                repl_config.get("config", {}).get("members")
                or repl_config.get("members")
                or []
            )
            mongo_data["repl_set_votes"] = sum(m.get("votes", 1) for m in members)
            limited["mongo"] = mongo_data

        if repl_status and "repl_set_healthy" not in mongo_data:
            healthy_states = {"PRIMARY", "SECONDARY", "ARBITER"}
            members = repl_status.get("members", [])
            mongo_data["repl_set_healthy"] = all(
                m.get("health", 1.0) == 1.0 and m.get("stateStr") in healthy_states
                for m in members
            )
            limited["mongo"] = mongo_data
    except Exception as e:
        logger.debug("Replica set derivation failed: %s", e)

    # ── Gateway4 default paths ────────────────────────────────────
    try:
        iag4_paths = capture_iag4_default_paths(structured_data)
        if iag4_paths:
            limited.setdefault("gateway4", {})["configured_paths"] = iag4_paths
    except Exception as e:
        logger.debug("Gateway4 path extraction failed: %s", e)

    # ── Log data passthrough ──────────────────────────────────────
    try:
        log_analysis = structured_data.get("platform", {}).get("log_analysis")
        if log_analysis:
            limited.setdefault("platform", {})["log_analysis"] = log_analysis

        webserver_logs = structured_data.get("platform", {}).get("webserver_logs")
        if webserver_logs:
            limited.setdefault("platform", {})["webserver_logs"] = webserver_logs

        mongo_log_analysis = structured_data.get("mongo", {}).get("log_analysis")
        if mongo_log_analysis:
            limited.setdefault("mongo", {})["log_analysis"] = mongo_log_analysis
    except Exception as e:
        logger.debug("Log data passthrough failed: %s", e)

    # ── Standalone checks passthrough ────────────────────────────
    # The checks section (python_version, etc.) contains small dicts
    # from standalone collectors.  Passthrough the full section so
    # all fields are available for current and future rules.
    try:
        checks_data = structured_data.get("checks", {})
        if checks_data:
            existing = limited.get("checks", {})
            existing.update(checks_data)
            limited["checks"] = existing
    except Exception as e:
        logger.debug("Checks passthrough failed: %s", e)

    return limited

# =================================================
# Main Capture Orchestrator
# =================================================

def run_capture(
        user_modules: list[str] | None = None,
        skip_guided: bool = False,
        skip_logs: bool = False,
        headless: bool = False,
        log_since=None,
        log_until=None,
) -> dict[str, Any]:
    """Orchestrator for capture modules"""

    # Initialize Atlas Context
    config = ctx().config
    rules = ctx().rules
    ruleset = ctx().ruleset

    # Initialize state tracking
    state = CaptureState()
    state.begin()
    capture_ui = CaptureUI(state)

    # Initialize data structures
    full_capture_json: dict[str, Any] = {}
    manifest = _init_manifest()

    with WarningCapture(state) as warning_capture:
        resolved = _resolve_modules(config, user_modules, log_since=log_since, log_until=log_until)
        state.running_subset = resolved.is_subset
        warning_capture.process_warnings()

        # Guard: if all targets failed, nothing to capture
        if not resolved.modules:
            console.print(f"\n[bold {theme.error}]No modules available to run[/bold {theme.error}]")
            if state.errors:
                console.print(f"[{theme.warning}]Target errors:[/{theme.warning}]")
                for err in state.errors:
                    console.print(f"  • {err}")
            console.print(f"\n[{theme.text_dim}]Check connectivity with 'platform-atlas preflight' and try again[/{theme.text_dim}]\n")
            return {"errors": state.errors}

        # Collect all module names first
        module_list = list(iter_module_functions(resolved.modules))

        # Skip log capture if user requests it
        if skip_logs:
            log_modules = {"platform_logs", "webserver_logs", "mongo_logs"}
            module_list = [(name, func) for name, func in module_list if name not in log_modules]
            logger.debug("Skipping log modules (--skip-logs)")

        for name, _ in module_list:
            transport_kind, target_name = resolved.transport_map.get(name, ("ssh", "unknown"))
            transport_kind = COLLECTOR_TRANSPORT.get(name, transport_kind)
            state.register_module(
                name,
                transport_type=transport_kind,
                target_name=target_name,
            )

        # Print Capture Headers
        show_premium_header()
        console.print()

        # Execute modules with Rich Live display and warning capture
        with Live(capture_ui.render(), console=console, refresh_per_second=10, transient=False) as live:
            for name, func in module_list:
                # Mark as running and update the display BEFORE executing
                state.start_module(name)
                live.update(capture_ui.render())

                execute_module(
                    name=name,
                    func=func,
                    state=state,
                    results=full_capture_json,
                    manifest=manifest,
                    warning_capture=warning_capture,
                    debug=config.debug,
                )
                live.update(capture_ui.render())

        console.print()

        # ========= VERIFY PROTOCOL-PRIMARY CONFIG DATA =========
        # Config modules (mongo_conf, redis_conf, gateway4_conf) rely on
        # protocol collectors as their primary source. If a protocol
        # collector failed to gather config data, register the conf module
        # as FAILED so guided recovery can offer manual collection.
        _PROTOCOL_CONF: dict[str, tuple[str, str, str]] = {
            # conf_module: (source_key, data_key, description)
            "mongo_conf":          ("mongo",       "config_file",      "MongoDB getCmdLineOpts"),
            "redis_conf":          ("redis",       "runtime_config",   "Redis CONFIG GET"),
            "gateway4_conf":       ("gateway4_api","runtime_config",   "Gateway4 API GET /config"),
        }

        # Only check sentinel if the deployment uses sentinels (HA2)
        try:
            if config.topology.mode.value == "ha2":
                _PROTOCOL_CONF["redis_sentinel_conf"] = (
                    "redis", "sentinel_runtime", "Redis SENTINEL MASTERS"
                )
        except Exception:
            pass

        # Kubernetes deployments don't use Gateway4 — remove unconditionally
        if config.is_kubernetes:
            _PROTOCOL_CONF.pop("gateway4_conf", None)
            # In K8s mode, platform_conf comes from values.yaml as a fallback
            # when Platform OAuth fails. Add it to the verification so the
            # capture engine knows to try the fallback.
            _PROTOCOL_CONF["platform_conf"] = (
                "platform", "health_status", "Platform OAuth API"
            )
        else:
            # Non-K8s: only check gateway4 if it's actually in the deployment
            try:
                has_gateway4 = any(
                    "gateway4" in t.get("modules", [])
                    for t in config.topology.targets
                )
                if not has_gateway4:
                    _PROTOCOL_CONF.pop("gateway4_conf", None)
            except Exception:
                # If topology access fails, be safe and remove gateway4
                _PROTOCOL_CONF.pop("gateway4_conf", None)

        _is_k8s = config.is_kubernetes
        for conf_name, (source_key, data_key, desc) in _PROTOCOL_CONF.items():
            # Skip if already registered (shouldn't happen, but guard)
            if conf_name in state.modules:
                continue
            proto_data = full_capture_json.get(source_key, {}).get(data_key)
            if proto_data:
                # Protocol collected config data — nothing to do
                continue

            # Protocol didn't collect config data — try SSH/K8s fallback
            fallback_fn = resolved.ssh_fallbacks.get(conf_name)
            if fallback_fn:
                fallback_label = "K8S/FALLBACK" if _is_k8s else "SSH/FALLBACK"
                fallback_transport = "k8s/fallback" if _is_k8s else "ssh/fallback"
                fallback_source = "values.yaml" if _is_k8s else "SSH"

                logger.info("Trying %s fallback for %s", fallback_source, conf_name)
                state.register_module(conf_name, transport_type=fallback_transport)
                state.start_module(conf_name)
                try:
                    result = fallback_fn()
                    if result:
                        full_capture_json[conf_name] = result
                        state.complete_module(conf_name, duration_ms=0)
                        console.print(
                            f"  [{theme.success}]✓[/{theme.success}] "
                            f"{conf_name:<20} "
                            f"[bold {theme.accent}]{fallback_label}[/bold {theme.accent}] "
                            f"[{theme.success}]Collected via {fallback_source} (protocol was unavailable)[/{theme.success}]"
                        )
                        continue
                    else:
                        state.fail_module(
                            conf_name,
                            f"{fallback_source} fallback returned no data",
                            duration_ms=0,
                        )
                except Exception as e:
                    state.fail_module(
                        conf_name,
                        f"{fallback_source} fallback failed: {e}",
                        duration_ms=0,
                    )
            else:
                # No SSH fallback available — register as failed for guided recovery
                state.register_module(conf_name, transport_type="protocol")
                state.start_module(conf_name)
                state.fail_module(
                    conf_name,
                    f"{desc} returned no data — manual entry available",
                    duration_ms=0,
                )

        # ========= GUIDED FALLBACK FOR FAILED MODULES =========
        if not skip_guided and not headless and state.failed_count > 0:
            from platform_atlas.capture.guided_collector import recover_failed_modules

            failed_names = [
                name for name, m in state.modules.items()
                if m.status == ModuleStatus.FAILED
            ]

            if failed_names:
                recover_failed_modules(failed_names, full_capture_json)

        # ========= POST-CAPTURE NORMALIZATION =========
        redis_conf = full_capture_json.get("redis_conf", {})
        if "user" in redis_conf:
            redis_conf["user"] = [
                [t for t in entry if not (isinstance(t, str) and t.startswith(">"))]
                for entry in normalize_acl_entries(redis_conf["user"])
            ]

        # ========= RESHAPE INTO NESTED HIERARCHY =========
        structured = reshape_capture(full_capture_json)

        # ========= FINALIZE =========
        limited_capture_json = finalize_capture(
            structured_data=structured,
            rules=rules,
            ruleset=ruleset,
            config=config,
            modules_ran=state.successful_module_names,
        )

        return limited_capture_json


if __name__ == "__main__":
    run_capture()
