"""
On-demand install of the troubleshooting browser's runtime.

Playwright and its Chromium build are NOT part of a default Atlas install: only people who enable the
troubleshooting browser and actually run ``support-bundle --browser-session`` ever download them. This module
works out what is missing, and installs it into the Python environment Atlas is running from.

Atlas is distributed as a WHL, so nothing here references an Atlas package index: Playwright is fetched from
whatever pip index the host already uses, and Chromium from Playwright's own download CDN. Offline / air-gapped
hosts must pre-stage both: install the playwright wheel from a local wheelhouse, then either run
``playwright install chromium`` on a connected machine and copy the browser directory, or point
``PLAYWRIGHT_BROWSERS_PATH`` at a pre-populated one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PLAYWRIGHT_SPEC = "playwright>=1.50,<2.0"
MANUAL_HINT = "python -m pip install playwright && python -m playwright install chromium"


@dataclass(slots=True)
class RuntimeStatus:
    """What the browser session still needs on this machine."""
    package_missing: bool
    chromium_missing: bool

    @property
    def ready(self) -> bool:
        return not (self.package_missing or self.chromium_missing)

    def describe(self) -> list[str]:
        """Human-readable list of what would be downloaded."""
        items = []
        if self.package_missing:
            items.append("Playwright Python package (about 40 MB)")
        if self.chromium_missing:
            items.append("Chromium browser build with video support (about 150 MB)")
        return items


def _browsers_dir(pkg_dir: Path) -> Path:
    """Where Playwright keeps its downloaded browsers (mirrors Playwright's own lookup)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override == "0":
        return pkg_dir / "driver" / "package" / ".local-browsers"
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def _chromium_installed() -> bool:
    """True when the installed Playwright already has its Chromium build on disk.

    This reads Playwright's browser registry and install marker straight from disk. Asking the driver
    (``sync_playwright()`` / ``executable_path``) would start a Node subprocess just to answer a yes/no
    question, and tearing it down leaves asyncio tasks that print "Task was destroyed but it is pending!"
    over the terminal.
    """
    try:
        spec = importlib.util.find_spec("playwright")
        if spec is None or not spec.origin:
            return False
        pkg_dir = Path(spec.origin).parent
        registry = json.loads((pkg_dir / "driver" / "package" / "browsers.json").read_text(encoding="utf-8"))
        revision = next(b["revision"] for b in registry["browsers"] if b["name"] == "chromium")
        return (_browsers_dir(pkg_dir) / f"chromium-{revision}" / "INSTALLATION_COMPLETE").is_file()
    except Exception:
        return False


def runtime_status() -> RuntimeStatus:
    """Check (without installing anything) what is missing."""
    if importlib.util.find_spec("playwright") is None:
        return RuntimeStatus(package_missing=True, chromium_missing=True)
    return RuntimeStatus(package_missing=False, chromium_missing=not _chromium_installed())


def _run(cmd: list[str]) -> str | None:
    """Run an installer command; return None on success or the tail of its output on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-6:])
    return tail or f"exit code {proc.returncode}"


def install_runtime(status: RuntimeStatus, say: Callable[[str], None] = print) -> str | None:
    """Install what ``status`` says is missing. Returns None on success, else a user-facing error."""
    if status.package_missing:
        if importlib.util.find_spec("pip") is None:
            return f"pip is not available in this Python environment. Install manually: {MANUAL_HINT}"
        say("Installing the Playwright package…")
        err = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", PLAYWRIGHT_SPEC])
        if err:
            return f"Could not install Playwright ({err}). If this host is offline, use: {MANUAL_HINT}"
        importlib.invalidate_caches()
    if status.chromium_missing:
        say("Downloading Chromium (one time, this can take a few minutes)…")
        err = _run([sys.executable, "-m", "playwright", "install", "chromium"])
        if err:
            return f"Could not download Chromium ({err}). If this host is offline, use: {MANUAL_HINT}"
    return None
