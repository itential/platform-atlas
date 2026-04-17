"""
Platform Atlas // HTML Architecture Collector Launcher

Opens the architecture HTML form in the user's browser, waits for the user
to export the JSON, then imports it.  Falls back to CLI if the file is not
found or the user opts out.
"""
from __future__ import annotations

import hashlib
import json
import webbrowser
import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from platform_atlas.core import ui
from platform_atlas.core.paths import ATLAS_HOME

logger = logging.getLogger(__name__)
console = Console()
theme = ui.theme

FORM_FILENAME = "architecture-form.html"
EXPORT_FILENAME = "atlas-architecture.json"


# ── File helpers ──────────────────────────────────────────────────────────────

def _get_form_path() -> Path:
    """Return the path to the HTML form in ~/.atlas/, syncing from the package if stale.

    Mirrors the size+hash logic in init_env._sync_directory: always loads the
    bundled bytes and overwrites the local copy if content has changed.
    """
    dest = ATLAS_HOME / FORM_FILENAME

    # Load bundled bytes — primary via importlib.resources (installed wheel),
    # fallback via filesystem path (editable / dev install).
    html_bytes: bytes | None = None
    try:
        from importlib.resources import files as pkg_files
        html_bytes = pkg_files("platform_atlas.guides").joinpath(FORM_FILENAME).read_bytes()
    except Exception:
        pass

    if html_bytes is None:
        fallback = Path(__file__).parent.parent / "guides" / FORM_FILENAME
        if fallback.exists():
            html_bytes = fallback.read_bytes()

    if html_bytes is None:
        raise FileNotFoundError(
            f"Could not locate {FORM_FILENAME}. "
            "Re-install platform-atlas or switch to CLI mode: "
            "platform-atlas config set manual_input_mode cli"
        )

    ATLAS_HOME.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        # Quick size check first, then full hash — overwrite only if stale.
        if dest.stat().st_size != len(html_bytes) or (
            hashlib.sha256(dest.read_bytes()).hexdigest()
            != hashlib.sha256(html_bytes).hexdigest()
        ):
            dest.write_bytes(html_bytes)
            logger.debug("Updated %s in ~/.atlas (package version is newer)", FORM_FILENAME)
    else:
        dest.write_bytes(html_bytes)
        logger.debug("Extracted %s to ~/.atlas", FORM_FILENAME)

    return dest


def _candidate_paths() -> list[Path]:
    """Ordered list of paths where the browser might have saved the JSON."""
    return [
        ATLAS_HOME / EXPORT_FILENAME,
        Path.home() / "Downloads" / EXPORT_FILENAME,
        Path.cwd() / EXPORT_FILENAME,
    ]


def _find_export() -> Path | None:
    """Return the first candidate path that exists, or None."""
    for path in _candidate_paths():
        if path.is_file():
            return path
    return None


def _load_and_validate(path: Path) -> dict[str, Any] | None:
    """Parse and minimally validate an exported architecture JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"  [{theme.error}]Could not read file: {e}[/{theme.error}]")
        return None

    arch = data.get("architecture_validation") if isinstance(data, dict) else None
    if not isinstance(arch, dict) or "completed" not in arch:
        console.print(
            f"  [{theme.error}]File doesn't look like an Atlas architecture export "
            f"(missing 'architecture_validation.completed').[/{theme.error}]"
        )
        return None

    return arch


def _persist(data: dict[str, Any]) -> None:
    """Write imported data to ~/.atlas/architecture.json (the canonical location)."""
    from platform_atlas.capture.collectors.manual import ATLAS_ARCHITECTURE_FILE
    try:
        ATLAS_ARCHITECTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ATLAS_ARCHITECTURE_FILE.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )
        logger.debug("Architecture data persisted to %s", ATLAS_ARCHITECTURE_FILE)
    except OSError as e:
        logger.warning("Could not persist architecture data: %s", e)


# ── Public entry point ────────────────────────────────────────────────────────

def launch_architecture_form() -> dict[str, Any] | None:
    """Open the HTML architecture form, wait for the export, and import it.

    Return values:
        dict  — raw content of the exported JSON (has 'completed', 'skipped',
                'status' keys).  Caller should use result['completed'].
        {}    — user explicitly skipped; caller should treat as no data.
        None  — user chose CLI fallback; caller should run CLI collector.
    """
    try:
        html_path = _get_form_path()
    except FileNotFoundError as e:
        console.print(f"\n[{theme.warning}]{e}[/{theme.warning}]")
        return None

    console.print(
        f"\n[bold {theme.primary}]Architecture Collector — HTML Form[/]\n"
        f"[{theme.text_dim}]Opening form in your browser …[/{theme.text_dim}]"
    )
    webbrowser.open(html_path.as_uri())

    console.print(
        f"\n[{theme.text_dim}]Fill out the form, then click "
        f"[bold]Export JSON[/bold] when finished.[/{theme.text_dim}]"
    )
    console.print(
        f"[{theme.text_dim}]The file will be named "
        f"[bold]{EXPORT_FILENAME}[/bold].[/{theme.text_dim}]\n"
    )

    while True:
        try:
            response = console.input(
                f"  Press [bold]Enter[/bold] when exported, "
                f"or type [bold]cli[/bold] to use terminal input instead: "
            ).strip().lower()
        except KeyboardInterrupt:
            return None

        if response == "cli":
            return None

        # ── Try auto-detection ────────────────────────────────────────────────
        found = _find_export()
        if found is not None:
            data = _load_and_validate(found)
            if data is not None:
                console.print(f"\n  [{theme.success}]✓ Loaded {found}[/{theme.success}]")
                _persist(data)
                return data

        # ── Auto-detect failed — ask for path ─────────────────────────────────
        console.print(
            f"\n  [{theme.warning}]Could not find [bold]{EXPORT_FILENAME}[/bold] "
            f"automatically.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Checked: "
            + ", ".join(str(p) for p in _candidate_paths())
            + f"[/{theme.text_dim}]"
        )

        choice = console.input(
            f"\n  Provide the [bold]file path[/bold], type [bold]cli[/bold] for "
            f"terminal input, or [bold]skip[/bold] to skip architecture collection: "
        ).strip()

        if not choice or choice.lower() == "skip":
            return {}

        if choice.lower() == "cli":
            return None

        custom = Path(choice).expanduser().resolve()
        if not custom.is_file():
            console.print(f"  [{theme.error}]File not found: {custom}[/{theme.error}]")
            continue

        data = _load_and_validate(custom)
        if data is not None:
            console.print(f"\n  [{theme.success}]✓ Loaded {custom}[/{theme.success}]")
            _persist(data)
            return data
