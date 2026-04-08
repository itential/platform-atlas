# pylint: disable=line-too-long
"""
ATLAS // Batch Import for Guided Manual Collection

Scans a user-provided directory for known capture filenames and
automatically matches them to the guided collection blueprint steps.
Files are loaded through the same parsing pipeline as the interactive
guided collector — JSON is tried first, then the step's custom parser.

Designed to be re-runnable: existing data is updated, new files are
added, and anything not found is simply skipped.

Usage:
    platform-atlas session run capture --manual --import-dir ~/atlas-capture/
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple

import questionary
from rich.console import Console
from rich.table import Table
from rich import box

from platform_atlas.core import ui
from platform_atlas.core.context import ctx
from platform_atlas.core.init_setup import QSTYLE
from platform_atlas.capture.guided_collector import (
    CollectionBlueprint,
    FileStep,
    GuidedCollector,
    ManualProgress,
    BLUEPRINTS,
)

logger = logging.getLogger(__name__)
console = Console()
theme = ui.theme


# ─────────────────────────────────────────────
# Deployment Context Detection
# ─────────────────────────────────────────────

# P6-only modules — excluded when legacy_profile is set (IAP 2023.x)
# Matches the automated capture logic in modules_registry.py lines 249-258
_P6_ONLY_MODULES = frozenset({
    "platform_conf",
    "python_version",
    "platform_logs",
    "webserver_logs",
})

_GATEWAY4_MODULES = frozenset({
    "gateway4",
    "gateway4_conf",
    "gateway4_db_sizes",
    "gateway4_sync_config",
})

_GATEWAY5_MODULES = frozenset({
    "gateway5",
})

# HA2-only modules — excluded when deployment mode is standalone
_HA2_MODULES = frozenset({
    "mongo_repl_status",
    "mongo_repl_config",
    "redis_sentinel_conf",
})

def prompt_import_context(
    blueprints: list[CollectionBlueprint],
) -> list[CollectionBlueprint]:
    """Detect deployment type and filter blueprints accordingly.

    IAP 2023.x vs Platform 6 is determined automatically from the
    environment's legacy_profile setting — no need to ask.

    Gateway presence is asked interactively since it can't be
    reliably inferred from config alone.

    Args:
        blueprints: The full blueprint list from get_blueprints_for_ruleset().

    Returns:
        A filtered copy of the blueprint list with irrelevant modules removed.
    """
    config = ctx().config
    exclude: set[str] = set()

    # ── IAP 2023.x detection (automatic) ─────────────
    is_legacy = bool(config.legacy_profile)

    if is_legacy:
        exclude |= _P6_ONLY_MODULES
        console.print(
            f"  [{theme.text_dim}]Detected IAP 2023.x environment "
            f"(profile: {config.legacy_profile})[/{theme.text_dim}]"
        )
        console.print(
            f"  [{theme.text_dim}]Skipping P6-only modules: "
            f"{', '.join(sorted(_P6_ONLY_MODULES))}[/{theme.text_dim}]"
        )
        console.print(
            f"\n  [{theme.primary}]Note:[/{theme.primary}] For 2023.x, also collect the profile endpoint:"
        )
        console.print(
            f"  [{theme.text_dim}]curl -sk https://<host>:3443/profiles/{config.legacy_profile}?token=TOKEN "
            f"> platform_profile.json[/{theme.text_dim}]\n"
        )
    else:
        console.print(
            f"  [{theme.text_dim}]Detected Platform 6 environment[/{theme.text_dim}]\n"
        )

    # ── HA2 detection (automatic from deployment config) ──
    deploy_mode = (config.deployment or {}).get("mode", "standalone")
    is_ha2 = deploy_mode == "ha2"

    if is_ha2:
        console.print(
            f"  [{theme.text_dim}]Detected HA2 deployment — replica set data will be collected[/{theme.text_dim}]"
        )
    else:
        exclude |= _HA2_MODULES
        console.print(
            f"  [{theme.text_dim}]Detected standalone deployment — skipping replica set modules[/{theme.text_dim}]"
        )

    console.print()

    # ── Gateway questions (interactive) ──────────────
    has_gateway4 = questionary.confirm(
        "Does this deployment use Gateway4 (Automation Gateway)?",
        default=False,
        style=QSTYLE,
    ).ask()

    if has_gateway4 is None:
        raise KeyboardInterrupt

    if not has_gateway4:
        exclude |= _GATEWAY4_MODULES

    has_gateway5 = questionary.confirm(
        "Does this deployment use Gateway5 (containerized)?",
        default=False,
        style=QSTYLE,
    ).ask()

    if has_gateway5 is None:
        raise KeyboardInterrupt

    if not has_gateway5:
        exclude |= _GATEWAY5_MODULES

    console.print()

    if exclude:
        excluded_names = [bp.name for bp in blueprints if bp.module in exclude]
        if excluded_names:
            logger.info("Excluding modules based on deployment context: %s", exclude)

    return [bp for bp in blueprints if bp.module not in exclude]


# ─────────────────────────────────────────────
# Filename → Blueprint/Step Mapping
# ─────────────────────────────────────────────

class FileMapping(NamedTuple):
    """Maps a filename stem to a specific blueprint module and step key."""
    module: str
    step_key: str


# Each entry maps a filename stem (no extension) to (module, step_key).
# Multiple stems can map to the same step to handle naming variations.
# The step_key "" means the file IS the entire module value.

_FILENAME_MAP: dict[str, FileMapping] = {
    # ── MongoDB ──
    "mongo_server_status":      FileMapping("mongo", "server_status"),
    "server_status":            FileMapping("mongo", "server_status"),
    "mongo_db_stats":           FileMapping("mongo", "db_stats"),
    "db_stats":                 FileMapping("mongo", "db_stats"),
    "mongo_conf":               FileMapping("mongo_conf", ""),
    "mongod":                   FileMapping("mongo_conf", ""),
    "mongo_repl_status":        FileMapping("mongo_repl_status", ""),
    "rs_status":                FileMapping("mongo_repl_status", ""),
    "mongo_repl_config":        FileMapping("mongo_repl_config", ""),
    "rs_conf":                  FileMapping("mongo_repl_config", ""),

    # ── Redis ──
    "redis_info":               FileMapping("redis", "info"),
    "redis_acl":                FileMapping("redis", "acl_users"),
    "redis_acl_users":          FileMapping("redis", "acl_users"),
    "redis_conf":               FileMapping("redis_conf", ""),
    "sentinel_conf":            FileMapping("redis_sentinel_conf", ""),

    # ── Platform API ──
    "platform_config":          FileMapping("platform", "config"),
    "platform_health_server":   FileMapping("platform", "health_server"),
    "platform_health_status":   FileMapping("platform", "health_status"),
    "platform_adapter_status":  FileMapping("platform", "adapter_status"),
    "platform_application_status": FileMapping("platform", "application_status"),
    "platform_adapter_props":   FileMapping("platform", "adapter_props"),
    "platform_application_props": FileMapping("platform", "application_props"),
    "platform_profile":         FileMapping("platform", "profile"),

    # ── Platform Config ──
    "platform_conf":            FileMapping("platform_conf", ""),
    "platform_properties":      FileMapping("platform_conf", ""),

    # ── Platform Supplemental ──
    "agmanager_size":           FileMapping("agmanager_size", ""),
    "python_version":           FileMapping("python_version", ""),

    # ── Platform Logs ──
    "platform_logs":            FileMapping("platform_logs", ""),
    "webserver_logs":           FileMapping("webserver_logs", ""),

    # ── Gateway4 ──
    "gateway4_packages":        FileMapping("gateway4", ""),
    "gateway4_conf":            FileMapping("gateway4_conf", ""),
    "gw4_db_sizes":             FileMapping("gateway4_db_sizes", ""),
    "gateway4_db_sizes":        FileMapping("gateway4_db_sizes", ""),
    "gateway4_sync_config":     FileMapping("gateway4_sync_config", ""),

    # ── Gateway5 ──
    "gateway5_config":          FileMapping("gateway5", ""),

    # ── System ──
    "system_info":              FileMapping("system", ""),
}


def _find_step(
    blueprints: list[CollectionBlueprint],
    module: str,
    step_key: str,
) -> FileStep | None:
    """Look up the FileStep for a given module and step key."""
    for bp in blueprints:
        if bp.module != module:
            continue
        for step in bp.steps:
            if step.key == step_key:
                return step
    return None


def _find_blueprint(
    blueprints: list[CollectionBlueprint],
    module: str,
) -> CollectionBlueprint | None:
    """Look up a blueprint by module name."""
    for bp in blueprints:
        if bp.module == module:
            return bp
    return None


# ─────────────────────────────────────────────
# Batch Import
# ─────────────────────────────────────────────

def batch_import(
    directory: Path,
    session_dir: Path,
    blueprints: list[CollectionBlueprint],
) -> dict[str, Any]:
    """
    Scan a directory for known capture files and import them.

    Matches files by stem (filename without extension) against the
    known filename map. Each matched file is loaded through the same
    parser pipeline as the interactive guided collector.

    Re-runnable: updates existing module data with newly found files.
    Files not found are silently skipped. The user can add more files
    and run again to incrementally build up the capture.

    Args:
        directory: Path to the directory containing capture files.
        session_dir: Session directory for saving progress.
        blueprints: Active blueprints (already filtered by prompt_import_context).

    Returns:
        The assembled capture data dict (module → data).
    """
    directory = Path(directory).expanduser().resolve()

    if not directory.is_dir():
        console.print(f"[{theme.error}]✗[/{theme.error}] Not a directory: {directory}")
        return {}

    # Load existing progress (supports re-running)
    progress = ManualProgress.load(session_dir)

    # Scan directory for recognized files
    matched: list[tuple[Path, FileMapping, FileStep]] = []
    unmatched: list[Path] = []

    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file():
            continue

        stem = file_path.stem.lower()
        mapping = _FILENAME_MAP.get(stem)

        if mapping is None:
            # Skip dotfiles and other non-data files silently
            if not file_path.name.startswith("."):
                unmatched.append(file_path)
            continue

        # Verify this module is in our active blueprints
        blueprint = _find_blueprint(blueprints, mapping.module)
        if blueprint is None:
            logger.debug(
                "File '%s' maps to module '%s' which is not in active blueprints — skipping",
                file_path.name, mapping.module,
            )
            continue

        step = _find_step(blueprints, mapping.module, mapping.step_key)
        if step is None:
            logger.debug(
                "File '%s' maps to step '%s.%s' which was not found — skipping",
                file_path.name, mapping.module, mapping.step_key,
            )
            continue

        matched.append((file_path, mapping, step))

    if not matched:
        console.print(f"\n[{theme.warning}]No recognized capture files found in:[/{theme.warning}] {directory}")
        console.print(f"[{theme.text_dim}]See the Manual Collection Guide for expected filenames.[/{theme.text_dim}]")
        if unmatched:
            console.print(f"[{theme.text_dim}]Unrecognized files: {', '.join(f.name for f in unmatched[:10])}[/{theme.text_dim}]")
        return progress.capture_data

    # Show what we found
    console.print(f"[{theme.primary}]Scanning:[/{theme.primary}] {directory}")
    console.print(f"[{theme.primary}]Found {len(matched)} recognized file(s)[/{theme.primary}]\n")

    # Load each file and assemble into module data
    imported = 0
    failed = 0
    modules_touched: set[str] = set()

    for file_path, mapping, step in matched:
        data = GuidedCollector._load_file(file_path, step)

        if data is None:
            console.print(f"  [{theme.error}]✗[/{theme.error}] {file_path.name} — failed to parse")
            failed += 1
            continue

        # Assemble into module data (same logic as _prompt_for_module)
        module = mapping.module
        if module not in progress.capture_data:
            progress.capture_data[module] = {}

        if step.key:
            # Keyed step — merge into the module dict
            progress.capture_data[module][step.key] = data
        else:
            # key="" — file IS the entire module value
            progress.capture_data[module] = data

        # Mark module as completed
        progress.completed[module] = f"imported from {directory.name}/"

        # Remove from skipped if it was previously skipped
        if module in progress.skipped:
            progress.skipped.remove(module)

        modules_touched.add(module)
        imported += 1

        console.print(
            f"  [{theme.success}]✓[/{theme.success}] {file_path.name} → {module}"
            + (f".{step.key}" if step.key else "")
        )

    # Save progress
    progress.save(session_dir)

    # Summary
    console.print(f"\n{'─' * 50}")
    console.print(
        f"[{theme.success}]Imported:[/{theme.success}] {imported} file(s) "
        f"across {len(modules_touched)} module(s)"
    )
    if failed:
        console.print(f"[{theme.error}]Failed:[/{theme.error}] {failed} file(s)")

    if unmatched:
        console.print(
            f"[{theme.text_dim}]Skipped {len(unmatched)} unrecognized file(s)[/{theme.text_dim}]"
        )

    # Show overall status
    console.print()
    _show_import_status(blueprints, progress)

    # Tell user about remaining items
    pending = [bp for bp in blueprints if bp.required and not progress.is_done(bp.module)]
    if pending:
        console.print(
            f"\n[{theme.warning}]{len(pending)} required module(s) still pending.[/{theme.warning}]"
        )
        console.print(
            f"[{theme.text_dim}]Add more files and re-run, or use "
            f"--manual without --import-dir for interactive prompts.[/{theme.text_dim}]"
        )
    else:
        console.print(f"\n[{theme.success}]All required modules collected.[/{theme.success}]")

    return progress.capture_data


def _show_import_status(
    blueprints: list[CollectionBlueprint],
    progress: ManualProgress,
) -> None:
    """Display a status table of all modules after import."""
    table = Table(
        title="Import Status",
        box=box.ROUNDED,
    )
    table.add_column("", width=3)
    table.add_column("Module", style="cyan")
    table.add_column("Name")
    table.add_column("Status", justify="center")

    for blueprint in blueprints:
        if blueprint.module in progress.completed:
            status = f"[{theme.success}]✓ Imported[/{theme.success}]"
            marker = f"[{theme.success}]✓[/{theme.success}]"
        elif blueprint.module in progress.skipped:
            status = f"[{theme.text_dim}]Skipped[/{theme.text_dim}]"
            marker = f"[{theme.text_dim}]—[/{theme.text_dim}]"
        else:
            if blueprint.required:
                status = f"[{theme.warning}]Missing[/{theme.warning}]"
                marker = f"[{theme.warning}]?[/{theme.warning}]"
            else:
                status = f"[{theme.text_dim}]Not found[/{theme.text_dim}]"
                marker = f"[{theme.text_dim}]—[/{theme.text_dim}]"

        table.add_row(marker, blueprint.module, blueprint.name, status)

    console.print(table)
