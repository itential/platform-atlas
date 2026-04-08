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
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.console import Console

from platform_atlas.core.credentials import (
    credential_store,
    CredentialBackendType,
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

    if store.backend_type == CredentialBackendType.VAULT:
        service_name = "HashiCorp Vault"
        # VaultBackend validated the connection during __init__.
        # If we got here, the connection is good — just check for secrets.
    else:
        service_name = "OS Keyring"
        is_secure, backend = verify_keyring_backend()
        if not is_secure:
            return CheckResult.fail(
                service_name,
                f"Insecure backend: {backend}",
                "Install a secure keyring (macOS Keychain, Windows Credential Locker)",
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

    return CheckResult.ok(
        service_name,
        f"All credentials available ({store.backend_name})",
    )

def _check_node_ssh(target: dict, timeout: float = 5.0) -> CheckResult:
    """Attempt an SSH connection to a single target node."""
    import paramiko

    name = target.get("name", "unknown")
    host = target.get("host", "")
    port = target.get("port", 22)
    username = target.get("username", "atlas")
    key_path = target.get("key_path")
    key_passphrase = target.get("key_passphrase")
    check_name = f"SSH → {name}"

    if target.get("transport", "ssh") == "local":
        return CheckResult.skip(
            check_name, "Local transport — SSH not required", group="ssh",
        )

    if target.get("transport") == "kubernetes":
        return CheckResult.skip(
            check_name, "Kubernetes transport — SSH not used", group="ssh",
        )

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

# Collectors that use their own protocol (pymongo, redis-py, OAuth/HTTP).
# These connect via URIs in the main config and only need to run once.
_CONNECTOR_COLLECTORS: frozenset[str] = frozenset({"mongo", "redis", "platform"})


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

    # -- Phase 0: Keyring check (credentials available?) --
    if not quiet:
        console.print(f"  [{theme.text_dim}]Phase 0: Credential store...[/{theme.text_dim}]\n")

    keyring_result = _check_credential_backend()
    report.results.append(keyring_result)

    if not keyring_result.passed:
        # No point continuing if we can't access credentials
        if not quiet:
            _print_report(console, report)
        return report

    if not quiet:
        console.print(f"\n[bold {theme.primary}]Running preflight checks...[/bold {theme.primary}]\n")

    # -- Phase 1: SSH node connectivity ------------------------------------
    ssh_healthy_targets: list[dict] = []

    if targets:
        ssh_targets = [t for t in targets if t.get("transport", "ssh") == "ssh"]

        if ssh_targets:
            if not quiet:
                console.print(f"  [{theme.text_dim}]Phase 1: SSH connectivity to {len(ssh_targets)} node(s)...[/{theme.text_dim}]\n")

            for target in ssh_targets:
                result = _check_node_ssh(target)
                report.results.append(result)
                logger.debug("SSH check %s: %s", target.get("name"), result.status.value)

                # Track which nodes we can actually reach for Phase 2
                if result.passed:
                    ssh_healthy_targets.append(target)

    # -- Phase 2: SSH-based collector checks per node ----------------------
    if ssh_healthy_targets:
        if not quiet:
            console.print(f"\n  [{theme.text_dim}]Phase 2: Node services via SSH...[/{theme.text_dim}]\n")

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
                all_checks = build_preflight_checks(transport)
            except Exception as e:
                for module_key in relevant:
                    report.results.append(CheckResult.fail(
                        name=f"{module_key} → {target_name}",
                        message=f"Build error: {e}",
                        group="node_services",
                    ))
                continue

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

    # Also run SSH-based checks locally if any local targets exist
    if targets:
        local_targets = [t for t in targets if t.get("transport", "ssh") == "local"]
        for target in local_targets:
            target_name = target.get("name", "local")
            target_modules = set(target.get("modules", []))
            if target_modules:
                relevant = target_modules & _SSH_COLLECTORS
            else:
                relevant = set(_SSH_COLLECTORS)
            if not relevant:
                continue

            local_transport = LocalTransport()
            all_checks = build_preflight_checks(local_transport)

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

    # -- Phase 2b: Kubernetes preflight checks --------------------------------
    if targets:
        k8s_targets = [t for t in targets if t.get("transport") == "kubernetes"]
        if k8s_targets:
            if not quiet:
                console.print(f"\n  [{theme.text_dim}]Phase 2b: Kubernetes configuration...[/{theme.text_dim}]\n")

            from platform_atlas.core.config import get_config
            try:
                cfg = get_config()
                from platform_atlas.capture.collectors.kubernetes import KubernetesCollector
                k8s_collector = KubernetesCollector(
                    values_yaml_path=cfg.values_yaml_path,
                    kubectl_context=cfg.kubectl_context,
                    kubectl_namespace=cfg.kubectl_namespace,
                    use_kubectl=cfg.use_kubectl,
                )
                result = k8s_collector.preflight()
                report.results.append(CheckResult(
                    name=result.name,
                    status=result.status,
                    message=result.message,
                    details=result.details,
                    group="kubernetes",
                ))
            except Exception as e:
                report.results.append(CheckResult.fail(
                    name="Kubernetes",
                    message=f"K8s preflight error: {type(e).__name__}: {e}",
                    group="kubernetes",
                ))

    # -- Phase 3: Connector-based checks (run once) ------------------------
    if not quiet:
        console.print(f"\n  [{theme.text_dim}]Phase 3: Service connectors (pymongo, redis-py, OAuth)...[/{theme.text_dim}]\n")

    # Build active module set from all targets
    all_active: set[str] = set()
    for t in (targets or []):
        all_active.update(t.get("modules", []))

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

    if not quiet:
        _print_report(console, report)
    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _print_report(console: Console, report: PreflightReport) -> None:
    """Display preflight results grouped by phase"""

    keyring = [r for r in report.results if r.group == "keyring"]
    if keyring:
        _print_check_table(console, "Phase 0 · Credential Store", keyring)

    ssh = report.ssh_results
    if ssh:
        _print_check_table(console, "Phase 1 · Node Connectivity (SSH)", ssh)

    node_svc = [r for r in report.results if r.group == "node_services"]
    if node_svc:
        _print_check_table(console, "Phase 2 · Node Services (via SSH)", node_svc)

    connectors = [r for r in report.results if r.group == "connectors"]
    if connectors:
        _print_check_table(console, "Phase 3 · Service Connectors", connectors)

    # -- Summary -----------------------------------------------------------
    summary = report.summary

    if report.all_passed:
        console.print(
            f"[bold {theme.success}]✓ Preflight complete[/bold {theme.success}] — "
            f"{summary[CheckStatus.PASS]} passed, "
            f"{summary[CheckStatus.SKIP]} skipped, "
            f"{summary[CheckStatus.WARN]} warnings\n"
        )
    else:
        console.print(
            f"[bold {theme.error}]✘ Preflight failed[/bold {theme.error}] — "
            f"{summary[CheckStatus.FAIL]} failed, "
            f"{summary[CheckStatus.PASS]} passed\n"
        )

        # Actionable hints grouped by failure type
        failed = [r for r in report.results if r.status == CheckStatus.FAIL]
        ssh_failures = [r for r in failed if r.group == "ssh"]
        node_failures = [r for r in failed if r.group == "node_services"]
        svc_failures = [r for r in failed if r.group == "connectors"]

        if ssh_failures:
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


def _print_check_table(console: Console, title: str, results: list[CheckResult]) -> None:
    """Render a group of check results as a styled table"""
    has_details = any(r.details for r in results)

    table = Table(box=box.ROUNDED, show_header=True, title_style=f"bold {theme.primary}")
    table.add_column("Check", style="bold", min_width=24)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Message", min_width=24)
    if has_details:
        table.add_column("Details", style="dim", max_width=44, overflow="ellipsis")

    status_styles = {
        CheckStatus.PASS: f"[{theme.success}]✓ PASS[/{theme.success}]",
        CheckStatus.FAIL: f"[{theme.error}]✘ FAIL[/{theme.error}]",
        CheckStatus.SKIP: f"[{theme.text_dim}]⊘ SKIP[/{theme.text_dim}]",
        CheckStatus.WARN: f"[{theme.warning}]⚠ WARN[/{theme.warning}]",
    }

    for result in results:
        row = [
            result.name,
            status_styles[result.status],
            result.message,
        ]
        if has_details:
            row.append(result.details)
        table.add_row(*row)

    console.print(Panel(table, title=title, border_style=theme.border_primary, expand=False))
    console.print()
