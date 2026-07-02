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
        table.add_row("Platform OAuth audit", "[green]enabled[/green]")
        if config.gateway4_uri:
            table.add_row(
                "Itential Automation Gateway 4 (IAG4)",
                "[green]enabled[/green]",
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
            table.add_row("Gateway under audit", f"[green]{kind_label}[/green]")
        else:
            table.add_row(
                "Gateway under audit",
                f"[{theme.warning}]not set — recreate the environment[/{theme.warning}]",
            )
        if kind in ("gateway4", "gw4-gw5"):
            api_state = (
                "[green]enabled[/green]" if config.gateway4_uri
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
        table.add_row("Full Platform + IAG4 audit", "[green]enabled[/green]")
        table.add_row("MongoDB / Redis / SSH collection", "[green]enabled[/green]")
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
def handle_tier_upgrade(_: Namespace) -> int:
    """Interactive Standard → Extended upgrade.

    Currently this flips the global tier and points the user at the
    Extended-mode setup commands they'll need to run. The full inline
    re-prompt for SSH/Mongo/Redis credentials is left to ``env edit``
    and ``config init`` so the upgrade flow stays composable rather
    than duplicating wizard logic.
    """
    if ctx().tier == "saas":
        _print_saas_conversion_blocked()
        return 1
    if ctx().tier == "extended":
        console.print(
            f"  [{theme.text_dim}]Already in Extended Mode — nothing to upgrade.[/{theme.text_dim}]"
        )
        return 0

    console.print(Panel(
        f"[bold]Upgrade to Extended Mode[/bold]\n\n"
        f"Extended Mode unlocks deeper auditing:\n"
        f"  ✓ MongoDB replica set & ACL audit\n"
        f"  ✓ Redis runtime config & ACL audit\n"
        f"  ✓ System-layer checks (CPU, memory, disk, ulimits)\n"
        f"  ✓ Configuration file validation\n"
        f"  ✓ Log analysis\n"
        f"  ✓ IAG5 / Kubernetes deployments\n\n"
        f"This requires additional connectivity that often involves your\n"
        f"database, infrastructure, or security teams.\n\n"
        f"[{theme.text_dim}]Itential is happy to help — talk to your CSM or\n"
        f"see the Extended Access Guide.[/{theme.text_dim}]",
        title="Upgrade to Extended",
        border_style=theme.accent,
        box=box.ROUNDED,
        expand=False,
    ))

    confirm = questionary.confirm(
        "Continue with the upgrade?",
        default=False,
        style=get_qstyle(),
    ).ask()
    # questionary returns None on Ctrl-C — propagate the interrupt rather
    # than treating it as "user said no".
    if confirm is None:
        raise KeyboardInterrupt
    if not confirm:
        console.print(f"  [{theme.text_dim}]Upgrade cancelled.[/{theme.text_dim}]")
        return 1

    rc = _persist_tier("extended")
    if rc == 0:
        console.print(
            f"\n  [{theme.text_dim}]Next steps:[/{theme.text_dim}]\n"
            f"  • Run [bold]platform-atlas env create[/bold] to define an Extended environment\n"
            f"    (or [bold]platform-atlas env edit[/bold] to fill in Mongo/Redis/SSH on the active env).\n"
            f"  • Run [bold]platform-atlas preflight[/bold] to validate connectivity.\n"
        )
    return rc


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

    console.print(Panel(
        f"[bold]Downgrade to Standard Mode[/bold]\n\n"
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
