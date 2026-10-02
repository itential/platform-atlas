"""
Per-environment validation baseline storage.

A baseline pins one session's validation results as the reference point for
an environment — the thing later runs get checked against. It is a COPY, not
a pointer: made at ``env baseline set`` time and stored independently, so it
stays intact even if the source session is later deleted or overwritten.
Only one baseline exists per environment at a time; setting a new one
replaces the old.

Layout::

    ~/.atlas/baselines/
        <env>.json

Each file is shaped::

    {
        "schema_version": 1,
        "environment_name": "prod",
        "source_session": "prod-audit-2026-01",
        "set_at": "ISO 8601 UTC",
        "session_metadata": {
            "created_at": ..., "target": ..., "organization_name": ...,
            "ruleset_id": ..., "ruleset_version": ..., "ruleset_profile": ...,
            "tier": ...,
        },
        "validation": {
            "metadata": {...},   # ValidationResults.metadata, as captured
            "results": [...],    # ValidationResults.rows, as captured
        }
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platform_atlas.core.paths import ATLAS_BASELINES_DIR
from platform_atlas.core.utils import atomic_write_json
from platform_atlas.validation.results import ValidationResults, load_validation_results

if TYPE_CHECKING:
    from platform_atlas.core.session_manager import Session

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Reuse the same forbidden-character set the environment/architecture stores use.
_FORBIDDEN = ("/", "\\", "\x00", "..")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_stem(env: str) -> str:
    """Validate ``env`` is a safe filename stem. Baselines always require a
    concrete environment — unlike architecture answers, there's no "_default"
    bucket for the no-active-environment case."""
    if not env or not isinstance(env, str):
        raise ValueError("A baseline requires a concrete environment name")
    name = env.strip()
    if not name or any(bad in name for bad in _FORBIDDEN):
        raise ValueError(f"Unsafe environment name for baseline path: {env!r}")
    return name


def path_for(env: str) -> Path:
    """Absolute path to the baseline JSON for ``env``."""
    return ATLAS_BASELINES_DIR / f"{_safe_stem(env)}.json"


def has_baseline(env: str) -> bool:
    """True if ``env`` currently has a pinned baseline."""
    try:
        return path_for(env).is_file()
    except ValueError:
        return False


def load_raw(env: str) -> dict[str, Any] | None:
    """Return the raw baseline record for ``env``, or None if unset/unreadable."""
    try:
        target = path_for(env)
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"baseline file is not a JSON object: {target}")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Baseline file unreadable for env=%s: %s", env, exc)
        return None


def load_results(env: str) -> ValidationResults | None:
    """Return the pinned baseline's validation results, or None if unset."""
    data = load_raw(env)
    if data is None:
        return None
    validation = data.get("validation") or {}
    return ValidationResults(
        rows=validation.get("results", []),
        metadata=validation.get("metadata", {}),
    )


def set_baseline(env: str, session: "Session") -> dict[str, Any]:
    """Copy ``session``'s validation results into the baseline for ``env``.

    Raises ValueError if the session doesn't belong to ``env`` or has no
    validation results yet — callers should check ``Session.validation_file
    .exists()`` themselves first if they want a friendlier error message,
    but this is enforced here too so the store can never end up holding a
    baseline copied from the wrong environment.
    """
    session_env = session.metadata.environment or ""
    if session_env != env:
        raise ValueError(
            f"Session '{session.name}' belongs to environment "
            f"{session_env or '(none)'!r}, not {env!r} — a baseline must come "
            f"from a session in the same environment"
        )
    if not session.validation_file.exists():
        raise ValueError(f"Session '{session.name}' has no validation results to use as a baseline")

    results = load_validation_results(session.validation_file)
    meta = session.metadata

    payload = {
        "schema_version": SCHEMA_VERSION,
        "environment_name": env,
        "source_session": session.name,
        "set_at": _now_iso(),
        "session_metadata": {
            "created_at": meta.created_at.isoformat(),
            "target": meta.target,
            "organization_name": meta.organization_name,
            "ruleset_id": meta.ruleset_id,
            "ruleset_version": meta.ruleset_version,
            "ruleset_profile": meta.ruleset_profile,
            "tier": meta.tier,
        },
        "validation": {
            "metadata": results.metadata,
            "results": results.rows,
        },
    }
    atomic_write_json(path_for(env), payload)
    logger.info("Set baseline for environment '%s' from session '%s'", env, session.name)
    return payload


def clear(env: str) -> bool:
    """Remove the baseline for ``env``. Returns True if anything was removed."""
    try:
        target = path_for(env)
    except ValueError:
        return False
    if not target.is_file():
        return False
    try:
        target.unlink()
        logger.info("Cleared baseline for environment '%s'", env)
        return True
    except OSError as exc:
        logger.warning("Could not remove baseline file for env=%s: %s", env, exc)
        return False


def list_envs_with_baseline() -> list[str]:
    """Names of environments that currently have a pinned baseline."""
    if not ATLAS_BASELINES_DIR.is_dir():
        return []
    return sorted(
        p.stem for p in ATLAS_BASELINES_DIR.glob("*.json")
        if p.is_file()
    )
