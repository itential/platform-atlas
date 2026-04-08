# pylint: disable=line-too-long
"""
Platform Atlas Command Dispatcher

Routes CLI commands to their handler functions.
Now fully integrated with SessionManager.
"""

import logging
from argparse import Namespace
from rich.console import Console

# ATLAS Core
from platform_atlas.core.cli import get_command_path
from platform_atlas.core.registry import registry
import platform_atlas.core.handlers # pylint: disable=unused-import # REQUIRED
from platform_atlas.core.exceptions import AtlasError, CredentialError
from platform_atlas.core import ui
from platform_atlas.core._version import __version__

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)

def dispatch(args: Namespace) -> int:
    """Route parsed arguments to the appropriate handler."""
    command_path = get_command_path(args)
    logger.debug("Dispatching command: %s", command_path or "(dashboard)")

    # Handle no command (show dashboard or help)
    if not command_path:
        try:
            from platform_atlas.core.dashboard import show_dashboard
            show_dashboard()
        except Exception:
            console.print(f"\n[bold {theme.primary}]Platform Atlas[/bold {theme.primary}] {__version__}\n")
            console.print(f"[{theme.warning}]⚠ Configuration not loaded[/{theme.warning}]")
            console.print("\n  Run [bold]platform-atlas config init[/bold] to set up")
            console.print("  Run [bold]platform-atlas --help[/bold] for all commands\n")
        return 0

    cmd = registry.resolve(command_path)

    if cmd is None:
        logger.debug("No handler found for: %s", command_path)
        console.print(f"[red]✗[/red] Unknown command: {' '.join(command_path)}")
        return 1

    logger.debug("Resolved handler: %s", cmd.handler.__name__)
    # Gate multi-tenant commands behind config flag
    if command_path and command_path[0] == "customer":
        try:
            from platform_atlas.core.context import ctx
            if not ctx().config.multi_tenant_mode:
                console.print(
                    f"\n[{theme.warning}]'customer' commands require multi_tenant_mode[/{theme.warning}]"
                    f"\n[{theme.text_dim}]Set \"multi_tenant_mode\": true in your config to enable[/{theme.text_dim}]\n"
                )
                return 1
        except Exception: # nosec B110 - config not loaded yet, handler will deal with it
            pass

    # Execute handler with error handling
    try:
        return cmd.handler(args)
    except CredentialError as e:
        logger.debug("Credential backend failed: %s", e, exc_info=True)
        console.print(f"\n[bold {theme.error}]Credential Backend Failed:[/bold {theme.error}] {e}\n")
        if hasattr(e, "details") and e.details.get("fix"):
            console.print(f"[{theme.text_dim}]{e.details['fix']}[/{theme.text_dim}]")
        console.print(f"[{theme.text_dim}]Check Vault connectivity and credentials, then retry.[/{theme.text_dim}]\n")
        return 1
    except AtlasError as e:
        logger.debug("Unexpected dispatch error: %s", e, exc_info=True)
        console.print(f"\n[bold red]✘[/bold red] Something went wrong. Check the log file for details.\n")
        return 1
    except KeyboardInterrupt:
        console.print(f"\n\n[{theme.warning}]Operation cancelled by user[/{theme.warning}]")
        return 130
    except ConnectionError as e:
        logger.debug("Connection failed: %s", e, exc_info=True)
        console.print(f"\n[bold {theme.error}]Connection Failed[/bold {theme.error}]\n")
        console.print(f"[{theme.text_dim}]Check your config URIs and run: platform-atlas preflight[/{theme.text_dim}]")
        return 1
    except PermissionError as e:
        logger.debug("Permission denied: %s", e, exc_info=True)
        console.print(f"\n[bold {theme.error}]Permission Denied[/bold {theme.error}]\n")
        console.print(f"[{theme.text_dim}]Check file permissions: chmod 600 ~/.atlas/config.json[/{theme.text_dim}]")
        return 1
    except Exception as e:
        logger.debug("Unhandled exception in dispatch", exc_info=True)
        console.print(f"\n[bold {theme.error}]Unexpected Error: {type(e).__name__}[/bold {theme.error}]\n")
        console.print(f"[{theme.text_dim}]{e}[/{theme.text_dim}]\n")
        console.print(f"[{theme.text_dim}]If this persists, run with --debug for full traceback[/{theme.text_dim}]")
        return 1
