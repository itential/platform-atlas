"""
ATLAS // Architecture Validation - Manual Collector

Collects architecture information that cannot be gathered through automated
data collection scripts.

When an environment is active, topology data is used to pre-fill answers
(node counts, gateway presence, deployment mode, etc.) so the user only
needs to confirm or adjust rather than re-enter everything.

Sections:
    - Environment Overview (type, location — asked once)
    - Platform Architecture
    - Gateway4 Architecture
    - Gateway5 Architecture
    - MongoDB Architecture
    - Redis Architecture
    - Load Balancer Configuration
    - Kubernetes Configuration (if applicable)
    - Monitoring & Health Checks
    - Network & Security
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from platform_atlas.core import ui
from platform_atlas.core.paths import ATLAS_HOME

logger = logging.getLogger(__name__)
console = Console()
theme = ui.theme

ATLAS_STYLE = questionary.Style([
    ("qmark", f"fg:{theme.primary} bold"),
    ("question", f"fg:{theme.text_primary} bold"),
    ("answer", f"fg:{theme.primary}"),
    ("pointer", f"fg:{theme.primary} bold"),
    ("highlighted", f"fg:{theme.primary} bold"),
    ("selected", f"fg:{theme.primary}"),
    ("separator", "fg:#888888"),
    ("instruction", "fg:#888888"),
])

# Common deployment types across components
DEPLOYMENT_TYPES = [
    "Bare Metal",
    "Virtual Machines (VMs)",
    "Kubernetes (AWS EKS)",
    "Kubernetes (Azure AKS)",
    "Kubernetes (Self-Managed)",
    "AWS Fargate",
    "Docker Containers",
    "Other",
]

ENVIRONMENT_TYPES = [
    "Production",
    "Staging",
    "Development",
    "QA / Test",
    "DR / Failover",
]

OS_TYPES = [
    "RHEL 8",
    "RHEL 9",
    "Rocky 8",
    "Rocky 9",
    "Alpine",
    "Amazon Linux 2",
    "Other",
]


# ─────────────── TOPOLOGY HINTS ─────────────── #

@dataclass(frozen=True)
class TopologyHints:
    """
    Pre-computed hints extracted from the active environment's topology.

    Used to pre-fill manual collector prompts so the user can confirm
    rather than re-enter values that Atlas already knows.
    """
    environment_name: str = ""
    environment_description: str = ""
    deployment_mode: str = ""           # standalone, ha2, custom
    iap_node_count: int = 0
    mongo_node_count: int = 0
    redis_node_count: int = 0
    has_gateway4: bool = False
    gateway4_node_count: int = 0
    has_gateway5: bool = False
    gateway5_node_count: int = 0
    inferred_env_type: str = ""         # Best guess at environment type
    # SSH-detected suggestions (architecture_autofill.py), keyed by section
    # exactly like architecture_store's "autofill" bucket — e.g.
    # {"platform": {"server_specs": {...}, "deployment": {...}}, ...}.
    # Empty unless a prior autofill run has data for this environment.
    autofill: dict[str, Any] = field(default_factory=dict)
    # Positive-only "is this component's primary node the same host as
    # Platform's" signal, keyed "mongodb"/"redis"/"gateway4"/"gateway5".
    # Never records False — different hostnames don't prove different
    # datacenters, so anything but an identical host is left unanswered.
    same_datacenter: dict[str, bool] = field(default_factory=dict)
    # True only when a real topology was actually parsed — distinguishes
    # "we checked and this environment genuinely has no Gateway5 node" from
    # "we have no topology data at all, so we don't know." Only the former
    # is safe to suggest skipping a section over.
    has_topology: bool = False

    @classmethod
    def from_config(cls, environment: str | None = None) -> TopologyHints:
        """
        Build hints for ``environment``'s own topology/config.

        When ``environment`` is omitted, falls back to the currently active
        environment (this class's original behavior). When given explicitly,
        reads that environment's own ``deployment`` block directly — never
        the active config's — so hints for env "prod" are never accidentally
        built from whatever env happens to be active right now.

        Returns empty hints if anything fails (no environment, no topology, etc.).
        """
        env_name = environment or ""
        env_desc = ""
        deployment_dict: dict | None = None

        if env_name:
            try:
                from platform_atlas.core.environment import get_environment_manager
                mgr = get_environment_manager()
                if mgr.exists(env_name):
                    env = mgr.load(env_name)
                    env_desc = env.description or ""
                    deployment_dict = env.deployment
            except Exception:
                pass
        else:
            try:
                from platform_atlas.core.config import get_config
                config = get_config()
            except Exception:
                return cls()

            env_name = config.active_environment or ""
            if env_name:
                try:
                    from platform_atlas.core.environment import get_environment_manager
                    mgr = get_environment_manager()
                    if mgr.exists(env_name):
                        env = mgr.load(env_name)
                        env_desc = env.description or ""
                except Exception:
                    pass
            try:
                deployment_dict = config.deployment
            except Exception:
                deployment_dict = None

        if not deployment_dict:
            return cls(
                environment_name=env_name,
                environment_description=env_desc,
                inferred_env_type=_guess_env_type(env_name, env_desc),
            )

        try:
            from platform_atlas.core.topology import DeploymentTopology
            topology = DeploymentTopology.from_dict(deployment_dict)
        except Exception:
            return cls(
                environment_name=env_name,
                environment_description=env_desc,
                inferred_env_type=_guess_env_type(env_name, env_desc),
            )

        mode = topology.mode.value if topology.mode else ""
        nodes = topology.nodes or []

        iap_count = 0
        mongo_count = 0
        redis_count = 0
        gw4_count = 0
        gw5_count = 0

        for node in nodes:
            role_val = node.role.value if node.role else ""
            modules = node.effective_modules or []

            if role_val == "all":
                iap_count += 1
                mongo_count += 1
                redis_count += 1
                if "gateway4" in modules:
                    gw4_count += 1
                if "gateway5" in modules:
                    gw5_count += 1
            elif role_val == "iap":
                iap_count += 1
            elif role_val == "mongo":
                mongo_count += 1
            elif role_val == "redis":
                redis_count += 1
            elif role_val == "iag":
                if "gateway4" in modules:
                    gw4_count += 1
                elif "gateway5" in modules:
                    gw5_count += 1
                else:
                    # Default: count as gateway but don't know which version
                    gw5_count += 1

        autofill: dict[str, Any] = {}
        try:
            from platform_atlas.core import architecture_store
            autofill = architecture_store.load(env_name).get("autofill") or {}
        except Exception:
            pass

        return cls(
            environment_name=env_name,
            environment_description=env_desc,
            deployment_mode=mode,
            iap_node_count=iap_count,
            mongo_node_count=mongo_count,
            redis_node_count=redis_count,
            has_gateway4=gw4_count > 0,
            gateway4_node_count=gw4_count,
            has_gateway5=gw5_count > 0,
            gateway5_node_count=gw5_count,
            inferred_env_type=_guess_env_type(env_name, env_desc),
            autofill=autofill,
            same_datacenter=_same_datacenter_hints(topology),
            has_topology=True,
        )

    def suggested_section_skips(self) -> dict[str, bool]:
        """Sections we can confidently say don't apply to this environment,
        purely from its own topology — e.g. a Gateway4-only environment
        definitely has no Gateway5. Only returned when a real topology was
        actually parsed (``has_topology``); never guessed from an absence
        of data. Callers still must not apply this over a section the user
        has already answered or explicitly un-skipped themselves.
        """
        if not self.has_topology:
            return {}
        out: dict[str, bool] = {}
        if not self.has_gateway4:
            out["gateway4"] = True
        if not self.has_gateway5:
            out["gateway5"] = True
        if self.deployment_mode and self.deployment_mode != "kubernetes":
            out["kubernetes"] = True
        return out

    def as_seed_dict(self) -> dict[str, Any]:
        """This hint set, shaped like architecture_store's completed/autofill
        dicts — the LOWEST-priority layer of the browser form's seed. Topology
        facts Atlas already has, free and instant; a confirmed answer or an
        SSH-probed autofill suggestion both take precedence when present.

        Values are strings (matching how the browser form itself stores typed
        answers) — ``"true"``/``"false"`` for confirm-type fields specifically,
        since that's the literal sentinel ``restoreConfirm()`` checks for, not
        a JSON boolean.
        """
        out: dict[str, Any] = {}
        if self.iap_node_count > 0:
            out.setdefault("platform", {})["active_instance_count"] = str(self.iap_node_count)
        if self.mongo_node_count > 0:
            mongodb = out.setdefault("mongodb", {})
            mongodb["mongo_node_count"] = str(self.mongo_node_count)
            if self.deployment_mode == "ha2":
                mongodb["deployment_type"] = "Replica Set (recommended for HA)"
            elif self.deployment_mode == "standalone":
                mongodb["deployment_type"] = "Standalone"
        if self.same_datacenter.get("mongodb"):
            out.setdefault("mongodb", {})["same_datacenter_as_platform"] = "true"
        if self.redis_node_count > 0:
            redis = out.setdefault("redis", {})
            redis["redis_node_count"] = str(self.redis_node_count)
            if self.deployment_mode == "ha2":
                redis["deployment_type"] = "Sentinel (recommended for HA)"
                redis["sentinel_count"] = str(self.redis_node_count)
            elif self.deployment_mode == "standalone":
                redis["deployment_type"] = "Single Instance"
        if self.same_datacenter.get("redis"):
            out.setdefault("redis", {})["same_datacenter_as_platform"] = "true"
        if self.gateway4_node_count > 0:
            out.setdefault("gateway4", {})["instance_count"] = str(self.gateway4_node_count)
        if self.same_datacenter.get("gateway4"):
            out.setdefault("gateway4", {})["same_datacenter_as_platform"] = "true"
        if self.same_datacenter.get("gateway5"):
            out.setdefault("gateway5", {})["same_datacenter_as_platform"] = "true"
        return out


def _primary_host_for(topology: Any, role: Any) -> str:
    """Lowercased host of ``role``'s primary node, falling back to the
    standalone all-in-one node (role=ALL covers every component on one
    host). Empty string when no such node exists."""
    from platform_atlas.core.topology import NodeRole
    node = topology.primary_node(role) or topology.primary_node(NodeRole.ALL)
    return (node.host or "").strip().lower() if node else ""


def _same_datacenter_hints(topology: Any) -> dict[str, bool]:
    """Positive-only "same host as Platform" signal per component.

    Atlas's topology model has no datacenter/AZ concept at all — only a
    hostname per node — so the only thing that can be answered with real
    confidence is the degenerate case where a component's primary node is
    the literal same host as Platform's (e.g. standalone all-in-one, or a
    custom topology that happens to co-locate them). Different hostnames
    prove nothing about physical placement, so that case is never recorded
    as False — just left out entirely for the user to answer.
    """
    from platform_atlas.core.topology import NodeRole
    platform_host = _primary_host_for(topology, NodeRole.IAP)
    if not platform_host:
        return {}

    out: dict[str, bool] = {}
    mongo_host = _primary_host_for(topology, NodeRole.MONGO)
    if mongo_host and mongo_host == platform_host:
        out["mongodb"] = True
    redis_host = _primary_host_for(topology, NodeRole.REDIS)
    if redis_host and redis_host == platform_host:
        out["redis"] = True

    gw_node = topology.primary_node(NodeRole.IAG) or topology.primary_node(NodeRole.ALL)
    if gw_node and (gw_node.host or "").strip().lower() == platform_host:
        modules = gw_node.effective_modules or []
        if "gateway4" in modules:
            out["gateway4"] = True
        if "gateway5" in modules:
            out["gateway5"] = True
    return out


def _guess_env_type(env_name: str, env_desc: str) -> str:
    """
    Best-effort guess at the environment type from the name/description.
    Returns the matching ENVIRONMENT_TYPES entry, or empty string if uncertain.
    """
    search = f"{env_name} {env_desc}".lower()

    patterns = {
        "Production":   ("prod", "production", "prd"),
        "Staging":      ("staging", "stage", "stg", "pre-prod", "preprod"),
        "Development":  ("dev", "development", "sandbox"),
        "QA / Test":    ("qa", "test", "uat", "sit"),
        "DR / Failover": ("dr", "disaster", "failover", "backup"),
    }

    for env_type, keywords in patterns.items():
        if any(kw in search for kw in keywords):
            return env_type

    return ""


# ─────────────── UI HELPERS ─────────────── #

def _section_banner(title: str, description: str = "") -> None:
    """Display a Rich panel as a section header"""
    body = Text(description, style="italic") if description else Text("")
    console.print()
    console.print(Rule(style=theme.primary))
    console.print(Panel(
        body,
        title=f"[bold {theme.primary}]{title}[/]",
        border_style=theme.primary,
        padding=(1, 2),
    ))
    console.print()


def _subsection(title: str) -> None:
    """Lighter visual break between question groups within a section"""
    console.print(f"\n  [{theme.primary}]── {title} ──[/]\n")


def _auto_fill_note(field_label: str, value: str) -> None:
    """Show a subtle note that a value was pre-filled from the environment"""
    console.print(
        f"  [{theme.text_dim}]↳ Pre-filled from environment: {value}[/{theme.text_dim}]"
    )


# ─────────────── PROMPT HELPERS ─────────────── #

def _ask_text(message: str, default: str = "", required: bool = True) -> str:
    """Prompt for a text value with optional default"""
    validate = (lambda val: True if val.strip() else "This field is required.") if required else None
    result = questionary.text(
        message, default=default, validate=validate, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_int(message: str, default: int = 0) -> int:
    """Prompt for an integer value"""
    def _validate(val: str) -> bool | str:
        try:
            int(val)
            return True
        except ValueError:
            return "Please enter a valid whole number."

    result = questionary.text(
        message, default=str(default), validate=_validate, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return int(result)


def _ask_select(message: str, choices: list[str], default: str = "") -> str:
    """Single-select from a list of choices, with optional pre-selected default"""
    kwargs: dict[str, Any] = {"style": ATLAS_STYLE}
    if default and default in choices:
        kwargs["default"] = default
    result = questionary.select(message, choices=choices, **kwargs).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_checkbox(message: str, choices: list[str], pre_checked: list[str] | None = None) -> list[str]:
    """Multi-select from a list of choices, with optional pre-checked defaults"""
    pre_checked = pre_checked or []
    options = [questionary.Choice(c, checked=c in pre_checked) for c in choices]
    result = questionary.checkbox(
        message, choices=options, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/No confirmation prompt"""
    result = questionary.confirm(
        message, default=default, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


# ─────────────── REUSABLE PROMPT GROUPS ─────────────── #

def _collect_server_specs(component_label: str, hint: dict[str, str] | None = None) -> dict[str, str]:
    """Reusable prompt group for server/VM/container specs.

    ``hint`` — auto-detected values (from ``architecture_autofill``) keyed
    the same as the returned dict; pre-selects the matching choice and
    prints an auto-fill note. Absent/unmatched keys prompt exactly as before.
    """
    _subsection(f"{component_label} Server Specs")
    hint = hint or {}

    if hint.get("os_type"):
        _auto_fill_note(f"{component_label} OS", f"{hint['os_type']} (auto-detected)")
    result = {"os_type": _ask_select(
        f"{component_label} — Operating System:", choices=OS_TYPES, default=hint.get("os_type", ""),
    )}

    if hint.get("cpu_cores"):
        _auto_fill_note(f"{component_label} CPU cores", f"{hint['cpu_cores']} (auto-detected)")
    result["cpu_cores"] = _ask_select(
        f"{component_label} — CPU cores per server:",
        choices=["2", "4", "8", "16", "32", "64+", "Unknown"],
        default=hint.get("cpu_cores", ""),
    )
    if hint.get("memory_gb"):
        _auto_fill_note(f"{component_label} memory", f"{hint['memory_gb']} GB (auto-detected)")
    result["memory_gb"] = _ask_select(
        f"{component_label} — Memory (GB) per server:",
        choices=["4", "8", "16", "32", "64", "128+", "Unknown"],
        default=hint.get("memory_gb", ""),
    )
    if hint.get("disk_space_gb"):
        _auto_fill_note(f"{component_label} disk space", f"{hint['disk_space_gb']} GB (auto-detected)")
    result["disk_space_gb"] = _ask_select(
        f"{component_label} — Disk space (GB) per server:",
        choices=["20", "50", "100", "200", "500", "1000+", "Unknown"],
        default=hint.get("disk_space_gb", ""),
    )

    return result


def _collect_deployment_type(component_label: str, hint: dict[str, str] | None = None) -> dict[str, str]:
    """Reusable prompt for deployment type"""
    hint = hint or {}
    detected = hint.get("deployment_type", "")
    if detected:
        _auto_fill_note(f"{component_label} deployment type", f"{detected} (auto-detected)")
    return {
        "deployment_type": _ask_select(
            f"{component_label} — Deployment type:",
            choices=DEPLOYMENT_TYPES,
            default=detected,
        )
    }


# ─────────────── SECTION COLLECTORS ─────────────── #

@dataclass
class ArchitectureSection:
    """Base for all architecture section collectors"""
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    hints: TopologyHints = field(default_factory=TopologyHints)

    def collect(self) -> dict[str, Any]:
        raise NotImplementedError


class EnvironmentOverviewCollector(ArchitectureSection):
    """Environment type and datacenter location — asked once for the whole deployment"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="environment", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Environment Overview",
            "General information about this deployment. These answers apply to the entire environment.",
        )

        # Pre-fill environment type if we can guess it from the env name
        default_env_type = self.hints.inferred_env_type
        if default_env_type:
            _auto_fill_note("Environment type", f"{default_env_type} (from environment '{self.hints.environment_name}')")

        self.data["environment_type"] = _ask_select(
            "Environment type:", choices=ENVIRONMENT_TYPES,
            default=default_env_type,
        )
        self.data["datacenter_location"] = _ask_text(
            "Datacenter location (e.g., us-east-1, London-DC2, Building 4 Lab):"
        )
        detected_hosting = (self.hints.autofill.get("environment") or {}).get("hosting_provider", "")
        if detected_hosting:
            _auto_fill_note("Hosting provider", f"{detected_hosting} (auto-detected via cloud-init)")
        self.data["hosting_provider"] = _ask_select(
            "Hosting provider:",
            choices=["AWS", "Azure", "GCP", "On-Premises", "Hybrid (On-Prem + Cloud)", "Other"],
            default=detected_hosting,
        )

        return self.data


class PlatformArchitectureCollector(ArchitectureSection):
    """Platform (IAP) topology, deployment type, and server specs"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="platform", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Platform Architecture",
            "Instance count, deployment type, and server specs for Itential Platform.",
        )

        # Pre-fill instance count from topology
        default_active = self.hints.iap_node_count if self.hints.iap_node_count > 0 else 0
        if default_active > 0:
            _auto_fill_note("Active instances", f"{default_active} (from topology)")

        self.data["active_instance_count"] = _ask_int(
            "Number of active Platform instances:", default=default_active
        )
        self.data["standby_instance_count"] = _ask_int("Number of standby Platform instances:", default=0)

        total = self.data["active_instance_count"] + self.data["standby_instance_count"]
        if total > 1:
            self.data["all_in_same_datacenter"] = _ask_confirm(
                "Are all Platform instances in the same datacenter?", default=True
            )
            if not self.data["all_in_same_datacenter"]:
                self.data["datacenter_details"] = _ask_text(
                    "  Describe the distribution (e.g., 2 active in us-east-1, 1 standby in us-west-2):"
                )

        # Deployment Type + Server Specs
        platform_autofill = self.hints.autofill.get("platform") or {}
        self.data["deployment"] = _collect_deployment_type("Platform", platform_autofill.get("deployment"))
        self.data["server_specs"] = _collect_server_specs("Platform", platform_autofill.get("server_specs"))

        return self.data


class Gateway4ArchitectureCollector(ArchitectureSection):
    """Gateway4 topology, specs, and migration plans"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="gateway4", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Gateway4 Architecture",
            "Instance count, device info, and migration plans for Automation Gateway 4.",
        )

        # Pre-fill presence from topology
        default_has_gw4 = self.hints.has_gateway4
        if default_has_gw4:
            _auto_fill_note("Gateway4", f"{self.hints.gateway4_node_count} node(s) detected in topology")

        has_gw4 = _ask_confirm("Does this environment have Gateway4?", default=default_has_gw4)
        if not has_gw4:
            self.data["present"] = False
            return self.data

        self.data["present"] = True

        default_count = self.hints.gateway4_node_count if self.hints.gateway4_node_count > 0 else 1
        self.data["instance_count"] = _ask_int("Number of Gateway4 servers:", default=default_count)
        if self.hints.same_datacenter.get("gateway4"):
            _auto_fill_note("Same datacenter as Platform", "Yes (auto-detected — same host as Platform)")
        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Are Gateway4 servers in the same datacenter as Platform?", default=True
        )

        # Specs
        gateway4_autofill = self.hints.autofill.get("gateway4") or {}
        self.data["deployment"] = _collect_deployment_type("Gateway4", gateway4_autofill.get("deployment"))
        self.data["server_specs"] = _collect_server_specs("Gateway4", gateway4_autofill.get("server_specs"))

        # Devices
        self.data["device_count"] = _ask_select(
            "Approximate number of network devices managed:",
            choices=["1-500", "500-2000", "2000-5000", "5000-10000", "10000+", "Unknown"],
        )

        # Migration
        _subsection("Gateway5 Migration")
        self.data["plans_to_migrate_to_gw5"] = _ask_confirm(
            "Are there plans to migrate from Gateway4 to Gateway5?"
        )
        if self.data["plans_to_migrate_to_gw5"]:
            self.data["migration_timeline"] = _ask_select(
                "  Expected migration timeline:",
                choices=["Next 3 months", "3-6 months", "6-12 months", "12+ months", "No timeline yet"],
            )

        return self.data


class Gateway5ArchitectureCollector(ArchitectureSection):
    """Gateway5 topology, cluster info, and HA configuration"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="gateway5", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Gateway5 Architecture",
            "Cluster configuration, HA, and server specs for Automation Gateway 5.",
        )

        # Pre-fill presence from topology
        default_has_gw5 = self.hints.has_gateway5
        if default_has_gw5:
            _auto_fill_note("Gateway5", f"{self.hints.gateway5_node_count} node(s) detected in topology")

        has_gw5 = _ask_confirm("Does this environment have Gateway5?", default=default_has_gw5)
        if not has_gw5:
            self.data["present"] = False
            return self.data

        self.data["present"] = True
        if self.hints.same_datacenter.get("gateway5"):
            _auto_fill_note("Same datacenter as Platform", "Yes (auto-detected — same host as Platform)")
        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Are Gateway5 servers in the same datacenter as Platform?", default=True
        )

        # Cluster Topology
        _subsection("Cluster Configuration")
        self.data["cluster_count"] = _ask_int("Number of Gateway5 clusters:", default=1)

        self.data["clusters"] = []
        for i in range(1, self.data["cluster_count"] + 1):
            console.print(f"  [{theme.text_dim}]Cluster {i}:[/]")
            cluster = {
                "server_count": _ask_int(f"  Cluster {i} — Number of servers:", default=1),
                "runner_count": _ask_int(f"  Cluster {i} — Number of runners:", default=1),
            }
            self.data["clusters"].append(cluster)

        # HA
        _subsection("High Availability")
        self.data["ha_enabled"] = _ask_confirm("Is HA enabled for Gateway5?")
        if self.data["ha_enabled"]:
            self.data["ha_mode"] = _ask_select(
                "  HA mode:", choices=["Active-Standby", "Active-Active", "Other"],
            )

        self.data["has_redundant_instances"] = _ask_confirm(
            "Are there redundant Gateway5 instances for failover?"
        )

        # Specs
        gateway5_autofill = self.hints.autofill.get("gateway5") or {}
        self.data["deployment"] = _collect_deployment_type("Gateway5", gateway5_autofill.get("deployment"))
        self.data["server_specs"] = _collect_server_specs("Gateway5", gateway5_autofill.get("server_specs"))

        return self.data


class MongoDBArchitectureCollector(ArchitectureSection):
    """MongoDB topology, replica set info, and server specs"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="mongodb", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "MongoDB Architecture",
            "Deployment topology and server specs.",
        )

        mongodb_autofill = self.hints.autofill.get("mongodb") or {}

        # A prior autofill run's capture-confirmed replica set (see
        # architecture_autofill.py) beats a guess from the deployment mode
        # name alone. There's no reliable capture-based signal for a
        # confirmed standalone (a replica-set probe simply never runs for
        # one), so that direction is always the deployment-mode guess.
        default_topology = mongodb_autofill.get("deployment_type", "")
        if default_topology:
            _auto_fill_note("Topology", f"{default_topology} (auto-detected)")
        elif self.hints.deployment_mode == "ha2":
            default_topology = "Replica Set (recommended for HA)"
            _auto_fill_note("Topology", f"{default_topology} (from deployment mode: {self.hints.deployment_mode})")
        elif self.hints.deployment_mode == "standalone":
            default_topology = "Standalone"
            _auto_fill_note("Topology", f"{default_topology} (from deployment mode: {self.hints.deployment_mode})")

        self.data["deployment_type"] = _ask_select(
            "MongoDB deployment topology:",
            choices=["Standalone", "Replica Set (recommended for HA)", "Sharded Cluster", "Other"],
            default=default_topology,
        )

        # Pre-fill node count (total servers, primary included) — a prior
        # autofill run's capture-derived replica-set size beats the
        # topology's configured node count, which beats a bare guess.
        detected_node_count = mongodb_autofill.get("mongo_node_count")
        if detected_node_count:
            default_count = int(detected_node_count)
            _auto_fill_note("MongoDB servers", f"{default_count} (auto-detected from capture)")
        else:
            default_count = self.hints.mongo_node_count if self.hints.mongo_node_count > 0 else 1
            if self.hints.mongo_node_count > 0:
                _auto_fill_note("MongoDB servers", f"{self.hints.mongo_node_count} (from topology)")

        self.data["mongo_node_count"] = _ask_int(
            "Number of MongoDB servers (total, including the primary):", default=default_count
        )

        if self.hints.same_datacenter.get("mongodb"):
            _auto_fill_note("Same datacenter as Platform", "Yes (auto-detected — same host as Platform)")
        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Is MongoDB in the same datacenter as Platform?", default=True
        )

        if self.data["mongo_node_count"] > 1:
            self.data["replicas_across_datacenters"] = _ask_confirm(
                "Are replica members distributed across multiple datacenters?"
            )
            if self.data["replicas_across_datacenters"]:
                self.data["datacenter_distribution"] = _ask_text(
                    "  Describe distribution (e.g., 2 in us-east-1, 1 arbiter in us-west-2):"
                )

        self.data["server_specs"] = _collect_server_specs("MongoDB", mongodb_autofill.get("server_specs"))

        return self.data


class RedisArchitectureCollector(ArchitectureSection):
    """Redis topology, sentinel config, and server specs"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="redis", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Redis Architecture",
            "Deployment topology and server specs.",
        )

        redis_autofill = self.hints.autofill.get("redis") or {}

        # A prior autofill run's capture-detected topology (redis-py's own
        # reported mode — see architecture_autofill.py) beats a guess from
        # the deployment mode name alone.
        default_topology = redis_autofill.get("deployment_type", "")
        if default_topology:
            _auto_fill_note("Topology", f"{default_topology} (auto-detected)")
        elif self.hints.deployment_mode == "ha2":
            default_topology = "Sentinel (recommended for HA)"
            _auto_fill_note("Topology", f"{default_topology} (from deployment mode: {self.hints.deployment_mode})")
        elif self.hints.deployment_mode == "standalone":
            default_topology = "Single Instance"
            _auto_fill_note("Topology", f"{default_topology} (from deployment mode: {self.hints.deployment_mode})")

        self.data["deployment_type"] = _ask_select(
            "Redis deployment topology:",
            choices=["Single Instance", "Sentinel (recommended for HA)", "Cluster", "Other"],
            default=default_topology,
        )

        # Pre-fill node count from topology
        default_node_count = self.hints.redis_node_count if self.hints.redis_node_count > 0 else 3
        if self.hints.redis_node_count > 0:
            _auto_fill_note("Redis nodes", f"{self.hints.redis_node_count} (from topology)")

        self.data["redis_node_count"] = _ask_int(
            "Number of Redis server nodes:", default=default_node_count
        )

        if self.data["deployment_type"].startswith("Sentinel"):
            # A live pgrep count from a prior autofill run beats the topology
            # node-count guess.
            detected_sentinel = redis_autofill.get("sentinel_count")
            if detected_sentinel:
                default_sentinel = int(detected_sentinel)
                _auto_fill_note("Sentinel instances", f"{default_sentinel} (auto-detected)")
            else:
                default_sentinel = self.hints.redis_node_count if self.hints.redis_node_count > 0 else 3
            self.data["sentinel_count"] = _ask_int(
                "Number of Sentinel instances:", default=default_sentinel
            )

        if self.hints.same_datacenter.get("redis"):
            _auto_fill_note("Same datacenter as Platform", "Yes (auto-detected — same host as Platform)")
        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Are Redis nodes in the same datacenter as Platform?", default=True
        )

        if self.data["redis_node_count"] > 1:
            self.data["nodes_across_datacenters"] = _ask_confirm(
                "Are Redis nodes distributed across multiple datacenters?"
            )

        self.data["server_specs"] = _collect_server_specs("Redis", redis_autofill.get("server_specs"))

        return self.data


class LoadBalancerArchitectureCollector(ArchitectureSection):
    """Load balancer config, health checks, and routing"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="load_balancer", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Load Balancer",
            "Type, routing policy, health checks, and session configuration.",
        )

        has_lb = _ask_confirm(
            "Is a load balancer deployed in front of Platform?", default=False,
        )
        if not has_lb:
            self.data["present"] = False
            return self.data
        self.data["present"] = True

        # Type
        self.data["lb_type"] = _ask_select(
            "Load balancer type:",
            choices=[
                "F5 BIG-IP", "Nginx", "AWS ALB", "AWS NLB",
                "Azure Load Balancer", "HAProxy", "Other",
            ],
        )
        if self.data["lb_type"] == "Other":
            self.data["lb_type_other"] = _ask_text("  Specify (e.g., Traefik, Envoy, Citrix):")

        # Routing + Stickiness
        self.data["routing_policy"] = _ask_select(
            "Routing policy:",
            choices=["Round Robin", "Least Connections", "IP Hash", "Weighted", "Other"],
        )
        self.data["session_stickiness"] = _ask_confirm(
            "Is session stickiness (sticky sessions) enabled?"
        )
        if not self.data["session_stickiness"]:
            console.print(
                f"  [{theme.warning}]Session stickiness is recommended for Platform.[/{theme.warning}]"
            )

        # Health Checks
        _subsection("Health Checks")
        self.data["platform_health_endpoint"] = _ask_text(
            "Platform health check endpoint (e.g., /health/status):",
            default="/health/status?exclude-services=true",
            required=False,
        )
        self.data["health_check_interval"] = _ask_select(
            "Health check interval:",
            choices=["5 seconds", "10 seconds", "30 seconds", "60 seconds", "Other", "Unknown"],
        )

        return self.data


class KubernetesArchitectureCollector(ArchitectureSection):
    """Kubernetes-specific configuration — probes, deployment method, resources"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="kubernetes", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Kubernetes Configuration",
            "Kubernetes-specific settings: deployment method, probe configuration, and resource allocation.",
        )

        is_k8s_detected = self.hints.deployment_mode == "kubernetes"
        if is_k8s_detected:
            _auto_fill_note("Deployed on Kubernetes", "Yes (from this environment's deployment mode)")
        is_k8s = _ask_confirm(
            "Is this environment deployed on Kubernetes?", default=is_k8s_detected
        )
        if not is_k8s:
            self.data["deployed_on_kubernetes"] = False
            return self.data

        self.data["deployed_on_kubernetes"] = True

        # K8s provider — already captured in environment, but confirm flavor
        self.data["k8s_distribution"] = _ask_select(
            "Kubernetes distribution:",
            choices=["AWS EKS", "Azure AKS", "Google GKE", "OpenShift", "Rancher (RKE/RKE2)", "Self-Managed (kubeadm)", "Other"],
        )

        # Node infrastructure
        _subsection("Kubernetes Node Infrastructure")
        self.data["node_instance_type"] = _ask_text(
            "Worker node instance type (e.g., m5.2xlarge, c6i.4xlarge) — leave blank if unknown:",
            required=False,
        )
        self.data["node_count"] = _ask_int(
            "Number of worker nodes in the cluster (0 = unknown):", default=0
        )

        # Deployment method
        _subsection("Kubernetes Deployment Method")
        self.data["deployment_method"] = _ask_select(
            "How are Itential components deployed to Kubernetes?",
            choices=["Helm Charts (recommended)", "ArgoCD + Helm", "FluxCD", "kubectl / Raw Manifests", "Kustomize", "Other"],
        )
        if "Helm" not in self.data["deployment_method"]:
            console.print(
                f"  [{theme.warning}]Itential recommends Helm charts for consistent, repeatable deployments.[/{theme.warning}]"
            )

        # HPA / autoscaling
        _subsection("Kubernetes Autoscaling (HPA)")
        self.data["hpa_enabled"] = _ask_confirm(
            "Is Horizontal Pod Autoscaler (HPA) configured for Itential pods?"
        )
        if self.data["hpa_enabled"]:
            self.data["hpa_min_replicas"] = _ask_int(
                "  HPA minimum replica count:", default=2
            )
            self.data["hpa_max_replicas"] = _ask_int(
                "  HPA maximum replica count:", default=5
            )
            self.data["hpa_scaling_metric"] = _ask_select(
                "  Scaling metric:",
                choices=["CPU utilization", "Memory utilization", "Both CPU & Memory", "Custom metric"],
            )

        # Helm values / resource allocation
        _subsection("Kubernetes Resource Configuration")
        self.data["has_custom_resources"] = _ask_confirm(
            "Have you customized CPU/memory limits or Helm values for Itential pods?"
        )
        if self.data["has_custom_resources"]:
            self.data["values_source"] = _ask_select(
                "Where are your Helm values / resource configs stored?",
                choices=["Git repository", "Local files on bastion/admin host", "ArgoCD ApplicationSet", "Rancher Fleet", "Other", "Unknown"],
            )
            self.data["resource_notes"] = _ask_text(
                "  Any notable resource overrides? (e.g., Platform pods set to 8Gi memory, 4 CPU):",
                required=False,
            )

        # Pod health
        _subsection("Pod Health")
        self.data["pod_restarts_observed"] = _ask_confirm(
            "Have pod restarts been observed?"
        )
        if self.data["pod_restarts_observed"]:
            self.data["pod_restart_notes"] = _ask_text(
                "  Approximate frequency or restart count (e.g., 3 restarts in the last 24h):",
                required=False,
            )

        # Probes — simplified
        _subsection("Kubernetes Probe Configuration")
        self.data["probes_customized"] = _ask_confirm(
            "Have liveness/readiness/startup probes been customized from Itential defaults?"
        )
        if self.data["probes_customized"]:
            self.data["probe_notes"] = _ask_text(
                "  Describe the changes (e.g., startup probe timeout increased to 120s):",
                required=False,
            )

        return self.data


class MonitoringHealthCheckCollector(ArchitectureSection):
    """Monitoring tools and health check mechanisms in use"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="monitoring", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Monitoring & Health Checks",
            "Monitoring tools and observability mechanisms for the Itential environment.",
        )

        monitoring_autofill = self.hints.autofill.get("monitoring") or {}
        detected_tools = monitoring_autofill.get("monitoring_tools") or []
        if detected_tools:
            _auto_fill_note("Monitoring tools", f"{', '.join(detected_tools)} (auto-detected)")

        # Primary monitoring tools
        self.data["monitoring_tools"] = _ask_checkbox(
            "Which monitoring tools are in use? (select all that apply)",
            choices=[
                "Prometheus",
                "Grafana",
                "Datadog",
                "Splunk",
                "New Relic",
                "Dynatrace",
                "Zabbix",
                "Nagios / Icinga",
                "AWS CloudWatch",
                "Azure Monitor",
                "Elastic / ELK Stack",
                "PagerDuty (alerting only)",
                "Itential Platform APIs",
                "Other",
                "None",
            ],
            pre_checked=detected_tools,
        )

        if "Other" in self.data["monitoring_tools"]:
            self.data["monitoring_tools_other"] = _ask_text(
                "  Specify other monitoring tools:"
            )

        if "None" in self.data["monitoring_tools"]:
            console.print(
                f"  [{theme.warning}]Itential recommends implementing monitoring for production environments.[/{theme.warning}]"
            )
            console.print(
                f"  [{theme.text_dim}]Prometheus and Grafana are open-source options that work well with "
                f"Itential Platform. Note that monitoring tools require their own resource allocation.[/{theme.text_dim}]"
            )
        else:
            # What's being monitored
            _subsection("Monitoring Coverage")
            self.data["monitored_components"] = _ask_checkbox(
                "Which Itential components are actively monitored? (select all that apply)",
                choices=[
                    "Platform (IAP) — application health and performance",
                    "MongoDB — replica set status, query performance, disk usage",
                    "Redis — memory usage, key eviction, sentinel status",
                    "Automation Gateway — job execution, health endpoints",
                    "Host / VM — CPU, memory, disk, network at the OS level",
                    "Load Balancer — backend health, request rates",
                    "None of the above are monitored specifically",
                ],
            )

            # Alerting
            _subsection("Alerting")
            self.data["has_alerting"] = _ask_confirm(
                "Are automated alerts configured for Itential component failures?"
            )
            if self.data["has_alerting"]:
                self.data["alert_channels"] = _ask_checkbox(
                    "  Alert delivery channels (select all that apply):",
                    choices=[
                        "Email",
                        "Slack / Teams",
                        "PagerDuty / OpsGenie",
                        "ServiceNow / Ticketing",
                        "SMS",
                        "Other",
                    ],
                )

            # Platform health endpoint monitoring
            _subsection("Platform Health Endpoint")
            self.data["monitors_health_endpoint"] = _ask_confirm(
                "Is the Platform /health/status endpoint being polled by an external monitor?",
                default=False,
            )
            if self.data["monitors_health_endpoint"]:
                self.data["health_poll_interval"] = _ask_select(
                    "  Polling interval:",
                    choices=["10 seconds", "30 seconds", "60 seconds", "5 minutes", "Other", "Unknown"],
                )

            # Log aggregation
            _subsection("Log Aggregation")
            self.data["has_log_aggregation"] = _ask_confirm(
                "Are Itential Platform logs being shipped to a central log aggregator?",
                default=False,
            )
            if self.data["has_log_aggregation"]:
                detected_log_agg = monitoring_autofill.get("log_aggregator", "")
                if detected_log_agg:
                    _auto_fill_note("Log aggregator", f"{detected_log_agg} (auto-detected)")
                self.data["log_aggregator"] = _ask_select(
                    "  Log aggregation tool:",
                    choices=["Splunk", "Elastic / ELK", "Datadog Logs", "Grafana Loki",
                             "AWS CloudWatch Logs", "Azure Log Analytics", "Other"],
                    default=detected_log_agg,
                )
                if self.data["log_aggregator"] == "Other":
                    self.data["log_aggregator_other"] = _ask_text(
                        "  Specify log aggregation tool:"
                    )

        return self.data


class NetworkSecurityCollector(ArchitectureSection):
    """Network connectivity and security standards — combined into one section"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="network_security", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Network & Security",
            "Network configuration and security compliance standards.",
        )

        network_autofill = self.hints.autofill.get("network_security") or {}

        # MTU — autofill stores the plain detected number; map it onto this
        # UI's own option string here (the browser form's option text differs
        # slightly in punctuation, so the raw value is what's actually shared).
        mtu_raw = str(network_autofill.get("mtu_size_raw") or "").strip()
        mtu_default = {
            "1500": "1500 (Standard — recommended)",
            "9000": "9000 (Jumbo Frames)",
        }.get(mtu_raw, "Other" if mtu_raw.isdigit() else "")
        if mtu_default:
            _auto_fill_note("MTU size", f"{mtu_raw} bytes (auto-detected)")

        self.data["mtu_size"] = _ask_select(
            "MTU size across platform network:",
            choices=["1500 (Standard — recommended)", "9000 (Jumbo Frames)", "Other", "Unknown"],
            default=mtu_default,
        )
        if self.data["mtu_size"] == "9000 (Jumbo Frames)":
            console.print(
                f"  [{theme.warning}]MTU 9000 has been observed to cause issues with Platform. MTU 1500 is recommended.[/{theme.warning}]"
            )
        elif self.data["mtu_size"] == "Other":
            self.data["mtu_size_other"] = _ask_text(
                "  Specify MTU size (bytes, e.g., 1492):",
                default=mtu_raw if mtu_default == "Other" else "",
            )

        # Connectivity concerns
        self.data["has_connectivity_concerns"] = _ask_confirm(
            "Any known network concerns between components? (latency, firewalls, VPNs)"
        )
        if self.data["has_connectivity_concerns"]:
            self.data["connectivity_notes"] = _ask_text(
                "  Describe (e.g., 15ms latency between Platform and MongoDB across VPN):"
            )

        # Security
        _subsection("Security Standards")
        detected_selinux = network_autofill.get("selinux_mode", "")
        if detected_selinux:
            _auto_fill_note("SELinux mode", f"{detected_selinux} (auto-detected)")
        self.data["selinux_mode"] = _ask_select(
            "SELinux mode:",
            choices=["Enforcing", "Permissive", "Disabled", "N/A (Containers / Non-Linux)"],
            default=detected_selinux,
        )
        detected_compliance = network_autofill.get("compliance_standards") or []
        if detected_compliance:
            _auto_fill_note("Compliance standards", f"{', '.join(detected_compliance)} (auto-detected)")
        self.data["compliance_standards"] = _ask_checkbox(
            "Security compliance standards enabled (select all that apply):",
            choices=[
                "FIPS 140-2", "FIPS 140-3", "DISA STIG",
                "CIS Benchmarks", "None", "Other",
            ],
            pre_checked=detected_compliance,
        )
        if "Other" in self.data["compliance_standards"]:
            self.data["compliance_other"] = _ask_text(
                "  Specify (e.g., FedRAMP, SOC 2, PCI-DSS):"
            )

        return self.data


class VulnerabilityAssessmentsCollector(ArchitectureSection):
    """Vulnerability assessment program — tools, frequency, ownership."""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="vulnerability_assessments", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Vulnerability Assessments",
            "How (and whether) this environment is scanned for known vulnerabilities.",
        )

        detected_tools = (self.hints.autofill.get("vulnerability_assessments") or {}).get("tools") or []
        if detected_tools:
            _auto_fill_note("Detected scanner agent(s)", f"{', '.join(detected_tools)} (auto-detected — running or installed)")

        cadence = _ask_select(
            "Does this environment undergo vulnerability assessments?",
            choices=["Yes — regularly", "Yes — ad-hoc / on demand", "No"],
        )
        self.data["performs_assessments"] = cadence

        if cadence == "No":
            return self.data

        self.data["tools"] = _ask_checkbox(
            "Which tool(s) are used? (select all that apply)",
            choices=[
                "Tenable / Nessus",
                "Qualys VMDR",
                "Rapid7 InsightVM",
                "Wiz",
                "Prisma Cloud / Twistlock",
                "Aqua Security",
                "CrowdStrike Falcon",
                "Trivy",
                "Grype",
                "Snyk",
                "Anchore",
                "OpenSCAP",
                "In-house / custom tooling",
                "Other",
            ],
            pre_checked=detected_tools,
        )
        if "Other" in self.data["tools"]:
            self.data["tools_other"] = _ask_text(
                "  Specify (e.g., Acunetix, Black Duck, Tanium Comply):"
            )

        self.data["frequency"] = _ask_select(
            "How often are assessments run?",
            choices=[
                "Continuously (always-on)",
                "Monthly",
                "Quarterly",
                "Annually",
                "Ad-hoc / on demand",
                "Unknown",
            ],
        )

        return self.data


class ArtificialIntelligenceCollector(ArchitectureSection):
    """AI/LLM usage alongside Itential — which provider, if any."""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="artificial_intelligence", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Artificial Intelligence",
            "Which LLM (if any) your organization uses alongside Itential.",
        )

        self.data["llm_provider"] = _ask_select(
            "Which LLM does your organization use with Itential?",
            choices=[
                "Claude", "ChatGPT", "Gemini", "Copilot",
                "Amazon Bedrock", "Mistral", "Ollama", "Other", "None",
            ],
        )
        if self.data["llm_provider"] == "Other":
            self.data["llm_provider_other"] = _ask_text(
                "  Specify LLM/provider:"
            )

        return self.data


# ─────────────── PROGRESS TRACKING (per-environment) ─────────────── #


@dataclass
class ArchitectureProgress:
    """Tracks which architecture sections have been collected for ONE environment.

    Architecture answers are stored per environment under
    ``~/.atlas/architecture/<env>.json`` so prod / staging / dev can carry
    different deployments. Use the ``architecture_store`` helpers for I/O —
    this class is just an in-memory view with the resume helpers.
    """
    environment: str = ""
    completed: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    status: str = "in_progress"

    def is_done(self, section_name: str) -> bool:
        return section_name in self.completed or section_name in self.skipped

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    def save(self) -> None:
        """Persist this env's progress under ~/.atlas/architecture/<env>.json"""
        from platform_atlas.core import architecture_store
        architecture_store.save(self.environment, {
            "completed": self.completed,
            "skipped": self.skipped,
            "status": self.status,
        })
        logger.debug(
            "Architecture progress saved (env=%s, %d sections done)",
            self.environment or "_default", len(self.completed),
        )

    @classmethod
    def load(cls, environment: str = "") -> ArchitectureProgress:
        """Load this env's progress from disk, or return a fresh record."""
        from platform_atlas.core import architecture_store
        data = architecture_store.load(environment)
        return cls(
            environment=environment or data.get("environment_name", ""),
            completed=data.get("completed") or {},
            skipped=list(data.get("skipped") or []),
            status=str(data.get("status") or "in_progress"),
        )


# ─────────────── ORCHESTRATOR ─────────────── #

def _sections_for_tier(sections: list[ArchitectureSection]) -> list[ArchitectureSection]:
    """Scope the section list to the active tier.

    SaaS audits are gateway-anchored: the Platform/MongoDB/Redis sections
    (and the other gateway's) are dropped entirely — those systems don't
    exist in that world. Load Balancer and Kubernetes stay (gateways may
    sit behind an LB; a containerized GW5 may live in K8s). Standard never
    reaches here (run_architecture_collection returns early); Extended
    keeps everything.
    """
    try:
        from platform_atlas.core.context import ctx
        if not ctx().is_saas:
            return sections
        kind = (ctx().config.saas_gateway_kind or "").strip().lower()
    except Exception:
        return sections
    drop: tuple[type, ...] = (
        PlatformArchitectureCollector,
        MongoDBArchitectureCollector,
        RedisArchitectureCollector,
    )
    if kind == "gateway4":
        drop += (Gateway5ArchitectureCollector,)
    elif kind == "gateway5":
        drop += (Gateway4ArchitectureCollector,)
    return [s for s in sections if not isinstance(s, drop)]


@dataclass
class ArchitectureValidationCollector:
    """Orchestrates all manual architecture validation data collection for ONE env.

    Architecture answers are stored at ``~/.atlas/architecture/<env>.json`` so
    prod / staging / dev can carry different deployments. Run-time pre-fill
    sources (in order of precedence): user's already-saved answers for this env,
    optional copy-from-source-env, topology hints from the active config.
    """

    environment: str = ""
    sections: list[ArchitectureSection] = field(default_factory=list)
    progress: ArchitectureProgress = field(init=False)

    def __post_init__(self) -> None:
        # Build topology hints for THIS env explicitly when one was given —
        # never the active environment's, which may be a different env
        # entirely (e.g. `env architecture prod` while `qa` is active).
        hints = TopologyHints.from_config(self.environment or None)

        if hints.environment_name:
            logger.debug(
                "Topology hints loaded for environment '%s': "
                "iap=%d, mongo=%d, redis=%d, gw4=%s, gw5=%s",
                hints.environment_name,
                hints.iap_node_count,
                hints.mongo_node_count,
                hints.redis_node_count,
                hints.has_gateway4,
                hints.has_gateway5,
            )

        # Resolve the env we're scoping to. Prefer the explicit constructor
        # argument; fall back to the active env from topology hints.
        if not self.environment and hints.environment_name:
            self.environment = hints.environment_name

        self.sections = _sections_for_tier([
            EnvironmentOverviewCollector(hints),
            PlatformArchitectureCollector(hints),
            Gateway4ArchitectureCollector(hints),
            Gateway5ArchitectureCollector(hints),
            MongoDBArchitectureCollector(hints),
            RedisArchitectureCollector(hints),
            LoadBalancerArchitectureCollector(hints),
            KubernetesArchitectureCollector(hints),
            MonitoringHealthCheckCollector(hints),
            NetworkSecurityCollector(hints),
            VulnerabilityAssessmentsCollector(hints),
            ArtificialIntelligenceCollector(hints),
        ])
        self.progress = ArchitectureProgress.load(self.environment)

    @property
    def pending_sections(self) -> list[ArchitectureSection]:
        """Sections not yet completed or skipped"""
        return [s for s in self.sections if not self.progress.is_done(s.name)]

    def seed_from_env(self, source_env: str) -> int:
        """Pre-fill ``completed`` from another environment's saved answers.

        Returns the number of sections seeded. The user can still walk through
        each section and tweak values — sections aren't marked done until the
        user actually confirms them via ``collect()``. Called only on a fresh
        run (no existing answers for the destination env).
        """
        from platform_atlas.core import architecture_store
        try:
            source_data = architecture_store.load(source_env)
        except ValueError as exc:
            console.print(f"[{theme.error}]Could not load env '{source_env}': {exc}[/{theme.error}]")
            return 0
        source_completed = source_data.get("completed") or {}
        if not source_completed:
            console.print(
                f"[{theme.warning}]Env '{source_env}' has no completed sections to copy from.[/{theme.warning}]"
            )
            return 0
        # Stash on each section so the section's own _ask_*-with-default
        # prompts can use the seeded value when the user confirms.
        seeded = 0
        for section in self.sections:
            seed = source_completed.get(section.name)
            if isinstance(seed, dict) and seed:
                section.data = json.loads(json.dumps(seed))  # deep copy
                seeded += 1
        return seeded

    def adopt_from_env(self, source_env: str) -> int:
        """Copy a source env's answers into THIS env wholesale, mark complete.

        For the "prod and dev are identical" case — no per-field walk-through.
        Returns the number of sections adopted.
        """
        from platform_atlas.core import architecture_store
        result = architecture_store.copy(source_env, self.environment)
        self.progress.completed = result.get("completed") or {}
        self.progress.skipped = list(result.get("skipped") or [])
        self.progress.status = result.get("status") or "complete"
        return len(self.progress.completed)

    def collect_all(self, force: bool = False) -> dict[str, Any]:
        """Run all section collectors and return a unified dict.

        Args:
            force: If True, re-collect all sections even if already complete.
        """
        # Under --force, walk every section regardless of prior completion —
        # but leave self.progress (loaded from disk) untouched until a
        # section is actually re-confirmed, so an abort before that point
        # saves back the same data that was already there instead of wiping it.
        pending = self.sections if force else self.pending_sections

        if not pending:
            # Everything's already answered/skipped — heal the status flag so
            # downstream consumers that key off it agree with reality.
            if self.progress.status != "complete":
                self.progress.status = "complete"
                self.progress.save()
            console.print(
                f"\n[{theme.success}]All architecture sections already "
                f"collected for env=[bold]{self.environment or '_default'}[/bold].[/{theme.success}]"
            )
            return {"architecture_validation": self.progress.completed}

        # Show resume info if we have partial progress
        if self.progress.completed and not force:
            done_names = ", ".join(self.progress.completed.keys())
            console.print(
                f"[{theme.text_dim}]Already collected: {done_names}[/{theme.text_dim}]"
            )

        # Show hints info if environment is active
        hints = self.sections[0].hints if self.sections else TopologyHints()
        env_label = self.environment or hints.environment_name or "_default"
        hints_note = (
            f"\n[{theme.accent}]Environment:[/{theme.accent}] [bold]{env_label}[/bold]"
            f"\n[{theme.text_dim}]Answers are saved per-environment. "
            f"Topology values from your config will be pre-filled where possible.[/{theme.text_dim}]"
        )

        console.print(Panel(
            "[bold]This section collects architecture details that cannot be gathered\n"
            "through automated data collection. Please have your infrastructure\n"
            "documentation available for reference.[/]\n\n"
            f"Remaining: {len(pending)} of {len(self.sections)} sections\n"
            f"Progress is saved — you can [bold]quit (Ctrl+C)[/bold] and resume anytime."
            f"{hints_note}",
            title=f"[bold {theme.primary}]Architecture Validation — Manual Collection[/]",
            border_style=theme.primary,
            padding=(1, 2),
        ))

        try:
            for section in pending:
                result = section.collect()
                self.progress.completed[section.name] = result
                self.progress.save()
        except KeyboardInterrupt:
            self.progress.save()
            console.print(
                f"\n[{theme.warning}]Architecture collection paused — "
                f"progress saved.[/{theme.warning}]"
            )
            console.print(
                f"[{theme.text_dim}]Run the same command again to "
                f"resume.[/{theme.text_dim}]"
            )
            raise

        # Mark complete
        self.progress.status = "complete"
        self.progress.save()

        return {"architecture_validation": self.progress.completed}

    def collect_section(self, section_name: str) -> dict[str, Any]:
        """Run a single section collector by name"""
        for section in self.sections:
            if section.name == section_name:
                result = section.collect()
                self.progress.completed[section.name] = result
                self.progress.save()
                return {section.name: result}

        available = [s.name for s in self.sections]
        raise ValueError(
            f"Unknown section: {section_name!r}. Available: {available}"
        )


def _ask_architecture_input_method(*, allow_autofill: bool = False) -> str | None:
    """Ask whether to fill out the architecture form in the browser or the terminal.

    ``allow_autofill`` adds a fourth choice that runs a best-effort SSH
    auto-detect pass first — offered only in the Extended tier (the only
    tier with SSH access to the whole fleet). It leads the list as the
    recommended path; otherwise the browser form does.
    """
    browser_label = "In my browser  — fill out a form, then finish from the CLI"
    if allow_autofill:
        choices = [
            questionary.Choice(
                ui.preferred_choice_title(
                    "Auto-detect what I can first, then fill in the rest  — read-only SSH checks"
                ),
                value="autofill",
            ),
            ui.choice_divider(),
            questionary.Choice(ui.alternate_choice_title(browser_label), value="browser"),
        ]
    else:
        choices = [
            questionary.Choice(ui.preferred_choice_title(browser_label), value="browser"),
            ui.choice_divider(),
        ]
    choices += [
        questionary.Choice(
            ui.alternate_choice_title("Here in the terminal  — a guided step-by-step walkthrough"),
            value="cli",
        ),
        questionary.Choice(
            ui.alternate_choice_title("Not right now  — cancel, nothing changes"),
            value="cancel",
        ),
    ]
    return questionary.select(
        "How would you like to fill out the architecture form?",
        choices=choices,
        style=ui.dim_divider_style(ATLAS_STYLE),
    ).ask()


def run_architecture_autofill(environment: str) -> None:
    """Run the best-effort, read-only SSH auto-detect pass and stage the results.

    Results are saved to ``architecture_store``'s ``autofill`` bucket — advisory
    suggestions, never a substitute for the user's own confirmed answers (see
    ``capture/collectors/architecture_autofill.py``). Never raises: a failed
    probe is reported to the user as a plain note, not an error, and the
    architecture flow always continues afterward regardless of outcome.
    """
    from platform_atlas.capture.collectors import architecture_autofill
    from platform_atlas.core import architecture_store

    console.print(Panel(
        "[bold]This runs a small set of read-only commands over SSH[/] against each node "
        "in your topology to pre-fill server specs (OS, CPU, memory, disk), container/VM/"
        "Kubernetes signals, SELinux mode, the FIPS flag, MTU, and running monitoring/log/"
        "vulnerability-scanner services — plus MongoDB replica count and Redis topology, "
        "reused from your most recent capture instead of new SSH calls.\n\n"
        "Nothing is written or changed on any node. Anything a check can't determine "
        "(missing binary, no permission) is simply left blank — you'll still confirm or "
        "fill in every field yourself next.",
        title=f"[bold {theme.primary}]Architecture Auto-Detect[/]",
        border_style=theme.primary,
        padding=(1, 2),
    ))
    if not _ask_confirm("Run these read-only checks now?", default=True):
        console.print(f"  [{theme.text_dim}]Skipped — nothing changed.[/{theme.text_dim}]\n")
        return

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

    console.print()
    result = None
    error: Exception | None = None
    with Progress(
        SpinnerColumn(style=theme.primary),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=26, complete_style=theme.primary, finished_style=theme.success),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        MofNCompleteColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Preparing auto-detect…", total=None)

        def _on_progress(done: int, total: int, label: str) -> None:
            # First real step arrives with a concrete total; before that the
            # bar shows an indeterminate pulse under "Preparing…".
            progress.update(task, total=total, completed=done, description=label)

        try:
            result = architecture_autofill.probe_environment(environment, on_progress=_on_progress)
        except Exception as exc:  # noqa: BLE001 — auto-detect must never break the flow
            error = exc
            progress.update(task, description=f"[{theme.warning}]Auto-detect could not run[/{theme.warning}]")

    if error is not None:
        console.print(f"\n  [{theme.warning}]Auto-detect could not run: {error}[/{theme.warning}]\n")
        return

    console.print()
    for node in result.node_summaries:
        label = node.node_name if node.display_role in ("", node.node_name) else f"{node.display_role} ({node.node_name})"
        console.print(f"  [{theme.text_dim}]{label}:[/{theme.text_dim}] {len(node.facts)} fact(s) checked")
        for note in node.notes:
            console.print(f"    [{theme.text_dim}]↳ {note}[/{theme.text_dim}]")

    if result.used_capture_session:
        console.print(
            f"  [{theme.text_dim}]MongoDB/Redis topology reused from capture "
            f"'{result.used_capture_session}'.[/{theme.text_dim}]"
        )

    architecture_store.save_autofill(environment, result.completed)
    console.print(
        f"\n[{theme.success}]✓[/{theme.success}] Auto-detect finished — suggestions saved for "
        f"{len(result.completed)} section(s). They'll show up as pre-filled defaults next.\n"
    )


def _resolve_env_for_arch(explicit_env: str | None) -> str:
    """Pick the env this architecture run targets — explicit > active > default."""
    if explicit_env:
        return explicit_env
    try:
        from platform_atlas.core.context import ctx
        env = ctx().active_environment
        if env:
            return env
    except Exception:
        pass
    return ""


def _maybe_offer_copy_from(target_env: str) -> str | None:
    """Offer to copy answers from another env. Returns the source env name, or None.

    Skipped silently when the target already has any answers, when no other env
    has data, or when the active session is the only one with data. The prompt
    is intentionally compact — answering No proceeds with the normal flow.
    """
    from platform_atlas.core import architecture_store
    if architecture_store.has_data(target_env):
        return None  # don't clobber existing answers without an explicit user gesture
    sources = [
        e for e in architecture_store.list_envs_with_data()
        if e != (target_env or architecture_store.DEFAULT_ENV_KEY)
        and architecture_store.has_data(e)
    ]
    if not sources:
        return None

    target_label = target_env or "this environment"
    pretty = ", ".join(sources)
    console.print(
        f"\n[{theme.accent}]Tip:[/{theme.accent}] you've already answered the architecture "
        f"questionnaire for: [bold]{pretty}[/bold]."
    )
    console.print(
        f"[{theme.text_dim}]If [bold]{target_label}[/bold] mostly matches one of those, you can "
        f"copy those answers in and tweak just what's different.[/{theme.text_dim}]"
    )

    if not _ask_confirm(f"Copy answers from another environment into {target_label}?", default=True):
        return None

    if len(sources) == 1:
        chosen = sources[0]
    else:
        chosen = _ask_select("Copy answers from which environment?", choices=sources)

    mode = _ask_select(
        "How would you like to use those answers?",
        choices=[
            "Copy and use as-is (mark complete; I'll edit later if needed)",
            "Copy as starting values, then walk through each section to confirm/tweak",
        ],
    )
    return f"{'adopt' if mode.startswith('Copy and use as-is') else 'seed'}::{chosen}"


def run_architecture_collection(
    force: bool = False,
    *,
    environment: str | None = None,
) -> dict[str, Any]:
    """Entry point for the architecture validation manual collector.

    Architecture answers are scoped per-environment under
    ``~/.atlas/architecture/<env>.json``. The first run after upgrading
    silently migrates the legacy global ``architecture.json`` into the active
    environment's bucket via ``architecture_store.migrate_legacy()``.

    Asks the user, every time, whether to fill the form out in the browser or
    the terminal (mirrors ``env create`` / ``tier upgrade``). Choosing the
    browser opens the HTML form and imports the exported JSON; choosing the
    terminal (or falling back mid-browser-flow) runs the CLI prompts instead.

    Returns an empty payload when the active tier is Standard — the
    architecture form is not part of the app-only Standard tier and is
    silently skipped during capture. In SaaS the form runs gateway-scoped:
    the Platform/MongoDB/Redis sections (and the other gateway's) are
    dropped in both the HTML form and the CLI fallback.
    """
    is_extended = False
    try:
        from platform_atlas.core.context import ctx
        if ctx().is_standard:
            return {"architecture_validation": {}}
        is_extended = ctx().is_extended
    except Exception:
        pass

    # One-time migration from the legacy global architecture.json. No-op after
    # the sentinel exists, so this is safe to call on every invocation.
    try:
        from platform_atlas.core import architecture_store
        architecture_store.migrate_legacy()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Legacy architecture migration skipped: %s", exc)

    target_env = _resolve_env_for_arch(environment)

    method = _ask_architecture_input_method(allow_autofill=is_extended)
    if method is None:
        raise KeyboardInterrupt
    if method == "cancel":
        console.print(f"\n  [{theme.text_dim}]Cancelled — nothing changed.[/{theme.text_dim}]\n")
        return {"architecture_validation": {}}

    if method == "autofill":
        run_architecture_autofill(target_env)
        # Auto-detect only stages suggestions — still need browser vs. terminal
        # to actually walk through (or seed) the answers themselves.
        method = _ask_architecture_input_method(allow_autofill=False)
        if method is None:
            raise KeyboardInterrupt
        if method == "cancel":
            console.print(f"\n  [{theme.text_dim}]Cancelled — auto-detected suggestions are saved for next time.[/{theme.text_dim}]\n")
            return {"architecture_validation": {}}

    if method == "browser":
        from platform_atlas.core.html_collector import launch_architecture_form
        result = launch_architecture_form(environment=target_env)

        if result is None:
            # Form file couldn't be located — fall through to terminal prompts below
            pass
        elif not result:
            # User skipped architecture collection entirely
            return {"architecture_validation": {}}
        else:
            # Successful HTML export: result has 'completed', 'skipped', 'status'
            return {"architecture_validation": result.get("completed", {})}

    # Offer to copy answers from another env BEFORE building the collector,
    # so a successful "adopt" path skips section-by-section walk entirely.
    copy_choice = None
    try:
        copy_choice = _maybe_offer_copy_from(target_env)
    except KeyboardInterrupt:
        raise

    collector = ArchitectureValidationCollector(environment=target_env)

    if copy_choice:
        mode, _, source_env = copy_choice.partition("::")
        if mode == "adopt":
            adopted = collector.adopt_from_env(source_env)
            console.print(
                f"\n[{theme.success}]✓[/{theme.success}] Copied {adopted} sections "
                f"from [bold]{source_env}[/bold] → [bold]{target_env or '_default'}[/bold]."
            )
            console.print(
                f"[{theme.text_dim}]Re-run with [bold]--force[/bold] to walk through and tweak any answers.[/{theme.text_dim}]"
            )
            return {"architecture_validation": collector.progress.completed}
        if mode == "seed":
            seeded = collector.seed_from_env(source_env)
            console.print(
                f"\n[{theme.text_dim}]Pre-filled {seeded} sections from [bold]{source_env}[/bold] — "
                f"each value is yours to confirm or change.[/{theme.text_dim}]"
            )

    return collector.collect_all(force=force)


def load_architecture_progress(environment: str = "") -> dict[str, Any] | None:
    """Load completed architecture data for ``environment`` (active env if blank)."""
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    if progress.is_complete and progress.completed:
        return {"architecture_validation": progress.completed}
    return None


def architecture_status(environment: str = "") -> tuple[str, int, int]:
    """Return ``(state, done, total)`` for an environment's architecture interview.

    ``state`` is ``"complete"`` when every section is answered or skipped,
    ``"partial"`` when only some are, and ``"empty"`` when none are. Derived
    from actual section coverage rather than the stored ``status`` flag, which
    is unreliable — intermediate saves and some entry paths leave a fully
    answered file marked ``"in_progress"``.
    """
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    try:
        section_names = [s.name for s in ArchitectureValidationCollector(environment=env).sections]
    except Exception:  # pragma: no cover - defensive; fall back to saved keys
        section_names = list(progress.completed.keys())
    total = len(section_names)
    done = sum(1 for name in section_names if progress.is_done(name))
    if done <= 0:
        return ("empty", 0, total)
    if total and done >= total:
        return ("complete", done, total)
    return ("partial", done, total)


def load_architecture_data(environment: str = "") -> dict[str, Any]:
    """Return saved architecture sections for ``environment`` (active env if blank).

    Unlike :func:`load_architecture_progress`, this never gates on the
    ``status`` flag — any saved sections (complete or partial) are returned so
    partially filled forms still surface in the report. Returns ``{}`` when
    nothing has been collected.
    """
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    return dict(progress.completed or {})


if __name__ == "__main__":
    raise SystemExit(
        "This module is not meant to be run directly. Use: platform-atlas"
    )
