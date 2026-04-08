# pylint: disable=line-too-long
"""
Utilities for Core Functions
"""

import sys
import os
import tempfile
import stat
import json
from pathlib import Path
from functools import wraps
from typing import Callable, Any
from rich import box
from rich.panel import Panel
from rich.text import Text
from rich.console import Console

from platform_atlas.core._version import __version__
from platform_atlas.core import ui
from platform_atlas.core.exceptions import AtlasError, SecurityError

theme = ui.theme
console = Console()

def handle_errors(
        exit_on_error: bool = True,
        show_traceback: bool = False,
        default_return: Any = None
):
    """Decorator to catch and format exceptions for CLI operations"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except AtlasError as e:
                print(f"\n{e.format_user_message()}\n", file=sys.stderr)
                if show_traceback:
                    import traceback
                    traceback.print_exc()
                if exit_on_error:
                    sys.exit(1)
                return default_return
            except KeyboardInterrupt:
                print("\n\nOperation cancelled by user.", file=sys.stderr)
                if exit_on_error:
                    sys.exit(1)
                return default_return
            except Exception as e:
                print(f"\nUNEXPECTED ERROR: {type(e).__name__}: {e}\n", file=sys.stderr)
                print("This is a bug. Please report it with the following details:\n")

                import traceback
                traceback.print_exc()

                if exit_on_error:
                    sys.exit(1)
                return default_return
        return wrapper
    return decorator

def show_premium_header(title: str = "Platform Atlas"):
    # Create gradient-like title effect
    title_text = Text()
    title_text.append("Platform ", style=f"bold {theme.primary_glow}")
    title_text.append("Atlas", style=f"bold {theme.accent}")

    console.clear()
    console.print(Panel(
        title_text,
        border_style=theme.border_primary,
        box=box.DOUBLE,
        subtitle=f"[{theme.text_dim}]Itential Platform Configuration Auditing[/]",
        subtitle_align="right",
        padding=(1, 4),
        style=f"{theme.text_primary} on {theme.bg_secondary}",
    ))

def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically - no partial writes, no corruption"""

    path = Path(path).resolve()
    parent = path.parent

    # Ensure parent directory exists with secure permissions
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Create temp file with secure permissions
    # O_XCEL ensures we fail if file exists (prevents symlink attack)
    fd = None
    tmp_path = None

    try:
        # Create unpredictable temp file
        fd, tmp_name = tempfile.mkstemp(
            prefix='.tmp_',
            suffix='_' + path.name,
            dir=str(parent)
        )
        tmp_path = Path(tmp_name)

        # Verify it's a regular file, not a symlink
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SecurityError(f"Temp file is not a regular file: {tmp_path}")

        # Set secure permissions (before writing sensitive data)
        if os.name == "posix":
            os.fchmod(fd, 0o600)

        # Write data
        content = json.dumps(data, indent=4, sort_keys=False) + "\n"
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        # Atomic replace
        tmp_path.replace(path)
        tmp_path = None
    finally:
        # Clean up on failure
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

def secure_mkdir(path: Path) -> None:
    """Create a directory with 0o700 permissions (owner-only)"""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
