"""
ATLAS // Environment Initialization & Bundled File Sync

Handles first-run setup and ongoing synchronization of bundled
rulesets, profiles, and pipelines between the installed Atlas
package and the local working directory (~/.atlas).

Sync behavior:
  - New files in the package are copied to ~/.atlas
  - Modified files (content hash mismatch) are updated in ~/.atlas
  - Files that only exist in ~/.atlas (user-created) are never touched
  - All changes are logged and summarized in the console
"""

import hashlib
import shutil
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from platform_atlas.core.paths import (
    ATLAS_HOME,
    ATLAS_SETTINGS_FILE,
    ATLAS_ENVIRONMENTS_DIR,
    ATLAS_RULESETS_DIR,
    ATLAS_PROFILES_DIR,
    ATLAS_PIPELINES_DIR,
    PROJECT_RULESETS,
    PROJECT_PROFILES,
    PROJECT_PIPELINES,
)

logger = logging.getLogger(__name__)
console = Console()


# ── Sync Engine ──────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    """Compute SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SyncResult:
    """Tracks what changed during a sync pass."""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.updated)

    @property
    def has_changes(self) -> bool:
        return self.total > 0


def _sync_directory(
    source_dir: Path,
    dest_dir: Path,
    *,
    glob_pattern: str = "*.json",
    label: str = "file",
) -> SyncResult:
    """Sync files from a bundled source directory to the local working copy.

    Compares each bundled file against its local counterpart:
      - Missing locally     -> copy (added)
      - Different content   -> overwrite (updated)
      - Identical content   -> skip (no action)

    Files that exist locally but NOT in the source are left alone;
    those are user-created and Atlas should never touch them.
    """
    result = SyncResult()

    if not source_dir.is_dir():
        logger.debug("Source directory does not exist, skipping: %s", source_dir)
        return result

    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    for src_file in sorted(source_dir.glob(glob_pattern)):
        if not src_file.is_file():
            continue

        dest_file = dest_dir / src_file.name

        if not dest_file.exists():
            # New file -- copy it over
            shutil.copy2(src_file, dest_file)
            result.added.append(src_file.name)
            logger.debug("Added new %s: %s", label, src_file.name)
            continue

        # Both exist -- quick size check first, then hash
        if src_file.stat().st_size != dest_file.stat().st_size:
            shutil.copy2(src_file, dest_file)
            result.updated.append(src_file.name)
            logger.debug("Updated %s (size changed): %s", label, src_file.name)
            continue

        if _file_hash(src_file) != _file_hash(dest_file):
            shutil.copy2(src_file, dest_file)
            result.updated.append(src_file.name)
            logger.debug("Updated %s (content changed): %s", label, src_file.name)

    return result


def sync_bundled_files() -> None:
    """Sync all bundled rulesets, profiles, and pipelines to ~/.atlas.

    Called on every Atlas run. Detects new and modified files from the
    installed package and updates the local working copies. User-created
    files in ~/.atlas are never touched.

    Prints a brief summary to the console if anything changed.
    """
    results: list[tuple[str, SyncResult]] = []

    # Rulesets (master rulesets + schema)
    r = _sync_directory(PROJECT_RULESETS, ATLAS_RULESETS_DIR, label="ruleset")
    if r.has_changes:
        results.append(("rulesets", r))

    # Profiles (ruleset overlays)
    r = _sync_directory(PROJECT_PROFILES, ATLAS_PROFILES_DIR, label="profile")
    if r.has_changes:
        results.append(("profiles", r))

    # Pipelines (MongoDB aggregation pipelines)
    r = _sync_directory(PROJECT_PIPELINES, ATLAS_PIPELINES_DIR, label="pipeline")
    if r.has_changes:
        results.append(("pipelines", r))

    if not results:
        logger.debug("All bundled files are up to date")
        return

    # Build and display summary
    total_added = sum(len(r.added) for _, r in results)
    total_updated = sum(len(r.updated) for _, r in results)

    parts = []
    if total_added:
        parts.append(f"{total_added} added")
    if total_updated:
        parts.append(f"{total_updated} updated")

    summary = ", ".join(parts)
    categories = ", ".join(name for name, _ in results)

    console.print(
        f"  [dim]Synced bundled files ({categories}): {summary}[/dim]"
    )
    logger.info(
        "Synced bundled files -- %s (%s)",
        summary,
        categories,
    )

    # Log individual files at debug level
    for category, r in results:
        for name in r.added:
            logger.debug("  [%s] added: %s", category, name)
        for name in r.updated:
            logger.debug("  [%s] updated: %s", category, name)


# ── Environment Initialization ───────────────────────────────

def init_env() -> None:
    """Initialize the local Atlas runtime environment.

    First run:  creates ~/.atlas structure, seeds all bundled files,
                and launches the interactive setup wizard.

    Subsequent: syncs bundled files (new + modified) and ensures
                required directories exist.
    """
    if not ATLAS_HOME.exists():
        # First run -- full setup
        from platform_atlas.core.init_setup import welcome_screen, start_setup_process
        console.print(
            "[bold green]Welcome to Platform Atlas! "
            "Let's start the setup process![/bold green]"
        )
        ATLAS_HOME.mkdir(mode=0o700, exist_ok=True)
        ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)
        ATLAS_SETTINGS_FILE.touch()
        sync_bundled_files()
        welcome_screen()
        start_setup_process()

        # Mark the current version as "seen" so the what's-new screen
        # doesn't fire after a fresh install — it's for upgrades only.
        try:
            from platform_atlas.core.whats_new import _mark_seen
            from platform_atlas.core._version import __version__
            _mark_seen(__version__)
        except Exception:
            pass
    else:
        # Existing install -- sync and ensure directories
        if not ATLAS_ENVIRONMENTS_DIR.exists():
            ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)

        sync_bundled_files()
