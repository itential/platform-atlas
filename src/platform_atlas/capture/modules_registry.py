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
    "mongo", "redis", "platform",
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


def build_modules_for_target(
    target: dict,
    log_since=None,
    log_until=None,
) -> tuple[dict[str, Callable], list[str], dict[str, Callable]]:
    """
    Build collector modules for a specific target.

    SSH-based collectors (system, filesystem, gateway4) share a single
    SSH transport to the target server.

    Protocol-based collectors (mongo, redis, platform) open their own
    connections using URIs from the application config — no SSH needed.

    Kubernetes targets use the KubernetesCollector to read values.yaml
    and optionally kubectl — no SSH transport at all.

    When the SSH transport fails:
    - Modules with protocol fallbacks (redis_conf, mongo_conf, etc.)
      are deferred for post-capture resolution — NOT shown as failures.
    - Modules without fallbacks (gateway4, system, etc.) are registered
      as immediate failures so they appear in the capture UI.

    Returns:
        (modules_dict, deferred_module_names)
    """
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
            k8s = KubernetesCollector(
                values_yaml_path=config.values_yaml_path,
                kubectl_context=config.kubectl_context,
                kubectl_namespace=config.kubectl_namespace,
                use_kubectl=config.use_kubectl,
            )

            # Load IAG5 values if configured
            if config.iag5_values_yaml_path:
                try:
                    k8s.load_additional_values(config.iag5_values_yaml_path)
                except Exception as e:
                    logger.debug("Failed to load IAG5 values: %s", e)

            # ── Always-run K8s modules (no protocol equivalent) ────
            # System info comes from K8s resource specs — no SSH or protocol alternative
            modules["system"] = k8s.collect_system_info
            # Raw helm values stored for reference/debugging
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
            if config.values_yaml_path:
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

    # ── SSH-based collectors (share one transport) ──────────────
    elif target.get("transport") != "kubernetes":
        ssh_needed = collectors_requested & _SSH_COLLECTOR_KEYS
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
                        modules["mongo_logs"] = functools.partial(
                            fs.get_mongo_logs, since=log_since, until=log_until
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
                            modules["platform_logs"] = functools.partial(
                                fs.get_platform_logs, since=log_since, until=log_until
                            )
                            modules["webserver_logs"] = functools.partial(
                                fs.get_webserver_logs, since=log_since, until=log_until
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
                    gw5 = Gateway5Collector(transport=transport)
                    modules["gateway5"] = gw5.collect_env

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
) -> dict[str, Callable]:
    """Get all preflight check functions"""
    if transport is None:
        transport = LocalTransport()

    checks: dict[str, Callable] = {}

    # SSH-dependent collectors - only build when requested or unfiltered
    ssh_keys = {"gateway4", "gateway5", "filesystem", "system"}
    if include is None or include & ssh_keys:
        gateway4 = Gateway4Collector(transport=transport)
        gateway5 = Gateway5Collector(transport=transport)
        filesystem = FileSystemInfoCollector(transport=transport)
        system = SystemInfoCollector(transport=transport)
        checks["gateway4"] = gateway4.preflight
        checks["gateway5"] = gateway5.preflight
        checks["filesystem"] = filesystem.preflight
        checks["system"] = system.preflight

    # Protocol-based collectors - static methods, no transport needed
    connector_keys = {"redis", "mongo", "platform", "gateway4_api"}
    if include is None or include & connector_keys:
        checks["redis"] = RedisCollector.preflight
        checks["mongo"] = MongoCollector.preflight
        checks["platform"] = PlatformCollector.preflight
        checks["gateway4_api"] = Gateway4ApiCollector.preflight

    return checks
