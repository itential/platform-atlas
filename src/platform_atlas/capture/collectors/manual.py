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
    ("question", "fg:#e0e0e0 bold"),
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

    @classmethod
    def from_config(cls) -> TopologyHints:
        """
        Build hints from the active config/environment.
        Returns empty hints if anything fails (no environment, no topology, etc.).
        """
        try:
            from platform_atlas.core.config import get_config
            config = get_config()
        except Exception:
            return cls()

        env_name = config.active_environment or ""
        env_desc = ""

        # Load environment description if available
        if env_name:
            try:
                from platform_atlas.core.environment import get_environment_manager
                mgr = get_environment_manager()
                if mgr.exists(env_name):
                    env = mgr.load(env_name)
                    env_desc = env.description or ""
            except Exception:
                pass

        # Extract topology node counts
        try:
            topology = config.topology
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
        )


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


def _ask_checkbox(message: str, choices: list[str]) -> list[str]:
    """Multi-select from a list of choices"""
    result = questionary.checkbox(
        message, choices=choices, style=ATLAS_STYLE
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

def _collect_server_specs(component_label: str) -> dict[str, str]:
    """Reusable prompt group for server/VM/container specs"""
    _subsection(f"{component_label} Server Specs")

    os_type = _ask_select(f"{component_label} — Operating System:", choices=OS_TYPES)
    result = {"os_type": os_type}
    if os_type == "Other":
        result["os_type_other"] = _ask_text("  Specify OS (e.g., Rocky Linux 9, Debian 12):")

    result["cpu_cores"] = _ask_select(
        f"{component_label} — CPU cores per server:",
        choices=["2", "4", "8", "16", "32", "64+", "Unknown"],
    )
    result["memory_gb"] = _ask_select(
        f"{component_label} — Memory (GB) per server:",
        choices=["4", "8", "16", "32", "64", "128+", "Unknown"],
    )
    result["disk_space_gb"] = _ask_select(
        f"{component_label} — Disk space (GB) per server:",
        choices=["20", "50", "100", "200", "500", "1000+", "Unknown"],
    )

    return result


def _collect_deployment_type(component_label: str) -> dict[str, str]:
    """Reusable prompt for deployment type with 'Other' fallback"""
    dep_type = _ask_select(
        f"{component_label} — Deployment type:",
        choices=DEPLOYMENT_TYPES,
    )
    result = {"deployment_type": dep_type}
    if dep_type == "Other":
        result["deployment_type_other"] = _ask_text("  Specify (e.g., LXC, Podman, Nomad):")
    return result


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
        self.data["hosting_provider"] = _ask_select(
            "Hosting provider:",
            choices=["AWS", "Azure", "GCP", "On-Premises", "Hybrid (On-Prem + Cloud)", "Other"],
        )
        if self.data["hosting_provider"] == "Other":
            self.data["hosting_provider_other"] = _ask_text(
                "  Specify provider (e.g., OCI, IBM Cloud, Equinix):"
            )

        return self.data


class PlatformArchitectureCollector(ArchitectureSection):
    """Platform (IAP) topology, deployment type, and server specs"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="platform", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "Platform Architecture",
            "Instance count, deployment type, and server specs for Itential Automation Platform.",
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
        self.data["deployment"] = _collect_deployment_type("Platform")
        self.data["server_specs"] = _collect_server_specs("Platform")

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
        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Are Gateway4 servers in the same datacenter as Platform?", default=True
        )

        # Specs
        self.data["deployment"] = _collect_deployment_type("Gateway4")
        self.data["server_specs"] = _collect_server_specs("Gateway4")

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
        self.data["deployment"] = _collect_deployment_type("Gateway5")
        self.data["server_specs"] = _collect_server_specs("Gateway5")

        return self.data


class MongoDBArchitectureCollector(ArchitectureSection):
    """MongoDB topology, replica set info, and server specs"""

    def __init__(self, hints: TopologyHints | None = None) -> None:
        super().__init__(name="mongodb", hints=hints or TopologyHints())

    def collect(self) -> dict[str, Any]:
        _section_banner(
            "MongoDB Architecture",
            "Replica set topology and server specs.",
        )

        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Is MongoDB in the same datacenter as Platform?", default=True
        )

        # Pre-fill replica count from topology
        default_count = self.hints.mongo_node_count if self.hints.mongo_node_count > 0 else 3
        if self.hints.mongo_node_count > 0:
            _auto_fill_note("Replica members", f"{self.hints.mongo_node_count} (from topology)")

        self.data["replica_count"] = _ask_int(
            "Number of MongoDB replica set members:", default=default_count
        )

        if self.data["replica_count"] > 1:
            self.data["replicas_across_datacenters"] = _ask_confirm(
                "Are replica members distributed across multiple datacenters?"
            )
            if self.data["replicas_across_datacenters"]:
                self.data["datacenter_distribution"] = _ask_text(
                    "  Describe distribution (e.g., 2 in us-east-1, 1 arbiter in us-west-2):"
                )

        self.data["server_specs"] = _collect_server_specs("MongoDB")

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

        # Infer default topology from deployment mode
        default_topology = ""
        if self.hints.deployment_mode == "ha2":
            default_topology = "Sentinel (recommended for HA)"
        elif self.hints.deployment_mode == "standalone":
            default_topology = "Single Instance"

        if default_topology:
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
            # Sentinel count defaults to same as redis node count in most setups
            default_sentinel = self.hints.redis_node_count if self.hints.redis_node_count > 0 else 3
            self.data["sentinel_count"] = _ask_int(
                "Number of Sentinel instances:", default=default_sentinel
            )

        self.data["same_datacenter_as_platform"] = _ask_confirm(
            "Are Redis nodes in the same datacenter as Platform?", default=True
        )

        if self.data["redis_node_count"] > 1:
            self.data["nodes_across_datacenters"] = _ask_confirm(
                "Are Redis nodes distributed across multiple datacenters?"
            )

        self.data["server_specs"] = _collect_server_specs("Redis")

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

        is_k8s = _ask_confirm(
            "Is this environment deployed on Kubernetes?", default=False
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
                "Other",
                "None",
            ],
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
                self.data["log_aggregator"] = _ask_select(
                    "  Log aggregation tool:",
                    choices=["Splunk", "Elastic / ELK", "Datadog Logs", "Grafana Loki",
                             "AWS CloudWatch Logs", "Azure Log Analytics", "Other"],
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

        # MTU
        self.data["mtu_size"] = _ask_select(
            "MTU size across platform network:",
            choices=["1500 (Standard — recommended)", "9000 (Jumbo Frames)", "Other", "Unknown"],
        )
        if self.data["mtu_size"] == "9000 (Jumbo Frames)":
            console.print(
                f"  [{theme.warning}]MTU 9000 has been observed to cause issues with Platform. MTU 1500 is recommended.[/{theme.warning}]"
            )
        elif self.data["mtu_size"] == "Other":
            self.data["mtu_size_other"] = _ask_text(
                "  Specify MTU size (bytes, e.g., 1492):"
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
        self.data["selinux_mode"] = _ask_select(
            "SELinux mode:",
            choices=["Enforcing", "Permissive", "Disabled", "N/A (Containers / Non-Linux)"],
        )
        self.data["compliance_standards"] = _ask_checkbox(
            "Security compliance standards enabled (select all that apply):",
            choices=[
                "FIPS 140-2", "FIPS 140-3", "DISA STIG",
                "CIS Benchmarks", "None", "Other",
            ],
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
        # Build topology hints from the active environment/config
        hints = TopologyHints.from_config()

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

        self.sections = [
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
        ]
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
        if force:
            self.progress = ArchitectureProgress(environment=self.environment)

        pending = self.pending_sections

        if not pending:
            console.print(
                f"\n[{theme.success}]All architecture sections already "
                f"collected for env=[bold]{self.environment or '_default'}[/bold].[/{theme.success}]"
            )
            return {"architecture_validation": self.progress.completed}

        # Show resume info if we have partial progress
        if self.progress.completed:
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


def _should_use_html() -> bool:
    """Return True if the config says to use the HTML collector (the default)."""
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if is_config_loaded():
            return getattr(get_config(), "manual_input_mode", "html") == "html"
    except Exception:
        pass
    return True  # default to HTML when config is unavailable


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

    When manual_input_mode is "html" (the default), opens the browser-based
    form and imports the exported JSON. Falls back to CLI prompts if the user
    chooses or if the form cannot be opened.

    Returns an empty payload when the active tier is Standard — architecture
    review is Extended-only and silently skipped during capture.
    """
    try:
        from platform_atlas.core.context import ctx
        if ctx().is_standard:
            return {"architecture_validation": {}}
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

    if not force and _should_use_html():
        from platform_atlas.core.html_collector import launch_architecture_form
        result = launch_architecture_form(environment=target_env)

        if result is None:
            # User chose CLI — fall through to terminal prompts below
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


if __name__ == "__main__":
    raise SystemExit(
        "This module is not meant to be run directly. Use: platform-atlas"
    )
