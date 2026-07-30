# pylint: disable=line-too-long
"""
Utilities for Core Functions
"""

import sys
import os
import re
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
# Errors go to stderr so they stay separate from parseable stdout. Themed to
# match the dispatch-level error surface (bold glyph headline + dim detail).
err_console = Console(stderr=True)


# ── Credential redaction ──────────────────────────────────────────────────
# Scrub the userinfo (``username[:password]``) out of any ``scheme://...@host``
# connection string before a capture artifact is written to disk. We mask ONLY
# the credential segment, leaving scheme/host/port/path/query intact so the
# value is still useful for validation and reporting. The captured URL is never
# needed for connecting — Atlas authenticates from the keyring/Vault store.
URI_CREDENTIAL_MASK = "*****"

# Matches ``://`` followed by ``user``, an optional ``:password``, then ``@``.
# ``[^:@/\s]`` keeps the match from spanning a host boundary or free-text space,
# so a non-credential URL (``mongodb://host:27017/db``) never matches.
_URI_CREDENTIALS_RE = re.compile(r"(://)([^:@/\s]*)(:[^@/\s]*)?@")

# Guards the recursive walk against pathological/cyclic-looking nesting.
_MAX_REDACT_DEPTH = 100


def redact_uri_credentials(value: Any, mask: str = URI_CREDENTIAL_MASK) -> Any:
    """Replace the userinfo in any ``scheme://user:pass@host`` URI with ``mask``.

    Only the credential segment is touched — scheme, host, port, path, and
    query string survive verbatim. Strings without an embedded ``user[:pass]@``
    are returned unchanged, as are non-string inputs. Idempotent.

        ``mongodb://itential:itential@h:27017/db?x=1``
            → ``mongodb://*****:*****@h:27017/db?x=1``
        ``redis://:secret@h:6379``  → ``redis://:*****@h:6379``
        ``mongodb://h:27017/db``    → ``mongodb://h:27017/db``  (unchanged)
    """
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match) -> str:
        scheme_sep = match.group(1)        # "://"
        user = match.group(2)              # username, possibly ""
        password_part = match.group(3)     # ":password" or None
        masked_user = mask if user else ""
        masked_pass = f":{mask}" if password_part is not None else ""
        return f"{scheme_sep}{masked_user}{masked_pass}@"

    return _URI_CREDENTIALS_RE.sub(_replace, value)


def redact_capture_credentials(
        obj: Any,
        mask: str = URI_CREDENTIAL_MASK,
        _depth: int = 0,
) -> Any:
    """Recursively redact URI credentials throughout a captured data structure.

    Walks dicts, lists, and tuples, applying :func:`redact_uri_credentials` to
    every string leaf and returning a redacted copy of any container it visits.
    Scalars (and strings with no credentials) are returned as-is. Pure — never
    mutates the input, so it is safe to call on a live ``full_capture_json``.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return obj
    if isinstance(obj, str):
        return redact_uri_credentials(obj, mask)
    if isinstance(obj, dict):
        return {k: redact_capture_credentials(v, mask, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_capture_credentials(v, mask, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_capture_credentials(v, mask, _depth + 1) for v in obj)
    return obj


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
                err_console.print(f"\n[bold {theme.error}]{ui.glyph('error')}[/bold {theme.error}] {e.message}")
                for key, value in e.details.items():
                    err_console.print(f"  [{theme.text_dim}]{key}: {value}[/{theme.text_dim}]")
                err_console.print("")
                if show_traceback:
                    import traceback
                    traceback.print_exc()
                if exit_on_error:
                    sys.exit(1)
                return default_return
            except KeyboardInterrupt:
                err_console.print(f"\n\n[{theme.warning}]Operation cancelled by user[/{theme.warning}]")
                if exit_on_error:
                    sys.exit(130)
                return default_return
            except Exception as e:
                err_console.print(
                    f"\n[bold {theme.error}]Unexpected Error: {type(e).__name__}[/bold {theme.error}]"
                )
                err_console.print(f"[{theme.text_dim}]{e}[/{theme.text_dim}]\n")
                # Only dump the raw traceback when the user asked for it (--debug);
                # otherwise keep the screen clean and point them at the flag.
                if show_traceback or "--debug" in sys.argv:
                    import traceback
                    traceback.print_exc()
                else:
                    err_console.print(
                        f"[{theme.text_dim}]This looks like a bug. "
                        f"Re-run with --debug for the full traceback.[/{theme.text_dim}]"
                    )
                if exit_on_error:
                    sys.exit(1)
                return default_return
        return wrapper
    return decorator

def show_premium_header(title: str = "Platform Atlas"):
    """Print the Atlas brand panel, optionally tinted by the active env's env_tint."""
    title_text = Text()
    title_text.append("Platform ", style=f"bold {theme.primary_glow}")
    title_text.append("Atlas", style=f"bold {theme.accent}")

    # Attempt to read active env for tint and env/tier subtitle.
    env_tint: str | None = None
    subtitle_str: str | None = None
    try:
        from platform_atlas.core.context import ctx as _ctx
        config = _ctx().config
        if config.active_environment:
            from platform_atlas.core.environment import get_environment_manager
            _mgr = get_environment_manager()
            if _mgr.exists(config.active_environment):
                _env = _mgr.load(config.active_environment)
                env_tint = getattr(_env, "env_tint", None)
            tier = getattr(config, "tier", None) or "standard"
            if config.compatibility_mode:
                _prefix_map = {"high": "[PROD]", "medium": "[STAGE]", "low": "[DEV]"}
                _prefix = _prefix_map.get(env_tint or "", "")
                _env_label = f"{_prefix} {config.active_environment}".strip()
            else:
                _env_label = config.active_environment
            subtitle_str = f"env: {_env_label} · tier: {tier}"
    except Exception:
        pass

    _TINT = {"high": "#C5258F", "medium": "#FDD058", "low": "#99CA3C"}
    border = _TINT.get(env_tint or "", theme.border_primary)

    console.clear()
    console.print(Panel(
        title_text,
        border_style=border,
        box=box.DOUBLE,
        subtitle=(
            f"[{theme.text_dim}]{subtitle_str}[/{theme.text_dim}]"
            if subtitle_str
            else f"[{theme.text_dim}]Itential Platform Configuration Auditing[/]"
        ),
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

        # Write data — `ensure_ascii=False` is mandatory per CLAUDE.md
        # because rule messages contain em-dashes and other non-ASCII
        # punctuation that would otherwise be escaped as \uNNNN sequences.
        content = json.dumps(data, indent=4, sort_keys=False, ensure_ascii=False) + "\n"
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


def split_path(path: str) -> list[str]:
    """Split a dot-notation rule path, respecting double-quoted segments.

    Used by both the validation engine (extract_value, _parent_section_exists)
    and the capture-side filter_capture_by_rules so that rule paths like
    ``adapters."My Adapter".enabled`` parse identically in both stages.
    Splitting on a raw ``.`` would shred quoted segments containing dots
    (e.g. version numbers, hostnames) into nonsense tokens."""
    keys: list[str] = []
    current: list[str] = []
    in_quotes = False

    for char in path:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            keys.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        keys.append("".join(current))
    return keys
