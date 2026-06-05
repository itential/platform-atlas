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
    CredentialError,
    reset_credential_store,
    verify_keyring_backend,
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
        is_secure, is_functional, backend = verify_keyring_backend()
        if not is_functional:
            console.print(Panel(
                f"[bold {theme.error}]No usable keyring backend found: {backend}[/bold {theme.error}]\n\n"
                f"[{theme.text_primary}]Platform Atlas cannot store credentials on this system.\n"
                f"  • macOS: Keychain (built-in)\n"
                f"  • Windows: Credential Locker (built-in)\n"
                f"  • Linux: install gnome-keyring, or configure Vault as the credential backend[/{theme.text_primary}]",
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
                    "Update Vault connection  — Re-enter URL, token, AppRole credentials, or wrapping token",
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

    # ── Credential backend ────────────────────────────────────────
    try:
        is_secure, is_functional, backend_name = verify_keyring_backend()
        _store = credential_store()
        _using_vault = _store.is_vault

        if _using_vault:
            # Vault mode: show two rows — one for OS keyring (Vault token storage),
            # one for the Vault credential backend itself.
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
        else:
            if not is_functional:
                rows.append(DoctorRow.from_tuple((
                    "Credential backend", "fail",
                    f"{backend_name} is not functional",
                    "Install gnome-keyring (Linux), or configure HashiCorp Vault.",
                )))
            elif not is_secure:
                rows.append(DoctorRow.from_tuple((
                    "Credential backend", "warn",
                    f"{backend_name} (unencrypted)",
                    "Switch to Secret Service / Keychain / Vault for production use.",
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
            with console.status("[blue]Probing reachability…[/blue]", spinner="dots") as _s:
                _s.update("[blue]Probing Platform OAuth URL…[/blue]")
                rows.append(DoctorRow.from_tuple(probe_platform_url(config)))
                _s.update("[blue]Probing Gateway4 health endpoint…[/blue]")
                _gw4 = probe_gateway4_url(config)
                if _gw4 is not None:
                    rows.append(DoctorRow.from_tuple(_gw4))
        else:
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


def _render_doctor_rows(
    rows: list[DoctorRow],
    *,
    env_name: str | None,
    tier: str | None,
) -> None:
    """Print the doctor results — one row per check + final summary."""
    header = f"[bold {theme.primary_glow}]Atlas Configuration Health Check[/]"
    if env_name:
        header += f"  [dim]— env: {env_name}"
        if tier:
            header += f" ({tier})"
        header += "[/dim]"
    console.print()
    console.print(header)
    console.print()

    counts = {"ok": 0, "warn": 0, "fail": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
        if row.status == "ok":
            badge = f"[{theme.success}]✓[/{theme.success}]"
        elif row.status == "warn":
            badge = f"[{theme.warning}]⚠[/{theme.warning}]"
        else:
            badge = f"[{theme.error}]✘[/{theme.error}]"
        console.print(f"{badge} [bold]{row.label:<28}[/bold] [dim]{row.detail}[/dim]")
        if row.status != "ok" and row.suggest:
            console.print(f"   [dim]→ {row.suggest}[/dim]")

    summary = (
        f"Summary: {counts.get('ok', 0)} OK · "
        f"{counts.get('warn', 0)} warning(s) · "
        f"{counts.get('fail', 0)} error(s)"
    )
    console.print()
    console.print(f"[dim]{summary}[/dim]")


@registry.register("config", "plain", description="Toggle plain/compatibility mode on or off")
def handle_config_plain(args: Namespace) -> int:
    """Enable or disable plain (compatibility) mode."""
    import questionary
    from platform_atlas.core.init_setup import QSTYLE

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
        style=QSTYLE,
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
    "debug_export_raw_capture": {
        "label": "Export raw capture (debug)",
        "default": False,
        "on": "Enabled",
        "off": "Disabled",
        "desc": "Also write 01_raw_capture.json (the full pre-filter capture) for debugging.",
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
        questionary.Choice("Keep raw logs after reports", value="bool:keep_logs_file"),
        questionary.Choice("Deep validation checks", value="bool:extended_validation_checks"),
        questionary.Choice("Export raw capture (debug)", value="bool:debug_export_raw_capture"),
        questionary.Separator("── Timeouts ──"),
        questionary.Choice("MongoDB aggregation timeout", value="mongo_timeout"),
        questionary.Choice("SSH connection timeout", value="secs:ssh_connect_timeout_s"),
        questionary.Choice("Platform API request timeout", value="secs:platform_api_timeout_s"),
        questionary.Choice("Redis connection timeout", value="secs:redis_timeout_s"),
        questionary.Separator(" "),
        questionary.Choice("Cancel", value="__cancel__"),
    ]

    setting = questionary.select(
        "Which setting would you like to edit?",
        choices=choices,
        style=QSTYLE,
    ).ask()

    if setting in (None, "__cancel__"):
        console.print(f"  [{theme.text_dim}]Cancelled — no changes made.[/{theme.text_dim}]")
        return 0
    if setting == "mongo_timeout":
        return _edit_mongo_aggregation_timeout()
    if setting == "manual_input_mode":
        return _edit_manual_input_mode()
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
        style=QSTYLE,
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
        style=QSTYLE,
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
        style=QSTYLE,
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
        style=QSTYLE,
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
