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

    # Handlers are imported here rather than at module level so the dashboard
    # path (no command_path) never pays the cost of loading all handler modules.
    # This import must stay above registry.resolve() — it triggers the
    # @registry.register decorators that populate the registry.
    import platform_atlas.core.handlers  # pylint: disable=unused-import,import-outside-toplevel

    cmd = registry.resolve(command_path)

    if cmd is None:
        logger.debug("No handler found for: %s", command_path)
        console.print(f"[red]✗[/red] Unknown command: {' '.join(command_path)}")
        return 1

    logger.debug("Resolved handler: %s", cmd.handler.__name__)

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
        # Full technical context (message + details + traceback) goes to the log;
        # the user gets a short, friendly headline plus a remediation hint if one
        # was attached to the error.
        logger.debug("Dispatch error (%s): %s | details=%s", type(e).__name__, e, e.details, exc_info=True)
        console.print(f"\n[bold {theme.error}]✘[/bold {theme.error}] {e.message}")
        hint = e.details.get("suggestion") or e.details.get("fix") or e.details.get("hint")
        if hint:
            console.print(f"  [{theme.text_dim}]{hint}[/{theme.text_dim}]")
        console.print(f"  [{theme.text_dim}]Run with --debug or see the log for details.[/{theme.text_dim}]\n")
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
