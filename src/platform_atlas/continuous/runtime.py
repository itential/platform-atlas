"""
Per-environment continuous-audit settings on the env overlay file.

Settings live in ``~/.atlas/environments/<env>.json`` under the key
``continuous_audit``. We read/write this section directly rather than
extending the ``Environment`` dataclass — dataclass extensions ripple
through serialization in unrelated paths, and this section is opaque to
the rest of Atlas.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR
from platform_atlas.continuous.models import ContinuousSettings

logger = logging.getLogger(__name__)


# Env file may carry notification channel secrets and (legacy) credential
# hints — never world-readable, even with a permissive umask.
_ENV_FILE_MODE = 0o600

# Defense-in-depth: validate_env_name is the upstream guard, but a hand-edited
# config or future caller might bypass it. Reject anything that doesn't look
# like a per-env filename component before constructing the path.
_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}$")


def _safe_env_name(name: str) -> str:
    """Return ``name`` if safe to use as a filename component; else raise."""
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("Environment name is empty")
    if not _SAFE_ENV_NAME_RE.match(candidate) or ".." in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError(f"Refusing to use unsafe environment name as path component: {name!r}")
    return candidate


def _env_file(name: str) -> Path:
    """Return the env JSON path, asserting it stays inside ATLAS_ENVIRONMENTS_DIR."""
    safe = _safe_env_name(name)
    path = ATLAS_ENVIRONMENTS_DIR / f"{safe}.json"
    root_resolved = ATLAS_ENVIRONMENTS_DIR.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PermissionError(f"Env file path escapes root {root_resolved}: {resolved}") from exc
    return path


def _read_raw(name: str) -> dict[str, Any]:
    try:
        path = _env_file(name)
    except (ValueError, PermissionError) as exc:
        logger.warning("Refusing to read env file: %s", exc)
        return {}
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read env file %s: %s", path, exc)
        return {}


def _write_raw(name: str, data: dict[str, Any]) -> None:
    """Atomic write with a randomized temp filename and 0o600 perms.

    The previous deterministic ``.json.tmp`` suffix could collide if two
    writers raced; ``mkstemp`` gives each a unique sibling tempfile so the
    final ``os.replace`` is the only contention point.
    """
    path = _env_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix="_" + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(tmp_name, _ENV_FILE_MODE)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_settings(environment: str) -> ContinuousSettings:
    """Read the continuous-audit config for an environment.

    Returns a default (disabled) ``ContinuousSettings`` if the env doesn't
    exist or the section isn't present.
    """
    if not environment:
        return ContinuousSettings()
    raw = _read_raw(environment)
    section = raw.get("continuous_audit") if isinstance(raw, dict) else None
    return ContinuousSettings.from_dict(section)


def write_settings(environment: str, settings: ContinuousSettings) -> None:
    """Persist the continuous-audit config to the environment file."""
    if not environment:
        raise ValueError("Cannot write continuous-audit settings without an environment name")
    raw = _read_raw(environment)
    if not isinstance(raw, dict):
        raise ValueError(f"Environment file for '{environment}' is not a JSON object")
    raw["continuous_audit"] = settings.to_dict()
    _write_raw(environment, raw)
    logger.info("Updated continuous-audit settings for env=%s: enabled=%s interval=%ds",
                environment, settings.enabled, settings.interval_seconds)


def is_enabled(environment: str) -> bool:
    """Cheap accessor for the banner / scheduler hot path."""
    return read_settings(environment).enabled


def can_enable(environment: str) -> tuple[bool, str]:
    """Whether continuous audit may be enabled for ``environment`` right now.

    Gate: a prior ``run-once`` must have completed without a capture/validation
    error. Without this, enabling could loop forever on broken credentials.

    Returns ``(allowed, reason_if_blocked)``. ``reason_if_blocked`` is empty
    when ``allowed`` is True.
    """
    if not environment:
        return False, "No active environment — set one before enabling continuous audit."
    # Lazy import: storage imports back into runtime via the engine.
    from platform_atlas.continuous import storage
    status = storage.read_status(environment)
    if not status:
        return False, "No test run found yet. Click 'Run once now' first to verify Platform OAuth."
    last_status = status.get("last_status")
    if last_status != "ok":
        err = status.get("last_error") or "unknown error"
        return False, f"Last test run failed ({err}). Re-run 'Run once now' successfully before enabling."
    return True, ""
