# pylint: disable=line-too-long
"""
Preflight Runner

Verifies connectivity to all configured infrastructure before capture.
Checks are split into two phases:

  1. Node checks — SSH reachability for every node in the deployment topology
  2. Collector checks — service-level connectivity (pymongo, redis-py, OAuth)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace as _dc_replace
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

from platform_atlas.core.credentials import (
    credential_store,
    active_secret_store,
    CredentialBackendType,
    FileStoreHealth,
    verify_keyring_backend,
)
from platform_atlas.core.exceptions import CredentialError
from platform_atlas.core import ui

logger = logging.getLogger(__name__)
theme = ui.theme


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class CheckStatus(Enum):
    """Result status for a preflight check"""
    PASS = "pass" # nosec B105
    FAIL = "fail"
    SKIP = "skip"
    WARN = "warn"

@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single preflight check"""
    name: str
    status: CheckStatus
    message: str
    details: str = ""
    group: str = ""       # "ssh" or "collectors" — used for display grouping

    @property
    def passed(self) -> bool:
        return self.status != CheckStatus.FAIL

    @classmethod
    def ok(cls, name: str, message: str = "OK", details: str = "", group: str = "") -> CheckResult:
        """Convenience constructor for passing checks"""
        return cls(name, CheckStatus.PASS, message, details, group)

    @classmethod
    def fail(cls, name: str, message: str, details: str = "", group: str = "") -> CheckResult:
        """Convenience constructor for failing checks"""
        return cls(name, CheckStatus.FAIL, message, details, group)

    @classmethod
    def skip(cls, name: str, message: str = "Skipped", details: str = "", group: str = "") -> CheckResult:
        """Convenience constructor for skipped checks"""
        return cls(name, CheckStatus.SKIP, message, details, group)

    @classmethod
    def warn(cls, name: str, message: str, details: str = "", group: str = "") -> CheckResult:
        """Convenience constructor for warnings"""
        return cls(name, CheckStatus.WARN, message, details, group)

@runtime_checkable
class SupportsPreflightCheck(Protocol):
    """Protocol for collectors that support preflight checks"""

    @staticmethod
    def preflight() -> CheckResult:
        """Run a lightweight connectivity/config check"""
        pass

@dataclass(slots=True)
class PreflightReport:
    """Aggregated results from all preflight checks"""
    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.status != CheckStatus.FAIL for r in self.results)

    @property
    def summary(self) -> dict[CheckStatus, int]:
        counts = {s: 0 for s in CheckStatus}
        for r in self.results:
            counts[r.status] += 1
        return counts

    @property
    def ssh_results(self) -> list[CheckResult]:
        return [r for r in self.results if r.group == "ssh"]

    @property
    def collector_results(self) -> list[CheckResult]:
        return [r for r in self.results if r.group == "collectors"]

def _check_credential_backend() -> CheckResult:
    """Verify the credential backend is functional and secrets are available."""
    try:
        store = credential_store()
    except CredentialError as e:
        return CheckResult.fail(
            "Credential Backend",
            f"Backend unavailable: {e}",
            details=e.details.get("fix", "") if hasattr(e, "details") else "",
        )
    except Exception as e:
        return CheckResult.fail(
            "Credential Backend",
            f"Could not initialize credential store: {type(e).__name__}: {e}",
        )

    file_store_active = False
    if store.backend_type == CredentialBackendType.VAULT:
        service_name = "HashiCorp Vault"
        # VaultBackend validated the connection during __init__.
        # If we got here, the connection is good — just check for secrets.
    elif active_secret_store().is_file:
        # The encrypted local file is the chosen backend. Functional, but
        # reported honestly as a warning (never a green pass, and never
        # described as an "encrypted keyring").
        service_name = "Credential Store"
        file_store_active = True
        if active_secret_store().health() == FileStoreHealth.UNREADABLE:
            return CheckResult.fail(
                service_name,
                "Encrypted local credential file is unreadable",
                "The file (~/.atlas/credentials.enc) or its key (~/.atlas/.keysalt) was changed, "
                "removed, or moved from another machine. Run 'platform-atlas config credentials' "
                "to create a fresh one and re-enter your credentials.",
            )
    else:
        service_name = "OS Keyring"
        is_secure, is_functional, backend = verify_keyring_backend()
        if not is_functional:
            # You explicitly chose the OS keyring but it can't store a secret on
            # this host. Point at the two stores that work everywhere.
            return CheckResult.fail(
                service_name,
                f"No usable keyring backend: {backend}",
                "Run 'platform-atlas config credentials --use-file-store' for an encrypted "
                "local file, or configure Vault as the credential backend",
            )
        if not is_secure:
            return CheckResult.warn(
                service_name,
                f"Unencrypted keyring backend: {backend}",
                "Credentials stored without encryption — consider gnome-keyring, "
                "'config credentials --use-file-store', or Vault for production",
            )

    # Check that required credentials exist (works for either backend)
    try:
        status = store.status()
    except Exception as e:
        return CheckResult.fail(
            service_name,
            f"Connection lost to {service_name}",
            str(e),
            group="keyring",
        )

    missing = []
    for key, exists in status.items():
        if exists:
            continue
        if key.required:
            missing.append(key.display_name)

    if missing:
        if store.is_vault:
            fix_msg = "Add missing secrets directly in Vault"
        else:
            fix_msg = "Run 'platform-atlas config credentials' to store credentials"

        return CheckResult.fail(
            service_name,
            f"Missing credentials: {', '.join(missing)}",
            fix_msg,
        )

    if file_store_active:
        return CheckResult.warn(
            service_name,
            f"All credentials available ({store.backend_name})",
            "Encrypted local file is the chosen backend — credentials are encrypted and "
            "machine-bound at ~/.atlas/credentials.enc. Run 'platform-atlas config credentials "
            "--use-keyring' to switch to the OS keyring.",
        )

    return CheckResult.ok(
        service_name,
        f"All credentials available ({store.backend_name})",
    )

def _check_node_control_master(target: dict, check_name: str) -> CheckResult:
    """Connectivity check for ControlMaster targets.

    Opens a brief ping through the user's pre-opened master socket and reports
    success/failure. Does not modify the master session — Atlas only multiplexes,
    it never opens or closes the master.
    """
    from platform_atlas.core.transport import (
        ControlMasterConfig, ControlMasterTransport,
    )
    from platform_atlas.core.exceptions import CollectorConnectionError

    socket_path = target.get("control_socket", "")
    ssh_target = target.get("ssh_target", "")

    if not socket_path:
        return CheckResult.fail(
            check_name,
            "ControlMaster socket path missing",
            details="Set 'ssh_control_socket' on this node",
            group="ssh",
        )
    if not ssh_target:
        return CheckResult.fail(
            check_name,
            "ControlMaster SSH destination missing",
            details="Set 'ssh_control_target' on this node",
            group="ssh",
        )

    port = target.get("port", 22)
    try:
        cfg = ControlMasterConfig(socket_path=socket_path, ssh_target=ssh_target, port=port)
    except ValueError as e:
        return CheckResult.fail(check_name, str(e), group="ssh")

    transport = ControlMasterTransport(cfg)
    try:
        transport.connect()
        return CheckResult.ok(
            check_name,
            f"Master socket reachable → {ssh_target}",
            details=f"socket: {socket_path}",
            group="ssh",
        )
    except CollectorConnectionError as e:
        return CheckResult.fail(
            check_name,
            e.message if hasattr(e, "message") else str(e),
            details=f"socket: {socket_path} — open the master before running Atlas",
            group="ssh",
        )
    except Exception as e:
        return CheckResult.fail(
            check_name, f"{type(e).__name__}: {e}", group="ssh",
        )
    finally:
        try:
            transport.close()
        except Exception:
            pass


def _check_node_ssh(target: dict, timeout: float = 5.0) -> CheckResult:
    """Connectivity check for a single target. Dispatches by transport.

    Despite the legacy name, this also handles ``local`` and ``kubernetes``
    (skipped) and ``control_master`` (delegated to the CM-specific helper).
    """
    import paramiko

    name = target.get("name", "unknown")
    host = target.get("host", "")
    port = target.get("port", 22)
    username = target.get("username", "atlas")
    key_path = target.get("key_path")
    # Expand a leading ~ so paramiko gets a real path (matches transport.py);
    # otherwise the literal "~/.ssh/..." fails with "No such file or directory".
    if key_path:
        key_path = str(Path(key_path).expanduser())
    key_passphrase = target.get("key_passphrase")
    transport_kind = target.get("transport", "ssh")
    check_name = f"SSH → {name}"

    if transport_kind == "local":
        return CheckResult.skip(
            check_name, "Local transport — SSH not required", group="ssh",
        )

    if transport_kind == "kubernetes":
        return CheckResult.skip(
            check_name, "Kubernetes transport — SSH not used", group="ssh",
        )

    if transport_kind == "control_master":
        return _check_node_control_master(target, check_name)

    if not host:
        return CheckResult.fail(
            check_name, "No host configured", group="ssh",
        )

    client = paramiko.SSHClient()
    # Preflight checks reachability only - no data is read or written
    # AutoAddPolicy is acceptable here since we're just testing connectivity
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # nosec B507

    try:
        try:
            client.load_system_host_keys()
        except Exception:
            pass

        connect_kwargs: dict = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": timeout,
            "banner_timeout": timeout,
        }

        if key_path:
            # Explicit key mode — use only this key, no agent fallback
            connect_kwargs["key_filename"] = key_path
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
            if key_passphrase:
                connect_kwargs["passphrase"] = key_passphrase
        else:
            # Agent mode — agent handles auth, no filesystem key discovery
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = False

        client.connect(**connect_kwargs)

        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return CheckResult.fail(
                check_name,
                "Connected but transport is not active",
                details=f"{username}@{host}:{port}",
                group="ssh",
            )

        return CheckResult.ok(
            check_name,
            f"Connected to {username}@{host}:{port}",
            group="ssh",
        )

    except paramiko.AuthenticationException as e:
        error_msg = str(e).lower()

        # Encrypted key shows up as auth failure
        if "encrypted" in error_msg or "passphrase" in error_msg:
            if key_passphrase:
                msg = "SSH key passphrase is incorrect"
                details = f"key: {key_path}"
            else:
                msg = "SSH key is encrypted — passphrase required"
                details = f"key: {key_path} — add 'ssh_key_passphrase' to config"
            return CheckResult.fail(check_name, msg, details=details, group="ssh")

        return CheckResult.fail(
            check_name,
            "Authentication failed",
            details=f"{username}@{host}:{port} — check SSH key or password",
            group="ssh",
        )
    except paramiko.ssh_exception.NoValidConnectionsError:
        return CheckResult.fail(
            check_name,
            "Connection refused",
            details=f"{host}:{port} — is SSH running on this host?",
            group="ssh",
        )
    except TimeoutError:
        return CheckResult.fail(
            check_name,
            f"Timed out after {timeout}s",
            details=f"{host}:{port} — host unreachable or firewalled",
            group="ssh",
        )
    except OSError as e:
        return CheckResult.fail(
            check_name,
            f"Network error: {e}",
            details=f"{host}:{port}",
            group="ssh",
        )
    except Exception as e:
        return CheckResult.fail(
            check_name,
            f"{type(e).__name__}: {e}",
            details=f"{username}@{host}:{port}",
            group="ssh",
        )
    finally:
        client.close()

# Collectors that operate over SSH to the target node.
# These must run per-node with the correct SSH transport.
_SSH_COLLECTORS: frozenset[str] = frozenset({"system", "filesystem", "gateway4"})

# Collectors that use their own protocol (pymongo, redis-py, OAuth/HTTP, ipsdk).
# These connect via URIs in the main config and only need to run once.
# `gateway4_api` is included so Standard-mode preflight can verify Gateway4 API
# reachability — it is the API counterpart to the SSH-based `gateway4` collector.
_CONNECTOR_COLLECTORS: frozenset[str] = frozenset({"mongo", "redis", "platform", "gateway4_api"})


def _filesystem_check_applies(role: str | None, modules: set[str]) -> bool:
    """Whether the generic "Config Files" check has anything to check on this node.

    An "iag" node is either Gateway4 or Gateway5 — only Gateway4 ships a
    properties.yml, so a Gateway5-only node (its own server config file, if
    any, is verified separately by Gateway5Collector.preflight()) has nothing
    for this check to look for. Unknown/empty modules (legacy config) keep
    the check, matching the existing "check everything" fallback.
    """
    if role == "iag" and modules and "gateway4" not in modules:
        return False
    return True


# Main entrypoint
def run_preflight(
    *,
    targets: list[dict] | None = None,
    quiet: bool = False,
) -> PreflightReport:
    """
    Run all preflight checks in three phases:

      1. SSH connectivity — can we reach each node?
      2. Node services    — can SSH-based collectors run on each node?
         (system, filesystem, gateway4)
      3. Connectors       — can URI-based collectors reach their services?
         (pymongo, redis-py, OAuth/HTTP)

    Tier-aware: Standard runs Phase 0 + connectors only (no SSH at all).
    SaaS runs the SSH phases against its gateway node(s), the gateway5
    file-source check, and the Gateway4 API connector — never Platform/
    Mongo/Redis/Kubernetes checks. Extended runs everything.

    Args:
        targets:  List of target dicts from the deployment topology.
        quiet:    Suppress console output.

    Returns:
        PreflightReport with all results.
    """
    from platform_atlas.core.transport import (
        LocalTransport, transport_from_config,
    )
    from platform_atlas.capture.modules_registry import build_preflight_checks

    console = Console(quiet=quiet)
    report = PreflightReport()

    # A single spinner carries progress across every phase, so the terminal
    # shows one live line while slow SSH/connector checks run and is left
    # clean for the final tree — no stacked "Phase N:" chatter. `status` is
    # None in quiet mode; every _tick() call then no-ops.
    status = _start_spinner(console, quiet)

    # -- Phase 0: Keyring check (credentials available?) --
    _tick(status, "Checking credential store…")

    keyring_result = _check_credential_backend()
    # The credential-store check returns most results with the default group="";
    # tag them "keyring" here so they always land in the Credential Store branch
    # (whose filter in _render_tree keys on group == "keyring").
    if keyring_result.group != "keyring":
        keyring_result = _dc_replace(keyring_result, group="keyring")
    report.results.append(keyring_result)

    if not keyring_result.passed:
        # No point continuing if we can't access credentials
        _stop_spinner(status)
        if not quiet:
            _print_report(console, report)
        return report

    # Tier-aware phase gating: Standard mode runs only connector preflights —
    # SSH (Phases 1, 2) and Kubernetes (Phase 2b) are skipped entirely.
    # SaaS runs the SSH phases (its gateway nodes) but never Kubernetes.
    from platform_atlas.core.context import ctx as _ctx
    is_standard = _ctx().is_standard
    is_saas = _ctx().is_saas

    # -- Phase 1: SSH node connectivity ------------------------------------
    ssh_healthy_targets: list[dict] = []

    if targets and not is_standard:
        ssh_targets = [
            t for t in targets
            if t.get("transport", "ssh") in ("ssh", "control_master")
        ]

        if ssh_targets:
            _tick(status, f"Node connectivity — testing SSH to {len(ssh_targets)} node(s)…")

            for target in ssh_targets:
                result = _check_node_ssh(target)
                report.results.append(result)
                logger.debug("SSH check %s: %s", target.get("name"), result.status.value)

                # Track which nodes we can actually reach for Phase 2
                if result.passed:
                    ssh_healthy_targets.append(target)

    # -- Phase 2: SSH-based collector checks per node ----------------------
    if ssh_healthy_targets:
        _tick(status, "Node services — probing collectors over SSH…")

        for target in ssh_healthy_targets:
            target_name = target.get("name", "unknown")
            target_modules = set(target.get("modules", []))

            # Determine which SSH-based collectors to check on this node.
            # If the target specifies modules, intersect with SSH collectors.
            # If no modules key (legacy config), check ALL SSH collectors.
            if target_modules:
                relevant = target_modules & _SSH_COLLECTORS
            else:
                relevant = set(_SSH_COLLECTORS)

            if "filesystem" in relevant and not _filesystem_check_applies(
                target.get("role"), target_modules
            ):
                relevant.discard("filesystem")

            if not relevant:
                continue

            # Build transport for this specific node
            try:
                transport = transport_from_config(target)
            except Exception as e:
                for module_key in relevant:
                    report.results.append(CheckResult.fail(
                        name=f"{module_key} → {target_name}",
                        message=f"Transport error: {e}",
                        group="node_services",
                    ))
                continue

            # Build checks using this node's transport
            try:
                all_checks = build_preflight_checks(
                    transport,
                    role=target.get("role"),
                    node_modules=frozenset(target_modules) if target_modules else None,
                )
            except Exception as e:
                for module_key in relevant:
                    report.results.append(CheckResult.fail(
                        name=f"{module_key} → {target_name}",
                        message=f"Build error: {e}",
                        group="node_services",
                    ))
                continue

            # IAG5 server-config-file nodes: read+parse gateway.conf over SSH and
            # surface the server-mode block here, in place of the printenv probe.
            gw5_conf_path = target.get("gateway5_conf_path", "")
            if gw5_conf_path:
                relevant.discard("gateway5")
                from platform_atlas.capture.collectors.gateway5 import Gateway5Collector
                try:
                    res = Gateway5Collector(
                        transport=transport, conf_path=gw5_conf_path,
                    ).preflight()
                    report.results.append(CheckResult(
                        name=f"gateway5 → {target_name}",
                        status=res.status, message=res.message,
                        details=res.details, group="node_services",
                    ))
                except Exception as e:
                    report.results.append(CheckResult.fail(
                        name=f"gateway5 → {target_name}",
                        message=f"{type(e).__name__}: {e}",
                        group="node_services",
                    ))

            for module_key in relevant:
                check_fn = all_checks.get(module_key)
                if check_fn is None:
                    continue

                check_label = f"{module_key} → {target_name}"
                try:
                    result = check_fn()
                    report.results.append(CheckResult(
                        name=check_label,
                        status=result.status,
                        message=result.message,
                        details=result.details,
                        group="node_services",
                    ))
                except Exception as e:
                    report.results.append(CheckResult.fail(
                        name=check_label,
                        message=f"{type(e).__name__}: {e}",
                        group="node_services",
                    ))

            # Clean up the SSH connection
            try:
                transport.close()
            except Exception:
                pass

    # Also run SSH-based checks locally if any local targets exist (non-Standard)
    if targets and not is_standard:
        local_targets = [t for t in targets if t.get("transport", "ssh") == "local"]
        for target in local_targets:
            target_name = target.get("name", "local")
            target_modules = set(target.get("modules", []))
            if target_modules:
                relevant = target_modules & _SSH_COLLECTORS
            else:
                relevant = set(_SSH_COLLECTORS)

            if "filesystem" in relevant and not _filesystem_check_applies(
                target.get("role"), target_modules
            ):
                relevant.discard("filesystem")

            if not relevant:
                continue

            local_transport = LocalTransport()
            all_checks = build_preflight_checks(
                local_transport,
                role=target.get("role"),
                node_modules=frozenset(target_modules) if target_modules else None,
            )

            for module_key in relevant:
                check_fn = all_checks.get(module_key)
                if check_fn is None:
                    continue

                check_label = f"{module_key} → {target_name}"
                try:
                    result = check_fn()
                    report.results.append(CheckResult(
                        name=check_label,
                        status=result.status,
                        message=result.message,
                        details=result.details,
                        group="node_services",
                    ))
                except Exception as e:
                    report.results.append(CheckResult.fail(
                        name=check_label,
                        message=f"{type(e).__name__}: {e}",
                        group="node_services",
                    ))

    # -- Phase 2b: Kubernetes preflight checks (Extended only — never SaaS) ---
    # The default primary (and default optional Gateway5) node share the
    # environment's global config fields — checked once, as before, to avoid
    # a duplicate identical row. An explicitly-namespaced additional target
    # (rare — see TargetNode's kubectl_namespace/context/kubeconfig/
    # values_yaml_path overrides) gets its own labeled check.
    if targets and not is_standard and not is_saas:
        k8s_targets = [t for t in targets if t.get("transport") == "kubernetes"]
        if k8s_targets:
            _tick(status, "Kubernetes — reading cluster configuration…")

            from platform_atlas.core.config import get_config
            from platform_atlas.capture.collectors.kubernetes import KubernetesCollector
            cfg = get_config()

            _k8s_override_fields = ("kubectl_namespace", "kubectl_context", "kubeconfig_path", "values_yaml_path")
            default_targets = [
                t for t in k8s_targets
                if not any(t.get(f) for f in _k8s_override_fields)
            ]
            _default_names = {t.get("name") for t in default_targets}
            extra_targets = [t for t in k8s_targets if t.get("name") not in _default_names]

            def _run_k8s_preflight(target: dict, check_label: str) -> None:
                try:
                    k8s_collector = KubernetesCollector(
                        values_yaml_path=target.get("values_yaml_path") or cfg.values_yaml_path,
                        kubectl_context=target.get("kubectl_context") or cfg.kubectl_context,
                        kubectl_namespace=target.get("kubectl_namespace") or cfg.kubectl_namespace,
                        kubeconfig_path=target.get("kubeconfig_path") or getattr(cfg, "kubeconfig_path", "") or "",
                        use_kubectl=cfg.use_kubectl,
                        kubectl_binary=getattr(cfg, "kubectl_binary_path", "") or "",
                    )
                    result = k8s_collector.preflight()
                    report.results.append(CheckResult(
                        name=check_label,
                        status=result.status,
                        message=result.message,
                        details=result.details,
                        group="kubernetes",
                    ))
                except Exception as e:
                    report.results.append(CheckResult.fail(
                        name=check_label,
                        message=f"K8s preflight error: {type(e).__name__}: {e}",
                        group="kubernetes",
                    ))

            # Default target(s) share config — one check total, same as before
            if default_targets:
                _run_k8s_preflight(default_targets[0], "Kubernetes")

            # Each explicitly-namespaced extra target gets its own check
            for target in extra_targets:
                _run_k8s_preflight(target, f"Kubernetes → {target.get('name', 'kubernetes')}")

    # -- Phase 2c: Gateway5 file-source preflight (Extended + SaaS) --------
    if targets and not is_standard:
        gw5_file_targets = [t for t in targets if t.get("transport") == "gateway5_file"]
        if gw5_file_targets:
            _tick(status, "Gateway5 — reading file source…")
            from platform_atlas.capture.collectors.gateway5 import Gateway5Collector
            for target in gw5_file_targets:
                target_name = target.get("name", "gateway5")
                try:
                    gw5 = Gateway5Collector(
                        source_path=target.get("gateway5_source_path", ""),
                    )
                    result = gw5.preflight()
                    report.results.append(CheckResult(
                        name=f"gateway5 → {target_name}",
                        status=result.status,
                        message=result.message,
                        details=result.details,
                        group="node_services",
                    ))
                except Exception as e:
                    report.results.append(CheckResult.fail(
                        name=f"gateway5 → {target_name}",
                        message=f"{type(e).__name__}: {e}",
                        group="node_services",
                    ))

    # -- Phase 3: Connector-based checks (run once) ------------------------
    connector_label = (
        "ipsdk" if is_saas else "pymongo, redis-py, OAuth"
    )
    _tick(status, f"Service connectors — {connector_label}…")

    # Build active module set from all targets
    all_active: set[str] = set()
    for t in (targets or []):
        all_active.update(t.get("modules", []))

    # A SaaS GW4 node lists the SSH module name ("gateway4"); the ipsdk API
    # reachability check rides along whenever the gateway is in scope —
    # the check itself reports SKIP when no gateway4_uri is configured.
    if is_saas and "gateway4" in all_active:
        all_active.add("gateway4_api")

    active_connectors = _CONNECTOR_COLLECTORS & all_active

    all_checks = build_preflight_checks(include=frozenset(active_connectors))

    for module_key in active_connectors:
        check_fn = all_checks.get(module_key)
        if check_fn is None:
            continue

        try:
            result = check_fn()
            report.results.append(CheckResult(
                name=result.name,
                status=result.status,
                message=result.message,
                details=result.details,
                group="connectors",
            ))
        except Exception as e:
            report.results.append(CheckResult.fail(
                name=module_key,
                message=f"{type(e).__name__}",
                details=str(e),
                group="connectors",
            ))

    _stop_spinner(status)
    if not quiet:
        _print_report(console, report)
    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

# Phase groups rendered as tree branches, in display order. The group key
# matches CheckResult.group; the label is the branch heading.
_PHASE_BRANCHES: list[tuple[str, str]] = [
    ("keyring", "Credential Store"),
    ("ssh", "Node Connectivity"),
    ("node_services", "Node Services"),
    ("kubernetes", "Kubernetes"),
    ("connectors", "Service Connectors"),
]

# Canonical glyph name + theme color per status, shared by leaves and
# per-branch tallies. The glyph itself is resolved through ``ui.glyph`` at
# render time so preflight draws from the CLI-wide vocabulary (and picks up
# ASCII fallbacks in plain mode).
_STATUS_GLYPH: dict[CheckStatus, tuple[str, str]] = {
    CheckStatus.PASS: ("success", theme.success),
    CheckStatus.FAIL: ("error", theme.error),
    CheckStatus.SKIP: ("skip", theme.text_dim),
    CheckStatus.WARN: ("warning", theme.warning),
}


def _start_spinner(console: Console, quiet: bool):
    """Start a single progress spinner, or return None in quiet mode."""
    if quiet:
        return None
    status = console.status(
        f"[{theme.primary}]Running preflight checks…[/{theme.primary}]",
        spinner="dots",
        spinner_style=theme.primary,
    )
    status.start()
    return status


def _tick(status, message: str) -> None:
    """Update the spinner label (no-op when quiet)."""
    if status is not None:
        status.update(f"[{theme.primary}]{message}[/{theme.primary}]")


def _stop_spinner(status) -> None:
    """Stop the spinner if one is running (no-op when quiet)."""
    if status is not None:
        status.stop()


def _phase_tally(results: list[CheckResult]) -> Text:
    """Compact per-branch count, e.g. '3✓ 1⚠ 1✗' in status colors."""
    tally = Text()
    for status in (CheckStatus.PASS, CheckStatus.WARN, CheckStatus.SKIP, CheckStatus.FAIL):
        count = sum(1 for r in results if r.status == status)
        if not count:
            continue
        name, color = _STATUS_GLYPH[status]
        if len(tally):
            tally.append(" ")
        tally.append(f"{count}{ui.glyph(name)}", style=color)
    return tally

def _print_report(console: Console, report: PreflightReport) -> None:
    """Render preflight results as a single tree, then a summary line."""
    _render_tree(console, report)
    _print_summary(console, report)


def _render_tree(console: Console, report: PreflightReport) -> None:
    """Render every check as one grouped tree — a branch per phase, a leaf
    per check, and an expanded ``↳`` detail line for failures and warnings.
    """
    try:
        from platform_atlas.core.context import ctx as _ctx
        env = _ctx().config.active_environment or "default"
    except Exception:  # pylint: disable=broad-except
        env = "default"

    root = Text()
    root.append("PREFLIGHT", style=f"bold {theme.primary}")
    root.append(f"  env {env}", style=theme.text_dim)
    tree = Tree(root, guide_style=theme.border_dim)

    # One uniform column across the whole tree: a dotted leader fills the gap
    # from each name to a shared column so every message lines up, the same
    # way `config doctor` aligns its rows.
    label_col = max((len(r.name) for r in report.results), default=0)

    for group, label in _PHASE_BRANCHES:
        rows = [r for r in report.results if r.group == group]
        if not rows:
            continue

        heading = Text()
        heading.append(f"{label}  ", style=f"bold {theme.text_primary}")
        heading.append_text(_phase_tally(rows))
        branch = tree.add(heading)

        for result in rows:
            name, color = _STATUS_GLYPH[result.status]
            leaf = Text()
            leaf.append(f"{ui.glyph(name)} ", style=color)
            leaf.append(result.name, style=theme.text_primary)
            if result.message:
                pad = max(1, label_col - len(result.name) + 1)
                leaf.append(f" {'·' * pad} ", style=theme.text_muted)
                leaf.append(result.message, style=theme.text_dim)
            node = branch.add(leaf)
            # Only failures/warnings expand their detail — passes stay quiet.
            if result.details and result.status in (CheckStatus.FAIL, CheckStatus.WARN):
                node.add(Text(f"↳ {result.details}", style=theme.text_muted))

    console.print()
    console.print(tree)
    console.print()


def _print_summary(console: Console, report: PreflightReport) -> None:
    """One-line verdict with per-status counts, plus next-step or fix hints."""
    summary = report.summary
    passed = summary[CheckStatus.PASS]
    warned = summary[CheckStatus.WARN]
    skipped = summary[CheckStatus.SKIP]
    failed = summary[CheckStatus.FAIL]

    line = Text()
    if report.all_passed:
        line.append(f"{ui.glyph('success')} Preflight complete", style=f"bold {theme.success}")
    else:
        line.append(f"{ui.glyph('error')} Preflight failed", style=f"bold {theme.error}")
    line.append("  —  ", style=theme.text_muted)
    line.append(f"{passed} passed", style=theme.success)
    line.append("  ·  ", style=theme.text_muted)
    line.append(f"{warned} warnings", style=theme.warning if warned else theme.text_dim)
    line.append("  ·  ", style=theme.text_muted)
    line.append(f"{skipped} skipped", style=theme.text_dim)
    line.append("  ·  ", style=theme.text_muted)
    line.append(f"{failed} failed", style=theme.error if failed else theme.text_dim)
    console.print(line)
    console.print()

    if report.all_passed:
        ui.next_step(
            "platform-atlas session run capture",
            label="Connectivity verified — start your capture",
        )
    else:
        # Actionable hints grouped by failure type
        failed_results = [r for r in report.results if r.status == CheckStatus.FAIL]
        ssh_failures = [r for r in failed_results if r.group == "ssh"]
        node_failures = [r for r in failed_results if r.group == "node_services"]
        svc_failures = [r for r in failed_results if r.group == "connectors"]

        if ssh_failures:
            # Separate ControlMaster socket failures from general SSH failures.
            # Socket failures are identifiable by "socket" appearing in the
            # message or details (set by _check_node_control_master).
            cm_failures = [r for r in ssh_failures if "socket" in (r.message + r.details).lower()]
            plain_ssh_failures = [r for r in ssh_failures if r not in cm_failures]

            if cm_failures:
                try:
                    from platform_atlas.core.context import ctx as _ctx
                    _env = _ctx().config.active_environment or "my-env"
                except Exception:  # pylint: disable=broad-except
                    _env = "my-env"
                console.print(f"  [{theme.primary_glow}]ControlMaster socket issue — manage with:[/{theme.primary_glow}]")
                console.print(f"  [{theme.text_dim}]  • [bold]platform-atlas env sockets {_env} --open[/bold]    open all sockets[/{theme.text_dim}]")
                console.print(f"  [{theme.text_dim}]  • [bold]platform-atlas env sockets {_env} --clean[/bold]   remove stale sockets (then re-open)[/{theme.text_dim}]")
                console.print(f"  [{theme.text_dim}]  • [bold]platform-atlas env sockets {_env}[/bold]           check current socket status[/{theme.text_dim}]")
                console.print()

            if plain_ssh_failures:
                console.print(f"  [{theme.text_dim}]SSH failures — verify:[/{theme.text_dim}]")
                console.print(f"  [{theme.text_dim}]  • Hosts are reachable (ping, telnet port 22)[/{theme.text_dim}]")
                console.print(f"  [{theme.text_dim}]  • SSH user and key are correct in your config[/{theme.text_dim}]")
                console.print(f"  [{theme.text_dim}]  • Target host keys are in known_hosts[/{theme.text_dim}]")
                console.print()

        if node_failures:
            console.print(f"  [{theme.text_dim}]Node service failures — verify:[/{theme.text_dim}]")
            console.print(f"  [{theme.text_dim}]  • Required files/services exist on the target node[/{theme.text_dim}]")
            console.print(f"  [{theme.text_dim}]  • SSH user has read permissions to config files[/{theme.text_dim}]")
            console.print()

        if svc_failures:
            console.print(f"  [{theme.text_dim}]Connector failures — verify:[/{theme.text_dim}]")
            console.print(f"  [{theme.text_dim}]  • URIs in config are correct (platform-atlas config show)[/{theme.text_dim}]")
            console.print(f"  [{theme.text_dim}]  • Services are running and accepting connections[/{theme.text_dim}]")
            console.print()
