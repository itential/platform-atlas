# pylint: disable=line-too-long
"""
Initial Setup Script for First-Time users to Atlas

The setup flow is split into two phases:
  1. Global Init — organization name, preferences, and settings that apply
     across all environments. Written to ~/.atlas/config.json.
  2. Environment Creation — connection details, topology, and credentials
     for one deployment target. Written to ~/.atlas/environments/<name>.json.

On first run, both phases execute back-to-back. Subsequent environments
can be added with ``platform-atlas env create``.
"""

import re
import logging
from typing import Any
from pathlib import Path

import questionary
from questionary import Style
from rich import box
from rich.rule import Rule
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Console, Group
from rich.align import Align

from platform_atlas.core.paths import ATLAS_HOME, ATLAS_CONFIG_FILE, ATLAS_ENVIRONMENTS_DIR
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.core.topology import (
    DeploymentMode, NodeRole, TargetNode, DeploymentTopology,
)
from platform_atlas.core.credentials import (
    credential_store,
    scoped_service_name,
    CredentialKey,
    CredentialStore,
    CredentialBackendType,
    VaultAuthMethod,
    VaultBackend,
    VaultConfig,
    verify_keyring_backend,
)
from platform_atlas.core.environment import (
    Environment,
    EnvironmentManager,
    get_environment_manager,
    validate_env_name,
)
from platform_atlas.core.exceptions import CredentialError
from platform_atlas.core import ui
from platform_atlas.core._version import __version__

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)

QSTYLE = Style(
    [
        ("qmark", f"fg:{theme.accent} bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", f"fg:{theme.success_glow} bold"),
        ("pointer", f"fg:{theme.accent} bold"),
        ("highlighted", f"fg:#000000 bg:{theme.primary} bold"),
        ("selected", "fg:#888888 bg:default"),
        ("instruction", f"fg:{theme.text_muted} italic"),
        ("text", "fg:#888888"),
        ("disabled", "fg:#555555 italic"),
    ]
)


# =================================================
# Shared helpers
# =================================================

def must(v: str, msg: str):
    """Used with the setup process"""
    return True if v.strip() else msg

def mask(s: str, keep: int = 4) -> str:
    """Mask function for redacting the client secret"""
    s = s.strip()
    if len(s) <= keep:
        return "•" * len(s)
    return ("•" * (len(s) - keep)) + s[-keep:]

def ask_text(label: str, instruction: str = "", uri: bool = False) -> str:
    """Used when asking user for text entry"""
    def _uri_check(v: str) -> bool:
        return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$", v.strip()))
    def _v(v: str):
        if not v.strip():
            return "Required"
        if uri and not _uri_check(v):
            return "Doesn't look like a URI (expected 'scheme://...')"
        return True
    return (questionary.text(label, instruction=instruction, validate=_v,
                             style=QSTYLE).ask() or "").strip()

def ask_text_optional(label: str, instruction: str = "") -> str:
    """Text prompt that allows empty input"""
    return (questionary.text(label, instruction=instruction,
                             style=QSTYLE).ask() or "").strip()

def ask_secret(label: str) -> str:
    """Used for asking user secret information to mask it"""
    return (questionary.password(label, validate=lambda v: must(v, "Required"),
                                 style=QSTYLE).ask() or "").strip()

def ask_uri_optional(label: str, instruction: str = "") -> str:
    """URI prompt that allows empty input, but validates format if something is entered."""
    def _v(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return True  # Empty is fine — it's optional
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$", v):
            return "Doesn't look like a URI (expected 'scheme://...')"
        return True

    return (questionary.text(label, instruction=instruction, validate=_v,
                             style=QSTYLE).ask() or "").strip()

def ask_vault_settings() -> VaultConfig:
    """Interactive wizard for HashiCorp Vault connection settings."""
    _section("Vault Configuration", "Connection details for your Vault server")

    url = ask_text("Vault URL", instruction="(e.g. https://vault.company.com:8200) ", uri=True)

    auth_method = questionary.select(
        "Authentication method",
        choices=[
            questionary.Choice("Token          — Use a Vault token directly", value="token"),
            questionary.Choice("AppRole        — Use role_id + secret_id", value="approle"),
        ],
        style=QSTYLE,
    ).ask()
    if auth_method is None:
        _bail()

    token = role_id = secret_id = None

    if auth_method == "token":
        token = ask_secret("Vault Token (hidden)")
    else:
        role_id = ask_secret("AppRole Role ID (hidden)")
        secret_id = ask_secret("AppRole Secret ID (hidden)")

    mount_point = ask_text_optional("KV v2 mount point", instruction="(default: secret) ") or "secret"
    secret_path = ask_text_optional("Secret path", instruction="(default: platform-atlas) ") or "platform-atlas"

    verify_ssl = questionary.confirm(
        "Verify Vault SSL certificate?",
        default=True,
        style=QSTYLE,
    ).ask()

    namespace = ask_text_optional("Vault namespace", instruction="(Enterprise only, leave blank if N/A) ")

    return VaultConfig(
        url=url,
        auth_method=VaultAuthMethod(auth_method),
        token=token,
        role_id=role_id,
        secret_id=secret_id,
        mount_point=mount_point,
        secret_path=secret_path,
        verify_ssl=bool(verify_ssl),
        namespace=namespace or None,
    )

def _bail(msg: str = "Canceled. No changes made.") -> None:
    """Print cancel message and exit"""
    console.print(f"\n[{theme.warning}]{msg}[/{theme.warning}]")
    raise SystemExit()

def _section(title: str, subtitle: str = "") -> None:
    """Print a styled section header"""
    header = f"[bold {theme.primary_glow}]{title}[/bold {theme.primary_glow}]"
    if subtitle:
        header += f"\n[{theme.text_dim}]{subtitle}[/{theme.text_dim}]"
    console.print()
    console.print(Panel(header, box=box.ROUNDED, border_style=theme.border_primary, expand=False))

def _hint(text: str) -> None:
    """Print a subtle hint line"""
    console.print(f"  [{theme.text_dim}]{text}[/{theme.text_dim}]")


# =================================================
# Deployment Topology Wizard
# =================================================

_MODE_CHOICES = [
    questionary.Choice(
        title="Standalone (All-in-One)  — IAP, Mongo, Redis on a single server",
        value="standalone_all",
    ),
    questionary.Choice(
        title="Standalone (Split)       — IAP, Mongo, Redis on separate servers",
        value="standalone_split",
    ),
    questionary.Choice(
        title="Highly Available (HA2)    — Redundant IAP, Mongo replica set, Redis sentinels",
        value="ha2",
    ),
    questionary.Choice(
        title="Custom                    — I'll define each node manually",
        value="custom",
    ),
]


def _ask_host(label: str, instruction: str = "") -> str:
    """Prompt for a hostname/IP with basic validation"""
    def _v(v: str):
        v = v.strip()
        if not v:
            return "Required — enter a hostname or IP address"
        if v.startswith("http"):
            return "Enter a hostname or IP, not a URL (e.g. 10.0.0.1 or iap-prod-01)"
        return True

    inst = instruction or "(hostname or IP) "
    return (questionary.text(label, instruction=inst, validate=_v,
                             style=QSTYLE).ask() or "").strip()


def _ask_ssh_user(default: str = "atlas") -> str:
    """Prompt for SSH username with a default"""
    result = questionary.text(
        "SSH username for these servers",
        instruction=f"(default: {default}) ",
        style=QSTYLE,
    ).ask()
    return (result or "").strip() or default

def _discover_ssh_keys(search_dir: Path | None = None) -> list[Path]:
    """Scan ~/.ssh/ for likely SSH key files"""
    search_dir = search_dir or Path.home() / ".ssh"
    if not search_dir.is_dir():
        return []

    skip_prefixes = ("known_", "authorized_", "config")
    skip_suffixes = (".pub", ".old", ".bak")

    return [
        f for f in sorted(search_dir.iterdir())
        if f.is_file()
        and not f.name.startswith(skip_prefixes)
        and not f.suffix in skip_suffixes
    ]

def _ask_ssh_key() -> str:
    """Prompt for an SSH key - offers discovered keys if available"""
    keys = _discover_ssh_keys()

    if keys:
        choices = [
            questionary.Choice(
                title=f"{k.name:24s}    ({k})",
                value=str(k),
            )
            for k in keys
        ]
        choices.append(questionary.Choice(
            title="Enter a path manually...",
            value="__manual__",
        ))
        choices.append(questionary.Choice(
            title="Skip - use ssh-agent instead",
            value="",
        ))

        result = questionary.select(
            "SSH private key",
            choices=choices,
            style=QSTYLE,
        ).ask()
        if result is None:
            _bail()
        if result != "__manual__":
            return result

    # Fallback: tab-completing path prompt
    result = questionary.path(
        "SSH private key path",
        default=str(Path.home() / ".ssh") + "/",
        only_directories=False,
        style=QSTYLE,
    ).ask()
    return (result or "").strip()


def _ask_ssh_key_passphrase(ssh_key: str) -> str:
    """Prompt for the SSH key passphrase when an explicit key is set"""
    if not ssh_key:
        return ""

    result = questionary.password(
        "SSH key passphrase",
        instruction="(leave blank if key is not encrypted) ",
        style=QSTYLE,
    ).ask()
    return (result or "").strip()

def _ask_ssh_port(default: int = 22) -> int:
    """Prompt for SSH port with a default"""
    result = questionary.text(
        "SSH port",
        instruction=f"(default: {default}) ",
        validate=lambda v: True if not v.strip() else (
            "Enter a valid port number (1-65535)"
            if not v.strip().isdigit() or not 1 <= int(v.strip()) <= 65535
            else True
        ),
        style=QSTYLE,
    ).ask()
    return int(result.strip()) if result and result.strip() else default

def _ask_ssh_discover_keys(ssh_key: str) -> bool:
    """Ask whether to auto-discover keys from ~/.ssh/ when no explicit key is set"""
    if ssh_key:
        return False

    result = questionary.confirm(
        "Search ~/.ssh/ for keys automatically?",
        instruction="(if no, only the ssh-agent will be used) ",
        default=False,
        style=QSTYLE,
    ).ask()
    return bool(result)

def _ask_ssh_host_key_policy() -> str:
    """Ask how to handle unknown SSH host keys"""
    result = questionary.select(
        "Unknown SSH host key handling",
        choices=[
            questionary.Choice(
                "Auto-add       - Trust on first connect (recommended)",
                value="auto_add",
            ),
            questionary.Choice(
                "Warn           - Connect but log a warning",
                value="warn",
            ),
            questionary.Choice(
                "Reject         - Fail if host not in known_hosts",
                value="reject",
            ),
        ],
        default="auto_add",
        style=QSTYLE,
    ).ask()
    return result or "auto_add"

def _ask_node_count(label: str, minimum: int, default: int) -> int:
    """Ask how many nodes of a type, with a minimum"""
    def _v(v: str):
        v = v.strip()
        if not v:
            return True  # will use default
        try:
            n = int(v)
        except ValueError:
            return "Enter a number"
        if n < minimum:
            return f"Minimum is {minimum}"
        return True

    result = questionary.text(
        label,
        instruction=f"(min: {minimum}, default: {default}) ",
        validate=_v,
        style=QSTYLE,
    ).ask()
    return int(result.strip()) if result and result.strip() else default


def _ask_hosts_for_role(role_label: str, count: int) -> list[str]:
    """Collect hostnames for N nodes of a given role"""
    hosts: list[str] = []
    for i in range(1, count + 1):
        host = _ask_host(f"  {role_label} #{i}")
        hosts.append(host)
    return hosts

def _ask_gateway_version() -> str | None:
    """Ask which Automation Gateway version is deployed"""
    add_gw = questionary.confirm(
        "Do you have any Automation Gateway (IAG) servers?",
        default=False,
        style=QSTYLE,
    ).ask()
    if not add_gw:
        return None

    version = questionary.select(
        "Which gateway version?",
        choices=[
            questionary.Choice("Gateway 4 (Python / venv-based)", value="gateway4"),
            questionary.Choice("Gateway 5 (Container / env-var-based)", value="gateway5"),
        ],
        style=QSTYLE,
    ).ask()
    return version

def _ask_gateway_nodes(common_ssh: dict) -> list[TargetNode]:
    """Ask about gateway servers and return configured TargetNodes"""
    gw_version = _ask_gateway_version()
    if gw_version is None:
        return []

    count = _ask_node_count("How many gateway servers", minimum=1, default=1)
    hosts = _ask_hosts_for_role("Gateway", count)

    gw_modules = ["system", gw_version, "filesystem"]
    nodes: list[TargetNode] = []
    for i, host in enumerate(hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.IAG,
            host=host,
            label=f"iag-{i:02d}",
            modules=gw_modules,
            **common_ssh,
        ))

    return nodes

def _ask_capture_scope() -> str:
    """Ask user to select capture scope for HA/multi-node deployments."""
    result = questionary.select(
        "Capture scope",
        choices=[
            questionary.Choice(
                "Primary only  — Connect to 1 node per role (recommended)",
                value="primary_only",
            ),
            questionary.Choice(
                "All nodes     — Connect to every node in the topology",
                value="all_nodes",
            ),
        ],
        default="primary_only",
        style=QSTYLE,
    ).ask()
    return result or "primary_only"


def _build_ssh_defaults(topology: DeploymentTopology) -> dict[str, Any]:
    """
    Extract shared SSH settings from the first node to store as
    ssh_defaults in the config. Nodes that differ will keep their
    own values via per-node overrides.
    """
    if not topology.nodes:
        return {}

    first = topology.nodes[0]
    defaults: dict[str, Any] = {"username": first.ssh_user}

    if first.ssh_key:
        defaults["key_path"] = first.ssh_key
    if first.ssh_key_passphrase:
        defaults["key_passphrase"] = first.ssh_key_passphrase
    if first.ssh_port != 22:
        defaults["port"] = first.ssh_port
    if first.ssh_discover_keys:
        defaults["discover_keys"] = True
    if first.ssh_host_key_policy != "auto_add":
        defaults["host_key_policy"] = first.ssh_host_key_policy

    return defaults


def _display_topology_review(
    topology: DeploymentTopology,
    capture_scope: str = "primary_only",
) -> None:
    """Display a pretty table of the configured topology"""
    table = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Node", style=f"bold {theme.text_primary}", min_width=16)
    table.add_column("Role", style=theme.accent, min_width=8)
    table.add_column("Host", style=theme.secondary, min_width=16)
    table.add_column("Primary", justify="center", min_width=8)
    table.add_column("Modules", style=theme.text_dim, min_width=24)

    for node in topology.nodes:
        is_captured = (
            capture_scope == "all_nodes"
            or node.primary
        )
        primary_badge = (
            f"[{theme.success}]✓[/{theme.success}]"
            if node.primary
            else f"[{theme.text_dim}]·[/{theme.text_dim}]"
        )
        modules_str = (
            ", ".join(node.effective_modules)
            if is_captured
            else f"[{theme.text_dim}]— (topology only)[/{theme.text_dim}]"
        )
        table.add_row(
            node.label,
            node.role.value.upper(),
            node.host,
            primary_badge,
            modules_str,
        )

    scope_label = (
        f"[{theme.info}]primary_only[/{theme.info}] — "
        "connecting to 1 node per role"
        if capture_scope == "primary_only"
        else f"[{theme.warning}]all_nodes[/{theme.warning}] — "
        "connecting to every node"
    )

    console.print(Panel(
        Group(
            Text(f" {topology.summary}\n", style=f"bold {theme.primary_glow}"),
            Text.from_markup(f" Capture scope: {scope_label}\n"),
            table,
        ),
        title="Deployment Topology",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))


def _wizard_standalone_all() -> DeploymentTopology:
    """Guide: single server with everything"""
    _hint("All services (IAP, MongoDB, Redis) on one server.")
    _hint("Atlas connects over SSH for system/config data, and uses")
    _hint("pymongo/redis-py/OAuth for service-specific collection.\n")

    host = _ask_host("Server hostname")
    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()

    common = {
        "ssh_user": ssh_user, "ssh_key": ssh_key,
        "ssh_key_passphrase": ssh_key_passphrase,
        "ssh_port": ssh_port, "ssh_discover_keys": ssh_discover_keys,
        "ssh_host_key_policy": ssh_host_key_policy,
    }

    # Ask which gateway version (if any) runs on the server
    gw_version = _ask_gateway_version()

    base_modules = ["system", "filesystem", "mongo", "redis", "platform"]
    if gw_version:
        base_modules.append(gw_version)


    nodes = [TargetNode(
        role=NodeRole.ALL, host=host,
        modules=base_modules, **common,
    )]

    return DeploymentTopology(mode=DeploymentMode.STANDALONE, nodes=nodes)


def _wizard_standalone_split() -> DeploymentTopology:
    """Guide: separate servers for IAP, Mongo, Redis"""
    _hint("IAP, MongoDB, and Redis each on their own server.")
    _hint("Enter the hostname or IP for each.\n")

    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {"ssh_user": ssh_user, "ssh_key": ssh_key,
              "ssh_key_passphrase": ssh_key_passphrase,
              "ssh_port": ssh_port,
              "ssh_discover_keys": ssh_discover_keys,
              "ssh_host_key_policy": ssh_host_key_policy}

    iap_host = _ask_host("IAP server")
    mongo_host = _ask_host("MongoDB server")
    redis_host = _ask_host("Redis server")

    nodes = [
        TargetNode(role=NodeRole.IAP, host=iap_host, **common),
        TargetNode(role=NodeRole.MONGO, host=mongo_host, **common),
        TargetNode(role=NodeRole.REDIS, host=redis_host, **common),
    ]

    gw_nodes = _ask_gateway_nodes(common)
    nodes.extend(gw_nodes)

    return DeploymentTopology(mode=DeploymentMode.STANDALONE, nodes=nodes)


def _wizard_ha2() -> DeploymentTopology:
    """Guide: HA2 multi-node deployment"""
    _hint("Highly Available architecture with redundant components.")
    _hint("Minimum: 2 IAP, 3 MongoDB (replica set), 3 Redis (sentinels).")
    _hint("MongoDB node count should be odd for healthy elections.")
    _hint("")
    _hint("Atlas connects to the PRIMARY node of each role for capture.")
    _hint("Non-primary nodes are recorded for topology validation.\n")

    # -- Shared SSH credentials (stored as ssh_defaults in config) -----------
    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {
        "ssh_user": ssh_user,
        "ssh_key": ssh_key,
        "ssh_key_passphrase": ssh_key_passphrase,
        "ssh_port": ssh_port,
        "ssh_discover_keys": ssh_discover_keys,
        "ssh_host_key_policy": ssh_host_key_policy,
    }

    # -- IAP nodes -----------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── IAP Servers ──[/{theme.primary_glow}]")
    _hint("First host listed is the primary (SSH + OAuth target)")
    iap_count = _ask_node_count("  How many IAP servers?", minimum=2, default=2)
    iap_hosts = _ask_hosts_for_role("IAP", iap_count)
    console.print()

    # -- MongoDB nodes -------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── MongoDB Replica Set ──[/{theme.primary_glow}]")
    _hint("First host listed is the primary (SSH + pymongo target)")
    mongo_count = _ask_node_count("  How many MongoDB servers?", minimum=3, default=3)
    if mongo_count % 2 == 0:
        console.print(
            f"  [{theme.warning}]⚠ Even number of Mongo nodes "
            f"— odd is recommended for elections[/{theme.warning}]"
        )
        keep_even = questionary.confirm(
            "  Continue with even count?", default=True, style=QSTYLE,
        ).ask()
        if not keep_even:
            mongo_count = _ask_node_count(
                "  How many MongoDB servers?", minimum=3, default=3,
            )
    mongo_hosts = _ask_hosts_for_role("MongoDB", mongo_count)
    console.print()

    # -- Redis nodes ---------------------------------------------------------
    console.print(f"  [{theme.primary_glow}]── Redis Sentinels ──[/{theme.primary_glow}]")
    _hint("First host listed is the primary (SSH + redis-py target)")
    redis_count = _ask_node_count("  How many Redis servers?", minimum=3, default=3)
    redis_hosts = _ask_hosts_for_role("Redis", redis_count)
    console.print()

    # -- Build topology nodes ------------------------------------------------
    nodes: list[TargetNode] = []

    for i, host in enumerate(iap_hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.IAP, host=host,
            label=f"iap-{i:02d}", **common,
        ))
    for i, host in enumerate(mongo_hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.MONGO, host=host,
            label=f"mongo-{i:02d}", **common,
        ))
    for i, host in enumerate(redis_hosts, 1):
        nodes.append(TargetNode(
            role=NodeRole.REDIS, host=host,
            label=f"redis-{i:02d}", **common,
        ))

    # -- Optional IAG --------------------------------------------------------
    gw_nodes = _ask_gateway_nodes(common)
    nodes.extend(gw_nodes)

    return DeploymentTopology(mode=DeploymentMode.HA2, nodes=nodes)


# -- Custom node roles available in the wizard ------------------------------

_CUSTOM_ROLE_CHOICES = [
    questionary.Choice("IAP          — Itential Automation Platform",  value="iap"),
    questionary.Choice("MongoDB      — Database server",               value="mongo"),
    questionary.Choice("Redis        — Cache / message broker",        value="redis"),
    questionary.Choice("IAG          — Itential Automation Gateway",   value="iag"),
    questionary.Choice("All-in-One   — IAP + Mongo + Redis on one box", value="all"),
    questionary.Choice("Custom       — I'll pick the modules myself",  value="custom"),
]

_ALL_MODULES = [
    questionary.Choice("system       — CPU, memory, disk, network",        value="system"),
    questionary.Choice("mongo        — MongoDB status via pymongo",        value="mongo"),
    questionary.Choice("redis        — Redis INFO via redis-py",           value="redis"),
    questionary.Choice("platform     — Platform API health via OAuth",     value="platform"),
    questionary.Choice("filesystem   — Config file collection via SSH",    value="filesystem"),
    questionary.Choice("gateway4     — Gateway4 packages via SSH",      value="gateway4"),
    questionary.Choice("gateway5     - Gateway5 env vars via SSH",         value="gateway5"),
]


def _wizard_custom() -> DeploymentTopology:
    """Guide: free-form node definition"""
    _hint("Define each node individually with its role and modules.")
    _hint("Add as many nodes as you need.\n")

    ssh_user = _ask_ssh_user()
    ssh_key = _ask_ssh_key()
    ssh_key_passphrase = _ask_ssh_key_passphrase(ssh_key)
    ssh_port = _ask_ssh_port()
    ssh_discover_keys = _ask_ssh_discover_keys(ssh_key)
    ssh_host_key_policy = _ask_ssh_host_key_policy()
    console.print()

    common = {"ssh_user": ssh_user, "ssh_key": ssh_key,
              "ssh_key_passphrase": ssh_key_passphrase,
              "ssh_port": ssh_port,
              "ssh_discover_keys": ssh_discover_keys,
              "ssh_host_key_policy": ssh_host_key_policy}
    nodes: list[TargetNode] = []

    while True:
        node_num = len(nodes) + 1
        console.print(f"  [{theme.primary_glow}]── Node #{node_num} ──[/{theme.primary_glow}]")

        host = _ask_host(f"  Hostname")

        role_val = questionary.select(
            f"  Role for {host}",
            choices=_CUSTOM_ROLE_CHOICES,
            style=QSTYLE,
        ).ask()
        if role_val is None:
            _bail()

        role = NodeRole(role_val)

        # For custom role, let them pick modules
        modules = None
        if role == NodeRole.CUSTOM:
            selected = questionary.checkbox(
                "  Select modules to run on this node",
                choices=_ALL_MODULES,
                style=QSTYLE,
            ).ask()
            if selected is None:
                _bail()
            modules = selected

        label = ask_text_optional(f"  Label", instruction=f"(default: {role.value}-{host}) ")

        nodes.append(TargetNode(
            role=role, host=host, label=label,
            modules=modules, **common,
        ))

        console.print()
        add_more = questionary.confirm(
            "Add another node?",
            default=True if node_num < 3 else False,
            style=QSTYLE,
        ).ask()
        if not add_more:
            break

    if not nodes:
        _bail("No nodes defined.")

    return DeploymentTopology(mode=DeploymentMode.CUSTOM, nodes=nodes)


def _wizard_kubernetes() -> tuple[DeploymentTopology, dict[str, Any]]:
    """
    Guide: Kubernetes deployment.

    Asks for Helm values.yaml path and optional kubectl configuration.
    Returns both the topology and K8s-specific metadata (values paths,
    kubectl settings) that get stored in the environment file.
    """
    _hint("Kubernetes deployment — no SSH required.")
    _hint("Atlas will collect system/config data from your Helm values file(s)")
    _hint("and use protocol collectors (OAuth, pymongo, redis-py) for live data.\n")

    k8s_meta: dict[str, Any] = {}

    # ── Data source selection ─────────────────────────────────────
    source_choice = questionary.select(
        "How should Atlas collect configuration data?",
        choices=[
            questionary.Choice(
                "Values file    — Provide a Helm values.yaml file",
                value="values",
            ),
            questionary.Choice(
                "kubectl        — Read from the live cluster (requires kubectl access)",
                value="kubectl",
            ),
            questionary.Choice(
                "Both           — kubectl primary, values.yaml as reference",
                value="both",
            ),
        ],
        style=QSTYLE,
    ).ask()
    if source_choice is None:
        _bail()

    # ── Values file path ──────────────────────────────────────────
    if source_choice in ("values", "both"):
        console.print()
        _hint("Provide the path to your IAP Helm chart values.yaml")
        _hint("This is the file used with 'helm install -f values.yaml'\n")

        values_path = questionary.path(
            "IAP values.yaml path",
            only_directories=False,
            validate=lambda v: (
                True if v.strip() and Path(v.strip()).expanduser().is_file()
                else "File not found — enter the full path to your values.yaml"
            ),
            style=QSTYLE,
        ).ask()
        if values_path is None:
            _bail()
        k8s_meta["values_yaml_path"] = str(Path(values_path.strip()).expanduser().resolve())
    else:
        k8s_meta["values_yaml_path"] = ""

    # ── kubectl configuration ─────────────────────────────────────
    if source_choice in ("kubectl", "both"):
        console.print()
        _hint("Configure kubectl access to the cluster")

        kubectl_context = ask_text_optional(
            "kubectl context",
            "(leave blank for current context) ",
        )
        kubectl_namespace = ask_text_optional(
            "Kubernetes namespace",
            "(e.g. itential, default) ",
        )
        k8s_meta["use_kubectl"] = True
        k8s_meta["kubectl_context"] = kubectl_context
        k8s_meta["kubectl_namespace"] = kubectl_namespace
    else:
        k8s_meta["use_kubectl"] = False
        k8s_meta["kubectl_context"] = ""
        k8s_meta["kubectl_namespace"] = ""

    # ── Gateway5 (IAG5) support ───────────────────────────────────
    console.print()
    has_gw5 = questionary.confirm(
        "Do you have IAG5 (Automation Gateway 5) in this deployment?",
        default=False,
        style=QSTYLE,
    ).ask()
    if has_gw5 is None:
        _bail()

    if has_gw5 and source_choice in ("values", "both"):
        iag5_same_file = questionary.confirm(
            "Is IAG5 configured in the same values.yaml file?",
            default=False,
            style=QSTYLE,
        ).ask()

        if not iag5_same_file:
            iag5_path = questionary.path(
                "IAG5 values.yaml path",
                only_directories=False,
                validate=lambda v: (
                    True if v.strip() and Path(v.strip()).expanduser().is_file()
                    else "File not found"
                ),
                style=QSTYLE,
            ).ask()
            if iag5_path is None:
                _bail()
            k8s_meta["iag5_values_yaml_path"] = str(
                Path(iag5_path.strip()).expanduser().resolve()
            )
        else:
            k8s_meta["iag5_values_yaml_path"] = ""
    else:
        k8s_meta["iag5_values_yaml_path"] = ""

    # Build K8s topology
    topology = DeploymentTopology.kubernetes(has_gateway5=bool(has_gw5))

    return topology, k8s_meta


def _display_kubernetes_review(
    topology: DeploymentTopology,
    k8s_meta: dict[str, Any],
) -> None:
    """Display a summary of the Kubernetes deployment configuration."""
    table = Table(
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Setting", style=f"bold {theme.text_primary}", min_width=24)
    table.add_column("Value", style=theme.text_secondary, min_width=40)

    table.add_row("Deployment mode", f"[bold {theme.accent}]KUBERNETES[/bold {theme.accent}]")

    if k8s_meta.get("values_yaml_path"):
        table.add_row("IAP values.yaml", k8s_meta["values_yaml_path"])
    if k8s_meta.get("iag5_values_yaml_path"):
        table.add_row("IAG5 values.yaml", k8s_meta["iag5_values_yaml_path"])

    kubectl_status = (
        f"Enabled (context: {k8s_meta.get('kubectl_context') or 'current'}, "
        f"namespace: {k8s_meta.get('kubectl_namespace') or 'default'})"
        if k8s_meta.get("use_kubectl")
        else f"[{theme.text_dim}]Disabled[/{theme.text_dim}]"
    )
    table.add_row("kubectl", kubectl_status)

    # Show which modules will run
    all_modules: list[str] = []
    for node in topology.nodes:
        all_modules.extend(node.effective_modules)
    modules_str = ", ".join(sorted(set(all_modules)))
    table.add_row("Collectors", modules_str)

    console.print(Panel(
        Group(
            Text(f" {topology.summary}\n", style=f"bold {theme.primary_glow}"),
            table,
        ),
        title="Kubernetes Deployment",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))


def ask_deployment() -> tuple[dict, dict[str, Any]]:
    """
    Run the deployment topology wizard and return a serialized dict
    ready to embed in the config file.

    Returns:
        (deployment_dict, k8s_meta) — k8s_meta is empty for non-K8s deployments.
    """
    _section(
        "Deployment Topology",
        "How is your Itential environment set up?",
    )

    # ── Kubernetes check first ────────────────────────────────────
    is_k8s = questionary.confirm(
        "Is this environment running in Kubernetes?",
        default=False,
        style=QSTYLE,
    ).ask()
    if is_k8s is None:
        _bail()

    if is_k8s:
        console.print()
        topology, k8s_meta = _wizard_kubernetes()

        console.print()
        _display_kubernetes_review(topology, k8s_meta)

        if not questionary.confirm("Does this look right?", default=True, style=QSTYLE).ask():
            retry = questionary.confirm("Start deployment setup over?", default=True, style=QSTYLE).ask()
            if retry:
                return ask_deployment()
            _bail()

        result = topology.to_dict()
        result["capture_scope"] = "primary_only"
        return result, k8s_meta

    # ── Standard (non-K8s) deployment ─────────────────────────────
    mode = questionary.select(
        "Select your deployment architecture",
        choices=_MODE_CHOICES,
        style=QSTYLE,
    ).ask()
    if mode is None:
        _bail()

    console.print()

    wizards = {
        "standalone_all":   _wizard_standalone_all,
        "standalone_split": _wizard_standalone_split,
        "ha2":              _wizard_ha2,
        "custom":           _wizard_custom,
    }
    topology = wizards[mode]()

    # Ask about capture scope for multi-node deployments
    if topology.mode in (DeploymentMode.HA2, DeploymentMode.CUSTOM):
        console.print()
        capture_scope = _ask_capture_scope()
    else:
        capture_scope = "primary_only"

    # Review
    console.print()
    _display_topology_review(topology, capture_scope=capture_scope)

    if not questionary.confirm("Does this look right?", default=True, style=QSTYLE).ask():
        retry = questionary.confirm("Start deployment setup over?", default=True, style=QSTYLE).ask()
        if retry:
            return ask_deployment()
        _bail()

    # Build the deployment dict with the new structure
    result = topology.to_dict()
    result["capture_scope"] = capture_scope
    result["ssh_defaults"] = _build_ssh_defaults(topology)

    return result, {}


# =================================================
# Environment Name Prompt
# =================================================

def _ask_env_name(default: str = "") -> str:
    """Prompt for an environment name with validation."""
    def _v(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return "Required"
        if not validate_env_name(v):
            return "Alphanumeric, hyphens, underscores only (1-64 chars, can't start/end with hyphen)"
        mgr = get_environment_manager()
        if mgr.exists(v):
            return f"Environment '{v}' already exists"
        return True

    result = questionary.text(
        "Environment name",
        instruction="(e.g. production, staging, dev) ",
        default=default,
        validate=_v,
        style=QSTYLE,
    ).ask()
    if result is None:
        _bail()
    return result.strip()


# =================================================
# Environment Creation Wizard
# =================================================

def create_environment_wizard(
    env_name: str | None = None,
    from_env: str | None = None,
) -> Environment | None:
    """
    Interactive wizard to create a new environment.

    This collects all the deployment-specific configuration:
      - Environment name and description
      - Platform URI and client ID
      - Credential backend choice + secrets
      - Deployment topology

    Returns the created Environment, or None if canceled.
    """
    _section(
        "Create Environment",
        "Configure a new deployment target",
    )

    mgr = get_environment_manager()

    # -- Copy from existing environment if --from was specified ----------------
    if from_env:
        if not mgr.exists(from_env):
            console.print(f"  [{theme.error}]Source environment '{from_env}' not found[/{theme.error}]")
            return None
        source = mgr.load(from_env)
        console.print(f"  [{theme.text_dim}]Copying from: {from_env}[/{theme.text_dim}]")
    else:
        source = None

    # -- Environment name -----------------------------------------------------
    if env_name is None:
        env_name = _ask_env_name()
    elif mgr.exists(env_name):
        console.print(f"  [{theme.error}]Environment '{env_name}' already exists[/{theme.error}]")
        return None

    description = ask_text_optional("Description", "(optional, e.g. 'Production US East') ")

    # -- Organization name ---------------------------------------------------
    # Try to default from global config if it exists
    default_org = ""
    try:
        if ATLAS_CONFIG_FILE.is_file():
            import json as _json
            with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as _f:
                _cfg = _json.load(_f)
            default_org = _cfg.get("organization_name", "")
    except Exception:
        pass

    org_name_input = ask_text_optional(
        "Organization Name",
        f"(e.g. 'Acme Corp'){' [default: ' + default_org + ']' if default_org else ''} ",
    )
    org_name = org_name_input or default_org

    # If copying, just save with new name and let user tweak later
    if source:
        new_env = Environment.from_dict(source.to_dict())
        new_env.name = env_name
        new_env.description = description or source.description
        new_env.organization_name = org_name or source.organization_name
        mgr.save(new_env)
        console.print(f"\n  [{theme.success}]✓ Environment '{env_name}' created (copied from {from_env})[/{theme.success}]")
        return new_env

    # -- Legacy profile (IAP 2023.x) ------------------------------------------
    is_legacy = questionary.confirm(
        "Is this a 2023.x environment?",
        default=False,
        style=QSTYLE,
    ).ask()
    if is_legacy is None:
        raise KeyboardInterrupt

    legacy_profile: str | None = None
    if is_legacy:
        legacy_profile = questionary.text(
            "What is the profile name that you're using in IAP 2023.x?",
            validate=lambda v: bool(v.strip()) or "Profile name cannot be empty",
            style=QSTYLE,
        ).ask()
        if legacy_profile is None:
            raise KeyboardInterrupt
        legacy_profile = legacy_profile.strip()

    # -- Verify keyring backend -----------------------------------------------
    is_secure, backend = verify_keyring_backend()
    if not is_secure:
        console.print(Panel(
            f"[bold {theme.error}]Insecure keyring backend detected: {backend}[/bold {theme.error}]\n\n"
            f"[{theme.text_primary}]Platform Atlas requires a secure OS credential store.\n"
            f"  • macOS: Keychain (built-in)\n"
            f"  • Windows: Credential Locker (built-in)\n"
            f"  • Linux: Install gnome-keyring + secretstorage + python3-dbus[/{theme.text_primary}]",
            border_style=theme.error,
            box=box.ROUNDED,
            expand=False,
        ))
        raise SystemExit(1)

    console.print(f"  [{theme.success}]✓ Credential store: {backend}[/{theme.success}]")
    console.print()

    # -- Credential Backend Selection -----------------------------------------
    _section("Credential Backend", "Where should Atlas retrieve secrets for this environment?")

    backend_choice = questionary.select(
        "Select credential backend",
        choices=[
            questionary.Choice(
                "OS Keyring  — Store credentials locally (macOS/Windows/Linux)",
                value="keyring",
            ),
            questionary.Choice(
                "Vault       — Read credentials from HashiCorp Vault (read-only)",
                value="vault",
            ),
        ],
        style=QSTYLE,
    ).ask()
    if backend_choice is None:
        _bail()

    # -- Connection Details ---------------------------------------------------
    _section("Connection Details", "Service URIs and credentials")

    platform_uri = ask_text("Platform URI", "(Example: https://localhost:3443) ", uri=True)
    platform_client_id = ask_text("Platform Client ID")

    # -- Vault-specific setup -------------------------------------------------
    vault_config: VaultConfig | None = None
    mongo_uri = redis_uri = platform_client_secret = None

    # Scoped keyring service for this environment's credentials
    scoped = scoped_service_name(env_name)

    if backend_choice == "vault":
        vault_config = ask_vault_settings()

        # Save Vault connection settings to the environment's keyring namespace
        VaultBackend.save_config_to_keyring(vault_config, service=scoped)
        console.print(f"  [{theme.success}]✓ Vault connection settings saved to OS keyring[/{theme.success}]")

        # Test the connection
        console.print(f"  [{theme.text_dim}]Testing Vault connection...[/{theme.text_dim}]")
        try:
            test_backend = VaultBackend(vault_config, service=scoped)
            console.print(f"  [{theme.success}]✓ Connected to Vault at {vault_config.url}[/{theme.success}]")
        except CredentialError as e:
            console.print(f"  [{theme.error}]✘ Vault connection failed: {e}[/{theme.error}]")
            retry = questionary.confirm("Retry Vault configuration?", default=True, style=QSTYLE).ask()
            if retry:
                vault_config = ask_vault_settings()
                VaultBackend.save_config_to_keyring(vault_config, service=scoped)
                test_backend = VaultBackend(vault_config, service=scoped)
                console.print(f"  [{theme.success}]✓ Connected to Vault[/{theme.success}]")
            else:
                _bail("Cannot continue without a working Vault connection.")

        # Show expected Vault keys and verify
        console.print()
        console.print(Panel(
            f"[bold {theme.primary_glow}]Expected Vault Secret Layout[/bold {theme.primary_glow}]\n\n"
            f"[{theme.text_primary}]Atlas expects the following keys at "
            f"[bold]{vault_config.mount_point}/{vault_config.secret_path}[/bold]:[/{theme.text_primary}]\n\n"
            f"  [{theme.accent}]platform_client_secret[/{theme.accent}]"
            f"  [{theme.text_dim}]— Platform OAuth client secret[/{theme.text_dim}]\n"
            f"  [{theme.accent}]mongo_uri[/{theme.accent}]"
            f"              [{theme.text_dim}]— Full MongoDB connection URI[/{theme.text_dim}]\n"
            f"  [{theme.accent}]redis_uri[/{theme.accent}]"
            f"              [{theme.text_dim}]— Full Redis connection URI[/{theme.text_dim}]\n"
            f"  [{theme.accent}]ssh_key_passphrase[/{theme.accent}]"
            f"     [{theme.text_dim}]— SSH key passphrase (optional)[/{theme.text_dim}]\n\n"
            f"[{theme.text_dim}]Example:[/{theme.text_dim}]\n"
            f"  [{theme.text_muted}]vault kv put {vault_config.mount_point}/{vault_config.secret_path} \\\n"
            f"    platform_client_secret=\"...\" \\\n"
            f"    mongo_uri=\"mongodb://user:pass@host:27017\" \\\n"
            f"    redis_uri=\"redis://user:pass@host:6379\" \\\n"
            f"    ssh_key_passphrase=\"...\"[/{theme.text_muted}]",
            box=box.ROUNDED,
            border_style=theme.border_primary,
            expand=False,
        ))

        # Retry loop for secret verification
        while True:
            console.print(f"\n  [{theme.text_dim}]Checking Vault for required secrets...[/{theme.text_dim}]")

            status_lines: list[str] = []
            missing_keys: list[CredentialKey] = []

            for key in CredentialKey:
                found = test_backend.exists(key.value)
                if found:
                    status_lines.append(
                        f"    [{theme.success}]✓[/{theme.success}] {key.display_name} ({key.value})"
                    )
                else:
                    missing_keys.append(key)
                    status_lines.append(
                        f"    [{theme.error}]✘[/{theme.error}] {key.display_name} ({key.value})"
                    )

            for line in status_lines:
                console.print(line)

            if not missing_keys:
                console.print(f"\n  [{theme.success}]✓ All required secrets found in Vault[/{theme.success}]")
                break

            console.print(
                f"\n  [{theme.warning}]⚠ {len(missing_keys)} credential(s) not found in Vault[/{theme.warning}]"
            )

            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice(
                        "Retry          — Check Vault again (after adding secrets)",
                        value="retry",
                    ),
                    questionary.Choice(
                        "Continue       — Finish setup now, add secrets to Vault later",
                        value="continue",
                    ),
                    questionary.Choice(
                        "Reconfigure    — Re-enter Vault connection settings",
                        value="reconfigure",
                    ),
                    questionary.Choice(
                        "Cancel         — Abort setup",
                        value="cancel",
                    ),
                ],
                style=QSTYLE,
            ).ask()

            if action is None or action == "cancel":
                _bail()
            elif action == "continue":
                console.print(
                    f"  [{theme.text_dim}]Continuing — add missing secrets to Vault "
                    f"before running a capture.[/{theme.text_dim}]"
                )
                break
            elif action == "reconfigure":
                vault_config = ask_vault_settings()
                VaultBackend.save_config_to_keyring(vault_config, service=scoped)
                try:
                    test_backend = VaultBackend(vault_config, service=scoped)
                    console.print(f"  [{theme.success}]✓ Connected to Vault at {vault_config.url}[/{theme.success}]")
                except CredentialError as e:
                    console.print(f"  [{theme.error}]✘ Connection failed: {e}[/{theme.error}]")
                    continue
            # else "retry" — just loops back to the top

        # No local secret prompts — they live in Vault
        mongo_uri = redis_uri = platform_client_secret = None

    # -- Keyring path: prompt for each secret locally -------------------------
    else:
        _hint("MongoDB and Redis URIs are optional — skip if not needed for your deployment")
        mongo_uri = ask_uri_optional("MongoDB URI", "(leave blank to skip) ")
        redis_uri = ask_uri_optional("Redis URI", "(leave blank to skip) ")
        platform_client_secret = ask_secret("Platform Client Secret (hidden)")

    # -- Deployment Topology --------------------------------------------------
    deployment, k8s_meta = ask_deployment()

    # -- Gateway4 API Credentials (if gateway4 is in the topology) ----------
    # Gateway4 is not supported in Kubernetes mode
    gateway4_uri = ""
    gateway4_username = ""
    gateway4_password = ""
    _is_kubernetes = deployment.get("mode") == "kubernetes"

    if not _is_kubernetes:
        _has_gateway4 = any(
            "gateway4" in node.get("modules", [])
            for node in deployment.get("nodes", [])
        )
        if _has_gateway4:
            _section("Gateway4 API", "Direct API connection for config collection (primary source)")
            _hint("Atlas connects to Gateway4's REST API to collect runtime configuration.")
            _hint("This is the primary method — SSH config file collection is the fallback.\n")

            gateway4_uri = ask_text(
                "Gateway4 API URI",
                "(Example: http://gateway-host:8083) ",
                uri=True,
            )
            gateway4_username = ask_text(
                "Gateway4 Username",
                "(default: admin@itential) ",
            )
            if not gateway4_username:
                gateway4_username = "admin@itential"

            if backend_choice == "keyring":
                gateway4_password = ask_secret("Gateway4 Password (hidden)")
            else:
                _hint("Gateway4 password must be stored in Vault as 'gateway4_password'")

    # -- Review ---------------------------------------------------------------
    _section("Review", "Everything we've collected for this environment")

    creds_table = Table(show_header=False, box=box.SIMPLE_HEAVY, pad_edge=True)
    creds_table.add_column("Field", style=f"bold {theme.text_primary}", min_width=24)
    creds_table.add_column("Value", style=theme.text_secondary)
    creds_table.add_row("environment", env_name)
    creds_table.add_row("organization", org_name or f"[{theme.text_dim}]—[/{theme.text_dim}]")
    creds_table.add_row("description", description or f"[{theme.text_dim}]—[/{theme.text_dim}]")
    creds_table.add_row("credential_backend", backend_choice)

    if backend_choice == "vault":
        creds_table.add_row("vault_url", vault_config.url)
        creds_table.add_row("vault_auth", vault_config.auth_method.value)
        creds_table.add_row("vault_path", f"{vault_config.mount_point}/{vault_config.secret_path}")
    else:
        creds_table.add_row("mongo_uri", mask(mongo_uri, keep=20) if mongo_uri else f"[{theme.text_dim}]— skipped[/{theme.text_dim}]")
        creds_table.add_row("redis_uri", mask(redis_uri, keep=20) if redis_uri else f"[{theme.text_dim}]— skipped[/{theme.text_dim}]")
        creds_table.add_row("platform_client_secret", mask(platform_client_secret))

    creds_table.add_row("platform_uri", platform_uri)
    creds_table.add_row("platform_client_id", platform_client_id)
    if legacy_profile:
        creds_table.add_row("legacy_profile", legacy_profile)
    if gateway4_uri:
        creds_table.add_row("gateway4_uri", gateway4_uri)
        creds_table.add_row("gateway4_username", gateway4_username)
    if k8s_meta.get("values_yaml_path"):
        creds_table.add_row("values_yaml", k8s_meta["values_yaml_path"])
    if k8s_meta.get("iag5_values_yaml_path"):
        creds_table.add_row("iag5_values_yaml", k8s_meta["iag5_values_yaml_path"])
    if k8s_meta.get("use_kubectl"):
        ctx_label = k8s_meta.get("kubectl_context") or "current"
        ns_label = k8s_meta.get("kubectl_namespace") or "default"
        creds_table.add_row("kubectl", f"{ctx_label} / {ns_label}")

    console.print(Panel(
        creds_table,
        title="Connection Details",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))

    # Re-display topology summary
    topology = DeploymentTopology.from_dict(deployment)
    if _is_kubernetes:
        _display_kubernetes_review(topology, k8s_meta)
    else:
        scope = deployment.get("capture_scope", "primary_only")
        _display_topology_review(topology, capture_scope=scope)

    env_path = ATLAS_ENVIRONMENTS_DIR / f"{env_name}.json"
    if not questionary.confirm(f"Save environment to {env_path}?", default=True, style=QSTYLE).ask():
        _bail("Canceled. Nothing was written.")

    # -- Store credentials (scoped to this environment) -----------------------
    if backend_choice == "keyring":
        # Create a store scoped to this environment's keyring namespace
        service = scoped_service_name(env_name)
        from platform_atlas.core.credentials import KeyringBackend
        scoped_backend = KeyringBackend(service)
        scoped_store = CredentialStore(service=service, backend_type=CredentialBackendType.KEYRING, env_name=env_name)

        scoped_store.set(CredentialKey.PLATFORM_SECRET, platform_client_secret)
        if mongo_uri:
            scoped_store.set(CredentialKey.MONGO_URI, mongo_uri)
        if redis_uri:
            scoped_store.set(CredentialKey.REDIS_URI, redis_uri)
        if gateway4_password:
            scoped_store.set(CredentialKey.GATEWAY4_PASSWORD, gateway4_password)

        # SSH passphrase handling (not applicable for Kubernetes)
        if not _is_kubernetes:
            ssh_defaults = deployment.get("ssh_defaults", {})
            ssh_passphrase = ssh_defaults.pop("key_passphrase", "")
            if ssh_passphrase:
                scoped_store.set(CredentialKey.SSH_PASSPHRASE, ssh_passphrase)

    else:
        # Vault mode: connection settings already saved to keyring above.
        if not _is_kubernetes:
            ssh_defaults = deployment.get("ssh_defaults", {})
            ssh_passphrase = ssh_defaults.pop("key_passphrase", "")
            if ssh_passphrase:
                console.print(
                    f"\n  [{theme.warning}]⚠ SSH key passphrase was provided but cannot be stored — "
                    f"Vault backend is read-only.[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Add '{CredentialKey.SSH_PASSPHRASE.value}' to your "
                    f"Vault secret manually.[/{theme.text_dim}]"
                )
            _has_gateway4 = any(
                "gateway4" in node.get("modules", [])
                for node in deployment.get("nodes", [])
            )
            if _has_gateway4 and not gateway4_password:
                console.print(
                    f"\n  [{theme.warning}]⚠ Gateway4 password must be added to Vault manually.[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Add '{CredentialKey.GATEWAY4_PASSWORD.value}' to your "
                    f"Vault secret.[/{theme.text_dim}]"
                )

    # Strip passphrases from individual node dicts before saving
    for node in deployment.get("nodes", []):
        node.pop("ssh_key_passphrase", None)

    # -- Build and save the Environment file ----------------------------------
    env = Environment(
        name=env_name,
        description=description,
        organization_name=org_name,
        platform_uri=platform_uri,
        platform_client_id=platform_client_id,
        credential_backend=backend_choice,
        deployment=deployment,
        legacy_profile=legacy_profile,
        gateway4_uri=gateway4_uri,
        gateway4_username=gateway4_username,
        values_yaml_path=k8s_meta.get("values_yaml_path", ""),
        iag5_values_yaml_path=k8s_meta.get("iag5_values_yaml_path", ""),
        kubectl_context=k8s_meta.get("kubectl_context", ""),
        kubectl_namespace=k8s_meta.get("kubectl_namespace", ""),
        use_kubectl=k8s_meta.get("use_kubectl", False),
    )

    mgr.save(env)

    # -- Set as active environment --------------------------------------------
    mgr.set_active(env_name)

    # -- Summary panel --------------------------------------------------------
    backend_label = (
        f"HashiCorp Vault ({vault_config.url})"
        if backend_choice == "vault"
        else f"OS keyring ({backend})"
    )

    cred_line = (
        "Credentials read from"
        if backend_choice == "vault"
        else "Credentials saved to"
    )

    console.print(Panel(
        f"[{theme.success_glow} bold]Environment saved[/{theme.success_glow} bold] to "
        f"[bold]{env_path}[/bold]\n"
        f"[{theme.success_glow} bold]{cred_line}[/{theme.success_glow} bold] "
        f"{backend_label}\n"
        f"[{theme.success_glow} bold]Active environment[/{theme.success_glow} bold] set to "
        f"[bold]{env_name}[/bold]",
        box=box.ROUNDED,
        border_style=theme.success,
        expand=False,
    ))

    return env

def start_setup_process() -> None:
    """
    Full setup process: global config + first environment.

    This is called on first run or via 'platform-atlas config init'.
    """
    console.print(Panel(
        f"[bold {theme.success_glow}]Atlas Setup[/bold {theme.success_glow}]\n"
        f"[{theme.text_dim}]First-time configuration[/{theme.text_dim}]",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False
    ))

    # -- Verify keyring backend before collecting any secrets ----------------
    is_secure, backend = verify_keyring_backend()
    if not is_secure:
        console.print(Panel(
            f"[bold {theme.error}]Insecure keyring backend detected: {backend}[/bold {theme.error}]\n\n"
            f"[{theme.text_primary}]Platform Atlas requires a secure OS credential store.\n"
            f"  • macOS: Keychain (built-in)\n"
            f"  • Windows: Credential Locker (built-in)\n"
            f"  • Linux: Install gnome-keyring + secretstorage + python3-dbus[/{theme.text_primary}]",
            border_style=theme.error,
            box=box.ROUNDED,
            expand=False,
        ))
        raise SystemExit(1)

    console.print(f"  [{theme.success}]✓ Credential store: {backend}[/{theme.success}]")
    console.print()

    if ATLAS_CONFIG_FILE.exists():
        ok = questionary.confirm(f"{ATLAS_CONFIG_FILE} already exists. Overwrite?", default=False, style=QSTYLE).ask()
        if not ok:
            _bail()

    # ================================================================
    # Phase 1: Global Settings
    # ================================================================
    _section("Global Settings", "Settings that apply across all environments")

    org_name = ask_text("Organization Name", "(Example: Acme Org) ")

    # -- Write global config --------------------------------------------------
    global_data: dict[str, Any] = {
        "organization_name": org_name,
        "verify_ssl": False,
        "dark_mode": True,
        "theme": "horizon-dark",
        "extended_validation_checks": True,
        "multi_tenant_mode": False,
        "debug": False,
    }

    atomic_write_json(ATLAS_CONFIG_FILE, global_data)

    console.print(Panel(
        f"[{theme.success_glow} bold]Global config saved[/{theme.success_glow} bold] to "
        f"[bold]{ATLAS_CONFIG_FILE}[/bold]",
        box=box.ROUNDED,
        border_style=theme.success,
        expand=False,
    ))

    # ================================================================
    # Phase 2: First Environment
    # ================================================================
    console.print()
    _hint("Now let's configure your first deployment environment.")
    _hint("Each environment represents one IAP deployment (dev, staging, production, etc.)\n")

    # Ensure environments directory exists
    ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)

    create_environment_wizard()

    # -- Offer to create additional environments ------------------------------
    while True:
        console.print()
        add_more = questionary.confirm(
            "Create another environment?",
            default=False,
            style=QSTYLE,
        ).ask()
        if not add_more:
            break
        create_environment_wizard()


def welcome_screen() -> None:
    """Initial welcome screen for first-time users"""

    body = Text()
    footer = Text()
    title = Text(f"Platform Atlas {__version__}", style=f"bold {theme.primary_glow}")
    subtitle = Text(
        "Itential Platform Configuration Auditing & Validation",
        style=f"italic {theme.text_muted}"
    )

    body.append("Welcome to Platform Atlas! 🎉\n\n", style=f"bold {theme.text_primary}")
    body.append("This software helps you audit, validate, and report on Itential Platform configurations\n"
                "ensuring they meet production standards and security requirements.\n\n")
    body.append("• Automated data collection from remote systems\n"
                "• Rule-based validation with customizable rulesets\n", style=theme.text_dim)

    footer.append("\nThis appears to be your first time using Platform Atlas!\n")
    footer.append(f"\nA configuration directory will be created at {ATLAS_HOME}\n\n",
                    style=f"bold {theme.success_glow}")
    footer.append("▶ New to Platform Atlas?\n", style=f"bold {theme.primary_glow}")
    footer.append("  ▷ Run with 'platform-atlas guide' to view the README\n", style=f"bold {theme.text_primary}")
    footer.append("  ▷ Run with --help to explore available commands\n", style=f"bold {theme.text_primary}")

    content = Group(
        Align.center(title),
        Align.center(subtitle),
        Rule(),
        body,
        Rule(),
        footer,
    )
    panel = Align.center(Panel(
        Align.center(content, vertical="middle"),
        border_style=theme.border_primary,
        padding=(1, 4),
        expand=False,
        title="WELCOME"
    ))

    console.clear()
    console.print(panel)
    console.input(f"[{theme.text_dim}]Press Enter to continue...[/{theme.text_dim}]")
