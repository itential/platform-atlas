#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Itential Platform Atlas

This script will capture specific configuration
datapoints, to review against recommended settings

"""

#----############## IMPORTS ##############----#

# stdlib only — must precede ALL other imports so the bootstrap can set
# NO_COLOR and patch rich.box before any Console() is instantiated.
import sys
import os
import json
import logging

# -- Windows UTF-8 bootstrap ----------------------------------------------
# Reconfigure stdout/stderr to UTF-8 on Windows before anything prints.
# Windows consoles default to a locale code page (cp1252/cp850) that cannot
# represent Atlas's Unicode output (em-dashes, box-drawing, checkmarks).
# This runs before Rich is imported so every Console() inherits the fix.
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            if hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    del _s
# -------------------------------------------------------------------------

# -- Plain / compatibility-mode bootstrap ---------------------------------
# Reads --plain from sys.argv OR compatibility_mode from config.json, then:
#   1. Sets NO_COLOR=1  — strips all ANSI color codes from every Console()
#   2. Replaces every Rich Unicode box style with ASCII equivalents so
#      panel/table borders render as + - | instead of ╭─╮ etc.
# This MUST run before the Rich import below and before any Atlas module
# is imported (they all create module-level Console() instances).
def _bootstrap_plain_mode() -> None:
    active = "--plain" in sys.argv
    if not active:
        try:
            from pathlib import Path
            _cfg = json.loads((Path.home() / ".atlas" / "config.json").read_text(encoding="utf-8"))
            active = bool(_cfg.get("compatibility_mode", False))
        except Exception:
            pass
    if active:
        os.environ.setdefault("NO_COLOR", "1")
        try:
            import rich.box as _rbox
            _ascii = _rbox.ASCII
            for _attr in dir(_rbox):
                if isinstance(getattr(_rbox, _attr), _rbox.Box):
                    setattr(_rbox, _attr, _ascii)
        except Exception:
            pass
        try:
            import rich.console as _rconsole
            _orig_init = _rconsole.Console.__init__
            def _plain_init(self, *args, **kwargs):
                kwargs.setdefault("emoji", False)
                kwargs.setdefault("highlight", False)
                _orig_init(self, *args, **kwargs)
            _rconsole.Console.__init__ = _plain_init
        except Exception:
            pass

_bootstrap_plain_mode()
del _bootstrap_plain_mode
# -------------------------------------------------------------------------

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
from platform_atlas.core.utils import atomic_write_json

console = Console()
theme = ui.theme

#----############## APP INFO ##############----#

__author__ = "Cody Rester"
__license__ = "GPL-3.0-or-later"

import signal
import threading
import time as _time

_SIGINT_EVENT: threading.Event = threading.Event()
_LAST_SIGINT: float = 0.0


def _setup_sigint_handler() -> None:
    """Register a cooperative SIGINT handler for graceful capture shutdown."""
    from platform_atlas.core.shutdown import request_shutdown

    def _handler(signum, frame):  # noqa: ARG001
        global _LAST_SIGINT
        now = _time.monotonic()
        if _SIGINT_EVENT.is_set() and (now - _LAST_SIGINT) < 2.0:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            import os
            os.kill(os.getpid(), signal.SIGINT)
            return
        _LAST_SIGINT = now
        _SIGINT_EVENT.set()
        request_shutdown()
        import sys as _sys
        print(
            "\n⚠ Interrupt received — cleaning up… (Ctrl-C again to force-exit)",
            file=_sys.stderr,
        )

    signal.signal(signal.SIGINT, _handler)


#----############## MAIN ##############----#
@handle_errors(exit_on_error=True, show_traceback=False)
def main() -> int:
    """Platform Atlas Main Entrypoint"""

    # Initialize ATLAS Environment
    if init_env():
        # Setup wizard just ran — exit cleanly so the user sees the setup
        # summary without the screen being cleared by the dashboard.
        return 0
    # Register cooperative SIGINT handler (after env init, before dispatch)
    _setup_sigint_handler()
    args = parse_args()

    # Persist --plain to config so future invocations activate compatibility
    # mode automatically without needing the flag again.
    if getattr(args, "plain", False):
        try:
            from pathlib import Path
            raw: dict = {}
            cfg_path = Path(ATLAS_CONFIG_FILE)
            if cfg_path.is_file():
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not raw.get("compatibility_mode"):
                raw["compatibility_mode"] = True
                atomic_write_json(cfg_path, raw)
        except Exception as _exc:
            logging.getLogger("platform_atlas").debug(
                "Could not persist compatibility_mode: %s", _exc
            )

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
    # Extract the --tier override if provided
    tier_override = getattr(args, "tier_override", None)

    # Don't require valid config to run the setup wizard
    command_path = get_command_path(args)
    if command_path == ("config", "init"):
        from platform_atlas.core.init_setup import start_setup_process
        try:
            start_setup_process()
        except KeyboardInterrupt:
            console.print(f"\n[bold {theme.warning}]Setup interrupted. No changes saved.[/bold {theme.warning}]")
            return 1
        return 0

    # Env commands that don't require a loaded config/context
    _ENV_NOCONFIG_COMMANDS = {
        ("env", "list"),
        ("env", "create"),
        ("env", "sockets"),
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
                console.print(f"\n[bold {theme.warning}]Operation cancelled.[/bold {theme.warning}]")
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
            f"\n[bold {theme.error}]Corrupt config file detected — removing it.[/bold {theme.error}]"
        )
        ATLAS_CONFIG_FILE.unlink(missing_ok=True)
        config_missing = True

    if config_missing:
        console.print(
            f"\n[bold {theme.warning}]No configuration found — starting setup wizard.[/bold {theme.warning}]\n"
        )
        from platform_atlas.core.init_setup import welcome_screen, start_setup_process
        try:
            welcome_screen()
            start_setup_process()
        except KeyboardInterrupt:
            console.print(f"\n[bold {theme.warning}]Setup interrupted. No changes saved.[/bold {theme.warning}]")
            return 1
        return 0

    # ── No-environment nudge ──────────────────────────────────────────
    # If config exists but no environments are configured, block commands
    # that truly cannot function without one and show a gentle nudge for
    # everything else. This lets users explore Atlas (config, guide, etc.)
    # before they're ready to define a deployment environment.
    try:
        from platform_atlas.core.environment import get_environment_manager
        _env_mgr = get_environment_manager()
        if not _env_mgr.has_any():
            # Commands that require an environment to do anything useful
            _NEEDS_ENV = {
                "session", "preflight", "fleet", "continuous-audit", "support-bundle",
            }
            _cmd_root = command_path[0] if command_path else None
            if _cmd_root in _NEEDS_ENV:
                console.print(
                    f"\n[{theme.text_dim}]No environments configured — this command requires one.[/{theme.text_dim}]"
                )
                console.print(
                    f"[{theme.text_dim}]Run 'platform-atlas env create' to set one up.[/{theme.text_dim}]\n"
                )
                return 1
            # For all other commands show a one-line hint (skip noisy cases)
            if command_path not in {("env", "create"), ("env", "list"), ("config", "init")}:
                console.print(
                    f"\n[{theme.text_dim}]No environments configured — "
                    f"run [bold]platform-atlas env create[/bold] to set one up.[/{theme.text_dim}]"
                )
    except Exception as _exc:
        logger.debug("No-env probe failed: %s", _exc)

    # Load configuration (with environment overlay if --env was passed,
    # and tier override if --tier was passed)
    try:
        context = init_context(
            env_override=env_override,
            tier_override=tier_override,
        )
    except Exception as e:
        console.print(f"[bold {theme.error}][PREFLIGHT][/bold {theme.error}] {e}")
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

    # ── Always-on continuous-audit reminder ────────────────────────
    # Printed on every invocation (including subcommands) when the active
    # environment has continuous audit enabled. Decorative — never blocks.
    # Guard on active_environment so the banner module (and its imports)
    # are never loaded when no environment is configured.
    if context.active_environment:
        try:
            from platform_atlas.continuous.banner import print_banner
            print_banner(context.active_environment)
        except Exception:
            pass

    #----############## DISPATCH ##############----#
    return dispatch(args)

if __name__ == "__main__":
    sys.exit(main())
