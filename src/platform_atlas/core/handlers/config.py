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
from platform_atlas.core.init_setup import QSTYLE, ask_secret, mask
from platform_atlas.core.config import load_config_safe
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.core.json_utils import load_json
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.theme import THEME_REGISTRY, get_theme_by_id, list_theme_ids
from platform_atlas.core.credentials import (
    credential_store,
    scoped_service_name,
    CredentialKey,
    CredentialBackendType,
    reset_credential_store,
    verify_keyring_backend,
)
from platform_atlas.core import ui

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
    new_deployment = ask_deployment()

    # If an environment is active, write to the environment file
    if config.active_environment:
        from platform_atlas.core.environment import get_environment_manager
        mgr = get_environment_manager()
        env = mgr.load(config.active_environment)
        env.deployment = new_deployment
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

@registry.register("config", "theme", description="Interactive theme switcher with live preview")
def handle_theme_switcher(args: Namespace) -> int:
    """Interactive theme switcher with live preview"""
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
        style=QSTYLE,
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

    # Read current config and update theme
    raw_config = load_json(ATLAS_CONFIG_FILE)
    raw_config["theme"] = selected

    # Atomically write the config with new theme added
    atomic_write_json(ATLAS_CONFIG_FILE, raw_config)

    # Preview the new theme
    new_theme = get_theme_by_id(selected)
    ui.console.print()
    ui.console.print(
        f"[{new_theme.success}]✓[/{new_theme.success}] "
        f"Theme set to [{new_theme.primary}]"
        f"{selected}[/{new_theme.primary}]"
    )
    ui.console.print(f"[{new_theme.text_dim}]Takes effect on next run[/{new_theme.text_dim}]")
    return 0

@registry.register("config", "credentials", description="View and update credentials")
def handle_config_credentials(args: Namespace) -> int:
    """View and update credentials in the active backend."""
    import questionary
    from platform_atlas.core.credentials import CredentialError

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
                f"  [{theme.error}]✘ Vault connection failed:[/{theme.error}] {e}"
            )
            console.print(
                f"  [{theme.text_dim}]This usually means your AppRole credentials "
                f"or token have changed.[/{theme.text_dim}]"
            )
            console.print()

            update = questionary.confirm(
                "Update Vault connection settings?",
                default=True,
                style=QSTYLE,
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
        # Keyring mode: verify the backend is secure
        is_secure, backend = verify_keyring_backend()
        if not is_secure:
            console.print(Panel(
                f"[bold {theme.error}]Insecure keyring backend: {backend}[/bold {theme.error}]\n\n"
                f"[{theme.text_primary}]Platform Atlas requires a secure OS credential store.\n"
                f"  • macOS: Keychain (built-in)\n"
                f"  • Windows: Credential Locker (built-in)\n"
                f"  • Linux: Install gnome-keyring + secretstorage + python3-dbus[/{theme.text_primary}]",
                border_style=theme.error,
                box=box.ROUNDED,
                expand=False,
            ))
            return 1

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

    for key in CredentialKey:
        value = store.get(key)
        if value:
            badge = f"[{theme.success}]✓ Stored[/{theme.success}]"
            preview = mask(value, keep=8) if len(value) > 12 else mask(value)
        else:
            badge = f"[{theme.error}]✘ Missing[/{theme.error}]"
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
            missing_keys = [key for key in CredentialKey if not store.get(key)]
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
                    "Update Vault connection  — Re-enter URL, token, or AppRole credentials",
                    value="update_vault",
                ),
                questionary.Choice(
                    "Verify Vault secrets     — Check which secrets Vault currently has",
                    value="verify",
                ),
                questionary.Choice("Done", value="done"),
            ],
            style=QSTYLE,
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
            style=QSTYLE,
        ).ask()

        if action is None or action == "done":
            break

        # Pick which credential
        cred_choices = [
            questionary.Choice(key.display_name, value=key)
            for key in CredentialKey
        ]
        selected = questionary.select(
            "Which credential?",
            choices=cred_choices,
            style=QSTYLE,
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
                style=QSTYLE,
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
        VaultBackend(vault_config, service=service)
        console.print(
            f"  [{theme.success}]✓ Connected to Vault at "
            f"{vault_config.url}[/{theme.success}]"
        )
    except Exception as e:
        console.print(
            f"  [{theme.error}]✘ Connection failed: {e}[/{theme.error}]"
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
                f"    [{theme.error}]✘[/{theme.error}] "
                f"{key.display_name} ({key.value})"
            )

@registry.register("config", "architecture", description="Collect or update architecture data")
def handle_config_architecture(args: Namespace) -> int:
    from platform_atlas.capture.collectors.manual import run_architecture_collection
    run_architecture_collection()
    return 0
