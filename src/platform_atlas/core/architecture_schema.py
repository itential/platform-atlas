"""
Architecture questionnaire — the single source of truth.

Every architecture surface (the terminal collectors, the standalone HTML form,
the WebUI page, the report banner, the pre-capture prompt) agrees on three
things, and this module owns all of them:

1. **What is asked** — sections, fields, kinds, option lists, and the
   conditions under which a follow-up applies.
2. **What counts as an answer** — every question is required. A question is
   answered when it holds a real value *or* an explicit out ("Unknown",
   "None", "N/A", a "Not present" gate). A blank is never an answer.
3. **How an out is stored** — select/text/multi outs are ordinary values
   (the option string, or what the user typed). Count and yes/no questions
   can't hold a string without changing their type, which downstream
   consumers (the JSON report schema, the warning rules) rely on, so their
   "Unknown" is recorded in a sibling ``"unknown": [<field keys>]`` list on
   the same object instead. The typed field is simply absent.

The three UIs stay hand-written; ``tests/test_architecture_schema.py`` fails
if their field keys, option lists or conditional wiring drift from this file.

Pure data + pure functions: no UI, no I/O, no context imports.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["select", "text", "int", "confirm", "multi", "clusters"]

UNKNOWN = "Unknown"
UNKNOWN_KEY = "unknown"  # sibling list holding keys of int/confirm fields answered "Unknown"

# ─────────────── OPTION LISTS ─────────────── #
# Every select / checkbox group offers an explicit out. environment_type is
# deliberately strict (no "Unknown") — see design/PLAN-architecture-completeness.md.

ENVIRONMENT_TYPES = ("Production", "Staging", "Development", "QA / Test", "DR / Failover")
HOSTING_PROVIDERS = ("AWS", "Azure", "GCP", "On-Premises", "Hybrid (On-Prem + Cloud)", "Other", UNKNOWN)
DEPLOYMENT_TYPES = (
    "Bare Metal", "Virtual Machines (VMs)", "Kubernetes (AWS EKS)", "Kubernetes (Azure AKS)",
    "Kubernetes (Self-Managed)", "AWS Fargate", "Docker Containers", "Other", UNKNOWN,
)
OS_TYPES = ("RHEL 8", "RHEL 9", "Rocky 8", "Rocky 9", "Alpine", "Amazon Linux 2", "Other", UNKNOWN)
CPU_CORES = ("2", "4", "8", "16", "32", "64+", UNKNOWN)
MEMORY_GB = ("4", "8", "16", "32", "64", "128+", UNKNOWN)
DISK_GB = ("20", "50", "100", "200", "500", "1000+", UNKNOWN)
GW4_DEVICE_COUNTS = ("1-500", "500-2000", "2000-5000", "5000-10000", "10000+", UNKNOWN)
GW4_MIGRATION_TIMELINES = ("Next 3 months", "3-6 months", "6-12 months", "12+ months", "No timeline yet", UNKNOWN)
GW5_HA_MODES = ("Active-Standby", "Active-Active", "Other", UNKNOWN)
MONGO_DEPLOYMENTS = ("Standalone", "Replica Set (recommended for HA)", "Sharded Cluster", "Other", UNKNOWN)
REDIS_DEPLOYMENTS = ("Single Instance", "Sentinel (recommended for HA)", "Cluster", "Other", UNKNOWN)
LB_TYPES = (
    "F5 BIG-IP", "Nginx", "AWS ALB", "AWS NLB", "Azure Load Balancer", "HAProxy", "Other", UNKNOWN,
)
LB_ROUTING_POLICIES = ("Round Robin", "Least Connections", "IP Hash", "Weighted", "Other", UNKNOWN)
LB_HEALTH_INTERVALS = ("5 seconds", "10 seconds", "30 seconds", "60 seconds", "Other", UNKNOWN)
K8S_DISTRIBUTIONS = (
    "AWS EKS", "Azure AKS", "Google GKE", "OpenShift", "Rancher (RKE/RKE2)",
    "Self-Managed (kubeadm)", "Other", UNKNOWN,
)
K8S_DEPLOYMENT_METHODS = (
    "Helm Charts (recommended)", "ArgoCD + Helm", "FluxCD", "kubectl / Raw Manifests", "Kustomize", "Other", UNKNOWN,
)
K8S_HPA_METRICS = ("CPU utilization", "Memory utilization", "Both CPU & Memory", "Custom metric", UNKNOWN)
K8S_VALUES_SOURCES = (
    "Git repository", "Local files on bastion/admin host", "ArgoCD ApplicationSet", "Rancher Fleet", "Other", UNKNOWN,
)
MONITORING_TOOLS = (
    "Prometheus", "Grafana", "Datadog", "Splunk", "New Relic", "Dynatrace", "Zabbix", "Nagios / Icinga",
    "AWS CloudWatch", "Azure Monitor", "Elastic / ELK Stack", "PagerDuty (alerting only)",
    "Itential Platform APIs", "Other", "None", UNKNOWN,
)
MONITORED_COMPONENTS = (
    "Platform (IAP) — application health and performance",
    "MongoDB — replica set status, query performance, disk usage",
    "Redis — memory usage, key eviction, sentinel status",
    "Automation Gateway — job execution, health endpoints",
    "Host / VM — CPU, memory, disk, network at the OS level",
    "Load Balancer — backend health, request rates",
    "None of the above are monitored specifically",
    UNKNOWN,
)
ALERT_CHANNELS = (
    "Email", "Slack / Teams", "PagerDuty / OpsGenie", "ServiceNow / Ticketing", "SMS", "Other", UNKNOWN,
)
HEALTH_POLL_INTERVALS = ("10 seconds", "30 seconds", "60 seconds", "5 minutes", "Other", UNKNOWN)
LOG_AGGREGATORS = (
    "Splunk", "Elastic / ELK", "Datadog Logs", "Grafana Loki", "AWS CloudWatch Logs",
    "Azure Log Analytics", "Other", UNKNOWN,
)
MTU_SIZES = ("1500 (Standard — recommended)", "9000 (Jumbo Frames)", "Other", UNKNOWN)
SELINUX_MODES = ("Enforcing", "Permissive", "Disabled", "N/A (Containers / Non-Linux)", UNKNOWN)
COMPLIANCE_STANDARDS = ("FIPS 140-2", "FIPS 140-3", "DISA STIG", "CIS Benchmarks", "None", "Other", UNKNOWN)
VA_PERFORMS = ("Yes — regularly", "Yes — ad-hoc / on demand", "No", UNKNOWN)
VA_TOOLS = (
    "Tenable / Nessus", "Qualys VMDR", "Rapid7 InsightVM", "Wiz", "Prisma Cloud / Twistlock", "Aqua Security",
    "CrowdStrike Falcon", "Trivy", "Grype", "Snyk", "Anchore", "OpenSCAP", "In-house / custom tooling",
    "Other", UNKNOWN,
)
VA_FREQUENCIES = (
    "Continuously (always-on)", "Weekly", "Monthly", "Quarterly", "Annually", "Ad-hoc / on demand", UNKNOWN,
)
LLM_PROVIDERS = (
    "Claude", "ChatGPT", "Gemini", "Copilot", "Amazon Bedrock", "Mistral", "Ollama", "Other", "None", UNKNOWN,
)

# ─────────────── SPEC TYPES ─────────────── #


@dataclass(frozen=True)
class Cond:
    """A follow-up applies only when every one of its conditions holds.

    ``key`` names a sibling field in the same container (the section dict, or
    the nested ``parent`` dict). ``keys`` is for ``sum_gt`` only.
    Ops: ``is_true`` / ``is_false`` (confirm answers — "Unknown" matches
    neither), ``eq``, ``startswith``, ``contains`` / ``lacks_all`` (multi),
    ``gt`` (numeric > value; Unknown/missing never matches), ``sum_gt``.
    """
    key: str = ""
    op: str = "is_true"
    value: Any = None
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldSpec:
    key: str
    kind: Kind
    prompt: str                                  # terminal wording
    options: tuple[str, ...] = ()
    parent: str | None = None                    # nested dict key ("deployment", "server_specs")
    when: tuple[Cond, ...] = ()
    input_id: str = ""                           # DOM id shared by the HTML form and the WebUI page
    default: Any = None                          # last-resort terminal default
    optional_unknown: bool = True                # int/confirm: may be answered "Unknown"
    optional: bool = False                       # may be left blank; never counted toward completeness
    multiline: bool = False                      # text: render as a textarea (HTML form / WebUI)


@dataclass(frozen=True)
class SectionSpec:
    name: str
    label: str
    blurb: str
    fields: tuple[FieldSpec, ...]
    gate: str | None = None                      # "present" / "deployed_on_kubernetes": explicit yes/no first
    gate_prompt: str = ""
    tiers_note: str = ""                         # documentation only

    def field(self, key: str, parent: str | None = None) -> FieldSpec | None:
        for f in self.fields:
            if f.key == key and f.parent == parent:
                return f
        return None


# ─────────────── SECTION DEFINITIONS ─────────────── #

def _f(key: str, kind: Kind, prompt: str, input_id: str = "", **kw: Any) -> FieldSpec:
    return FieldSpec(key=key, kind=kind, prompt=prompt, input_id=input_id, **kw)


def _other(parent_key: str, prompt: str, input_id: str, parent: str | None = None) -> FieldSpec:
    return _f(f"{parent_key}_other", "text", prompt, input_id, parent=parent,
              when=(Cond(parent_key, "eq", "Other"),))


def _deployment(label: str, p: str) -> tuple[FieldSpec, ...]:
    return (
        _f("deployment_type", "select", f"{label} — Deployment type:", f"f-{p}-deployment-type",
           options=DEPLOYMENT_TYPES, parent="deployment"),
        _other("deployment_type", "  Specify deployment type:", f"f-{p}-deployment-type-other", parent="deployment"),
    )


def _server_specs(label: str, p: str) -> tuple[FieldSpec, ...]:
    return (
        _f("os_type", "select", f"{label} — Operating System:", f"f-{p}-os-type",
           options=OS_TYPES, parent="server_specs"),
        _other("os_type", "  Specify operating system:", f"f-{p}-os-type-other", parent="server_specs"),
        _f("cpu_cores", "select", f"{label} — CPU cores per server:", f"f-{p}-cpu-cores",
           options=CPU_CORES, parent="server_specs"),
        _f("memory_gb", "select", f"{label} — Memory (GB) per server:", f"f-{p}-memory-gb",
           options=MEMORY_GB, parent="server_specs"),
        _f("disk_space_gb", "select", f"{label} — Disk space (GB) per server:", f"f-{p}-disk-space-gb",
           options=DISK_GB, parent="server_specs"),
    )


_MONITORING_ACTIVE = (Cond("monitoring_tools", "lacks_all", ("None", UNKNOWN)),)

SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        name="environment", label="Environment Overview",
        blurb="General information about this deployment. These answers apply to the entire environment.",
        fields=(
            _f("environment_type", "select", "Environment type:", "f-environment-type", options=ENVIRONMENT_TYPES),
            _f("datacenter_location", "text",
               "Datacenter location (e.g., us-east-1, London-DC2, Building 4 Lab):", "f-datacenter-location"),
            _f("hosting_provider", "select", "Hosting provider:", "f-hosting-provider", options=HOSTING_PROVIDERS),
            _other("hosting_provider", "  Specify hosting provider:", "f-hosting-provider-other"),
            _f("automation_scope", "text",
               "Network devices or infrastructure you automate with Itential (optional — vendors, device types, "
               "use cases):", "f-automation-scope", optional=True, multiline=True),
        ),
    ),
    SectionSpec(
        name="platform", label="Platform Architecture",
        blurb="Instance count, deployment type, and server specs for Itential Platform.",
        fields=(
            _f("active_instance_count", "int", "Number of active Platform instances:", "f-active-instance-count"),
            _f("standby_instance_count", "int", "Number of standby Platform instances:", "f-standby-instance-count",
               default=0),
            _f("all_in_same_datacenter", "confirm", "Are all Platform instances in the same datacenter?",
               "f-all-in-same-datacenter",
               when=(Cond(op="sum_gt", keys=("active_instance_count", "standby_instance_count"), value=1),)),
            _f("datacenter_details", "text",
               "  Describe the distribution (e.g., 2 active in us-east-1, 1 standby in us-west-2):",
               "f-datacenter-details", when=(Cond("all_in_same_datacenter", "is_false"),)),
            *_deployment("Platform", "platform"),
            *_server_specs("Platform", "platform"),
        ),
    ),
    SectionSpec(
        name="gateway4", label="Gateway4 Architecture",
        blurb="Instance count, device info, and migration plans for Automation Gateway 4.",
        gate="present", gate_prompt="Does this environment have Gateway4?",
        fields=(
            _f("instance_count", "int", "Number of Gateway4 servers:", "f-gw4-instance-count", default=1),
            _f("same_datacenter_as_platform", "confirm", "Are Gateway4 servers in the same datacenter as Platform?",
               "f-gw4-same-dc"),
            *_deployment("Gateway4", "gw4"),
            *_server_specs("Gateway4", "gw4"),
            _f("device_count", "select", "Approximate number of network devices managed:", "f-gw4-device-count",
               options=GW4_DEVICE_COUNTS),
            _f("plans_to_migrate_to_gw5", "confirm", "Are there plans to migrate from Gateway4 to Gateway5?",
               "f-gw4-plans-migrate"),
            _f("migration_timeline", "select", "  Expected migration timeline:", "f-gw4-migration-timeline",
               options=GW4_MIGRATION_TIMELINES, when=(Cond("plans_to_migrate_to_gw5", "is_true"),)),
        ),
    ),
    SectionSpec(
        name="gateway5", label="Gateway5 Architecture",
        blurb="Cluster configuration, HA, and server specs for Automation Gateway 5.",
        gate="present", gate_prompt="Does this environment have Gateway5?",
        fields=(
            _f("same_datacenter_as_platform", "confirm", "Are Gateway5 servers in the same datacenter as Platform?",
               "f-gw5-same-dc"),
            _f("cluster_count", "int", "Number of Gateway5 clusters:", "f-gw5-cluster-count", default=1),
            _f("clusters", "clusters", "Cluster servers and runners:", "",
               when=(Cond("cluster_count", "gt", 0),)),
            _f("ha_enabled", "confirm", "Is HA enabled for Gateway5?", "f-gw5-ha-enabled"),
            _f("ha_mode", "select", "  HA mode:", "f-gw5-ha-mode", options=GW5_HA_MODES,
               when=(Cond("ha_enabled", "is_true"),)),
            _f("has_redundant_instances", "confirm", "Are there redundant Gateway5 instances for failover?",
               "f-gw5-redundant"),
            *_deployment("Gateway5", "gw5"),
            *_server_specs("Gateway5", "gw5"),
        ),
    ),
    SectionSpec(
        name="mongodb", label="MongoDB Architecture",
        blurb="Deployment topology and server specs.",
        fields=(
            _f("deployment_type", "select", "MongoDB deployment topology:", "f-mongo-deployment-type",
               options=MONGO_DEPLOYMENTS),
            _f("mongo_node_count", "int", "Number of MongoDB servers (total, including the primary):",
               "f-mongo-node-count", default=1),
            _f("same_datacenter_as_platform", "confirm", "Is MongoDB in the same datacenter as Platform?",
               "f-mongo-same-dc"),
            _f("replicas_across_datacenters", "confirm",
               "Are replica members distributed across multiple datacenters?", "f-mongo-across-dc",
               when=(Cond("mongo_node_count", "gt", 1),)),
            _f("datacenter_distribution", "text",
               "  Describe distribution (e.g., 2 in us-east-1, 1 arbiter in us-west-2):", "f-mongo-dc-distribution",
               when=(Cond("replicas_across_datacenters", "is_true"),)),
            *_server_specs("MongoDB", "mongo"),
        ),
    ),
    SectionSpec(
        name="redis", label="Redis Architecture",
        blurb="Deployment topology and server specs.",
        fields=(
            _f("deployment_type", "select", "Redis deployment topology:", "f-redis-deployment-type",
               options=REDIS_DEPLOYMENTS),
            _f("redis_node_count", "int", "Number of Redis server nodes:", "f-redis-node-count", default=3),
            _f("sentinel_count", "int", "Number of Sentinel instances:", "f-redis-sentinel-count", default=3,
               when=(Cond("deployment_type", "startswith", "Sentinel"),)),
            _f("same_datacenter_as_platform", "confirm", "Are Redis nodes in the same datacenter as Platform?",
               "f-redis-same-dc"),
            _f("nodes_across_datacenters", "confirm", "Are Redis nodes distributed across multiple datacenters?",
               "f-redis-across-dc", when=(Cond("redis_node_count", "gt", 1),)),
            *_server_specs("Redis", "redis"),
        ),
    ),
    SectionSpec(
        name="load_balancer", label="Load Balancer",
        blurb="Type, routing policy, health checks, and session configuration.",
        gate="present", gate_prompt="Is a load balancer deployed in front of Platform?",
        fields=(
            _f("lb_type", "select", "Load balancer type:", "f-lb-type", options=LB_TYPES),
            _other("lb_type", "  Specify (e.g., Traefik, Envoy, Citrix):", "f-lb-type-other"),
            _f("routing_policy", "select", "Routing policy:", "f-lb-routing-policy", options=LB_ROUTING_POLICIES),
            _f("session_stickiness", "confirm", "Is session stickiness (sticky sessions) enabled?",
               "f-lb-stickiness"),
            _f("platform_health_endpoint", "text", "Platform health check endpoint (e.g., /health/status):",
               "f-lb-health-endpoint", default="/health/status?exclude-services=true"),
            _f("health_check_interval", "select", "Health check interval:", "f-lb-health-interval",
               options=LB_HEALTH_INTERVALS),
        ),
    ),
    SectionSpec(
        name="kubernetes", label="Kubernetes Configuration",
        blurb="Kubernetes-specific settings: deployment method, probe configuration, and resource allocation.",
        gate="deployed_on_kubernetes", gate_prompt="Is this environment deployed on Kubernetes?",
        fields=(
            _f("k8s_distribution", "select", "Kubernetes distribution:", "f-k8s-distribution",
               options=K8S_DISTRIBUTIONS),
            _f("node_instance_type", "text", "Worker node instance type (e.g., m5.2xlarge, c6i.4xlarge):",
               "f-k8s-node-instance-type"),
            _f("node_count", "int", "Number of worker nodes in the cluster:", "f-k8s-node-count", default=3),
            _f("deployment_method", "select", "How are Itential components deployed to Kubernetes?",
               "f-k8s-deployment-method", options=K8S_DEPLOYMENT_METHODS),
            _f("hpa_enabled", "confirm", "Is Horizontal Pod Autoscaler (HPA) configured for Itential pods?",
               "f-k8s-hpa-enabled"),
            _f("hpa_min_replicas", "int", "  HPA minimum replica count:", "f-k8s-hpa-min-replicas", default=2,
               when=(Cond("hpa_enabled", "is_true"),)),
            _f("hpa_max_replicas", "int", "  HPA maximum replica count:", "f-k8s-hpa-max-replicas", default=5,
               when=(Cond("hpa_enabled", "is_true"),)),
            _f("hpa_scaling_metric", "select", "  Scaling metric:", "f-k8s-hpa-scaling-metric",
               options=K8S_HPA_METRICS, when=(Cond("hpa_enabled", "is_true"),)),
            _f("has_custom_resources", "confirm",
               "Have you customized CPU/memory limits or Helm values for Itential pods?", "f-k8s-custom-resources"),
            _f("values_source", "select", "Where are your Helm values / resource configs stored?",
               "f-k8s-values-source", options=K8S_VALUES_SOURCES, when=(Cond("has_custom_resources", "is_true"),)),
            _f("resource_notes", "text",
               "  Any notable resource overrides? (e.g., Platform pods set to 8Gi memory, 4 CPU):",
               "f-k8s-resource-notes", when=(Cond("has_custom_resources", "is_true"),)),
            _f("pod_restarts_observed", "confirm", "Have pod restarts been observed?", "f-k8s-pod-restarts"),
            _f("pod_restart_notes", "text",
               "  Approximate frequency or restart count (e.g., 3 restarts in the last 24h):",
               "f-k8s-pod-restart-notes", when=(Cond("pod_restarts_observed", "is_true"),)),
            _f("probes_customized", "confirm",
               "Have liveness/readiness/startup probes been customized from Itential defaults?",
               "f-k8s-probes-customized"),
            _f("probe_notes", "text", "  Describe the changes (e.g., startup probe timeout increased to 120s):",
               "f-k8s-probe-notes", when=(Cond("probes_customized", "is_true"),)),
        ),
    ),
    SectionSpec(
        name="monitoring", label="Monitoring & Health Checks",
        blurb="Monitoring tools and observability mechanisms for the Itential environment.",
        fields=(
            _f("monitoring_tools", "multi", "Which monitoring tools are in use? (select all that apply)",
               "g-monitoring-tools", options=MONITORING_TOOLS),
            _f("monitoring_tools_other", "text", "  Specify other monitoring tools:", "f-monitoring-tools-other",
               when=(Cond("monitoring_tools", "contains", "Other"),)),
            _f("monitored_components", "multi",
               "Which Itential components are actively monitored? (select all that apply)",
               "g-monitored-components", options=MONITORED_COMPONENTS, when=_MONITORING_ACTIVE),
            _f("has_alerting", "confirm", "Are automated alerts configured for Itential component failures?",
               "f-mon-has-alerting", when=_MONITORING_ACTIVE),
            _f("alert_channels", "multi", "  Alert delivery channels (select all that apply):", "g-alert-channels",
               options=ALERT_CHANNELS, when=_MONITORING_ACTIVE + (Cond("has_alerting", "is_true"),)),
            _f("monitors_health_endpoint", "confirm",
               "Is the Platform /health/status endpoint being polled by an external monitor?",
               "f-mon-health-endpoint", when=_MONITORING_ACTIVE),
            _f("health_poll_interval", "select", "  Polling interval:", "f-mon-health-poll-interval",
               options=HEALTH_POLL_INTERVALS, when=_MONITORING_ACTIVE + (Cond("monitors_health_endpoint", "is_true"),)),
            _f("has_log_aggregation", "confirm",
               "Are Itential Platform logs being shipped to a central log aggregator?", "f-mon-log-agg",
               when=_MONITORING_ACTIVE),
            _f("log_aggregator", "select", "  Log aggregation tool:", "f-mon-log-aggregator",
               options=LOG_AGGREGATORS, when=_MONITORING_ACTIVE + (Cond("has_log_aggregation", "is_true"),)),
            _f("log_aggregator_other", "text", "  Specify log aggregation tool:", "f-mon-log-agg-other",
               when=_MONITORING_ACTIVE + (Cond("log_aggregator", "eq", "Other"),)),
        ),
    ),
    SectionSpec(
        name="network_security", label="Network & Security",
        blurb="Network configuration and security compliance standards.",
        fields=(
            _f("mtu_size", "select", "MTU size across platform network:", "f-net-mtu-size", options=MTU_SIZES),
            _f("mtu_size_other", "text", "  Specify MTU size (bytes, e.g., 1492):", "f-net-mtu-size-other",
               when=(Cond("mtu_size", "eq", "Other"),)),
            _f("has_connectivity_concerns", "confirm",
               "Any known network concerns between components? (latency, firewalls, VPNs)", "f-net-connectivity"),
            _f("connectivity_notes", "text",
               "  Describe (e.g., 15ms latency between Platform and MongoDB across VPN):",
               "f-net-connectivity-notes", when=(Cond("has_connectivity_concerns", "is_true"),)),
            _f("selinux_mode", "select", "SELinux mode:", "f-sec-selinux-mode", options=SELINUX_MODES),
            _f("compliance_standards", "multi", "Security compliance standards enabled (select all that apply):",
               "g-compliance-standards", options=COMPLIANCE_STANDARDS),
            _f("compliance_other", "text", "  Specify (e.g., FedRAMP, SOC 2, PCI-DSS):", "f-sec-compliance-other",
               when=(Cond("compliance_standards", "contains", "Other"),)),
        ),
    ),
    SectionSpec(
        name="vulnerability_assessments", label="Vulnerability Assessments",
        blurb="How (and whether) this environment is scanned for known vulnerabilities.",
        fields=(
            _f("performs_assessments", "select", "Does this environment undergo vulnerability assessments?",
               "f-va-performs", options=VA_PERFORMS),
            _f("tools", "multi", "Which tool(s) are used? (select all that apply)", "g-va-tools",
               options=VA_TOOLS, when=(Cond("performs_assessments", "startswith", "Yes"),)),
            _f("tools_other", "text", "  Specify (e.g., Acunetix, Black Duck, Tanium Comply):", "f-va-tools-other",
               when=(Cond("performs_assessments", "startswith", "Yes"), Cond("tools", "contains", "Other"))),
            _f("frequency", "select", "How often are assessments run?", "f-va-frequency",
               options=VA_FREQUENCIES, when=(Cond("performs_assessments", "startswith", "Yes"),)),
        ),
    ),
    SectionSpec(
        name="artificial_intelligence", label="Artificial Intelligence",
        blurb="Which LLM (if any) your organization uses alongside Itential.",
        fields=(
            _f("llm_provider", "select", "Which LLM does your organization use with Itential?",
               "f-ai-llm-provider", options=LLM_PROVIDERS),
            _other("llm_provider", "  Specify LLM/provider:", "f-ai-llm-provider-other"),
        ),
    ),
)

SECTION_BY_NAME: dict[str, SectionSpec] = {s.name: s for s in SECTIONS}
SECTION_NAMES: tuple[str, ...] = tuple(s.name for s in SECTIONS)

# Sections that are meaningless in a SaaS (gateway-anchored) audit. The gateway
# the audit is NOT about is added by ``active_sections``.
_SAAS_DROPPED = ("platform", "mongodb", "redis")


def active_sections(*, is_saas: bool = False, saas_gateway_kind: str = "") -> tuple[SectionSpec, ...]:
    """Sections that apply to the current tier.

    Standard never asks the architecture questions at all; Extended asks every
    section; SaaS drops Platform/MongoDB/Redis and the gateway it isn't about.
    ``saas_gateway_kind`` is ``gateway4`` / ``gateway5`` / ``gw4-gw5`` / ``""``.
    """
    if not is_saas:
        return SECTIONS
    kind = (saas_gateway_kind or "").strip().lower()
    drop = set(_SAAS_DROPPED)
    if kind == "gateway4":
        drop.add("gateway5")
    elif kind == "gateway5":
        drop.add("gateway4")
    return tuple(s for s in SECTIONS if s.name not in drop)


# ─────────────── NORMALIZATION (legacy shapes → current) ─────────────── #

_LEGACY_VA = {
    "Yes—regularly": "Yes — regularly",
    "Yes—ad-hoc / on demand": "Yes — ad-hoc / on demand",
}


# Option strings older forms saved that no longer match a current option exactly.
# (section, field key) → {old value: current value}. Applied in memory only.
LEGACY_OPTION_ALIASES: dict[tuple[str, str], dict[str, str]] = {
    ("network_security", "mtu_size"): {"1500 (Standard—recommended)": "1500 (Standard — recommended)"},
    ("redis", "deployment_type"): {"Sentinel": "Sentinel (recommended for HA)", "Standalone": "Single Instance"},
    ("vulnerability_assessments", "frequency"): {
        "Ad hoc / On demand": "Ad-hoc / on demand",
        "Continuous (daily / on commit)": "Continuously (always-on)",
    },
}


def normalize_section(name: str, data: Any) -> dict[str, Any]:
    """Return a copy of ``data`` with older saved shapes mapped onto today's.

    In-memory only — never rewrites the stored file. Handles: MongoDB
    ``replica_count`` (pre-3.0, additional members) → ``mongo_node_count``;
    legacy unspaced em-dash VA answers; boolean VA answers from the old WebUI
    toggle; the WebUI's ``app_servers`` / ``script_runners`` cluster keys.
    """
    if not isinstance(data, dict):
        return {}
    out = copy.deepcopy(data)
    if name == "mongodb" and "mongo_node_count" not in out and out.get("replica_count") is not None:
        try:
            out["mongo_node_count"] = int(out["replica_count"]) + 1
        except (TypeError, ValueError):
            pass
        out.setdefault(
            "deployment_type",
            "Replica Set (recommended for HA)" if _as_int(out.get("mongo_node_count")) not in (None, 1) else "Standalone",
        )
    if name == "vulnerability_assessments":
        pa = out.get("performs_assessments")
        if pa in _LEGACY_VA:
            out["performs_assessments"] = _LEGACY_VA[pa]
        elif pa is True:
            out["performs_assessments"] = "Yes — regularly"
        elif pa is False:
            out["performs_assessments"] = "No"
    for (sec, key), aliases in LEGACY_OPTION_ALIASES.items():
        if sec == name and isinstance(out.get(key), str) and out[key] in aliases:
            out[key] = aliases[out[key]]
    if name == "gateway5" and isinstance(out.get("clusters"), list):
        fixed = []
        for c in out["clusters"]:
            if isinstance(c, dict):
                c = dict(c)
                if "server_count" not in c and "app_servers" in c:
                    c["server_count"] = c.pop("app_servers")
                if "runner_count" not in c and "script_runners" in c:
                    c["runner_count"] = c.pop("script_runners")
            fixed.append(c)
        out["clusters"] = fixed
    return out


# ─────────────── EVALUATION ─────────────── #

def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def unknown_keys(container: Any) -> set[str]:
    """Field keys answered "Unknown" on ``container`` (an int/confirm out)."""
    if not isinstance(container, dict):
        return set()
    raw = container.get(UNKNOWN_KEY)
    return {k for k in raw if isinstance(k, str)} if isinstance(raw, list) else set()


def _cond_met(cond: Cond, container: dict[str, Any]) -> bool:
    val = container.get(cond.key) if cond.key else None
    if cond.op == "is_true":
        return val is True
    if cond.op == "is_false":
        return val is False
    if cond.op == "eq":
        return val == cond.value
    if cond.op == "startswith":
        return isinstance(val, str) and val.startswith(cond.value)
    if cond.op == "contains":
        return isinstance(val, list) and cond.value in val
    if cond.op == "lacks_all":
        # Not yet answered ⇒ the follow-ups aren't "applicable yet", but the
        # parent itself is unanswered so the section is incomplete anyway.
        return isinstance(val, list) and bool(val) and not any(v in val for v in cond.value)
    if cond.op == "gt":
        n = _as_int(val)
        return n is not None and n > cond.value
    if cond.op == "sum_gt":
        parts = [_as_int(container.get(k)) for k in cond.keys]
        known = [p for p in parts if p is not None]
        return bool(known) and sum(known) > cond.value
    return False


def _applies(f: FieldSpec, container: dict[str, Any]) -> bool:
    return all(_cond_met(c, container) for c in f.when)


def _container(data: dict[str, Any], f: FieldSpec) -> dict[str, Any]:
    if f.parent is None:
        return data
    nested = data.get(f.parent)
    return nested if isinstance(nested, dict) else {}


def _is_answered(f: FieldSpec, container: dict[str, Any]) -> bool:
    if f.kind == "multi":
        val = container.get(f.key)
        return isinstance(val, list) and len(val) > 0
    if f.kind == "text" or f.kind == "select":
        val = container.get(f.key)
        return isinstance(val, str) and bool(val.strip())
    if f.kind == "int":
        if f.key in unknown_keys(container) and f.optional_unknown:
            return True
        val = container.get(f.key)
        if isinstance(val, str) and val.strip().lower() == UNKNOWN.lower():
            return True
        return _as_int(val) is not None
    if f.kind == "confirm":
        if f.key in unknown_keys(container) and f.optional_unknown:
            return True
        return isinstance(container.get(f.key), bool)
    if f.kind == "clusters":
        return False  # evaluated by _clusters_answered (needs the sibling count)
    return False


def _clusters_answered(container: dict[str, Any]) -> bool:
    """Every declared cluster states its server and runner counts (or Unknown)."""
    if "cluster_count" in unknown_keys(container):
        return True
    n = _as_int(container.get("cluster_count"))
    if n is None or n <= 0:
        return True
    clusters = container.get("clusters")
    if not isinstance(clusters, list) or len(clusters) < n:
        return False
    for c in clusters[:n]:
        if not isinstance(c, dict):
            return False
        unk = unknown_keys(c)
        for key in ("server_count", "runner_count"):
            if key not in unk and _as_int(c.get(key)) is None:
                return False
    return True


def missing_fields(name: str, data: Any) -> list[FieldSpec]:
    """Applicable fields of section ``name`` that hold no answer yet.

    Does not look at the section gate — see :func:`section_state`.
    """
    spec = SECTION_BY_NAME[name]
    d = normalize_section(name, data)
    out: list[FieldSpec] = []
    for f in spec.fields:
        container = _container(d, f)
        if f.parent is not None and not isinstance(d.get(f.parent), dict):
            container = {}
        if f.optional or not _applies(f, container):
            continue
        if f.kind == "clusters":
            if not _clusters_answered(d):
                out.append(f)
            continue
        if not _is_answered(f, container):
            out.append(f)
    return out


def applicable_fields(name: str, data: Any) -> list[FieldSpec]:
    """Fields that currently apply (conditions met), answered or not."""
    spec = SECTION_BY_NAME[name]
    d = normalize_section(name, data)
    return [f for f in spec.fields if _applies(f, _container(d, f))]


SectionState = Literal["complete", "partial", "empty"]


def section_state(name: str, data: Any, *, skipped: bool = False) -> SectionState:
    """``complete`` / ``partial`` / ``empty`` for one section.

    ``skipped`` is the legacy ``skipped`` list membership: for a gated section
    it means "Not present" (answered). A gate that is missing is unanswered
    even if the section dict is otherwise populated.
    """
    spec = SECTION_BY_NAME[name]
    d = normalize_section(name, data)

    if spec.gate:
        gate_val = d.get(spec.gate)
        if gate_val is False or (skipped and gate_val is None):
            return "complete"
        if gate_val is not True:
            # Gate unanswered. Anything else in the dict is a ghost (e.g. the
            # old auto-saved {"present": true, <prefilled default>} shape).
            return "empty"
    missing = missing_fields(name, d)
    if not missing:
        return "complete"
    answered_any = any(
        k for k in d if k not in (spec.gate, UNKNOWN_KEY) and d.get(k) not in (None, "", [], {})
    ) or bool(unknown_keys(d))
    return "partial" if answered_any or spec.gate else "empty"


def section_progress(name: str, data: Any, *, skipped: bool = False) -> tuple[int, int]:
    """``(answered, applicable)`` field counts — gate counted as one field."""
    spec = SECTION_BY_NAME[name]
    d = normalize_section(name, data)
    total = answered = 0
    if spec.gate:
        total += 1
        gate_val = d.get(spec.gate)
        if gate_val is False or (skipped and gate_val is None):
            return 1, 1
        if gate_val is True:
            answered += 1
        else:
            return 0, 1
    for f in applicable_fields(name, d):
        if f.optional:
            continue
        total += 1
        if f.kind == "clusters":
            answered += 1 if _clusters_answered(d) else 0
        elif _is_answered(f, _container(d, f)):
            answered += 1
    return answered, total


@dataclass
class ArchitectureStatus:
    """Whole-environment roll-up used by the CLI prompt, the report and the WebUI."""
    state: SectionState = "empty"                       # complete only when every active section is
    done: int = 0                                       # complete sections
    total: int = 0                                      # active sections
    sections: dict[str, SectionState] = field(default_factory=dict)
    missing: dict[str, list[str]] = field(default_factory=dict)   # section → unanswered field keys
    answered_fields: int = 0
    total_fields: int = 0

    @property
    def is_complete(self) -> bool:
        return self.state == "complete"

    @property
    def incomplete_sections(self) -> list[str]:
        return [n for n, s in self.sections.items() if s != "complete"]


def evaluate(
    completed: Any,
    skipped: Any = None,
    *,
    is_saas: bool = False,
    saas_gateway_kind: str = "",
) -> ArchitectureStatus:
    """Roll every active section up into one status. Pure; tolerant of junk input."""
    completed = completed if isinstance(completed, dict) else {}
    skipped_set = set(skipped) if isinstance(skipped, (list, tuple, set)) else set()
    status = ArchitectureStatus()
    for spec in active_sections(is_saas=is_saas, saas_gateway_kind=saas_gateway_kind):
        data = completed.get(spec.name)
        was_skipped = spec.name in skipped_set and spec.gate is not None
        st = section_state(spec.name, data, skipped=was_skipped)
        status.sections[spec.name] = st
        status.total += 1
        a, t = section_progress(spec.name, data, skipped=was_skipped)
        status.answered_fields += a
        status.total_fields += t
        if st == "complete":
            status.done += 1
        else:
            gate_missing = [spec.gate] if spec.gate and normalize_section(spec.name, data).get(spec.gate) is None else []
            if gate_missing:
                status.missing[spec.name] = gate_missing
            else:
                status.missing[spec.name] = [f.key for f in missing_fields(spec.name, data)]
    if status.total and status.done >= status.total:
        status.state = "complete"
    elif any(s != "empty" for s in status.sections.values()):
        status.state = "partial"
    else:
        status.state = "empty"
    return status


def field_label(section: str, key: str) -> str:
    """Human wording for a field key (the terminal prompt, de-punctuated)."""
    spec = SECTION_BY_NAME.get(section)
    if spec is not None:
        if key == spec.gate:
            return spec.gate_prompt.rstrip("?")
        for f in spec.fields:
            if f.key == key:
                return f.prompt.strip().rstrip(":?").strip()
    return key.replace("_", " ")


def display_unknowns(data: Any) -> Any:
    """Report/display copy: replace each ``unknown``-listed absent field with the
    literal string ``"Unknown"`` and drop the bookkeeping list. Recurses into
    nested dicts and cluster lists. Never used for the JSON export, whose
    schema keeps int/boolean types."""
    if isinstance(data, list):
        return [display_unknowns(i) for i in data]
    if not isinstance(data, dict):
        return data
    out = {k: display_unknowns(v) for k, v in data.items() if k != UNKNOWN_KEY}
    for key in unknown_keys(data):
        out.setdefault(key, UNKNOWN)
    return out


# Public names for callers outside this module (the terminal collectors).
applies = _applies
as_int = _as_int
