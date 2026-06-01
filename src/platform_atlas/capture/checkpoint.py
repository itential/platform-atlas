"""Per-session capture checkpoint — incremental collector result persistence.

After each collector module completes, its output is atomically written into
``00_checkpoint.json`` inside the session directory. On an interrupted run the
next capture invocation can pre-populate results from the checkpoint and skip
the collectors that already succeeded, cutting resume time proportionally to
how much had already been collected.

Lifecycle:
    1. CaptureCheckpoint(session_dir) created in the session handler.
    2. If .exists is True and the user confirms resume, the handler passes
       the checkpoint to run_capture() which pre-populates full_capture_json.
    3. run_capture() calls .save() after each successful module.
    4. After session.mark_stage_complete(CAPTURE), the handler calls .clear().
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_CHECKPOINT_FILENAME = "00_checkpoint.json"


class CaptureCheckpoint:
    """Manages a single per-session capture checkpoint file."""

    def __init__(self, session_dir: Path) -> None:
        self._path = session_dir / _CHECKPOINT_FILENAME

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> dict:
        """Return checkpoint data; empty dict when not present or unreadable."""
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read capture checkpoint %s: %s", self._path, exc)
            return {}

    def completed_modules(self) -> list[str]:
        """List of module names that have a non-empty result in the checkpoint."""
        data = self.load()
        return [k for k, v in data.items() if v and not k.startswith("_")]

    def save(self, flat_results: dict) -> None:
        """Atomically overwrite the checkpoint with the current flat results."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".tmp_",
            suffix="_checkpoint.json",
            dir=str(parent),
        )
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(flat_results, fh, indent=2, default=str, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Remove the checkpoint file — called after a successful capture save."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove capture checkpoint %s: %s", self._path, exc)
