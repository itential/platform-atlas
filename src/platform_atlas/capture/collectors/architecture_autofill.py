"""
ATLAS // Architecture Form Autofill

Best-effort, read-only SSH probing that pre-fills parts of the Architecture &
Observability form so users don't have to type values Atlas can determine on
its own. Extended tier only (gated by ``require_extended``) — optional and
user-driven, never run as part of a normal capture.

Results are staged in ``architecture_store``'s ``autofill`` bucket — kept
separate from ``completed`` (the user's confirmed answers) so a probe never
counts as "this section is done" and never overwrites anything the user has
already entered. Anything a probe can't determine (missing binary, no
permission, timeout, ambiguous signal) is simply omitted from the result —
never guessed, never surfaced as an error. See
``design/Atlas-2.1/ARCHITECTURE_AUTOFILL_RESEARCH.md`` for the command-by-
command risk assessment this module implements.

Per-node commands only ever touch: OS release info, CPU/memory/disk totals,
container/VM/Kubernetes signals, SELinux mode, the FIPS crypto flag, one
network interface's MTU, the running-service list, and (RHEL-family only)
the installed-package list. MongoDB replica topology and Redis deployment
topology are read from the most recent completed capture for the
environment instead of new SSH calls — that data already exists there.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from platform_atlas.core.context import require_extended
from platform_atlas.core.topology import role_display_label
from platform_atlas.core.transport import transport_from_config

logger = logging.getLogger(__name__)

# Called once per probe step so the CLI can drive a live progress bar:
# ``on_progress(completed_steps, total_steps, current_activity_label)``.
# Purely advisory — ``probe_environment`` works identically when it is None.
ProgressCallback = Callable[[int, int, str], None]

CPU_BUCKETS = ["2", "4", "8", "16", "32", "64+"]
MEMORY_BUCKETS = ["4", "8", "16", "32", "64", "128+"]
DISK_BUCKETS = ["20", "50", "100", "200", "500", "1000+"]

# Monitoring / log-aggregation / VA-scanner keyword -> form option label.
# Matched (case-insensitively) against `systemctl list-units` service names
# and (VA tools only) `rpm -qa` package names.
_MONITORING_TOOL_PATTERNS: dict[str, str] = {
    "prometheus": "Prometheus",
    "grafana": "Grafana",
    "datadog": "Datadog",
    "splunk": "Splunk",
    "newrelic": "New Relic",
    "dynatrace": "Dynatrace",
    "zabbix": "Zabbix",
    "nagios": "Nagios / Icinga",
    "icinga": "Nagios / Icinga",
    "cloudwatch": "AWS CloudWatch",
    "azuremonitor": "Azure Monitor",
    "collectd": "Prometheus",  # node_exporter/collectd commonly feed Prometheus
}
_LOG_AGGREGATOR_PATTERNS: dict[str, str] = {
    "fluentd": "Elastic / ELK",
    "fluent-bit": "Elastic / ELK",
    "logstash": "Elastic / ELK",
    "filebeat": "Elastic / ELK",
    "splunkforwarder": "Splunk",
    "loki": "Grafana Loki",
    "cloudwatch-agent": "AWS CloudWatch Logs",
    "azure-mdsd": "Azure Log Analytics",
}
_VA_TOOL_PATTERNS: dict[str, str] = {
    "nessus": "Tenable / Nessus",
    "qualys": "Qualys VMDR",
    "rapid7": "Rapid7 InsightVM",
    "insight": "Rapid7 InsightVM",
    "wiz": "Wiz",
    "twistlock": "Prisma Cloud / Twistlock",
    "prisma": "Prisma Cloud / Twistlock",
    "aqua": "Aqua Security",
    "falcon": "CrowdStrike Falcon",
    "crowdstrike": "CrowdStrike Falcon",
}


# ── raw fact parsing (pure functions — no SSH, unit-testable) ──────────────

def _parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def _round_up_bucket(value: float, buckets: list[str]) -> str:
    for bucket in buckets[:-1]:
        if value <= int(bucket):
            return bucket
    return buckets[-1]


def _map_os_type(os_release_text: str | None) -> str:
    if not os_release_text:
        return ""
    fields = _parse_os_release(os_release_text)
    os_id = (fields.get("ID") or "").lower()
    version_id = fields.get("VERSION_ID") or ""
    major = version_id.split(".")[0] if version_id else ""
    if os_id == "alpine":
        return "Alpine"
    if os_id == "amzn" and major == "2":
        return "Amazon Linux 2"
    if os_id in ("rhel", "rocky") and major in ("8", "9"):
        return f"{'RHEL' if os_id == 'rhel' else 'Rocky'} {major}"
    return "Other" if os_id else ""


def _map_cpu_cores(nproc_output: str | None) -> str:
    if nproc_output is None:
        return ""
    try:
        return _round_up_bucket(int(nproc_output.strip()), CPU_BUCKETS)
    except ValueError:
        return ""


def _map_memory_gb(meminfo_output: str | None) -> str:
    match = re.search(r"(\d+)", meminfo_output or "")
    if not match:
        return ""
    gib = int(match.group(1)) / (1024 * 1024)
    return _round_up_bucket(gib, MEMORY_BUCKETS)


def _map_disk_gb(df_output: str | None) -> str:
    lines = [ln.strip() for ln in (df_output or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return ""
    match = re.search(r"(\d+)", lines[-1])
    if not match:
        return ""
    return _round_up_bucket(int(match.group(1)), DISK_BUCKETS)


def _map_deployment_type(cgroup_text: str | None, virt_output: str | None, k8s_serviceaccount: bool) -> str:
    cgroup_lower = (cgroup_text or "").lower()
    virt = (virt_output or "").strip().lower()
    if k8s_serviceaccount or "kubepods" in cgroup_lower:
        # We know it's Kubernetes but not which distribution — a wrong guess
        # here is worse than leaving it for the user to pick.
        return ""
    if "docker" in cgroup_lower or "containerd" in cgroup_lower:
        return "Docker Containers"
    if virt and virt != "none":
        return "Virtual Machines (VMs)"
    if virt == "none":
        return "Bare Metal"
    return ""


def _map_selinux_mode(getenforce_output: str | None, getenforce_ran: bool, os_release_text: str | None) -> str:
    value = (getenforce_output or "").strip()
    if value in ("Enforcing", "Permissive", "Disabled"):
        return value
    if not getenforce_ran:
        os_id = (_parse_os_release(os_release_text or "").get("ID") or "").lower()
        if os_id and os_id not in ("rhel", "rocky", "centos", "fedora", "amzn"):
            return "N/A (Containers / Non-Linux)"
    return ""


def _map_fips(fips_flag: str | None, os_release_text: str | None) -> list[str]:
    if (fips_flag or "").strip() != "1":
        return []
    major = (_parse_os_release(os_release_text or "").get("VERSION_ID") or "").split(".")[0]
    try:
        return ["FIPS 140-3"] if major and int(major) >= 9 else ["FIPS 140-2"]
    except ValueError:
        return ["FIPS 140-2"]


def _map_service_matches(services_text: str | None, patterns: dict[str, str]) -> list[str]:
    text_lower = (services_text or "").lower()
    matched: list[str] = []
    for keyword, label in patterns.items():
        if keyword in text_lower and label not in matched:
            matched.append(label)
    return matched


def _map_hosting_provider(cloud_init_json: str | None) -> str:
    if not cloud_init_json:
        return ""
    try:
        data = json.loads(cloud_init_json)
    except (ValueError, TypeError):
        return ""
    cloud_name = str((data.get("v1") or {}).get("cloud_name", "")).lower()
    return {"aws": "AWS", "azure": "Azure", "gce": "GCP"}.get(cloud_name, "")


# ── SSH probing ─────────────────────────────────────────────────────────────

@dataclass
class NodeProbeResult:
    """Everything gathered (and not gathered) from one topology node."""
    node_name: str
    role: str
    display_role: str
    facts: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # honest "couldn't detect X" lines


# Environment-wide phases run on exactly one node (the Platform/IAP node, or
# the first reachable one) — these are the fields that describe the deployment
# as a whole rather than a single host.
_ENV_WIDE_PHASES: tuple[str, ...] = (
    "Security policy",
    "Network / MTU",
    "Running services",
    "Installed packages",
    "Cloud metadata",
)


def _phase_plan(role: str, probe_environment_wide: bool) -> list[str]:
    """Ordered probe phases for one node.

    Single source of truth for both what ``_NodeProber.run`` executes and how
    many progress steps it emits (so the CLI bar total never drifts from the
    real work). Does **not** include the implicit leading "Connecting" step.
    """
    plan = ["Server specs", "Deployment signals"]
    if probe_environment_wide:
        plan.extend(_ENV_WIDE_PHASES)
    if role == "redis":
        plan.append("Redis sentinels")
    return plan


class _NodeProber:  # pylint: disable=too-few-public-methods
    """Runs the read-only probe set against one already-resolved target dict."""

    def __init__(self, target: dict[str, Any], *, probe_environment_wide: bool) -> None:
        self._target = target
        self._probe_environment_wide = probe_environment_wide

    def run(self, on_phase: Callable[[str], None] | None = None) -> NodeProbeResult:
        """Probe this node and return everything gathered (and not gathered).

        ``on_phase`` (optional) is called with a short label just before each
        phase runs — including the leading "Connecting" step — so a caller can
        surface live progress. It never affects what is collected.
        """
        name = self._target.get("name", "local")
        role = self._target.get("role", "")
        result = NodeProbeResult(node_name=name, role=role, display_role=role_display_label(role) if role else name)

        def _emit(label: str) -> None:
            if on_phase is not None:
                on_phase(label)

        _emit("Connecting")
        try:
            transport = transport_from_config(self._target)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            result.notes.append(f"Could not open a connection: {exc}")
            return result

        dispatch: dict[str, Callable[[Any, NodeProbeResult], None]] = {
            "Server specs": self._phase_server_specs,
            "Deployment signals": self._phase_deployment_signals,
            "Security policy": self._phase_security_policy,
            "Network / MTU": self._phase_network_mtu,
            "Running services": self._phase_running_services,
            "Installed packages": self._phase_installed_packages,
            "Cloud metadata": self._phase_cloud_metadata,
            "Redis sentinels": self._phase_redis_sentinels,
        }
        try:
            with transport:
                for label in _phase_plan(role, self._probe_environment_wide):
                    _emit(label)
                    dispatch[label](transport, result)
        except Exception as exc:  # noqa: BLE001 — one bad node must never abort the run
            result.notes.append(f"Probing stopped early: {exc}")

        return result

    def _phase_server_specs(self, transport, result: NodeProbeResult) -> None:
        facts = result.facts
        facts["os_release"] = self._run(transport, "cat /etc/os-release", result)
        facts["nproc"] = self._run(transport, "nproc --all", result)
        facts["meminfo"] = self._run(transport, "grep MemTotal /proc/meminfo", result)
        facts["disk"] = self._run(transport, "df -BG --output=size /", result)

    def _phase_deployment_signals(self, transport, result: NodeProbeResult) -> None:
        facts = result.facts
        facts["cgroup"] = self._run(transport, "cat /proc/1/cgroup", result, optional=True)
        facts["virt"] = self._run(transport, "systemd-detect-virt --vm", result, allow_nonzero=True)
        facts["k8s_serviceaccount"] = self._check(
            transport, "test -d /var/run/secrets/kubernetes.io/serviceaccount",
        )

    def _phase_security_policy(self, transport, result: NodeProbeResult) -> None:
        facts = result.facts
        getenforce_out = self._run(transport, "getenforce", result, optional=True)
        facts["getenforce"] = getenforce_out
        facts["getenforce_ran"] = getenforce_out is not None
        facts["fips"] = self._run(transport, "cat /proc/sys/crypto/fips_enabled", result, optional=True)

    def _phase_network_mtu(self, transport, result: NodeProbeResult) -> None:
        result.facts["mtu"] = self._probe_mtu(transport, result)

    def _phase_running_services(self, transport, result: NodeProbeResult) -> None:
        result.facts["services"] = self._run(
            transport,
            "systemctl list-units --type=service --state=running --no-pager --plain",
            result,
        )

    def _phase_installed_packages(self, transport, result: NodeProbeResult) -> None:
        result.facts["rpm_qa"] = self._run(transport, "rpm -qa", result, optional=True)

    def _phase_cloud_metadata(self, transport, result: NodeProbeResult) -> None:
        result.facts["cloud_init"] = self._run(
            transport, "cat /run/cloud-init/instance-data.json", result, optional=True,
        )

    def _phase_redis_sentinels(self, transport, result: NodeProbeResult) -> None:
        result.facts["sentinel_count"] = self._run(
            transport, "pgrep -c redis-sentinel", result, allow_nonzero=True, silent=True,
        )

    def _probe_mtu(self, transport, result: NodeProbeResult) -> str | None:
        ifaces_out = self._run(
            transport, "find /sys/class/net -mindepth 1 -maxdepth 1", result, optional=True,
        )
        if not ifaces_out:
            return None
        for path in ifaces_out.splitlines():
            iface = path.strip().rsplit("/", 1)[-1]
            if not iface or iface == "lo":
                continue
            mtu = self._run(transport, f"cat /sys/class/net/{iface}/mtu", result, optional=True, silent=True)
            if mtu:
                return mtu.strip()
        return None

    @staticmethod
    def _run(transport, command: str, result: NodeProbeResult, *,
              allow_nonzero: bool = False, optional: bool = False, silent: bool = False) -> str | None:
        """Run one read-only command; return stripped stdout, or None if unavailable.

        ``optional`` commands (a binary that legitimately may not exist, e.g.
        `rpm` on a Debian-family host) never add a note on failure. ``silent``
        additionally suppresses notes even for a non-optional command — used
        for presence checks where "not found" is itself the answer we want,
        not a gap to report.
        """
        try:
            outcome = transport.run_command(command)
        except Exception as exc:  # noqa: BLE001
            if not (optional or silent):
                result.notes.append(f"`{command.split()[0]}`: {exc}")
            return None
        if outcome.ok or allow_nonzero:
            return outcome.stdout.strip()
        if not (optional or silent):
            result.notes.append(f"`{command.split()[0]}`: {(outcome.stderr or '').strip() or 'command failed'}")
        return None

    @staticmethod
    def _check(transport, command: str) -> bool:
        """Run a presence-check command (e.g. `test -d ...`) for its exit code alone.

        Never notes a failure — a false result (path absent, on this host) is
        a legitimate answer, not a gap to report.
        """
        try:
            return transport.run_command(command).ok
        except Exception:  # noqa: BLE001
            return False


# ── fact -> architecture_store field mapping ────────────────────────────────

def _server_specs(facts: dict[str, Any]) -> dict[str, str]:
    out = {
        "os_type": _map_os_type(facts.get("os_release")),
        "cpu_cores": _map_cpu_cores(facts.get("nproc")),
        "memory_gb": _map_memory_gb(facts.get("meminfo")),
        "disk_space_gb": _map_disk_gb(facts.get("disk")),
    }
    return {k: v for k, v in out.items() if v}


def _deployment(facts: dict[str, Any]) -> dict[str, str]:
    value = _map_deployment_type(facts.get("cgroup"), facts.get("virt"), bool(facts.get("k8s_serviceaccount")))
    return {"deployment_type": value} if value else {}


def _environment_wide(facts: dict[str, Any]) -> dict[str, Any]:
    """Fields answered once for the whole environment, from the Platform node."""
    out: dict[str, Any] = {}
    selinux = _map_selinux_mode(facts.get("getenforce"), bool(facts.get("getenforce_ran")), facts.get("os_release"))
    if selinux:
        out["selinux_mode"] = selinux
    fips = _map_fips(facts.get("fips"), facts.get("os_release"))
    if fips:
        out["compliance_standards"] = fips
    if facts.get("mtu"):
        out["mtu_size_raw"] = facts["mtu"]  # canonical numeric value; each UI maps to its own option string
    monitoring = _map_service_matches(facts.get("services"), _MONITORING_TOOL_PATTERNS)
    if monitoring:
        out["monitoring_tools"] = monitoring
    log_agg = _map_service_matches(facts.get("services"), _LOG_AGGREGATOR_PATTERNS)
    if log_agg:
        out["log_aggregator"] = log_agg[0]
    va_tools = _map_service_matches(facts.get("services"), _VA_TOOL_PATTERNS)
    va_tools += [t for t in _map_service_matches(facts.get("rpm_qa"), _VA_TOOL_PATTERNS) if t not in va_tools]
    if va_tools:
        out["vulnerability_assessments_tools"] = va_tools
    hosting_provider = _map_hosting_provider(facts.get("cloud_init"))
    if hosting_provider:
        out["hosting_provider"] = hosting_provider
    return out


# Section(s) this role's server specs / deployment type feed. "iag" is
# resolved from the node's effective modules (gateway4 vs gateway5); "all"
# is the standalone all-in-one role — one host standing in for every
# component, so its facts apply to every section it's running.
_ROLE_TO_SECTIONS = {"iap": ("platform",), "mongo": ("mongodb",), "redis": ("redis",)}


def _sections_for_node(target: dict[str, Any]) -> tuple[str, ...]:
    role = target.get("role", "")
    if role in _ROLE_TO_SECTIONS:
        return _ROLE_TO_SECTIONS[role]
    modules = target.get("modules") or []
    if role == "iag":
        if "gateway5" in modules:
            return ("gateway5",)
        if "gateway4" in modules:
            return ("gateway4",)
        return ()
    if role == "all":
        sections = ["platform", "mongodb", "redis"]
        if "gateway5" in modules:
            sections.append("gateway5")
        if "gateway4" in modules:
            sections.append("gateway4")
        return tuple(sections)
    return ()


# ── capture reuse (MongoDB replica / Redis topology) ────────────────────────

def _facts_from_recent_capture(environment: str) -> tuple[dict[str, Any], str | None]:
    """Best-effort MongoDB/Redis topology facts from the most recent completed
    capture for this environment. Returns ({} , None) when no capture exists
    yet — never treated as an error, just nothing to add."""
    from platform_atlas.core.session_manager import get_session_manager

    try:
        sessions = [
            s for s in get_session_manager().list(sort_by="updated_at")
            if s.metadata.environment == environment and s.metadata.capture_completed
        ]
    except Exception:  # noqa: BLE001
        return {}, None
    if not sessions:
        return {}, None

    session = sessions[0]
    try:
        data = json.loads(session.capture_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None

    out: dict[str, Any] = {}
    # HA2-only: repl_config is only ever attempted when deployment.mode == "ha2"
    # (see mongo.py::collect()) — standalone envs simply won't have this key,
    # so there's no equivalent confirmed-standalone signal to autofill from.
    # `members` is replSetGetConfig's full member list (primary included), so
    # its length is already the TOTAL server count — no off-by-one adjustment.
    members = (((data.get("mongo") or {}).get("repl_config") or {}).get("config") or {}).get("members")
    if isinstance(members, list) and members:
        out["mongodb"] = {"mongo_node_count": len(members), "deployment_type": "Replica Set (recommended for HA)"}

    # redis.py::collect() puts a top-level "mode" ("sentinel" or "redis") on
    # every successful capture — the only unambiguous signal worth trusting
    # here; anything else (plain standalone vs. cluster) isn't distinguishable
    # from this field alone, so it's left for the user rather than guessed.
    if str((data.get("redis") or {}).get("mode") or "").lower() == "sentinel":
        out["redis"] = {"deployment_type": "Sentinel (recommended for HA)"}

    return out, session.name


# ── orchestration ────────────────────────────────────────────────────────────

def _node_label(target: dict[str, Any]) -> str:
    """Human label for a target, e.g. ``"IAP (iap-01)"`` — matches the per-node
    summary lines printed after the probe so the progress text reads the same."""
    name = target.get("name", "local")
    role = target.get("role", "")
    display = role_display_label(role) if role else ""
    if display and display != name:
        return f"{display} ({name})"
    return name


def _total_steps(targets: list[dict[str, Any]]) -> int:
    """Optimistic count of progress steps a full probe emits: for each node one
    "Connecting" step plus its phase plan, then one final capture-reuse step.

    Environment-wide phases are counted once (the first node in the sorted list
    runs them). A node that fails to connect emits fewer steps than counted, so
    the driver clamps to this total and snaps to 100% at the end — the bar still
    finishes cleanly rather than overshooting or stalling short.
    """
    total = 1  # trailing "Reusing capture data" step
    env_wide_assigned = False
    for target in targets:
        total += 1  # "Connecting"
        total += len(_phase_plan(target.get("role", ""), not env_wide_assigned))
        env_wide_assigned = True
    return total


@dataclass
class AutofillResult:
    """Outcome of one ``probe_environment()`` run."""
    completed: dict[str, Any] = field(default_factory=dict)  # architecture_store "autofill" shape
    node_summaries: list[NodeProbeResult] = field(default_factory=list)
    used_capture_session: str | None = None


def probe_environment(environment: str, *, on_progress: ProgressCallback | None = None) -> AutofillResult:
    """Run the full best-effort autofill pass for ``environment``.

    Extended tier only — raises ``TierViolationError`` otherwise (checked
    here in addition to the menu-level gating in ``manual.py``, so this
    function is never safe to call from the wrong tier by accident).

    ``on_progress`` (optional) is called once per probe step with
    ``(completed, total, label)`` so a caller can render a live progress bar.
    It is advisory only — the collected result is identical when it is None.
    """
    require_extended(
        "architecture autofill",
        hint="Architecture auto-detection is available in the Extended tier only.",
    )
    from platform_atlas.core.context import ctx

    result = AutofillResult()
    targets = [t for t in (ctx().config.targets or []) if t.get("transport") in ("ssh", "control_master")]

    # Probe the Platform/IAP node first (or whichever target is primary) so
    # the environment-wide fields (SELinux, FIPS, MTU, monitoring, hosting
    # provider) come from the component customers think of as "the server."
    targets.sort(key=lambda t: 0 if t.get("role") == "iap" else 1)

    total = _total_steps(targets)
    completed = 0

    def _advance(label: str) -> None:
        nonlocal completed
        completed += 1
        if on_progress is not None:
            on_progress(min(completed, total), total, label)

    env_wide_done = False

    for target in targets:
        sections = _sections_for_node(target)
        probe_env_wide = not env_wide_done
        node_label = _node_label(target)
        node_result = _NodeProber(target, probe_environment_wide=probe_env_wide).run(
            on_phase=lambda phase, _label=node_label: _advance(f"{_label} · {phase}"),
        )
        result.node_summaries.append(node_result)
        if probe_env_wide and node_result.facts:
            env_wide_done = True

        specs = _server_specs(node_result.facts)
        deployment = _deployment(node_result.facts)
        for section in sections:
            section_data: dict[str, Any] = {}
            if specs:
                section_data["server_specs"] = specs
            # mongodb has no deployment_type field; redis's deployment_type
            # means Single/Sentinel/Cluster topology (set below, from the
            # capture-derived signal), not infra type — this VM/container/
            # bare-metal signal doesn't apply to it.
            if section not in ("mongodb", "redis") and deployment:
                section_data["deployment"] = deployment
            if section == "redis" and node_result.facts.get("sentinel_count"):
                try:
                    section_data["sentinel_count"] = int(node_result.facts["sentinel_count"])
                except ValueError:
                    pass
            if section_data:
                result.completed.setdefault(section, {}).update(section_data)

        if probe_env_wide and node_result.facts:
            env_wide = _environment_wide(node_result.facts)
            if "hosting_provider" in env_wide:
                result.completed.setdefault("environment", {})["hosting_provider"] = env_wide["hosting_provider"]
            network_security = {k: v for k, v in env_wide.items()
                                 if k in ("selinux_mode", "compliance_standards", "mtu_size_raw")}
            if network_security:
                result.completed.setdefault("network_security", {}).update(network_security)
            if "monitoring_tools" in env_wide or "log_aggregator" in env_wide:
                monitoring = {k: v for k, v in env_wide.items() if k in ("monitoring_tools", "log_aggregator")}
                result.completed.setdefault("monitoring", {}).update(monitoring)
            if "vulnerability_assessments_tools" in env_wide:
                result.completed.setdefault("vulnerability_assessments", {})["tools"] = (
                    env_wide["vulnerability_assessments_tools"]
                )

    _advance("Reusing MongoDB / Redis capture data")
    capture_facts, session_name = _facts_from_recent_capture(environment)
    result.used_capture_session = session_name
    for section, data in capture_facts.items():
        result.completed.setdefault(section, {}).update(data)

    # Land exactly on 100% regardless of any nodes that emitted fewer steps
    # than counted (e.g. a connection that failed before its phases ran).
    if on_progress is not None:
        on_progress(total, total, "Done")

    return result
