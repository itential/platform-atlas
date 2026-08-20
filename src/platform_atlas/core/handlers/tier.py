# pylint: disable=line-too-long
"""
Dispatch Handler ::: Tier Management

Commands:
    tier show       — Display current tier and what's enabled
    tier set        — Set the global default tier (standard | extended)
    tier upgrade    — Interactive Standard → Extended upgrade
    tier downgrade  — Interactive Extended → Standard downgrade

SaaS is not a global default and is never a conversion target — a SaaS
environment binds its tier (and gateway kind) at create time. Upgrade/
downgrade between Standard and Extended work exactly as before.
"""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path
from typing import Any

import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from platform_atlas.core import ui
from platform_atlas.core.context import ctx
from platform_atlas.core.config import _VALID_TIERS
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.registry import registry
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.core.init_setup import get_qstyle

console = Console()
theme = ui.theme
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# tier show
# ---------------------------------------------------------------------------

@registry.register("tier", "show", description="Display the active tier and what's enabled")
def handle_tier_show(_: Namespace) -> int:
    """Print the active tier with a summary of enabled capability."""
    config = ctx().config
    tier = config.tier

    table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 2))
    table.add_column(style=f"bold {theme.text_primary}")
    table.add_column()

    color = {
        "standard": theme.tier_standard,
        "saas": theme.tier_saas,
    }.get(tier, theme.tier_extended)
    badge = f"[bold {color}]{tier.upper()}[/bold {color}]"
    table.add_row("Mode", badge)

    if tier == "standard":
        table.add_row("Platform OAuth audit", f"[{theme.success}]enabled[/{theme.success}]")
        if config.gateway4_uri:
            table.add_row(
                "Itential Automation Gateway 4 (IAG4)",
                f"[{theme.success}]enabled[/{theme.success}]",
            )
        else:
            table.add_row(
                "Itential Automation Gateway 4 (IAG4)",
                f"[{theme.text_dim}]not configured — add gateway4_uri to your env[/{theme.text_dim}]",
            )
        try:
            ruleset = ctx().ruleset
            table.add_row("Active rules", f"{len(ruleset.rules)} (Standard-tier)")
        except Exception:
            pass
    elif tier == "saas":
        kind = (config.saas_gateway_kind or "").strip().lower()
        if kind:
            kind_label = {"gateway4": "Gateway 4 (IAG4)", "gateway5": "Gateway 5 (IAG5)", "gw4-gw5": "Gateway 4 + Gateway 5"}.get(kind, kind)
            table.add_row("Gateway under audit", f"[{theme.success}]{kind_label}[/{theme.success}]")
        else:
            table.add_row(
                "Gateway under audit",
                f"[{theme.warning}]not set — recreate the environment[/{theme.warning}]",
            )
        if kind in ("gateway4", "gw4-gw5"):
            api_state = (
                f"[{theme.success}]enabled[/{theme.success}]" if config.gateway4_uri
                else f"[{theme.text_dim}]not configured — add gateway4_uri to your env[/{theme.text_dim}]"
            )
            table.add_row("Gateway4 API (ipsdk)", api_state)
        table.add_row(
            "Platform / MongoDB / Redis",
            f"[{theme.text_dim}]not part of a SaaS audit[/{theme.text_dim}]",
        )
        try:
            ruleset = ctx().ruleset
            table.add_row("Active rules", f"{len(ruleset.rules)} (SaaS gateway rules)")
        except Exception:
            pass
    else:
        table.add_row("Full Platform + IAG4 audit", f"[{theme.success}]enabled[/{theme.success}]")
        table.add_row("MongoDB / Redis / SSH collection", f"[{theme.success}]enabled[/{theme.success}]")
        try:
            ruleset = ctx().ruleset
            table.add_row("Active rules", f"{len(ruleset.rules)} (full ruleset)")
        except Exception:
            pass

    console.print(Panel(
        table,
        title=f"Platform Atlas — Active Tier",
        border_style=color,
        box=box.ROUNDED,
        expand=False,
    ))

    if tier == "standard":
        console.print(
            f"\n  [{theme.text_dim}]Want deeper validation? Run "
            f"[bold]platform-atlas tier upgrade[/bold] to add MongoDB, Redis, "
            f"and system-layer audits.[/{theme.text_dim}]\n"
        )

    return 0


# ---------------------------------------------------------------------------
# tier set
# ---------------------------------------------------------------------------

@registry.register("tier", "set", description="Set the global default tier")
def handle_tier_set(args: Namespace) -> int:
    """Persist a new global tier to ~/.atlas/config.json."""
    new_tier = (getattr(args, "tier_value", None) or "").strip().lower()
    if not new_tier:
        new_tier = questionary.select(
            "Set the global default tier:",
            choices=["standard", "extended"],
            default=ctx().tier,
            style=get_qstyle(),
        ).ask()
        # Match the project-wide pattern (environment.py, session.py,
        # init_setup.py): questionary returns None on Ctrl-C; re-raise so
        # the interrupt surfaces with the standard exit-130 signal instead
        # of being swallowed into a plain non-zero return.
        if new_tier is None:
            raise KeyboardInterrupt

    if new_tier not in _VALID_TIERS:
        console.print(
            f"  [{theme.error}]Invalid tier '{new_tier}'. Valid: "
            f"{sorted(_VALID_TIERS)}[/{theme.error}]"
        )
        return 1

    if new_tier == "saas":
        console.print(
            f"  [{theme.warning}]SaaS is not a global default — it is chosen "
            f"per-environment at create time.[/{theme.warning}]\n"
            f"  [{theme.text_dim}]Run [bold]platform-atlas env create[/bold] and pick "
            f"the SaaS tier there.[/{theme.text_dim}]"
        )
        return 1

    return _persist_tier(new_tier)


# ---------------------------------------------------------------------------
# tier upgrade  (Standard → Extended)
# ---------------------------------------------------------------------------

@registry.register("tier", "upgrade", description="Upgrade Standard → Extended (interactive)")
def handle_tier_upgrade(args: Namespace) -> int:
    """Guided Standard → Extended upgrade.

    Walks the user through the extra pieces Extended needs (deployment
    topology, SSH, and Mongo/Redis connections) and only flips the tier at
    the very end, once everything is in place and confirmed. Backing out at
    any point — Ctrl-C, declining a step, or choosing the browser form and
    not returning — leaves the environment untouched in Standard Mode.

    The optional ``--from-file`` path finishes an upgrade that was started in
    the browser form, applying its downloaded JSON to the active environment.
    """
    if ctx().tier == "saas":
        _print_saas_conversion_blocked()
        return 1
    if ctx().tier == "extended":
        console.print(
            f"  [{theme.success}]You're already in Extended Mode[/{theme.success}] — "
            f"nothing to upgrade. [{theme.text_dim}]Run "
            f"[bold]platform-atlas tier show[/bold] to see what's enabled.[/{theme.text_dim}]"
        )
        return 0

    from_file = getattr(args, "from_file", None)
    if from_file:
        try:
            return _handle_tier_upgrade_from_file(from_file, keep_file=getattr(args, "keep_file", False))
        except KeyboardInterrupt:
            _print_upgrade_cancelled()
            return 1

    # -- Extended attaches to a concrete environment; require an active one ---
    env = _resolve_active_env_for_upgrade()
    if env is None:
        return 1  # a friendly explanation was already printed

    _print_upgrade_intro(env)

    confirm = questionary.confirm(
        f"Ready to walk through upgrading '{env.name}' to Extended Mode?",
        default=True,
        style=get_qstyle(),
    ).ask()
    # questionary returns None on Ctrl-C — propagate the interrupt rather
    # than treating it as "user said no".
    if confirm is None:
        raise KeyboardInterrupt
    if not confirm:
        _print_upgrade_cancelled()
        return 1

    method = _ask_upgrade_method()
    if method is None:
        raise KeyboardInterrupt
    if method == "cancel":
        _print_upgrade_cancelled()
        return 1
    if method == "browser":
        return _handle_tier_upgrade_html(env)

    # method == "cli"
    try:
        return _run_tier_upgrade_walkthrough(env)
    except KeyboardInterrupt:
        _print_upgrade_cancelled()
        return 1


# ---------------------------------------------------------------------------
# tier downgrade  (Extended → Standard)
# ---------------------------------------------------------------------------

@registry.register("tier", "downgrade", description="Downgrade Extended → Standard (interactive)")
def handle_tier_downgrade(_: Namespace) -> int:
    """Interactive Extended → Standard downgrade.

    Preserves env credentials in keyring so a future ``tier upgrade`` is
    friction-free. Only flips the global tier and warns about reduced
    coverage.
    """
    if ctx().tier == "saas":
        _print_saas_conversion_blocked()
        return 1
    if ctx().tier == "standard":
        console.print(
            f"  [{theme.text_dim}]Already in Standard Mode — nothing to downgrade.[/{theme.text_dim}]"
        )
        return 0

    active_env = ctx().config.active_environment
    env_line = (
        f"Active environment: [bold {theme.accent}]{active_env}[/bold {theme.accent}]\n\n"
        if active_env
        else f"[{theme.text_dim}]No active environment — only the global default tier "
             f"will change.[/{theme.text_dim}]\n\n"
    )

    console.print(Panel(
        f"[bold]Downgrade to Standard Mode[/bold]\n\n"
        f"{env_line}"
        f"Standard Mode runs only Platform OAuth (and optional IAG4 API).\n"
        f"You will [bold]lose visibility into[/bold]:\n"
        f"  • MongoDB replica set & ACL audits\n"
        f"  • Redis runtime config & ACL audits\n"
        f"  • System-layer checks\n"
        f"  • Configuration file validation\n"
        f"  • Log analysis (SSH-derived)\n\n"
        f"[{theme.text_dim}]Stored credentials (Mongo URI, Redis URI, SSH passphrase) are\n"
        f"preserved so [bold]tier upgrade[/bold] can restore Extended quickly.[/{theme.text_dim}]",
        title="Downgrade to Standard",
        border_style=theme.warning,
        box=box.ROUNDED,
        expand=False,
    ))

    confirm = questionary.confirm(
        "Continue with the downgrade?",
        default=False,
        style=get_qstyle(),
    ).ask()
    # questionary returns None on Ctrl-C — propagate the interrupt rather
    # than treating it as "user said no".
    if confirm is None:
        raise KeyboardInterrupt
    if not confirm:
        console.print(f"  [{theme.text_dim}]Downgrade cancelled.[/{theme.text_dim}]")
        return 1

    return _persist_tier("standard")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_saas_conversion_blocked() -> None:
    """Explain why a SaaS environment cannot be converted to another tier."""
    console.print(
        f"  [{theme.warning}]This environment is a SaaS (single-gateway) audit — "
        f"its tier is fixed at create time.[/{theme.warning}]\n"
        f"  [{theme.text_dim}]A SaaS environment has a fundamentally different shape "
        f"(gateway anchor, no Platform/Mongo/Redis), so converting it would leave it "
        f"half-invalid. Create a new environment with "
        f"[bold]platform-atlas env create[/bold] instead.[/{theme.text_dim}]"
    )


# ---------------------------------------------------------------------------
# Guided upgrade — shared pieces
# ---------------------------------------------------------------------------

def _print_upgrade_cancelled() -> None:
    """Reassure the user that a cancelled upgrade changed nothing."""
    console.print(
        f"\n  [{theme.text_dim}]No problem — upgrade cancelled. Nothing was changed, and "
        f"your environment is still in Standard Mode.[/{theme.text_dim}]\n"
        f"  [{theme.text_dim}]Run [bold]platform-atlas tier upgrade[/bold] again whenever "
        f"you're ready.[/{theme.text_dim}]\n"
    )


def _resolve_active_env_for_upgrade():
    """Return the active environment to upgrade, or None with guidance printed.

    Extended Mode's extra pieces (topology, SSH, Mongo/Redis) attach to a
    concrete environment, so an active one is required. Rather than failing
    with a terse error, we explain what to do next.
    """
    from platform_atlas.core.environment import get_environment_manager

    mgr = get_environment_manager()
    active = mgr.get_active_name()

    if not active or not mgr.exists(active):
        console.print(Panel(
            f"[bold]Let's pick an environment first[/bold]\n\n"
            f"Extended Mode adds infrastructure auditing (MongoDB, Redis, SSH) to a\n"
            f"specific environment, so we need an active one to upgrade.\n\n"
            f"[{theme.text_dim}]Do one of the following, then run "
            f"[bold]platform-atlas tier upgrade[/bold] again:[/{theme.text_dim}]\n"
            f"  • [bold]platform-atlas env switch[/bold]  — make an existing environment active\n"
            f"  • [bold]platform-atlas env create[/bold]  — set up a new environment",
            title="No active environment",
            border_style=theme.warning,
            box=box.ROUNDED,
            expand=False,
        ))
        return None

    try:
        env = mgr.load(active)
    except Exception as exc:  # pylint: disable=broad-except
        console.print(
            f"  [{theme.error}]Could not load the active environment '{active}': {exc}[/{theme.error}]"
        )
        return None

    if (getattr(env, "tier", None) or "").lower() == "saas":
        _print_saas_conversion_blocked()
        return None

    return env


def _print_upgrade_intro(env) -> None:
    """Warm, non-scary summary of what the upgrade will do."""
    console.print(Panel(
        f"[bold]Upgrade to Extended Mode[/bold]\n\n"
        f"Active environment: [bold {theme.accent}]{env.name}[/bold {theme.accent}]\n\n"
        f"Extended Mode unlocks deeper auditing:\n"
        f"  ✓ MongoDB replica set & ACL audit\n"
        f"  ✓ Redis runtime config & ACL audit\n"
        f"  ✓ System-layer checks (CPU, memory, disk, ulimits)\n"
        f"  ✓ Configuration file validation & log analysis\n"
        f"  ✓ IAG5 / Kubernetes deployments\n\n"
        f"We'll ask for a few extra details — how your deployment is laid out and\n"
        f"how to reach MongoDB and Redis. This sometimes involves your database,\n"
        f"infrastructure, or security teams, so take your time.\n\n"
        f"[{theme.success}]Nothing changes until the very end[/{theme.success}] — you can stop at any\n"
        f"point and your environment stays exactly as it is now.\n\n"
        f"[{theme.text_dim}]Itential is happy to help — talk to your CSM or see the\n"
        f"Extended Access Guide.[/{theme.text_dim}]",
        title="Upgrade to Extended",
        border_style=theme.accent,
        box=box.ROUNDED,
        expand=False,
    ))


def _ask_upgrade_method() -> str | None:
    """Ask whether to enter the Extended details in the terminal or the browser."""
    return questionary.select(
        "How would you like to enter the Extended details?",
        choices=[
            questionary.Choice(
                "Here in the terminal  — a guided step-by-step walkthrough",
                value="cli",
            ),
            questionary.Choice(
                "In my browser  — fill out a form, then finish from the CLI",
                value="browser",
            ),
            questionary.Choice(
                "Not right now  — cancel, nothing changes",
                value="cancel",
            ),
        ],
        style=get_qstyle(),
    ).ask()


def _upgrade_next_steps() -> None:
    """Print the post-upgrade next steps (shared by both entry paths)."""
    console.print(
        f"\n  [{theme.text_dim}]Next steps:[/{theme.text_dim}]\n"
        f"  • Run [bold]platform-atlas preflight[/bold] to validate connectivity.\n"
        f"  • Run [bold]platform-atlas config doctor[/bold] to confirm every credential resolves.\n"
        f"  • Run [bold]platform-atlas session run all[/bold] for a full Extended audit.\n"
    )


def _infer_gateway_kind(deployment: dict) -> str | None:
    """Mirror the create wizard's gateway_kind inference from topology nodes."""
    nodes = deployment.get("nodes", [])
    has_gw4 = any("gateway4" in n.get("modules", []) for n in nodes)
    has_gw5 = any("gateway5" in n.get("modules", []) for n in nodes)
    if has_gw4 and has_gw5:
        return "gw4-gw5"
    return None


def _apply_extended_topology(env, deployment: dict, k8s_meta: dict, ssh_key: str) -> None:
    """Write the Extended topology + gateway/k8s fields onto ``env`` in memory.

    Does not touch credentials or the tier — the caller commits those.
    """
    env.deployment = deployment
    env.ssh_key = ssh_key
    inferred_kind = _infer_gateway_kind(deployment)
    if inferred_kind:
        env.gateway_kind = inferred_kind
    if deployment.get("mode") == "kubernetes":
        env.values_yaml_path = k8s_meta.get("values_yaml_path", "") or getattr(env, "values_yaml_path", "")
        env.iag5_values_yaml_path = k8s_meta.get("iag5_values_yaml_path", "") or getattr(env, "iag5_values_yaml_path", "")
        env.values_yaml_chart_defaults_path = (
            k8s_meta.get("values_yaml_chart_defaults_path", "")
            or getattr(env, "values_yaml_chart_defaults_path", "")
        )
        env.iag5_values_yaml_chart_defaults_path = (
            k8s_meta.get("iag5_values_yaml_chart_defaults_path", "")
            or getattr(env, "iag5_values_yaml_chart_defaults_path", "")
        )
        env.kubectl_context = k8s_meta.get("kubectl_context", "") or getattr(env, "kubectl_context", "")
        env.kubectl_namespace = k8s_meta.get("kubectl_namespace", "") or getattr(env, "kubectl_namespace", "")
        env.kubectl_binary_path = (
            k8s_meta.get("kubectl_binary_path", "") or getattr(env, "kubectl_binary_path", "")
        )
        env.use_kubectl = k8s_meta.get("use_kubectl", getattr(env, "use_kubectl", False))


# ---------------------------------------------------------------------------
# Guided upgrade — terminal walkthrough
# ---------------------------------------------------------------------------

def _run_tier_upgrade_walkthrough(env) -> int:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Collect the Extended additions in the terminal, staging everything.

    Nothing is written until the final confirmation: topology and secrets are
    held in memory, then committed together (env file → credentials → tier).
    """
    from platform_atlas.core.environment import get_environment_manager, propagate_ssh_key
    from platform_atlas.core.credentials import CredentialKey, scoped_service_name
    from platform_atlas.core.topology import DeploymentTopology
    from platform_atlas.core.init_setup import (
        ask_deployment,
        ask_text,
        ask_text_with_default,
        ask_secret,
        _collect_and_verify_db_uri,
        _test_mongo_connection,
        _test_redis_connection,
        _configure_protocol_jumphost,
        _ask_tls_toggle,
        _ask_ssh_key_passphrase,
        _explicit_substrate,
        _display_topology_review,
        _display_kubernetes_review,
    )

    backend = (getattr(env, "credential_backend", None) or "keyring").lower()

    # -- 1. Deployment topology ----------------------------------------------
    deployment, k8s_meta = ask_deployment()
    is_k8s = deployment.get("mode") == "kubernetes"

    # Resolve the SSH key: a key picked in the topology wins, else keep the
    # one the environment may already carry.
    ssh_defaults = deployment.get("ssh_defaults", {})
    ssh_key = ssh_defaults.get("key_path", "") or getattr(env, "ssh_key", "")
    if ssh_key:
        deployment = propagate_ssh_key(deployment, ssh_key)
        ssh_defaults = deployment.get("ssh_defaults", {})

    # Pull SSH secrets out of the topology so they land in the credential
    # store, not the env file (matches the create wizard).
    ssh_passphrase = ""
    ssh_password = ""
    if not is_k8s:
        ssh_passphrase = ssh_defaults.pop("key_passphrase", "")
        ssh_password = ssh_defaults.pop("password", "")
    for node in deployment.get("nodes", []):
        node.pop("ssh_key_passphrase", None)
        node.pop("ssh_password", None)

    # -- 2. Gateway4 API (only if a gateway4 node was added and none exists) --
    gateway4_uri = getattr(env, "gateway4_uri", "") or ""
    gateway4_username = getattr(env, "gateway4_username", "") or ""
    gateway4_password = ""
    has_gw4_node = any("gateway4" in n.get("modules", []) for n in deployment.get("nodes", []))
    if has_gw4_node and not gateway4_uri and not is_k8s:
        _section_line("Gateway 4 API", "Direct API connection for config collection (primary source)")
        gateway4_uri = ask_text("Gateway4 API URI", "(Example: http://gateway-host:8083) ", uri=True)
        gateway4_username = ask_text_with_default("Gateway4 Username", default="admin@itential")
        if backend in ("keyring", "file"):
            gateway4_password = ask_secret("Gateway4 Password (hidden)")

    # -- 3. Database connections (keyring/file only; Vault reads at runtime) --
    mongo_uri = ""
    redis_uri = ""
    mongo_tls_enabled = False
    redis_tls_enabled = False
    protocol_jumphost = None
    if backend == "vault":
        vault_keys = ["mongo_uri", "redis_uri"]
        if not is_k8s:
            if ssh_key:
                vault_keys.append("ssh_key_passphrase")
            vault_keys.append("ssh_password")
        if has_gw4_node and not gateway4_uri:
            vault_keys.append("gateway4_password")
        keys_fmt = ", ".join(f"[bold]{key}[/bold]" for key in vault_keys)
        console.print(
            f"\n  [{theme.warning}]This environment uses HashiCorp Vault.[/{theme.warning}] "
            f"[{theme.text_dim}]Atlas reads these from Vault at capture time, so there's "
            f"nothing to enter here — just make sure your Vault secret has {keys_fmt}.[/{theme.text_dim}]\n"
        )
        mongo_tls_enabled = _ask_tls_toggle("MongoDB")
        redis_tls_enabled = _ask_tls_toggle("Redis")
    else:
        _section_line("Database Connections", "How Atlas reaches MongoDB and Redis")
        _hint_line("Both are optional — skip either if it isn't part of this deployment.")
        mongo_uri, mongo_tls_enabled = _collect_and_verify_db_uri(
            "MongoDB URI",
            schemes=("mongodb://", "mongodb+srv://"),
            test_fn=_test_mongo_connection,
            tls_label="MongoDB",
        )
        redis_uri, redis_tls_enabled = _collect_and_verify_db_uri(
            "Redis URI",
            schemes=("redis://", "rediss://"),
            test_fn=_test_redis_connection,
            tls_label="Redis",
        )
        console.print()
        protocol_jumphost = _configure_protocol_jumphost(
            mongo_uri, redis_uri,
            mongo_tls_enabled=mongo_tls_enabled, redis_tls_enabled=redis_tls_enabled,
        )
        if ssh_key and not ssh_passphrase and not is_k8s:
            ssh_passphrase = _ask_ssh_key_passphrase(ssh_key)

    # -- 4. Review everything before committing ------------------------------
    _section_line("Review", "Here's what we'll add — nothing is saved yet")
    topology = DeploymentTopology.from_dict(deployment)
    if is_k8s:
        _display_kubernetes_review(topology, k8s_meta)
    else:
        _display_topology_review(topology, capture_scope=deployment.get("capture_scope", "primary_only"))
    _render_upgrade_credentials_summary(
        env, backend, mongo_uri, redis_uri, gateway4_uri, ssh_key, ssh_passphrase, ssh_password,
        protocol_jumphost, mongo_tls_enabled, redis_tls_enabled,
    )

    confirm = questionary.confirm(
        f"All set — switch '{env.name}' to Extended Mode now?",
        default=True,
        style=get_qstyle(),
    ).ask()
    if confirm is None:
        raise KeyboardInterrupt
    if not confirm:
        _print_upgrade_cancelled()
        return 1

    # -- 5. Commit (point of no return): env file → credentials → tier -------
    mgr = get_environment_manager()
    _apply_extended_topology(env, deployment, k8s_meta, ssh_key)
    if gateway4_uri:
        env.gateway4_uri = gateway4_uri
        env.gateway4_username = gateway4_username or "admin@itential"
    env.protocol_jumphost = protocol_jumphost
    env.mongo_tls_enabled = mongo_tls_enabled
    env.redis_tls_enabled = redis_tls_enabled
    env.tier = "extended"
    mgr.save(env)

    if backend in ("keyring", "file"):
        service = scoped_service_name(env.name)
        substrate = _explicit_substrate(backend)
        if mongo_uri:
            substrate.set(service, CredentialKey.MONGO_URI.value, mongo_uri)
        if redis_uri:
            substrate.set(service, CredentialKey.REDIS_URI.value, redis_uri)
        if gateway4_password:
            substrate.set(service, CredentialKey.GATEWAY4_PASSWORD.value, gateway4_password)
        if ssh_passphrase:
            substrate.set(service, CredentialKey.SSH_PASSPHRASE.value, ssh_passphrase)
        if ssh_password:
            substrate.set(service, CredentialKey.SSH_PASSWORD.value, ssh_password)

    rc = _persist_tier("extended")
    if rc == 0:
        console.print(
            f"  [{theme.success}]✓ '{env.name}' is now an Extended environment.[/{theme.success}]"
        )
        _upgrade_next_steps()
    return rc


def _render_upgrade_credentials_summary(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    env, backend: str, mongo_uri: str, redis_uri: str,
    gateway4_uri: str, ssh_key: str, ssh_passphrase: str, ssh_password: str,
    protocol_jumphost: dict | None = None,
    mongo_tls_enabled: bool = False, redis_tls_enabled: bool = False,
) -> None:
    """Show the non-topology additions (credentials/paths) about to be applied."""
    from platform_atlas.core.utils import redact_uri_credentials

    t = Table(show_header=False, box=box.SIMPLE_HEAVY, pad_edge=True)
    t.add_column("Field", style=f"bold {theme.text_primary}", min_width=22)
    t.add_column("Value", style=theme.text_secondary)

    t.add_row("environment", env.name)
    t.add_row("credential_backend", backend)
    dim_skip = f"[{theme.text_dim}]— skipped[/{theme.text_dim}]"
    if backend == "vault":
        t.add_row("mongo_uri", f"[{theme.text_dim}]from Vault at runtime[/{theme.text_dim}]")
        t.add_row("redis_uri", f"[{theme.text_dim}]from Vault at runtime[/{theme.text_dim}]")
    else:
        t.add_row("mongo_uri", redact_uri_credentials(mongo_uri) if mongo_uri else dim_skip)
        t.add_row("redis_uri", redact_uri_credentials(redis_uri) if redis_uri else dim_skip)
    t.add_row("mongo_tls_enabled", "yes" if mongo_tls_enabled else "no")
    t.add_row("redis_tls_enabled", "yes" if redis_tls_enabled else "no")
    if ssh_key:
        t.add_row("ssh_key", ssh_key)
        if backend != "vault":
            t.add_row(
                "ssh_key_passphrase",
                f"[{theme.success}]provided[/{theme.success}]" if ssh_passphrase
                else f"[{theme.text_dim}]none / unencrypted key[/{theme.text_dim}]",
            )
    if ssh_password and backend != "vault":
        t.add_row("ssh_password", f"[{theme.success}]provided[/{theme.success}]")
    if gateway4_uri:
        t.add_row("gateway4_uri", gateway4_uri)
    if protocol_jumphost:
        t.add_row(
            "jumphost_tunnel",
            f"[{theme.success}]{protocol_jumphost.get('ssh_target', '')}[/{theme.success}]",
        )

    console.print(Panel(
        t,
        title="Connection Details",
        box=box.ROUNDED,
        border_style=theme.border_primary,
        expand=False,
    ))


# ---------------------------------------------------------------------------
# Guided upgrade — browser form + --from-file finish
# ---------------------------------------------------------------------------

def _get_tier_upgrade_html_path() -> Path | None:
    """Locate tier-upgrade.html, syncing it from the package into ~/.atlas/guides/."""
    from platform_atlas.core.paths import ATLAS_HOME, ATLAS_HOME_GUIDES

    dest = ATLAS_HOME_GUIDES / "tier-upgrade.html"
    html_bytes: bytes | None = None

    try:
        from importlib.resources import files as pkg_files
        html_bytes = pkg_files("platform_atlas.guides").joinpath("tier-upgrade.html").read_bytes()
    except Exception:  # pylint: disable=broad-except
        pass

    if html_bytes is None:
        fallback = Path(__file__).parent.parent.parent / "guides" / "tier-upgrade.html"
        if fallback.exists():
            html_bytes = fallback.read_bytes()

    if html_bytes is None:
        return None

    ATLAS_HOME_GUIDES.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest.write_bytes(html_bytes)

    # Sync the shared CSS + motion assets the wizard shell references.
    from platform_atlas.core.guide_assets import sync_guide_assets
    sync_guide_assets(ATLAS_HOME_GUIDES)

    # Remove the pre-guides-folder copy that used to live directly under ~/.atlas.
    legacy = ATLAS_HOME / "tier-upgrade.html"
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

    return dest


def _handle_tier_upgrade_html(env) -> int:
    """Open the browser-based tier-upgrade builder, then bow out unchanged."""
    path = _get_tier_upgrade_html_path()
    if path is None:
        console.print(
            f"\n  [{theme.error}]The tier-upgrade page couldn't be found. "
            f"Try reinstalling platform-atlas, or choose the terminal walkthrough "
            f"instead.[/{theme.error}]\n"
        )
        return 1

    if ui.maybe_open_html(f"file://{path.resolve()}"):
        console.print(
            f"\n  [{theme.primary_glow}]Tier Upgrade Builder[/{theme.primary_glow}]  "
            f"[{theme.text_dim}]opened in your browser[/{theme.text_dim}]"
        )
    else:
        console.print(
            f"\n  [{theme.primary_glow}]Tier Upgrade Builder[/{theme.primary_glow}]  "
            f"[{theme.text_dim}]server environment detected — open manually: {path}[/{theme.text_dim}]"
        )
    console.print(
        f"\n  [{theme.text_secondary}]1. Fill out the form for "
        f"[bold]{env.name}[/bold] (including credentials) and click "
        f"[bold]Generate Encrypted Bundle[/bold].[/{theme.text_secondary}]"
    )
    console.print(f"  [{theme.text_secondary}]2. Copy the one-time passphrase it shows you.[/{theme.text_secondary}]")
    console.print(f"  [{theme.text_secondary}]3. Save the .atlasenv.enc file, then run:[/{theme.text_secondary}]")
    console.print(
        f"\n  [{theme.accent}]platform-atlas tier upgrade --from-file "
        f"<path-to-bundle.atlasenv.enc>[/{theme.accent}]"
    )
    console.print(
        f"\n  [{theme.text_dim}]Nothing has changed yet — '{env.name}' is still in Standard "
        f"Mode. You'll enter the passphrase when prompted; the bundle is shredded after a "
        f"successful upgrade (--keep-file to retain it). If the browser isn't an option "
        f"(e.g. a headless server), run [bold]platform-atlas tier upgrade[/bold] again and "
        f"pick the terminal walkthrough.[/{theme.text_dim}]\n"
    )
    return 0


def _handle_tier_upgrade_from_file(file_path: str, keep_file: bool = False) -> int:  # pylint: disable=too-many-branches,too-many-statements,too-many-return-statements,too-many-locals
    """Finish a browser-started upgrade by applying its bundle to the active env.

    An encrypted bundle (``.atlasenv.enc``) is decrypted with its passphrase and
    its secrets are written straight into the credential backend (no re-typing),
    then the bundle is shredded unless ``keep_file`` is set.  A plain
    ``_tier_upgrade`` JSON (no encryption) still falls back to the interactive
    credential prompts for backward compatibility.
    """
    from platform_atlas.core.environment import get_environment_manager, propagate_ssh_key
    from platform_atlas.core.topology import DeploymentTopology
    from platform_atlas.core.init_setup import _display_topology_review, _display_kubernetes_review
    from platform_atlas.core.handlers.env import (
        _collect_credentials_post_html_setup, _apply_bundle_credentials, _shred_bundle_file,
    )
    from platform_atlas.core import bundle_crypto

    env = _resolve_active_env_for_upgrade()
    if env is None:
        return 1

    src = Path(file_path).expanduser()
    if not src.exists():
        console.print(f"\n  [{theme.error}]File not found: {src}[/{theme.error}]\n")
        return 1
    if src.suffix.lower() not in (".json", ".enc"):
        console.print(f"\n  [{theme.warning}]Expected a .json file or .enc bundle from the form.[/{theme.warning}]\n")
        return 1
    try:
        parsed = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"\n  [{theme.error}]That file isn't valid JSON: {exc}[/{theme.error}]\n")
        return 1

    # -- Encrypted bundle: prompt + decrypt ----------------------------------
    was_encrypted = bundle_crypto.is_encrypted_bundle(parsed)
    if was_encrypted:
        _section_line("Encrypted Bundle", f"decrypting {src.name}")
        console.print(
            f"  [{theme.text_dim}]Enter the passphrase the builder showed you when this "
            f"bundle was generated.[/{theme.text_dim}]\n"
        )
        raw = None
        for attempt in range(3):
            passphrase = questionary.password("Bundle passphrase:", style=get_qstyle()).ask()
            if passphrase is None:
                console.print(f"\n  [{theme.text_dim}]Cancelled[/{theme.text_dim}]\n")
                return 1
            try:
                raw = bundle_crypto.decrypt_bundle(parsed, passphrase)
                break
            except bundle_crypto.BundleDecryptError:
                remaining = 2 - attempt
                console.print(
                    f"  [{theme.error}]✗ Wrong passphrase{'' if not remaining else f' — {remaining} left'}."
                    f"[/{theme.error}]"
                )
            except bundle_crypto.BundleError as exc:
                console.print(f"\n  [{theme.error}]Bundle is not valid: {exc}[/{theme.error}]\n")
                return 1
        if raw is None:
            return 1
    else:
        raw = parsed

    if not isinstance(raw, dict):
        console.print(f"\n  [{theme.error}]Bundle content must be a JSON object.[/{theme.error}]\n")
        return 1

    bundle_secrets = raw.pop("secrets", None)
    vault_connection = raw.pop("vault_connection", None)

    if not raw.pop("_tier_upgrade", False):
        console.print(
            f"\n  [{theme.warning}]This file doesn't look like a tier-upgrade form export.[/{theme.warning}]\n"
            f"  [{theme.text_dim}]Use the JSON downloaded from the Tier Upgrade Builder, or run "
            f"[bold]platform-atlas tier upgrade[/bold] for the terminal walkthrough.[/{theme.text_dim}]\n"
        )
        return 1

    deployment = raw.get("deployment") or {}
    ssh_key = (raw.get("ssh_key") or "").strip()
    if ssh_key and deployment:
        deployment = propagate_ssh_key(deployment, ssh_key)
    is_k8s = deployment.get("mode") == "kubernetes"

    # -- Review before committing --------------------------------------------
    _section_line("Review", f"loaded from {src.name} — nothing is saved yet")
    if deployment:
        topology = DeploymentTopology.from_dict(deployment)
        if is_k8s:
            _display_kubernetes_review(topology, raw.get("k8s_meta", {}))
        else:
            _display_topology_review(topology, capture_scope=deployment.get("capture_scope", "primary_only"))
    else:
        console.print(
            f"  [{theme.text_dim}]No deployment topology in the form — you can add one later "
            f"with [bold]platform-atlas env edit[/bold].[/{theme.text_dim}]"
        )

    confirm = questionary.confirm(
        f"Switch '{env.name}' to Extended Mode and enter its credentials now?",
        default=True,
        style=get_qstyle(),
    ).ask()
    if confirm is None:
        raise KeyboardInterrupt
    if not confirm:
        _print_upgrade_cancelled()
        return 1

    # -- Commit: apply topology, flip tier, then collect secrets -------------
    mgr = get_environment_manager()
    if deployment:
        _apply_extended_topology(env, deployment, raw.get("k8s_meta", {}), ssh_key)
    elif ssh_key:
        env.ssh_key = ssh_key
    gw4_uri = (raw.get("gateway4_uri") or "").strip()
    if gw4_uri:
        env.gateway4_uri = gw4_uri
        env.gateway4_username = (raw.get("gateway4_username") or "admin@itential").strip()
    jumphost_from_form = raw.get("protocol_jumphost")
    if jumphost_from_form:
        env.protocol_jumphost = jumphost_from_form
    env.mongo_tls_enabled = bool(raw.get("mongo_tls_enabled", False))
    env.redis_tls_enabled = bool(raw.get("redis_tls_enabled", False))
    env.tier = "extended"
    mgr.save(env)

    # Flip the global tier before collecting secrets so the environment is in a
    # consistent Extended state even if credential entry is interrupted.
    _persist_tier("extended")

    console.print(
        f"\n  [{theme.success}]✓ '{env.name}' is now an Extended environment.[/{theme.success}]"
    )

    # Encrypted bundle: the Mongo/Redis/SSH secrets came with it, so write them
    # straight in (the Platform secret from Standard is untouched — the bundle
    # never carries it). Plain JSON: prompt interactively as before.
    if was_encrypted:
        try:
            _apply_bundle_credentials(env, bundle_secrets, vault_connection, skip_platform=True)
        except KeyboardInterrupt:
            console.print(
                f"\n  [{theme.warning}]Credential setup stopped. The bundle was kept — re-run "
                f"[bold]tier upgrade --from-file {src}[/bold], or "
                f"[bold]config credentials[/bold] to set them manually.[/{theme.warning}]\n"
            )
            return 0
        except Exception as exc:  # pylint: disable=broad-except
            console.print(f"\n  [{theme.error}]Could not store credentials: {exc}[/{theme.error}]")
            console.print(
                f"  [{theme.warning}]The upgrade is complete and the bundle kept — run "
                f"[bold]platform-atlas config credentials[/bold] to finish.[/{theme.warning}]\n"
            )
            return 0
        if not keep_file:
            _shred_bundle_file(src)
    else:
        try:
            _collect_credentials_post_html_setup(env, skip_platform=True)
        except KeyboardInterrupt:
            console.print(
                f"\n  [{theme.warning}]Credential entry stopped. Run "
                f"[bold]platform-atlas config credentials[/bold] to finish adding your "
                f"MongoDB/Redis/SSH secrets.[/{theme.warning}]\n"
            )
            return 0

    _upgrade_next_steps()
    return 0


def _section_line(title: str, subtitle: str = "") -> None:
    """Lightweight section header for the guided upgrade steps."""
    console.print()
    line = f"[bold {theme.primary_glow}]{title}[/bold {theme.primary_glow}]"
    if subtitle:
        line += f"  [{theme.text_dim}]{subtitle}[/{theme.text_dim}]"
    console.print(line)
    console.print()


def _hint_line(text: str) -> None:
    """Dim helper line used inside the guided upgrade steps."""
    console.print(f"  [{theme.text_dim}]{text}[/{theme.text_dim}]")


def _persist_tier(new_tier: str) -> int:
    """Write ``tier`` to ~/.atlas/config.json and refresh in-memory state."""
    if not ATLAS_CONFIG_FILE.is_file():
        console.print(
            f"  [{theme.error}]No config file at {ATLAS_CONFIG_FILE}. "
            f"Run 'platform-atlas config init' first.[/{theme.error}]"
        )
        return 1

    try:
        with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
    except Exception as exc:
        console.print(f"  [{theme.error}]Could not read config: {exc}[/{theme.error}]")
        return 1

    # The effective (displayed) tier is what the context resolved — it may
    # differ from config.json when an environment overlay overrides it.
    effective_old_tier = ctx().tier
    config_old_tier = data.get("tier", "extended")

    if effective_old_tier == new_tier:
        console.print(
            f"  [{theme.text_dim}]Tier already set to '{new_tier}'.[/{theme.text_dim}]"
        )
        return 0

    data["tier"] = new_tier
    try:
        atomic_write_json(ATLAS_CONFIG_FILE, data)
    except Exception as exc:
        console.print(f"  [{theme.error}]Could not write config: {exc}[/{theme.error}]")
        return 1

    # If the active environment has its own tier field it takes precedence over
    # config.json (env overlay is priority 3, config.json is priority 4).
    # Update the env file too so the change isn't silently overridden on reload.
    active_env = ctx().config.active_environment
    if active_env:
        try:
            from platform_atlas.core.environment import get_environment_manager
            mgr = get_environment_manager()
            if mgr.exists(active_env):
                env = mgr.load(active_env)
                if env.tier == "saas":
                    # SaaS envs bind tier at create time — never converted.
                    # The new default still applies to non-SaaS environments.
                    console.print(
                        f"  [{theme.text_dim}]Active environment '{active_env}' is a "
                        f"SaaS audit — its tier stays fixed; the new default applies "
                        f"to other environments.[/{theme.text_dim}]"
                    )
                elif env.tier:
                    env.tier = new_tier
                    mgr.save(env)
        except Exception as exc:
            logger.debug("Could not propagate tier to active environment: %s", exc)

    old_display = effective_old_tier
    console.print(
        f"\n  [{theme.success}]✓ Tier updated:[/{theme.success}] "
        f"[bold]{old_display}[/bold] → [bold]{new_tier}[/bold]\n"
        f"  [{theme.text_dim}]Run [bold]platform-atlas tier show[/bold] to verify.[/{theme.text_dim}]\n"
    )
    return 0
