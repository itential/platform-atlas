# pylint: disable=line-too-long,too-many-locals,too-many-instance-attributes,unnecessary-pass
"""
Platform Atlas Session Manager

Manages audit sessions including creation, lifecycle, file organization,
and metadata tracking. Each session represents a complete audit workflow
from capture through validation to reporting.

Sessions are the primary unit of work in Atlas. Each session binds together
an environment, ruleset, and profile at creation time. Switching sessions
atomically restores the full context (environment, ruleset, profile).

Session Structure:
    ~/.atlas/sessions/<session-name>/
        ├── session.json            # Metadata
        ├── 01_capture.json         # Captured data
        ├── 02_validation.parquet   # Validation results
        ├── report.html             # Generated report
        ├── session.log             # Execution log
        └── debug.log               # Debug output (if --debug)
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import logging
import sys
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field, fields as dataclass_fields
from typing import Any, Iterator

from platform_atlas.core.paths import ATLAS_HOME_SESSIONS
from platform_atlas.core.utils import atomic_write_json, secure_mkdir
from platform_atlas.core.exceptions import (
    SessionError,
    SessionNotFoundError,
    SessionAlreadyExistsError,
    SessionInvalidStateError,
    SessionLockedError,
    NoActiveSessionError
)
from platform_atlas.core._version import __version__

logger = logging.getLogger(__name__)

# ── Platform-specific file locking ───────────────────────────────────────────
# fcntl is POSIX-only; Windows does not have it.  On Windows we skip advisory
# locking — single-process use is the common case and concurrent corruption is
# less likely than on a shared server.  O_CLOEXEC is also POSIX-only.
if sys.platform == "win32":
    import msvcrt as _msvcrt
    _O_CLOEXEC: int = 0

    def _flock_acquire_nb(fd: int) -> None:  # noqa: E306
        # Windows byte-range lock via msvcrt.locking.
        # Extend the file to at least 1 byte so there is a lockable range,
        # then lock byte 0.  Raises OSError (normalised to EACCES) when
        # another process already holds the lock.
        os.ftruncate(fd, max(1, os.fstat(fd).st_size))
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
        except OSError:
            raise OSError(errno.EACCES, "Session locked by another process") from None

    def _flock_release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl as _fcntl_mod
    _O_CLOEXEC = os.O_CLOEXEC
    def _flock_acquire_nb(fd: int) -> None:  # noqa: E306
        _fcntl_mod.flock(fd, _fcntl_mod.LOCK_EX | _fcntl_mod.LOCK_NB)
    def _flock_release(fd: int) -> None:
        _fcntl_mod.flock(fd, _fcntl_mod.LOCK_UN)

# Active session tracking file
ACTIVE_SESSION_FILE = ATLAS_HOME_SESSIONS / ".active"

class SessionStatus(str, Enum):
    """Session lifecycle states"""
    CREATED = "created"
    CAPTURING = "capturing"
    CAPTURED = "captured"
    VALIDATING = "validating"
    VALIDATED = "validated"
    REPORTED = "reported"
    FAILED = "failed"
    ABORTED = "aborted"
    ARCHIVED = "archived"

    def __str__(self) -> str:
        return self.value

class SessionStage(str, Enum):
    """Workflow stages within a session"""
    CAPTURE = "capture"
    VALIDATE = "validate"
    REPORT = "report"

    def __str__(self) -> str:
        return self.value

@dataclass(slots=True)
class SessionMetadata():
    """Session metadata and state"""
    name: str
    created_at: datetime
    updated_at: datetime
    status: SessionStatus
    description: str = ""
    target: str = ""
    organization_name: str = ""
    ruleset_id: str = ""
    ruleset_version: str = ""
    ruleset_profile: str = ""
    environment: str = ""
    atlas_version: str = __version__
    # Tier — bound at session creation, immutable afterward (like ruleset).
    # The session-switch flow restores this atomically alongside env/ruleset
    # so capture/validation/report semantics stay internally consistent.
    # Empty string (not "standard") so the report handler's fallback chain
    # (`meta.tier or df.attrs.get("tier") or "extended"`) correctly resolves
    # pre-tier sessions (created before 1.7) to "extended" rather than
    # short-circuiting on the truthy default.
    tier: str = ""

    # Stage tracking
    capture_completed: bool = False
    validation_completed: bool = False
    report_completed: bool = False

    # Execution metadata
    modules_ran: list[str] = field(default_factory=list)
    total_rules: int = 0
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0

    # Log date range (set when --log-since/--log-until used during capture)
    log_since: str = ""
    log_until: str = ""

    # File tracking
    capture_file: str | None = None
    validation_file: str | None = None
    report_file: str | None = None

    @property
    def is_editable(self) -> bool:
        """
        True if the session's bindings (env, ruleset, profile) can still
        be changed. Once capture begins, the session is locked.
        """
        return self.status == SessionStatus.CREATED and not self.capture_completed

    @property
    def next_step_label(self) -> tuple[str, str]:
        """
        Return (description, command) for the logical next step in the
        session pipeline. Useful for switch confirmations and the dashboard.
        """
        status = str(self.status)
        next_map = {
            "created":    ("Run data capture",      "platform-atlas session run capture"),
            "capturing":  ("Resume or re-run capture", "platform-atlas session run capture"),
            "captured":   ("Run validation",        "platform-atlas session run validate"),
            "validating": ("Resume validation",     "platform-atlas session run validate"),
            "validated":  ("Generate report",       "platform-atlas session run report"),
            "reported":   ("View report or export", f"platform-atlas session show {self.name}"),
            "failed":     ("Review errors",         f"platform-atlas session show {self.name}"),
        }
        return next_map.get(status, ("Continue", "platform-atlas session --help"))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        # Convert datetime objects to ISO strings
        data['created_at'] = self.created_at.isoformat()
        data['updated_at'] = self.updated_at.isoformat()
        # Convert enums to strings
        data['status'] = str(self.status)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMetadata:
        """Create from dictionary (JSON deserialization)"""
        # Work on a copy so we don't mutate the caller's dict
        data = dict(data)

        # Parse datetime strings
        data['created_at'] = datetime.fromisoformat(data['created_at'])
        data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        # Parse enum
        data['status'] = SessionStatus(data['status'])

        # Filter to known fields for forward compatibility
        # (older Atlas versions won't crash on newer session.json files)
        known = {f.name for f in dataclass_fields(cls)}
        unknown = set(data) - known
        if unknown:
            logger.debug("Ignoring unknown session metadata fields: %s", unknown)

        return cls(**{k: v for k, v in data.items() if k in known})

    def stamp_context(self) -> None:
        """
        Stamp the current Atlas context onto this metadata.

        For sessions created with the new binding flow (env/ruleset/profile
        selected at creation), this preserves the bound values and only
        fills in derived fields like ruleset_version.

        For legacy sessions created without bindings, this backfills from
        whatever is currently active — matching the old behavior.
        """
        from platform_atlas.core.context import ctx
        from platform_atlas.core.ruleset_manager import get_ruleset_manager

        context = ctx()
        rm = get_ruleset_manager()

        # Only overwrite if the session doesn't already have bindings
        # (backward compat for sessions created before the binding flow)
        if not self.ruleset_id:
            self.ruleset_id = rm.get_active_ruleset_id() or ""
        if not self.ruleset_profile:
            self.ruleset_profile = rm.get_active_profile_id() or ""
        if not self.environment:
            self.environment = context.active_environment or ""
        if not self.organization_name:
            self.organization_name = context.config.organization_name or ""

        # Always refresh the derived version field from the loaded ruleset
        if context.has_ruleset:
            self.ruleset_version = context.ruleset.ruleset.get("version", "")
        else:
            self.ruleset_version = ""

        # Always refresh tier too — this is called once, right after capture
        # actually runs, so it must reflect the tier collectors really ran
        # under. The tier stamped at `session create` can go stale if the
        # environment's tier changes between creation and capture, and
        # report-time gating (which sections appear in report.html) trusts
        # this field.
        self.tier = context.tier

        self.atlas_version = __version__


@dataclass(slots=True)
class Session:
    """Represents a complete audit session"""
    metadata: SessionMetadata

    def __repr__(self) -> str:
        return f"<Session {self.name!r} status={self.metadata.status}>"

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def directory(self) -> Path:
        """Get session directory path"""
        return ATLAS_HOME_SESSIONS / self.metadata.name

    @property
    def exists(self) -> bool:
        """Check if session directory exists"""
        return self.directory.exists()

    @property
    def metadata_file(self) -> Path:
        """Session metadata file path"""
        return self.directory / "session.json"

    @property
    def capture_file(self) -> Path:
        """Capture data file path"""
        return self.directory / "01_capture.json"

    @property
    def logs_file(self) -> Path:
        """Separate file for log analysis data (removed after report)"""
        return self.directory / "01_logs.json"

    @property
    def rbac_file(self) -> Path:
        """Separate file for RBAC authorization capture (removed after report)"""
        return self.directory / "01_capture_rbac.json"

    @property
    def validation_file(self) -> Path:
        """Validation results file path"""
        return self.directory / "02_validation.parquet"

    @property
    def kubernetes_namespaces_file(self) -> Path:
        """Additional Kubernetes namespace validation results (JSON).

        Kept separate from validation_file rather than DataFrame.attrs —
        Parquet round-trips drop .attrs, so anything needed after reload
        must be its own file. Absent entirely when the environment has no
        additional namespaces (the common case)."""
        return self.directory / "02_kubernetes_namespaces.json"

    @property
    def report_file(self) -> Path:
        """Report file path"""
        return self.directory / "report.html"

    @property
    def log_file(self) -> Path:
        """Session log file path"""
        return self.directory / "session.log"

    @property
    def debug_log_file(self) -> Path:
        """Debug log file path"""
        return self.directory / "debug.log"

    @property
    def architecture_progress_file(self) -> Path:
        """Architecture questions progress file path"""
        return self.directory / "architecture_progress.json"

    @property
    def operational_data_file(self) -> Path:
        """Operational report raw data (JSON) file path"""
        return self.directory / "04_operational.json"

    @property
    def webui_viewmodel_file(self) -> Path:
        """WebUI viewmodel JSON file path — typed contract consumed by the
        WebUI's tabbed Compliance/Operational/Architecture experience.

        Generated at report time alongside report.html (same viewmodel drives
        both). The WebUI falls back to building this on the fly for sessions
        that predate the file (older Atlas versions) or whose cached
        schema_version is stale.
        """
        return self.directory / "06_webui_viewmodel.json"

    def ensure_exists(self) -> None:
        """Create session directory if it doesn't exist"""
        secure_mkdir(self.directory)

    @contextmanager
    def exclusive_lock(self, *, timeout: float = 5.0) -> Iterator[None]:
        """Acquire a POSIX advisory lock on this session for the duration.

        Concurrent CLI runs against the same session corrupt the parquet/JSON
        files (last writer wins, half-written outputs leak through). The lock
        file lives at ``<session>/.atlas.lock`` and is held for the lifetime
        of the context. ``timeout`` is a short grace window for racing peers
        before we surface ``SessionLockedError`` to the user."""
        self.ensure_exists()
        lockfile = self.directory / ".atlas.lock"
        fd = os.open(lockfile, os.O_RDWR | os.O_CREAT | _O_CLOEXEC, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    _flock_acquire_nb(fd)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        raise SessionLockedError(
                            f"Session '{self.name}' is locked by another "
                            f"Platform Atlas process.",
                            details={"lockfile": str(lockfile)},
                        ) from None
                    time.sleep(0.2)
            try:
                # Stamp the holder so a stale lockfile is human-debuggable.
                os.ftruncate(fd, 0)
                os.write(
                    fd,
                    f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n".encode(),
                )
                yield
            finally:
                _flock_release(fd)
        finally:
            os.close(fd)

    def save_metadata(self) -> None:
        """Save metadata to disk via temp+rename so a crash mid-write
        cannot leave a half-written session.json on disk."""
        self.metadata.updated_at = datetime.now(timezone.utc)
        self.ensure_exists()

        # to_dict() returns datetimes; convert to isoformat strings before
        # handing off to atomic_write_json (which does not accept default=).
        payload = json.loads(json.dumps(self.metadata.to_dict(), default=str))
        atomic_write_json(self.metadata_file, payload)

    def load_metadata(self) -> None:
        """Load metadata from disk"""
        if not self.metadata_file.exists():
            raise SessionNotFoundError(
                f"Session metadata not found: {self.name}",
                details={"path": str(self.metadata_file)}
            )

        with open(self.metadata_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.metadata = SessionMetadata.from_dict(data)

    def update_status(self, status: SessionStatus) -> None:
        """Update session status"""
        self.metadata.status = status
        self.save_metadata()

    def mark_stage_complete(self, stage: SessionStage) -> None:
        """Mark a workflow stage as complete"""
        if stage == SessionStage.CAPTURE:
            self.metadata.capture_completed = True
            self.metadata.capture_file = "01_capture.json"
            if self.metadata.status == SessionStatus.CAPTURING:
                self.metadata.status = SessionStatus.CAPTURED

        elif stage == SessionStage.VALIDATE:
            self.metadata.validation_completed = True
            self.metadata.validation_file = "02_validation.parquet"
            if self.metadata.status == SessionStatus.VALIDATING:
                self.metadata.status = SessionStatus.VALIDATED

        elif stage == SessionStage.REPORT:
            self.metadata.report_completed = True
            self.metadata.report_file = "report.html"
            if self.metadata.status == SessionStatus.VALIDATED:
                self.metadata.status = SessionStatus.REPORTED

        self.save_metadata()

    def get_size(self) -> int:
        """Get total size of session directory in bytes"""
        if not self.exists:
            return 0

        total_size = 0
        for file_path in self.directory.rglob('*'):
            if file_path.is_file():
                total_size += file_path.stat().st_size
        return total_size

    def get_file_count(self) -> int:
        """Get number of files in session"""
        if not self.exists:
            return 0
        return sum(1 for _ in self.directory.rglob('*') if _.is_file())


class SessionManager:
    """Manages Platform Atlas audit sessions"""

    # Subdirectory inside an exported archive that holds the report files.
    # The archive's top level carries only the REPORT.html splash, so the
    # report link in that splash must agree with this name.
    EXPORT_SUBDIR = "session_files"

    def __init__(self):
        """Initialize session manager"""
        # Ensure sessions directory exists
        secure_mkdir(ATLAS_HOME_SESSIONS)

    def __repr__(self) -> str:
        count = sum(1 for _ in ATLAS_HOME_SESSIONS.iterdir()) if ATLAS_HOME_SESSIONS.exists() else 0
        return f"<SessionManager sessions={count}>"


    def create(
        self,
        name: str,
        *,
        description: str = "",
        target: str = "",
        organization_name: str = "",
        environment: str = "",
        ruleset_id: str = "",
        ruleset_profile: str = "",
        tier: str = "",
        force: bool = False
    ) -> Session:
        """Create a new audit session with bound environment, ruleset, profile, and tier.

        ``tier`` defaults to the active config tier when not supplied — that
        way callers don't need to know about tier resolution.
        """
        # Validate session name
        if not self._validate_session_name(name):
            raise SessionError(
                f"Invalid session name: {name}",
                details={
                    "rules": "3-64 chars, alphanumeric/hyphens/underscores only",
                    "example": "prod-audit-feb-2026"
                }
            )

        session_dir = ATLAS_HOME_SESSIONS / name

        # Check if exists
        if session_dir.exists() and not force:
            raise SessionAlreadyExistsError(
                f"Session already exists: {name}",
                details={"use_force": "Use force=True to overwrite"}
            )

        # Resolve tier from active config when caller didn't specify one.
        if not tier:
            try:
                from platform_atlas.core.config import get_config, is_config_loaded
                if is_config_loaded():
                    tier = get_config().tier
            except Exception:
                pass
            if not tier:
                tier = "standard"

        # Create session
        now = datetime.now(timezone.utc)
        metadata = SessionMetadata(
            name=name,
            created_at=now,
            updated_at=now,
            status=SessionStatus.CREATED,
            description=description,
            target=target,
            organization_name=organization_name,
            environment=environment,
            ruleset_id=ruleset_id,
            ruleset_profile=ruleset_profile,
            tier=tier,
        )

        session = Session(metadata=metadata)
        session.ensure_exists()
        session.save_metadata()

        logger.info("Created session: %s", name)
        return session

    def get(self, name: str) -> Session:
        """Get an existing session."""
        session_dir = ATLAS_HOME_SESSIONS / name

        if not session_dir.exists():
            raise SessionNotFoundError(
                f"Session not found: {name}",
                details={"path": str(session_dir)}
            )

        # Load metadata
        metadata_file = session_dir / "session.json"
        if not metadata_file.exists():
            raise SessionError(
                f"Session metadata missing: {name}",
                details={"expected": str(metadata_file)}
            )

        with open(metadata_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            metadata = SessionMetadata.from_dict(data)

        return Session(metadata=metadata)

    def list(
        self,
        *,
        limit: int | None = None,
        sort_by: str = "created_at",
        reverse: bool = True,
        status_filter: SessionStatus | None = None
    ) -> list[Session]:
        """List all sessions."""
        sessions = []

        if not ATLAS_HOME_SESSIONS.exists():
            return sessions

        # Scan session directories
        for session_dir in ATLAS_HOME_SESSIONS.iterdir():
            if not session_dir.is_dir():
                continue

            # Skip hidden files like .active
            if session_dir.name.startswith('.'):
                continue

            metadata_file = session_dir / "session.json"
            if not metadata_file.exists():
                logger.warning("Skipping invalid session %s", session_dir.name)
                continue

            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    metadata = SessionMetadata.from_dict(data)

                session = Session(metadata=metadata)

                if status_filter and session.metadata.status != status_filter:
                    continue

                sessions.append(session)
            except Exception as e:
                # A single unreadable session.json must never abort the whole
                # listing — a truncated or hand-edited file would otherwise
                # take out `session list`, `repair`, `prune`, `trend` and
                # `fleet status` at once, including the tools used to fix it.
                logger.warning("Skipping invalid session %s: %s", session_dir.name, e)
                continue

        # Sort sessions
        if sort_by == "created_at":
            sessions.sort(key=lambda s: s.metadata.created_at, reverse=reverse)
        elif sort_by == "updated_at":
            sessions.sort(key=lambda s: s.metadata.updated_at, reverse=reverse)
        elif sort_by == "name":
            sessions.sort(key=lambda s: s.name, reverse=reverse)

        # Apply limit
        if limit:
            sessions = sessions[:limit]

        return sessions

    def delete(self, name: str, *, force: bool = False) -> None:
        """Delete a session."""
        session = self.get(name)

        if not force:
            # Don't delete active session without force
            active = self.get_active_session_name()
            if active == name:
                raise SessionError(
                    f"Cannot delete active session: {name}",
                    details={"suggestion": "Deactivate first or use force=True"}
                )

        # Remove directory
        shutil.rmtree(session.directory)
        logger.info("Deleted session: %s", name)

        # Clear active if this was the active session
        if self.get_active_session_name() == name:
            self.clear_active()

    def set_status(self, name: str, status: str) -> None:
        """Update a session's status if it is not already in a terminal state."""
        _TERMINAL = {SessionStatus.REPORTED, SessionStatus.ARCHIVED}
        try:
            session = self.get(name)
            current = session.metadata.status
            if current in _TERMINAL:
                logger.debug(
                    "set_status(%s, %s) skipped — already terminal (%s)", name, status, current
                )
                return
            session.update_status(SessionStatus(status))
        except Exception as exc:
            logger.debug("set_status(%s, %s) failed: %s", name, status, exc)

    def set_active(self, name: str) -> None:
        """Set a session as active."""
        session = self.get(name)

        # Write to active file
        secure_mkdir(ACTIVE_SESSION_FILE.parent)
        ACTIVE_SESSION_FILE.write_text(name, encoding='utf-8')

        logger.info("Set active session: %s", name)

    def activate_session_context(self, name: str) -> Session:
        """
        Activate a session and restore its full context.

        Sets the active session, switches the environment, and loads the
        session's ruleset + profile. This is the primary mechanism for
        switching between audit contexts.

        Returns the activated session for display purposes.
        """
        session = self.get(name)
        meta = session.metadata

        # 1. Set the active session pointer
        self.set_active(name)

        # 2. Restore the session's environment (if bound)
        if meta.environment:
            try:
                from platform_atlas.core.environment import get_environment_manager
                env_mgr = get_environment_manager()
                if env_mgr.exists(meta.environment):
                    env_mgr.set_active(meta.environment)
                    logger.info("Restored environment: %s", meta.environment)
                else:
                    logger.warning(
                        "Session environment '%s' no longer exists — "
                        "environment not switched",
                        meta.environment,
                    )
            except Exception as e:
                logger.warning("Failed to restore environment '%s': %s", meta.environment, e)

        # 3. Restore the session's ruleset + profile (if bound)
        if meta.ruleset_id:
            try:
                # Write directly to settings.json — the next command's
                # init_context() will load and validate from there.
                # Going through RulesetManager.set_active_ruleset() here
                # can silently fail if the ruleset/profile can't be loaded
                # in this transient context.
                from platform_atlas.core.paths import ATLAS_SETTINGS_FILE
                from platform_atlas.core.utils import secure_mkdir

                secure_mkdir(ATLAS_SETTINGS_FILE.parent)
                with open(ATLAS_SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump({
                        "active_ruleset": meta.ruleset_id,
                        "active_profile": meta.ruleset_profile or None,
                    }, f, indent=4, ensure_ascii=False)

                logger.info(
                    "Restored ruleset: %s (profile: %s)",
                    meta.ruleset_id,
                    meta.ruleset_profile or "none",
                )
            except Exception as e:
                logger.warning(
                    "Failed to restore ruleset '%s': %s", meta.ruleset_id, e
                )

        return session

    def get_active_session_name(self) -> str | None:
        """Get the name of the active session."""
        if not ACTIVE_SESSION_FILE.exists():
            return None

        name = ACTIVE_SESSION_FILE.read_text(encoding='utf-8').strip()

        # Verify session still exists
        session_dir = ATLAS_HOME_SESSIONS / name
        if not session_dir.exists():
            logger.warning(
                "Active session '%s' no longer exists on disk - clearing stale pointer",
                name,
            )
            # Clear stale active session
            self.clear_active()
            return None

        return name

    def get_active(self) -> Session:
        """Get the active session object."""
        name = self.get_active_session_name()

        if not name:
            raise NoActiveSessionError(
                "No active session",
                details={"suggestion": "Use 'session active <n>' to set one"}
            )

        return self.get(name)

    def clear_active(self) -> None:
        """Clear the active session"""
        if ACTIVE_SESSION_FILE.exists():
            ACTIVE_SESSION_FILE.unlink()
            logger.info("Cleared active session")

    def export(
        self,
        name: str,
        output_path: Path,
        *,
        archive_format: str = "zip",
        include_debug: bool = False,
        redact: bool = True,
        report_json_path: Path | None = None,
        arc_dir_name: str | None = None,
        splash_path: Path | None = None,
    ) -> Path:
        """Export a session as a delivery archive.

        The archive always carries the customer-facing deliverable set — the
        report (compliance / operational / architecture as pages in one
        file), the machine-readable ``report.json``, the session metadata,
        and a README. ``include_debug`` additionally bundles the execution
        log and raw capture for Itential-side troubleshooting.

        ``redact=False`` (the legacy ``--no-redact`` flag) is treated as an
        alias for ``include_debug`` — raw capture is now gated by a single
        switch.

        Args:
            report_json_path: Path to a caller-generated ``report.json`` (the
                same structured export produced by ``report --format json``).
                The handler generates this because JSON assembly needs pandas
                and the reporting engine, which this module intentionally
                avoids importing.
            arc_dir_name: Name of the top-level folder inside the archive.
                Defaults to the session name; the handler passes an
                organization-aware name (``ATLAS-<org>-<session>-<date>``).
            splash_path: Path to a caller-rendered splash / cover page. When
                provided (and a report is present to link to), it is placed
                at the archive's top level as ``REPORT.html`` — the single
                landing file. The report itself moves into the
                ``session_files/`` subdirectory.

        Archive layout::

            <folder_name>/
                REPORT.html          # splash (only when a report is present)
                session_files/
                    report.html  report.json  session.json  <env>-architecture.json  README.txt
                    [debug files]
        """
        session = self.get(name)

        include_raw = include_debug or not redact
        folder_name = arc_dir_name or name

        # Architecture-overview answers live outside the session directory,
        # per environment (~/.atlas/architecture/<env>.json) — resolve and
        # rename so it's clear what the file is once separated from that
        # directory. Missing/never-completed is normal; the exists() check
        # in the loop below silently skips it. An unsafe historical env name
        # (pre-dates validation) shouldn't fail the whole export.
        from platform_atlas.core import architecture_store
        try:
            architecture_src: Path | None = architecture_store.path_for(session.metadata.environment)
        except ValueError:
            architecture_src = None
        architecture_arcname = (
            f"{architecture_src.stem}-architecture.json" if architecture_src is not None else "architecture.json"
        )

        # Candidate files in the order they should read in the README.
        # (arcname, source path, description, debug_only)
        candidates: list[tuple[str, Path | None, str, bool]] = [
            ("report.html",         session.report_file,
             "Compliance, Operational, and Architecture report (open in a browser)", False),
            ("report.json",         report_json_path,
             "Machine-readable report (Customer360 / Salesforce ingestion)", False),
            ("session.json",        session.metadata_file,
             "Session metadata", False),
            (architecture_arcname,  architecture_src,
             "Architecture overview answers for this environment", False),
            ("session.log",         session.log_file,
             "Execution log", True),
            ("01_capture.json",     session.capture_file,
             "Raw captured configuration", True),
            ("debug.log",           session.debug_log_file,
             "Debug output", True),
        ]

        # Create temp export directory. The report files go into a
        # session_files/ subdirectory; only the REPORT.html splash sits at the
        # top level, so unzipping leaves one obvious file to open.
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = Path(tmpdir) / folder_name
            files_dir = export_dir / self.EXPORT_SUBDIR
            files_dir.mkdir(parents=True)

            included: list[tuple[str, str]] = []  # (arcname, description) for README
            for arcname, src, desc, debug_only in candidates:
                if src is None:
                    continue
                if debug_only and not include_raw:
                    continue
                if Path(src).exists():
                    shutil.copy2(src, files_dir / arcname)
                    included.append((arcname, desc))

            # Splash (REPORT.html) at the top level → links into the report
            # under session_files/. Only added when that report exists, so a
            # capture-only export never carries a dead "Enter Report" link.
            report_present = any(arc == "report.html" for arc, _ in included)
            has_splash = (
                splash_path is not None
                and Path(splash_path).exists()
                and report_present
            )
            if has_splash:
                shutil.copy2(splash_path, export_dir / "REPORT.html")

            # README lives beside the files it lists, inside session_files/.
            readme_content = self._build_export_readme(
                session, included, subdir=self.EXPORT_SUBDIR, has_splash=has_splash
            )
            (files_dir / "README.txt").write_text(readme_content, encoding="utf-8")

            # Create archive
            if archive_format == "zip":
                import zipfile
                with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for file in sorted(export_dir.rglob('*')):
                        if file.is_file():
                            arcname = file.relative_to(export_dir.parent)
                            zf.write(file, arcname)

            elif archive_format == "tar.gz":
                import tarfile
                with tarfile.open(output_path, 'w:gz') as tf:
                    tf.add(export_dir, arcname=folder_name)

            else:
                raise ValueError(f"Unsupported format: {archive_format}")

        logger.info("Exported session '%s' to %s", name, output_path)
        return output_path

    @staticmethod
    def _build_export_readme(
        session: "Session",
        included: list[tuple[str, str]],
        *,
        subdir: str = "session_files",
        has_splash: bool = False,
    ) -> str:
        """Render the export README, listing exactly the bundled files.

        The organization name is included up front so delivered archives stay
        traceable to a customer even after they're detached from the filename.
        The layout section reflects the on-disk structure: a REPORT.html splash
        at the top level (when present) and the report files under ``subdir``.
        """
        meta = session.metadata
        org_label = meta.organization_name or "Unknown"
        env_label = meta.environment or "Unknown"
        ruleset = f"{meta.ruleset_id} v{meta.ruleset_version}".strip()

        lines = [
            "Platform Atlas — Audit Export",
            "=============================",
            "",
            f"Organization:   {org_label}",
            f"Session:        {session.name}",
            f"Environment:    {env_label}",
            f"Created:        {meta.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Target:         {meta.target or 'Unknown'}",
            f"Ruleset:        {ruleset}",
            f"Profile:        {meta.ruleset_profile or 'None'}",
            f"Atlas Version:  {meta.atlas_version}",
            "",
            "Layout",
            "------",
        ]
        if has_splash:
            lines.append("  REPORT.html             Start here — opens the audit report (in a browser).")
            lines.append(f"  {subdir}/")
        else:
            lines.append(f"  {subdir}/               Report files:")
        lines += [f"    - {arc:<20} {desc}" for arc, desc in included]
        lines.append(f"    - {'README.txt':<20} This file")
        lines += [
            "",
            "Next Steps",
            "----------",
            "Attach this archive to your Itential Enablement Request (ER) ticket so the",
            "Itential team can review your audit results.",
            "",
        ]
        return "\n".join(lines)

    def cleanup_old(self, days: int = 30) -> list[str]:
        """Delete sessions older than specified days."""
        from datetime import timedelta

        threshold = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = []

        for session in self.list():
            if session.metadata.created_at < threshold:
                try:
                    self.delete(session.name, force=True)
                    deleted.append(session.name)
                except SessionError as e:
                    logger.warning("Failed to delete %s: %s", session.name, e)

        return deleted

    @staticmethod
    def _validate_session_name(name: str) -> bool:
        """Validate session name format."""
        import re
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9_-]{1,62}[a-zA-Z0-9])?$'
        return bool(re.match(pattern, name))

# Convenience functions
_manager: SessionManager | None = None

def get_session_manager() -> SessionManager:
    """Get the global session manager instance"""
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager

# Convenience shortcuts
def create_session(name: str, **kwargs) -> Session:
    """Create a new session (convenience function)"""
    return get_session_manager().create(name, **kwargs)


def get_session(name: str) -> Session:
    """Get a session (convenience function)"""
    return get_session_manager().get(name)


def list_sessions(**kwargs) -> list[Session]:
    """List sessions (convenience function)"""
    return get_session_manager().list(**kwargs)


def get_active_session() -> Session:
    """Get active session (convenience function)"""
    return get_session_manager().get_active()


def rehydrate_validation_attrs(df, session: "Session") -> None:
    """Rehydrate a validation DataFrame's ``.attrs`` from the session's
    capture JSON (and metadata as a fallback).

    Why this exists: parquet round-trips lose ``df.attrs`` (CLAUDE.md
    hard-won lesson #3). Anything that loads ``02_validation.parquet`` and
    then feeds it to the report or diff engines MUST call this helper
    first, otherwise downstream `.attrs.get(...)` reads silently return
    defaults — diff banners disappear, hostname shows "Unknown", tier
    detection misses the cross-tier case, etc.

    Centralizing this in session_manager (instead of duplicating it in
    handlers/ and routes/) keeps the CLI and WebUI code paths in lock
    step. If new metadata fields are added to capture JSON, only this
    function needs updating.
    """
    if not session.capture_file.exists():
        # Capture file missing — degrade gracefully using session metadata.
        meta = session.metadata
        df.attrs.setdefault("organization_name", meta.organization_name or "Unknown")
        df.attrs.setdefault("environment", meta.environment or "")
        df.attrs.setdefault("ruleset_id", meta.ruleset_id or "")
        df.attrs.setdefault("ruleset_version", getattr(meta, "ruleset_version", "") or "")
        df.attrs.setdefault("ruleset_profile", meta.ruleset_profile or "")
        df.attrs.setdefault("tier", getattr(meta, "tier", None) or "extended")
        return

    try:
        with open(session.capture_file, encoding="utf-8") as f:
            capture = json.load(f)

        atlas = capture.get("_atlas", {})
        metadata = atlas.get("metadata", {})
        system_facts = atlas.get("system_facts", {})
        platform_data = capture.get("platform", {})
        health_server = (
            platform_data.get("health_server", {})
            if isinstance(platform_data, dict) else {}
        )

        df.attrs["hostname"] = system_facts.get("hostname", "Unknown")
        df.attrs["platform_ver"] = health_server.get("version", "Unknown")
        df.attrs["ruleset_id"] = metadata.get("ruleset_id", "")
        df.attrs["ruleset_version"] = metadata.get("ruleset_version", "")
        df.attrs["ruleset_profile"] = metadata.get("ruleset_profile", "")
        df.attrs["modules_ran"] = metadata.get("modules_ran", [])
        df.attrs["captured_at"] = metadata.get("captured_at", "")
        df.attrs["organization_name"] = metadata.get("organization_name", "")
        df.attrs["environment"] = metadata.get("environment", "")
        # Tier must round-trip — diff_engine reads this to detect cross-tier
        # comparisons (Standard vs Extended); without it both sides default
        # to "extended" and the cross-tier banner never fires.
        df.attrs["tier"] = (
            metadata.get("tier")
            or getattr(session.metadata, "tier", None)
            or "extended"
        )
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable capture — fall back to session metadata so
        # the caller still gets *something*, rather than completely empty
        # attrs that produce a broken report.
        meta = session.metadata
        df.attrs.setdefault("organization_name", meta.organization_name or "Unknown")
        df.attrs.setdefault("environment", meta.environment or "")
        df.attrs.setdefault("ruleset_id", meta.ruleset_id or "")
        df.attrs.setdefault("ruleset_version", getattr(meta, "ruleset_version", "") or "")
        df.attrs.setdefault("tier", getattr(meta, "tier", None) or "extended")
