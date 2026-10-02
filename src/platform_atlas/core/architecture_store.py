"""
Per-environment architecture-overview storage.

Architecture answers used to live in a single global ``~/.atlas/architecture.json``;
1.7.x scopes them per environment so prod / staging / dev can carry different
deployments and the user can copy answers from one env to another when they
mostly match.

Layout::

    ~/.atlas/architecture/
        <env>.json         — per-environment answers (one per env)
        _default.json      — fallback bucket when no active env exists
        .legacy-migrated   — sentinel written after the one-time migration

Each file is shaped::

    {
        "schema_version": 2,
        "environment_name": "prod",
        "completed":   {section_name: {field: value, ...}, ...},
        "skipped":     [section_name, ...],
        "status":      "in_progress" | "complete" | "skipped",
        "created_at":  "ISO 8601 UTC",
        "updated_at":  "ISO 8601 UTC"
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_atlas.core.paths import ATLAS_ARCHITECTURE_DIR, ATLAS_ARCHITECTURE_FILE, ATLAS_HOME

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 3
DEFAULT_ENV_KEY = "_default"
LEGACY_MIGRATED_SENTINEL = ATLAS_ARCHITECTURE_DIR / ".legacy-migrated"

# Reuse the same forbidden-character set the env manager uses.
_FORBIDDEN = ("/", "\\", "\x00", "..")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir() -> None:
    ATLAS_ARCHITECTURE_DIR.mkdir(parents=True, exist_ok=True)


_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]+$")


def is_safe_env_name(env: str | None) -> bool:
    """Public: True if ``env`` would normalize cleanly (no exception).

    Lets callers pre-flight a name without try/except — used by the WebUI
    route to render a friendly error page instead of crashing on existing
    envs whose names contain characters like ``!`` or ``#`` from before
    the validator was tightened.
    """
    if not env:
        return True  # falls back to DEFAULT_ENV_KEY
    name = str(env).strip()
    if not name:
        return True
    if any(bad in name for bad in _FORBIDDEN):
        return False
    return bool(_SAFE_ENV_NAME_RE.match(name))


def _normalize_env(env: str | None) -> str:
    """Coerce ``env`` to a safe filename stem; empty/None → ``_default``."""
    if not env:
        return DEFAULT_ENV_KEY
    name = str(env).strip()
    if not name:
        return DEFAULT_ENV_KEY
    for bad in _FORBIDDEN:
        if bad in name:
            raise ValueError(f"Refusing architecture path for unsafe env name: {name!r}")
    # Belt-and-suspenders: only accept characters the env manager would.
    if not _SAFE_ENV_NAME_RE.match(name):
        raise ValueError(f"Architecture env name must be alnum/space/_/-/.: {name!r}")
    return name


def path_for(env: str | None) -> Path:
    """Absolute path to the architecture JSON for ``env``."""
    name = _normalize_env(env)
    return ATLAS_ARCHITECTURE_DIR / f"{name}.json"


def _atomic_write_text(target: Path, text: str) -> None:
    """Write text atomically via same-dir tempfile + replace + fsync."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix="_" + target.name, dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _empty_payload(env: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "environment_name": env,
        "completed": {},
        "skipped": [],
        "status": "in_progress",
        # Best-effort SSH-detected suggestions (see architecture_autofill.py) —
        # kept separate from `completed` so a probe never counts as "this
        # section is done" and never overwrites a user-entered answer. Only
        # `completed` represents confirmed answers.
        "autofill": {},
        "autofill_at": None,
        "created_at": now,
        "updated_at": now,
    }


def load(env: str | None) -> dict[str, Any]:
    """Return the architecture record for ``env`` (empty default if absent)."""
    name = _normalize_env(env)
    target = path_for(name)
    if not target.is_file():
        return _empty_payload(name)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"architecture file is not a JSON object: {target}")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Architecture file unreadable for env=%s: %s", name, exc)
        return _empty_payload(name)
    # Backfill legacy fields so callers can rely on the schema-2 shape.
    data.setdefault("schema_version", 1)
    data.setdefault("environment_name", name)
    data.setdefault("completed", {})
    data.setdefault("skipped", [])
    data.setdefault("status", "in_progress")
    data.setdefault("autofill", {})
    data.setdefault("autofill_at", None)
    return data


def save(env: str | None, data: dict[str, Any]) -> Path:
    """Write the architecture record for ``env`` atomically."""
    name = _normalize_env(env)
    _ensure_dir()
    payload = dict(data)
    payload["schema_version"] = SCHEMA_VERSION
    payload["environment_name"] = name
    payload.setdefault("completed", {})
    payload.setdefault("skipped", [])
    payload.setdefault("status", "in_progress")
    payload.setdefault("autofill", {})
    payload.setdefault("autofill_at", None)
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()
    payload["updated_at"] = _now_iso()
    target = path_for(name)
    _atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return target


def save_autofill(env: str | None, autofill_completed: dict[str, Any]) -> dict[str, Any]:
    """Replace the ``autofill`` bucket with this run's SSH-detected suggestions.

    Never touches ``completed`` — autofill results are advisory only. Each
    run fully replaces the previous autofill snapshot (it reflects current
    reality; there's nothing to preserve from a stale one), stamping
    ``autofill_at`` so consumers can show the user how fresh it is.
    """
    data = load(env)
    data["autofill"] = autofill_completed
    data["autofill_at"] = _now_iso()
    save(env, data)
    return data


def _migrate_legacy_mongo_replica_count(mongodb: dict[str, Any]) -> None:
    """MongoDB used to ask "number of replica set members" (additional nodes
    beyond the primary, 0 = standalone) — a field that a `min="1"` form input
    made impossible to actually answer as standalone, and that two separate
    autofill code paths also mis-populated with a total node count instead of
    the additional-beyond-primary count it was documented to mean. Replaced
    with `deployment_type` + `mongo_node_count` (total servers), mirroring
    Redis's fields. An environment answered before this change only has
    `replica_count` on disk — mutating it here (in-memory only, on the
    seed handed to the form) keeps its section from suddenly looking
    unanswered, without silently rewriting the saved file underneath it.
    """
    if not isinstance(mongodb, dict) or "mongo_node_count" in mongodb:
        return
    legacy = mongodb.get("replica_count")
    if legacy is None:
        return
    try:
        legacy_count = int(legacy)
    except (TypeError, ValueError):
        return
    mongodb["mongo_node_count"] = str(legacy_count + 1)
    mongodb.setdefault(
        "deployment_type",
        "Replica Set (recommended for HA)" if legacy_count > 0 else "Standalone",
    )


def seeded_answers(env: str | None) -> dict[str, Any]:
    """``completed`` (confirmed answers) merged with ``autofill`` filling only
    the gaps — the shape both the browser form and the terminal collector's
    pre-fill hints read from. A confirmed answer always wins; autofill only
    ever supplies a field/section the user hasn't answered yet.
    """
    data = load(env)
    merged: dict[str, Any] = json.loads(json.dumps(data.get("autofill") or {}))
    for section, fields in (data.get("completed") or {}).items():
        if isinstance(fields, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **fields}
        else:
            merged[section] = fields
    if isinstance(merged.get("mongodb"), dict):
        _migrate_legacy_mongo_replica_count(merged["mongodb"])
    return merged


def delete(env: str | None) -> bool:
    """Remove the architecture record for ``env``. Returns True if anything was removed."""
    name = _normalize_env(env)
    target = path_for(name)
    if not target.is_file():
        return False
    try:
        target.unlink()
        return True
    except OSError as exc:
        logger.warning("Could not remove architecture file for env=%s: %s", name, exc)
        return False


def list_envs_with_data() -> list[str]:
    """Names of environments that have any architecture answers on disk."""
    if not ATLAS_ARCHITECTURE_DIR.is_dir():
        return []
    out: list[str] = []
    for entry in ATLAS_ARCHITECTURE_DIR.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        if entry.name.startswith("."):
            continue
        out.append(entry.stem)
    return sorted(out)


def has_data(env: str | None) -> bool:
    """True if ``env`` has any completed sections on disk."""
    data = load(env)
    return bool(data.get("completed"))


def copy(src_env: str, dst_env: str) -> dict[str, Any]:
    """Copy the source environment's answers onto the destination.

    The destination keeps its own ``environment_name`` and timestamps; only the
    user-authored content (``completed``, ``skipped``, ``status``) is replaced.
    Returns the new destination payload.
    """
    if _normalize_env(src_env) == _normalize_env(dst_env):
        raise ValueError("Source and destination environments must differ")
    source = load(src_env)
    if not source.get("completed"):
        raise ValueError(f"Source environment '{src_env}' has no completed sections to copy")
    destination = load(dst_env)
    destination["completed"] = json.loads(json.dumps(source.get("completed", {})))  # deep copy
    destination["skipped"] = list(source.get("skipped", []))
    destination["status"] = source.get("status", "in_progress")
    destination["copied_from"] = _normalize_env(src_env)
    destination["copied_at"] = _now_iso()
    save(dst_env, destination)
    return destination


def migrate_legacy() -> Path | None:
    """Migrate the legacy global ``architecture.json`` into a per-env file.

    Runs at most once per install. Behavior:
        * If the legacy file is missing or the migration sentinel exists, return None.
        * Otherwise, copy the legacy content to ``<active_env>.json`` (or
          ``_default.json`` when no active env is set), then create the
          sentinel so subsequent runs are no-ops. The legacy file is left
          on disk as a tombstone with a ``migrated_to`` key so users can
          confirm what happened.

    Returns the destination path on success, or None when nothing was migrated.
    """
    if LEGACY_MIGRATED_SENTINEL.exists():
        return None
    legacy = ATLAS_ARCHITECTURE_FILE
    if not legacy.is_file():
        # Still create the sentinel so we don't keep retrying.
        _ensure_dir()
        try:
            LEGACY_MIGRATED_SENTINEL.write_text(_now_iso(), encoding="utf-8")
        except OSError:
            pass
        return None

    # Resolve the active environment without forcing context init — we may be
    # invoked very early. Best-effort, fall back to the default bucket.
    target_env = DEFAULT_ENV_KEY
    try:
        from platform_atlas.core.config import load_config
        cfg = load_config()
        if getattr(cfg, "active_environment", None):
            target_env = cfg.active_environment
    except Exception:  # noqa: BLE001
        pass

    try:
        legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(legacy_data, dict):
            legacy_data = {"completed": {}, "skipped": [], "status": "in_progress"}
    except (OSError, json.JSONDecodeError):
        logger.warning("Legacy architecture.json unreadable; skipping migration")
        legacy_data = {}

    payload = _empty_payload(_normalize_env(target_env))
    payload["completed"] = legacy_data.get("completed") or {}
    payload["skipped"] = legacy_data.get("skipped") or []
    payload["status"] = legacy_data.get("status") or "in_progress"
    payload["migrated_from_legacy"] = True
    payload["migrated_at"] = _now_iso()
    target = save(target_env, payload)

    # Leave a tombstone on the legacy file so curious users can see what
    # happened, but rename it so it stops being treated as authoritative.
    try:
        tombstone = ATLAS_HOME / "architecture.legacy.json"
        legacy_data["migrated_to"] = str(target)
        legacy_data["migrated_at"] = _now_iso()
        tombstone.write_text(json.dumps(legacy_data, indent=2, ensure_ascii=False), encoding="utf-8")
        legacy.unlink()
    except OSError:
        pass

    try:
        LEGACY_MIGRATED_SENTINEL.write_text(_now_iso(), encoding="utf-8")
    except OSError:
        pass

    logger.info("Migrated legacy architecture.json → %s", target)
    return target
