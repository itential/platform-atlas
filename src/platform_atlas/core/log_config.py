"""
Centralized logging configuration for Platform Atlas

Call setup_logging() once from main(). Every module that does
logging.getLogger(__name__) will automatically inherit this config.
"""
from __future__ import annotations

import os
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path

from platform_atlas.core.paths import ATLAS_HOME, ATLAS_LOG_FILE

LOG_FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Keep a reference so we can attach session handlers later
_root_logger: logging.Logger | None = None

def setup_logging(*, debug: bool = False) -> None:
    """Configure logging for the entire application"""
    global _root_logger

    _root_logger = logging.getLogger("platform_atlas")
    _root_logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Don't double-add handlers if called twice
    if _root_logger.handlers:
        return

    # --- Console handler: warnings+ unless debug ---
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    _root_logger.addHandler(console_handler)

    # --- Global file handler: everything to ~/.atlas/atlas.log
    try:
        ATLAS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            ATLAS_LOG_FILE,
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=3,              # Keep atlas.log.1, .2, .3
            encoding="utf-8"
        )
        # Secure the log file after creation
        if os.name == "posix":
            os.chmod(ATLAS_LOG_FILE, 0o600)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        _root_logger.addHandler(file_handler)
    except OSError as exc:
        # Don't crash if we can't write logs - just warn to stderr
        console_handler.setLevel(logging.DEBUG)
        _root_logger.warning("Could not open log file %s: %s", ATLAS_LOG_FILE, exc)

def attach_session_log(session_log_path: Path) -> logging.Handler | None:
    """
    Add a session-specific file handler. Call from dispatch handlers
    after the active session is known.

    Returns the handler so it can be removed later if needed
    """
    if _root_logger is None:
        return None

    try:
        session_log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            session_log_path,
            maxBytes=2 * 1024 * 1024,   # 2 MB per session
            backupCount=1,              # Keep one backup
            encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        _root_logger.addHandler(handler)
        _root_logger.debug("Session log attached: %s", session_log_path)
        return handler
    except OSError as exc:
        _root_logger.warning("Could not open session log %s: %s", session_log_path, exc)
        return None

def detach_handler(handler: logging.Handler | None) -> None:
    """Remove a previously attached handler"""
    if handler and _root_logger:
        _root_logger.removeHandler(handler)
        handler.close()

def enable_debug() -> None:
    """Upgrade logging to DEBUG after config is loaded"""
    if _root_logger is None:
        return

    _root_logger.setLevel(logging.DEBUG)
    for handler in _root_logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.setLevel(logging.DEBUG)
