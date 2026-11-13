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
        "schema_version": 4,
        "environment_name": "prod",
        "completed":   {section_name: {field: value, ...}, ...},
        "skipped":     [section_name, ...],
        "status":      "in_progress" | "complete" | "skipped",
        "autofill":    {section_name: {field: value, ...}, ...},
        "autofill_at": "ISO 8601 UTC" | null,
        "answers_updated_at": "ISO 8601 UTC",   # last time ``completed`` actually changed
        "source":      "cli" | "html" | "webui" | "copy",   # where the answers were last changed
        "created_at":  "ISO 8601 UTC",
        "updated_at":  "ISO 8601 UTC"
    }

``status`` is advisory only — whether the answers are complete is always
derived from the content by ``architecture_schema.evaluate``.
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


SCHEMA_VERSION = 4
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


def save(env: str | None, data: dict[str, Any], *, source: str | None = None) -> Path:
    """Write the architecture record for ``env`` atomically.

    Two things are carried over from what's already on disk when the caller
    doesn't supply them, so a progress save from a surface that only knows
    about answers (the terminal collector, the WebUI auto-save) never throws
    away the SSH-detected ``autofill`` bucket:

    * ``autofill`` / ``autofill_at``
    * ``source`` — the surface (``cli`` / ``html`` / ``webui`` / ``copy``) that
      last changed the answers; a save that changes nothing leaves it alone.
    * ``answers_updated_at`` — stamped now only when ``completed`` actually
      differs from what was stored, so an auto-detect run (which rewrites the
      file but not the answers) doesn't make the answers look newer than the
      suggestions it just produced.
    """
    name = _normalize_env(env)
    _ensure_dir()
    previous: dict[str, Any] | None = None
    target = path_for(name)
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            previous = None

    payload = dict(data)
    payload["schema_version"] = SCHEMA_VERSION
    payload["environment_name"] = name
    payload.setdefault("completed", {})
    payload.setdefault("skipped", [])
    payload.setdefault("status", "in_progress")
    if "autofill" not in payload:
        payload["autofill"] = (previous or {}).get("autofill") or {}
        payload.setdefault("autofill_at", (previous or {}).get("autofill_at"))
    payload.setdefault("autofill_at", None)

    if source and (previous or {}).get("completed") != payload["completed"]:
        payload["source"] = source
    elif "source" not in payload and (previous or {}).get("source"):
        payload["source"] = previous["source"]

    now = _now_iso()
    if previous is None or previous.get("completed") != payload["completed"]:
        payload["answers_updated_at"] = now
    else:
        payload["answers_updated_at"] = (
            payload.get("answers_updated_at") or previous.get("answers_updated_at")
            or previous.get("updated_at") or now
        )
    if not payload.get("created_at"):
        payload["created_at"] = (previous or {}).get("created_at") or now
    payload["updated_at"] = now
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


def merge_answers(lower: dict[str, Any], upper: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two ``{section: {field: value}}`` dicts; ``upper`` wins.

    Nested dicts (``server_specs``, ``deployment``) merge field by field, so a
    layer that only knows the OS doesn't wipe the CPU/memory a lower layer
    had. Empty values (``None``/``""``/``[]``) never override. A field that
    ``upper`` answers is dropped from the merged ``unknown`` list, so a
    detected value can't be contradicted by a stale "Unknown"; an "Unknown"
    that ``upper`` records explicitly removes the lower layer's value.
    """
    def _empty(v: Any) -> bool:
        return v is None or v == "" or v == [] or v == {}

    def _merge(a: Any, b: Any) -> Any:
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(a)
            for k, v in b.items():
                if k == "unknown":
                    continue  # resolved below, once every answer is merged
                if _empty(v):
                    out.setdefault(k, v)
                    continue
                out[k] = _merge(a[k], v) if k in a else json.loads(json.dumps(v))
            lower_unk = a.get("unknown") if isinstance(a.get("unknown"), list) else []
            upper_unk = b.get("unknown") if isinstance(b.get("unknown"), list) else []
            # An explicit "Unknown" from upper beats a lower layer's value; a
            # lower "Unknown" survives only if nothing answered the field.
            explicit = [k for k in upper_unk if _empty(b.get(k))]
            for k in explicit:
                out.pop(k, None)
            kept = [k for k in lower_unk if k not in explicit and _empty(out.get(k))]
            merged_unk = kept + [k for k in explicit if k not in kept]
            if merged_unk:
                out["unknown"] = merged_unk
            else:
                out.pop("unknown", None)
            return out
        return json.loads(json.dumps(b))

    return _merge(lower or {}, upper or {})


def seeded_answers(env: str | None) -> dict[str, Any]:
    """The answers every surface should pre-fill from, for ``env``.

    Two sources: ``completed`` (what the user confirmed) and ``autofill``
    (what the last SSH auto-detect run observed). The newer one wins per
    field: if auto-detect ran after the answers last changed, its observations
    are the freshest picture of the environment and override; otherwise the
    user's own answers win and auto-detect only fills what they left blank.
    """
    data = load(env)
    completed = data.get("completed") or {}
    autofill = data.get("autofill") or {}
    answers_at = str(data.get("answers_updated_at") or data.get("updated_at") or "")
    autofill_at = str(data.get("autofill_at") or "")
    autofill_is_newer = bool(autofill) and bool(autofill_at) and autofill_at > answers_at
    if autofill_is_newer:
        merged = merge_answers(completed, autofill)
    else:
        merged = merge_answers(autofill, completed)
    if isinstance(merged.get("mongodb"), dict):
        _migrate_legacy_mongo_replica_count(merged["mongodb"])
    # Older forms saved option strings that no longer match a current option
    # exactly (e.g. Redis "Sentinel"); map them so a select can restore them.
    from platform_atlas.core.architecture_schema import SECTION_BY_NAME, normalize_section
    for name, data in list(merged.items()):
        if name in SECTION_BY_NAME and isinstance(data, dict):
            merged[name] = normalize_section(name, data)
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
    save(dst_env, destination, source="copy")
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
