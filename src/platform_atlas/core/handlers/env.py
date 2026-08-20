# pylint: disable=line-too-long
"""
Dispatch Handler ::: Environment Management

Commands:
    env list      — List all environments and show which is active
    env switch    — Switch the active environment
    env show      — Show details of an environment
    env create    — Create a new environment (interactive wizard)
    env remove    — Delete an environment
    env edit      — Open an environment file in $EDITOR
"""

from __future__ import annotations

import logging
import os
from argparse import Namespace
from pathlib import Path

import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from platform_atlas.core.registry import registry
from platform_atlas.core.environment import (
    Environment,
    get_environment_manager,
    normalize_env_name,
    propagate_ssh_key,
    validate_env_name,
)
from platform_atlas.core.init_setup import get_qstyle, create_environment_wizard
from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
from platform_atlas.core import ui

console = Console()
theme = ui.theme
logger = logging.getLogger(__name__)

# Display labels for the three audit modes (tiers). Keyed by the raw tier value
# stored on an environment / in config.json.
_MODE_LABELS: dict[str, str] = {
    "standard": "Standard",
    "extended": "Extended",
    "saas": "SaaS",
}
# Theme color attribute used to tint each mode in the env-list table.
_MODE_COLOR_ATTR: dict[str, str] = {
    "standard": "info",
    "extended": "accent",
    "saas": "success",
}


def _global_default_tier() -> str:
    """Read the base (un-overlaid) global tier from config.json.

    Mirrors ``load_config``'s migration shim: a config with no ``tier`` field
    predates the tier system and is treated as Extended. Used as the fallback
    mode for environments that don't set their own ``tier``.
    """
    import json

    from platform_atlas.core.paths import ATLAS_CONFIG_FILE

    try:
        with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as f:
            return (json.load(f).get("tier") or "extended").strip().lower()
    except Exception:
        return "extended"


def _mode_cell(env: object, global_tier: str) -> str:
    """Render the Mode column cell for an environment.

    Effective mode is the environment's own ``tier`` if set, otherwise the
    global default. Always resolves to exactly one of Standard / Extended / SaaS.
    """
    eff = (getattr(env, "tier", None) or global_tier).strip().lower()
    label = _MODE_LABELS.get(eff, eff.capitalize() or "—")
    color = getattr(theme, _MODE_COLOR_ATTR.get(eff, "text_primary"))
    return f"[bold {color}]{label}[/bold {color}]"


@registry.register("env", "list", description="List all environments")
def handle_env_list(args: Namespace) -> int:
    """List all environments, highlighting the active one."""
    mgr = get_environment_manager()
    env_names = mgr.list_names()

    if not env_names:
        console.print(f"\n  [{theme.text_dim}]No environments configured.[/{theme.text_dim}]")
        console.print(f"  [{theme.text_dim}]Run 'platform-atlas env create' to set one up.[/{theme.text_dim}]\n")
        return 0

    active = mgr.get_active_name()

    loaded: list[tuple[str, object | None]] = []
    for name in env_names:
        try:
            loaded.append((name, mgr.load(name)))
        except Exception:
            loaded.append((name, None))

    table = Table(
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
        border_style=theme.border_primary,
    )
    table.add_column("Name", style=f"bold {theme.text_primary}", min_width=18)
    table.add_column("Description", style=theme.text_secondary, min_width=28)
    table.add_column("Platform URI", style=theme.text_dim, min_width=24)
    table.add_column("Mode", justify="center", min_width=10)
    table.add_column("Backend", style=theme.text_dim, min_width=10)

    global_tier = _global_default_tier()

    for name, env in loaded:
        if env is None:
            table.add_row(name, f"[{theme.error}]error loading[/{theme.error}]", "", "", "")
            continue

        is_active = name == active
        marker = f"[bold {theme.success}]{ui.glyph('active')}[/bold {theme.success}] " if is_active else "  "
        name_display = (
            f"{marker}[bold {theme.accent}]{name}[/bold {theme.accent}]"
            if is_active
            else f"{marker}{name}"
        )
        if getattr(env, "partial", False):
            name_display += f"  [{theme.warning}]⚠ incomplete[/{theme.warning}]"

        row = [
            name_display,
            env.description or f"[{theme.text_dim}]—[/{theme.text_dim}]",
            env.platform_uri or f"[{theme.text_dim}]—[/{theme.text_dim}]",
            _mode_cell(env, global_tier),
            env.credential_backend,
        ]
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print()
    return 0


@registry.register("env", "switch", description="Switch the active environment")
def handle_env_switch(args: Namespace) -> int:
    """
    Switch the active environment.

    After switching, checks for sessions bound to that environment and
    offers to switch to one — which also restores the session's ruleset
    and profile for a complete context switch.
    """
    mgr = get_environment_manager()
    env_names = mgr.list_names()

    if not env_names:
        console.print(f"\n  [{theme.warning}]No environments configured.[/{theme.warning}]")
        console.print(f"  [{theme.text_dim}]Run 'platform-atlas env create' first.[/{theme.text_dim}]\n")
        return 1

    # Use the positional arg if given, otherwise prompt
    target = getattr(args, "env_name", None)

    if target is None:
        active = mgr.get_active_name()
        choices = []
        for name in env_names:
            try:
                mgr.load(name)
                suffix = " (active)" if name == active else ""
                label = f"{name}{suffix}"
            except Exception:
                label = name
            choices.append(questionary.Choice(title=label, value=name))

        target = questionary.select(
            "Switch to environment:",
            choices=choices,
            default=active if active in env_names else env_names[0],
            style=get_qstyle(),
        ).ask()

        if target is None:
            console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
            return 1

    if not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]")
        console.print(f"  [{theme.text_dim}]Available: {', '.join(env_names)}[/{theme.text_dim}]\n")
        return 1

    mgr.set_active(target)
    console.print(f"\n  [{theme.success}]✓[/{theme.success}] Active environment: [{theme.accent}]{target}[/{theme.accent}]")

    # ── Offer to switch to a session bound to this environment ────
    try:
        from platform_atlas.core.session_manager import get_session_manager
        session_mgr = get_session_manager()
        all_sessions = session_mgr.list()
        matching = [s for s in all_sessions if s.metadata.environment == target]

        if matching:
            console.print(
                f"\n  [{theme.text_dim}]{len(matching)} session(s) use this environment.[/{theme.text_dim}]"
            )

            session_choices = []
            active_session = session_mgr.get_active_session_name()
            for s in matching:
                suffix = " (active)" if s.name == active_session else ""
                profile_part = f" + {s.metadata.ruleset_profile}" if s.metadata.ruleset_profile else ""
                ruleset_part = f"  [{s.metadata.ruleset_id}{profile_part}]" if s.metadata.ruleset_id else ""
                label = f"{s.name}{ruleset_part} ({s.metadata.status.value}){suffix}"
                session_choices.append(questionary.Choice(title=label, value=s.name))

            session_choices.append(questionary.Choice(
                title="── Skip (just switch environment)",
                value="_skip",
            ))

            selected = questionary.select(
                "Switch to a session?",
                choices=session_choices,
                style=get_qstyle(),
            ).ask()

            if selected and selected != "_skip":
                session = session_mgr.activate_session_context(selected)
                console.print(
                    f"  [{theme.success}]✓[/{theme.success}] Active session: "
                    f"[{theme.accent}]{selected}[/{theme.accent}]"
                )
                if session.metadata.ruleset_id:
                    profile_part = f" + {session.metadata.ruleset_profile}" if session.metadata.ruleset_profile else ""
                    console.print(
                        f"    Ruleset: [{theme.secondary}]{session.metadata.ruleset_id}"
                        f"{profile_part}[/{theme.secondary}]"
                    )
        else:
            console.print(
                f"  [{theme.text_dim}]No sessions use this environment. "
                f"Create one with: session create <name>[/{theme.text_dim}]"
            )
    except Exception as e:
        logger.debug("Session lookup after env switch failed: %s", e)

    console.print()
    return 0


@registry.register("env", "show", description="Show environment details")
def handle_env_show(args: Namespace) -> int:
    """Display details of an environment."""
    import json
    from rich.syntax import Syntax

    mgr = get_environment_manager()
    target = getattr(args, "env_name", None)

    if target is None:
        target = mgr.get_active_name()
        if target is None:
            console.print(f"\n  [{theme.warning}]No active environment set.[/{theme.warning}]")
            console.print(f"  [{theme.text_dim}]Specify one: platform-atlas env show <name>[/{theme.text_dim}]\n")
            return 1

    if not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]\n")
        return 1

    env = mgr.load(target)
    active = mgr.get_active_name()
    active_badge = f"  [{theme.success}](active)[/{theme.success}]" if target == active else ""

    # Pretty-print the JSON
    formatted = json.dumps(env.to_dict(), indent=4, default=str, ensure_ascii=False)
    syntax = Syntax(formatted, "json", theme="monokai", line_numbers=False)

    console.print()
    console.print(Panel(
        syntax,
        title=f"[bold {theme.primary_glow}]{target}[/bold {theme.primary_glow}]{active_badge}",
        subtitle=f"[{theme.text_dim}]{env.file_path}[/{theme.text_dim}]",
        border_style=theme.border_primary,
        padding=(1, 2),
    ))

    # Show topology if present
    if env.deployment:
        from platform_atlas.core.topology import DeploymentTopology
        from platform_atlas.core.init_setup import _display_topology_review
        topology = DeploymentTopology.from_dict(env.deployment)
        scope = env.deployment.get("capture_scope", "primary_only")
        _display_topology_review(topology, capture_scope=scope)

    console.print()
    return 0


@registry.register("env", "architecture",
                   description="Collect or edit an environment's architecture information")
def handle_env_architecture(args: Namespace) -> int:
    """Open the architecture form (HTML or CLI prompts) for an environment.

    Architecture answers are stored per-environment under
    ``~/.atlas/architecture/<env>.json`` and feed the audit report. This is the
    place to fill them in or update them outside of a capture run.
    """
    from platform_atlas.core.context import ctx
    from platform_atlas.capture.collectors.manual import run_architecture_collection

    mgr = get_environment_manager()
    target = getattr(args, "env_name", None)
    if target is None:
        target = mgr.get_active_name() or ""

    if target and not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]\n")
        return 1

    try:
        if ctx().is_standard:
            console.print(
                f"\n  [{theme.warning}]Architecture review applies to the Extended tier "
                f"only — nothing to collect in Standard.[/{theme.warning}]\n"
            )
            return 0
    except Exception:  # pylint: disable=broad-except
        pass

    console.print(
        f"\n[{theme.primary}]Architecture information for "
        f"[bold]{target or 'the active environment'}[/bold][/{theme.primary}]"
    )
    run_architecture_collection(
        environment=target,
        force=bool(getattr(args, "force", False)),
    )
    return 0


def _read_guide_bytes(*relparts: str) -> bytes | None:
    """Read a packaged guide file, falling back to the source tree in dev installs."""
    from platform_atlas.core.guide_assets import read_guide_bytes
    return read_guide_bytes(*relparts)


def _get_env_setup_html_path() -> Path | None:
    """Locate env-setup.html, syncing it and its assets to ~/.atlas/guides/ if needed."""
    from platform_atlas.core.paths import ATLAS_HOME, ATLAS_HOME_GUIDES

    dest = ATLAS_HOME_GUIDES / "env-setup.html"
    html_bytes = _read_guide_bytes("env-setup.html")
    if html_bytes is None:
        return None

    ATLAS_HOME_GUIDES.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest.write_bytes(html_bytes)

    # Sync the shared CSS + motion assets alongside the page. A missing asset
    # only means the wizard renders without styling/animation — never fatal.
    from platform_atlas.core.guide_assets import sync_guide_assets
    sync_guide_assets(ATLAS_HOME_GUIDES)

    # Remove the pre-guides-folder copy that used to live directly under ~/.atlas.
    legacy = ATLAS_HOME / "env-setup.html"
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

    return dest


def _handle_env_create_html_setup() -> int:
    """Open the browser-based environment setup wizard."""
    path = _get_env_setup_html_path()
    if path is None:
        console.print(
            f"\n  [{theme.error}]Setup page not found. "
            f"Try reinstalling platform-atlas.[/{theme.error}]\n"
        )
        return 1

    if ui.maybe_open_html(f"file://{path.resolve()}"):
        console.print(
            f"\n  [{theme.primary_glow}]Environment Setup Builder[/{theme.primary_glow}]  "
            f"[{theme.text_dim}]opened in your browser[/{theme.text_dim}]"
        )
    else:
        console.print(
            f"\n  [{theme.primary_glow}]Environment Setup Builder[/{theme.primary_glow}]  "
            f"[{theme.text_dim}]server environment detected — open manually: {path}[/{theme.text_dim}]"
        )
    console.print(f"\n  [{theme.text_secondary}]1. Fill out the form (including credentials) and click "
                  f"[bold]Generate Encrypted Bundle[/bold].")
    console.print(f"  [{theme.text_secondary}]2. Copy the one-time passphrase it shows you.[/{theme.text_secondary}]")
    console.print(f"  [{theme.text_secondary}]3. Save the .atlasenv.enc file, then run:[/{theme.text_secondary}]")
    console.print(
        f"\n  [{theme.accent}]platform-atlas env create --from-file "
        f"<path-to-bundle.atlasenv.enc>[/{theme.accent}]\n"
    )
    console.print(
        f"  [{theme.text_dim}]You'll be asked for the passphrase. The bundle is shredded after a "
        f"successful import (use --keep-file to retain it).[/{theme.text_dim}]\n"
    )
    return 0


def _section_header(title: str, subtitle: str = "") -> None:
    """Print a consistent wizard section header."""
    console.print()
    console.print(f"[bold {theme.primary_glow}]{title}[/bold {theme.primary_glow}]", end="")
    if subtitle:
        console.print(f"  [{theme.text_dim}]{subtitle}[/{theme.text_dim}]", end="")
    console.print()
    console.print()


def _display_env_summary(env) -> None:  # pylint: disable=too-many-branches
    """Print a Rich table summarising an environment's non-sensitive fields."""
    tier = (getattr(env, "tier", None) or "extended").lower()
    backend = (getattr(env, "credential_backend", None) or "keyring").lower()

    t = Table(
        box=box.ROUNDED,
        show_header=False,
        pad_edge=True,
        border_style=theme.border_primary,
        min_width=60,
    )
    t.add_column("Field", style=theme.text_dim, min_width=22)
    t.add_column("Value", style=theme.text_primary)

    t.add_row("Environment Name", f"[bold {theme.accent}]{env.name}[/bold {theme.accent}]")
    t.add_row("Tier", tier)
    t.add_row("Credential Backend", backend)
    if env.description:
        t.add_row("Description", env.description)
    if env.env_tint:
        t.add_row("Banner Tint", env.env_tint)

    if tier in ("standard", "extended"):
        t.add_row("", "")
        t.add_row(f"[{theme.text_dim}]── Platform ──[/{theme.text_dim}]", "")
        t.add_row(
            "Platform URL",
            env.platform_uri or f"[{theme.warning}](not set)[/{theme.warning}]",
        )
        t.add_row(
            "Platform Client ID",
            env.platform_client_id or f"[{theme.warning}](not set)[/{theme.warning}]",
        )

    if env.gateway4_uri:
        t.add_row("", "")
        t.add_row(f"[{theme.text_dim}]── Gateway 4 ──[/{theme.text_dim}]", "")
        t.add_row("Gateway4 URL", env.gateway4_uri)
        t.add_row("Gateway4 Username", env.gateway4_username or "admin@itential")

    if env.ssh_key and tier in ("extended", "saas"):
        t.add_row("", "")
        t.add_row(f"[{theme.text_dim}]── SSH ──[/{theme.text_dim}]", "")
        key_exists = Path(env.ssh_key).expanduser().exists()
        key_display = env.ssh_key
        if not key_exists:
            key_display += f" [{theme.warning}]⚠ file not found[/{theme.warning}]"
        t.add_row("SSH Key", key_display)

    if tier == "extended":
        t.add_row("", "")
        t.add_row(f"[{theme.text_dim}]── Database TLS ──[/{theme.text_dim}]", "")
        t.add_row("MongoDB TLS", "enabled" if getattr(env, "mongo_tls_enabled", False) else "disabled")
        t.add_row("Redis TLS", "enabled" if getattr(env, "redis_tls_enabled", False) else "disabled")

    if env.deployment and tier in ("extended", "saas"):
        t.add_row("", "")
        t.add_row(f"[{theme.text_dim}]── Topology ──[/{theme.text_dim}]", "")
        mode = env.deployment.get("mode", "unknown")
        nodes = env.deployment.get("nodes", [])
        t.add_row("Mode", mode)
        t.add_row("Nodes", str(len(nodes)))
        for node in nodes[:6]:  # show up to 6 nodes to keep it readable
            role = node.get("role", "?")
            host = node.get("host", "?")
            primary = " (primary)" if node.get("primary") else ""
            t.add_row(f"  {role}", f"{host}{primary}")
        if len(nodes) > 6:
            t.add_row("", f"  … {len(nodes) - 6} more")

    console.print(Panel(
        t,
        title=f"[bold {theme.primary_glow}]Environment Summary[/bold {theme.primary_glow}]",
        border_style=theme.border_primary,
        padding=(0, 1),
    ))
    console.print()


def _env_field_choice(label: str, val: str, key: str) -> questionary.Choice:
    """Choice with a colored label and a dim current value for the env-edit list."""
    return questionary.Choice(
        title=[(f"fg:{theme.primary}", f"{label:<28} "), (f"fg:{theme.text_dim}", val)],
        value=key,
    )


def _run_env_edit_loop(env) -> None:  # pylint: disable=too-many-branches,too-many-statements
    """
    Interactive loop to review and correct environment fields loaded from a file.

    Presents a questionary.select menu of editable fields; the user selects
    one to change, edits its value, then returns to the menu.  Choosing 'Done'
    (or Ctrl-C) exits the loop.
    """
    from platform_atlas.core.init_setup import _validate_http_url  # local: avoid circular import

    tier = (getattr(env, "tier", None) or "extended").lower()

    while True:
        _display_env_summary(env)

        choices = []

        choices.append(_env_field_choice("Environment Name",   env.name, "name"))
        if tier in ("standard", "extended"):
            choices.append(_env_field_choice("Platform URL",       env.platform_uri or "(not set)",       "platform_uri"))
            choices.append(_env_field_choice("Platform Client ID", env.platform_client_id or "(not set)", "platform_client_id"))
        if env.gateway4_uri or tier in ("saas",):
            choices.append(_env_field_choice("Gateway4 URL",      env.gateway4_uri or "(not set)",            "gateway4_uri"))
            choices.append(_env_field_choice("Gateway4 Username", env.gateway4_username or "admin@itential",  "gateway4_username"))
        if tier in ("extended", "saas"):
            choices.append(_env_field_choice("SSH Key Path",      env.ssh_key or "(not set)", "ssh_key"))
        if tier == "extended":
            choices.append(_env_field_choice(
                "MongoDB TLS", "enabled" if getattr(env, "mongo_tls_enabled", False) else "disabled", "mongo_tls_enabled",
            ))
            choices.append(_env_field_choice(
                "Redis TLS", "enabled" if getattr(env, "redis_tls_enabled", False) else "disabled", "redis_tls_enabled",
            ))
        choices.append(_env_field_choice("Description",       env.description or "(not set)",       "description"))
        choices.append(questionary.Choice(title="Done — everything looks right", value="_done"))

        selected = questionary.select(
            "Select a field to edit, or 'Done' to continue:",
            choices=choices,
            style=get_qstyle(),
        ).ask()

        if selected is None or selected == "_done":
            break

        if selected == "name":
            mgr = get_environment_manager()

            def _v_name(v: str) -> bool | str:
                v = v.strip()
                if not v:
                    return "Required"
                if not validate_env_name(v):
                    return "Lowercase letters, numbers, hyphens, underscores, or dots only"
                return True

            new_name = questionary.text(
                "New environment name:",
                default=env.name,
                validate=_v_name,
                style=get_qstyle(),
            ).ask()
            if new_name is None:
                raise KeyboardInterrupt
            new_name = new_name.strip()
            if new_name and new_name != env.name:
                if mgr.exists(new_name):
                    console.print(
                        f"  [{theme.warning}]⚠ '{new_name}' already exists — choose a different name.[/{theme.warning}]"
                    )
                else:
                    env.name = new_name
                    console.print(f"  [{theme.success}]✓ Name updated[/{theme.success}]")

        elif selected == "platform_uri":
            new_val = questionary.text(
                "Platform URL:",
                default=env.platform_uri or "",
                validate=lambda v: _validate_http_url(v) if v.strip() else "Required",
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.platform_uri = new_val.strip()

        elif selected == "platform_client_id":
            new_val = questionary.text(
                "Platform Client ID:",
                default=env.platform_client_id or "",
                validate=lambda v: True if v.strip() else "Required",
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.platform_client_id = new_val.strip()

        elif selected == "gateway4_uri":
            new_val = questionary.text(
                "Gateway4 API URL (leave blank to remove):",
                default=env.gateway4_uri or "",
                validate=lambda v: _validate_http_url(v) if v.strip() else True,
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.gateway4_uri = new_val.strip()

        elif selected == "gateway4_username":
            new_val = questionary.text(
                "Gateway4 Username:",
                default=env.gateway4_username or "admin@itential",
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.gateway4_username = new_val.strip()

        elif selected == "ssh_key":
            new_val = questionary.text(
                "SSH Key Path (leave blank to remove):",
                default=env.ssh_key or "",
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.ssh_key = new_val.strip()
            if env.ssh_key and env.deployment:
                env.deployment = propagate_ssh_key(env.deployment, env.ssh_key)

        elif selected == "mongo_tls_enabled":
            new_val = questionary.confirm(
                "Enable TLS for MongoDB connections?",
                default=getattr(env, "mongo_tls_enabled", False),
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.mongo_tls_enabled = new_val

        elif selected == "redis_tls_enabled":
            new_val = questionary.confirm(
                "Enable TLS for Redis connections?",
                default=getattr(env, "redis_tls_enabled", False),
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.redis_tls_enabled = new_val

        elif selected == "description":
            new_val = questionary.text(
                "Description:",
                default=env.description or "",
                style=get_qstyle(),
            ).ask()
            if new_val is None:
                raise KeyboardInterrupt
            env.description = new_val.strip()

        console.print()


def _collect_credentials_post_html_setup(env, skip_platform: bool = False) -> None:  # pylint: disable=too-many-branches
    """
    Collect and store credentials after creating an environment from a file.

    Mirrors the wizard credential-collection flow: prompts for each required
    secret based on tier, tests connections where possible, and offers
    fix/retry/skip on failures. Credentials go straight into the configured
    backend — never written to any file.

    ``skip_platform`` skips the Platform Client Secret prompt — used by the
    tier-upgrade flow, where the Standard environment already has a working
    Platform secret and only the Extended additions (Mongo/Redis/SSH) are new.
    """
    from platform_atlas.core.credentials import (
        CredentialKey, scoped_service_name, KeyringSecretStore, FileSecretStore,
    )
    from platform_atlas.core.init_setup import (
        _test_platform_oauth, _test_mongo_connection, _test_redis_connection,
        _warn_if_missing_authsource, ask_scheme_uri_optional,
        _configure_protocol_jumphost, _ask_tls_toggle,
    )

    tier = (getattr(env, "tier", None) or "extended").lower()
    backend = (getattr(env, "credential_backend", None) or "keyring").lower()
    env_name = env.name

    # ── SSH key file check ────────────────────────────────────────────────
    if getattr(env, "ssh_key", "") and tier in ("extended", "saas"):
        key_path = Path(env.ssh_key).expanduser()
        if not key_path.exists():
            console.print(
                f"\n  [{theme.warning}]⚠  SSH key not found: {env.ssh_key}[/{theme.warning}]"
            )
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice("Enter a different key path", value="fix"),
                    questionary.Choice("Remove the SSH key (you can set it later via env edit)", value="clear"),
                    questionary.Choice("Skip — keep the path as-is", value="skip"),
                ],
                style=get_qstyle(),
            ).ask()
            if action is None:
                raise KeyboardInterrupt
            if action == "fix":
                new_key = questionary.text(
                    "SSH Key Path:",
                    validate=lambda v: True if Path(v).expanduser().exists() else f"File not found: {v}",
                    style=get_qstyle(),
                ).ask()
                if new_key is None:
                    raise KeyboardInterrupt
                env.ssh_key = new_key.strip()
                if env.deployment:
                    env.deployment = propagate_ssh_key(env.deployment, env.ssh_key)
                get_environment_manager().save(env)
            elif action == "clear":
                env.ssh_key = ""
                if env.deployment:
                    env.deployment = propagate_ssh_key(env.deployment, "")
                get_environment_manager().save(env)
            # skip: leave as-is

    _section_header("Credentials", f"for '{env_name}'")
    console.print(
        f"  [{theme.text_dim}]Stored in the [{theme.text_primary}]{backend}[/{theme.text_primary}]"
        f" backend, scoped to this environment. Never written to any file.[/{theme.text_dim}]\n"
    )

    if backend == "vault":
        console.print(
            f"  [{theme.warning}]Vault backend — add these keys to your Vault KV secret manually:[/{theme.warning}]"
        )
        _keys_needed = []
        if tier in ("standard", "extended"):
            _keys_needed.append(CredentialKey.PLATFORM_SECRET.value)
        if tier == "extended":
            _keys_needed += [CredentialKey.MONGO_URI.value, CredentialKey.REDIS_URI.value]
        if getattr(env, "gateway4_uri", ""):
            _keys_needed.append(CredentialKey.GATEWAY4_PASSWORD.value)
        if getattr(env, "ssh_key", "") and tier in ("extended", "saas"):
            _keys_needed.append(CredentialKey.SSH_PASSPHRASE.value)
        for k in _keys_needed:
            console.print(f"  [{theme.text_dim}]  + {k}[/{theme.text_dim}]")
        console.print()

        if tier == "extended":
            mongo_tls_enabled = _ask_tls_toggle("MongoDB")
            redis_tls_enabled = _ask_tls_toggle("Redis")
            if mongo_tls_enabled != getattr(env, "mongo_tls_enabled", False) or \
                    redis_tls_enabled != getattr(env, "redis_tls_enabled", False):
                env.mongo_tls_enabled = mongo_tls_enabled
                env.redis_tls_enabled = redis_tls_enabled
                get_environment_manager().save(env)
        return

    service = scoped_service_name(env_name)
    substrate = FileSecretStore() if backend == "file" else KeyringSecretStore()

    # ── Platform Client Secret (Standard + Extended) ──────────────────────
    if tier in ("standard", "extended") and not skip_platform:
        console.print(
            f"  [{theme.text_dim}]Platform URL: {env.platform_uri}  ·  "
            f"Client ID: {env.platform_client_id}[/{theme.text_dim}]"
        )
        _verify_ssl = True
        try:
            from platform_atlas.core.paths import ATLAS_CONFIG_FILE as _cfg
            import json as _json
            if _cfg.is_file():
                _verify_ssl = bool(_json.loads(_cfg.read_text()).get("verify_ssl", True))
        except Exception:  # pylint: disable=broad-except
            pass

        while True:
            secret = questionary.password(
                "Platform Client Secret:",
                style=get_qstyle(),
            ).ask()
            if secret is None:
                raise KeyboardInterrupt

            if secret:
                console.print(f"  [{theme.text_dim}]Testing Platform OAuth ...[/{theme.text_dim}]")
                ok, detail = _test_platform_oauth(
                    env.platform_uri, env.platform_client_id, secret,
                    verify_ssl=_verify_ssl,
                )
                if ok:
                    console.print(f"  [{theme.success}]✓ {detail}[/{theme.success}]")
                    substrate.set(service, CredentialKey.PLATFORM_SECRET.value, secret)
                    break
                console.print(f"  [{theme.error}]✗ {detail}[/{theme.error}]")
                action = questionary.select(
                    "How would you like to proceed?",
                    choices=[
                        questionary.Choice("Re-enter the secret", value="retry"),
                        questionary.Choice("Fix Platform URL or Client ID", value="fix_url"),
                        questionary.Choice("Skip the test, save this secret anyway", value="skip"),
                        questionary.Choice("Cancel credential setup", value="cancel"),
                    ],
                    style=get_qstyle(),
                ).ask()
                if action is None or action == "cancel":
                    raise KeyboardInterrupt
                if action == "fix_url":
                    from platform_atlas.core.init_setup import _validate_http_url
                    new_uri = questionary.text(
                        "Platform URL:",
                        default=env.platform_uri or "",
                        validate=_validate_http_url,
                        style=get_qstyle(),
                    ).ask()
                    if new_uri is None:
                        raise KeyboardInterrupt
                    new_id = questionary.text(
                        "Platform Client ID:",
                        default=env.platform_client_id or "",
                        validate=lambda v: True if v.strip() else "Required",
                        style=get_qstyle(),
                    ).ask()
                    if new_id is None:
                        raise KeyboardInterrupt
                    env.platform_uri = new_uri.strip()
                    env.platform_client_id = new_id.strip()
                    get_environment_manager().save(env)
                elif action == "skip":
                    substrate.set(service, CredentialKey.PLATFORM_SECRET.value, secret)
                    console.print(f"  [{theme.warning}]⚠  Saved without verification[/{theme.warning}]")
                    break
                # "retry" — loop
            else:
                console.print(f"  [{theme.warning}]⚠  No secret entered — skipped[/{theme.warning}]")
                break

    # ── MongoDB URI (Extended only) ───────────────────────────────────────
    mongo_uri = ""
    redis_uri = ""
    mongo_tls_enabled = False
    redis_tls_enabled = False
    if tier == "extended":
        console.print()
        first_pass = True
        while True:
            uri = ask_scheme_uri_optional(
                "MongoDB Connection URI",
                schemes=("mongodb", "mongodb+srv"),
                instruction="(e.g. mongodb://user:pass@host:27017/admin) ",
            )
            if not uri:
                console.print(f"  [{theme.warning}]⚠  MongoDB URI skipped[/{theme.warning}]")
                break
            _warn_if_missing_authsource(uri)
            if first_pass:
                mongo_tls_enabled = _ask_tls_toggle("MongoDB")
                first_pass = False
            console.print(f"  [{theme.text_dim}]Testing MongoDB connection ...[/{theme.text_dim}]")
            ok, detail = _test_mongo_connection(uri, tls_enabled=mongo_tls_enabled)
            if ok:
                console.print(f"  [{theme.success}]✓ {detail}[/{theme.success}]")
                substrate.set(service, CredentialKey.MONGO_URI.value, uri)
                mongo_uri = uri
                break
            console.print(f"  [{theme.error}]✗ {detail}[/{theme.error}]")
            console.print(
                f"  [{theme.text_dim}]Note: if Mongo is only reachable via SSH tunnel "
                f"a direct test will fail — choose 'Skip the test' to save anyway.[/{theme.text_dim}]"
            )
            console.print(
                f"  [{theme.text_dim}]If you're using password authentication, make sure "
                f"the URI ends with ?authSource=<db-name> — MongoDB will reject the "
                f"connection if the user was created in a database other than "
                f"'admin'.[/{theme.text_dim}]"
            )
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice("Re-enter the URI", value="retry"),
                    questionary.Choice("Skip the test, save this URI anyway (advanced)", value="skip"),
                    questionary.Choice("Clear URI and continue without", value="clear"),
                    questionary.Choice("Cancel credential setup", value="cancel"),
                ],
                style=get_qstyle(),
            ).ask()
            if action is None or action == "cancel":
                raise KeyboardInterrupt
            if action == "skip":
                substrate.set(service, CredentialKey.MONGO_URI.value, uri)
                console.print(f"  [{theme.warning}]⚠  Saved without verification[/{theme.warning}]")
                mongo_uri = uri
                break
            if action == "clear":
                console.print(f"  [{theme.text_dim}]MongoDB URI cleared[/{theme.text_dim}]")
                mongo_tls_enabled = False
                break
            # "retry" — loop

        # ── Redis URI ─────────────────────────────────────────────────────
        console.print()
        first_pass = True
        while True:
            uri = ask_scheme_uri_optional(
                "Redis Connection URI",
                schemes=("redis", "rediss"),
                instruction="(e.g. redis://:pass@host:6379  or  redis://host:6379 for no auth) ",
            )
            if not uri:
                console.print(f"  [{theme.warning}]⚠  Redis URI skipped[/{theme.warning}]")
                break
            if first_pass:
                redis_tls_enabled = _ask_tls_toggle("Redis")
                first_pass = False
            console.print(f"  [{theme.text_dim}]Testing Redis connection ...[/{theme.text_dim}]")
            ok, detail = _test_redis_connection(uri, tls_enabled=redis_tls_enabled)
            if ok:
                console.print(f"  [{theme.success}]✓ {detail}[/{theme.success}]")
                substrate.set(service, CredentialKey.REDIS_URI.value, uri)
                redis_uri = uri
                break
            console.print(f"  [{theme.error}]✗ {detail}[/{theme.error}]")
            console.print(
                f"  [{theme.text_dim}]Note: if Redis is only reachable via SSH tunnel "
                f"a direct test will fail — choose 'Skip the test' to save anyway.[/{theme.text_dim}]"
            )
            action = questionary.select(
                "How would you like to proceed?",
                choices=[
                    questionary.Choice("Re-enter the URI", value="retry"),
                    questionary.Choice("Skip the test, save this URI anyway (advanced)", value="skip"),
                    questionary.Choice("Clear URI and continue without", value="clear"),
                    questionary.Choice("Cancel credential setup", value="cancel"),
                ],
                style=get_qstyle(),
            ).ask()
            if action is None or action == "cancel":
                raise KeyboardInterrupt
            if action == "skip":
                substrate.set(service, CredentialKey.REDIS_URI.value, uri)
                console.print(f"  [{theme.warning}]⚠  Saved without verification[/{theme.warning}]")
                redis_uri = uri
                break
            if action == "clear":
                console.print(f"  [{theme.text_dim}]Redis URI cleared[/{theme.text_dim}]")
                redis_tls_enabled = False
                break

        # ── Basic TLS toggle ─────────────────────────────────────────────
        if mongo_tls_enabled != getattr(env, "mongo_tls_enabled", False) or \
                redis_tls_enabled != getattr(env, "redis_tls_enabled", False):
            env.mongo_tls_enabled = mongo_tls_enabled
            env.redis_tls_enabled = redis_tls_enabled
            get_environment_manager().save(env)

        # ── Jumphost tunnel (advanced, optional) ───────────────────────────
        # A browser-based setup form may already have collected settings into
        # env.protocol_jumphost — verify those for real here; otherwise offer
        # the full interactive flow. Either way this is the same code path
        # capture uses (open_protocol_tunnel), so a pass here means it works.
        console.print()
        jumphost_dict = _configure_protocol_jumphost(
            mongo_uri, redis_uri,
            existing=getattr(env, "protocol_jumphost", None),
            mongo_tls_enabled=mongo_tls_enabled, redis_tls_enabled=redis_tls_enabled,
        )
        if jumphost_dict != getattr(env, "protocol_jumphost", None):
            env.protocol_jumphost = jumphost_dict
            get_environment_manager().save(env)

    # ── Gateway4 password ─────────────────────────────────────────────────
    if getattr(env, "gateway4_uri", ""):
        console.print()
        gw4_pass = questionary.password(
            f"Gateway4 Password  (for {env.gateway4_username or 'admin@itential'} at {env.gateway4_uri}):",
            style=get_qstyle(),
        ).ask()
        if gw4_pass is None:
            raise KeyboardInterrupt
        if gw4_pass:
            substrate.set(service, CredentialKey.GATEWAY4_PASSWORD.value, gw4_pass)
            console.print(f"  [{theme.success}]✓ Gateway4 Password saved[/{theme.success}]")
        else:
            console.print(f"  [{theme.warning}]⚠  Gateway4 Password skipped[/{theme.warning}]")

    # ── SSH key passphrase ────────────────────────────────────────────────
    if getattr(env, "ssh_key", "") and tier in ("extended", "saas"):
        console.print()
        ssh_pass = questionary.password(
            "SSH Key Passphrase  (press Enter if your key has no passphrase):",
            style=get_qstyle(),
        ).ask()
        if ssh_pass is None:
            raise KeyboardInterrupt
        if ssh_pass:
            substrate.set(service, CredentialKey.SSH_PASSPHRASE.value, ssh_pass)
            console.print(f"  [{theme.success}]✓ SSH Passphrase saved[/{theme.success}]")

    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}]  Credentials saved to "
        f"[bold]{backend}[/bold] backend.\n"
    )


def _apply_bundle_credentials(
    env, secrets: dict | None, vault_connection: dict | None, skip_platform: bool = False,
) -> None:
    """
    Write the secrets carried inside a decrypted setup bundle straight into the
    configured credential backend, testing connections where possible.

    Unlike :func:`_collect_credentials_post_html_setup`, nothing is prompted for
    — the values were already entered in the browser and encrypted.  A failed
    connection test only offers save-anyway / skip / cancel (a failure here is
    usually an SSH-tunnel-only target, not a wrong secret).  Vault backends store
    and verify the Vault *connection* block instead; the audited secrets live in
    Vault itself.

    ``skip_platform`` leaves the Platform Client Secret alone — used by the
    tier-upgrade flow, where Standard already stored a working Platform secret
    and only the Extended additions travel in the bundle.
    """
    from platform_atlas.core.credentials import (
        CredentialKey, scoped_service_name, KeyringSecretStore, FileSecretStore, applicable_keys,
    )
    from platform_atlas.core.init_setup import (
        _test_platform_oauth, _test_mongo_connection, _test_redis_connection,
    )

    tier = (getattr(env, "tier", None) or "extended").lower()
    backend = (getattr(env, "credential_backend", None) or "keyring").lower()
    env_name = env.name
    service = scoped_service_name(env_name)

    _section_header("Credentials", f"for '{env_name}' — from encrypted bundle")

    # Non-blocking heads-up: a bundle is portable, so an SSH key path baked into
    # it may not exist on the machine doing the import.
    ssh_key = getattr(env, "ssh_key", "")
    if ssh_key and tier in ("extended", "saas") and not Path(ssh_key).expanduser().exists():
        console.print(
            f"  [{theme.warning}]⚠  SSH key not found on this host: {ssh_key}[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Capture will need it — correct the path with "
            f"'platform-atlas env edit' if it's wrong.[/{theme.text_dim}]\n"
        )

    if backend == "vault":
        _apply_bundle_vault(env, vault_connection, service)
        return

    substrate = FileSecretStore() if backend == "file" else KeyringSecretStore()
    applicable = {k.value for k in applicable_keys(tier)}
    secrets = secrets if isinstance(secrets, dict) else {}
    stored: list[str] = []
    console.print(
        f"  [{theme.text_dim}]Writing to the [{theme.text_primary}]{backend}[/{theme.text_primary}]"
        f" backend, scoped to this environment. Never written to any file.[/{theme.text_dim}]\n"
    )

    def _store_with_test(key: CredentialKey, value: str, label: str, test_fn) -> None:
        """Store *value* under *key*; if *test_fn* is given, verify first and
        offer save-anyway / skip / cancel on failure."""
        if not value:
            return
        if test_fn is None:
            substrate.set(service, key.value, value)
            stored.append(label)
            console.print(f"  [{theme.success}]✓ {label} stored[/{theme.success}]")
            return
        console.print(f"  [{theme.text_dim}]Testing {label} ...[/{theme.text_dim}]")
        ok, detail = test_fn(value)
        if ok:
            substrate.set(service, key.value, value)
            stored.append(label)
            console.print(f"  [{theme.success}]✓ {detail}[/{theme.success}]")
            return
        console.print(f"  [{theme.error}]✗ {detail}[/{theme.error}]")
        console.print(
            f"  [{theme.text_dim}]A target reachable only via SSH tunnel will fail a direct "
            f"test — 'Save anyway' keeps the value from your bundle.[/{theme.text_dim}]"
        )
        if key is CredentialKey.MONGO_URI:
            console.print(
                f"  [{theme.text_dim}]If you're using password authentication, make sure "
                f"the URI ends with ?authSource=<db-name> — MongoDB will reject the "
                f"connection if the user was created in a database other than "
                f"'admin'.[/{theme.text_dim}]"
            )
        action = questionary.select(
            "How would you like to proceed?",
            choices=[
                questionary.Choice("Save anyway — keep this value from the bundle", value="save"),
                questionary.Choice(f"Skip {label} (set later via config credentials)", value="skip"),
                questionary.Choice("Cancel credential setup", value="cancel"),
            ],
            style=get_qstyle(),
        ).ask()
        if action is None or action == "cancel":
            raise KeyboardInterrupt
        if action == "save":
            substrate.set(service, key.value, value)
            stored.append(label)
            console.print(f"  [{theme.warning}]⚠  Saved without verification[/{theme.warning}]")

    # ── Platform Client Secret ────────────────────────────────────────────
    if not skip_platform and CredentialKey.PLATFORM_SECRET.value in applicable and getattr(env, "platform_uri", ""):
        secret = secrets.get(CredentialKey.PLATFORM_SECRET.value, "")
        if secret:
            _verify_ssl = True
            try:
                from platform_atlas.core.paths import ATLAS_CONFIG_FILE as _cfg
                import json as _json
                if _cfg.is_file():
                    _verify_ssl = bool(_json.loads(_cfg.read_text()).get("verify_ssl", True))
            except Exception:  # pylint: disable=broad-except
                pass
            _store_with_test(
                CredentialKey.PLATFORM_SECRET, secret, "Platform OAuth",
                lambda s: _test_platform_oauth(
                    env.platform_uri, env.platform_client_id, s, verify_ssl=_verify_ssl),
            )

    # ── MongoDB / Redis URIs ──────────────────────────────────────────────
    # The bundle already carries mongo_tls_enabled/redis_tls_enabled on `env`
    # (set from the form's JSON before this function runs) — apply them here
    # so the test connects exactly the way a real capture would, instead of
    # testing a bare URI and failing a TLS-only target.
    mongo_tls_enabled = getattr(env, "mongo_tls_enabled", False)
    redis_tls_enabled = getattr(env, "redis_tls_enabled", False)
    if CredentialKey.MONGO_URI.value in applicable:
        _store_with_test(
            CredentialKey.MONGO_URI, secrets.get(CredentialKey.MONGO_URI.value, ""),
            "MongoDB connection",
            lambda u: _test_mongo_connection(u, tls_enabled=mongo_tls_enabled),
        )
    if CredentialKey.REDIS_URI.value in applicable:
        _store_with_test(
            CredentialKey.REDIS_URI, secrets.get(CredentialKey.REDIS_URI.value, ""),
            "Redis connection",
            lambda u: _test_redis_connection(u, tls_enabled=redis_tls_enabled),
        )

    # ── Jumphost tunnel (advanced, optional) ──────────────────────────────
    # Not a secret — it lives on the Environment itself, not this bundle's
    # secrets map — but verifying it needs a live Mongo/Redis URI, so it
    # happens here alongside them rather than duplicating the URI lookups.
    if tier == "extended" and getattr(env, "protocol_jumphost", None):
        from platform_atlas.core.init_setup import _configure_protocol_jumphost
        existing_jumphost = env.protocol_jumphost
        jumphost_dict = _configure_protocol_jumphost(
            secrets.get(CredentialKey.MONGO_URI.value, ""),
            secrets.get(CredentialKey.REDIS_URI.value, ""),
            existing=existing_jumphost,
            mongo_tls_enabled=mongo_tls_enabled, redis_tls_enabled=redis_tls_enabled,
        )
        if jumphost_dict != existing_jumphost:
            env.protocol_jumphost = jumphost_dict
            get_environment_manager().save(env)

    # ── Gateway4 password / SSH secrets (no live test) ────────────────────
    if CredentialKey.GATEWAY4_PASSWORD.value in applicable and getattr(env, "gateway4_uri", ""):
        _store_with_test(
            CredentialKey.GATEWAY4_PASSWORD, secrets.get(CredentialKey.GATEWAY4_PASSWORD.value, ""),
            "Gateway4 Password", None,
        )
    if CredentialKey.SSH_PASSPHRASE.value in applicable:
        _store_with_test(
            CredentialKey.SSH_PASSPHRASE, secrets.get(CredentialKey.SSH_PASSPHRASE.value, ""),
            "SSH Key Passphrase", None,
        )
    if CredentialKey.SSH_PASSWORD.value in applicable:
        _store_with_test(
            CredentialKey.SSH_PASSWORD, secrets.get(CredentialKey.SSH_PASSWORD.value, ""),
            "SSH Password", None,
        )

    if stored:
        console.print(
            f"\n  [{theme.success}]✓[/{theme.success}]  Credentials saved to "
            f"[bold]{backend}[/bold] backend.\n"
        )
    else:
        console.print(
            f"\n  [{theme.warning}]⚠  This bundle carried no credentials.[/{theme.warning}] "
            f"[{theme.text_dim}]Set them with 'platform-atlas config credentials' before "
            f"running a capture.[/{theme.text_dim}]\n"
        )


def _apply_bundle_vault(env, vault_connection: dict | None, service: str) -> None:
    """Store and verify the Vault *connection* block carried in a bundle.

    Atlas never writes the audited secrets to Vault (they live there already and
    are read at runtime) — so a Vault bundle carries the connection details
    (URL + auth) instead.  We persist those to the local secret store and check
    we can actually reach Vault.
    """
    from platform_atlas.core.credentials import (
        VaultConfig, VaultAuthMethod, VaultBackend, CredentialKey,
    )
    from platform_atlas.core.init_setup import _explicit_substrate
    from platform_atlas.core.exceptions import CredentialError

    if not isinstance(vault_connection, dict) or not vault_connection.get("url"):
        console.print(
            f"  [{theme.warning}]⚠  No Vault connection details in this file — any existing "
            f"Vault configuration is left unchanged.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Make sure the credentials this tier needs are present in your "
            f"Vault KV, or run 'platform-atlas config credentials' to set up Vault.[/{theme.text_dim}]\n"
        )
        return

    vss = (getattr(env, "vault_secret_store", None) or "file").lower()
    try:
        auth_method = VaultAuthMethod(vault_connection.get("auth_method", "token"))
    except ValueError:
        auth_method = VaultAuthMethod.TOKEN

    cfg = VaultConfig(
        url=vault_connection["url"],
        auth_method=auth_method,
        token=vault_connection.get("token") or None,
        role_id=vault_connection.get("role_id") or None,
        secret_id=vault_connection.get("secret_id") or None,
        wrapping_token=vault_connection.get("wrapping_token") or None,
        token_file_path=vault_connection.get("token_file_path") or None,
        mount_point=vault_connection.get("mount_point") or "secret",
        secret_path=vault_connection.get("secret_path") or "platform-atlas",
        verify_ssl=bool(vault_connection.get("verify_ssl", True)),
        namespace=vault_connection.get("namespace") or None,
    )

    console.print(f"  [{theme.text_dim}]Testing Vault connection at {cfg.url} ...[/{theme.text_dim}]")
    connected = False
    try:
        VaultBackend(cfg, service=service)
        console.print(f"  [{theme.success}]✓ Connected to Vault at {cfg.url}[/{theme.success}]")
        connected = True
    except CredentialError as exc:
        console.print(f"  [{theme.error}]✗ Vault connection failed: {exc}[/{theme.error}]")
        action = questionary.select(
            "How would you like to proceed?",
            choices=[
                questionary.Choice("Save the settings anyway (fix Vault later)", value="save"),
                questionary.Choice("Cancel credential setup", value="cancel"),
            ],
            style=get_qstyle(),
        ).ask()
        if action is None or action == "cancel":
            raise KeyboardInterrupt

    VaultBackend.save_config_to_keyring(
        cfg, service=service, store=_explicit_substrate("vault", vss),
    )
    store_label = "encrypted local file" if vss == "file" else "OS keyring"
    console.print(
        f"  [{theme.success}]✓[/{theme.success}]  Vault connection settings saved to the "
        f"[bold]{store_label}[/bold]."
    )
    if connected:
        console.print(
            f"  [{theme.text_dim}]Atlas expects audited secrets (e.g. "
            f"{CredentialKey.PLATFORM_SECRET.value}) at {cfg.mount_point}/{cfg.secret_path} "
            f"in Vault.[/{theme.text_dim}]"
        )
    console.print()


def _shred_bundle_file(src: Path) -> None:
    """Best-effort secure delete of an imported encrypted bundle."""
    try:
        length = src.stat().st_size
        with open(src, "r+b") as fh:
            fh.write(os.urandom(max(length, 1)))
            fh.flush()
            os.fsync(fh.fileno())
        src.unlink()
        console.print(
            f"  [{theme.text_dim}]Shredded imported bundle {src.name} "
            f"(pass --keep-file to retain it).[/{theme.text_dim}]"
        )
    except Exception:  # pylint: disable=broad-except
        try:
            src.unlink()
        except Exception:  # pylint: disable=broad-except
            console.print(
                f"  [{theme.warning}]⚠  Could not remove {src} — delete it manually; "
                f"it contains encrypted credentials.[/{theme.warning}]"
            )


def _handle_env_create_from_file(  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
    file_path: str, env_name_override: str | None, keep_file: bool = False,
) -> int:
    """Create an environment from a JSON file or an encrypted setup bundle.

    An encrypted bundle (``.atlasenv.enc``, marked ``_atlas_bundle``) is
    decrypted with a passphrase, then treated exactly like a browser-wizard
    JSON: name validation, duplicate check, review/edit loop, and — because the
    bundle already carries the secrets — a pre-seeded, connection-tested
    credential write (no re-typing).  A successful bundle import shreds the file
    unless ``keep_file`` is set.  Plain ``--from-file`` JSON (no
    ``_html_setup``) skips the summary and credential steps.
    """
    import json as _json
    from platform_atlas.core import bundle_crypto

    src = Path(file_path).expanduser()
    if not src.exists():
        console.print(f"\n  [{theme.error}]File not found: {src}[/{theme.error}]\n")
        return 1
    if src.suffix.lower() not in (".json", ".enc"):
        console.print(f"\n  [{theme.warning}]Expected a .json file or .enc bundle.[/{theme.warning}]\n")
        return 1

    try:
        parsed = _json.loads(src.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        console.print(f"\n  [{theme.error}]Invalid JSON: {exc}[/{theme.error}]\n")
        return 1

    # ── Encrypted bundle: prompt for the passphrase and decrypt ────────────
    was_encrypted = bundle_crypto.is_encrypted_bundle(parsed)
    if was_encrypted:
        _section_header("Encrypted Bundle", f"decrypting {src.name}")
        console.print(
            f"  [{theme.text_dim}]Enter the passphrase shown by the setup builder "
            f"when this bundle was generated.[/{theme.text_dim}]\n"
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
                if remaining:
                    console.print(
                        f"  [{theme.error}]✗ Wrong passphrase — {remaining} "
                        f"attempt{'s' if remaining > 1 else ''} left.[/{theme.error}]"
                    )
                else:
                    console.print(f"  [{theme.error}]✗ Wrong passphrase.[/{theme.error}]\n")
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

    is_html_setup = bool(raw.pop("_html_setup", False))
    bundle_secrets = raw.pop("secrets", None)
    vault_connection = raw.pop("vault_connection", None)

    if env_name_override:
        raw["name"] = env_name_override

    if not raw.get("name"):
        console.print(
            f"\n  [{theme.error}]The JSON file must include a 'name' field.[/{theme.error}]\n"
        )
        return 1

    # ── Name validation ───────────────────────────────────────────────────
    candidate = raw["name"]
    if not validate_env_name(candidate):
        suggestion = normalize_env_name(candidate)
        console.print(
            f"\n  [{theme.warning}]⚠  '{candidate}' is not a valid environment name.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Names must be lowercase, start with a letter or digit, "
            f"and contain only letters, numbers, hyphens, underscores, or dots.[/{theme.text_dim}]"
        )
        choices = []
        if suggestion and validate_env_name(suggestion) and suggestion != candidate:
            choices.append(questionary.Choice(
                title=f"Use suggestion: '{suggestion}'",
                value=("use", suggestion),
            ))
        choices.append(questionary.Choice(title="Enter a different name", value=("enter", "")))
        choices.append(questionary.Choice(title="Cancel", value=("cancel", "")))

        action, value = questionary.select(
            "How would you like to proceed?",
            choices=choices,
            style=get_qstyle(),
        ).ask() or ("cancel", "")

        if action == "cancel":
            return 1
        if action == "use":
            raw["name"] = value
        elif action == "enter":
            def _v(v: str) -> bool | str:
                v = v.strip()
                if not v:
                    return "Required"
                if not validate_env_name(v):
                    return "Lowercase letters, numbers, hyphens, underscores, or dots only"
                return True
            new_name = questionary.text(
                "Environment name:",
                validate=_v,
                style=get_qstyle(),
            ).ask()
            if new_name is None:
                return 1
            raw["name"] = new_name.strip()

    # ── Duplicate name check ──────────────────────────────────────────────
    mgr = get_environment_manager()
    if mgr.exists(raw["name"]):
        console.print(
            f"\n  [{theme.warning}]⚠  An environment named '{raw['name']}' already exists.[/{theme.warning}]"
        )
        action = questionary.select(
            "How would you like to proceed?",
            choices=[
                questionary.Choice("Enter a different name", value="rename"),
                questionary.Choice("Overwrite the existing environment", value="overwrite"),
                questionary.Choice("Cancel", value="cancel"),
            ],
            style=get_qstyle(),
        ).ask()
        if action is None or action == "cancel":
            return 1
        if action == "rename":
            def _v_rename(v: str) -> bool | str:
                v = v.strip()
                if not v:
                    return "Required"
                if not validate_env_name(v):
                    return "Lowercase letters, numbers, hyphens, underscores, or dots only"
                if mgr.exists(v):
                    return f"'{v}' also exists — choose a different name"
                return True
            new_name = questionary.text(
                "New environment name:",
                validate=_v_rename,
                style=get_qstyle(),
            ).ask()
            if new_name is None:
                return 1
            raw["name"] = new_name.strip()
        # "overwrite" — continue with the original name

    # The organization name lives only in config.json (set at `platform-atlas
    # init`, changed via `config edit`). Environments never carry it, so any
    # `organization_name` in an imported/hand-edited bundle is dropped by
    # Environment.from_dict below rather than overriding the global value.
    try:
        env = Environment.from_dict(raw)
    except Exception as exc:  # pylint: disable=broad-except
        console.print(f"\n  [{theme.error}]Could not load environment: {exc}[/{theme.error}]\n")
        return 1

    # ── SaaS/Gateway 5 sanity check ────────────────────────────────────────
    # A saas_gateway_kind of "gateway5"/"gw4-gw5" needs a GW5 deployment node
    # (SSH, server-config, or Compose/Helm file) to have anything to capture.
    # env-setup.html previously had a bug that could drop this node even when
    # the form fields were filled in — flag it here instead of silently
    # saving an environment that will hit "No modules available to run".
    # Not marked `partial` — that flag would route a future `env create
    # <name>` fast-path to `env edit`'s topology editor, which drives the
    # Extended-tier wizard and isn't SaaS-aware.
    if env.tier == "saas" and env.saas_gateway_kind in ("gateway5", "gw4-gw5"):
        gw5_nodes = [
            n for n in (env.deployment or {}).get("nodes", [])
            if "gateway5" in (n.get("modules") or [])
        ]
        if not gw5_nodes:
            console.print(
                f"\n  [{theme.warning}]⚠  No Gateway 5 connection details were found "
                f"in this bundle.[/{theme.warning}]"
            )
            console.print(
                f"  [{theme.text_dim}]A SaaS Gateway 5 audit needs an SSH host, server-config "
                f"path, or Compose/Helm file to read env vars from. Without one, capture will "
                f"find no modules to run. The environment will still be saved — fill in the "
                f"Gateway 5 details and run 'platform-atlas env create {raw['name']}' again "
                f"(choosing overwrite) before capturing.[/{theme.text_dim}]"
            )

    # ── HTML-setup: summary + review/edit loop ────────────────────────────
    if is_html_setup:
        _section_header("Review Environment", f"loaded from {src.name}")
        try:
            _run_env_edit_loop(env)
        except KeyboardInterrupt:
            console.print(f"\n  [{theme.text_dim}]Cancelled[/{theme.text_dim}]\n")
            return 1

    ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    mgr.save(env)
    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}]  Environment "
        f"[bold]{env.name}[/bold] saved"
    )

    # ── Credential handling ───────────────────────────────────────────────
    # Encrypted bundle: the secrets travelled with it, so write them straight in
    # (pre-seeded, no re-typing). Plain HTML-setup JSON: prompt interactively.
    if was_encrypted:
        try:
            _apply_bundle_credentials(env, bundle_secrets, vault_connection)
        except KeyboardInterrupt:
            console.print(
                f"\n  [{theme.warning}]Credential setup cancelled. The bundle was kept — "
                f"re-run 'env create --from-file {src}' to try again, or "
                f"'config credentials' to set them manually.[/{theme.warning}]\n"
            )
            mgr.set_active(env.name)
            ui.next_step("platform-atlas session create <name>", label="Environment Ready")
            return 0
        except Exception as exc:  # pylint: disable=broad-except
            # The environment is already saved; don't let a credential-store
            # failure (e.g. an unavailable keyring) crash or shred the bundle.
            console.print(
                f"\n  [{theme.error}]Could not store credentials: {exc}[/{theme.error}]"
            )
            console.print(
                f"  [{theme.warning}]The environment was saved and the bundle kept. "
                f"Fix the issue, then run 'platform-atlas config credentials' — or re-run "
                f"'env create --from-file {src}'.[/{theme.warning}]\n"
            )
            mgr.set_active(env.name)
            ui.next_step("platform-atlas config credentials", label="Environment Saved")
            return 0
        # Success — the bundle has done its job; shred it unless asked to keep.
        if not keep_file:
            _shred_bundle_file(src)
    elif is_html_setup:
        try:
            _collect_credentials_post_html_setup(env)
        except KeyboardInterrupt:
            console.print(
                f"\n  [{theme.warning}]Credential entry cancelled. "
                f"Run 'platform-atlas config credentials' to set them later.[/{theme.warning}]\n"
            )
            mgr.set_active(env.name)
            ui.next_step("platform-atlas session create <name>", label="Environment Ready")
            return 0

    set_active = questionary.confirm(
        f"Set '{env.name}' as the active environment?",
        default=True,
        style=get_qstyle(),
    ).ask()
    if set_active:
        mgr.set_active(env.name)
        console.print(f"  [{theme.text_dim}]Active environment → {env.name}[/{theme.text_dim}]")

    console.print()
    ui.next_step("platform-atlas session create <name>", label="Environment Ready")
    return 0


def _ask_env_create_method() -> str | None:
    """Ask whether to enter the new environment's details in the terminal or the browser."""
    return questionary.select(
        "How would you like to set up this environment?",
        choices=[
            questionary.Choice(
                "Here in the terminal  — a guided step-by-step wizard",
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


@registry.register("env", "create", description="Create a new environment")
def handle_env_create(args: Namespace) -> int:
    """Create a new environment via the interactive wizard."""
    env_name = getattr(args, "env_name", None)
    from_env = getattr(args, "from_env", None)
    from_file = getattr(args, "from_file", None)
    keep_file = getattr(args, "keep_file", False)

    if from_file:
        return _handle_env_create_from_file(from_file, env_name, keep_file=keep_file)

    # Fast-path: if a specific name was given and a partial env exists, route
    # the user to env edit before the full wizard runs.  The wizard itself also
    # has this guard (for cases where no name was given up front), but catching
    # it here lets us print a clearer, command-level message.
    if env_name:
        mgr = get_environment_manager()
        if mgr.exists(env_name):
            try:
                _existing = mgr.load(env_name)
                if getattr(_existing, "partial", False):
                    console.print(
                        f"\n  [{theme.warning}]⚠  '{env_name}' has an incomplete setup.[/{theme.warning}]"
                    )
                    console.print(
                        f"  [{theme.text_dim}]Run: platform-atlas env edit {env_name}[/{theme.text_dim}]\n"
                    )
                    return 1
            except Exception:
                pass

    # Copying from an existing environment is a fully CLI-driven operation —
    # there's nothing for the browser form to add, so skip the method prompt.
    if not from_env:
        method = _ask_env_create_method()
        if method is None:
            raise KeyboardInterrupt
        if method == "cancel":
            console.print(f"\n  [{theme.text_dim}]Cancelled. Nothing was created.[/{theme.text_dim}]\n")
            return 1
        if method == "browser":
            return _handle_env_create_html_setup()

    try:
        result = create_environment_wizard(env_name=env_name, from_env=from_env)
    except KeyboardInterrupt:
        # _bail() raises KeyboardInterrupt so the wrapping main() handler
        # can emit a consistent non-zero exit code; we just return 1.
        return 1
    except SystemExit:
        # Legacy: in case something below still raises SystemExit, treat it
        # as a cancellation. New code should use KeyboardInterrupt.
        return 1

    if result:
        ui.next_step("platform-atlas session create <name>", label="Environment Ready")
        return 0
    return 1


@registry.register("env", "remove", description="Remove an environment")
def handle_env_remove(args: Namespace) -> int:
    """Delete an environment file."""
    mgr = get_environment_manager()
    target = getattr(args, "env_name", None)

    if target is None:
        console.print(f"\n  [{theme.error}]Specify an environment: platform-atlas env remove <name>[/{theme.error}]\n")
        return 1

    if not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]\n")
        return 1

    # Confirm
    force = getattr(args, "force", False)
    if not force:
        confirm = questionary.confirm(
            f"Delete environment '{target}'? This cannot be undone.",
            default=False,
            style=get_qstyle(),
        ).ask()
        if not confirm:
            console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
            return 1

    # If this is the active environment, clear it
    active = mgr.get_active_name()
    if active == target:
        mgr.clear_active()
        console.print(f"  [{theme.text_dim}]Cleared active environment (was {target})[/{theme.text_dim}]")

    mgr.remove(target)
    console.print(f"\n  [{theme.success}]✓[/{theme.success}] Environment '{target}' removed\n")
    return 0


# ── Editable field descriptors for env edit ───────────────────────
# Field types:
#   "text"   — free-form non-empty string
#   "url"    — http:// or https:// URL (validated)
#   "choice" — fixed enum (see handler for choices)
#   "bool"   — yes/no confirm
_EDITABLE_FIELDS = [
    ("description",          "Description",            "text"),
    ("platform_uri",         "Platform URI",           "url"),
    ("platform_client_id",   "Platform Client ID",     "text"),
    ("credential_backend",   "Credential Backend",     "choice"),
    ("legacy_profile",       "Legacy Profile (2023.x)","text"),
    ("gateway4_uri",         "Gateway4 URI",           "url"),
    ("gateway4_username",    "Gateway4 Username",      "text"),
    ("ssh_key",              "SSH Key Path",            "text"),
    ("log_path_override",         "Platform Log Directory", "text"),
    ("webserver_log_path_override", "Webserver Log File",     "text"),
    ("mongo_log_path_override",   "MongoDB Log File",       "text"),
    ("mongo_tls_enabled",         "MongoDB TLS",            "bool"),
    ("redis_tls_enabled",         "Redis TLS",              "bool"),
    ("debug_export_raw_capture",  "Debug: Export Raw Capture", "bool"),
    ("env_tint",                  "Banner Tint",               "choice"),
]

_BACKEND_CHOICES = ["keyring", "vault"]


_DEFAULT_K8S_NODE_LABELS = frozenset({"k8s-platform", "k8s-gateway5"})


def _count_extra_k8s_namespaces(deployment: dict | None) -> int:
    """Count nodes beyond the default k8s-platform/k8s-gateway5 pair.

    Rare — most environments have exactly one namespace and this is 0."""
    if not deployment:
        return 0
    return sum(
        1 for n in deployment.get("nodes", [])
        if n.get("label") not in _DEFAULT_K8S_NODE_LABELS
    )


def _manage_extra_k8s_namespaces(env) -> bool:
    """List/add/remove additional Kubernetes namespace targets on ``env``.

    Operates directly on ``env.deployment["nodes"]`` (the same raw dict
    shape TargetNode.to_dict()/from_dict() round-trip through) since these
    extra nodes have no other persisted representation. Returns True if
    anything changed.
    """
    from platform_atlas.core.topology import NodeRole, TargetNode
    from platform_atlas.core.init_setup import ask_text_optional, _validate_yaml_file

    if env.deployment is None:
        env.deployment = {"mode": "kubernetes", "nodes": []}
    nodes = env.deployment.setdefault("nodes", [])
    changed = False

    while True:
        extras = [n for n in nodes if n.get("label") not in _DEFAULT_K8S_NODE_LABELS]
        console.print()
        if extras:
            console.print(f"  [{theme.text_dim}]Additional namespaces:[/{theme.text_dim}]")
            for n in extras:
                console.print(
                    f"    • {n.get('label')} ({n.get('role')}, "
                    f"ns: {n.get('kubectl_namespace') or 'default'})"
                )
        else:
            console.print(f"  [{theme.text_dim}]No additional namespaces configured.[/{theme.text_dim}]")

        action = questionary.select(
            "Additional namespaces",
            choices=[
                questionary.Choice("Add a namespace", value="add"),
                *(
                    [questionary.Choice("Remove a namespace", value="remove")]
                    if extras else []
                ),
                questionary.Choice("Back", value="back"),
            ],
            style=get_qstyle(),
        ).ask()
        if action is None or action == "back":
            break

        if action == "add":
            ns_role_val = questionary.select(
                "What does this namespace contain?",
                choices=[
                    questionary.Choice("Platform (IAP)", value="iap"),
                    questionary.Choice("Gateway5 (IAG5)", value="iag"),
                ],
                style=get_qstyle(),
            ).ask()
            if ns_role_val is None:
                continue
            ns_role = NodeRole(ns_role_val)

            ns_num = len(extras) + 1
            ns_default_label = f"k8s-{'platform' if ns_role == NodeRole.IAP else 'gateway5'}-{ns_num}"
            ns_label = ask_text_optional("Label", instruction=f"(default: {ns_default_label}) ") or ns_default_label
            if any(n.get("label") == ns_label for n in nodes):
                console.print(f"  [{theme.error}]✗ Label '{ns_label}' is already in use.[/{theme.error}]\n")
                continue
            ns_namespace = ask_text_optional("Kubernetes namespace", "(e.g. itential-blue, iag5-east) ")
            ns_context = ask_text_optional("kubectl context", "(leave blank for current context) ")
            ns_kubeconfig = ask_text_optional(
                "kubeconfig path", "(leave blank to use the default kubeconfig/cluster) "
            )
            ns_values_path = questionary.path(
                "values.yaml path",
                only_directories=False,
                validate=_validate_yaml_file,
                style=get_qstyle(),
            ).ask()
            if ns_values_path is None:
                continue

            node = TargetNode(
                role=ns_role,
                host="kubernetes",
                label=ns_label,
                transport="kubernetes",
                kubectl_namespace=ns_namespace,
                kubectl_context=ns_context,
                kubeconfig_path=ns_kubeconfig,
                values_yaml_path=str(Path(ns_values_path.strip()).expanduser().resolve()),
                modules=["kubernetes", "gateway5"] if ns_role == NodeRole.IAG else ["kubernetes"],
            )
            nodes.append(node.to_dict())
            changed = True
            console.print(f"  [{theme.success}]✓ Added namespace '{ns_label}'[/{theme.success}]\n")

        elif action == "remove":
            remove_label = questionary.select(
                "Remove which namespace?",
                choices=[
                    questionary.Choice(f"{n.get('label')} ({n.get('role')})", value=n.get("label"))
                    for n in extras
                ] + [questionary.Choice("Cancel", value=None)],
                style=get_qstyle(),
            ).ask()
            if not remove_label:
                continue
            env.deployment["nodes"] = [n for n in nodes if n.get("label") != remove_label]
            nodes = env.deployment["nodes"]
            changed = True
            console.print(f"  [{theme.success}]✓ Removed namespace '{remove_label}'[/{theme.success}]\n")

    if changed:
        # capture_scope must be all_nodes once any additional namespace
        # exists — primary_only would silently drop it (see ask_deployment()
        # and DeploymentTopology.capture_targets()).
        final_extras = [n for n in nodes if n.get("label") not in _DEFAULT_K8S_NODE_LABELS]
        env.deployment["capture_scope"] = "all_nodes" if final_extras else "primary_only"

    return changed


def _prompt_and_apply_field(
    env: Environment, field_name: str, label: str, field_type: str,
) -> bool | None:
    """Prompt for a new value of one Environment field and apply it if changed.

    Used by `env edit`'s field-picker loop. Does not save — the caller
    decides when to persist. Returns ``True`` if the value changed, ``False``
    if the user answered but left it the same, or ``None`` if they cancelled
    the prompt.
    """
    current = getattr(env, field_name, None)

    if field_type == "choice" and field_name == "credential_backend":
        new_value = questionary.select(
            f"{label} (current: {current or 'keyring'}):",
            choices=_BACKEND_CHOICES,
            default=current if current in _BACKEND_CHOICES else "keyring",
            style=get_qstyle(),
        ).ask()
        if new_value is None:
            return None
    elif field_type == "choice" and field_name == "env_tint":
        _dl_choices = [
            questionary.Choice("(none) — default theme", value="none"),
            questionary.Choice("low — green tint (dev/test)", value="low"),
            questionary.Choice("medium — amber tint (staging)", value="medium"),
            questionary.Choice("high — pink tint (production)", value="high"),
        ]
        current_dl = current or "none"
        new_value = questionary.select(
            f"{label}:",
            choices=_dl_choices,
            default=next((c for c in _dl_choices if c.value == current_dl), _dl_choices[0]),
            style=get_qstyle(),
        ).ask()
        if new_value is None:
            return None
        new_value = None if new_value == "none" else new_value
    elif field_type == "bool":
        new_value = questionary.confirm(
            f"{label} (current: {'on' if current else 'off'})?",
            default=bool(current),
            style=get_qstyle(),
        ).ask()
        if new_value is None:
            return None
    elif field_type == "url":
        # Platform / Gateway4 URLs — must be http(s) and have a hostname.
        # Blank input is allowed for optional fields (gateway4_uri); when
        # the field already had a value, blank means "clear".
        from platform_atlas.core.init_setup import _validate_http_url
        prompt_text = f"{label}"
        if current:
            prompt_text += f" (current: {current}; leave blank to clear)"

        def _v_url(v: str) -> bool | str:
            s = (v or "").strip()
            if not s:
                return True  # blank → clear (handled below)
            return _validate_http_url(s)

        new_value = questionary.text(
            prompt_text + ":",
            default=str(current) if current else "",
            validate=_v_url,
            style=get_qstyle(),
        ).ask()
        if new_value is None:
            return None
        new_value = new_value.strip()
    else:
        prompt_text = f"{label}"
        if current:
            prompt_text += f" (current: {current})"
        if field_name == "ssh_key":
            prompt_text += " (leave blank to remove)"

        new_value = questionary.text(
            prompt_text + ":",
            default=str(current) if current else "",
            style=get_qstyle(),
        ).ask()
        if new_value is None:
            return None
        new_value = new_value.strip()

    # Apply the change
    old_value = getattr(env, field_name, None)
    if new_value == old_value:
        return False

    # Booleans persist as-is; empty strings collapse to None so the
    # dataclass default kicks back in and overlay-merge skips them.
    if field_type == "bool":
        setattr(env, field_name, bool(new_value))
    else:
        setattr(env, field_name, new_value if new_value else None)
    # SSH key: propagate into deployment nodes and ssh_defaults so the
    # transport layer reads the updated path without requiring a topology
    # re-wizard.
    if field_name == "ssh_key" and env.deployment:
        env.deployment = propagate_ssh_key(env.deployment, new_value or "")
    return True


def _run_deployment_topology_editor(env: Environment) -> bool:
    """Run the full deployment-topology wizard and apply the result to *env*.

    Used by `env edit`'s "Replace topology" action — it needs to build a
    fresh topology and land it on the environment, moving any SSH secrets
    into the credential store
    (never the env JSON) along the way. Does not save `env` — the caller
    decides when to persist. Always returns True (raises KeyboardInterrupt
    on cancel, matching the rest of this module's `.ask()` convention).
    """
    from platform_atlas.core.init_setup import ask_deployment, _display_topology_review
    from platform_atlas.core.topology import DeploymentTopology

    new_deployment, k8s_meta = ask_deployment()

    # Pop credentials before storing the deployment dict — they must never
    # reach the env JSON file. Store them in the credential backend instead.
    ssh_passphrase = ""
    ssh_password = ""
    if new_deployment:
        for node_dict in new_deployment.get("nodes", []):
            node_dict.pop("ssh_key_passphrase", None)
            node_dict.pop("ssh_password", None)
        sd = new_deployment.get("ssh_defaults") or {}
        ssh_passphrase = sd.pop("key_passphrase", "")
        ssh_password = sd.pop("password", "")

    env.deployment = new_deployment
    # Persist Kubernetes metadata so K8s-only fields (values_yaml, kubectl
    # context/namespace) survive a topology re-edit.
    if k8s_meta:
        if "values_yaml_path" in k8s_meta:
            env.values_yaml_path = k8s_meta.get("values_yaml_path", "")
        if "iag5_values_yaml_path" in k8s_meta:
            env.iag5_values_yaml_path = k8s_meta.get("iag5_values_yaml_path", "")
        if "kubectl_context" in k8s_meta:
            env.kubectl_context = k8s_meta.get("kubectl_context", "")
        if "kubectl_namespace" in k8s_meta:
            env.kubectl_namespace = k8s_meta.get("kubectl_namespace", "")
        if "kubectl_binary_path" in k8s_meta:
            env.kubectl_binary_path = k8s_meta.get("kubectl_binary_path", "")
        if "use_kubectl" in k8s_meta:
            env.use_kubectl = bool(k8s_meta.get("use_kubectl", False))

    # Store SSH credentials in the credential backend (never the env JSON).
    # Explicitly scoped to this env's own name — never the ambient active
    # environment — so this is correct even when fixing a non-active env.
    backend = (getattr(env, "credential_backend", None) or "keyring").strip().lower()
    if backend != "vault":
        from platform_atlas.core.credentials import (
            CredentialKey, FileSecretStore, KeyringSecretStore, scoped_service_name,
        )
        substrate = FileSecretStore() if backend == "file" else KeyringSecretStore()
        scoped = scoped_service_name(env.name)
        try:
            if ssh_passphrase:
                substrate.set(scoped, CredentialKey.SSH_PASSPHRASE.value, ssh_passphrase)
            if ssh_password:
                substrate.set(scoped, CredentialKey.SSH_PASSWORD.value, ssh_password)
        except Exception as exc:
            console.print(f"  [{theme.error}]✗ Failed to store SSH credentials: {exc}[/{theme.error}]")
    else:
        from platform_atlas.core.credentials import CredentialKey
        if ssh_passphrase:
            console.print(
                f"  [{theme.warning}]⚠ SSH passphrase provided but Vault is read-only — "
                f"add '{CredentialKey.SSH_PASSPHRASE.value}' to your Vault secret manually.[/{theme.warning}]"
            )
        if ssh_password:
            console.print(
                f"  [{theme.warning}]⚠ SSH password provided but Vault is read-only — "
                f"add '{CredentialKey.SSH_PASSWORD.value}' to your Vault secret manually.[/{theme.warning}]"
            )

    topology = DeploymentTopology.from_dict(new_deployment)
    scope = new_deployment.get("capture_scope", "primary_only")
    _display_topology_review(topology, capture_scope=scope)
    console.print(f"  [{theme.success}]✓ Deployment topology updated[/{theme.success}]\n")
    return True


@registry.register("env", "edit", description="Edit an environment's settings")
def handle_env_edit(args: Namespace) -> int:
    """Interactively edit an existing environment's settings."""
    mgr = get_environment_manager()
    target = getattr(args, "env_name", None)

    # Default to the active environment if none specified
    if target is None:
        target = mgr.get_active_name()
        if target is None:
            console.print(f"\n  [{theme.warning}]No active environment set.[/{theme.warning}]")
            console.print(f"  [{theme.text_dim}]Specify one: platform-atlas env edit <name>[/{theme.text_dim}]\n")
            return 1

    if not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]\n")
        return 1

    env = mgr.load(target)
    active = mgr.get_active_name()
    active_badge = f"  [{theme.success}](active)[/{theme.success}]" if target == active else ""

    console.print()
    console.print(
        f"[bold {theme.primary_glow}]Edit Environment:[/bold {theme.primary_glow}] "
        f"[bold]{target}[/bold]{active_badge}\n"
    )

    changed = False

    # -- Retroactive Gateway4 API detection ------------------------------------
    # If the environment has gateway4 in its topology but no API credentials
    # configured, prompt the user to set them up now.
    _has_gw4 = False
    try:
        if env.deployment:
            _has_gw4 = any(
                "gateway4" in node.get("modules", [])
                for node in env.deployment.get("nodes", [])
            )
    except Exception:
        pass

    if _has_gw4 and not env.gateway4_uri:
        console.print(
            f"  [{theme.warning}]⚠ Gateway4 detected in topology but API credentials "
            f"are not configured.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Atlas uses the Gateway4 REST API as the primary source "
            f"for config collection.[/{theme.text_dim}]"
        )
        configure_gw4 = questionary.confirm(
            "Configure Gateway4 API connection now?",
            default=True,
            style=get_qstyle(),
        ).ask()
        if configure_gw4 is None:
            raise KeyboardInterrupt
        if configure_gw4:
            from platform_atlas.core.init_setup import _validate_http_url
            gw4_uri = questionary.text(
                "Gateway4 API URI (e.g., http://gateway-host:8083)",
                validate=lambda v: True if not v.strip() else _validate_http_url(v),
                style=get_qstyle(),
            ).ask()
            if gw4_uri is None:
                raise KeyboardInterrupt
            gw4_uri = gw4_uri.strip()
            if gw4_uri:
                env.gateway4_uri = gw4_uri
                changed = True

            gw4_user = questionary.text(
                "Gateway4 Username",
                default="admin@itential",
                style=get_qstyle(),
            ).ask()
            if gw4_user is None:
                raise KeyboardInterrupt
            if gw4_user:
                env.gateway4_username = gw4_user
                changed = True

            if env.credential_backend == "keyring":
                gw4_pass = questionary.password(
                    "Gateway4 Password (hidden)",
                    style=get_qstyle(),
                ).ask()
                if gw4_pass is None:
                    raise KeyboardInterrupt
                if gw4_pass:
                    try:
                        from platform_atlas.core.credentials import (
                            credential_store, CredentialKey,
                        )
                        credential_store().set(CredentialKey.GATEWAY4_PASSWORD, gw4_pass)
                        console.print(
                            f"  [{theme.success}]✓ Gateway4 password stored[/{theme.success}]"
                        )
                    except Exception as e:
                        console.print(
                            f"  [{theme.error}]✗ Failed to store password: {e}[/{theme.error}]"
                        )
            else:
                from platform_atlas.core.credentials import CredentialKey
                console.print(
                    f"  [{theme.text_dim}]Add '{CredentialKey.GATEWAY4_PASSWORD.value}' "
                    f"to your Vault secret.[/{theme.text_dim}]"
                )
            console.print()

    while True:
        # Build choices showing current values
        field_choices = []
        for field_name, label, _ in _EDITABLE_FIELDS:
            current = getattr(env, field_name, None)
            display = str(current) if current else "[not set]"
            if len(display) > 50:
                display = display[:47] + "..."
            field_choices.append(_env_field_choice(label, display, field_name))

        field_choices.append(questionary.Choice(
            title=[(f"fg:{theme.accent}", "Deployment Topology       (opens topology wizard)")],
            value="_deployment",
        ))
        _edit_tier = (getattr(env, "tier", None) or "extended").lower()
        if _edit_tier == "extended":
            field_choices.append(questionary.Choice(
                title=[(f"fg:{theme.accent}", "Jumphost Tunnel           (advanced, Mongo/Redis via SSH)")],
                value="_jumphost",
            ))
        field_choices.append(questionary.Choice(title="Done", value="_done"))

        selected = questionary.select(
            "Select a field to edit:",
            choices=field_choices,
            style=get_qstyle(),
        ).ask()

        if selected is None or selected == "_done":
            break

        # Deployment topology — sub-menu: edit node / change scope / replace
        if selected == "_deployment":
            # A SaaS env's shape (one gateway, gateway_only mode) is fixed at
            # create time — the platform topology wizard would let it grow
            # IAP/Mongo/Redis nodes it must never have.
            if (getattr(env, "tier", None) or "") == "saas":
                console.print(
                    f"  [{theme.warning}]This is a SaaS (single-gateway) environment — its "
                    f"topology is fixed at create time.[/{theme.warning}]\n"
                    f"  [{theme.text_dim}]To point at a different gateway or change the GW5 "
                    f"source, create a new environment with "
                    f"[bold]platform-atlas env create[/bold].[/{theme.text_dim}]\n"
                )
                continue

            from platform_atlas.core.init_setup import _display_topology_review
            from platform_atlas.core.topology import DeploymentTopology, DeploymentMode

            # Kubernetes settings (namespace, context, values.yaml, kubectl) live on
            # the environment itself, not on any node — so the SSH-oriented node editor
            # can't touch them. For K8s envs, offer a dedicated settings editor instead
            # of "Edit a node", which has nothing meaningful to change on a K8s node.
            _is_k8s_env = False
            if env.deployment:
                try:
                    _is_k8s_env = (
                        DeploymentTopology.from_dict(env.deployment).mode == DeploymentMode.KUBERNETES
                    )
                except Exception:
                    _is_k8s_env = False

            _topo_action = questionary.select(
                "Deployment Topology — what would you like to change?",
                choices=[
                    (
                        questionary.Choice(
                            "Edit Kubernetes settings — namespace, context, values.yaml, kubectl",
                            value="kubernetes",
                        )
                        if _is_k8s_env
                        else questionary.Choice(
                            "Edit a node           — change hostname, transport, or socket", value="node"
                        )
                    ),
                    questionary.Choice("Change capture scope  — primary-only vs all nodes", value="scope"),
                    questionary.Choice("Replace topology      — re-run the full topology wizard", value="replace"),
                    questionary.Choice("Back", value="back"),
                ],
                style=get_qstyle(),
            ).ask()
            if _topo_action is None or _topo_action == "back":
                continue

            if _topo_action == "scope":
                _current_scope = (env.deployment or {}).get("capture_scope", "primary_only")
                _new_scope = questionary.select(
                    "Capture scope",
                    choices=[
                        questionary.Choice(
                            f"Primary only  — one node per role (current)" if _current_scope == "primary_only"
                            else "Primary only  — one node per role",
                            value="primary_only",
                        ),
                        questionary.Choice(
                            f"All nodes     — every node in topology (current)" if _current_scope == "all_nodes"
                            else "All nodes     — every node in topology",
                            value="all_nodes",
                        ),
                    ],
                    style=get_qstyle(),
                ).ask()
                if _new_scope is None:
                    continue
                if env.deployment is None:
                    env.deployment = {}
                env.deployment["capture_scope"] = _new_scope
                changed = True
                console.print(f"  [{theme.success}]✓ Capture scope → {_new_scope}[/{theme.success}]\n")
                continue

            if _topo_action == "kubernetes":
                from platform_atlas.core.init_setup import (
                    ask_text_optional, _validate_yaml_file, _display_kubernetes_review,
                )

                _k8s_touched = False
                while True:
                    _k_use = "Enabled" if env.use_kubectl else "Disabled"
                    _k_field = questionary.select(
                        "Kubernetes settings — what would you like to change?",
                        choices=[
                            questionary.Choice(
                                f"Platform values.yaml   {env.values_yaml_path or '(not set)'}",
                                value="values",
                            ),
                            questionary.Choice(
                                f"Platform chart defaults {env.values_yaml_chart_defaults_path or '(not set)'}",
                                value="values_defaults",
                            ),
                            questionary.Choice(
                                f"IAG5 values.yaml       {env.iag5_values_yaml_path or '(not set)'}",
                                value="iag5",
                            ),
                            questionary.Choice(
                                f"IAG5 chart defaults    {env.iag5_values_yaml_chart_defaults_path or '(not set)'}",
                                value="iag5_defaults",
                            ),
                            questionary.Choice(
                                f"Use kubectl (live)     {_k_use}",
                                value="use_kubectl",
                            ),
                            questionary.Choice(
                                f"kubectl context        {env.kubectl_context or '(current)'}",
                                value="context",
                            ),
                            questionary.Choice(
                                f"kubectl namespace      {env.kubectl_namespace or '(default)'}",
                                value="namespace",
                            ),
                            questionary.Choice(
                                f"kubectl binary path    {env.kubectl_binary_path or '(from PATH)'}",
                                value="binary",
                            ),
                            questionary.Choice(
                                f"Additional namespaces  {_count_extra_k8s_namespaces(env.deployment)} configured",
                                value="namespaces",
                            ),
                            questionary.Choice("Back", value="back"),
                        ],
                        style=get_qstyle(),
                    ).ask()
                    if _k_field is None or _k_field == "back":
                        break

                    if _k_field == "namespaces":
                        if _manage_extra_k8s_namespaces(env):
                            changed = True
                            _k8s_touched = True
                        continue

                    if _k_field in ("values", "iag5", "values_defaults", "iag5_defaults"):
                        _labels = {
                            "values": "Platform values.yaml",
                            "iag5": "IAG5 values.yaml",
                            "values_defaults": "Platform chart-defaults values.yaml",
                            "iag5_defaults": "IAG5 chart-defaults values.yaml",
                        }
                        _currents = {
                            "values": env.values_yaml_path,
                            "iag5": env.iag5_values_yaml_path,
                            "values_defaults": env.values_yaml_chart_defaults_path,
                            "iag5_defaults": env.iag5_values_yaml_chart_defaults_path,
                        }
                        _label = _labels[_k_field]
                        _current = _currents[_k_field]
                        _v = questionary.path(
                            f"{_label} path (leave blank to clear)",
                            only_directories=False,
                            default=_current or "",
                            style=get_qstyle(),
                        ).ask()
                        if _v is None:
                            raise KeyboardInterrupt
                        _v = _v.strip()
                        if _v:
                            _ok = _validate_yaml_file(_v)
                            if _ok is not True:
                                console.print(f"  [{theme.error}]✗ {_ok}[/{theme.error}]\n")
                                continue
                            _resolved = str(Path(_v).expanduser().resolve())
                        else:
                            _resolved = ""
                        if _k_field == "values":
                            env.values_yaml_path = _resolved
                        elif _k_field == "iag5":
                            env.iag5_values_yaml_path = _resolved
                        elif _k_field == "values_defaults":
                            env.values_yaml_chart_defaults_path = _resolved
                        else:
                            env.iag5_values_yaml_chart_defaults_path = _resolved

                    elif _k_field == "use_kubectl":
                        _u = questionary.confirm(
                            "Read configuration from the live cluster via kubectl?",
                            default=bool(env.use_kubectl),
                            style=get_qstyle(),
                        ).ask()
                        if _u is None:
                            raise KeyboardInterrupt
                        env.use_kubectl = bool(_u)

                    elif _k_field == "context":
                        env.kubectl_context = ask_text_optional(
                            "kubectl context", "(leave blank for current context) "
                        )

                    elif _k_field == "namespace":
                        env.kubectl_namespace = ask_text_optional(
                            "Kubernetes namespace", "(e.g. itential, default) "
                        )

                    elif _k_field == "binary":
                        _b = questionary.text(
                            "Path to kubectl binary (leave blank to use PATH)",
                            default=env.kubectl_binary_path or "",
                            style=get_qstyle(),
                        ).ask()
                        if _b is None:
                            raise KeyboardInterrupt
                        _b = _b.strip()
                        if _b:
                            _bp = Path(_b).expanduser()
                            if not (_bp.is_file() and os.access(_bp, os.X_OK)):
                                console.print(
                                    f"  [{theme.error}]✗ '{_bp}' is not a valid executable.[/{theme.error}]\n"
                                )
                                continue
                            env.kubectl_binary_path = str(_bp)
                        else:
                            env.kubectl_binary_path = ""

                    changed = True
                    _k8s_touched = True
                    console.print(f"  [{theme.success}]✓ Kubernetes settings updated[/{theme.success}]\n")

                if _k8s_touched:
                    # Warn (non-blocking) if the edits left no reachable data source.
                    if not env.use_kubectl and not env.values_yaml_path:
                        console.print(
                            f"  [{theme.warning}]⚠ No data source configured — enable kubectl or set a "
                            f"values.yaml path, or Atlas can't collect from this cluster.[/{theme.warning}]\n"
                        )
                    _k8s_meta = {
                        "values_yaml_path": env.values_yaml_path,
                        "iag5_values_yaml_path": env.iag5_values_yaml_path,
                        "values_yaml_chart_defaults_path": env.values_yaml_chart_defaults_path,
                        "iag5_values_yaml_chart_defaults_path": env.iag5_values_yaml_chart_defaults_path,
                        "use_kubectl": env.use_kubectl,
                        "kubectl_context": env.kubectl_context,
                        "kubectl_namespace": env.kubectl_namespace,
                    }
                    _display_kubernetes_review(
                        DeploymentTopology.from_dict(env.deployment), _k8s_meta
                    )
                continue

            if _topo_action == "node" and env.deployment:
                _topo = DeploymentTopology.from_dict(env.deployment)
                _node_choices = [
                    questionary.Choice(
                        title=(
                            f"{_n.label:<20}  {_n.role.value:<8}  {_n.transport:<14}  "
                            f"{_n.host}"
                        ),
                        value=_i,
                    )
                    for _i, _n in enumerate(_topo.nodes)
                ]
                _node_choices.append(questionary.Choice("Back", value=-1))
                _node_idx = questionary.select(
                    "Select a node to edit:",
                    choices=_node_choices,
                    style=get_qstyle(),
                ).ask()
                if _node_idx is None or _node_idx == -1:
                    continue

                _node = _topo.nodes[_node_idx]
                console.print(
                    f"\n  [{theme.primary_glow}]Editing: {_node.label}[/{theme.primary_glow}]  "
                    f"[{theme.text_dim}]role={_node.role.value}  transport={_node.transport}[/{theme.text_dim}]\n"
                )

                _node_field = questionary.select(
                    "What would you like to change?",
                    choices=[
                        questionary.Choice(f"Hostname          {_node.host}", value="host"),
                        questionary.Choice(f"Transport         {_node.transport}", value="transport"),
                        *(
                            [
                                questionary.Choice(
                                    f"CM Socket path    {_node.ssh_control_socket or '(not set)'}",
                                    value="socket",
                                ),
                                questionary.Choice(
                                    f"CM SSH destination {_node.ssh_control_target or '(not set)'}",
                                    value="cm_target",
                                ),
                            ]
                            if _node.transport == "control_master"
                            else [
                                questionary.Choice(
                                    f"SSH username      {_node.ssh_user}",
                                    value="ssh_user",
                                ),
                            ]
                        ),
                        questionary.Choice("Back", value="back"),
                    ],
                    style=get_qstyle(),
                ).ask()
                if _node_field is None or _node_field == "back":
                    continue

                if _node_field == "host":
                    _new_val = questionary.text(
                        "New hostname or IP",
                        default=_node.host,
                        style=get_qstyle(),
                    ).ask()
                    if _new_val is None:
                        raise KeyboardInterrupt
                    _new_val = _new_val.strip()
                    if _new_val:
                        _topo.nodes[_node_idx].host = _new_val
                        if not _topo.nodes[_node_idx].label or _topo.nodes[_node_idx].label.endswith(_node.host):
                            _topo.nodes[_node_idx].label = f"{_node.role.value}-{_new_val}"

                elif _node_field == "transport":
                    _new_transport = questionary.select(
                        "Transport",
                        choices=["ssh", "control_master", "local"],
                        default=_node.transport,
                        style=get_qstyle(),
                    ).ask()
                    if _new_transport is None:
                        raise KeyboardInterrupt
                    _topo.nodes[_node_idx].transport = _new_transport

                elif _node_field == "socket":
                    _new_val = questionary.text(
                        "Socket path",
                        default=_node.ssh_control_socket,
                        style=get_qstyle(),
                    ).ask()
                    if _new_val is None:
                        raise KeyboardInterrupt
                    _topo.nodes[_node_idx].ssh_control_socket = _new_val.strip()

                elif _node_field == "cm_target":
                    _new_val = questionary.text(
                        "SSH destination (e.g. user@host@psmp)",
                        default=_node.ssh_control_target,
                        style=get_qstyle(),
                    ).ask()
                    if _new_val is None:
                        raise KeyboardInterrupt
                    _topo.nodes[_node_idx].ssh_control_target = _new_val.strip()

                elif _node_field == "ssh_user":
                    _new_val = questionary.text(
                        "SSH username",
                        default=_node.ssh_user,
                        style=get_qstyle(),
                    ).ask()
                    if _new_val is None:
                        raise KeyboardInterrupt
                    _new_val = _new_val.strip()
                    if _new_val:
                        _topo.nodes[_node_idx].ssh_user = _new_val

                # Rebuild the deployment dict from the updated topology
                _scope = (env.deployment or {}).get("capture_scope", "primary_only")
                _ssh_defaults = (env.deployment or {}).get("ssh_defaults", {})
                env.deployment = _topo.to_dict()
                env.deployment["capture_scope"] = _scope
                if _ssh_defaults:
                    env.deployment["ssh_defaults"] = _ssh_defaults
                changed = True

                _display_topology_review(_topo, capture_scope=_scope)
                console.print(f"  [{theme.success}]✓ Node updated[/{theme.success}]\n")
                continue

            # "replace" — fall through to the existing full-wizard path
            _run_deployment_topology_editor(env)
            changed = True
            continue

        # Jumphost tunnel — advanced, optional, Extended tier only
        if selected == "_jumphost":
            _jh_backend = (getattr(env, "credential_backend", None) or "keyring").strip().lower()
            _mongo_uri = ""
            _redis_uri = ""
            if _jh_backend == "vault":
                console.print(
                    f"  [{theme.warning}]⚠  Vault backend — Atlas can't read the live "
                    f"MongoDB/Redis URI here to test a tunnel.[/{theme.warning}]"
                )
                _mongo_uri = questionary.text(
                    "MongoDB URI to test against (optional, not stored)",
                    style=get_qstyle(),
                ).ask()
                if _mongo_uri is None:
                    raise KeyboardInterrupt
                _redis_uri = questionary.text(
                    "Redis URI to test against (optional, not stored)",
                    style=get_qstyle(),
                ).ask()
                if _redis_uri is None:
                    raise KeyboardInterrupt
                _mongo_uri = _mongo_uri.strip()
                _redis_uri = _redis_uri.strip()
            else:
                from platform_atlas.core.credentials import (
                    CredentialKey, FileSecretStore, KeyringSecretStore, scoped_service_name,
                )
                _jh_substrate = FileSecretStore() if _jh_backend == "file" else KeyringSecretStore()
                _jh_scoped = scoped_service_name(target)
                _mongo_uri = _jh_substrate.get(_jh_scoped, CredentialKey.MONGO_URI.value) or ""
                _redis_uri = _jh_substrate.get(_jh_scoped, CredentialKey.REDIS_URI.value) or ""

            from platform_atlas.core.init_setup import _configure_protocol_jumphost
            _existing_jumphost = getattr(env, "protocol_jumphost", None)
            _jumphost_dict = _configure_protocol_jumphost(
                _mongo_uri, _redis_uri, existing=_existing_jumphost,
                mongo_tls_enabled=getattr(env, "mongo_tls_enabled", False),
                redis_tls_enabled=getattr(env, "redis_tls_enabled", False),
            )
            if _jumphost_dict != _existing_jumphost:
                env.protocol_jumphost = _jumphost_dict
                mgr.save(env)
                changed = True
                console.print(f"  [{theme.success}]✓ Jumphost tunnel settings updated[/{theme.success}]\n")
            else:
                console.print(f"  [{theme.text_dim}]No change[/{theme.text_dim}]\n")
            continue

        # Find the field descriptor
        field_entry = next(
            (f for f in _EDITABLE_FIELDS if f[0] == selected), None
        )
        if field_entry is None:
            continue

        field_name, label, field_type = field_entry
        result = _prompt_and_apply_field(env, field_name, label, field_type)
        if result is None:
            continue
        if result:
            changed = True
            console.print(f"  [{theme.success}]✓ {label} updated[/{theme.success}]\n")
        else:
            console.print(f"  [{theme.text_dim}]No change[/{theme.text_dim}]\n")

    # Save if anything changed
    if changed:
        mgr.save(env)
        console.print(f"  [{theme.success}]✓[/{theme.success}] Environment [{theme.accent}]{target}[/{theme.accent}] saved\n")
    else:
        console.print(f"  [{theme.text_dim}]No changes made[/{theme.text_dim}]\n")

    return 0


@registry.register("env", "sockets", description="Check and manage ControlMaster socket health")
def handle_env_sockets(args: Namespace) -> int:
    """Show the status of all ControlMaster sockets for an environment.

    With --clean, removes stale socket files so fresh master connections can
    be opened without 'socket exists but is not responding' errors.
    """
    import stat as _stat
    import subprocess as _sp
    from rich.table import Table
    from rich import box as _box

    mgr = get_environment_manager()
    target = getattr(args, "env_name", None) or mgr.get_active_name()
    clean = getattr(args, "clean", False)

    if target is None:
        console.print(
            f"\n  [{theme.warning}]Specify an environment: "
            f"platform-atlas env sockets <name>[/{theme.warning}]\n"
        )
        return 1

    if not mgr.exists(target):
        console.print(f"\n  [{theme.error}]Environment '{target}' not found[/{theme.error}]\n")
        return 1

    env = mgr.load(target)
    if not env.deployment and not env.protocol_jumphost:
        console.print(
            f"\n  [{theme.text_dim}]No deployment topology configured for '{target}'.[/{theme.text_dim}]\n"
        )
        return 0

    from platform_atlas.core.topology import DeploymentTopology, NodeRole, TargetNode
    cm_nodes = []
    if env.deployment:
        topo = DeploymentTopology.from_dict(env.deployment)
        cm_nodes = [n for n in topo.nodes if n.transport == "control_master"]

    # The jumphost tunnel (Mongo/Redis) reuses the same ControlMaster socket
    # mechanism as a topology node — model it as one so it gets the exact
    # same status/open/clean handling below for free, instead of duplicating it.
    _jh = env.protocol_jumphost
    if _jh and _jh.get("control_socket") and _jh.get("ssh_target"):
        cm_nodes.append(TargetNode(
            role=NodeRole.CUSTOM,
            label="jumphost (Mongo/Redis tunnel)",
            transport="control_master",
            ssh_control_socket=_jh["control_socket"],
            ssh_control_target=_jh["ssh_target"],
            ssh_port=int(_jh.get("port") or 22),
        ))

    if not cm_nodes:
        console.print(
            f"\n  [{theme.text_dim}]No ControlMaster nodes in '{target}'.[/{theme.text_dim}]\n"
        )
        return 0

    def _check_socket(node) -> tuple[str, str]:
        """Return (status_key, detail_message)."""
        if not node.ssh_control_target:
            return "unconfigured", "SSH destination not set — run env edit to fix"
        path = node.ssh_control_socket
        if not path:
            return "missing", "No socket path configured"
        p = Path(path)
        if not p.exists():
            return "missing", f"{path}"
        if _stat.S_ISSOCK(p.stat().st_mode) if hasattr(_stat, "S_ISSOCK") else True:
            try:
                chk = _sp.run(
                    ["ssh", "-O", "check", "-S", path, node.ssh_control_target],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if chk.returncode == 0:
                    return "ok", path
                return "stale", path
            except Exception:  # pylint: disable=broad-except
                return "stale", path
        return "stale", f"{path}  (not a socket file)"

    table = Table(
        box=_box.ROUNDED,
        show_lines=False,
        pad_edge=True,
        border_style=theme.border_primary,
    )
    table.add_column("Node", style=f"bold {theme.text_primary}", min_width=16)
    table.add_column("Socket", style=theme.text_dim, min_width=32)
    table.add_column("Status", min_width=12)
    table.add_column("SSH destination", style=theme.text_dim, min_width=24)

    open_mode = getattr(args, "open_sockets", False)
    show_all = getattr(args, "show_all_nodes", False)

    _status_map = {}
    hidden_count = 0
    for node in cm_nodes:
        status_key, detail = _check_socket(node)
        _status_map[node.label] = status_key
        if status_key == "unconfigured" and not show_all:
            hidden_count += 1
            continue
        status_display = {
            "ok":           f"[{theme.success}]✓  open[/{theme.success}]",
            "missing":      f"[{theme.error}]✗  not found[/{theme.error}]",
            "stale":        f"[{theme.warning}]⚠  stale[/{theme.warning}]",
            "unconfigured": f"[{theme.text_dim}]—  no SSH dest[/{theme.text_dim}]",
        }.get(status_key, status_key)
        table.add_row(
            node.label,
            detail if status_key in ("missing", "stale", "unconfigured") else node.ssh_control_socket,
            status_display,
            node.ssh_control_target or f"[{theme.text_dim}](not set)[/{theme.text_dim}]",
        )

    if not clean:
        console.print()
        console.print(table)
        if hidden_count:
            noun = "node" if hidden_count == 1 else "nodes"
            console.print(
                f"  [{theme.text_dim}]{hidden_count} {noun} with no SSH destination hidden"
                f" — use --all to show them[/{theme.text_dim}]"
            )
        console.print()

    def _get_persist() -> str:
        """Read control_persist_minutes from config; fall back to 60m if context not loaded."""
        try:
            from platform_atlas.core.context import ctx as _ctx
            return f"{_ctx().config.control_persist_minutes}m"
        except Exception:  # pylint: disable=broad-except
            return "60m"

    def _ssh_open_cmd(node) -> str:
        """Single-line ssh -M command string for a node (safe to copy-paste)."""
        _persist = _get_persist()
        port_arg = f"-p {node.ssh_port} " if node.ssh_port != 22 else ""
        return (f"ssh -M -S {node.ssh_control_socket} {port_arg}-o ControlPersist={_persist} "
                f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null "
                f"-fN {node.ssh_control_target}")

    def _auto_open_node(node) -> bool:
        """Open a ControlMaster master connection interactively. Returns True on success."""
        import subprocess as _sp2
        _persist = _get_persist()
        sock_path = Path(node.ssh_control_socket)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass
        port_args = ["-p", str(node.ssh_port)] if node.ssh_port != 22 else []
        cmd = [
            "ssh", "-M", "-S", node.ssh_control_socket,
            *port_args,
            "-o", f"ControlPersist={_persist}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-fN", node.ssh_control_target,
        ]
        try:
            result = _sp2.run(cmd)  # stdio inherited — user sees MFA/password prompt
            return result.returncode == 0
        except Exception as exc:  # pylint: disable=broad-except
            console.print(f"  [{theme.error}]Error opening {node.label}: {exc}[/{theme.error}]")
            return False

    def _do_clean_stale(nodes_to_clean):
        for node in nodes_to_clean:
            p = Path(node.ssh_control_socket)
            try:
                p.unlink(missing_ok=True)
                console.print(f"  [{theme.success}]✓[/{theme.success}] Removed stale socket: {node.ssh_control_socket}")
            except OSError as exc:
                console.print(f"  [{theme.error}]✗[/{theme.error}] Could not remove {node.ssh_control_socket}: {exc}")

    stale_nodes = [n for n in cm_nodes if _status_map.get(n.label) == "stale"]
    actionable_nodes = [n for n in cm_nodes if _status_map.get(n.label) in ("missing", "stale") and n.ssh_control_target]

    if clean:
        if stale_nodes:
            _do_clean_stale(stale_nodes)
            console.print(f"\n  [{theme.text_dim}]Re-open master connections with: platform-atlas env sockets {target} --open[/{theme.text_dim}]")
        else:
            console.print(f"\n  [{theme.success}]✓[/{theme.success}]  No stale sockets — nothing to clean.")
        console.print()
        return 0

    if not actionable_nodes and not stale_nodes:
        console.print()
        return 0

    if open_mode:
        # --open flag: skip the prompt, go straight to auto-open
        if stale_nodes:
            _do_clean_stale(stale_nodes)
            console.print()
        for node in actionable_nodes:
            console.print(f"  [{theme.text_dim}]Opening {node.label} — complete authentication when prompted...[/{theme.text_dim}]")
            ok = _auto_open_node(node)
            if ok:
                chk_status, _ = _check_socket(node)
                if chk_status == "ok":
                    console.print(f"  [{theme.success}]✓[/{theme.success}] {node.label} — socket open")
                else:
                    console.print(f"  [{theme.warning}]⚠[/{theme.warning}] {node.label} — SSH exited but socket check failed; verify manually")
            else:
                console.print(f"  [{theme.error}]✗[/{theme.error}] {node.label} — SSH command failed")
            console.print()
        return 0

    # Interactive: ask the user what they'd like to do
    has_unconfigured = any(_status_map.get(n.label) == "unconfigured" for n in cm_nodes)

    choices = [
        questionary.Choice("Open them automatically  (Atlas runs SSH, you enter credentials)", value="auto"),
        questionary.Choice("Show me the commands to open manually", value="show"),
        questionary.Choice("Done", value="done"),
    ]
    action = questionary.select(
        "Some sockets need to be opened. What would you like to do?",
        choices=choices,
        style=get_qstyle(),
    ).ask()
    if action is None or action == "done":
        if has_unconfigured:
            console.print(
                f"\n  [{theme.text_dim}]Nodes with no SSH destination: run "
                f"platform-atlas env edit {target} → Deployment Topology → Edit a node[/{theme.text_dim}]"
            )
        console.print()
        return 0

    if action == "show":
        console.print()
        for node in actionable_nodes:
            console.print(f"  [{theme.text_dim}]{node.label}:[/{theme.text_dim}]")
            console.print(f"  {_ssh_open_cmd(node)}")
            console.print()
        if has_unconfigured:
            console.print(
                f"  [{theme.text_dim}]Nodes with no SSH destination: run "
                f"platform-atlas env edit {target} → Deployment Topology → Edit a node[/{theme.text_dim}]"
            )
        return 0

    # action == "auto"
    if stale_nodes:
        _do_clean_stale(stale_nodes)
        console.print()
    for node in actionable_nodes:
        console.print(f"  [{theme.text_dim}]Opening {node.label} — complete authentication when prompted...[/{theme.text_dim}]")
        ok = _auto_open_node(node)
        if ok:
            chk_status, _ = _check_socket(node)
            if chk_status == "ok":
                console.print(f"  [{theme.success}]✓[/{theme.success}] {node.label} — socket open")
            else:
                console.print(f"  [{theme.warning}]⚠[/{theme.warning}] {node.label} — SSH exited but socket check failed; verify manually")
        else:
            console.print(f"  [{theme.error}]✗[/{theme.error}] {node.label} — SSH command failed")
        console.print()

    console.print()
    return 0
