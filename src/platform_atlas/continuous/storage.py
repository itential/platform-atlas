"""
On-disk layout for continuous-audit data.

    ~/.atlas/continuous/<env>/
        runs/<run_id>.json     — full run reports (the external JSON contract)
        runs/latest.json       — symlink (or copy on systems without symlinks)
        events.ndjson          — append-only drift event log
        alerts.json            — current alert state (one per rule+path)
        status.json            — scheduler heartbeat (last_run_*, last_error)

Flat files chosen over SQLite for inspectability and ease of backup. Volume
at hourly cadence is well within what flat files handle (~168 small JSONs/wk).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from platform_atlas.core.paths import ATLAS_HOME

logger = logging.getLogger(__name__)

# ── Platform-specific file locking ───────────────────────────────────────────
# All call sites already catch OSError and fall through, so a no-op stub on
# Windows is safe — the code degrades to unguarded reads/writes, which is
# fine for single-process use.
try:
    import fcntl as _fcntl_mod
    _LOCK_EX: int = _fcntl_mod.LOCK_EX
    _LOCK_UN: int = _fcntl_mod.LOCK_UN
    def _flock(fd: int, op: int) -> None:
        _fcntl_mod.flock(fd, op)
except ImportError:
    _LOCK_EX = _LOCK_UN = 0
    def _flock(fd: int, op: int) -> None:  # Windows no-op
        pass


CONTINUOUS_ROOT = ATLAS_HOME / "continuous"

# events.ndjson grows unbounded at hourly cadence (~40 MB / yr). Rotate when it
# exceeds the byte threshold by truncating to the most-recent N lines.
EVENTS_MAX_BYTES = 10 * 1024 * 1024
EVENTS_RETAIN_LINES = 5000

# Files written here may contain captured Platform values (Mongo URIs, OAuth
# secrets, etc. that surface as drift values). Restrict to user-only so a
# permissive umask doesn't expose them to other accounts on shared hosts.
SECURE_FILE_MODE = 0o600

# Defense-in-depth: even though validate_env_name rejects ``..``, ``/``, and
# ``\``, hand-edited config or future code paths might bypass that validator.
# Anything that doesn't look like a safe per-env directory name is rejected.
_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}$")


def _safe_env_name(environment: str) -> str:
    """Sanitize the env name used as a directory component under CONTINUOUS_ROOT.

    Empty / falsy → "_default" (matches the legacy behavior callers expect).
    Anything else must match the documented env-name pattern (no traversal,
    no separators, no nulls). Raises ValueError on rejection so the bad path
    surfaces at the API boundary rather than silently writing somewhere weird.
    """
    name = (environment or "").strip() or "_default"
    if name == "_default":
        return name
    if not _SAFE_ENV_NAME_RE.match(name) or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Refusing to use unsafe environment name as path component: {environment!r}")
    return name


def _assert_under_root(path: Path, root: Path) -> Path:
    """Resolve ``path`` and assert it lives under ``root``.

    Mirrors the helper in ``os_scheduler.py`` — we deliberately duplicate it
    here rather than introducing a cross-module dependency on what's an
    eight-line filesystem primitive. Raises PermissionError on escape.
    """
    root_resolved = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PermissionError(f"Path escapes root {root_resolved}: {resolved}") from exc
    return resolved


def _atomic_write_text(target: Path, text: str, *, mode: int = SECURE_FILE_MODE) -> None:
    """Write ``text`` to ``target`` atomically via same-dir tempfile + replace.

    Ensures the parent directory exists, fsyncs the contents, then replaces
    the destination. On SIGINT or disk-full mid-write the original file
    survives intact rather than being half-written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix="_" + target.name, dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def env_dir(environment: str) -> Path:
    """Return (and create) the per-environment directory.

    Validates the env name against ``_SAFE_ENV_NAME_RE`` and asserts the
    resulting path stays under ``CONTINUOUS_ROOT``. A hand-edited config that
    sets ``active_environment="../foo"`` raises ValueError here instead of
    silently writing into a sibling directory tree.
    """
    name = _safe_env_name(environment)
    path = CONTINUOUS_ROOT / name
    _assert_under_root(path, CONTINUOUS_ROOT)
    path.mkdir(parents=True, exist_ok=True)
    (path / "runs").mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def env_lock(environment: str) -> Iterator[None]:
    """Process-exclusive flock on ``<env_dir>/.run.lock``.

    Used by the engine to serialize concurrent ``run_once`` invocations
    across the WebUI scheduler, OS timers (systemd / launchd), and manual
    CLI runs. Falls through quietly on platforms without working flock.
    """
    base = env_dir(environment)
    lock_path = base / ".run.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _flock(fd, _LOCK_EX)
        except OSError as exc:
            logger.debug("flock unavailable on %s (%s); proceeding without lock", lock_path, exc)
        yield
    finally:
        try:
            _flock(fd, _LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


@contextlib.contextmanager
def alerts_lock(environment: str) -> Iterator[None]:
    """Read-modify-write lock on alerts.json. Same shape as ``env_lock``."""
    base = env_dir(environment)
    lock_path = base / ".alerts.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _flock(fd, _LOCK_EX)
        except OSError as exc:
            logger.debug("flock unavailable on %s (%s); proceeding without lock", lock_path, exc)
        yield
    finally:
        try:
            _flock(fd, _LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _runs_dir(environment: str) -> Path:
    return env_dir(environment) / "runs"


def run_path(environment: str, run_id: str) -> Path:
    return _runs_dir(environment) / f"{run_id}.json"


def latest_path(environment: str) -> Path:
    return _runs_dir(environment) / "latest.json"


def events_path(environment: str) -> Path:
    return env_dir(environment) / "events.ndjson"


def alerts_path(environment: str) -> Path:
    return env_dir(environment) / "alerts.json"


def status_path(environment: str) -> Path:
    return env_dir(environment) / "status.json"


def now_iso() -> str:
    """ISO 8601 UTC, second precision, Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_id(environment: str) -> str:
    """Sortable, filename-safe run ID with microsecond + nonce.

    The microsecond component plus a 4-hex random suffix guarantees uniqueness
    even if two run-once invocations land in the same wall-clock second from the
    OS scheduler and a manual trigger, or from the WebUI scheduler racing the
    in-process scheduler during a tight enable/disable cycle.
    """
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    micro = f"{now.microsecond:06d}"
    nonce = secrets.token_hex(2)
    suffix = (environment or "default").replace("/", "_").replace(" ", "_")
    return f"{stamp}-{micro}-{nonce}-{suffix}"


# ── Run reports ───────────────────────────────────────────────────────

def write_run(environment: str, run: dict[str, Any]) -> Path:
    """Atomically write the run report and refresh ``latest.json``."""
    run_id = run["run_id"]
    target = run_path(environment, run_id)
    _atomic_write_text(target, json.dumps(run, indent=2, ensure_ascii=False))
    _refresh_latest_pointer(environment, target)
    return target


def _refresh_latest_pointer(environment: str, target: Path) -> None:
    """Point ``latest.json`` at ``target``; symlink first, copy fallback."""
    latest = latest_path(environment)
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        os.symlink(target.name, latest)  # relative target — survives moves
    except (OSError, NotImplementedError):
        try:
            _atomic_write_text(latest, target.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Failed to refresh latest.json for env=%s: %s", environment, exc)


def read_run(environment: str, run_id: str) -> dict[str, Any] | None:
    path = run_path(environment, run_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read run %s: %s", run_id, exc)
        return None


def read_latest(environment: str) -> dict[str, Any] | None:
    """Read the most recent run, preferring ``latest.json`` for speed.

    If ``latest.json`` is a symlink we verify the target resolves under the
    runs directory before following it — defends against a hostile symlink
    plant (the user already has write access at this point, but the principle
    of least surprise still applies).
    """
    latest = latest_path(environment)
    runs_root = _runs_dir(environment)
    if latest.is_symlink():
        try:
            _assert_under_root(latest, runs_root)
        except (PermissionError, OSError) as exc:
            logger.warning("latest.json symlink for env=%s escapes runs/: %s", environment, exc)
            return _fallback_to_newest_run(environment)
    if latest.is_file() or latest.is_symlink():
        try:
            return json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return _fallback_to_newest_run(environment)


def _fallback_to_newest_run(environment: str) -> dict[str, Any] | None:
    runs = list_runs(environment, limit=1)
    if not runs:
        return None
    return read_run(environment, runs[0])


def list_runs(environment: str, *, limit: int | None = None) -> list[str]:
    """Return run IDs, newest first. Pure filename inspection — no JSON parse."""
    runs = _runs_dir(environment)
    if not runs.is_dir():
        return []
    ids = sorted(
        (p.stem for p in runs.glob("*.json") if p.name != "latest.json"),
        reverse=True,
    )
    if limit is not None:
        ids = ids[:limit]
    return ids


def prune_runs(environment: str, retain: int) -> int:
    """Delete oldest run JSONs beyond ``retain``. Returns count removed.

    If ``latest.json`` ends up pointing at a pruned run, repoint it at the new
    newest surviving run so subsequent ``read_latest`` calls don't fall back to
    a directory listing.
    """
    if retain <= 0:
        return 0
    ids = list_runs(environment)
    excess = ids[retain:]
    pruned_set = set(excess)
    removed = 0
    for run_id in excess:
        try:
            run_path(environment, run_id).unlink()
            removed += 1
        except OSError as exc:
            logger.debug("Could not prune %s: %s", run_id, exc)
    if removed:
        logger.info("Pruned %d old continuous-audit runs in env=%s", removed, environment or "_default")
        _maybe_repoint_latest(environment, pruned_set)
    return removed


def _maybe_repoint_latest(environment: str, pruned_ids: set[str]) -> None:
    """If latest.json targets a pruned run, repoint it at the new newest run."""
    latest = latest_path(environment)
    survivors = list_runs(environment, limit=1)
    if not survivors:
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
        except OSError:
            pass
        return

    pointed_run_id: str | None = None
    try:
        if latest.is_symlink():
            target_name = os.readlink(latest)
            pointed_run_id = Path(target_name).stem
        elif latest.is_file():
            pointed_run_id = json.loads(latest.read_text(encoding="utf-8")).get("run_id")
    except (OSError, json.JSONDecodeError, ValueError):
        pointed_run_id = None

    if pointed_run_id is None or pointed_run_id in pruned_ids:
        target = run_path(environment, survivors[0])
        if target.is_file():
            _refresh_latest_pointer(environment, target)


# ── Status heartbeat ──────────────────────────────────────────────────

def write_status(environment: str, status: dict[str, Any]) -> None:
    """Write the scheduler heartbeat. Best-effort — never raises."""
    try:
        _atomic_write_text(status_path(environment), json.dumps(status, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.debug("Failed to write status for env=%s: %s", environment, exc)


def read_status(environment: str) -> dict[str, Any]:
    path = status_path(environment)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ── Event log (append-only NDJSON) ────────────────────────────────────

def append_events(environment: str, events: Iterable[dict[str, Any]]) -> int:
    """Append drift events as one JSON object per line. Returns count.

    Holds an exclusive ``fcntl.flock`` for the duration of the append so
    concurrent run-once invocations cannot interleave partial JSON lines.
    Rotates the file to the most recent ``EVENTS_RETAIN_LINES`` lines when it
    exceeds ``EVENTS_MAX_BYTES`` to bound long-term growth.
    """
    path = events_path(environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure the file exists with secure perms before we open it for append —
    # ``open("a")`` honors umask, which on common systems leaves files 0o644.
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, SECURE_FILE_MODE)
            os.close(fd)
        except OSError:
            pass
    else:
        try:
            os.chmod(path, SECURE_FILE_MODE)
        except OSError:
            pass
    count = 0
    with path.open("a", encoding="utf-8") as fh:
        try:
            _flock(fh.fileno(), _LOCK_EX)
        except OSError as exc:
            logger.debug("flock unavailable on events.ndjson (%s); proceeding without lock", exc)
        try:
            _rotate_events_locked(path, fh)
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                count += 1
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        finally:
            try:
                _flock(fh.fileno(), _LOCK_UN)
            except OSError:
                pass
    return count


def _rotate_events_locked(path: Path, fh: Any) -> None:
    """If the events file exceeds the byte threshold, truncate to the last N lines.

    Caller must hold the exclusive flock on ``fh``. Rotation truncates the
    existing inode in place rather than replacing it, so the append handle
    ``fh`` stays valid for subsequent writes.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < EVENTS_MAX_BYTES:
        return
    try:
        # ``deque(maxlen=N)`` is O(1) per append AND O(1) per implicit eviction
        # — meaningfully cheaper than the previous ``list.pop(0)`` loop, which
        # was O(n²) over the rotation window.
        with path.open("r", encoding="utf-8") as src:
            tail: deque[str] = deque(src, maxlen=EVENTS_RETAIN_LINES)
        with path.open("w", encoding="utf-8") as dst:
            dst.writelines(tail)
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError:
                pass
        try:
            os.chmod(path, SECURE_FILE_MODE)
        except OSError:
            pass
        fh.seek(0, os.SEEK_END)
        logger.info("Rotated events.ndjson to last %d lines (was %d bytes)", len(tail), size)
    except OSError as exc:
        logger.warning("Failed to rotate events.ndjson at %s: %s", path, exc)


def read_events(environment: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read events newest-first. Reverses the file in memory — fine for our volumes."""
    path = events_path(environment)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Skipping malformed event line in %s", path)
    out.reverse()
    if limit is not None:
        out = out[:limit]
    return out


# ── Environments enumeration ──────────────────────────────────────────

def list_environments() -> list[str]:
    """Return all environments that have a continuous-audit directory."""
    if not CONTINUOUS_ROOT.is_dir():
        return []
    return sorted(p.name for p in CONTINUOUS_ROOT.iterdir() if p.is_dir())
