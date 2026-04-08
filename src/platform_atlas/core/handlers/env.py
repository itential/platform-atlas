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
from argparse import Namespace

import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from platform_atlas.core.registry import registry
from platform_atlas.core.environment import (
    get_environment_manager,
    validate_env_name,
)
from platform_atlas.core.init_setup import QSTYLE, create_environment_wizard
from platform_atlas.core import ui

console = Console()
theme = ui.theme
logger = logging.getLogger(__name__)


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

    table = Table(
        box=box.ROUNDED,
        show_lines=False,
        pad_edge=True,
        border_style=theme.border_primary,
    )
    table.add_column("Name", style=f"bold {theme.text_primary}", min_width=16)
    table.add_column("Organization", style=theme.text_primary, min_width=18)
    table.add_column("Description", style=theme.text_secondary, min_width=28)
    table.add_column("Platform URI", style=theme.text_dim, min_width=24)
    table.add_column("Backend", style=theme.text_dim, min_width=10)
    table.add_column("Active", justify="center", min_width=8)

    for name in env_names:
        try:
            env = mgr.load(name)
        except Exception:
            table.add_row(name, f"[{theme.error}]error loading[/{theme.error}]", "", "", "")
            continue

        is_active = name == active
        active_badge = (
            f"[{theme.success}]●[/{theme.success}]"
            if is_active
            else f"[{theme.text_dim}]·[/{theme.text_dim}]"
        )
        name_display = (
            f"[bold {theme.accent}]{name}[/bold {theme.accent}]"
            if is_active
            else name
        )

        table.add_row(
            name_display,
            env.organization_name or f"[{theme.text_dim}]—[/{theme.text_dim}]",
            env.description or f"[{theme.text_dim}]—[/{theme.text_dim}]",
            env.platform_uri or f"[{theme.text_dim}]—[/{theme.text_dim}]",
            env.credential_backend,
            active_badge,
        )

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
                env = mgr.load(name)
                org = env.organization_name
                suffix = " (active)" if name == active else ""
                label = f"{name}{suffix}"
                if org:
                    label += f"  ({org})"
            except Exception:
                label = name
            choices.append(questionary.Choice(title=label, value=name))

        target = questionary.select(
            "Switch to environment:",
            choices=choices,
            default=active if active in env_names else env_names[0],
            style=QSTYLE,
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
                style=QSTYLE,
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
                f"Create one with: session create <n>[/{theme.text_dim}]"
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


@registry.register("env", "create", description="Create a new environment")
def handle_env_create(args: Namespace) -> int:
    """Create a new environment via the interactive wizard."""
    env_name = getattr(args, "env_name", None)
    from_env = getattr(args, "from_env", None)

    try:
        result = create_environment_wizard(env_name=env_name, from_env=from_env)
    except SystemExit:
        return 1

    return 0 if result else 1


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
            style=QSTYLE,
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
_EDITABLE_FIELDS = [
    ("organization_name",    "Organization Name",      "text"),
    ("description",          "Description",            "text"),
    ("platform_uri",         "Platform URI",           "text"),
    ("platform_client_id",   "Platform Client ID",     "text"),
    ("credential_backend",   "Credential Backend",     "choice"),
    ("legacy_profile",       "Legacy Profile (2023.x)","text"),
    ("gateway4_uri",         "Gateway4 URI",           "text"),
    ("gateway4_username",    "Gateway4 Username",      "text"),
]

_BACKEND_CHOICES = ["keyring", "vault"]


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
            style=QSTYLE,
        ).ask()
        if configure_gw4:
            gw4_uri = questionary.text(
                "Gateway4 API URI (e.g., http://gateway-host:8083)",
                style=QSTYLE,
            ).ask()
            if gw4_uri:
                env.gateway4_uri = gw4_uri
                changed = True

            gw4_user = questionary.text(
                "Gateway4 Username",
                default="admin@itential",
                style=QSTYLE,
            ).ask()
            if gw4_user:
                env.gateway4_username = gw4_user
                changed = True

            if env.credential_backend == "keyring":
                gw4_pass = questionary.password(
                    "Gateway4 Password (hidden)",
                    style=QSTYLE,
                ).ask()
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
                            f"  [{theme.error}]✘ Failed to store password: {e}[/{theme.error}]"
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
            display = str(current) if current else f"[not set]"
            # Truncate long values for the menu
            if len(display) > 50:
                display = display[:47] + "..."
            field_choices.append(
                questionary.Choice(
                    title=f"{label:<26} {display}",
                    value=field_name,
                )
            )

        field_choices.append(
            questionary.Choice(
                title="Deployment Topology       (opens topology wizard)",
                value="_deployment",
            )
        )
        field_choices.append(questionary.Choice(title="Done", value="_done"))

        selected = questionary.select(
            "Select a field to edit:",
            choices=field_choices,
            style=QSTYLE,
        ).ask()

        if selected is None or selected == "_done":
            break

        # Deployment topology — delegate to the existing wizard
        if selected == "_deployment":
            from platform_atlas.core.init_setup import ask_deployment, _display_topology_review
            from platform_atlas.core.topology import DeploymentTopology

            new_deployment = ask_deployment()
            env.deployment = new_deployment
            changed = True

            topology = DeploymentTopology.from_dict(new_deployment)
            scope = new_deployment.get("capture_scope", "primary_only")
            _display_topology_review(topology, capture_scope=scope)
            console.print(f"  [{theme.success}]✓ Deployment topology updated[/{theme.success}]\n")
            continue

        # Find the field descriptor
        field_entry = next(
            (f for f in _EDITABLE_FIELDS if f[0] == selected), None
        )
        if field_entry is None:
            continue

        field_name, label, field_type = field_entry
        current = getattr(env, field_name, None)

        if field_type == "choice" and field_name == "credential_backend":
            new_value = questionary.select(
                f"{label} (current: {current or 'keyring'}):",
                choices=_BACKEND_CHOICES,
                default=current if current in _BACKEND_CHOICES else "keyring",
                style=QSTYLE,
            ).ask()
            if new_value is None:
                continue
        else:
            prompt_text = f"{label}"
            if current:
                prompt_text += f" (current: {current})"

            new_value = questionary.text(
                prompt_text + ":",
                default=str(current) if current else "",
                style=QSTYLE,
            ).ask()
            if new_value is None:
                continue
            new_value = new_value.strip()

        # Apply the change
        old_value = getattr(env, field_name, None)
        if new_value != old_value:
            setattr(env, field_name, new_value if new_value else None)
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
