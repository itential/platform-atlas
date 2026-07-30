# pylint: disable=line-too-long
"""
Dispatch Handler ::: Config
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any


from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax

from platform_atlas.core.registry import registry
from platform_atlas.core.context import ctx
from platform_atlas.core.init_setup import get_qstyle, ask_secret, mask
from platform_atlas.core.config import load_config_safe
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.core.json_utils import load_json
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.theme import THEME_REGISTRY, get_theme_by_id, list_theme_ids
from platform_atlas.core.credentials import (
    credential_store,
    scoped_service_name,
    active_secret_store,
    FileStoreHealth,
    CredentialKey,
    CredentialError,
    reset_credential_store,
    verify_keyring_backend,
    applicable_keys,
)
from platform_atlas.core import ui

import re as _re
from dataclasses import dataclass as _dataclass

@_dataclass
class DoctorRow:
    """One row in the config-doctor results."""
    id: str
    label: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    suggest: str

    @classmethod
    def from_tuple(cls, t: tuple[str, str, str, str]) -> "DoctorRow":
        label, status, detail, suggest = t
        row_id = _re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        return cls(id=row_id, label=label, status=status, detail=detail, suggest=suggest or "")


console = Console()
theme = ui.theme
MASK = "••••••••••••••••••"

SENSITIVE_PATTERNS: set[str] = {
    "secret",
    "password",
    "token",
    "uri",
    "api_key",
}

URI_FIELDS: set[str] = {"mongo_url", "redis_uri", "platform_uri"}

def _is_sensitive(field_name: str) -> bool:
    """Check if a field name matches any sensitve pattern"""
    name_lower = field_name.lower()
    return any(pattern in name_lower for pattern in SENSITIVE_PATTERNS)

def _mask_value(field_name: str, value: Any) -> str:
    """Return a masted representation of a sensitive value"""
    if value is None:
        return "null"

    str_val = str(value)

    if field_name in URI_FIELDS and "://" in str_val:
        scheme, _, _ = str_val.partition("://")
        return f"{scheme}://{MASK}"

    return MASK

@registry.register("config", "show", description="Pretty-print the Atlas config with sensitive values masked")
def config_show(args: Namespace) -> int:
    """Pretty-print the Atlas config with sensitive values masked"""
    success, err = load_config_safe(ATLAS_CONFIG_FILE)
    if not success:
        console.print(f"[{theme.error}]{err}[/{theme.error}]")
        return 1

    path = Path(ATLAS_CONFIG_FILE)
    data: dict[str, Any] = load_json(path)

    # Build redacted copy for display
    display_data = {}
    for key, value in data.items():
        if not args.full and _is_sensitive(key) and value is not None:
            display_data[key] = _mask_value(key, value)
        else:
            display_data[key] = value

    # Render as syntax-highlighted JSON
    formatted = json.dumps(display_data, indent=4, default=str, ensure_ascii=False)
    config_syntax = Syntax(formatted, "json", theme="monokai", line_numbers=False)

    console.print(Panel(
        config_syntax,
        title=f"[bold {theme.primary_glow}]Atlas Config[/] - {path}",
        border_style=theme.border_primary,
        padding=(1, 2),
    ))

    # Show active environment info if applicable
    config = ctx().config
    if config.active_environment:
        try:
            from platform_atlas.core.environment import get_environment_manager
            mgr = get_environment_manager()
            env = mgr.load(config.active_environment)

            env_data = env.to_dict()
            if not args.full:
                for key in list(env_data.keys()):
                    if _is_sensitive(key) and env_data[key] is not None:
                        env_data[key] = _mask_value(key, env_data[key])

            env_formatted = json.dumps(env_data, indent=4, default=str, ensure_ascii=False)
            env_syntax = Syntax(env_formatted, "json", theme="monokai", line_numbers=False)

            console.print(Panel(
                env_syntax,
                title=f"[bold {theme.accent}]Active Environment[/] - {env.file_path}",
                border_style=theme.accent,
                padding=(1, 2),
            ))
        except Exception:
            pass

    # Show deployment topology if present
    merged_data = data.copy()
    if config.active_environment:
        try:
            from platform_atlas.core.environment import get_environment_manager
            mgr = get_environment_manager()
            env = mgr.load(config.active_environment)
            merged_data.update(env.as_config_overlay())
        except Exception:
            pass

    if "deployment" in merged_data:
        from platform_atlas.core.topology import DeploymentTopology
        topology = DeploymentTopology.from_dict(merged_data["deployment"])
        scope = merged_data["deployment"].get("capture_scope", "primary_only")
        from platform_atlas.core.init_setup import _display_topology_review
        _display_topology_review(topology, capture_scope=scope)

    if not args.full:
        console.print(
            f"[{theme.text_dim}]Sensitive values are masked. "
            f"Use [{theme.secondary}]--full[/{theme.secondary}] to display actual values.[/{theme.text_dim}]"
            )
    return 0

@registry.register("config", "deployment", description="Reconfigure deployment topology")
def handle_config_deployment(args: Namespace) -> int:
    from platform_atlas.core.init_setup import ask_deployment

    config = ctx().config
    new_deployment, k8s_meta = ask_deployment()

    # If an environment is active, write to the environment file
    if config.active_environment:
        from platform_atlas.core.environment import get_environment_manager
        mgr = get_environment_manager()
        env = mgr.load(config.active_environment)
        env.deployment = new_deployment
        # Persist Kubernetes metadata alongside the topology so K8s-only
        # fields don't get dropped on a topology reconfigure.
        if k8s_meta:
            if "values_yaml_path" in k8s_meta:
                env.values_yaml_path = k8s_meta.get("values_yaml_path", "")
            if "iag5_values_yaml_path" in k8s_meta:
                env.iag5_values_yaml_path = k8s_meta.get("iag5_values_yaml_path", "")
            if "kubectl_context" in k8s_meta:
                env.kubectl_context = k8s_meta.get("kubectl_context", "")
            if "kubectl_namespace" in k8s_meta:
                env.kubectl_namespace = k8s_meta.get("kubectl_namespace", "")
            if "use_kubectl" in k8s_meta:
                env.use_kubectl = bool(k8s_meta.get("use_kubectl", False))
        mgr.save(env)
        console.print(
            f"\n[{theme.success}]✓[/{theme.success}] Deployment topology updated "
            f"in environment [{theme.accent}]{config.active_environment}[/{theme.accent}]"
        )
    else:
        # Legacy mode: write to config.json
        raw_config = load_json(ATLAS_CONFIG_FILE)
        raw_config["deployment"] = new_deployment
        atomic_write_json(ATLAS_CONFIG_FILE, raw_config)
        console.print(f"\n[{theme.success}]✓[/{theme.success}] Deployment topology updated")

    return 0

def _edit_theme() -> int:
    """Interactive theme switcher — called from config edit."""
    import questionary

    config = ctx().config
    current_id = config.theme
    theme_ids = list_theme_ids()

    # Show available themes with preview swatches
    ui.console.print()
    ui.console.print(f"[bold]Available Themes[/bold]")
    ui.console.print()

    for tid in theme_ids:
        t = get_theme_by_id(tid)
        marker = f"[{t.success}]✓[/{t.success}]" if tid == current_id else " "
        swatch = (
            f"[{t.primary}]██[/{t.primary}]"
            f"[{t.secondary}]██[/{t.secondary}]"
            f"[{t.accent}]██[/{t.accent}]"
            f"[{t.success}]██[/{t.success}]"
            f"[{t.error}]██[/{t.error}]"
            f"[{t.warning}]██[/{t.warning}]"
            f"[{t.info}]██[/{t.info}]"
        )
        label = f"[bold {t.primary}]{tid}[/bold {t.primary}]"
        ui.console.print(f"   {marker} {label:<40} {swatch}")

    ui.console.print()

    # Let user pick
    choices = []
    for tid in theme_ids:
        suffix = " (active)" if tid == current_id else ""
        choices.append(questionary.Choice(title=f"{tid}{suffix}", value=tid))

    selected = questionary.select(
        "Select a theme:",
        choices=choices,
        default=current_id if current_id in theme_ids else theme_ids[0],
        style=get_qstyle(),
    ).ask()

    if selected is None:
        ui.console.print(f"[{theme.text_dim}]Cancelled[/{theme.text_dim}]")
        return 1

    if selected == current_id:
        ui.console.print(f"[{theme.text_dim}]Already using {selected}[/{theme.text_dim}]")
        return 1

    if selected not in THEME_REGISTRY:
        ui.console.print(f"[bold {theme.error}]Unknown theme: {selected}[/bold {theme.error}]")
        return 1

    raw_config = load_json(ATLAS_CONFIG_FILE)
    raw_config["theme"] = selected
    atomic_write_json(ATLAS_CONFIG_FILE, raw_config)

    new_theme = get_theme_by_id(selected)
    ui.console.print()
    ui.console.print(
        f"[{new_theme.success}]✓[/{new_theme.success}] "
        f"Theme set to [{new_theme.primary}]"
        f"{selected}[/{new_theme.primary}]"
    )
    ui.console.print(f"[{new_theme.text_dim}]Takes effect on next run[/{new_theme.text_dim}]")
    return 0

def _switch_credential_backend(target: str) -> int:
    """`config credentials --use-file-store|--use-keyring` — explicitly switch
    this environment's credential backend between the OS keyring and the
    encrypted local file.

    No auto-anything: it saves the choice and re-resolves the store. Secrets are
    NOT copied between stores — re-run `config credentials` to enter them into
    the new backend.

    Switching INTO the OS keyring is guarded: an unusable keyring is refused
    outright (the switch would record fine and then fail on the next credential
    read/write — exactly the broken state to avoid), and a functional-but-
    unencrypted keyring is allowed with a warning. The encrypted file store
    works on any host, so `--use-file-store` needs no such guard.
    """
    if target == "keyring":
        is_secure, is_functional, backend_name = verify_keyring_backend()
        if not is_functional:
            console.print()
            console.print(Panel(
                f"[bold {theme.error}]Can't switch to the OS keyring — it isn't usable on "
                f"this host.[/bold {theme.error}]\n\n"
                f"[{theme.text_primary}]Detected backend: {backend_name} (not functional)\n\n"
                f"Switching would record the choice and then fail on the next credential\n"
                f"read or write, so the backend was left unchanged.\n\n"
                f"Use the encrypted local file instead — it works on any host:\n"
                f"  platform-atlas config credentials --use-file-store[/{theme.text_primary}]",
                border_style=theme.error,
                box=box.ROUNDED,
                expand=False,
            ))
            console.print()
            return 1
        if not is_secure:
            console.print()
            console.print(
                f"  [{theme.warning}]⚠ {backend_name} works but stores secrets "
                f"UNENCRYPTED.[/{theme.warning}]")
            console.print(f"  [{theme.text_dim}]For encrypted storage instead, run:[/{theme.text_dim}]")
            console.print(
                f"  [{theme.text_dim}]  platform-atlas config credentials "
                f"--use-file-store[/{theme.text_dim}]")
    _persist_config_value("credential_backend", target)
    _persist_config_value("use_file_store", False)  # retire the deprecated flag
    reset_credential_store()
    store = credential_store()
    if target == "file":
        label = "encrypted local file"
        where = ("Credentials are stored encrypted and machine-bound at\n"
                 "  ~/.atlas/credentials.enc   (key salt: ~/.atlas/.keysalt)\n\n")
    else:
        label = "OS keyring"
        where = "Credentials are stored in this host's OS keyring.\n\n"
    console.print()
    console.print(Panel(
        f"[bold {theme.success}]Credential backend set to the {label}.[/bold {theme.success}]\n\n"
        f"[{theme.text_primary}]{where}"
        f"Atlas will use this store from now on — nothing auto-switches. Secrets are\n"
        f"not copied between stores; re-run\n"
        f"  platform-atlas config credentials\n"
        f"to (re)enter your credentials into the {label}.[/{theme.text_primary}]",
        border_style=theme.success,
        box=box.ROUNDED,
        expand=False,
    ))
    console.print(f"  [{theme.text_dim}]Backend: {store.backend_name}[/{theme.text_dim}]")
    console.print()
    return 0


def _tier_credential_keys() -> list[CredentialKey]:
    """Credential keys relevant to the active tier.

    Keys outside the active tier's applicable set are hidden — MongoDB/Redis/
    SSH in Standard, Platform/MongoDB/Redis in SaaS. They are not used there,
    and the tier-aware store refuses to write them anyway, so offering them
    in the credentials UI would only confuse the user and then error out.
    """
    usable = applicable_keys()
    return [k for k in CredentialKey if k in usable]


@registry.register("config", "credentials", description="View and update credentials")
def handle_config_credentials(args: Namespace) -> int:
    """View and update credentials in the active backend."""
    import questionary
    from platform_atlas.core.credentials import CredentialError

    # Explicit backend switch (non-interactive — applies and exits).
    if getattr(args, "cred_use_file_store", False):
        return _switch_credential_backend("file")
    if getattr(args, "cred_use_keyring", False):
        return _switch_credential_backend("keyring")

    # --- Attempt to initialize the credential store ---
    # Vault backend connects eagerly, so stale/invalid AppRole credentials
    # will raise here before the user gets a chance to update them.
    try:
        store = credential_store()
    except (CredentialError, Exception) as e:
        # Determine if we're in Vault mode (config says vault but connection failed)
        is_vault_mode = False
        try:
            from platform_atlas.core.config import get_config
            cfg = get_config()
            is_vault_mode = cfg.credential_backend == "vault"
        except Exception:
            pass

        if is_vault_mode:
            console.print()
            console.print(
                f"[bold {theme.primary_glow}]Credential Store[/bold {theme.primary_glow}]"
                f"  [{theme.text_dim}]Backend: HashiCorp Vault[/{theme.text_dim}]"
            )
            console.print()
            console.print(
                f"  [{theme.error}]✗ Vault connection failed:[/{theme.error}] {e}"
            )
            console.print(
                f"  [{theme.text_dim}]This usually means your AppRole credentials "
                f"or token have changed.[/{theme.text_dim}]"
            )
            console.print()

            update = questionary.confirm(
                "Update Vault connection settings?",
                default=True,
                style=get_qstyle(),
            ).ask()

            if update is None or not update:
                return 1

            return _handle_vault_connection_update()

        # Non-Vault error — re-raise, something else is wrong
        raise

    # Show which environment credentials are scoped to
    env_label = ""
    if store.env_name:
        env_label = f"  [{theme.accent}]env: {store.env_name}[/{theme.accent}]"

    # --- Backend header and security check ---
    if store.is_vault:
        console.print()
        console.print(
            f"[bold {theme.primary_glow}]Credential Store[/bold {theme.primary_glow}]"
            f"  [{theme.text_dim}]Backend: {store.backend_name}[/{theme.text_dim}]{env_label}"
        )
    else:
        # Local-store mode. The substrate is the chosen one — the OS keyring or
        # the encrypted local file. The file store is a valid, non-fatal state.
        if active_secret_store().is_file:
            file_store = active_secret_store()
            file_health = file_store.health()
            if file_health == FileStoreHealth.UNREADABLE:
                console.print(Panel(
                    f"[bold {theme.error}]Your encrypted local credential file can't be opened.[/bold {theme.error}]\n\n"
                    f"[{theme.text_primary}]The file (~/.atlas/credentials.enc) or its key (~/.atlas/.keysalt) was\n"
                    f"changed, removed, or moved from another machine, so it can't be decrypted on\n"
                    f"this host. Nothing here can't be re-entered — Atlas can start fresh: it will\n"
                    f"create a brand-new encrypted file and you simply re-enter your credentials.[/{theme.text_primary}]",
                    border_style=theme.error,
                    box=box.ROUNDED,
                    expand=False,
                ))
                recreate = questionary.confirm(
                    "Create a fresh encrypted credential file and re-enter credentials now?",
                    default=True,
                    style=get_qstyle(),
                ).ask()
                if recreate is None:
                    raise KeyboardInterrupt
                if not recreate:
                    console.print(
                        f"  [{theme.text_dim}]No changes made — re-run "
                        f"'platform-atlas config credentials' when ready.[/{theme.text_dim}]"
                    )
                    return 0
                file_store.reset()
                console.print(
                    f"  [{theme.success}]✓ Cleared the old file — a new encrypted file will be created "
                    f"as you enter credentials below.[/{theme.success}]"
                )
            elif file_health == FileStoreHealth.EMPTY:
                console.print(Panel(
                    f"[bold {theme.warning}]Encrypted local file credential store — no credentials saved yet.[/bold {theme.warning}]\n\n"
                    f"[{theme.text_primary}]You chose the encrypted local file backend, so credentials are stored\n"
                    f"encrypted and machine-bound at ~/.atlas/credentials.enc. Add them below and\n"
                    f"the encrypted file is created automatically.[/{theme.text_primary}]",
                    border_style=theme.warning,
                    box=box.ROUNDED,
                    expand=False,
                ))
            else:
                console.print(Panel(
                    f"[bold {theme.warning}]Using the encrypted local file credential store.[/bold {theme.warning}]\n\n"
                    f"[{theme.text_primary}]You chose the encrypted local file backend, so credentials are stored\n"
                    f"encrypted and machine-bound at ~/.atlas/credentials.enc.\n"
                    f"  • Protects against stolen disks/backups and casual inspection\n"
                    f"  • Run 'platform-atlas config credentials --use-keyring' to switch to the OS keyring[/{theme.text_primary}]",
                    border_style=theme.warning,
                    box=box.ROUNDED,
                    expand=False,
                ))
        else:
            # Keyring substrate: verify it is secure
            is_secure, is_functional, backend = verify_keyring_backend()
            if not is_functional:
                # You chose the OS keyring but it can't store a secret here.
                # Point at the encrypted file instead.
                console.print(Panel(
                    f"[bold {theme.error}]No usable credential store on this system: {backend}[/bold {theme.error}]\n\n"
                    f"[{theme.text_primary}]Run 'platform-atlas config credentials --use-file-store' to store\n"
                    f"credentials in an encrypted local file instead.[/{theme.text_primary}]",
                    border_style=theme.error,
                    box=box.ROUNDED,
                    expand=False,
                ))
                return 1
            if not is_secure:
                console.print(Panel(
                    f"[bold {theme.warning}]Unencrypted keyring backend active: {backend}[/bold {theme.warning}]\n\n"
                    f"[{theme.text_primary}]Credentials will be stored without encryption.\n"
                    f"  • Linux: install gnome-keyring for encrypted Secret Service storage\n"
                    f"  • Or run 'platform-atlas config credentials --use-file-store' for an encrypted local file\n"
                    f"  • Any platform: configure HashiCorp Vault as the credential backend[/{theme.text_primary}]",
                    border_style=theme.warning,
                    box=box.ROUNDED,
                    expand=False,
                ))

        console.print()
        console.print(
            f"[bold {theme.primary_glow}]Credential Store[/bold {theme.primary_glow}]"
            f"  [{theme.text_dim}]Backend: {store.backend_name}[/{theme.text_dim}]{env_label}"
        )

    # --- Status table (identical for both backends) ---
    console.print()

    status_table = Table(show_header=True, box=box.SIMPLE_HEAVY, pad_edge=True)
    status_table.add_column("Credential", style=f"bold {theme.text_primary}", min_width=24)
    status_table.add_column("Status", justify="center", min_width=12)
    status_table.add_column("Preview", style=theme.text_dim, min_width=20)

    for key in _tier_credential_keys():
        value = store.get(key)
        if value:
            badge = f"[{theme.success}]✓ Stored[/{theme.success}]"
            preview = mask(value, keep=8) if len(value) > 12 else mask(value)
        else:
            badge = f"[{theme.error}]✗ Missing[/{theme.error}]"
            if store.is_read_only:
                # Vault backend — show the key name so users know what to create
                preview = f"[{theme.warning}]key: {key.value}[/{theme.warning}]"
            else:
                preview = ""
        status_table.add_row(key.display_name, badge, preview)

    console.print(status_table)
    console.print()

    # --- Vault mode: secrets are read-only, but connection settings are updatable ---
    if store.is_read_only:
        vault_backend = store._backend  # Access for path info
        vault_path = ""
        if hasattr(vault_backend, "config"):
            vault_path = vault_backend.config.full_path

        console.print(
            f"  [{theme.text_dim}]Vault secrets are read-only — "
            f"manage secrets directly in Vault[/{theme.text_dim}]"
        )
        if vault_path:
            console.print(
                f"  [{theme.text_dim}]Path: {vault_path}[/{theme.text_dim}]"
            )

            # Show missing keys with the full Vault path for easy copy-paste
            missing_keys = [key for key in _tier_credential_keys() if not store.get(key)]
            if missing_keys:
                console.print(
                    f"\n  [{theme.warning}]Missing Vault keys "
                    f"(create these at the path above):[/{theme.warning}]"
                )
                for key in missing_keys:
                    console.print(
                        f"    [{theme.text_dim}]{vault_path}[/{theme.text_dim}]"
                        f" → [{theme.warning}]{key.value}[/{theme.warning}]"
                    )
        console.print()

        # --- Vault connection settings (stored in OS keyring, always editable) ---
        action = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice(
                    "Update Vault connection  — Re-enter URL, token, AppRole credentials, or wrapping token",
                    value="update_vault",
                ),
                questionary.Choice(
                    "Verify Vault secrets     — Check which secrets Vault currently has",
                    value="verify",
                ),
                questionary.Choice("Done", value="done"),
            ],
            style=get_qstyle(),
        ).ask()

        if action is None or action == "done":
            return 0

        if action == "update_vault":
            return _handle_vault_connection_update()

        if action == "verify":
            _display_vault_secret_status(store)

        console.print()
        return 0

    # --- Keyring mode: interactive update/delete loop (existing behavior) ---
    while True:
        action = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("Update a credential", value="update"),
                questionary.Choice("Delete a credential", value="delete"),
                questionary.Choice("Done", value="done"),
            ],
            style=get_qstyle(),
        ).ask()

        if action is None or action == "done":
            break

        # Pick which credential
        cred_choices = [
            questionary.Choice(key.display_name, value=key)
            for key in _tier_credential_keys()
        ]
        selected = questionary.select(
            "Which credential?",
            choices=cred_choices,
            style=get_qstyle(),
        ).ask()

        if selected is None:
            continue

        if action == "update":
            new_value = ask_secret(f"New value for {selected.display_name}")
            if not new_value:
                console.print(f"  [{theme.warning}]Skipped (empty value)[/{theme.warning}]")
                continue

            store.set(selected, new_value)
            console.print(f"  [{theme.success}]✓ {selected.display_name} updated[/{theme.success}]\n")

        elif action == "delete":
            confirm = questionary.confirm(
                f"Delete {selected.display_name} from keyring?",
                default=False,
                style=get_qstyle(),
            ).ask()

            if confirm:
                store.delete(selected)
                console.print(f"  [{theme.warning}]✓ {selected.display_name} deleted[/{theme.warning}]\n")
            else:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]\n")

    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Vault Helpers (config credentials subcommand)
# ═══════════════════════════════════════════════════════════════════════════

def _handle_vault_connection_update() -> int:
    """Re-enter and validate Vault connection settings, then save to OS keyring."""
    from platform_atlas.core.init_setup import ask_vault_settings
    from platform_atlas.core.config import get_config
    from platform_atlas.core.credentials import VaultBackend

    # Determine the scoped keyring service for the active environment
    try:
        cfg = get_config()
        service = scoped_service_name(cfg.active_environment)
    except Exception:
        service = scoped_service_name(None)

    vault_config = ask_vault_settings()

    # Test connection before saving — don't overwrite working settings with bad ones
    console.print(f"\n  [{theme.text_dim}]Testing Vault connection...[/{theme.text_dim}]")
    try:
        test_backend = VaultBackend(vault_config, service=service)
        console.print(
            f"  [{theme.success}]✓ Connected to Vault at "
            f"{vault_config.url}[/{theme.success}]"
        )
        if test_backend.token_ttl > 0:
            ttl_label = (f"{test_backend.token_ttl // 60}m {test_backend.token_ttl % 60}s"
                         + (" (renewable)" if test_backend.token_renewable else " (not renewable)"))
            console.print(f"  [{theme.text_dim}]Token TTL: {ttl_label}[/{theme.text_dim}]")
    except Exception as e:
        console.print(
            f"  [{theme.error}]✗ Connection failed: {e}[/{theme.error}]"
        )
        console.print(
            f"  [{theme.text_dim}]Vault connection settings were NOT saved.[/{theme.text_dim}]"
        )
        return 1

    # Save validated connection settings to the scoped keyring namespace
    VaultBackend.save_config_to_keyring(vault_config, service=service)
    console.print(
        f"  [{theme.success}]✓ Vault connection settings updated in OS keyring[/{theme.success}]"
    )

    # Reset the singleton so subsequent calls pick up the new config
    reset_credential_store()

    # Show updated credential status from the new Vault connection
    console.print()
    new_store = credential_store()
    _display_vault_secret_status(new_store)

    console.print()
    return 0


def _display_vault_secret_status(store) -> None:
    """Print a status line for each CredentialKey showing whether Vault has it."""
    console.print(f"  [{theme.text_dim}]Checking Vault for secrets...[/{theme.text_dim}]")
    for key in CredentialKey:
        found = store.exists(key)
        if found:
            console.print(
                f"    [{theme.success}]✓[/{theme.success}] "
                f"{key.display_name} ({key.value})"
            )
        else:
            console.print(
                f"    [{theme.error}]✗[/{theme.error}] "
                f"{key.display_name} ({key.value})"
            )

@registry.register("config", "architecture", description="Collect or update architecture data")
def handle_config_architecture(args: Namespace) -> int:
    from platform_atlas.capture.collectors.manual import run_architecture_collection
    run_architecture_collection()
    return 0


def probe_platform_url(config) -> tuple[str, str, str, str]:
    """Single Platform URL reachability check (~3s TCP timeout)."""
    import socket
    from urllib.parse import urlparse as _urlparse

    if not config.platform_uri:
        return (
            "Platform URL", "warn",
            "no platform_uri configured",
            "Run `platform-atlas env edit` to set it.",
        )
    parsed = _urlparse(config.platform_uri)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=3):
            return (
                "Platform URL", "ok",
                f"{config.platform_uri} (TCP {host}:{port} reachable)",
                "",
            )
    except OSError as exc:
        return (
            "Platform URL", "warn",
            f"{config.platform_uri} — {exc}",
            "Confirm the URL, network access, and any required VPN.",
        )


def probe_gateway4_url(config) -> tuple[str, str, str, str] | None:
    """Single Gateway4 URL reachability check (~3s TCP timeout).

    Returns None when no Gateway4 URI is configured (it's optional).
    """
    import socket
    from urllib.parse import urlparse as _urlparse

    if not config.gateway4_uri:
        return None
    parsed = _urlparse(config.gateway4_uri)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=3):
            return (
                "Gateway4 URL", "ok",
                f"{config.gateway4_uri} (TCP reachable)",
                "",
            )
    except OSError as exc:
        return (
            "Gateway4 URL", "warn",
            f"{config.gateway4_uri} — {exc}",
            "Verify the URL or run `platform-atlas env edit`.",
        )


def collect_doctor_rows(
    *, skip_url_probes: bool = False, show_spinner: bool = False,
) -> tuple[list[DoctorRow], str | None, str | None]:
    """Build the config-doctor result rows without rendering them.

    Returns ``(rows, env_name, tier)`` where each row is a ``DoctorRow``
    with status one of ``"ok" | "warn" | "fail"``. Shared between the CLI
    handler and the WebUI ``/config/doctor`` route so both surfaces stay in
    lockstep.

    When ``skip_url_probes`` is ``True``, the Platform / Gateway4 URL
    reachability rows are omitted — the WebUI uses this for an instant
    initial render and htmx-streams the URL rows in via separate
    endpoints (see :func:`probe_platform_url` / :func:`probe_gateway4_url`).
    """
    import os
    import shutil
    import sys as _sys
    from pathlib import Path as _P

    from platform_atlas.core.paths import (
        ATLAS_CONFIG_FILE, ATLAS_ENVIRONMENTS_DIR, ATLAS_HOME,
    )

    rows: list[DoctorRow] = []

    # ── Global config ─────────────────────────────────────────────
    cfg_path = _P(ATLAS_CONFIG_FILE)
    if not cfg_path.is_file():
        rows.append(DoctorRow.from_tuple((
            "Config file", "fail",
            f"not found at {cfg_path}",
            "Run `platform-atlas config init` to create one.",
        )))
        return rows, None, None

    mode = cfg_path.stat().st_mode & 0o777
    if os.name == "posix" and mode & 0o077:
        rows.append(DoctorRow.from_tuple((
            "Config file", "warn",
            f"{cfg_path} (chmod {oct(mode)} — should be 600)",
            f"Run `chmod 600 {cfg_path}` to tighten permissions.",
        )))
    else:
        rows.append(DoctorRow.from_tuple(("Config file", "ok", str(cfg_path), "")))

    # ── Python version ────────────────────────────────────────────
    vi = _sys.version_info
    py_ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 11):
        rows.append(DoctorRow.from_tuple(("Python version", "ok", f"{py_ver} (supported)", "")))
    else:
        rows.append(DoctorRow.from_tuple((
            "Python version", "fail",
            f"{py_ver} is below the 3.11 minimum",
            "Install Python 3.11+ and reinstall Platform Atlas in that interpreter.",
        )))

    # ── Python binary path (informational) ────────────────────────
    rows.append(DoctorRow.from_tuple(("Python binary", "ok", _sys.executable, "")))

    # ── Available disk space ──────────────────────────────────────
    probe_dir = _P(ATLAS_HOME) if _P(ATLAS_HOME).exists() else _P.home()
    try:
        free_bytes = shutil.disk_usage(str(probe_dir)).free
        free_gb = free_bytes / (1024 ** 3)
        if free_gb >= 1000:
            free_val = f"{free_gb / 1024:.2f} TB free"
        else:
            free_val = f"{free_gb:.2f} GB free"
        detail = f"{free_val} at {probe_dir}"
        if free_gb >= 5:
            rows.append(DoctorRow.from_tuple(("Available disk space", "ok", detail, "")))
        elif free_gb >= 0.5:
            rows.append(DoctorRow.from_tuple((
                "Available disk space", "warn",
                detail,
                "Capture + report artifacts can use ~100 MB per session — free up space.",
            )))
        else:
            rows.append(DoctorRow.from_tuple((
                "Available disk space", "fail",
                detail,
                "Free at least 500 MB before running capture / report.",
            )))
    except OSError as exc:
        rows.append(DoctorRow.from_tuple((
            "Available disk space", "warn",
            f"could not stat {probe_dir}: {exc}",
            "",
        )))

    # ── Environment file ──────────────────────────────────────────
    config = ctx().config
    env_name = config.active_environment
    env_path: _P | None = None
    if env_name:
        env_path = _P(ATLAS_ENVIRONMENTS_DIR) / f"{env_name}.json"
        if env_path.is_file():
            rows.append(DoctorRow.from_tuple((f"Environment '{env_name}'", "ok", str(env_path), "")))
        else:
            rows.append(DoctorRow.from_tuple((
                f"Environment '{env_name}'", "fail",
                f"file missing at {env_path}",
                "Run `platform-atlas env create` to recreate it.",
            )))
    else:
        rows.append(DoctorRow.from_tuple((
            "Active environment", "warn",
            "no environment is active",
            "Run `platform-atlas env list` then `platform-atlas env switch <name>`.",
        )))

    # ── Tier ──────────────────────────────────────────────────────
    tier = config.tier
    rows.append(DoctorRow.from_tuple(("Tier", "ok", tier, "")))

    # Does this tier talk to the Platform at all? SaaS audits a single gateway
    # with no Platform/Mongo/Redis, so the Platform credential + URL checks below
    # don't apply and would otherwise false-flag a perfectly healthy SaaS setup.
    # Resolve through the credential store's own per-tier key sets so the doctor,
    # the store, and capture all agree on what "uses Platform" means.
    platform_used = CredentialKey.PLATFORM_SECRET in applicable_keys(tier)

    # ── Credential backend ────────────────────────────────────────
    try:
        _store = credential_store()
        _using_vault = _store.is_vault
        _store_is_file = active_secret_store().is_file
        _file_unreadable = _store_is_file and active_secret_store().health() == FileStoreHealth.UNREADABLE

        if _using_vault:
            # Vault mode: the connection settings (URL/token) live in the local
            # secret store chosen for this env — OS keyring or the encrypted file.
            if _file_unreadable:
                rows.append(DoctorRow.from_tuple((
                    "Vault settings store", "fail",
                    "Encrypted local file — UNREADABLE (~/.atlas/credentials.enc)",
                    "File or key (~/.atlas/.keysalt) missing/changed — run 'config credentials' to recreate.",
                )))
            elif _store_is_file:
                # Deliberate choice (vault_secret_store = file). The OS keyring is
                # NOT implicated — don't warn about a setting the user picked.
                rows.append(DoctorRow.from_tuple((
                    "Vault settings store", "ok",
                    "Encrypted local file (~/.atlas/credentials.enc)",
                    "",
                )))
            else:
                is_secure, is_functional, backend_name = verify_keyring_backend()
                if not is_functional:
                    rows.append(DoctorRow.from_tuple((
                        "OS Keyring backend", "fail",
                        f"{backend_name} is not functional",
                        "OS keyring stores Vault URL and token — must be functional.",
                    )))
                elif not is_secure:
                    rows.append(DoctorRow.from_tuple((
                        "OS Keyring backend", "warn",
                        f"{backend_name} (unencrypted, stores Vault token)",
                        "Switch to Secret Service / Keychain for encrypted token storage.",
                    )))
                else:
                    rows.append(DoctorRow.from_tuple((
                        "OS Keyring backend", "ok",
                        f"{backend_name} (stores Vault URL and token)",
                        "",
                    )))
            rows.append(DoctorRow.from_tuple((
                "Credential backend", "ok",
                "HashiCorp Vault (secrets stored in Vault KV)",
                "",
            )))
        elif _file_unreadable:
            rows.append(DoctorRow.from_tuple((
                "Credential backend", "fail",
                "Encrypted local file — UNREADABLE (~/.atlas/credentials.enc)",
                "File or key (~/.atlas/.keysalt) missing/changed — run 'config credentials' to recreate.",
            )))
        elif _store_is_file:
            # Deliberately chosen encrypted file backend. The OS keyring is NOT
            # implicated — this is the user's pick. Amber (never green) only
            # because the actual secrets at rest are less isolated than the
            # keyring; the hint is honest, not a false "keyring unavailable."
            rows.append(DoctorRow.from_tuple((
                "Credential backend", "warn",
                "Encrypted local file (~/.atlas/credentials.enc, machine-bound)",
                "Your selected backend — machine-bound & encrypted at rest. Use 'config credentials --use-keyring' for the OS keyring.",
            )))
        else:
            is_secure, is_functional, backend_name = verify_keyring_backend()
            if not is_functional:
                rows.append(DoctorRow.from_tuple((
                    "Credential backend", "fail",
                    f"{backend_name} is not functional",
                    "Run 'config credentials --use-file-store' for an encrypted local file, or configure Vault.",
                )))
            elif not is_secure:
                rows.append(DoctorRow.from_tuple((
                    "Credential backend", "warn",
                    f"{backend_name} (unencrypted)",
                    "Switch to Secret Service / Keychain / Vault, or use --use-file-store.",
                )))
            else:
                rows.append(DoctorRow.from_tuple(("Credential backend", "ok", backend_name, "")))
    except Exception as exc:
        rows.append(DoctorRow.from_tuple((
            "Credential backend", "fail",
            f"probe raised {type(exc).__name__}: {exc}",
            "Re-run with --debug for the full traceback.",
        )))

    # ── Platform secret ───────────────────────────────────────────
    # Only checked for platform-anchored tiers (standard/extended). SaaS never
    # uses a Platform Client Secret, so the row is omitted entirely rather than
    # reported as a missing credential.
    if platform_used:
        try:
            store = credential_store()
            if store.exists(CredentialKey.PLATFORM_SECRET):
                rows.append(DoctorRow.from_tuple(("Platform Client Secret", "ok", "stored", "")))
            else:
                rows.append(DoctorRow.from_tuple((
                    "Platform Client Secret", "fail",
                    "missing from credential store",
                    "Run `platform-atlas config credentials` to set it.",
                )))
        except CredentialError as exc:
            rows.append(DoctorRow.from_tuple((
                "Credential store", "fail",
                f"unavailable: {exc}",
                "Run `platform-atlas config credentials` to reconfigure.",
            )))

    # ── URL reachability (Platform + optional Gateway4) ───────────
    # Slow checks (~3s TCP timeout each) — the WebUI passes
    # skip_url_probes=True for an instant initial render and pulls these
    # in via separate htmx endpoints.
    if not skip_url_probes:
        try:
            _compat = ctx().config.compatibility_mode
        except Exception:
            _compat = True
        if show_spinner and not _compat:
            from rich.status import Status
            with console.status(f"[{theme.info}]Probing reachability…[/{theme.info}]", spinner="dots") as _s:
                if platform_used:
                    _s.update(f"[{theme.info}]Probing Platform OAuth URL…[/{theme.info}]")
                    rows.append(DoctorRow.from_tuple(probe_platform_url(config)))
                _s.update(f"[{theme.info}]Probing Gateway4 health endpoint…[/{theme.info}]")
                _gw4 = probe_gateway4_url(config)
                if _gw4 is not None:
                    rows.append(DoctorRow.from_tuple(_gw4))
        else:
            if platform_used:
                rows.append(DoctorRow.from_tuple(probe_platform_url(config)))
            _gw4 = probe_gateway4_url(config)
            if _gw4 is not None:
                rows.append(DoctorRow.from_tuple(_gw4))

    # ── Ruleset ──────────────────────────────────────────────────
    try:
        from platform_atlas.core.ruleset_manager import get_ruleset_manager
        rm = get_ruleset_manager()
        active_id = rm.get_active_ruleset_id()
        if active_id:
            meta = rm.get_metadata(active_id)
            rows.append(DoctorRow.from_tuple((
                "Active ruleset",
                "ok",
                f"{active_id} ({meta.rule_count} rules)",
                "",
            )))
        else:
            rows.append(DoctorRow.from_tuple((
                "Active ruleset",
                "warn",
                "no ruleset selected",
                "Run `platform-atlas ruleset list` and `ruleset set <id>`.",
            )))
    except Exception as exc:
        rows.append(DoctorRow.from_tuple((
            "Active ruleset",
            "warn",
            f"could not load: {type(exc).__name__}: {exc}",
            "",
        )))

    # ── SSH key path (Extended only) ─────────────────────────────
    if tier == "extended" and config.deployment:
        ssh_defaults = (config.deployment or {}).get("ssh_defaults", {})
        key_path = ssh_defaults.get("key_path", "")
        if key_path:
            p = _P(key_path).expanduser()
            if not p.is_file():
                rows.append(DoctorRow.from_tuple((
                    "SSH key path",
                    "fail",
                    f"{p} not found",
                    "Run `platform-atlas env edit` to point at the right key.",
                )))
            elif p.suffix == ".pub":
                rows.append(DoctorRow.from_tuple((
                    "SSH key path",
                    "fail",
                    f"{p} is a public key (.pub) — Atlas needs the private key",
                    "Pick the private key (the file without `.pub`).",
                )))
            else:
                rows.append(DoctorRow.from_tuple(("SSH key path", "ok", str(p), "")))
        else:
            rows.append(DoctorRow.from_tuple((
                "SSH key",
                "ok",
                "no key path — using ssh-agent",
                "",
            )))

    return rows, env_name, tier


@registry.register(
    "config", "doctor",
    description="Run a health check on the current Atlas configuration",
)
def handle_config_doctor(args: Namespace) -> int:
    """One-shot health check. Returns 0=ok, 1=warnings, 2=failures.

    With --json: emits structured JSON to stdout (nothing else).
    With --no-url-probes: skips the slow TCP reachability checks.
    """
    use_json: bool = getattr(args, "json", False)
    skip_probes: bool = getattr(args, "no_url_probes", False)

    rows, env_name, tier = collect_doctor_rows(
        skip_url_probes=skip_probes,
        show_spinner=(not use_json),
    )

    has_fail = any(r.status == "fail" for r in rows)
    has_warn = any(r.status == "warn" for r in rows)

    if use_json:
        from platform_atlas.core._version import __version__ as _ver
        import sys as _sys
        output = {
            "schema": "atlas-doctor/v1",
            "version": _ver,
            "env": env_name,
            "tier": tier,
            "summary": {
                "ok":   sum(1 for r in rows if r.status == "ok"),
                "warn": sum(1 for r in rows if r.status == "warn"),
                "fail": sum(1 for r in rows if r.status == "fail"),
            },
            "checks": [
                {
                    "id":      r.id,
                    "label":   r.label,
                    "status":  r.status,
                    "detail":  r.detail,
                    "suggest": r.suggest or None,
                }
                for r in rows
            ],
        }
        _sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
        _sys.stdout.write("\n")
    else:
        _render_doctor_rows(rows, env_name=env_name, tier=tier)

    if has_fail:
        return 2
    if has_warn:
        return 1
    return 0


# ── Config-doctor tree grouping ───────────────────────────────────
# collect_doctor_rows() feeds the CLI, the --json output, and the WebUI
# /config/doctor route, so it stays flat. Grouping for the CLI tree view
# lives here in the renderer. Rows are classified by id; ids are slugified
# labels (see DoctorRow.from_tuple) and a few are dynamic — e.g. the active
# environment row is "environment_<name>" — so we match those by prefix.
_DOCTOR_GROUP_ORDER: tuple[str, ...] = (
    "Runtime",
    "Environment & Tier",
    "Credentials",
    "Connectivity & Rules",
    "Other",
)

_DOCTOR_GROUP_BY_ID: dict[str, str] = {
    "python_version":         "Runtime",
    "python_binary":          "Runtime",
    "available_disk_space":   "Runtime",
    "config_file":            "Environment & Tier",
    "active_environment":     "Environment & Tier",
    "tier":                   "Environment & Tier",
    "os_keyring_backend":     "Credentials",
    "vault_settings_store":   "Credentials",
    "credential_backend":     "Credentials",
    "credential_store":       "Credentials",
    "platform_client_secret": "Credentials",
    "ssh_key_path":           "Credentials",
    "ssh_key":                "Credentials",
    "platform_url":           "Connectivity & Rules",
    "gateway4_url":           "Connectivity & Rules",
    "active_ruleset":         "Connectivity & Rules",
}

_DOCTOR_GLYPH: dict[str, str] = {"ok": "✓", "warn": "⚠", "fail": "✗"}
_DOCTOR_TAG:   dict[str, str] = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[fail]"}
_DOCTOR_LABEL_COL = 26


def _doctor_group_for(row: DoctorRow) -> str:
    """Map a doctor row to its display group, robust to dynamic ids."""
    if row.id.startswith("environment_"):
        return "Environment & Tier"
    return _DOCTOR_GROUP_BY_ID.get(row.id, "Other")


def _group_doctor_rows(rows: list[DoctorRow]) -> list[tuple[str, list[DoctorRow]]]:
    """Bucket rows into ordered (group, rows) pairs; drop empty groups and
    preserve the original row order within each group."""
    buckets: dict[str, list[DoctorRow]] = {name: [] for name in _DOCTOR_GROUP_ORDER}
    for row in rows:
        buckets[_doctor_group_for(row)].append(row)
    return [(name, buckets[name]) for name in _DOCTOR_GROUP_ORDER if buckets[name]]


def _doctor_status_color(status: str) -> str:
    return {"ok": theme.success, "warn": theme.warning}.get(status, theme.error)


def _doctor_counts(rows: list[DoctorRow]) -> dict[str, int]:
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def _doctor_summary_line(counts: dict[str, int]) -> str:
    return (
        f"Summary: {counts.get('ok', 0)} OK · "
        f"{counts.get('warn', 0)} warning(s) · "
        f"{counts.get('fail', 0)} error(s)"
    )


def _doctor_subtitle(
    counts: dict[str, int], *, env_name: str | None, tier: str | None,
) -> tuple[list[str], str, str]:
    """Return ``(chips, health_text, health_state)`` for the line under the title.

    ``chips`` are the env / tier labels; ``health_text`` is e.g. "12/12 healthy"
    or "10 OK · 1 warn · 1 fail"; ``health_state`` is "ok" | "warn" | "fail".
    """
    chips: list[str] = []
    if env_name:
        chips.append(env_name)
    if tier:
        chips.append(f"{tier} tier")
    total = sum(counts.values())
    ok, warn, fail = counts.get("ok", 0), counts.get("warn", 0), counts.get("fail", 0)
    if fail == 0 and warn == 0:
        return chips, f"{ok}/{total} healthy", "ok"
    bits = [f"{ok} OK"]
    if warn:
        bits.append(f"{warn} warn")
    if fail:
        bits.append(f"{fail} fail")
    return chips, " · ".join(bits), ("fail" if fail else "warn")


def _render_doctor_rows(
    rows: list[DoctorRow],
    *,
    env_name: str | None,
    tier: str | None,
) -> None:
    """Print the doctor results as a grouped tree (ASCII list in plain mode)."""
    counts = _doctor_counts(rows)
    groups = _group_doctor_rows(rows)
    if ui.is_plain_mode():
        _render_doctor_plain(groups, counts, env_name=env_name, tier=tier)
    else:
        _render_doctor_tree(groups, counts, env_name=env_name, tier=tier)
    console.print()
    console.print(f"[{theme.text_secondary}]{_doctor_summary_line(counts)}[/{theme.text_secondary}]")


def _render_doctor_tree(
    groups: list[tuple[str, list[DoctorRow]]],
    counts: dict[str, int],
    *,
    env_name: str | None,
    tier: str | None,
) -> None:
    """Rich Tree: title → env/health → groups → checks (suggestion hangs below)."""
    from rich.tree import Tree
    from rich.text import Text

    console.print()
    root = Tree(
        Text("Atlas Configuration Health Check", style=f"bold {theme.primary_glow}"),
        guide_style=theme.text_dim,
    )

    chips, health, state = _doctor_subtitle(counts, env_name=env_name, tier=tier)
    subtitle = Text()
    for chip in chips:
        subtitle.append(chip, style=theme.text_secondary)
        subtitle.append("  ·  ", style=theme.text_dim)
    subtitle.append(health, style=f"bold {_doctor_status_color(state)}")
    branch = root.add(subtitle)

    for gname, grp_rows in groups:
        gnode = branch.add(Text(gname, style=f"bold {theme.primary_glow}"))
        for row in grp_rows:
            color = _doctor_status_color(row.status)
            leaf = Text()
            leaf.append(f"{_DOCTOR_GLYPH.get(row.status, '•')} ", style=color)
            leaf.append(row.label, style=("bold" if row.status == "ok" else f"bold {color}"))
            pad = max(1, _DOCTOR_LABEL_COL - len(row.label))
            leaf.append(f" {'·' * pad} ", style=theme.text_dim)
            leaf.append(row.detail, style=theme.text_secondary)
            node = gnode.add(leaf)
            if row.status != "ok" and row.suggest:
                node.add(Text(f"↳ {row.suggest}", style=theme.text_dim))

    console.print(root)


def _render_doctor_plain(
    groups: list[tuple[str, list[DoctorRow]]],
    counts: dict[str, int],
    *,
    env_name: str | None,
    tier: str | None,
) -> None:
    """Pure-ASCII grouped list for plain/compatibility mode (no Tree, no glyphs)."""
    from rich.text import Text

    console.print()
    console.print(Text("Atlas Configuration Health Check"))
    chips, health, _state = _doctor_subtitle(counts, env_name=env_name, tier=tier)
    console.print(Text("  " + " - ".join([*chips, health])))
    console.print()

    for gname, grp_rows in groups:
        console.print(Text(gname))
        console.print(Text("-" * len(gname)))
        for row in grp_rows:
            tag = _DOCTOR_TAG.get(row.status, "[ ?? ]")
            pad = max(1, _DOCTOR_LABEL_COL - len(row.label))
            console.print(Text(f"  {tag} {row.label} {'.' * pad} {row.detail}"))
            if row.status != "ok" and row.suggest:
                console.print(Text(f"         -> {row.suggest}"))
        console.print()


@registry.register("config", "plain", description="Toggle plain/compatibility mode on or off")
def handle_config_plain(args: Namespace) -> int:
    """Enable or disable plain (compatibility) mode."""
    import questionary
    from platform_atlas.core.init_setup import get_qstyle

    current = ctx().config.compatibility_mode
    status_word = "enabled" if current else "disabled"

    console.print()
    console.print(f"  Plain mode is currently {status_word}.")
    console.print(
        f"  When enabled: no colors, no Unicode borders, no ANSI codes — "
        f"pure ASCII output for terminals that don't support Rich formatting."
    )
    console.print()

    enable = questionary.confirm(
        "Enable plain/compatibility mode?",
        default=not current,
        style=get_qstyle(),
    ).ask()

    if enable is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1

    if enable == current:
        console.print(f"  [{theme.text_dim}]No change — plain mode remains {status_word}.[/{theme.text_dim}]")
        return 0

    raw_config = load_json(ATLAS_CONFIG_FILE)
    raw_config["compatibility_mode"] = enable
    atomic_write_json(ATLAS_CONFIG_FILE, raw_config)

    action = "enabled" if enable else "disabled"
    console.print(f"\n  Plain mode {action}. Takes effect on the next invocation.")
    return 0


# Friendly minutes → milliseconds for the MongoDB aggregation timeout (the
# server-side maxTimeMS cap that kills a runaway aggregation the instant it is
# exceeded — enforced in MongoCollector.from_config()).
_MONGO_TIMEOUT_CHOICES: tuple[tuple[int, int], ...] = (
    (1, 60_000),
    (5, 300_000),
    (10, 600_000),
    (15, 900_000),
    (30, 1_800_000),
)

# Seconds-based connection / request timeouts. Each key is a Config field
# defaulted to the collector's historical value and injected at that collector's
# construction point; the menu only offers a small, bounded, safe set.
_SECONDS_TIMEOUTS: dict[str, dict] = {
    "ssh_connect_timeout_s": {
        "label": "SSH connection timeout",
        "default": 10,
        "choices": (10, 20, 30, 45, 60),
        "desc": "How long to wait for an SSH connection to a target host before giving up. "
                "Raise it for slow links, jump hosts, or high-latency networks.",
    },
    "platform_api_timeout_s": {
        "label": "Platform API request timeout",
        "default": 30,
        "choices": (15, 30, 45, 60, 90),
        "desc": "How long to wait for each Itential Platform API request. "
                "Raise it for slow instances or large configuration payloads.",
    },
    "redis_timeout_s": {
        "label": "Redis connection timeout",
        "default": 5,
        "choices": (5, 10, 15, 30, 45),
        "desc": "How long to wait when connecting to or reading from Redis.",
    },
}

# Boolean behavior settings (already-persisted Config fields, just exposed here).
_BOOL_SETTINGS: dict[str, dict] = {
    "debug": {
        "label": "Debug logging",
        "default": False,
        "on": "Enabled",
        "off": "Disabled",
        "desc": "Enable verbose debug logging across all Atlas commands.",
    },
    "compatibility_mode": {
        "label": "Compatibility mode (plain output)",
        "default": False,
        "on": "Enabled — plain ASCII, no Rich formatting",
        "off": "Disabled — full Rich output",
        "desc": "Strip Rich formatting and use plain ASCII output. Equivalent to always passing --plain.",
    },
    "keep_logs_file": {
        "label": "Keep raw logs after reports",
        "default": False,
        "on": "Keep the raw log file",
        "off": "Delete after reports are generated",
        "desc": "Whether 01_logs.json is kept once all reports have been built.",
    },
    "extended_validation_checks": {
        "label": "Deep validation checks",
        "default": True,
        "on": "Enabled",
        "off": "Disabled",
        "desc": "Runs the extended deep-check validation engine during validation.",
    },
    "enable_rbac_collection": {
        "label": "RBAC authorization collection",
        "default": False,
        "on": "Enabled (collects accounts, groups, roles, methods from /authorization/*)",
        "off": "Disabled (default — skips privacy-sensitive authorization graph)",
        "desc": "Pulls the full RBAC graph from Platform 6 for the RBAC tab in the unified report.",
    },
    "debug_export_raw_capture": {
        "label": "Export raw capture (debug)",
        "default": False,
        "on": "Enabled",
        "off": "Disabled",
        "desc": "Also write 01_raw_capture.json (the full pre-filter capture) for debugging.",
    },
    "verify_ssl": {
        "label": "SSL certificate verification",
        "default": True,
        "on": "Enabled — verify SSL/TLS certificates (recommended)",
        "off": "Disabled — skip SSL verification (self-signed/trusted-network only)",
        "desc": "Whether Atlas verifies SSL/TLS certificates when connecting to Platform and Gateway APIs. "
                "Disable only in environments with self-signed or untrusted certificates.",
    },
}


def _fmt_secs(seconds: int) -> str:
    """Human-friendly seconds label (60 → '1 minute')."""
    if seconds == 60:
        return "1 minute"
    if seconds > 60 and seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def _persist_config_value(key: str, value) -> None:
    """Write a single key to the global config.json atomically."""
    raw_config = load_json(ATLAS_CONFIG_FILE)
    raw_config[key] = value
    atomic_write_json(ATLAS_CONFIG_FILE, raw_config)


@registry.register("config", "edit", description="Edit individual configuration settings interactively")
def handle_config_edit(args: Namespace) -> int:
    """Interactively edit individual Atlas configuration settings."""
    import questionary

    choices = [
        questionary.Separator("── Behavior ──"),
        questionary.Choice("Manual input mode (browser form / terminal)", value="manual_input_mode"),
        questionary.Choice("Browser open mode (auto / always / never)", value="browser_mode"),
        questionary.Choice("Network policy (third-party connections)", value="network_policy"),
        questionary.Choice("Debug logging", value="bool:debug"),
        questionary.Choice("Keep raw logs after reports", value="bool:keep_logs_file"),
        questionary.Choice("Deep validation checks", value="bool:extended_validation_checks"),
        questionary.Choice("RBAC authorization collection", value="bool:enable_rbac_collection"),
        questionary.Choice("Export raw capture (debug)", value="bool:debug_export_raw_capture"),
        questionary.Separator("── Security ──"),
        questionary.Choice("SSL certificate verification", value="bool:verify_ssl"),
        questionary.Separator("── Timeouts ──"),
        questionary.Choice("MongoDB aggregation timeout", value="mongo_timeout"),
        questionary.Choice("SSH connection timeout", value="secs:ssh_connect_timeout_s"),
        questionary.Choice("Platform API request timeout", value="secs:platform_api_timeout_s"),
        questionary.Choice("Redis connection timeout", value="secs:redis_timeout_s"),
        questionary.Separator("── Appearance ──"),
        questionary.Choice("Theme", value="theme"),
        questionary.Choice("Compatibility mode (plain output)", value="bool:compatibility_mode"),
        questionary.Separator(" "),
        questionary.Choice("Cancel", value="__cancel__"),
    ]

    setting = questionary.select(
        "Which setting would you like to edit?",
        choices=choices,
        style=get_qstyle(),
    ).ask()

    if setting in (None, "__cancel__"):
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 0
    if setting == "mongo_timeout":
        return _edit_mongo_aggregation_timeout()
    if setting == "manual_input_mode":
        return _edit_manual_input_mode()
    if setting == "network_policy":
        return _edit_network_policy()
    if setting == "browser_mode":
        return _edit_browser_mode()
    if setting == "theme":
        return _edit_theme()
    if setting.startswith("secs:"):
        return _edit_seconds_timeout(setting.split(":", 1)[1])
    if setting.startswith("bool:"):
        return _edit_bool_setting(setting.split(":", 1)[1])
    return 0


def _edit_seconds_timeout(field: str) -> int:
    """Pick a seconds-based timeout from a bounded, safe set of options."""
    import questionary

    spec = _SECONDS_TIMEOUTS[field]
    default = spec["default"]
    current = int(getattr(ctx().config, field, default) or default)

    console.print()
    console.print(f"  [{theme.text_dim}]{spec['desc']}[/{theme.text_dim}]")
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {_fmt_secs(current)}\n")

    choices = [
        questionary.Choice(
            _fmt_secs(s)
            + ("  (default)" if s == default else "")
            + ("  (current)" if s == current and s != default else ""),
            value=s,
        )
        for s in spec["choices"]
    ]
    selected = questionary.select(
        f"Set {spec['label']} to:",
        choices=choices,
        default=current if current in spec["choices"] else default,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1
    if selected == current:
        console.print(f"  [{theme.text_dim}]No change — stays at {_fmt_secs(current)}.[/{theme.text_dim}]")
        return 0

    _persist_config_value(field, int(selected))
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] {spec['label']} set to "
        f"[bold]{_fmt_secs(int(selected))}[/bold]. "
        f"[{theme.text_dim}]Takes effect on the next capture.[/{theme.text_dim}]"
    )
    return 0


def _edit_bool_setting(field: str) -> int:
    """Toggle a boolean behavior setting via an explicit two-choice select."""
    import questionary

    spec = _BOOL_SETTINGS[field]
    current = bool(getattr(ctx().config, field, spec["default"]))

    console.print()
    console.print(f"  [{theme.text_dim}]{spec['desc']}[/{theme.text_dim}]")
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {spec['on'] if current else spec['off']}\n")

    choices = [
        questionary.Choice(spec["on"] + ("  (current)" if current else ""), value=True),
        questionary.Choice(spec["off"] + ("  (current)" if not current else ""), value=False),
    ]
    selected = questionary.select(
        f"{spec['label']}:",
        choices=choices,
        default=current,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1
    if selected == current:
        console.print(f"  [{theme.text_dim}]No change.[/{theme.text_dim}]")
        return 0

    _persist_config_value(field, bool(selected))
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] {spec['label']} → "
        f"[bold]{spec['on'] if selected else spec['off']}[/bold]."
    )
    return 0


def _edit_manual_input_mode() -> int:
    """Choose how manual/extended inputs are collected: browser form or terminal."""
    import questionary

    current = (getattr(ctx().config, "manual_input_mode", "html") or "html").lower()
    if current not in ("html", "cli"):
        current = "html"

    console.print()
    console.print(
        f"  [{theme.text_dim}]How Atlas collects manual/extended inputs during capture: an HTML "
        f"form opened in your browser, or prompts in the terminal (better for headless / "
        f"SSH-only sessions).[/{theme.text_dim}]"
    )
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {current}\n")

    choices = [
        questionary.Choice("Browser form (html)" + ("  (current)" if current == "html" else ""), value="html"),
        questionary.Choice("Terminal prompts (cli)" + ("  (current)" if current == "cli" else ""), value="cli"),
    ]
    selected = questionary.select(
        "Manual input mode:",
        choices=choices,
        default=current,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1
    if selected == current:
        console.print(f"  [{theme.text_dim}]No change — stays '{current}'.[/{theme.text_dim}]")
        return 0

    _persist_config_value("manual_input_mode", selected)
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] Manual input mode set to [bold]{selected}[/bold]."
    )
    return 0


def _edit_network_policy() -> int:
    """Choose the outbound network policy (allow / disallow)."""
    import questionary

    current = getattr(ctx().config, "network_policy", "allow") or "allow"

    console.print()
    console.print(
        f"  [{theme.text_dim}]Controls whether Atlas may contact third-party services "
        f"not part of the audited environment.[/{theme.text_dim}]"
    )
    console.print(
        f"  [{theme.text_dim}]  allow    — Google Fonts CDN (reports), GitLab adapter checks, "
        f"GitHub ruleset updates, webhook notifications[/{theme.text_dim}]"
    )
    console.print(
        f"  [{theme.text_dim}]  disallow — all third-party connections blocked; "
        f"reports use embedded fonts[/{theme.text_dim}]"
    )
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {current}\n")

    choices = [
        questionary.Choice(
            "allow — permit third-party connections (Google Fonts, GitLab, GitHub)"
            + ("  (current)" if current == "allow" else ""),
            value="allow",
        ),
        questionary.Choice(
            "disallow — block all third-party connections, embed fonts in reports"
            + ("  (current)" if current == "disallow" else ""),
            value="disallow",
        ),
    ]
    selected = questionary.select(
        "Network policy:",
        choices=choices,
        default=current,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1

    # Always write explicitly — load_config() sanitizes "" or absent → "allow",
    # so a "no change" guard based on the loaded Config value would silently
    # leave an empty string in config.json when the user confirms "allow".
    _persist_config_value("network_policy", selected)
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] Network policy → [bold]{selected}[/bold]."
    )
    if selected == "disallow":
        console.print(
            f"  [{theme.text_dim}]New reports will embed fonts; adapter checks and "
            f"ruleset updates will be blocked.[/{theme.text_dim}]"
        )
    return 0


def _edit_browser_mode() -> int:
    """Choose whether Atlas auto-opens generated HTML in a browser."""
    import questionary

    current = getattr(ctx().config, "browser_mode", "auto") or "auto"

    console.print()
    console.print(
        f"  [{theme.text_dim}]Controls whether Atlas auto-opens generated HTML "
        f"(reports, What's New, setup wizards) in a browser.[/{theme.text_dim}]"
    )
    console.print(
        f"  [{theme.text_dim}]  auto   — open unless this looks like a headless server "
        f"session (Linux, no DISPLAY/WAYLAND_DISPLAY)[/{theme.text_dim}]"
    )
    console.print(f"  [{theme.text_dim}]  always — always try to open[/{theme.text_dim}]")
    console.print(f"  [{theme.text_dim}]  never  — never open; always print the file path[/{theme.text_dim}]")
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {current}\n")

    choices = [
        questionary.Choice(
            "auto — skip on a detected headless server, open otherwise"
            + ("  (current)" if current == "auto" else ""),
            value="auto",
        ),
        questionary.Choice(
            "always — always try to open a browser"
            + ("  (current)" if current == "always" else ""),
            value="always",
        ),
        questionary.Choice(
            "never — never open a browser, just print the file path"
            + ("  (current)" if current == "never" else ""),
            value="never",
        ),
    ]
    selected = questionary.select(
        "Browser open mode:",
        choices=choices,
        default=current,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1
    if selected == current:
        console.print(f"  [{theme.text_dim}]No change — stays '{current}'.[/{theme.text_dim}]")
        return 0

    _persist_config_value("browser_mode", selected)
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] Browser open mode set to [bold]{selected}[/bold]."
    )
    return 0


def _edit_mongo_aggregation_timeout() -> int:
    """Pick the MongoDB aggregation timeout from friendly minute options."""
    import questionary

    current_ms = getattr(ctx().config, "mongo_aggregation_timeout_ms", 60_000) or 60_000
    current_min = current_ms // 60_000

    console.print()
    console.print(
        f"  [{theme.text_dim}]Caps how long operational-report MongoDB aggregations may run "
        f"before they are killed. Raise it for very large datasets — the query is stopped "
        f"the instant the limit is hit.[/{theme.text_dim}]"
    )
    console.print(f"  [{theme.text_dim}]Current:[/{theme.text_dim}] {current_min} minute(s)\n")

    choices = [
        questionary.Choice(
            f"{mins} minute{'s' if mins != 1 else ''}" + ("  (current)" if ms == current_ms else ""),
            value=ms,
        )
        for mins, ms in _MONGO_TIMEOUT_CHOICES
    ]
    valid_ms = {ms for _, ms in _MONGO_TIMEOUT_CHOICES}

    selected = questionary.select(
        "Set MongoDB aggregation timeout to:",
        choices=choices,
        default=current_ms if current_ms in valid_ms else 60_000,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 1

    if selected == current_ms:
        console.print(f"  [{theme.text_dim}]No change — timeout stays at {current_min} minute(s).[/{theme.text_dim}]")
        return 0

    raw_config = load_json(ATLAS_CONFIG_FILE)
    raw_config["mongo_aggregation_timeout_ms"] = selected
    atomic_write_json(ATLAS_CONFIG_FILE, raw_config)

    new_min = selected // 60_000
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] MongoDB aggregation timeout set to "
        f"[bold]{new_min} minute{'s' if new_min != 1 else ''}[/bold]. "
        f"[{theme.text_dim}]Takes effect on the next capture.[/{theme.text_dim}]"
    )
    return 0
