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

# IAG5 serverSettings/applicationSettings keys → GATEWAY_* env var names
_IAG5_TO_GATEWAY_ENV: dict[str, str] = {
    # applicationSettings
    "logLevel": "GATEWAY_LOG_LEVEL",
    "storeBackend": "GATEWAY_STORE_BACKEND",
    # serverSettings
    "connectEnabled": "GATEWAY_CONNECT_ENABLED",
    "connectInsecureEnabled": "GATEWAY_CONNECT_INSECURE_TLS",
}


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
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Run a kubectl command with optional context and namespace."""
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    if namespace:
        cmd.extend(["--namespace", namespace])
    cmd.extend(args)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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
    use_kubectl: bool = False

    _iap_values: dict[str, Any] = field(default_factory=dict, repr=False)
    _iag5_values: dict[str, Any] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.values_yaml_path:
            self._load_values()

    def _load_values(self) -> None:
        """Load and parse the values.yaml file(s)."""
        if self._loaded:
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
            self._iap_values = raw
            logger.debug("Loaded IAP values.yaml from %s", path)
        elif "serverSettings" in raw or "applicationSettings" in raw:
            self._iag5_values = raw
            logger.debug("Loaded IAG5 values.yaml from %s", path)
        else:
            # Assume IAP if we can't tell — env block may be empty/commented
            self._iap_values = raw
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

        if "serverSettings" in raw or "applicationSettings" in raw:
            self._iag5_values = raw
            logger.debug("Loaded IAG5 values.yaml from %s", filepath)
        else:
            self._iag5_values = raw
            logger.debug("Loaded additional values.yaml from %s", filepath)

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

        # Kubernetes-specific metadata
        info["kubernetes"] = {
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
        }

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
        Extract Gateway5 configuration from IAG5 values.yaml.

        Maps to the same capture path as the SSH Gateway5 collector
        (CAPTURE_STRUCTURE["gateway5"] → "gateway5").
        """
        from platform_atlas.capture.collectors.gateway5 import _CollectedVars

        if not self._iag5_values:
            return {}

        values = self._iag5_values
        collected = _CollectedVars()
        collected.seed()

        # applicationSettings → GATEWAY_* env vars
        app_settings = values.get("applicationSettings", {})
        for yaml_key, env_name in _IAG5_TO_GATEWAY_ENV.items():
            val = app_settings.get(yaml_key)
            if val is not None:
                collected.set_if_missing(env_name, str(val), "helm_values")

        # Derive additional vars from settings structure
        if app_settings.get("storeBackend"):
            collected.set_if_missing(
                "GATEWAY_STORE_BACKEND",
                str(app_settings["storeBackend"]),
                "helm_values",
            )

        if app_settings.get("logLevel"):
            collected.set_if_missing(
                "GATEWAY_LOG_LEVEL",
                str(app_settings["logLevel"]),
                "helm_values",
            )

        # serverSettings
        server_settings = values.get("serverSettings", {})
        if server_settings.get("connectEnabled") is not None:
            collected.set_if_missing(
                "GATEWAY_CONNECT_ENABLED",
                str(server_settings["connectEnabled"]).lower(),
                "helm_values",
            )

        if server_settings.get("connectInsecureEnabled") is not None:
            collected.set_if_missing(
                "GATEWAY_CONNECT_INSECURE_TLS",
                str(server_settings["connectInsecureEnabled"]).lower(),
                "helm_values",
            )

        # Check for HA configuration
        runner_settings = values.get("runnerSettings", {})
        if runner_settings.get("replicaCount", 0) > 0:
            collected.set_if_missing(
                "GATEWAY_SERVER_DISTRIBUTED_EXECUTION",
                "true",
                "helm_values",
            )

        # TLS from top-level useTLS
        if values.get("useTLS") is not None:
            tls_val = str(values["useTLS"]).lower()
            collected.set_if_missing("GATEWAY_SERVER_USE_TLS", tls_val, "helm_values")
            collected.set_if_missing("GATEWAY_CLIENT_USE_TLS", tls_val, "helm_values")
            collected.set_if_missing("GATEWAY_RUNNER_USE_TLS", tls_val, "helm_values")

        # Inline env overrides from serverSettings.env and applicationSettings.env
        for env_source in [
            server_settings.get("env", {}),
            runner_settings.get("env", {}),
            app_settings.get("env", {}),
        ]:
            if isinstance(env_source, dict):
                for key, val in env_source.items():
                    if isinstance(key, str) and key.startswith("GATEWAY_"):
                        collected.set_if_missing(key, str(val), "helm_env_override")

        if not collected.resolved:
            return {}

        return collected.to_dict()

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

    # ── kubectl enhancement methods ──────────────────────────────

    def _kubectl_available(self) -> bool:
        """Check if kubectl is installed and accessible."""
        return shutil.which("kubectl") is not None

    def _enhance_system_with_kubectl(self, info: dict[str, Any]) -> None:
        """Add live pod data from kubectl to the system info dict."""
        try:
            # Get pod status
            result = _run_kubectl(
                ["get", "pods", "-o", "json"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
            )
            if result.returncode == 0:
                pod_data = json.loads(result.stdout)
                pods = pod_data.get("items", [])

                # Filter to IAP pods (common label patterns)
                iap_pods = [
                    p for p in pods
                    if "iap" in p.get("metadata", {}).get("name", "").lower()
                    or "itential" in p.get("metadata", {}).get("name", "").lower()
                    or "platform" in p.get("metadata", {}).get("name", "").lower()
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
                    for p in iap_pods
                ]

        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
            logger.debug("kubectl pod enrichment failed: %s", e)

        try:
            # Get resource usage (requires metrics-server)
            result = _run_kubectl(
                ["top", "pods", "--no-headers"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
            )
            if result.returncode == 0 and result.stdout.strip():
                usage = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3:
                        usage.append({
                            "pod": parts[0],
                            "cpu": parts[1],
                            "memory": parts[2],
                        })
                if usage:
                    info["kubernetes"]["resource_usage"] = usage
        except (subprocess.TimeoutExpired, ValueError) as e:
            logger.debug("kubectl top enrichment failed: %s", e)

    def collect_kubectl_env(self) -> dict[str, Any]:
        """
        Collect live environment variables from a running IAP pod via kubectl exec.

        Prompts the user for confirmation before exec'ing into a pod.
        Falls back gracefully if kubectl is unavailable or the user declines.
        Returns a platform config dict in the same format as platform_conf.
        """
        if not self.use_kubectl or not self._kubectl_available():
            return {}

        # kubectl exec opens a session into a running container — ask first
        try:
            import questionary
            allow = questionary.confirm(
                "Run 'kubectl exec printenv' in an IAP pod to collect live environment variables?",
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
            # Find an IAP pod
            result = _run_kubectl(
                [
                    "get", "pods",
                    "-o", "jsonpath={.items[0].metadata.name}",
                    "-l", "app.kubernetes.io/name=iap",
                ],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
            )

            if result.returncode != 0 or not result.stdout.strip():
                # Try alternative label
                result = _run_kubectl(
                    [
                        "get", "pods",
                        "-o", "jsonpath={.items[0].metadata.name}",
                    ],
                    context=self.kubectl_context,
                    namespace=self.kubectl_namespace,
                )

            pod_name = result.stdout.strip()
            if not pod_name:
                logger.debug("No IAP pod found for kubectl exec")
                return {}

            # Exec printenv in the pod
            result = _run_kubectl(
                ["exec", pod_name, "--", "printenv"],
                context=self.kubectl_context,
                namespace=self.kubectl_namespace,
                timeout=15.0,
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
        """Verify values.yaml is accessible and optionally check kubectl."""
        from platform_atlas.core.preflight import CheckResult

        service_name = "Kubernetes"
        issues: list[str] = []

        # Check values.yaml
        if self.values_yaml_path:
            path = Path(self.values_yaml_path).expanduser().resolve()
            if not path.is_file():
                return CheckResult.fail(
                    service_name,
                    f"Values file not found: {path}",
                )
            try:
                with open(path, "r", encoding="utf-8") as f:
                    yaml.safe_load(f)
            except Exception as e:
                return CheckResult.fail(
                    service_name,
                    f"Cannot parse values.yaml: {e}",
                )
        else:
            issues.append("No values.yaml path configured")

        # Check kubectl if enabled
        if self.use_kubectl:
            if not self._kubectl_available():
                issues.append("kubectl not found in PATH")
            else:
                try:
                    result = _run_kubectl(
                        ["cluster-info"],
                        context=self.kubectl_context,
                        namespace=self.kubectl_namespace,
                        timeout=10.0,
                    )
                    if result.returncode != 0:
                        issues.append(f"kubectl cluster-info failed: {result.stderr.strip()[:100]}")
                except subprocess.TimeoutExpired:
                    issues.append("kubectl cluster-info timed out")

        if issues and not self.values_yaml_path:
            return CheckResult.fail(service_name, "; ".join(issues))

        if issues:
            return CheckResult.warn(
                service_name,
                f"Values file OK, but: {'; '.join(issues)}",
            )

        detail = f"Values: {self.values_yaml_path}"
        if self.use_kubectl:
            detail += f" | kubectl: {self.kubectl_context or 'default'}/{self.kubectl_namespace or 'default'}"

        return CheckResult.ok(service_name, detail)


if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
