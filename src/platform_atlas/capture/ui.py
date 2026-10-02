"""
Platform Atlas // Capture Engine

Live rendering for capture now lives in ``core/pipeline_ui.py`` (the shared
Capture/Validate/Report cards). This module keeps only the pieces that are
about warning collection and terminal safety, not rendering.
"""

from __future__ import annotations

import logging
import warnings

from rich.console import Console

from platform_atlas.capture.models import CaptureState

logger = logging.getLogger(__name__)


def restore_terminal_cursor() -> None:
    """Restore terminal state (cursor) — called during cooperative shutdown."""
    try:
        Console().show_cursor(True)
    except Exception:
        pass


class WarningCapture:
    """Context manager to capture warnings and add them to CaptureState"""

    def __init__(self, state: CaptureState):
        self.state = state
        self._catch_warnings = None

    def __enter__(self):
        self._catch_warnings = warnings.catch_warnings(record=True)
        self._caught = self._catch_warnings.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Process any caught warnings before exiting
        self.process_warnings()
        return self._catch_warnings.__exit__(exc_type, exc_val, exc_tb)

    def process_warnings(self) -> None:
        """Transfer caught warnings to the CaptureState"""
        for w in self._caught:
            category = w.category.__name__
            message = str(w.message)
            self.state.add_warning(category, message)
        # Clear processed warnings
        self._caught.clear()
