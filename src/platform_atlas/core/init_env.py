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
import json
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


_NON_RULESET_FILES = frozenset({"manifest.json", "rules.schema.json"})


def _get_ruleset_version(path: Path) -> str:
    """Extract the semver string from a ruleset JSON. Returns '0.0.0' on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("ruleset", {}).get("version", "0.0.0") or "0.0.0"
    except Exception:
        return "0.0.0"


def _sync_rulesets_version_aware(source_dir: Path, dest_dir: Path) -> SyncResult:
    """Sync bundled rulesets to ~/.atlas, but never overwrite a locally higher version.

    For actual ruleset files (those with a ``ruleset.version`` field):
      - Missing locally       → copy (added)
      - Bundled version newer → overwrite (updated)
      - Local version same or newer → skip (user has a downloaded update; keep it)

    For non-ruleset files (schema, manifest, etc.) the original hash-comparison
    logic applies so they stay current across package upgrades.
    """
    from packaging.version import Version

    result = SyncResult()

    if not source_dir.is_dir():
        logger.debug("Source directory does not exist, skipping: %s", source_dir)
        return result

    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    for src_file in sorted(source_dir.glob("*.json")):
        if not src_file.is_file():
            continue

        dest_file = dest_dir / src_file.name

        if not dest_file.exists():
            shutil.copy2(src_file, dest_file)
            result.added.append(src_file.name)
            logger.debug("Added new ruleset: %s", src_file.name)
            continue

        # Non-ruleset support files: fall back to hash comparison
        if src_file.name in _NON_RULESET_FILES:
            if _file_hash(src_file) != _file_hash(dest_file):
                shutil.copy2(src_file, dest_file)
                result.updated.append(src_file.name)
                logger.debug("Updated support file (content changed): %s", src_file.name)
            continue

        # Ruleset files: only overwrite if bundled is strictly newer
        src_version = _get_ruleset_version(src_file)
        dest_version = _get_ruleset_version(dest_file)

        try:
            bundled_is_newer = Version(src_version) > Version(dest_version)
        except Exception:
            bundled_is_newer = False

        if bundled_is_newer:
            shutil.copy2(src_file, dest_file)
            result.updated.append(src_file.name)
            logger.debug(
                "Updated ruleset (bundled v%s > local v%s): %s",
                src_version, dest_version, src_file.name,
            )
        else:
            logger.debug(
                "Keeping local ruleset (local v%s >= bundled v%s): %s",
                dest_version, src_version, src_file.name,
            )

    return result


def sync_bundled_files() -> None:
    """Sync all bundled rulesets, profiles, and pipelines to ~/.atlas.

    Called on every Atlas run. Detects new and modified files from the
    installed package and updates the local working copies. User-created
    files in ~/.atlas are never touched.

    Prints a brief summary to the console if anything changed.
    """
    results: list[tuple[str, SyncResult]] = []

    # Rulesets — version-aware so downloaded updates are never overwritten
    r = _sync_rulesets_version_aware(PROJECT_RULESETS, ATLAS_RULESETS_DIR)
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


# ── Force Re-Sync (destructive, user-invoked) ────────────────

def _list_json_names(directory: Path) -> list[str]:
    """Return sorted names of the ``*.json`` files directly inside ``directory``.

    Non-recursive on purpose: the rulesets dir contains a ``profiles`` subdir
    that must be treated as its own bucket, not folded into the ruleset list.
    """
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.glob("*.json") if p.is_file())


@dataclass
class ForceSyncPlan:
    """Preview of a force re-sync (full wipe + recopy from the bundled source).

    ``lost_*`` are the local files with no bundled counterpart — user-created
    rulesets/profiles, or newer copies downloaded via ``ruleset update``. A full
    wipe deletes these permanently, so the handler surfaces them before asking
    the user to confirm.
    """
    source_rulesets: list[str] = field(default_factory=list)
    source_profiles: list[str] = field(default_factory=list)
    local_rulesets: list[str] = field(default_factory=list)
    local_profiles: list[str] = field(default_factory=list)

    @property
    def lost_rulesets(self) -> list[str]:
        bundled = set(self.source_rulesets)
        return [n for n in self.local_rulesets if n not in bundled]

    @property
    def lost_profiles(self) -> list[str]:
        bundled = set(self.source_profiles)
        return [n for n in self.local_profiles if n not in bundled]


def plan_force_resync() -> ForceSyncPlan:
    """Compute what a force re-sync would copy and which local-only files it would destroy."""
    return ForceSyncPlan(
        source_rulesets=_list_json_names(PROJECT_RULESETS),
        source_profiles=_list_json_names(PROJECT_PROFILES),
        local_rulesets=_list_json_names(ATLAS_RULESETS_DIR),
        local_profiles=_list_json_names(ATLAS_PROFILES_DIR),
    )


def force_resync_from_source() -> SyncResult:
    """Full-wipe the local rulesets + profiles and copy the bundled set fresh.

    DESTRUCTIVE. Every ``*.json`` in ``ATLAS_RULESETS_DIR`` and
    ``ATLAS_PROFILES_DIR`` is deleted first — including user-created files and
    rulesets downloaded via ``ruleset update`` — then the bundled source is
    copied over. No backup is taken, so callers MUST confirm with the user
    before invoking. Unlike the automatic startup sync, the ruleset version
    gate is ignored here: the bundled files always win.
    """
    result = SyncResult()

    ATLAS_RULESETS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ATLAS_PROFILES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    # 1. Wipe local JSON. The rulesets glob is non-recursive, so the nested
    #    profiles subdir is left alone here and cleared on its own below.
    removed = 0
    stale_files = list(ATLAS_RULESETS_DIR.glob("*.json")) + list(ATLAS_PROFILES_DIR.glob("*.json"))
    for stale in stale_files:
        if stale.is_file():
            stale.unlink()
            removed += 1
    logger.info("Force re-sync: removed %d local ruleset/profile file(s)", removed)

    # 2. Copy the bundled source over the now-empty dirs.
    for src_dir, dest_dir in (
        (PROJECT_RULESETS, ATLAS_RULESETS_DIR),
        (PROJECT_PROFILES, ATLAS_PROFILES_DIR),
    ):
        if not src_dir.is_dir():
            continue
        for src_file in sorted(src_dir.glob("*.json")):
            if src_file.is_file():
                shutil.copy2(src_file, dest_dir / src_file.name)
                result.added.append(src_file.name)

    logger.info("Force re-sync: copied %d file(s) from source", len(result.added))
    return result


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

        # Record the template hash on a fresh install so the what's-new
        # screen doesn't fire — it's for upgrades only.
        try:
            from platform_atlas.core.whats_new import mark_seen_fresh_install
            mark_seen_fresh_install()
        except Exception:
            pass
    else:
        # Existing install -- sync and ensure directories
        if not ATLAS_ENVIRONMENTS_DIR.exists():
            ATLAS_ENVIRONMENTS_DIR.mkdir(mode=0o700, exist_ok=True)

        sync_bundled_files()
