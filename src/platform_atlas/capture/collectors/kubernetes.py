"""
Kubernetes Collector — Helm values.yaml and kubectl-based data collection

Replaces SSH-based collectors for Kubernetes deployments. Data is extracted
from Helm chart values files (IAP + optionally IAG5) and reshaped into the
same capture JSON structure that the SSH collectors produce, so downstream
validation and reporting work identically.

Data source priority:
    1. kubectl exec / kubectl get (live cluster data)
    2. values.yaml parsing (declarative config)

When kubectl is unavailable, the collector falls back gracefully to
values.yaml-only mode — the user is prompted during environment setup.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from platform_atlas.core.preflight import CheckResult

import yaml

from platform_atlas.capture.collectors.helm_utils import merge_helm_values
from platform_atlas.core.context import require_extended

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IAP env var → platform.properties key mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The Helm values.yaml env block uses ITENTIAL_<PROPERTY_NAME> where
# the property name is the UPPERCASE version of the same key found in
# the platform.properties file on bare-metal/VM installs. Conversion
# is simply: strip the ITENTIAL_ prefix and lowercase.
#
#   values.yaml:          ITENTIAL_MONGO_URL: "mongodb://..."
#   platform.properties:  mongo_url=mongodb://...


def _coerce_value(val: str) -> Any:
    """Coerce a string value to its natural Python type."""
    if not isinstance(val, str):
        return val
    lowered = val.lower()
    if lowered in ("yes", "true", "on"):
        return True
    if lowered in ("no", "false", "off"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _parse_resource_value(value: str) -> dict[str, Any]:
    """Parse a Kubernetes resource quantity (e.g. '14Gi', '3', '1000m')."""
    if not isinstance(value, str):
        return {"raw": value}

    value = value.strip()

    # Memory: Gi, Mi, Ki, G, M, K
    for suffix, multiplier in [
        ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024),
        ("G", 1e9), ("M", 1e6), ("K", 1e3),
    ]:
        if value.endswith(suffix):
            num = value[:-len(suffix)]
            try:
                return {"raw": value, "bytes": int(float(num) * multiplier)}
            except ValueError:
                return {"raw": value}

    # CPU: millicores
    if value.endswith("m"):
        try:
            return {"raw": value, "millicores": int(value[:-1]), "cores": float(value[:-1]) / 1000}
        except ValueError:
            return {"raw": value}

    # Plain number (CPU cores or bytes)
    try:
        return {"raw": value, "value": float(value)}
    except ValueError:
        return {"raw": value}


def _run_kubectl(
    args: list[str],
    *,
    context: str = "",
    namespace: str = "",
    kubeconfig: str = "",
    timeout: float = 30.0,
    binary: str = "kubectl",
) -> subprocess.CompletedProcess:
    """Run a kubectl command with optional context, namespace, and kubeconfig."""
    cmd = [binary or "kubectl"]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    if context:
        cmd.extend(["--context", context])
    if namespace:
        cmd.extend(["--namespace", namespace])
    cmd.extend(args)

    logger.debug("kubectl: %s", " ".join(cmd))
    t0 = time.monotonic()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.monotonic() - t0
    logger.debug("kubectl exit=%d (%.2fs): %s", result.returncode, elapsed, " ".join(cmd))
    if result.returncode != 0 and result.stderr:
        logger.debug("kubectl stderr: %s", result.stderr.strip()[:400])
    return result


def _compute_qos_class(resources: dict[str, Any]) -> str:
    """Derive the Kubernetes QoS class from a pod's resource spec.

    Guaranteed: CPU and memory both have matching requests == limits.
    BestEffort: No requests or limits defined at all.
    Burstable:  Everything else (partial limits, or requests < limits).
    """
    requests = resources.get("requests", {})
    limits = resources.get("limits", {})

    if not requests and not limits:
        return "BestEffort"

    cpu_req = requests.get("cpu")
    cpu_lim = limits.get("cpu")
    mem_req = requests.get("memory")
    mem_lim = limits.get("memory")

    if (cpu_req and cpu_lim and str(cpu_req) == str(cpu_lim) and
            mem_req and mem_lim and str(mem_req) == str(mem_lim)):
        return "Guaranteed"

    return "Burstable"


@dataclass
class KubernetesCollector:
    """
    Collects configuration data from Kubernetes Helm values and kubectl.

    Produces output dicts keyed to the same module names as the SSH-based
    collectors (system, platform_conf, gateway5) so the capture engine's
    CAPTURE_STRUCTURE mapping works identically.
    """

    values_yaml_path: str = ""
    kubectl_context: str = ""
    kubectl_namespace: str = ""
    kubeconfig_path: str = ""  # empty = kubectl's default (KUBECONFIG env / ~/.kube/config)
    use_kubectl: bool = False
    kubectl_binary: str = ""  # empty = resolve "kubectl" from PATH
    # Optional path to each chart's own default values.yaml. Real Helm
    # deployments commonly layer an environment-specific override file
    # (values_yaml_path) on top of the chart's defaults — a setting left at
    # its chart default never appears in the override file at all. When set,
    # these are merged UNDER the corresponding override (Helm -f semantics:
    # the override wins) before the values are used by any collector.
    values_yaml_defaults_path: str = ""
    iag5_values_yaml_defaults_path: str = ""

    _iap_values: dict[str, Any] = field(default_factory=dict, repr=False)
    _iag5_values: dict[str, Any] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        require_extended(
            "KubernetesCollector",
            hint="Kubernetes collection requires Extended Mode.",
        )
        if self.values_yaml_path:
            self._load_values()

    def _load_values(self) -> None:
        """Load and parse the values.yaml file(s)."""
        if self._loaded:
            return
        if not self.values_yaml_path:
            return

        path = Path(self.values_yaml_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Values file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Expected dict from {path}, got {type(raw).__name__}")

        # Detect whether this is an IAP or IAG5 values file
        # IAP values have an 'env' key with ITENTIAL_* vars
        # IAG5 values have 'serverSettings' / 'applicationSettings'
        if "env" in raw and any(
            k.startswith("ITENTIAL_") for k in (raw.get("env") or {})
        ):
            self._iap_values = self._merge_with_defaults(
                raw, self.values_yaml_defaults_path, label="IAP"
            )
            logger.debug("Loaded IAP values.yaml from %s", path)
        elif "serverSettings" in raw or "applicationSettings" in raw:
            self._iag5_values = self._merge_with_defaults(
                raw, self.iag5_values_yaml_defaults_path, label="IAG5"
            )
            logger.debug("Loaded IAG5 values.yaml from %s", path)
        else:
            # Assume IAP if we can't tell — env block may be empty/commented
            self._iap_values = self._merge_with_defaults(
                raw, self.values_yaml_defaults_path, label="IAP"
            )
            logger.debug("Loaded values.yaml as IAP (default) from %s", path)

        self._loaded = True

    def load_additional_values(self, path: str) -> None:
        """Load a second values.yaml (for IAG5 when IAP was loaded first)."""
        filepath = Path(path).expanduser().resolve()
        if not filepath.is_file():
            raise FileNotFoundError(f"Values file not found: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Expected dict from {filepath}, got {type(raw).__name__}")

        self._iag5_values = self._merge_with_defaults(
            raw, self.iag5_values_yaml_defaults_path, label="IAG5"
        )
        if "serverSettings" in raw or "applicationSettings" in raw:
            logger.debug("Loaded IAG5 values.yaml from %s", filepath)
        else:
            logger.debug("Loaded additional values.yaml from %s", filepath)

    def _merge_with_defaults(
        self, override: dict[str, Any], defaults_path: str, *, label: str
    ) -> dict[str, Any]:
        """Layer ``override`` on top of a chart's default values.yaml, Helm
        ``-f`` style, when ``defaults_path`` is configured.

        Best-effort: a missing or invalid defaults file is logged and
        skipped rather than raised — the override file alone is still a
        valid (if less complete) capture, and the defaults file is purely
        supplementary.
        """
        if not defaults_path:
            return override

        path = Path(defaults_path).expanduser().resolve()
        if not path.is_file():
            logger.debug(
                "%s chart-defaults file not found: %s — skipping merge", label, path
            )
            return override

        try:
            with open(path, "r", encoding="utf-8") as f:
                defaults = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            logger.debug(
                "%s chart-defaults file is not valid YAML: %s — skipping merge",
                label, exc,
            )
            return override

        if not isinstance(defaults, dict):
            logger.debug(
                "%s chart-defaults file %s did not parse to a mapping — skipping merge",
                label, path,
            )
            return override

        logger.debug("%s: merging chart defaults from %s under override", label, path)
        return merge_helm_values(defaults, override)

    # ── Module-compatible collection methods ──────────────────────

    def collect_system_info(self) -> dict[str, Any]:
        """
        Produce system info from K8s resource specs.

        Maps to the same capture path as the SSH system collector
        (CAPTURE_STRUCTURE["system"] → "system").
        """
        self._load_values()
        values = self._iap_values

        info: dict[str, Any] = {
            "meta": {
                "ts": time.time(),
                "source": "kubernetes",
            },
            "host": {
                "hostname": "kubernetes-pod",
                "fqdn": "kubernetes-pod",
            },
            "os": {
                "system": "Linux",
                "platform": "Kubernetes",
            },
        }

        # Always create the kubernetes section so kubectl-only fields have a
        # parent to land in even when values.yaml is absent. Values.yaml-derived
        # fields are only added below when _iap_values is non-empty — otherwise
        # they would be False/None with the key present, causing false FAILs on
        # rules that should SKIP when values.yaml is not configured.
        info["kubernetes"] = {}

        if values:
            # Parse resource requests/limits from the IAP values
            resources = values.get("resources", {})
            requests = resources.get("requests", {})
            limits = resources.get("limits", {})

            cpu_req = _parse_resource_value(str(requests.get("cpu", "")))
            mem_req = _parse_resource_value(str(requests.get("memory", "")))
            mem_limit = _parse_resource_value(str(limits.get("memory", "")))

            info["cpu"] = {
                "cores_logical": cpu_req.get("value") or cpu_req.get("cores"),
                "source": "kubernetes_resource_request",
            }

            # Use limits.memory as the "total" since that's the pod ceiling
            mem_bytes = mem_limit.get("bytes") or mem_req.get("bytes")
            info["memory"] = {
                "virtual": {"total": mem_bytes} if mem_bytes else {},
                "source": "kubernetes_resource_limit",
            }

            info["kubernetes"].update({
                "replica_count": values.get("replicaCount"),
                "image": values.get("image", {}),
                "resources": resources,
                "service": values.get("service", {}),
                "ingress_enabled": values.get("ingress", {}).get("enabled"),
                "use_tls": values.get("useTLS"),
                "use_websockets": values.get("useWebSockets"),
                "cert_manager_enabled": values.get("certManager", {}).get("enabled"),
                "storage_class": values.get("storageClass", {}),
                "pvc": values.get("persistentVolumeClaims", {}),
                # Health probe configuration — both should be enabled in production
                "liveness_probe_enabled": values.get("livenessProbe", {}).get("enabled"),
                "readiness_probe_enabled": values.get("readinessProbe", {}).get("enabled"),
                # Log persistence — emptyDir (false) means logs are lost on pod restart
                "mount_log_volume": values.get("mountLogVolume"),
                # QoS class: Guaranteed requires cpu.requests == cpu.limits AND
                # memory.requests == memory.limits; anything else is Burstable/BestEffort
                "qos_class": _compute_qos_class(resources),
                # Startup probe
                "startup_probe_enabled": values.get("startupProbe", {}).get("enabled"),
                # Probe timeout configuration (seconds) — K8s default is 1s when not set
                "liveness_probe_timeout_seconds": values.get("livenessProbe", {}).get("timeoutSeconds"),
                "readiness_probe_timeout_seconds": values.get("readinessProbe", {}).get("timeoutSeconds"),
                "startup_probe_timeout_seconds": values.get("startupProbe", {}).get("timeoutSeconds"),
                "liveness_probe_initial_delay_seconds": values.get("livenessProbe", {}).get("initialDelaySeconds"),
                "readiness_probe_initial_delay_seconds": values.get("readinessProbe", {}).get("initialDelaySeconds"),
                "startup_probe_failure_threshold": values.get("startupProbe", {}).get("failureThreshold"),
                # Resource definition flags — each is True only when the value is explicitly set
                "cpu_requests_set": requests.get("cpu") is not None,
                "cpu_limits_set": limits.get("cpu") is not None,
                "memory_requests_set": requests.get("memory") is not None,
                "memory_limits_set": limits.get("memory") is not None,
            })

        # If kubectl is available, enhance with live data
        if self.use_kubectl and self._kubectl_available():
            self._enhance_system_with_kubectl(info)

        return info

    def collect_platform_conf(self) -> dict[str, Any]:
        """
        Extract platform configuration from IAP values.yaml env block.

        Maps to the same capture path as the SSH filesystem collector's
        get_unformatted_config(service_name="platform")
        (CAPTURE_STRUCTURE["platform_conf"] → "platform.config_file").

        The env var names in values.yaml are identical to platform.properties
        keys, just uppercased and prefixed with ITENTIAL_. We reverse that:
            ITENTIAL_MONGO_URL → mongo_url
        """
        self._load_values()
        env_block = self._iap_values.get("env", {})

        if not env_block:
            logger.debug("No env block in IAP values.yaml")
            return {}

        config: dict[str, Any] = {}

        for env_key, env_value in env_block.items():
            if not isinstance(env_key, str) or not env_key.startswith("ITENTIAL_"):
                continue

            # Strip prefix and lowercase — matches platform.properties key names
            prop_key = env_key.removeprefix("ITENTIAL_").lower()
            config[prop_key] = _coerce_value(str(env_value))

        if not config:
            logger.debug("No ITENTIAL_* env vars found in values.yaml")
            return {}

        return config

    def collect_gateway5(self) -> dict[str, Any]:
        """
        Extract Gateway5 configuration from the loaded IAG5 values.yaml.

        Delegates to :func:`parse_gateway5_yaml` (shared with the SSH and
        file-based Gateway5 collectors) so every source understands the same
        Compose/Helm shapes and emits an identical capture structure.
        Maps to CAPTURE_STRUCTURE["gateway5"] → "gateway5".
        """
        from platform_atlas.capture.collectors.gateway5 import parse_gateway5_yaml

        if not self._iag5_values:
            return {}

        collected = parse_gateway5_yaml(self._iag5_values)
        return collected.to_dict() if collected.resolved else {}

    def collect_kubernetes_helm(self) -> dict[str, Any]:
        """
        Store the raw Helm values for reference/debugging.

        Maps to CAPTURE_STRUCTURE["kubernetes_helm"] → "kubernetes.helm_values".
        """
        self._load_values()
        result: dict[str, Any] = {}

        if self._iap_values:
            result["iap"] = self._iap_values
        if self._iag5_values:
            result["iag5"] = self._iag5_values

        return result if result else {}

    # ── kubectl connectivity ─────────────────────────────────────

    def _kubectl_binary(self) -> str:
        """Resolve the kubectl binary: configured path, or 'kubectl' from PATH."""
        return self.kubectl_binary or "kubectl"

    def _kubectl_available(self) -> bool:
        """Quick binary-presence check (use _test_kubectl for a full probe)."""
        if self.kubectl_binary:
            return Path(self.kubectl_binary).is_file()
        return shutil.which("kubectl") is not None

    def _test_kubectl(self) -> tuple[bool, str]:
        """Full kubectl connectivity probe — binary, client, API server, namespace access.

        Returns (success, reason_string). Analogous to SSH's preflight test:
        checks that kubectl is installed, the client works, the API server is
        reachable, and we have at least get-pods permission in the namespace.
        """
        logger.debug(
            "kubectl preflight probe (context=%s namespace=%s)",
            self.kubectl_context or "default",
            self.kubectl_namespace or "default",
        )
        if not self._kubectl_available():
            return False, "kubectl binary not found"

        binary = self._kubectl_binary()

        # Client version check (no cluster contact needed)
        try:
            logger.debug("kubectl: %s version --client --output=json", binary)
            r = subprocess.run(
                [binary, "version", "--client", "--output=json"],
                capture_output=True, text=True, timeout=5.0,
            )
            if r.returncode != 0:
                return False, f"kubectl version --client failed: {r.stderr.strip()[:120]}"
        except subprocess.TimeoutExpired:
            return False, "kubectl version --client timed out"

        # API server reachability
        try:
            r = _run_kubectl(
                ["cluster-info", "--request-timeout=5s"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                timeout=10.0,
                binary=self._kubectl_binary(),
            )
            if r.returncode != 0:
                return False, f"kubectl cluster-info failed: {r.stderr.strip()[:120]}"
        except subprocess.TimeoutExpired:
            return False, "kubectl cluster-info timed out"

        # Namespace-level permission check
        try:
            ns = self.kubectl_namespace or "default"
            r = _run_kubectl(
                ["get", "pods", "--request-timeout=5s"],
                context=self.kubectl_context,
                namespace=ns,
                timeout=10.0,
                binary=self._kubectl_binary(),
            )
            if r.returncode != 0:
                return False, f"kubectl get pods denied in namespace '{ns}': {r.stderr.strip()[:120]}"
        except subprocess.TimeoutExpired:
            return False, "kubectl get pods timed out"

        ctx_label = self.kubectl_context or "default"
        ns_label = self.kubectl_namespace or "default"
        return True, f"context={ctx_label} namespace={ns_label}"

    def _find_iap_pod(self) -> str:
        """Return the name of a running IAP pod, or empty string if none found."""
        logger.debug("kubectl: searching for IAP pod by label selector")
        for label in ("app.kubernetes.io/name=iap", "app=iap"):
            cmd = ["get", "pods", "-l", label, "-o", "jsonpath={.items[0].metadata.name}"]
            try:
                r = _run_kubectl(
                    cmd,
                    context=self.kubectl_context,
                    namespace=self.kubectl_namespace,
                    kubeconfig=self.kubeconfig_path,
                    timeout=10.0,
                    binary=self._kubectl_binary(),
                )
                name = r.stdout.strip()
                if r.returncode == 0 and name:
                    logger.debug("kubectl: found IAP pod %r (label=%s)", name, label)
                    return name
            except subprocess.TimeoutExpired:
                continue
        logger.debug("kubectl: no IAP pod found")
        return ""

    # ── kubectl enhancement methods ──────────────────────────────

    def _enhance_system_with_kubectl(self, info: dict[str, Any]) -> None:
        """Add live pod status and resource usage from kubectl to the system info dict.

        Platform API (health/server, health/adapters, health/applications) is the
        primary source for version and service data. kubectl is used here only for
        data the API cannot provide: pod scheduling state and live resource consumption.
        """
        logger.debug(
            "kubectl: enhancing system info with live cluster data (context=%s namespace=%s)",
            self.kubectl_context or "default",
            self.kubectl_namespace or "default",
        )
        try:
            result = _run_kubectl(
                ["get", "pods", "-o", "json"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                binary=self._kubectl_binary(),
            )
            if result.returncode == 0:
                pod_data = json.loads(result.stdout)
                pods = pod_data.get("items", [])

                # Recognize both Platform pods (iap/itential/platform) and
                # Gateway5 pods (gateway5/iag5/iag) — a namespace pointed at
                # an additional Gateway5 deployment has none of the former.
                _relevant_names = ("iap", "itential", "platform", "gateway5", "iag5", "iag")
                relevant_pods = [
                    p for p in pods
                    if any(
                        name in p.get("metadata", {}).get("name", "").lower()
                        for name in _relevant_names
                    )
                ]

                info["kubernetes"]["pods"] = [
                    {
                        "name": p["metadata"]["name"],
                        "phase": p.get("status", {}).get("phase"),
                        "restart_count": sum(
                            cs.get("restartCount", 0)
                            for cs in p.get("status", {}).get("containerStatuses", [])
                        ),
                        "node": p.get("spec", {}).get("nodeName"),
                        "ready": all(
                            cs.get("ready", False)
                            for cs in p.get("status", {}).get("containerStatuses", [])
                        ),
                    }
                    for p in relevant_pods
                ]

                # Aggregate restart counts across all relevant pods
                _restart_counts = [
                    pod["restart_count"]
                    for pod in info["kubernetes"]["pods"]
                ]
                info["kubernetes"]["max_restart_count"] = max(_restart_counts, default=0)
                info["kubernetes"]["pods_with_restarts"] = sum(
                    1 for c in _restart_counts if c > 0
                )

        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
            logger.debug("kubectl pod enrichment failed: %s", e)

        try:
            result = _run_kubectl(
                ["top", "pods", "--no-headers"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                binary=self._kubectl_binary(),
            )
            if result.returncode == 0 and result.stdout.strip():
                usage = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3:
                        usage.append({"pod": parts[0], "cpu": parts[1], "memory": parts[2]})
                if usage:
                    info["kubernetes"]["resource_usage"] = usage
        except (subprocess.TimeoutExpired, ValueError) as e:
            logger.debug("kubectl top enrichment failed: %s", e)

        self._collect_hpa(info)
        self._collect_nodes(info)
        self._collect_deployment_kind(info)

    def _collect_hpa(self, info: dict[str, Any]) -> None:
        """Add HPA configuration from kubectl get hpa to info["kubernetes"]."""
        try:
            result = _run_kubectl(
                ["get", "hpa", "-o", "json"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                binary=self._kubectl_binary(),
            )
            if result.returncode != 0:
                return
            hpa_data = json.loads(result.stdout)
            items = hpa_data.get("items", [])
            if not items:
                info["kubernetes"]["hpa_enabled"] = False
                return
            # Use the first HPA object (typically one per deployment)
            hpa = items[0]
            spec = hpa.get("spec", {})
            metrics = spec.get("metrics", [])
            metric_type = None
            if metrics:
                m = metrics[0]
                metric_type = (
                    m.get("resource", {}).get("name")
                    or m.get("type")
                )
            info["kubernetes"]["hpa_enabled"] = True
            info["kubernetes"]["hpa_min_replicas"] = spec.get("minReplicas")
            info["kubernetes"]["hpa_max_replicas"] = spec.get("maxReplicas")
            info["kubernetes"]["hpa_metric_type"] = metric_type
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
            logger.debug("kubectl HPA collection failed: %s", e)

    def _collect_nodes(self, info: dict[str, Any]) -> None:
        """Add node count and instance type from kubectl get nodes to info["kubernetes"]."""
        _INSTANCE_TYPE_LABELS = (
            "node.kubernetes.io/instance-type",
            "beta.kubernetes.io/instance-type",
            "topology.kubernetes.io/instance-type",
        )
        try:
            result = _run_kubectl(
                ["get", "nodes", "-o", "json"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                binary=self._kubectl_binary(),
            )
            if result.returncode != 0:
                return
            node_data = json.loads(result.stdout)
            nodes = node_data.get("items", [])
            info["kubernetes"]["node_count"] = len(nodes)
            instance_types: list[str] = []
            for node in nodes:
                labels = node.get("metadata", {}).get("labels", {})
                for label_key in _INSTANCE_TYPE_LABELS:
                    itype = labels.get(label_key)
                    if itype and itype not in instance_types:
                        instance_types.append(itype)
                        break
            info["kubernetes"]["node_instance_types"] = instance_types
            info["kubernetes"]["node_instance_type"] = instance_types[0] if instance_types else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
            logger.debug("kubectl node collection failed: %s", e)

    def _collect_deployment_kind(self, info: dict[str, Any]) -> None:
        """Detect deployment object kind (Deployment vs StatefulSet) from kubectl."""
        try:
            result = _run_kubectl(
                ["get", "deployment,statefulset", "-o", "json"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                binary=self._kubectl_binary(),
            )
            if result.returncode != 0:
                return
            data = json.loads(result.stdout)
            items = data.get("items", [])
            if not items:
                info["kubernetes"]["deployment_kind"] = "Unknown"
                return
            # Report the kind of the first object found
            info["kubernetes"]["deployment_kind"] = items[0].get("kind", "Unknown")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
            logger.debug("kubectl deployment kind collection failed: %s", e)

    def collect_kubectl_env(self) -> dict[str, Any]:
        """
        Collect live environment variables from a running IAP pod via kubectl exec.

        Prompts the user for confirmation before exec'ing into a pod.
        Falls back gracefully if kubectl is unavailable, the user declines,
        or the process is running non-interactively (daemon/headless/CI).
        Returns a platform config dict in the same format as platform_conf.
        """
        if not self.use_kubectl or not self._kubectl_available():
            return {}

        # Skip the interactive prompt entirely when stdin is not a TTY —
        # daemon mode, WebUI jobs, CI, and piped execution all land here.
        # KeyboardInterrupt from a None questionary return would otherwise
        # escape as BaseException and kill the capture job.
        import sys
        if not sys.stdin.isatty():
            logger.debug("kubectl exec printenv skipped — no interactive TTY")
            return {}

        # kubectl exec opens a session into a running container — ask first
        try:
            import questionary
            allow = questionary.confirm(
                "Run 'kubectl exec printenv' in an IAP pod to collect live environment variables?\n"
                "  Warning: captured values include credentials (MongoDB URI, Redis URI, client\n"
                "  secret). These are stored in 01_capture.json (owner-only) and can be redacted\n"
                "  before sharing via 'session export --redact'.",
                default=False,
            ).ask()
            if allow is None:
                raise KeyboardInterrupt
            if not allow:
                logger.debug("User declined kubectl exec — skipping")
                return {}
        except ImportError:
            logger.debug("questionary not available — skipping kubectl exec prompt")
            return {}

        try:
            pod_name = self._find_iap_pod()
            if not pod_name:
                logger.debug("No IAP pod found for kubectl exec")
                return {}

            logger.debug("kubectl: exec printenv into pod %r to collect live env vars", pod_name)
            result = _run_kubectl(
                ["exec", pod_name, "--", "printenv"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                kubeconfig=self.kubeconfig_path,
                timeout=15.0,
                binary=self._kubectl_binary(),
            )

            if result.returncode != 0:
                logger.debug("kubectl exec printenv failed: %s", result.stderr)
                return {}

            config: dict[str, Any] = {}
            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if not key.startswith("ITENTIAL_"):
                    continue

                # Strip prefix and lowercase — matches platform.properties keys
                prop_key = key.removeprefix("ITENTIAL_").lower()
                config[prop_key] = _coerce_value(value)

            return config

        except (subprocess.TimeoutExpired, Exception) as e:
            logger.debug("kubectl exec collection failed: %s", e)
            return {}

    # ── Preflight check ──────────────────────────────────────────

    def preflight(self) -> "CheckResult":
        """Verify at least one data source (values.yaml or kubectl) is accessible."""
        from platform_atlas.core.preflight import CheckResult

        service_name = "Kubernetes"
        values_ok = False
        kubectl_ok = False
        issues: list[str] = []
        detail_parts: list[str] = []

        # Check values.yaml if configured
        if self.values_yaml_path:
            path = Path(self.values_yaml_path).expanduser().resolve()
            if not path.is_file():
                issues.append(f"Values file not found: {path}")
            else:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        yaml.safe_load(f)
                    values_ok = True
                    detail_parts.append(f"values.yaml: {path.name}")
                except Exception as e:
                    issues.append(f"Cannot parse values.yaml: {e}")

        # Check kubectl if enabled — full probe: binary, client, API server, namespace access
        if self.use_kubectl:
            ok, reason = self._test_kubectl()
            if ok:
                kubectl_ok = True
                ctx_label = self.kubectl_context or "default"
                ns_label = self.kubectl_namespace or "default"
                detail_parts.append(f"kubectl: {ctx_label}/{ns_label}")
            else:
                issues.append(f"kubectl unavailable: {reason}")

        # Need at least one working source
        if not values_ok and not kubectl_ok:
            if issues:
                return CheckResult.fail(service_name, "; ".join(issues))
            return CheckResult.fail(
                service_name,
                "No data source available — configure values.yaml path and/or enable kubectl",
            )

        if issues:
            return CheckResult.warn(
                service_name,
                f"{'; '.join(issues)} (continuing with available sources)",
            )

        return CheckResult.ok(service_name, " | ".join(detail_parts))


if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
