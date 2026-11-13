"""Troubleshooting browser: records a Platform repro session for ``support-bundle --browser-session``."""

from platform_atlas.browser_session.installer import install_runtime, runtime_status
from platform_atlas.browser_session.recorder import (
    BrowserSessionResult,
    SessionOptions,
    run_session,
    unavailable_reason,
)

__all__ = [
    "BrowserSessionResult",
    "SessionOptions",
    "install_runtime",
    "run_session",
    "runtime_status",
    "unavailable_reason",
]
