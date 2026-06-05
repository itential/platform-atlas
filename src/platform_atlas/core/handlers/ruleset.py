# pylint: disable=line-too-long
"""
Dispatch Handler ::: Rulesets
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import questionary
from packaging.version import Version
from rich.console import Console
from rich.table import Table
from rich.text import Text

# ATLAS Core
from platform_atlas.core import ui
from platform_atlas.core._version import __version__
from platform_atlas.core.paths import ATLAS_RULESETS_DIR, ATLAS_RULESET_UPDATE_STATE

# ATLAS Management
from platform_atlas.core.registry import registry
from platform_atlas.core.ruleset_manager import get_ruleset_manager

theme = ui.theme
console = Console()
logger = logging.getLogger(__name__)

_RULESET_MANIFEST_URL = (
    "https://raw.githubusercontent.com/itential/platform-atlas/main"
    "/src/platform_atlas/rules/rulesets/manifest.json"
)

# Ruleset ids from the (remote) manifest become filenames under ATLAS_RULESETS_DIR.
# Restrict them to a strict allowlist — no '.', '/', or '\' means no '..' traversal
# and no absolute-path override. Real ids look like "p6-master-ruleset".
_SAFE_RULESET_ID = re.compile(r"[A-Za-z0-9_-]+")

# ── Ruleset update helpers ────────────────────────────────────────────────────

def _find_compatible_version(versions: list[dict], atlas_ver: Version) -> dict | None:
    """Return the first (newest) manifest version entry compatible with atlas_ver."""
    for entry in versions:
        min_ver = entry.get("min_atlas_version", "0.0.0")
        try:
            if atlas_ver >= Version(min_ver):
                return entry
        except Exception:
            continue
    return None


def _current_ruleset_version(ruleset_id: str) -> str:
    """Return the highest version currently installed (user-local or bundled)."""
    from platform_atlas.core.paths import PROJECT_RULESETS

    def _read_version(path: Path) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("ruleset", {}).get("version", "0.0.0") or "0.0.0"
        except Exception:
            return "0.0.0"

    user_path = ATLAS_RULESETS_DIR / f"{ruleset_id}.json"
    bundled_path = PROJECT_RULESETS / f"{ruleset_id}.json"

    user_ver = _read_version(user_path) if user_path.is_file() else "0.0.0"
    bundled_ver = _read_version(bundled_path) if bundled_path.is_file() else "0.0.0"

    try:
        return user_ver if Version(user_ver) >= Version(bundled_ver) else bundled_ver
    except Exception:
        return user_ver or bundled_ver


def _write_update_state(state: dict) -> None:
    """Atomically write the ruleset update state file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp_ruleset_state_", dir=str(ATLAS_RULESET_UPDATE_STATE.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ATLAS_RULESET_UPDATE_STATE)
        logger.debug("Update state file written to %s", ATLAS_RULESET_UPDATE_STATE)
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.debug("Could not write update state file: %s", exc)


def _clear_update_state() -> None:
    """Remove the update state file if it exists."""
    try:
        ATLAS_RULESET_UPDATE_STATE.unlink(missing_ok=True)
        logger.debug("Update state file removed")
    except OSError as exc:
        logger.debug("Could not remove update state file: %s", exc)


@registry.register("ruleset", "update", description="Check for and download ruleset updates")
def handle_ruleset_update(args: Namespace) -> int:
    """Check for ruleset updates and optionally download them."""
    from platform_atlas.core.init_setup import QSTYLE

    update_url = _RULESET_MANIFEST_URL

    # ── Step 1: Fetch manifest ──────────────────────────────────────────────
    console.print(f"\n  [dim]Checking for ruleset updates...[/dim]")
    logger.debug("Fetching manifest from %s", update_url)

    manifest_bytes: bytes
    try:
        req = urllib.request.Request(
            update_url,
            headers={"User-Agent": f"platform-atlas/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            manifest_bytes = resp.read(512 * 1024)  # 512 KB cap — manifests are never large
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("Manifest URL returned 404: %s", update_url)
            console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset manifest couldn't be reached — the repository may have moved or be temporarily unavailable.")
            console.print(f"  [dim]Try again later or contact Itential support if the issue persists.[/dim]")
        else:
            logger.debug("Manifest fetch returned HTTP %d: %s", exc.code, update_url)
            console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset repository returned an unexpected error (HTTP {exc.code}). Try again later.")
        return 1
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc) or exc)
        reason_lower = reason.lower()
        if "ssl" in reason_lower or "certificate" in reason_lower:
            logger.debug("Manifest fetch SSL error: %s", exc)
            console.print(f"[{theme.error}]✘[/{theme.error}] There was a certificate problem connecting to the ruleset repository.")
            console.print(f"  [dim]If you're behind a corporate proxy, check your SSL configuration.[/dim]")
        elif "timed out" in reason_lower:
            logger.debug("Manifest fetch timed out after 5s at %s", update_url)
            console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset repository took too long to respond. Try again when your connection is more stable.")
        else:
            logger.debug("Manifest fetch failed — DNS/connection error: %s", exc)
            console.print(f"[{theme.error}]✘[/{theme.error}] Couldn't reach the ruleset repository — check your internet connection and try again.")
        return 1
    except Exception as exc:
        logger.debug("Manifest fetch unexpected error: %s", exc)
        console.print(f"[{theme.error}]✘[/{theme.error}] An unexpected error occurred while fetching the manifest. Try again later.")
        return 1

    logger.debug("Manifest fetched — %d bytes", len(manifest_bytes))

    # ── Step 2: Parse and validate manifest ────────────────────────────────
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("Manifest JSON parse error: %s", exc)
        console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset manifest couldn't be parsed — it may be temporarily malformed. Try again later.")
        return 1

    schema_version = manifest.get("schema_version", 0)
    if schema_version != 1:
        logger.debug("Manifest schema_version=%d, expected 1", schema_version)
        console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset manifest format has changed (schema version {schema_version} — this Atlas understands version 1).")
        console.print(f"  [dim]Upgrade Atlas to use the latest ruleset updates.[/dim]")
        return 1

    if "rulesets" not in manifest:
        logger.debug("Manifest validation error — missing field: rulesets")
        console.print(f"[{theme.error}]✘[/{theme.error}] The ruleset manifest is missing expected fields and can't be processed.")
        return 1

    logger.debug("Manifest valid — schema_version=%d, %d rulesets listed", schema_version, len(manifest["rulesets"]))

    # ── Step 3: Determine what's available and what needs updating ──────────
    atlas_ver = Version(__version__)
    updates_available: list[dict] = []
    skipped_upgrades: list[dict] = []

    for entry in manifest["rulesets"]:
        ruleset_id = entry.get("id", "")
        if not ruleset_id:
            continue

        # Manifest is remote data — reject any id that could escape the rulesets
        # dir before it's used to build file paths (read here, write at os.replace).
        if not _SAFE_RULESET_ID.fullmatch(ruleset_id):
            logger.warning("Skipping ruleset with unsafe id from manifest: %r", ruleset_id)
            continue

        logger.debug("Processing ruleset: %s", ruleset_id)

        compatible = _find_compatible_version(entry.get("versions", []), atlas_ver)

        if compatible is None:
            latest = (entry.get("versions") or [{}])[0]
            logger.debug("No compatible version for %s — all require newer Atlas", ruleset_id)
            skipped_upgrades.append({
                "id": ruleset_id,
                "description": entry.get("description", ""),
                "available_version": latest.get("ruleset_version", "?"),
                "min_atlas_version": latest.get("min_atlas_version", "?"),
                "reason": "requires_atlas_upgrade",
            })
            console.print(
                f"  [dim]{ruleset_id} {latest.get('ruleset_version', '?')} requires "
                f"Atlas >= {latest.get('min_atlas_version', '?')} "
                f"(you have {__version__}) — your current ruleset is still working great.[/dim]"
            )
            continue

        manifest_ver = compatible.get("ruleset_version", "0.0.0")
        current_ver = _current_ruleset_version(ruleset_id)
        logger.debug("Ruleset %s: current=%s  manifest=%s", ruleset_id, current_ver, manifest_ver)

        try:
            needs_update = Version(manifest_ver) > Version(current_ver)
        except Exception:
            needs_update = False

        if not needs_update:
            logger.debug("%s is up to date at v%s", ruleset_id, current_ver)
            continue

        updates_available.append({
            "id": ruleset_id,
            "description": entry.get("description", ""),
            "current_version": current_ver,
            "available_version": manifest_ver,
            "download_url": compatible.get("download_url", ""),
            "sha256": compatible.get("sha256", ""),
        })

    # ── Step 4: Report results ──────────────────────────────────────────────
    if not updates_available:
        console.print(f"\n  [{theme.success}]✓[/{theme.success}] All rulesets are up to date.")
        _clear_update_state()
        return 0

    console.print()
    for u in updates_available:
        console.print(
            f"  [{theme.info}]↑[/{theme.info}]  [bold]{u['id']}[/bold]  "
            f"[dim]{u['current_version']}[/dim] → [{theme.success}]{u['available_version']}[/{theme.success}]"
        )
        if u.get("description"):
            console.print(f"     [dim]{u['description']}[/dim]")
    console.print()

    # ── Step 5: Prompt ──────────────────────────────────────────────────────
    confirm = questionary.confirm(
        "Download and apply these updates now?",
        default=True,
        style=QSTYLE,
    ).ask()

    if confirm is None:
        raise KeyboardInterrupt

    if not confirm:
        state = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "updates": [
                {
                    "id": u["id"],
                    "description": u.get("description", ""),
                    "current_version": u["current_version"],
                    "available_version": u["available_version"],
                }
                for u in updates_available
            ],
            "skipped_upgrades": skipped_upgrades,
        }
        _write_update_state(state)
        console.print(f"\n  [dim]No problem — you can update any time by running:[/dim]  [bold]platform-atlas ruleset update[/bold]\n")
        return 0

    # ── Step 6: Download, verify, and install ──────────────────────────────
    ATLAS_RULESETS_DIR.mkdir(parents=True, exist_ok=True)

    successes: list[str] = []
    failures: list[str] = []

    for update in updates_available:
        ruleset_id = update["id"]
        download_url = update["download_url"]
        expected_sha256 = update["sha256"]

        if not download_url:
            logger.debug("No download_url for %s — skipping", ruleset_id)
            failures.append(ruleset_id)
            continue

        # download_url is remote data too — pin to HTTPS so a tampered manifest
        # can't redirect the fetch to a file:// or other local-scheme URL.
        if not download_url.lower().startswith("https://"):
            logger.warning("Refusing non-HTTPS download_url for %s: %r", ruleset_id, download_url)
            failures.append(ruleset_id)
            continue

        console.print(f"  [dim]Downloading {ruleset_id}...[/dim]")
        logger.debug("Downloading %s v%s from %s", ruleset_id, update["available_version"], download_url)

        final_path = ATLAS_RULESETS_DIR / f"{ruleset_id}.json"
        tmp_path = ATLAS_RULESETS_DIR / f".tmp_{ruleset_id}_{uuid.uuid4().hex[:8]}.json"

        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": f"platform-atlas/{__version__}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read(10 * 1024 * 1024 + 1)  # cap at 10 MB + 1 byte sentinel

            if len(data) > 10 * 1024 * 1024:
                logger.debug("Ruleset response too large for %s — discarding", ruleset_id)
                console.print(
                    f"  [{theme.error}]✘[/{theme.error}] {ruleset_id} response was unexpectedly large "
                    f"and was discarded."
                )
                failures.append(ruleset_id)
                continue

            # Verify SHA256 before touching the filesystem
            if not expected_sha256:
                logger.warning("No SHA-256 in manifest for %s — refusing installation", ruleset_id)
                console.print(
                    f"  [{theme.error}]✘[/{theme.error}] {ruleset_id} — manifest entry is missing "
                    f"an integrity hash. Skipping to protect your current ruleset."
                )
                failures.append(ruleset_id)
                continue

            actual_sha256 = hashlib.sha256(data).hexdigest()
            logger.debug("SHA256 computed: %s (expected: %s)", actual_sha256, expected_sha256)

            if actual_sha256 != expected_sha256:
                logger.debug("SHA256 mismatch for %s — discarding download", ruleset_id)
                console.print(
                    f"  [{theme.error}]✘[/{theme.error}] {ruleset_id} didn't pass integrity verification "
                    f"and was discarded. Your current ruleset is unchanged. "
                    f"Try again or contact Itential support if this persists."
                )
                failures.append(ruleset_id)
                continue

            # Write to temp then atomic rename — set 0o600 before promoting
            fd_tmp = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                if os.name == "posix":
                    os.fchmod(fd_tmp, 0o600)
                with os.fdopen(fd_tmp, "wb") as f:
                    f.write(data)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, final_path)
            logger.debug("Atomic rename: %s → %s", tmp_path, final_path)

            console.print(f"  [{theme.success}]✓[/{theme.success}] {ruleset_id} updated to v{update['available_version']}")
            successes.append(ruleset_id)

        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            logger.debug("Download failed for %s: %s", ruleset_id, exc)
            try:
                tmp_path.unlink(missing_ok=True)
                logger.debug("Temp file cleanup: %s", tmp_path)
            except OSError:
                pass
            console.print(f"  [{theme.error}]✘[/{theme.error}] {ruleset_id} — download failed. Your existing version is unchanged.")
            failures.append(ruleset_id)

        except Exception as exc:
            logger.debug("Unexpected error downloading %s: %s", ruleset_id, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            console.print(f"  [{theme.error}]✘[/{theme.error}] {ruleset_id} — download failed. Your existing version is unchanged.")
            failures.append(ruleset_id)

    # ── Step 7: Final summary and state cleanup ────────────────────────────
    # Only clear the pending-update notice when every ruleset succeeded.
    # A partial success still has outstanding updates — leave the notice up.
    if successes and not failures:
        _clear_update_state()

    if failures:
        s, f = len(successes), len(failures)
        total = s + f
        if s > 0:
            console.print(f"\n  [{theme.warning}]⚠[/{theme.warning}] {s} of {total} rulesets updated. {f} failed — existing versions unchanged.")
        else:
            console.print(f"\n  [{theme.error}]✘[/{theme.error}] All downloads failed. Your existing rulesets are unchanged.")
        return 1

    console.print(f"\n  [{theme.success}]✓[/{theme.success}] All rulesets are up to date.\n")
    return 0


# ── List / active / load / etc. ──────────────────────────────────────────────

@registry.register("ruleset", "list", description="List available rulesets")
def handle_list_rulesets(args: Namespace) -> int:
    """List available rulesets"""
    manager = get_ruleset_manager()
    rulesets = manager.discover_rulesets()
    active_id = manager.get_active_ruleset_id()
    active_profile = manager.get_active_profile_id()

    if not rulesets:
        console.print(f"[yellow]No rulesets in {manager.RULESETS_DIR}[/yellow]")
        return 1

    table = Table(title="Available Rulesets", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Version", style="yellow")
    table.add_column("Profile", style="magenta")
    table.add_column("Rules", justify="right", style="green")

    for rs in rulesets:
        is_active = rs.id == active_id
        mark = "✓" if rs.id == active_id else ""
        profile = active_profile or "-" if is_active else "-"
        table.add_row(mark, rs.id, rs.name, rs.version, profile, str(rs.rule_count))
    console.print(table)
    return 0

@registry.register("ruleset", "profiles", description="List available profiles")
def handle_list_profiles(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profiles = manager.discover_profiles()
    active_profile = manager.get_active_profile_id()

    if not profiles:
        console.print(f"[yellow]No Profiles in {manager.PROFILES_DIR}[/yellow]")
        return 1

    table = Table(title="Available Profiles", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    table.add_column("Overrides", justify="right", style="yellow")

    for p in profiles:
        mark = "✓" if p.id == active_profile else ""
        table.add_row(mark, p.id, p.name, p.description, str(p.override_count))
    console.print(table)
    return 0

@registry.register("ruleset", "load", description="Load and active a ruleset")
def handle_load_ruleset(args: Namespace) -> int:
    """Load and active a ruleset"""

    # Grab ruleset_id from args
    ruleset_id = args.ruleset_id
    profile_id = getattr(args, "profile", None)

    try:
        get_ruleset_manager().set_active_ruleset(ruleset_id, profile_id)
        #console.print(f"[{theme.success}]✓[/{theme.success}] Activated: [bold]{ruleset_id}[/bold]")
        msg = f"Activated: [bold]{ruleset_id}[/bold]"
        if profile_id:
            msg += f" with profile [bold]{profile_id}[/bold]"
        console.print(f"[{theme.success}]✓[/{theme.success}] {msg}")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1

@registry.register("ruleset", "active", description="Show currently active ruleset")
def handle_active_ruleset(args: Namespace) -> int:
    """Show currently active ruleset"""
    manager = get_ruleset_manager()
    active_id = manager.get_active_ruleset_id()

    if not active_id:
        console.print(f"[{theme.warning}]No active ruleset has been set.[/{theme.warning}]")
        console.print(f"Use: [{theme.primary}]platform-atlas --load-ruleset <id>[/{theme.primary}]")
        return 1

    try:
        m = manager.get_metadata(active_id)
        table = Table(title=f"{m.name} ({m.id})", title_style=f"bold {theme.primary}", show_header=False)
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Version", m.version)
        table.add_row("Rules", str(m.rule_count))
        table.add_row("Target", m.target_product)
        table.add_row("Author", m.author)
        table.add_row("Description", m.description)

        console.print(table)
    except FileNotFoundError:
        console.print(f"[{theme.warning}]⚠[/{theme.warning}] '{active_id}' missing, clearing...")
        manager.clear_active_ruleset()

@registry.register("ruleset", "profile", "set", description="Set the active profile")
def handle_set_profile(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profile_id = args.profile_id
    ruleset_id = manager.get_active_ruleset_id()

    if not ruleset_id:
        console.print(f"[{theme.warning}]No active ruleset. Load one first.[/{theme.warning}]")
        return 1

    try:
        manager.set_active_ruleset(ruleset_id, profile_id)
        console.print(f"[{theme.success}]✓[/{theme.success}] Profile set: [bold]{profile_id}[/bold]")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1

@registry.register("ruleset", "profile", "clear", description="Clear the active profile")
def handle_clear_profile(args: Namespace) -> int:
    manager = get_ruleset_manager()
    ruleset_id = manager.get_active_ruleset_id()

    if not ruleset_id:
        console.print(f"[{theme.warning}]No active ruleset[/{theme.warning}]")
        return 1

    manager.set_active_ruleset(ruleset_id, None)
    console.print(f"[{theme.success}]✓[/{theme.success}] Profile cleared")
    return 0

@registry.register("ruleset", "profile", "list", description="List available profiles")
def handle_profile_list(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profiles = manager.discover_profiles()
    active_profile = manager.get_active_profile_id()

    if not profiles:
        console.print(f"[yellow]No Profiles in {manager.PROFILES_DIR}[/yellow]")
        return 1

    table = Table(title="Available Profiles", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    table.add_column("Overrides", justify="right", style="yellow")

    for p in profiles:
        mark = "✓" if p.id == active_profile else ""
        table.add_row(mark, p.id, p.name, p.description, str(p.override_count))
    console.print(table)
    return 0

@registry.register("ruleset", "profile", "active", description="List available profiles")
def handle_profile_active(args: Namespace) -> int:
    """Show the currently active profile"""
    manager = get_ruleset_manager()
    active_profile = manager.get_active_profile_id()

    if not active_profile:
        console.print(f"[{theme.warning}]No active profile[/{theme.warning}]")
        return 0

    try:
        profile_data = manager._load_profile(active_profile)
        console.print(f"[{theme.success}]✓[/{theme.success}] Active profile: [bold]{active_profile}[/bold]")
        console.print(f"  [dim]Name:[/dim] {profile_data.get('profile_name', '-')}")
        console.print(f"  [dim]Description:[/dim] {profile_data.get('description', '-')}")
        console.print(f"  [dim]Overrides:[/dim] {len(profile_data.get('rules', {}))}")
        return 0
    except FileNotFoundError:
        console.print(f"[{theme.warning}]⚠[/{theme.warning}] Profile '{active_profile}' missing, clearing...")
        ruleset_id = manager.get_active_ruleset_id()
        if ruleset_id:
            manager.set_active_ruleset(ruleset_id, None)
        return 1

@registry.register("ruleset", "info", description="Show detailed ruleset information")
def handle_ruleset_info(args: Namespace) -> int:
    """Show detailed ruleset information"""

    # Grab ruleset_id from args
    ruleset_id = args.ruleset_id

    try:
        m = get_ruleset_manager().get_metadata(ruleset_id)
        is_active = m.id == get_ruleset_manager().get_active_ruleset_id()

        table = Table(title=f"{m.name} ({m.id})", title_style=f"bold {theme.primary}", show_header=False)
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Version", m.version)
        table.add_row("Rules", str(m.rule_count))
        table.add_row("Target", m.target_product)
        table.add_row("Author", m.author)
        table.add_row("Description", m.description)
        table.add_row("File", m.file_path.name)
        table.add_row("Modified", m.last_modified.strftime('%Y-%m-%d %H:%M'))
        table.add_row("Active", "[green]Yes ✓[/green]" if is_active else "No")

        console.print(table)
    except FileNotFoundError:
        console.print(f"[{theme.error}]✘[/{theme.error}] Not found: [bold]{ruleset_id}[/bold]")
        return 1
    return 0

@registry.register("ruleset", "clear", description="Clear the active ruleset")
def handle_clear_ruleset(args: Namespace) -> int:
    """Clear the active ruleset"""
    manager = get_ruleset_manager()
    if active_id := manager.get_active_ruleset_id():
        manager.clear_active_ruleset()
        console.print(f"[green]✓[/green] Cleared: [bold]{active_id}[/bold]")
    else:
        console.print("[yellow]No active ruleset[/yellow]")
    return 0

@registry.register("ruleset", "rules", description="Display all rules in a ruleset")
def handle_ruleset_rules(args: Namespace) -> int:
    """Display all rules in the active of specified ruleset as a Rich table"""
    try:
        rm = get_ruleset_manager()

        # Resolve which ruleset to load
        ruleset_id = getattr(args, "ruleset_id", None) or rm.get_active_ruleset_id()

        if not ruleset_id:
            console.print(
                f"[{theme.warning}]No ruleset specified or active[/{theme.warning}]"
            )
            console.print(
                f"[{theme.text_dim}]Use 'platform-atlas ruleset load <id>' "
                f"or specify one: 'platform-atlas ruleset rules <id>'[/{theme.text_dim}]"
            )
            return 1

        # Get metadata (validates the ruleset exists)
        metadata = rm.get_metadata(ruleset_id)

        # Load the raw JSON to get th rules array
        with open(metadata.file_path, "r", encoding="utf-8") as f:
            ruleset_data = json.load(f)

        rules = ruleset_data.get("rules", [])

        if not rules:
            console.print(f"[{theme.warning}]Ruleset contains no rules[/{theme.warning}]")

        # Apply filters
        category_filter = getattr(args, "category", None)
        severity_filter = getattr(args, "severity", None)

        if category_filter:
            rules = [
                r for r in rules
                if r.get("category", "").lower() == category_filter.lower()
            ]
        if severity_filter:
            rules = [
                r for r in rules
                if r.get("severity", "").lower() == severity_filter.lower()
            ]

        if not rules:
            console.print(
                f"[{theme.warning}]No rules match the applied filters[/{theme.warning}]"
            )
            return 0

        # Severity styling
        severity_styles = {
            "critical": f"bold {theme.error}",
            "warning": theme.warning,
            "info": theme.info
        }

        # Build the table
        table = Table(
            title=f"\n{metadata.name}",
            caption=f"{len(rules)} rule{'s' if len(rules) != 1 else ''}",
            show_lines=False,
            pad_edge=True,
            expand=False,
        )

        table.add_column("Rule ID", style="dim", width=10)
        table.add_column("Enabled", justify="center", width=8)
        table.add_column("Name", style="bold", max_width=60)
        table.add_column("Category", width=12)
        table.add_column("Severity", width=10)
        table.add_column("Type", style="dim", width=12)
        table.add_column("Operator", width=10)
        table.add_column("Target Path", style=f"dim {theme.accent}", max_width=40, overflow="ellipsis")

        for rule in rules:
            severity = rule.get("severity", "info").lower()
            sev_style = severity_styles.get(severity, "")
            enabled = rule.get("enabled", True)
            validation = rule.get("validation", {})

            table.add_row(
                rule.get("rule_number", "-"),
                Text("✓", style=theme.success) if enabled else Text("✖", style=f"bold {theme.error}"),
                rule.get("name", "-"),
                rule.get("category", "-"),
                Text(severity, style=sev_style),
                validation.get("type", "-"),
                validation.get("operator", "-"),
                rule.get("path", "-"),
            )
        console.print(table)
        return 0

    except FileNotFoundError:
        console.print(f"[{theme.error}]✖[/{theme.error}] Ruleset not found: {ruleset_id}")
        return 1
    except Exception as e:
        console.print(f"[{theme.error}]✖[/{theme.error}] {e}")

@registry.register("ruleset", "setup", description="Interactive ruleset and profile selection")
def handle_ruleset_setup(args: Namespace) -> int:
    """Interactively select a ruleset and profile."""
    from platform_atlas.core.init_setup import QSTYLE
    manager = get_ruleset_manager()
    rulesets = manager.discover_rulesets()

    if not rulesets:
        console.print(f"\n  [{theme.warning}]No rulesets found.[/{theme.warning}]")
        console.print(f"  [{theme.text_dim}]Add rulesets to {manager.RULESETS_DIR}[/{theme.text_dim}]\n")
        return 1

    active_ruleset = manager.get_active_ruleset_id()
    active_profile = manager.get_active_profile_id()

    # ── Step 1: Select ruleset ──
    # Use file_path.stem as the value — set_active_ruleset resolves
    # the path as RULESETS_DIR / f"{ruleset_id}.json", so the value
    # must match the filename, not the internal JSON id.
    ruleset_choices = []
    for rs in rulesets:
        file_id = rs.file_path.stem
        suffix = " (active)" if file_id == active_ruleset else ""
        label = f"{rs.id}  —  {rs.name} v{rs.version} ({rs.rule_count} rules){suffix}"
        ruleset_choices.append(questionary.Choice(title=label, value=file_id))

    default_ruleset = active_ruleset if active_ruleset else rulesets[0].file_path.stem
    selected_ruleset = questionary.select(
        "Select ruleset:",
        choices=ruleset_choices,
        default=default_ruleset,
        style=QSTYLE,
    ).ask()

    if selected_ruleset is None:
        console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
        return 1

    # ── Step 2: Select profile ──
    profiles = manager.discover_profiles()

    _NO_PROFILE = "__none__"
    selected_profile = None
    if profiles:
        profile_choices = [
            questionary.Choice(title="None (no profile)", value=_NO_PROFILE),
        ]
        for p in profiles:
            file_id = p.file_path.stem
            suffix = " (active)" if file_id == active_profile else ""
            label = f"{p.id}  —  {p.name} ({p.override_count} overrides){suffix}"
            profile_choices.append(questionary.Choice(title=label, value=file_id))

        default_profile = active_profile if active_profile else _NO_PROFILE
        result = questionary.select(
            "Select profile:",
            choices=profile_choices,
            default=default_profile,
            style=QSTYLE,
        ).ask()

        if result is None:
            console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
            return 1

        selected_profile = None if result == _NO_PROFILE else result

    # ── Apply ──
    try:
        manager.set_active_ruleset(selected_ruleset, selected_profile)
    except FileNotFoundError as e:
        console.print(f"\n  [{theme.error}]✘[/{theme.error}] {e}\n")
        return 1

    msg = f"Active ruleset: [{theme.accent}]{selected_ruleset}[/{theme.accent}]"
    if selected_profile:
        msg += f" with profile [{theme.accent}]{selected_profile}[/{theme.accent}]"
    console.print(f"\n  [{theme.success}]✓[/{theme.success}] {msg}\n")
    return 0

@registry.register("ruleset", "switch", description="Switch the active ruleset and profile")
def handle_ruleset_switch(args: Namespace) -> int:
    """Switch the active ruleset and profile (alias for setup)"""
    return handle_ruleset_setup(args)


@registry.register("ruleset", "profile", "rule-disable", description="Disable a rule in the active profile")
def handle_profile_rule_disable(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profile_id = manager.get_active_profile_id()
    ruleset_id = manager.get_active_ruleset_id()
    if not profile_id:
        console.print(f"[{theme.warning}]No active profile — set one with 'platform-atlas ruleset profile set <id>'[/{theme.warning}]")
        return 1
    rule_number = args.rule_number.strip().upper()
    try:
        manager.disable_rule_in_profile(rule_number, profile_id)
        if ruleset_id:
            manager.set_active_ruleset(ruleset_id, profile_id)
        console.print(f"[{theme.success}]✓[/{theme.success}] Disabled [bold]{rule_number}[/bold] in profile [bold]{profile_id}[/bold]")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1


@registry.register("ruleset", "profile", "rule-enable", description="Re-enable a rule in the active profile")
def handle_profile_rule_enable(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profile_id = manager.get_active_profile_id()
    ruleset_id = manager.get_active_ruleset_id()
    if not profile_id:
        console.print(f"[{theme.warning}]No active profile — set one with 'platform-atlas ruleset profile set <id>'[/{theme.warning}]")
        return 1
    rule_number = args.rule_number.strip().upper()
    try:
        manager.enable_rule_in_profile(rule_number, profile_id)
        if ruleset_id:
            manager.set_active_ruleset(ruleset_id, profile_id)
        console.print(f"[{theme.success}]✓[/{theme.success}] Re-enabled [bold]{rule_number}[/bold] in profile [bold]{profile_id}[/bold]")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1


@registry.register("ruleset", "skip-rule", description="Suppress a rule for the active environment")
def handle_skip_rule(args: Namespace) -> int:
    from datetime import datetime, timezone
    from platform_atlas.core.environment import get_environment_manager
    from platform_atlas.core.init_setup import QSTYLE

    mgr = get_environment_manager()
    env = mgr.get_active()
    if env is None:
        console.print(f"[{theme.warning}]No active environment — set one with 'platform-atlas env switch'[/{theme.warning}]")
        return 1

    rule_number = args.rule_number.strip().upper()

    # Validate rule exists in the active ruleset
    rm = get_ruleset_manager()
    ruleset_id = rm.get_active_ruleset_id()
    if ruleset_id:
        try:
            metadata = rm.get_metadata(ruleset_id)
            with open(metadata.file_path, "r", encoding="utf-8") as _f:
                ruleset_data = json.load(_f)
            valid_rules = {r["rule_number"] for r in ruleset_data.get("rules", []) if "rule_number" in r}
            if rule_number not in valid_rules:
                console.print(
                    f"[{theme.error}]✘[/{theme.error}] [bold]{rule_number}[/bold] does not exist in ruleset "
                    f"[bold]{ruleset_id}[/bold]"
                )
                console.print(f"  [dim]Use 'platform-atlas ruleset rules' to list valid rule numbers.[/dim]")
                return 1
        except (OSError, json.JSONDecodeError, KeyError) as _exc:
            logger.warning("Could not validate rule number against ruleset %s: %s", ruleset_id, _exc)

    current = list(env.skip_rules or [])
    existing_numbers = {r["rule_number"] for r in current if isinstance(r, dict)}

    if rule_number in existing_numbers:
        console.print(
            f"[{theme.warning}]⚠[/{theme.warning}] [bold]{rule_number}[/bold] is already suppressed "
            f"in environment [bold]{env.name}[/bold]"
        )
        return 0

    # Collect reason — flag or interactive prompt
    reason = getattr(args, "reason", None)
    if reason is not None:
        # Flag was supplied — strip and validate immediately; never fall through to prompt
        reason = reason.strip()
        if len(reason) < 10:
            console.print(
                f"[{theme.error}]✘[/{theme.error}] --reason must be at least 10 characters "
                f"(got {len(reason)})."
            )
            return 1
    else:
        reason_raw = questionary.text(
            f"Reason for suppressing {rule_number} (min 10 characters):",
            style=QSTYLE,
        ).ask()
        if reason_raw is None:
            raise KeyboardInterrupt
        reason = reason_raw.strip()

    if len(reason) < 10:
        console.print(
            f"[{theme.error}]✘[/{theme.error}] Reason must be at least 10 characters "
            f"(got {len(reason)})."
        )
        return 1

    entry: dict = {
        "rule_number": rule_number,
        "reason": reason,
        "suppressed_at": datetime.now(timezone.utc).isoformat(),
    }
    env.skip_rules = current + [entry]
    mgr.save(env)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Suppressed [bold]{rule_number}[/bold] "
        f"in environment [bold]{env.name}[/bold]"
    )
    console.print(f"  [dim]Rerun validation or generate a new report to see the updated status.[/dim]")
    return 0


@registry.register("ruleset", "unskip-rule", description="Remove a rule suppression for the active environment")
def handle_unskip_rule(args: Namespace) -> int:
    from platform_atlas.core.environment import get_environment_manager
    mgr = get_environment_manager()
    env = mgr.get_active()
    if env is None:
        console.print(f"[{theme.warning}]No active environment — set one with 'platform-atlas env switch'[/{theme.warning}]")
        return 1
    rule_number = args.rule_number.strip().upper()
    current = list(env.skip_rules or [])
    existing_numbers = {r["rule_number"] for r in current if isinstance(r, dict)}
    if rule_number not in existing_numbers:
        console.print(
            f"[{theme.warning}]⚠[/{theme.warning}] [bold]{rule_number}[/bold] is not suppressed "
            f"in environment [bold]{env.name}[/bold]"
        )
        return 0
    env.skip_rules = [r for r in current if r.get("rule_number") != rule_number] or None
    mgr.save(env)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Restored [bold]{rule_number}[/bold] "
        f"in environment [bold]{env.name}[/bold]"
    )
    console.print(f"  [dim]Rerun validation or generate a new report to see the updated status.[/dim]")
    return 0
