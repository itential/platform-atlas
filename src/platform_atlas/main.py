#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Itential Platform Atlas

This script will capture specific configuration
datapoints, to review against recommended settings

"""

#----############## IMPORTS ##############----#

import json
import sys
import logging
from rich.console import Console

# ATLAS Imports
from platform_atlas.core.init_env import init_env
from platform_atlas.core.cli import parse_args, get_command_path
from platform_atlas.core.dispatch import dispatch
from platform_atlas.core.utils import handle_errors
from platform_atlas.core.paths import ATLAS_CONFIG_FILE

from platform_atlas.core import ui
from platform_atlas.core.context import init_context
from platform_atlas.core._version import __version__

console = Console()

#----############## APP INFO ##############----#

__author__ = "Cody Rester"
__contact__ = "cody.rester@itential.com"
__license__ = "Apache-2.0"

#----############## MAIN ##############----#
@handle_errors(exit_on_error=True, show_traceback=False)
def main() -> int:
    """Platform Atlas Main Entrypoint"""

    # Initialize ATLAS Environment
    init_env()
    args = parse_args()

    # Set up logging before anything else runs
    from platform_atlas.core.log_config import setup_logging, enable_debug
    setup_logging(debug=getattr(args, "debug", False))

    # Set starting log message
    logger = logging.getLogger("platform_atlas")
    logger.info("="*60)
    logger.info("Platform Atlas v%s starting", __version__)

    # ── What's New (one-time upgrade notice) ──────────────────────
    # Show before anything else if --whats-new was passed, or
    # automatically on first run after an upgrade.
    whats_new_forced = getattr(args, "whats_new", False)
    if whats_new_forced:
        from platform_atlas.core.whats_new import maybe_show_whats_new
        maybe_show_whats_new(force=True)
        # If --whats-new was the only intent, exit cleanly
        command_path = get_command_path(args)
        if not command_path:
            return 0

    # Extract the --env override if provided
    env_override = getattr(args, "env_override", None)

    # Don't require valid config to run the setup wizard
    command_path = get_command_path(args)
    if command_path == ("config", "init"):
        from platform_atlas.core.init_setup import start_setup_process
        try:
            start_setup_process()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Setup interrupted. No changes saved.[/bold yellow]")
            return 1
        return 0

    # Env commands that don't require a loaded config/context
    _ENV_NOCONFIG_COMMANDS = {
        ("env", "list"),
        ("env", "create"),
    }
    if command_path in _ENV_NOCONFIG_COMMANDS:
        # These commands work before config is fully set up
        from platform_atlas.core.registry import registry
        import platform_atlas.core.handlers  # pylint: disable=unused-import
        cmd = registry.resolve(command_path)
        if cmd:
            try:
                return cmd.handler(args)
            except KeyboardInterrupt:
                console.print(f"\n[bold yellow]Operation cancelled.[/bold yellow]")
                return 1
        return 1

    # Validate config before loading
    config_missing = (
        not ATLAS_CONFIG_FILE.exists()
        or ATLAS_CONFIG_FILE.stat().st_size == 0
    )
    config_corrupt = False

    if not config_missing:
        try:
            with open(ATLAS_CONFIG_FILE, encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, ValueError):
            config_corrupt = True

    if config_corrupt:
        console.print(
            "\n[bold red]Corrupt config file detected — removing it.[/bold red]"
        )
        ATLAS_CONFIG_FILE.unlink(missing_ok=True)
        config_missing = True

    if config_missing:
        console.print(
            "\n[bold yellow]No configuration found — starting setup wizard.[/bold yellow]\n"
        )
        from platform_atlas.core.init_setup import welcome_screen, start_setup_process
        try:
            welcome_screen()
            start_setup_process()
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Setup interrupted. No changes saved.[/bold yellow]")
            return 1

    # Load configuration (with environment overlay if --env was passed)
    try:
        context = init_context(env_override=env_override)
    except Exception as e:
        console.print(f"[bold red][PREFLIGHT][/bold red] {e}")
        return 1

    # Load UI theme
    ui.theme._resolved = context.theme

    # Enable debugging if set in config
    if context.debug:
        enable_debug()

    # Log active environment
    if context.active_environment:
        logger.info("Active environment: %s", context.active_environment)

    # ── Auto what's-new check (only on dashboard, not subcommands) ──
    if not whats_new_forced and not command_path:
        try:
            from platform_atlas.core.whats_new import maybe_show_whats_new
            maybe_show_whats_new()
        except Exception:
            pass  # Never block startup for a cosmetic feature

    #----############## DISPATCH ##############----#
    return dispatch(args)

if __name__ == "__main__":
    sys.exit(main())
