"""
Platform Atlas // Capture Engine
"""

from __future__ import annotations

import logging
from time import time
from collections.abc import Mapping
from typing import Any, Callable, TypeVar, Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live

# ATLAS Imports

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
from platform_atlas.core.utils import show_premium_header, redact_capture_credentials
from platform_atlas.core.shutdown import shutdown_requested, run_cleanups, register_cleanup
from platform_atlas.core.exceptions import CaptureAborted, ConfigError
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

    # Authorization (RBAC)
    "authorization":        "authorization",

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
        state.complete_module(name, duration_ms, result)

        # Process any warnings that occurred during this module
        warning_capture.process_warnings()

        return True
    except Exception as e:
        duration_ms = (time() - start_time) * 1000
        error_msg = f"{type(e).__name__}: {e}"

        results[name] = {}
        state.fail_module(name, error_msg, duration_ms)
        logger.debug("Module %s failed: %s", name, error_msg, exc_info=True)
        return False

_K8S_OVERRIDE_FIELDS = ("kubectl_namespace", "kubectl_context", "kubeconfig_path", "values_yaml_path")


def _is_explicit_kubernetes_override(target: dict) -> bool:
    """
    True when a Kubernetes-transport target carries its own namespace/context/
    kubeconfig/values.yaml — i.e. it's an explicitly-configured extra namespace,
    not the default primary (or default optional Gateway5) node sharing the
    global config fields. Explicit-override targets never occupy a shared
    canonical module slot, regardless of role or processing order.
    """
    return target.get("transport") == "kubernetes" and any(
        target.get(field_name) for field_name in _K8S_OVERRIDE_FIELDS
    )


# Modules whose connection type depends on which transport the target uses
# (system, filesystem, etc.). Protocol collectors always use their own label
# regardless of transport. For these, preserve "local", "control_master", and
# "kubernetes" instead of overriding with "ssh" from COLLECTOR_TRANSPORT — a
# Kubernetes-collected system/gateway5 module never touches SSH and
# shouldn't be labeled in the capture UI as if it did.
_TRANSPORT_BOUND = frozenset({"system", "filesystem", "gateway4", "gateway5", "mongo_logs"})
_PRESERVE_TRANSPORT = frozenset({"local", "control_master", "kubernetes"})


def _display_transport_kind(name: str, transport_kind: str) -> str:
    """Resolve the transport label shown in the live capture UI for a module."""
    if name not in _TRANSPORT_BOUND or transport_kind not in _PRESERVE_TRANSPORT:
        return COLLECTOR_TRANSPORT.get(name, transport_kind)
    return transport_kind


def _resolve_modules(
        config: Config,
        user_modules: list[str] | None = None,
        log_since=None,
        log_until=None,
        skip_ssh_nodes: frozenset[str] | None = None,
) -> ResolvedModules:
    """Discover targets, build collectors, and filter to user selection.

    Module names are only unique *within* a role (e.g. "system", "mongo_logs"
    are produced by every node of a role). When multiple nodes of the same
    role exist (HA2 replica-set members, multiple Kubernetes namespaces of
    the same role), the primary node's data owns the canonical module slot —
    everything else is preserved separately in ``multi_target_modules``
    instead of being silently overwritten. Explicitly-namespaced Kubernetes
    targets (carrying their own namespace/context/kubeconfig/values.yaml)
    never claim a canonical slot at all, regardless of role, since they
    represent a distinct deployment the caller asked to track separately.
    Cross-role collisions between nodes that are neither of these (e.g. an
    IAP host, a Mongo host, and a Redis host all registering "system") keep
    today's behavior unchanged — last-processed wins.
    """
    target_errors: list[tuple[str, str]] = []

    targets = config.targets or [{"name": "local", "transport": "local"}]
    all_modules: dict[str, Callable] = {}
    transport_map: dict[str, tuple[str, str]] = {}
    all_deferred: list[str] = []
    all_ssh_fallbacks: dict[str, Callable] = {}
    multi_target_modules: dict[str, dict[str, Callable]] = {}

    # Tracks which target currently owns each canonical module-name slot,
    # and that owner's role/primary status, so a later same-role primary
    # can correctly reclaim a slot a non-primary node happened to claim first.
    owner_role: dict[str, str] = {}
    owner_primary: dict[str, bool] = {}

    for target in targets:
        target_name = target.get("name", "local")
        target_kind = target.get("transport", "local")
        target_role = target.get("role", "")
        target_primary = bool(target.get("primary", False))
        explicit_override = _is_explicit_kubernetes_override(target)
        try:
            target_modules, deferred, ssh_fallbacks = build_modules_for_target(
                target, log_since=log_since, log_until=log_until,
                skip_ssh_nodes=skip_ssh_nodes,
            )
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            target_errors.append((target_name, error_msg))
            logger.debug("Skipping target '%s': %s", target_name, error_msg)
            continue

        for mod_name, func in target_modules.items():
            if explicit_override:
                # Explicitly-namespaced Kubernetes target — always separate,
                # never touches the shared canonical slot.
                multi_target_modules.setdefault(target_name, {})[mod_name] = func
                continue

            existing_role = owner_role.get(mod_name)

            if existing_role is None:
                all_modules[mod_name] = func
                transport_map[mod_name] = (target_kind, target_name)
                owner_role[mod_name] = target_role
                owner_primary[mod_name] = target_primary
                continue

            if existing_role != target_role:
                # Cross-role collision (e.g. separate IAP/Mongo/Redis hosts,
                # or the default Kubernetes primary + default Gateway5 node,
                # both sharing global config) — unchanged pre-existing
                # behavior: last target processed wins.
                all_modules[mod_name] = func
                transport_map[mod_name] = (target_kind, target_name)
                owner_role[mod_name] = target_role
                owner_primary[mod_name] = target_primary
                continue

            # Same-role collision — multiple nodes of one role. Primary's
            # data stays canonical; the rest are preserved, not dropped.
            if target_primary and not owner_primary.get(mod_name, False):
                prev_kind, prev_name = transport_map[mod_name]
                multi_target_modules.setdefault(prev_name, {})[mod_name] = all_modules[mod_name]
                all_modules[mod_name] = func
                transport_map[mod_name] = (target_kind, target_name)
                owner_primary[mod_name] = True
            elif target_primary:
                # Two "primary" claimants of the same role (shouldn't
                # normally happen — _assign_primaries guarantees one per
                # role) — keep the earlier one canonical, demote this one.
                multi_target_modules.setdefault(target_name, {})[mod_name] = func
            else:
                multi_target_modules.setdefault(target_name, {})[mod_name] = func

        all_deferred.extend(deferred)
        all_ssh_fallbacks.update(ssh_fallbacks)

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
        target_errors=target_errors,
        multi_target_modules=multi_target_modules,
    )

# Module keys available exclusively in the Standard tier. Anything outside
# this set in modules_ran indicates Extended-tier data was imported.
_STANDARD_MODULES = frozenset({"platform", "gateway4_api", "authorization"})


def _infer_tier_from_modules(modules_ran: list[str], config_tier: str) -> str:
    """Return the effective tier based on which modules were captured.

    Promotes to 'extended' when any non-Standard module is present in the
    imported data, overriding a config default of 'standard'. Never demotes
    an already-extended or saas tier.
    """
    if config_tier in ("extended", "saas"):
        return config_tier
    if any(m not in _STANDARD_MODULES for m in modules_ran):
        return "extended"
    return config_tier


def finalize_capture(
    structured_data: dict[str, Any],
    rules: dict[str, Any],
    ruleset: Any,
    config: Any,
    modules_ran: list[str],
    failed_modules: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Post-process structured capture data into final capture format.

    Takes the reshaped (nested) capture data, filters by ruleset paths,
    then injects Atlas metadata and derived adapter/application data.
    """
    # Sections that ride along with the filtered output regardless of whether
    # specific rule paths reference them. These either feed the operational/
    # diff renderers or back ``alt_path`` fallbacks during validation.
    _PASSTHROUGH = (
        "authorization",
        "redis.runtime_config",
        "redis.sentinel_runtime",
        "redis.key_count",
        # Only a handful of INFO leaves (redis_version, maxmemory_policy, ...)
        # are referenced by rule paths — the redis_analysis extended check
        # needs the whole INFO dump (memory, clients, persistence,
        # replication, stats sections), so keep it wholesale.
        "redis.info",
        "gateway4.runtime_config",
        "gateway4.api_status",
        # IAG5 server config-file source: keep the whole config_file subtree
        # (incl. the application_mode record) — only rule-referenced leaves would
        # otherwise survive, dropping the mode provenance.
        "gateway5.config_file",
        "platform.log_analysis",
        "platform.webserver_logs",
        # PLAT-048 is the only rule reading this section, and it needs the
        # empty-properties default case (endpoint reached, key just absent)
        # to be distinguishable from "endpoint unreachable" — a filtered-out
        # leaf would otherwise drop the whole section and make both look the
        # same to _parent_section_exists().
        "platform.application_props",
        "mongo.log_analysis",
        # Always preserve the kubernetes section so the validation engine's
        # has_k8s_data check works even when values.yaml is absent and kubectl
        # data hasn't populated any rule-path fields yet.
        "system.kubernetes",
        # Demoted same-role/explicit-namespace module results (HA2 replica
        # extras, additional Kubernetes namespaces) — not referenced by any
        # rule path, but consumed directly by multi-target validation.
        "_multi_target",
    )
    limited = filter_capture_by_rules(
        structured_data,
        rules,
        passthrough_paths=list(_PASSTHROUGH),
    )

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
            "failed_modules": failed_modules or [],
            "captured_at": timestamp,
            "tier": _infer_tier_from_modules(modules_ran or [], config.tier),
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

    # ── Redis ACL (protocol ACL LIST primary, SSH config_file fallback) ──
    try:
        protocol_acl = structured_data.get("redis", {}).get("acl")
        if protocol_acl:
            limited.setdefault("redis", {})["acl"] = protocol_acl
        else:
            redis_config_file = structured_data.get("redis", {}).get("config_file", {})
            if "user" in redis_config_file:
                limited.setdefault("redis", {})["acl"] = redis_config_file["user"]
    except Exception as e:
        logger.debug("Redis ACL extraction failed: %s", e)

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

    # ── WiredTiger cache derivation (pymongo serverStatus only — no SSH
    # fallback exists for live cache stats, only for static config) ──────
    try:
        wt_cache = (
            structured_data.get("mongo", {})
            .get("server_status", {})
            .get("wiredTiger", {})
            .get("cache", {})
        )
        if wt_cache:
            cache_bytes_used = wt_cache.get("bytes currently in the cache")
            cache_bytes_max = wt_cache.get("maximum bytes configured")
            dirty_bytes = wt_cache.get("tracked dirty bytes in the cache")
            pages_read = wt_cache.get("pages read into cache")
            pages_requested = wt_cache.get("pages requested from the cache")

            # WT renamed this stat between server versions ("pages evicted by
            # application threads" on older WiredTiger → "page evict attempts
            # by application threads" on newer) — try both, oldest first.
            app_thread_evictions = wt_cache.get("pages evicted by application threads")
            if app_thread_evictions is None:
                app_thread_evictions = wt_cache.get("page evict attempts by application threads")

            derived: dict[str, Any] = {
                "cache_bytes_used": cache_bytes_used,
                "cache_bytes_max": cache_bytes_max,
                "app_thread_evictions": app_thread_evictions,
            }
            if cache_bytes_used is not None and cache_bytes_max:
                derived["cache_utilization_pct"] = cache_bytes_used / cache_bytes_max * 100
            if dirty_bytes is not None and cache_bytes_max:
                derived["dirty_fill_pct"] = dirty_bytes / cache_bytes_max * 100
            if pages_read is not None and pages_requested:
                derived["cache_miss_ratio_pct"] = pages_read / pages_requested * 100

            limited.setdefault("mongo", {})["wiredtiger_cache"] = derived
    except Exception as e:
        logger.debug("WiredTiger cache derivation failed: %s", e)

    # ── Gateway4 default paths ────────────────────────────────────
    try:
        iag4_paths = capture_iag4_default_paths(structured_data)
        if iag4_paths:
            limited.setdefault("gateway4", {})["configured_paths"] = iag4_paths
    except Exception as e:
        logger.debug("Gateway4 path extraction failed: %s", e)

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

    # Universal chokepoint for the written 01_capture.json (run_capture, manual
    # capture, continuous engine all funnel through here). Masks scheme://user:
    # pass@ only — host/port/path/query and the PLAT-027 '?' check are preserved.
    limited = redact_capture_credentials(limited)

    return limited

# =================================================
# Main Capture Orchestrator
# =================================================

# Specs driving the interactive retry-with-custom-path flow for each log
# module. Same pattern for all three — only the wording, env-override field,
# and collector kwarg name differ. Kept here so capture_engine, the WebUI
# service, and tests share one source of truth.
LOG_MODULE_RETRY_SPECS: dict[str, dict[str, str]] = {
    "platform_logs": {
        "label": "Platform log capture",
        "kind_label": "Platform log directory",
        "instruction": "(absolute path, e.g. /opt/itential/log) ",
        "hint": (
            "The default log directory was unreachable or contained no "
            ".log files. You can supply a custom path and retry."
        ),
        "env_field": "log_path_override",
        "collector_method": "get_platform_logs",
        "path_kwarg": "log_dir",
    },
    "webserver_logs": {
        "label": "Webserver log capture",
        "kind_label": "Webserver log file",
        "instruction": "(absolute file path, e.g. /var/log/itential/platform/webserver.log) ",
        "hint": (
            "The default webserver log file was unreachable or unreadable. "
            "You can supply a custom file path and retry."
        ),
        "env_field": "webserver_log_path_override",
        "collector_method": "get_webserver_logs",
        "path_kwarg": "log_path",
    },
    "mongo_logs": {
        "label": "MongoDB log capture",
        "kind_label": "MongoDB log file",
        "instruction": "(absolute file path, e.g. /var/log/mongodb/mongod.log) ",
        "hint": (
            "The default MongoDB log file was unreachable or unreadable. "
            "You can supply a custom file path and retry."
        ),
        "env_field": "mongo_log_path_override",
        "collector_method": "get_mongo_logs",
        "path_kwarg": "log_path",
    },
}


def retry_log_module_with_custom_path(
        *,
        module_name: str,
        target_dict: dict,
        custom_path: str,
        log_since,
        log_until,
) -> dict[str, Any]:
    """Run the collector for ``module_name`` against ``custom_path`` once.

    Pure I/O — no UI, no state mutation. Returns the collector's raw result
    so callers (CLI retry prompt, WebUI retry endpoint) can decide how to
    surface success/failure. Raises whatever the collector raises on
    failure; the caller is responsible for handling exceptions.
    """
    spec = LOG_MODULE_RETRY_SPECS.get(module_name)
    if spec is None:
        raise ValueError(f"No retry spec for module '{module_name}'")

    from platform_atlas.core.transport import transport_from_config
    from platform_atlas.capture.collectors.filesystem import FileSystemInfoCollector

    transport = None
    try:
        transport = transport_from_config(target_dict)
        fs = FileSystemInfoCollector(transport=transport)
        collector_method = getattr(fs, spec["collector_method"])
        kwargs = {
            "since": log_since,
            "until": log_until,
            spec["path_kwarg"]: custom_path,
        }
        return collector_method(**kwargs)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


def _target_for_module(
        module_name: str,
        resolved: ResolvedModules,
        config: Config,
) -> dict | None:
    """Look up the target dict that owned ``module_name`` during capture."""
    _, target_name = resolved.transport_map.get(module_name, ("ssh", ""))
    for t in (config.targets or []):
        if t.get("name") == target_name:
            return t
    return None


def find_target_for_log_module(
        module_name: str,
        config: Config,
) -> dict | None:
    """Resolve modules against ``config`` and return the target that would
    have hosted ``module_name`` during capture.

    Public wrapper around ``_resolve_modules`` + ``_target_for_module`` so
    post-capture consumers (the WebUI retry endpoint, future tooling) can
    rebuild transports without depending on private engine helpers.
    """
    resolved = _resolve_modules(config, user_modules=None)
    return _target_for_module(module_name, resolved, config)


def _retry_log_module_with_prompt(
        *,
        module_name: str,
        state: CaptureState,
        resolved: ResolvedModules,
        config: Config,
        full_capture_json: dict[str, Any],
        log_since,
        log_until,
) -> None:
    """Interactive post-capture retry flow for one failed log module.

    Triggered after main capture when ``module_name`` is in FAILED state
    and we're running interactively. Asks the user for a custom path,
    re-runs the collector, patches the flat capture JSON on success, and
    optionally persists the path to the active environment so subsequent
    captures pick it up automatically.
    """
    import questionary

    spec = LOG_MODULE_RETRY_SPECS[module_name]
    failure_msg = (state.modules[module_name].error_message or "").strip()
    console.print()
    console.print(
        f"[bold {theme.warning}]{spec['label']} failed[/bold {theme.warning}]"
    )
    if failure_msg:
        console.print(f"  [{theme.text_dim}]{failure_msg}[/{theme.text_dim}]")
    console.print(f"  [{theme.text_dim}]{spec['hint']}[/{theme.text_dim}]")

    _retry = questionary.confirm(
        f"Retry with a custom {spec['kind_label'].lower()}?",
        default=True,
    ).ask()
    if _retry is None:
        raise KeyboardInterrupt
    if not _retry:
        return

    target_dict = _target_for_module(module_name, resolved, config)
    if target_dict is None:
        _, target_name = resolved.transport_map.get(module_name, ("ssh", ""))
        console.print(
            f"  [{theme.error}]Could not locate target '{target_name}' "
            f"to retry against.[/{theme.error}]"
        )
        return

    custom_path = (questionary.text(
        f"Custom {spec['kind_label']}:",
        instruction=spec["instruction"],
    ).ask() or "").strip()
    if not custom_path:
        console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
        return

    try:
        retry_result = retry_log_module_with_custom_path(
            module_name=module_name,
            target_dict=target_dict,
            custom_path=custom_path,
            log_since=log_since,
            log_until=log_until,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"  [{theme.error}]Retry failed: {type(exc).__name__}: {exc}[/{theme.error}]"
        )
        return

    full_capture_json[module_name] = retry_result
    state.start_module(module_name)
    state.complete_module(module_name, duration_ms=0)
    console.print(
        f"  [{theme.success}]✓ Recovered {module_name} from "
        f"[bold]{custom_path}[/bold][/{theme.success}]"
    )

    # Auto-persist the working path to the active environment so the next
    # capture picks it up without re-prompting. Skipped only when there's
    # no active env (nothing to write to) — the path stayed in this run's
    # capture either way.
    active_env = getattr(config, "active_environment", None)
    if not active_env:
        console.print(
            f"  [{theme.text_dim}]No active environment — path was not "
            f"persisted. Set --env or activate one to remember this choice.[/{theme.text_dim}]"
        )
        return

    env_field = spec["env_field"]
    try:
        from platform_atlas.core.environment import get_environment_manager
        mgr = get_environment_manager()
        env = mgr.load(active_env)
        setattr(env, env_field, custom_path)
        mgr.save(env)
        console.print(
            f"  [{theme.success}]✓ Saved {env_field} to "
            f"[bold]{active_env}[/bold] — future captures will use this path "
            f"automatically.[/{theme.success}]"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"  [{theme.error}]Could not save to env: "
            f"{type(exc).__name__}: {exc}[/{theme.error}]"
        )


def run_capture(
        user_modules: list[str] | None = None,
        skip_guided: bool = False,
        skip_logs: bool = False,
        headless: bool = False,
        log_since=None,
        log_until=None,
        on_raw_capture: Callable[[dict[str, Any]], None] | None = None,
        checkpoint=None,
        skip_ssh_nodes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Orchestrator for capture modules"""

    # Shadow the module-level console with a quiet one when running headless
    # (WebUI jobs) so the Rich Live panels and progress output don't bleed into
    # the server's stdout/terminal.
    console = Console(quiet=headless)  # noqa: F841 — intentional local shadow

    # Initialize Atlas Context
    config = ctx().config
    rules = ctx().rules
    ruleset = ctx().ruleset

    # Initialize state tracking
    state = CaptureState()
    state.begin()
    capture_ui = CaptureUI(state)
    register_cleanup("capture_ui", capture_ui.stop)

    # Initialize data structures. Per-module status lives in ``state.modules``
    # (CaptureState); the parallel ``manifest`` dict that used to be built
    # here was redundant and never reached the output, so it was removed.
    full_capture_json: dict[str, Any] = {}

    # Pre-populate from checkpoint when resuming an interrupted capture
    if checkpoint is not None and checkpoint.exists:
        _ckpt_data = checkpoint.load()
        if _ckpt_data:
            full_capture_json.update(_ckpt_data)
            logger.info(
                "Resuming capture from checkpoint: %d module(s) already collected",
                len([k for k, v in _ckpt_data.items() if v]),
            )

    with WarningCapture(state) as warning_capture:
        resolved = _resolve_modules(
            config, user_modules,
            log_since=log_since, log_until=log_until,
            skip_ssh_nodes=skip_ssh_nodes,
        )
        state.running_subset = resolved.is_subset
        warning_capture.process_warnings()

        # Guard: if all targets failed, nothing to capture
        if not resolved.modules:
            console.print(f"\n[bold {theme.error}]No modules available to run[/bold {theme.error}]")
            if resolved.target_errors:
                console.print(f"[{theme.warning}]Target errors:[/{theme.warning}]")
                for err in resolved.target_errors:
                    console.print(f"  • {err}")
            console.print(f"\n[{theme.text_dim}]Check connectivity with 'platform-atlas preflight' and try again[/{theme.text_dim}]\n")
            return {"errors": resolved.target_errors}

        # Collect all module names first
        module_list = list(iter_module_functions(resolved.modules))

        # Skip log capture if user requests it
        if skip_logs:
            log_modules = {"platform_logs", "webserver_logs", "mongo_logs"}
            module_list = [(name, func) for name, func in module_list if name not in log_modules]
            logger.debug("Skipping log modules (--skip-logs)")

        for name, _ in module_list:
            transport_kind, target_name = resolved.transport_map.get(name, ("ssh", "unknown"))
            transport_kind = _display_transport_kind(name, transport_kind)
            state.register_module(
                name,
                transport_type=transport_kind,
                target_name=target_name,
            )

        # Print Capture Headers (CLI only — suppressed in headless/WebUI mode)
        if not headless:
            show_premium_header()
        console.print()

        # Execute modules with Rich Live display and warning capture.
        #
        # Targets are independent — running them in parallel across separate
        # SSH connections / protocol clients halves wall-clock time on
        # multi-node topologies. Within a target we keep order so the
        # transport (paramiko, pymongo, redis-py) doesn't have to be
        # re-entered concurrently — most aren't thread-safe per connection.
        from collections import defaultdict
        from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
        from threading import Lock

        groups: dict[str, list[tuple[str, Callable]]] = defaultdict(list)
        for name, func in module_list:
            _, target_name = resolved.transport_map.get(name, ("ssh", "local"))
            groups[target_name].append((name, func))

        results_lock = Lock()
        # Cap concurrency at 8 so a 50-node topology doesn't open 50 SSH
        # connections at once — keeps memory and FD count bounded.
        max_workers = min(8, max(1, len(groups)))

        def _run_target_group(items: list[tuple[str, Callable]]) -> None:
            for name, func in items:
                if shutdown_requested():
                    break
                with results_lock:
                    # Already collected from checkpoint — mark complete and skip
                    if name in full_capture_json and full_capture_json[name]:
                        state.start_module(name)
                        state.complete_module(name, duration_ms=0)
                        continue
                    state.start_module(name)
                # execute_module mutates ``state`` and ``results`` — both are
                # safe under the lock; the heavy I/O happens outside it.
                local_results: dict[str, Any] = {}
                execute_module(
                    name=name,
                    func=func,
                    state=state,
                    results=local_results,
                    warning_capture=warning_capture,
                    debug=config.debug,
                )
                with results_lock:
                    full_capture_json.update(local_results)
                    if checkpoint is not None and local_results.get(name):
                        try:
                            checkpoint.save(full_capture_json)
                        except Exception as _ckpt_err:
                            logger.debug("Checkpoint save failed: %s", _ckpt_err)

        with Live(capture_ui.render(), console=console, refresh_per_second=10, transient=False) as live:
            if max_workers == 1:
                # Single target — keep the simple path so semantics match
                # exactly when there's no parallelism to gain.
                for items in groups.values():
                    _run_target_group(items)
                    live.update(capture_ui.render())
                    if shutdown_requested():
                        break
            else:
                with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="atlas-cap") as ex:
                    futures = [ex.submit(_run_target_group, items) for items in groups.values()]
                    pending = set(futures)
                    while pending:
                        if shutdown_requested():
                            for _f in pending:
                                _f.cancel()
                            break
                        # Block until a worker finishes or the refresh tick
                        # elapses. Waiting on the futures themselves beats a
                        # sleep-poll loop: the UI still repaints on a steady
                        # cadence, but the last worker to finish ends the wait
                        # immediately instead of costing up to another tick.
                        _done, pending = futures_wait(pending, timeout=0.1)
                        live.update(capture_ui.render())
                    # Surface any worker exception. A target group that died
                    # outside execute_module's own error handling would
                    # otherwise leave its modules pinned at RUNNING while the
                    # capture reported success — swallowing these is how a real
                    # failure becomes an unexplained SKIP at validation time.
                    for f in futures:
                        if not f.done() or f.cancelled():
                            continue
                        exc = f.exception()
                        if exc is not None:
                            logger.debug(
                                "Capture worker raised: %s", exc, exc_info=exc
                            )
                            state.add_warning(
                                "capture",
                                f"A capture target failed unexpectedly: {exc}",
                            )
            live.update(capture_ui.render())

        console.print()

        # Check for cooperative shutdown
        if shutdown_requested():
            run_cleanups()
            raise CaptureAborted("Capture stopped by user interrupt (Ctrl-C).")

        # ========= VERIFY PROTOCOL-PRIMARY CONFIG DATA =========
        # Config modules (mongo_conf, redis_conf, gateway4_conf) rely on
        # protocol collectors as their primary source. If a protocol
        # collector failed to gather config data, register the conf module
        # as FAILED so guided recovery can offer manual collection.
        _PROTOCOL_CONF: dict[str, tuple[str, str | tuple[str, ...], str]] = {
            # conf_module: (source_key, data_key(s), description)
            "mongo_conf":          ("mongo",       "config_file",      "MongoDB getCmdLineOpts"),
            # ACL LIST is checked alongside CONFIG GET — the ACL rules
            # rules validate never appear in CONFIG GET's namespace, so a
            # working CONFIG GET must not mask a failed/denied ACL LIST.
            "redis_conf":          ("redis",       ("runtime_config", "acl"), "Redis CONFIG GET / ACL LIST"),
            "gateway4_conf":       ("gateway4_api","runtime_config",   "Gateway4 API GET /config"),
        }

        # Only check sentinel if the deployment uses sentinels (HA2).
        # ``is_ha2`` already fails safe to False when no topology is defined
        # (Standard/SaaS), so no exception guard is needed here — and wrapping
        # it in one would hide a genuine HA2 misconfiguration behind the same
        # silence as the expected no-topology case.
        if config.is_ha2:
            _PROTOCOL_CONF["redis_sentinel_conf"] = (
                "redis", "sentinel_runtime", "Redis SENTINEL MASTERS"
            )

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
            # Non-K8s: only check gateway4 if it's actually in the deployment.
            # ``config.targets`` is the accessor that yields target dicts;
            # DeploymentTopology holds TargetNode dataclasses under ``nodes``
            # and has no ``targets`` member at all. Reaching for one raised
            # AttributeError on every capture, which a broad ``except
            # Exception`` then swallowed — silently dropping gateway4_conf and
            # disabling its SSH fallback. Catch only what a missing or invalid
            # topology actually raises, so a programming error surfaces.
            try:
                has_gateway4 = any(
                    "gateway4" in (t.get("modules") or [])
                    for t in (config.targets or ())
                )
            except ConfigError as exc:
                logger.debug(
                    "Topology unavailable — skipping gateway4 conf verification: %s",
                    exc,
                )
                has_gateway4 = False
            if not has_gateway4:
                _PROTOCOL_CONF.pop("gateway4_conf", None)

        # Only verify conf data for collectors that actually ran. In Standard
        # tier, mongo and redis collectors are never registered, so mongo_conf
        # and redis_conf must not be treated as failures — they are simply not
        # part of a Standard capture.
        _ran_module_names = set(resolved.modules.keys())
        _PROTOCOL_CONF = {
            k: v for k, v in _PROTOCOL_CONF.items()
            if v[0] in _ran_module_names
        }

        _is_k8s = config.is_kubernetes
        for conf_name, (source_key, data_key, desc) in _PROTOCOL_CONF.items():
            # Skip if already registered (shouldn't happen, but guard)
            if conf_name in state.modules:
                continue
            data_keys = (data_key,) if isinstance(data_key, str) else data_key
            source_data = full_capture_json.get(source_key, {})
            if all(source_data.get(k) for k in data_keys):
                # Protocol collected all required config data — nothing to do
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

        # ========= LOG PATH RETRY (interactive) =========
        # When a log module failed because its default path was missing,
        # unreadable, or empty, give the user one inline shot at re-running
        # with a custom path before we hand off to file-based guided recovery.
        # Skipped in headless / --skip-guided so WebUI jobs and CI runs stay
        # non-interactive — the WebUI surfaces its own retry UI on the
        # session detail page using the persisted ``failed_modules`` metadata.
        if not skip_guided and not headless:
            for _log_module in LOG_MODULE_RETRY_SPECS:
                if (
                    _log_module in state.modules
                    and state.modules[_log_module].status == ModuleStatus.FAILED
                ):
                    try:
                        _retry_log_module_with_prompt(
                            module_name=_log_module,
                            state=state,
                            resolved=resolved,
                            config=config,
                            full_capture_json=full_capture_json,
                            log_since=log_since,
                            log_until=log_until,
                        )
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("%s retry helper failed: %s", _log_module, exc)

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

        # ========= MULTI-TARGET MODULES (demoted same-name collisions) =========
        # Same-role duplicate nodes (HA2 replica members) and explicitly-
        # namespaced Kubernetes targets produce module names that collide
        # with the canonical set above; _resolve_modules routes those here
        # instead of letting them silently overwrite the canonical data.
        # Volume is low (empty in the common single-node case), so a simple
        # sequential pass is fine — no need to fold into the concurrent
        # executor used for the canonical module list above.
        multi_target_results: dict[str, dict[str, Any]] = {}
        for _mt_target_name, _mt_modules in resolved.multi_target_modules.items():
            for _mt_mod_name, _mt_func in _mt_modules.items():
                if shutdown_requested():
                    break
                try:
                    _mt_result = call_with_context(_mt_func)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Multi-target module %s@%s failed: %s",
                        _mt_mod_name, _mt_target_name, exc,
                    )
                    continue
                if not _mt_result:
                    continue
                multi_target_results.setdefault(_mt_target_name, {})[_mt_mod_name] = _mt_result

        # ========= RESHAPE INTO NESTED HIERARCHY =========
        structured = reshape_capture(full_capture_json)

        if multi_target_results:
            _multi_target_meta = {
                t.get("name", "local"): {
                    "role": t.get("role", ""),
                    "namespace": t.get("kubectl_namespace", ""),
                    "context": t.get("kubectl_context", ""),
                }
                for t in (config.targets or [])
            }
            structured["_multi_target"] = {
                target_name: {
                    **_multi_target_meta.get(
                        target_name, {"role": "", "namespace": "", "context": ""}
                    ),
                    "data": reshape_capture(mod_results),
                }
                for target_name, mod_results in multi_target_results.items()
            }

        # Mask scheme://user:pass@ creds before the raw debug export writes
        # 01_raw_capture.json (01_capture.json/checkpoint scrubbed elsewhere).
        structured = redact_capture_credentials(structured)

        # ========= DEBUG RAW EXPORT =========
        # Fire after reshape, before filter — callers use this to drop
        # 01_raw_capture.json with the same nested shape rules consume but
        # without ruleset-based pruning, so rule authors can trace any
        # dot-notation path that came back from the collectors.
        if on_raw_capture is not None:
            try:
                on_raw_capture(structured)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Raw capture export callback failed: %s", exc)

        # ========= FINALIZE =========
        limited_capture_json = finalize_capture(
            structured_data=structured,
            rules=rules,
            ruleset=ruleset,
            config=config,
            modules_ran=state.successful_module_names,
            failed_modules=state.failed_modules_summary,
        )

        return limited_capture_json


if __name__ == "__main__":
    run_capture()
