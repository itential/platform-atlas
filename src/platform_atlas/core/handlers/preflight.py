# pylint: disable=line-too-long
"""
Dispatch Handler ::: Preflight
"""

import logging
from argparse import Namespace
from rich.console import Console

# ATLAS Core
from platform_atlas.core.context import ctx
from platform_atlas.core import ui

# ATLAS Management
from platform_atlas.core.registry import registry
from platform_atlas.core.preflight import run_preflight
from platform_atlas.core.exceptions import CredentialError

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)

@registry.register("preflight", description="Run preflight connectivity checks")
def handle_preflight(args: Namespace) -> int:
    """Run preflight checks against scoped targets"""
    config = ctx().config

    # Get targets filtered by capture scope
    try:
        targets = list(config.targets)

        if not targets:
            console.print(f"[{theme.warning}]⚠ No deployment targets configured[/{theme.warning}]")
            console.print(f"[{theme.text_dim}]Run: platform-atlas config deployment[/{theme.text_dim}]")
            return 1

        logger.debug("Preflight: %d targets, scope=%s",
                    len(targets), getattr(config, "capture_scope", "unknown"))
    except Exception as e:
        console.print(
            f"\n    [bold {theme.error}]Credential Backend failed:[/bold {theme.error}] {e}"
        )
        console.print(
            f"\n    [{theme.text_dim}]Check Vault connectivity and credentials, "
            f"then retry.[/{theme.text_dim}]\n"
        )
        raise SystemExit(1)

    # Show what we're checking
    try:
        topology = config.topology
        scope = config.capture_scope
        console.print(
            f"[{theme.text_dim}]Topology: {topology.summary}  "
            f"·  Scope: {scope}  "
            f"·  Targets: {len(targets)}[/{theme.text_dim}]"
        )
    except Exception:
        pass

    report = run_preflight(targets=targets)

    if not report.all_passed:
        return 1
    return 0
