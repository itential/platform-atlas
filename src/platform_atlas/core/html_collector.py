"""
Platform Atlas // HTML Architecture Collector Launcher

Opens the architecture HTML form in the user's browser, waits for the user
to export the JSON, then imports it.  Falls back to CLI if the file is not
found or the user opts out.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from platform_atlas.core import ui
from platform_atlas.core.paths import ATLAS_HOME, ATLAS_HOME_GUIDES

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
    dest = ATLAS_HOME_GUIDES / FORM_FILENAME

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
            "Re-install platform-atlas, or choose the terminal option next time "
            "the architecture form runs."
        )

    ATLAS_HOME_GUIDES.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Remove the pre-guides-folder copy that used to live directly under ~/.atlas.
    legacy = ATLAS_HOME / FORM_FILENAME
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

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

    # Sync the shared CSS + motion assets the form's wizard shell references.
    from platform_atlas.core.guide_assets import sync_guide_assets
    sync_guide_assets(ATLAS_HOME_GUIDES)

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
    """Parse and minimally validate an exported architecture JSON file.

    Pulls ``environment_name`` from either the wrapper or the inner block so
    the import path can detect cross-env exports. The returned dict has the
    shape the architecture_store expects (completed, skipped, status, plus
    optional environment_name).
    """
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

    # Promote environment_name onto the inner block if the form put it on the
    # wrapper. Either location is honored on read.
    if "environment_name" not in arch and isinstance(data, dict):
        embedded = data.get("environment_name")
        if embedded:
            arch["environment_name"] = embedded
    return arch


def _persist(data: dict[str, Any], environment: str = "") -> None:
    """Write imported architecture data under ~/.atlas/architecture/<env>.json."""
    from platform_atlas.core import architecture_store
    try:
        architecture_store.save(environment, data)
        logger.debug(
            "Architecture data persisted for env=%s",
            environment or architecture_store.DEFAULT_ENV_KEY,
        )
    except (OSError, ValueError) as e:
        logger.warning("Could not persist architecture data: %s", e)


def _form_url_for(html_path: Path, environment: str, organization: str = "") -> str:
    """Build the file:// URL with the env (and optional org) as query strings."""
    base = html_path.as_uri()
    # The form reads URLSearchParams to display the env in its header and
    # pre-fill the company/organization field on first load.
    from urllib.parse import quote, urlencode
    params: list[tuple[str, str]] = []
    if environment:
        params.append(("env", environment))
    if organization:
        params.append(("org", organization))
    # SaaS audits get a gateway-scoped form: given the tier and gateway
    # kind, the page hides the Platform/MongoDB/Redis sections (and the
    # other gateway's) from its markup, progress, and export.
    try:
        from platform_atlas.core.context import ctx
        if ctx().is_saas:
            params.append(("tier", "saas"))
            params.append(("gw", (ctx().config.saas_gateway_kind or "").strip().lower()))
    except Exception:
        pass
    if not params:
        return base
    return f"{base}?{urlencode(params, quote_via=quote)}"


def _lookup_organization_name() -> str:
    """Resolve the global organization name to pre-fill into the HTML form.

    The organization name lives only in config.json (set at first-run, changed
    via ``config edit``) — it is the single value for the whole install, so this
    reads config directly. Returns empty string when config is unavailable; the
    form silently no-ops the pre-fill in that case.
    """
    try:
        from platform_atlas.core.paths import ATLAS_CONFIG_FILE
        if ATLAS_CONFIG_FILE.is_file():
            cfg = json.loads(ATLAS_CONFIG_FILE.read_text(encoding="utf-8"))
            return (cfg.get("organization_name") or "").strip()
    except Exception:
        pass
    return ""


# ── Public entry point ────────────────────────────────────────────────────────

def launch_architecture_form(environment: str = "") -> dict[str, Any] | None:
    """Open the HTML architecture form, wait for the export, and import it.

    Args:
        environment: target environment for the answers. The form receives this
            via ``?env=<name>``; the import refuses an export whose
            ``environment_name`` (when set) doesn't match, so users can't
            accidentally overwrite the wrong env.

    Return values:
        dict  — raw content of the exported JSON (has 'completed', 'skipped',
                'status' keys).  Caller should use result['completed'].
        {}    — user explicitly skipped; caller should treat as no data.
        None  — the form file itself couldn't be located; caller should fall
                back to the CLI collector.

    Raises TierViolationError if invoked while the active tier is Standard —
    the architecture form is not part of the app-only Standard tier (it is
    offered in Extended and, gateway-scoped, in SaaS).
    """
    from platform_atlas.core.context import require_infra
    require_infra(
        "architecture form",
        hint="The architecture form is unavailable in Standard Mode.",
    )

    try:
        html_path = _get_form_path()
    except FileNotFoundError as e:
        console.print(f"\n[{theme.warning}]{e}[/{theme.warning}]")
        return None

    organization = _lookup_organization_name()

    form_url = _form_url_for(html_path, environment, organization)
    opened = ui.maybe_open_html(form_url)
    console.print(
        f"\n[bold {theme.primary}]Architecture Collector — HTML Form[/]\n"
        + (
            f"[{theme.text_dim}]Opening form in your browser …[/{theme.text_dim}]"
            if opened else
            f"[{theme.text_dim}]Server environment detected — open this form manually: "
            f"{form_url}[/{theme.text_dim}]"
        )
    )
    if environment:
        console.print(
            f"[{theme.text_dim}]Filling out for environment: [bold]{environment}[/bold][/{theme.text_dim}]"
        )

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
            console.input("  Press [bold]Enter[/bold] when exported: ")
        except KeyboardInterrupt:
            return None

        # ── Try auto-detection ────────────────────────────────────────────────
        found = _find_export()
        if found is not None:
            data = _load_and_validate(found)
            if data is not None and _env_mismatch_check(data, environment):
                console.print(f"\n  [{theme.success}]✓ Loaded {found}[/{theme.success}]")
                _persist(data, environment)
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
            f"\n  Provide the [bold]file path[/bold], or type "
            f"[bold]skip[/bold] to skip architecture collection: "
        ).strip()

        if not choice or choice.lower() == "skip":
            return {}

        custom = Path(choice).expanduser().resolve()
        if not custom.is_file():
            console.print(f"  [{theme.error}]File not found: {custom}[/{theme.error}]")
            continue

        data = _load_and_validate(custom)
        if data is not None and _env_mismatch_check(data, environment):
            console.print(f"\n  [{theme.success}]✓ Loaded {custom}[/{theme.success}]")
            _persist(data, environment)
            return data


def _env_mismatch_check(data: dict[str, Any], expected_env: str) -> bool:
    """Confirm an env-mismatched export before letting it overwrite this env.

    The form embeds ``environment_name`` in the export when launched with
    ``?env=`` set. If the user re-uses an old export from a different env,
    we warn and let them decide rather than silently clobbering.
    Returns True when import should proceed.
    """
    embedded = str(data.get("environment_name") or "").strip()
    if not embedded or not expected_env:
        return True
    if embedded == expected_env:
        return True
    console.print(
        f"\n  [{theme.warning}]The export was generated for env "
        f"[bold]{embedded}[/bold] but you're filling in env "
        f"[bold]{expected_env}[/bold].[/{theme.warning}]"
    )
    answer = console.input(
        f"  Import anyway and overwrite [bold]{expected_env}[/bold]'s answers? "
        f"[bold]y[/bold]/N: "
    ).strip().lower()
    return answer in ("y", "yes")
