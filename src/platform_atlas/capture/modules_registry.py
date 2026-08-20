"""
Module Registry for Collectors
"""

import functools
import logging
from typing import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from platform_atlas.core.context import ctx
from platform_atlas.core.transport import Transport, LocalTransport

from platform_atlas.capture.collectors.mongo import MongoCollector
from platform_atlas.capture.collectors.redis import RedisCollector
from platform_atlas.capture.collectors.system import SystemInfoCollector
from platform_atlas.capture.collectors.filesystem import FileSystemInfoCollector
from platform_atlas.capture.collectors.platform import PlatformCollector
from platform_atlas.capture.collectors.gateway4 import Gateway4Collector
from platform_atlas.capture.collectors.gateway4_api import Gateway4ApiCollector
from platform_atlas.capture.collectors.gateway5 import Gateway5Collector
from platform_atlas.capture.collectors.kubernetes import KubernetesCollector
from platform_atlas.capture.collectors.authorization import AuthorizationCollector

logger = logging.getLogger(__name__)

class ModuleCategory(Enum):
    """Categories for grouping modules in selection UI"""
    SYSTEM = auto()
    DATABASE = auto()
    PLATFORM = auto()
    GATEWAY = auto()
    KUBERNETES = auto()

@dataclass(frozen=True)
class ModuleInfo:
    """Metadata about a collector module"""
    key: str
    name: str
    description: str
    category: ModuleCategory
    requires_config: list[str] = field(default_factory=list)
    default_enabled: bool = True

# Define module metadata separately from instantiation
MODULE_DEFINITIONS: list[ModuleInfo] =[
    ModuleInfo(
        key="system",
        name="System Info",
        description="CPU, memory, disk, network from local OS",
        category=ModuleCategory.SYSTEM,
    ),
    ModuleInfo(
        key="mongo",
        name="MongoDB Status",
        description="Server status and database statistics",
        category=ModuleCategory.DATABASE,
        requires_config=["mongo_uri"],
    ),
    ModuleInfo(
        key="mongo_conf",
        name="MongoDB Config File",
        description="Parse mongo.conf",
        category=ModuleCategory.DATABASE,
    ),
    ModuleInfo(
        key="redis",
        name="Redis Status",
        description="Redis INFO and ACL data",
        category=ModuleCategory.DATABASE,
        requires_config=["redis_uri"],
    ),
    ModuleInfo(
        key="redis_conf",
        name="Redis Config File",
        description="Parse redis.conf",
        category=ModuleCategory.DATABASE,
    ),
    ModuleInfo(
        key="redis_sentinel_conf",
        name="Redis Sentinel Config File",
        description="Parse sentinel.conf",
        category=ModuleCategory.DATABASE,
    ),
    ModuleInfo(
        key="platform",
        name="Platform API",
        description="Health, config, adapters info from Platform API",
        category=ModuleCategory.PLATFORM,
        requires_config=["platform_uri", "platform_client_id", "platform_client_secret"],
    ),
    ModuleInfo(
        key="authorization",
        name="RBAC Authorization",
        description="Authorization graph (accounts, groups, roles, methods, views) from /authorization/*",
        category=ModuleCategory.PLATFORM,
        requires_config=["platform_uri", "platform_client_id", "platform_client_secret"],
        default_enabled=False,
    ),
    ModuleInfo(
        key="platform_conf",
        name="Platform Config File",
        description="Parse platform.properties",
        category=ModuleCategory.PLATFORM,
    ),
    ModuleInfo(
        key="gateway4",
        name="Gateway4 Packages",
        description="pip list from Gateway4 venv",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="gateway5",
        name="Gateway5 Environment",
        description="Environment variables from Gateway5",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="gateway4_sync_config",
        name="Gateway4 Sync Config",
        description="Check if --sync-config is enabled in service",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="gateway4_db_config",
        name="Gateway4 SQLite Config",
        description="Collection paths from Gateway4 SQLite database",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="gateway4_conf",
        name="Gateway4 Config File",
        description="Parse Gateway4 properties.yml",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="agmanager_size",
        name="AGManager Pronghorn Size",
        description="Report size of pronghorn.json for AGManager",
        category=ModuleCategory.PLATFORM,
    ),
    ModuleInfo(
        key="python_version",
        name="Platform Python Version Check",
        description="Checks if python3.9 and python3.11 are installed",
        category=ModuleCategory.PLATFORM,
    ),
    ModuleInfo(
        key="iagctl_checks",
        name="IAGCTL Checks",
        description="Checks iagctl version and registry info",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="gateway4_db_sizes",
        name="Gateway4 Database Sizes",
        description="Report size of databases for Gateway4",
        category=ModuleCategory.GATEWAY,
    ),
    ModuleInfo(
        key="platform_logs",
        name="Platform Log Analysis",
        description="Parse IAP logs for error frequency and heuristic keyword hits",
        category=ModuleCategory.PLATFORM,
        requires_config=["platform_uri"],
    ),
    ModuleInfo(
        key="webserver_logs",
        name="Platform Webserver Log Analysis",
        description="Parse IAP Webserver logs for issues",
        category=ModuleCategory.PLATFORM,
        requires_config=["platform_uri"],
    ),
    ModuleInfo(
        key="mongo_logs",
        name="MongoDB Log Analysis",
        description="Parse MongoDB logs for errors, warnings, and heuristic keyword hits",
        category=ModuleCategory.DATABASE,
        requires_config=["mongo_uri"],
    ),
    ModuleInfo(
        key="kubernetes_helm",
        name="Kubernetes Helm Values",
        description="Raw Helm chart values for reference",
        category=ModuleCategory.KUBERNETES,
    ),
]

def get_module_info(key: str) -> ModuleInfo | None:
    """Get module info by key"""
    for m in MODULE_DEFINITIONS:
        if m.key == key:
            return m
    return None

def get_all_module_keys() -> list[str]:
    """Return all available module keys"""
    return [m.key for m in MODULE_DEFINITIONS]

def get_default_module_keys() -> list[str]:
    """Returns keys for modules enabled by default"""
    return [m.key for m in MODULE_DEFINITIONS if m.default_enabled]


# Collectors that need an SSH transport to the target server
_SSH_COLLECTOR_KEYS: frozenset[str] = frozenset({
    "system", "filesystem", "gateway4", "gateway5"
})

# Collectors that open their own connections via URIs in config
_PROTOCOL_COLLECTOR_KEYS: frozenset[str] = frozenset({
    "mongo", "redis", "platform", "authorization",
})


def _ssh_unavailable(module_name: str, error: str) -> Callable:
    """Create a placeholder callable for SSH modules that couldn't connect.

    When the SSH transport fails entirely, we still want these modules
    to appear in the capture UI as FAILED rather than silently vanishing.
    Only used for modules that have NO protocol-based fallback.
    """
    from platform_atlas.core.exceptions import CollectorConnectionError

    def _fail() -> dict:
        raise CollectorConnectionError(
            f"SSH unavailable — cannot collect {module_name}",
            details={"error": error},
        )
    return _fail


def _compute_expected_ssh_modules(
    collectors_requested: set[str],
    ssh_needed: set[str],
) -> list[str]:
    """Determine which modules WOULD be registered if SSH succeeds.

    Used to register placeholder modules when the SSH transport
    fails, so they appear as failures in the capture UI.
    """
    config = ctx().config
    expected: list[str] = []

    if "system" in ssh_needed and "platform" in collectors_requested:
        expected.append("system")

    # Filesystem-based modules
    if "filesystem" in ssh_needed:
        # NOTE: mongo_conf, redis_conf, redis_sentinel_conf, and
        # gateway4_conf are excluded — their primary data comes from
        # protocol collectors (pymongo, redis-py, ipsdk).

        if not config.legacy_profile and "platform" in collectors_requested:
            expected.extend([
                "platform_conf", "agmanager_size", "python_version",
                "platform_logs", "webserver_logs",
            ])

        if "mongo" in collectors_requested:
            expected.append("mongo_logs")

        if "gateway4" in collectors_requested:
            expected.append("gateway4_db_sizes")

        if "gateway5" in collectors_requested:
            expected.append("iagctl_checks")

    # Gateway collectors (direct SSH, not filesystem)
    if "gateway4" in ssh_needed:
        expected.extend(["gateway4", "gateway4_sync_config", "gateway4_db_config"])
    if "gateway5" in ssh_needed:
        expected.append("gateway5")

    return expected


def _build_modules_standard(
    target: dict,
) -> tuple[dict[str, Callable], list[str], dict[str, Callable]]:
    """
    Standard-tier module builder.

    Only Platform (OAuth) and Gateway4 API (ipsdk) are registered. SSH
    transports, Mongo/Redis protocol collectors, and Kubernetes flows are
    not constructed under any condition — defense 1 of the hard mode
    boundary.

    Kubernetes targets are not honored in Standard. If a target asks for
    a kubernetes transport, we still produce no modules — Standard does
    not run cluster-side collection.
    """
    collectors_requested = set(target.get("modules", []))
    if not collectors_requested:
        return {}, [], {}

    config = ctx().config
    modules: dict[str, Callable] = {}

    if "platform" in collectors_requested:
        pc = PlatformCollector.from_config(
            metrics_debug=config.debug,
            verify_ssl=config.verify_ssl,
        )
        modules["platform"] = pc.get_platform_info

    if "rbac_authorization" not in getattr(config, "disabled_extended_checks", []):
        ac = AuthorizationCollector.from_config()
        modules["authorization"] = ac.collect

    # Both names accepted — "gateway4" in target.modules during Standard
    # captures resolves to the API collector (the SSH-based one is
    # Extended-only and gated by require_extended()).
    if "gateway4_api" in collectors_requested or "gateway4" in collectors_requested:
        gw4_api = Gateway4ApiCollector.from_config()
        if gw4_api is not None:
            modules["gateway4_api"] = gw4_api.collect

    return modules, [], {}


def _compute_expected_ssh_modules_saas(
    ssh_needed: set[str],
    kind: str,
) -> list[str]:
    """SaaS analogue of _compute_expected_ssh_modules — gateway-scoped.

    Lists the modules that WOULD have registered had SSH connected, so a
    failed gateway SSH still surfaces them as FAILED in the capture UI
    instead of silently vanishing.
    """
    expected: list[str] = []
    if "system" in ssh_needed:
        expected.append("system")
    if kind in ("gateway4", "gw4-gw5"):
        if "gateway4" in ssh_needed:
            expected.extend(["gateway4", "gateway4_sync_config", "gateway4_db_config"])
        if "filesystem" in ssh_needed:
            expected.append("gateway4_db_sizes")
    if kind in ("gateway5", "gw4-gw5"):
        if "gateway5" in ssh_needed:
            expected.append("gateway5")
        if "filesystem" in ssh_needed:
            expected.append("iagctl_checks")
    return expected


def _build_modules_saas(
    target: dict,
) -> tuple[dict[str, Callable], list[str], dict[str, Callable]]:
    """
    SaaS-tier module builder — the audit revolves around one gateway.

    Only the chosen gateway's collectors are constructed, narrowed by
    ``saas_gateway_kind``:

      gateway4 — ipsdk API (primary) + optional SSH supplement
                 (pip list, sync-config, DB config/sizes, properties.yml
                 fallback) + host facts
      gateway5 — env vars via SSH printenv OR a local Compose/Helm file
                 + iagctl checks + host facts (SSH source only)

    Platform, Mongo, Redis, and Kubernetes collectors are never
    constructed under any condition — defense 1 of the hard mode
    boundary, mirroring _build_modules_standard.
    """
    collectors_requested = set(target.get("modules", []))
    if not collectors_requested:
        return {}, [], {}

    config = ctx().config
    kind = (config.saas_gateway_kind or "").strip().lower()
    modules: dict[str, Callable] = {}
    ssh_fallbacks: dict[str, Callable] = {}

    # Narrow to the environment's gateway kind. GW4-only never runs gateway5
    # modules; GW5-only never runs gateway4 modules; gw4-gw5 runs both.
    if kind == "gateway4":
        collectors_requested -= {"gateway5"}
    elif kind == "gateway5":
        collectors_requested -= {"gateway4", "gateway4_api"}
    # gw4-gw5: keep both gateway sets — no narrowing needed

    # Kubernetes targets are not honored in SaaS (matches Standard).
    if target.get("transport") == "kubernetes":
        logger.debug("SaaS does not run Kubernetes targets — skipping '%s'",
                     target.get("name", "unknown"))
        return {}, [], {}

    # ── Gateway5 file source (Docker Compose / Helm values — no SSH) ──
    if target.get("transport") == "gateway5_file":
        if "gateway5" in collectors_requested and kind in ("gateway5", "gw4-gw5"):
            gw5 = Gateway5Collector(
                source_path=target.get("gateway5_source_path", ""),
            )
            modules["gateway5"] = gw5.collect_from_file
        return modules, [], ssh_fallbacks

    # ── SSH-based collectors (share one transport) ──────────────
    if target.get("transport") in ("ssh", "control_master"):
        from platform_atlas.core.transport import transport_from_config

        ssh_needed = collectors_requested & _SSH_COLLECTOR_KEYS
        if ssh_needed:
            try:
                transport = transport_from_config(target)
                logger.debug("SSH transport created for SaaS target '%s' → %s",
                             target.get("name"), type(transport).__name__)

                if "system" in ssh_needed:
                    # Host facts of the gateway host — informational context
                    # in SaaS (no platform gate, unlike the Extended flow).
                    sys_collector = SystemInfoCollector(transport=transport)
                    modules["system"] = sys_collector.get_system_info

                if "filesystem" in ssh_needed:
                    fs = FileSystemInfoCollector(transport=transport)
                    if kind in ("gateway4", "gw4-gw5") and "gateway4" in collectors_requested:
                        # properties.yml is the SSH fallback — ipsdk is primary.
                        ssh_fallbacks["gateway4_conf"] = fs.get_gateway4_conf
                        modules["gateway4_db_sizes"] = fs.check_gateway4_db_size
                    if kind in ("gateway5", "gw4-gw5") and "gateway5" in collectors_requested:
                        modules["iagctl_checks"] = fs.get_iagctl_checks

                if kind in ("gateway4", "gw4-gw5") and "gateway4" in ssh_needed:
                    gw = Gateway4Collector(transport=transport)
                    modules["gateway4"] = gw.pip_list
                    modules["gateway4_sync_config"] = gw.sync_config
                    modules["gateway4_db_config"] = gw.get_config

                if kind in ("gateway5", "gw4-gw5") and "gateway5" in ssh_needed:
                    conf_path = target.get("gateway5_conf_path", "")
                    gw5 = Gateway5Collector(transport=transport, conf_path=conf_path)
                    modules["gateway5"] = (
                        gw5.collect_from_ssh_conf if conf_path else gw5.collect_env
                    )

            except Exception as e:
                ssh_error = str(e)
                logger.info(
                    "SSH transport failed for SaaS target '%s': %s — "
                    "registering SSH modules as unavailable",
                    target.get("name"), e,
                )
                for mod_name in _compute_expected_ssh_modules_saas(ssh_needed, kind):
                    modules[mod_name] = _ssh_unavailable(mod_name, ssh_error)

    # ── Gateway4 API (ipsdk) — primary source for GW4 config data ──
    if kind in ("gateway4", "gw4-gw5") and (
        "gateway4_api" in collectors_requested or "gateway4" in collectors_requested
    ):
        gw4_api = Gateway4ApiCollector.from_config()
        if gw4_api is not None:
            modules["gateway4_api"] = gw4_api.collect

    return modules, [], ssh_fallbacks


def build_modules_for_target(
    target: dict,
    log_since=None,
    log_until=None,
    skip_ssh_nodes: frozenset[str] | None = None,
) -> tuple[dict[str, Callable], list[str], dict[str, Callable]]:
    """
    Build collector modules for a specific target.

    Tier-aware:
    - In Standard mode, only Platform (OAuth) and Gateway4 API (ipsdk)
      are constructed; SSH and protocol-Extended collectors are pruned at
      this layer (defense 1 of the hard mode boundary).
    - In SaaS mode, only the chosen gateway's collectors are constructed
      (see _build_modules_saas) — never Platform/Mongo/Redis/Kubernetes.
    - In Extended mode (the existing flow):
      SSH-based collectors (system, filesystem, gateway4) share a single
      SSH transport to the target server. Protocol-based collectors
      (mongo, redis, platform) open their own connections using URIs
      from the application config. Kubernetes targets use the
      KubernetesCollector to read values.yaml and optionally kubectl.

    When the SSH transport fails (Extended only):
    - Modules with protocol fallbacks (redis_conf, mongo_conf, etc.)
      are deferred for post-capture resolution — NOT shown as failures.
    - Modules without fallbacks (gateway4, system, etc.) are registered
      as immediate failures so they appear in the capture UI.

    Returns:
        (modules_dict, deferred_module_names, ssh_fallbacks)
    """
    if ctx().is_standard:
        return _build_modules_standard(target)
    if ctx().is_saas:
        return _build_modules_saas(target)

    from platform_atlas.core.transport import transport_from_config

    collectors_requested = set(target.get("modules", []))
    if not collectors_requested:
        logger.debug("Target '%s' has no modules configured - skipping",
                     target.get("name", "unknown"))
        return {}, [], {}

    config = ctx().config
    modules: dict[str, Callable] = {}
    deferred: list[str] = []
    ssh_fallbacks: dict[str, Callable] = {}

    logger.debug("Target '%s' requested modules: %s",
                 target.get("name"), sorted(collectors_requested))

    # ── Kubernetes transport (values.yaml + kubectl) ──────────────
    if target.get("transport") == "kubernetes":
        if "kubernetes" in collectors_requested:
            # A node carrying its own namespace/context/kubeconfig/values.yaml
            # is an explicitly-added extra namespace (see TargetNode) — its
            # own values.yaml is the complete picture for that namespace, not
            # a companion to the environment's default IAP values.yaml. Nodes
            # with none of these set are the default primary (or default
            # optional Gateway5) node and behave exactly as before, reading
            # the environment's global config fields.
            _has_override = bool(
                target.get("kubectl_namespace")
                or target.get("kubectl_context")
                or target.get("kubeconfig_path")
                or target.get("values_yaml_path")
            )
            _values_path = target.get("values_yaml_path") or config.values_yaml_path
            k8s = KubernetesCollector(
                values_yaml_path=_values_path,
                kubectl_context=target.get("kubectl_context") or config.kubectl_context,
                kubectl_namespace=target.get("kubectl_namespace") or config.kubectl_namespace,
                kubeconfig_path=target.get("kubeconfig_path") or getattr(config, "kubeconfig_path", "") or "",
                use_kubectl=config.use_kubectl,
                kubectl_binary=getattr(config, "kubectl_binary_path", "") or "",
                values_yaml_defaults_path=getattr(config, "values_yaml_chart_defaults_path", "") or "",
                iag5_values_yaml_defaults_path=getattr(config, "iag5_values_yaml_chart_defaults_path", "") or "",
            )

            # Load the global IAG5 values.yaml only for the default node —
            # an explicit-override node's own values_yaml_path is self-detected
            # (IAP vs IAG5 shape) and doesn't combine with the global default.
            if not _has_override and config.iag5_values_yaml_path:
                try:
                    k8s.load_additional_values(config.iag5_values_yaml_path)
                except Exception as e:
                    logger.debug("Failed to load IAG5 values: %s", e)

            # ── Always-run K8s modules (no protocol equivalent) ────
            # System info comes from K8s resource specs — no SSH or protocol alternative
            modules["system"] = k8s.collect_system_info
            # Raw helm values — only register when a file is actually configured
            if _values_path or (not _has_override and config.iag5_values_yaml_path):
                modules["kubernetes_helm"] = k8s.collect_kubernetes_helm

            # Gateway5 from K8s values — no protocol alternative for GW5 config
            if "gateway5" in collectors_requested and k8s._iag5_values:
                modules["gateway5"] = k8s.collect_gateway5

            # ── Fallback-only K8s modules (protocol is primary) ────
            # Platform config from values.yaml is the fallback, just like
            # SSH config files are the fallback for bare-metal. Protocol
            # collectors (OAuth, pymongo, redis-py) are the primary source.
            # The capture engine's post-capture verification step will try
            # these fallbacks if the protocol collectors don't get config data.
            if _values_path:
                ssh_fallbacks["platform_conf"] = k8s.collect_platform_conf

            # kubectl exec env is a second-tier fallback (live runtime > static values)
            if config.use_kubectl:
                # kubectl env vars are more accurate than values.yaml
                # (catches runtime overrides), but only available if kubectl works.
                # Store the values.yaml fallback and let kubectl override if available.
                _values_fallback = ssh_fallbacks.get("platform_conf")
                def _kubectl_then_values():
                    """Try kubectl exec first, fall back to values.yaml."""
                    live_conf = k8s.collect_kubectl_env()
                    if live_conf:
                        return live_conf
                    if _values_fallback:
                        return _values_fallback()
                    return {}
                ssh_fallbacks["platform_conf"] = _kubectl_then_values

        # Protocol collectors work the same in K8s — register them below
        # (fall through to the protocol section)

    # ── Gateway5 file source (Docker Compose / Helm values — no SSH) ──
    elif target.get("transport") == "gateway5_file":
        if "gateway5" in collectors_requested:
            gw5 = Gateway5Collector(
                source_path=target.get("gateway5_source_path", ""),
            )
            modules["gateway5"] = gw5.collect_from_file

    # ── SSH-based collectors (share one transport) ──────────────
    elif target.get("transport") != "kubernetes":
        ssh_needed = collectors_requested & _SSH_COLLECTOR_KEYS
        _node_name = target.get("name", "")
        if target.get("protocol_only"):
            # Managed service (e.g. AWS Elasticache) — SSH not available;
            # SSH modules omitted silently, protocol collectors run below.
            logger.debug("Node '%s' is protocol-only — skipping SSH collectors", _node_name)
            ssh_needed = set()
        elif skip_ssh_nodes and _node_name in skip_ssh_nodes:
            # User chose "proceed anyway" with socket not ready — register SSH
            # modules as unavailable so capture completes with skipped entries.
            logger.info(
                "Node '%s' SSH skipped (socket not ready) — registering SSH modules as unavailable",
                _node_name,
            )
            for mod_name in _compute_expected_ssh_modules(collectors_requested, ssh_needed):
                modules[mod_name] = _ssh_unavailable(mod_name, "ControlMaster socket not ready")
            ssh_needed = set()
        if ssh_needed:
            try:
                transport = transport_from_config(target)
                logger.debug("SSH transport created for target '%s' → %s",
                            target.get("name"), type(transport).__name__)

                if "system" in ssh_needed:
                    if "platform" in collectors_requested:
                        # Run SystemInfo in Platform Server
                        sys_collector = SystemInfoCollector(transport=transport)
                        modules["system"] = sys_collector.get_system_info

                if "filesystem" in ssh_needed:
                    fs = FileSystemInfoCollector(transport=transport)

                    # Config modules are NOT registered as primary — protocol
                    # collectors handle that. But we pre-create SSH fallback
                    # callables so the capture engine can try them if protocol
                    # fails to get config data.
                    if "mongo" in collectors_requested:
                        ssh_fallbacks["mongo_conf"] = fs.get_mongo_conf
                        _mongo_log_override = (
                            getattr(config, "mongo_log_path_override", "") or None
                        )
                        modules["mongo_logs"] = functools.partial(
                            fs.get_mongo_logs,
                            since=log_since,
                            until=log_until,
                            log_path=_mongo_log_override,
                        )

                    if "redis" in collectors_requested:
                        ssh_fallbacks["redis_conf"] = lambda: fs.get_unformatted_config(
                            service_name="redis"
                        )
                        try:
                            if config.topology.mode.value == "ha2":
                                ssh_fallbacks["redis_sentinel_conf"] = lambda: fs.get_unformatted_config(
                                    service_name="sentinel"
                                )
                        except Exception:
                            pass

                    if "gateway4" in collectors_requested:
                        ssh_fallbacks["gateway4_conf"] = fs.get_gateway4_conf

                    if not config.legacy_profile: # Only run these checks on P6
                        if "platform" in collectors_requested:
                            modules["platform_conf"] = lambda: fs.get_unformatted_config(
                                service_name="platform"
                            )

                            modules["agmanager_size"] = fs.check_agmanager_size
                            modules["python_version"] = fs.get_python_version
                            # Honor the env's log_path_override when set; the
                            # collector falls back to PLATFORM6_LOG_PATH_ROOT
                            # when log_dir is empty/None.
                            _log_dir_override = (
                                getattr(config, "log_path_override", "") or None
                            )
                            modules["platform_logs"] = functools.partial(
                                fs.get_platform_logs,
                                since=log_since,
                                until=log_until,
                                log_dir=_log_dir_override,
                            )
                            _webserver_log_override = (
                                getattr(config, "webserver_log_path_override", "") or None
                            )
                            modules["webserver_logs"] = functools.partial(
                                fs.get_webserver_logs,
                                since=log_since,
                                until=log_until,
                                log_path=_webserver_log_override,
                            )

                    # Only register gateway4-specific filesystem checks
                    # when gateway4 is in the node's module list
                    if "gateway4" in collectors_requested:
                        modules["gateway4_db_sizes"] = fs.check_gateway4_db_size

                    if "gateway5" in collectors_requested:
                        modules["iagctl_checks"] = fs.get_iagctl_checks

                if "gateway4" in ssh_needed:
                    gw = Gateway4Collector(transport=transport)
                    modules["gateway4"] = gw.pip_list
                    modules["gateway4_sync_config"] = gw.sync_config
                    modules["gateway4_db_config"] = gw.get_config

                if "gateway5" in ssh_needed:
                    conf_path = target.get("gateway5_conf_path", "")
                    gw5 = Gateway5Collector(transport=transport, conf_path=conf_path)
                    modules["gateway5"] = (
                        gw5.collect_from_ssh_conf if conf_path else gw5.collect_env
                    )

            except Exception as e:
                ssh_error = str(e)
                logger.info(
                    "SSH transport failed for target '%s': %s — "
                    "registering SSH modules as unavailable, "
                    "protocol collectors will still run",
                    target.get("name"), e,
                )

                # Register failure placeholders for SSH modules that couldn't run
                for mod_name in _compute_expected_ssh_modules(
                    collectors_requested, ssh_needed
                ):
                    modules[mod_name] = _ssh_unavailable(mod_name, ssh_error)

    # ── Protocol-based collectors (own connections via URIs) ────
    if "mongo" in collectors_requested:
        mc = MongoCollector.from_config()
        if mc is not None:
            modules["mongo"] = mc.collect

    if "redis" in collectors_requested:
        rc = RedisCollector.from_config()
        if rc is not None:
            modules["redis"] = rc.collect

    if "platform" in collectors_requested:
        pc = PlatformCollector.from_config(
            metrics_debug=config.debug,
            verify_ssl=config.verify_ssl,
        )
        modules["platform"] = pc.get_platform_info

    if "rbac_authorization" not in getattr(config, "disabled_extended_checks", []) and "platform" in collectors_requested:
        ac = AuthorizationCollector.from_config()
        modules["authorization"] = ac.collect

    # Gateway4 API — primary source for gateway4 config data
    # (not used in Kubernetes mode — no Gateway4 support)
    if "gateway4" in collectors_requested and target.get("transport") != "kubernetes":
        gw4_api = Gateway4ApiCollector.from_config()
        if gw4_api is not None:
            modules["gateway4_api"] = gw4_api.collect

    return modules, deferred, ssh_fallbacks

def build_preflight_checks(
        transport: Transport | None = None,
        *,
        include: frozenset[str] | None = None,
        role: str | None = None,
        node_modules: frozenset[str] | None = None,
) -> dict[str, Callable]:
    """Get all preflight check functions.

    Tier-aware: Standard mode never builds SSH-dependent or
    Extended-only protocol checks (Mongo, Redis). Only Platform and
    Gateway4 API preflights run in Standard. SaaS builds only the chosen
    gateway's SSH checks plus host facts, and the Gateway4 API connector
    when the environment audits a GW4 — never Platform/Mongo/Redis.

    ``role`` (the node's topology role, e.g. "mongo"/"redis"/"iap"/"iag")
    scopes the filesystem config-file check to that node's own service —
    a Mongo node is never checked for a Redis/Sentinel/Platform file.
    ``node_modules`` disambiguates an "iag" node further — Gateway4 vs.
    Gateway5 — since the role alone doesn't say which gateway is installed.
    """
    if transport is None:
        transport = LocalTransport()

    checks: dict[str, Callable] = {}
    is_standard = ctx().is_standard
    is_saas = ctx().is_saas
    saas_kind = (ctx().config.saas_gateway_kind or "").strip().lower() if is_saas else ""

    # SSH-dependent collectors - only build when requested or unfiltered.
    # Skipped entirely in Standard (no SSH preflight in Standard); scoped
    # to the chosen gateway in SaaS.
    ssh_keys = {"gateway4", "gateway5", "filesystem", "system"}
    if is_saas:
        if saas_kind == "gw4-gw5":
            ssh_keys = {"system", "filesystem", "gateway4", "gateway5"}
        else:
            ssh_keys = {"system", "filesystem",
                        "gateway4" if saas_kind == "gateway4" else "gateway5"}
    if not is_standard and (include is None or include & ssh_keys):
        if "gateway4" in ssh_keys:
            checks["gateway4"] = Gateway4Collector(transport=transport).preflight
        if "gateway5" in ssh_keys:
            checks["gateway5"] = Gateway5Collector(transport=transport).preflight
        if "filesystem" in ssh_keys:
            checks["filesystem"] = FileSystemInfoCollector(
                transport=transport, role=role, modules=node_modules,
            ).preflight
        if "system" in ssh_keys:
            checks["system"] = SystemInfoCollector(transport=transport).preflight

    # Protocol-based collectors - static methods, no transport needed.
    # Standard mode runs only platform + gateway4_api; SaaS runs only
    # gateway4_api (and only for a GW4 environment).
    connector_keys = {"redis", "mongo", "platform", "gateway4_api"}
    standard_connector_keys = {"platform", "gateway4_api"}
    if is_standard:
        allowed_connectors = standard_connector_keys
    elif is_saas:
        allowed_connectors = (
            {"gateway4_api"} if saas_kind in ("gateway4", "gw4-gw5") else set()
        )
    else:
        allowed_connectors = connector_keys
    if include is None or include & allowed_connectors:
        if "platform" in allowed_connectors:
            checks["platform"] = PlatformCollector.preflight
        if "gateway4_api" in allowed_connectors:
            checks["gateway4_api"] = Gateway4ApiCollector.preflight
        if "redis" in allowed_connectors:
            checks["redis"] = RedisCollector.preflight
        if "mongo" in allowed_connectors:
            checks["mongo"] = MongoCollector.preflight

    return checks
