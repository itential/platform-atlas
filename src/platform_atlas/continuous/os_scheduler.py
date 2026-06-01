"""
OS-level scheduling for continuous audit.

Two backends:

    Linux (systemd):
        ~/.config/systemd/user/platform-atlas-continuous-<slug>.{service,timer}
        managed via ``systemctl --user``.

    macOS (launchd):
        ~/Library/LaunchAgents/com.itential.platform-atlas.continuous-<slug>.plist
        managed via ``launchctl bootstrap gui/$UID …`` (modern domain-targeted syntax).

On other platforms (Windows, BSD, Linux without systemd-user) ``is_available()``
returns False, callers fall back to the WebUI in-process scheduler, and the UI
surfaces an "OS scheduler unavailable" notice.

Public API is intentionally backend-agnostic — callers reach for
``is_available()``, ``install()``, ``uninstall()``, ``status()``, ``is_installed()``,
and ``unit_basename()``. Whether the resulting unit is a systemd timer or a
launchd agent is an implementation detail.

Security:
    - Env names are slugified to ``[a-z0-9][a-z0-9._-]{0,63}`` before they
      ever reach a filename or argv slot.
    - Both ``systemctl`` and ``launchctl`` are resolved via ``shutil.which``;
      argv lists are passed to ``subprocess.run`` (no ``shell=True``).
    - Unit files are atomically written under hardcoded roots and the resolved
      write target is asserted under that root before any I/O happens.
"""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess  # nosec B404 — argv-list invocations only, validated below
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Cache for ``status()`` — invalidated on install/uninstall. The WebUI's
# topbar pill calls this on every page render and the WebUI scheduler tick
# calls it every 30 s; without caching we'd burn ~5 systemctl shell-outs per
# page (each with a 15 s timeout if dbus is wedged).
_STATUS_CACHE_TTL_SECONDS = 30.0
_status_cache: dict[str, tuple[float, "TimerStatus"]] = {}
_status_cache_lock = threading.Lock()


def _cache_get(env: str) -> "TimerStatus | None":
    with _status_cache_lock:
        entry = _status_cache.get(env)
        if entry is None:
            return None
        cached_at, value = entry
        if (time.monotonic() - cached_at) > _STATUS_CACHE_TTL_SECONDS:
            _status_cache.pop(env, None)
            return None
        return value


def _cache_put(env: str, value: "TimerStatus") -> None:
    with _status_cache_lock:
        _status_cache[env] = (time.monotonic(), value)


def _cache_invalidate(env: str) -> None:
    """Drop the cached status for ``env`` after install/uninstall."""
    with _status_cache_lock:
        _status_cache.pop(env, None)


# ── Platform detection ───────────────────────────────────────────────

_PLATFORM = platform.system()  # "Linux" | "Darwin" | "Windows" | …
_IS_LINUX = _PLATFORM == "Linux"
_IS_DARWIN = _PLATFORM == "Darwin"


# ── Roots & names ────────────────────────────────────────────────────

SYSTEMD_USER_ROOT = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_AGENT_ROOT = Path.home() / "Library" / "LaunchAgents"

# systemd unit name prefix (what users see in ``systemctl --user list-timers``)
SYSTEMD_NAME_PREFIX = "platform-atlas-continuous-"
# launchd label prefix (Apple convention is reverse-DNS)
LAUNCHD_LABEL_PREFIX = "com.itential.platform-atlas.continuous-"

SUBPROCESS_TIMEOUT = 15  # seconds — local ops, fast

# Per systemd.unit(5) and launchd label rules: stay narrower than both.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class TimerStatus:
    """One env's view of the OS-level scheduler."""
    available: bool          # backend present at all?
    installed: bool          # unit/plist exists on disk?
    active: bool             # currently loaded / waiting / firing?
    enabled: bool            # will start at boot/login?
    linger_enabled: bool     # will runs persist across user logout?
    next_elapse: str = ""    # human-readable next-run estimate, if known
    detail: str = ""         # status text for the UI
    persistence_hint: str = ""  # platform-specific command to make scheduling persistent
    backend: str = ""        # "systemd" | "launchd" | ""


# ── Slug + name helpers ───────────────────────────────────────────────

def _slugify(env: str) -> str:
    """Lower, replace runs of disallowed chars with '-', trim, length-cap."""
    if not env:
        raise ValueError("Environment name is required for OS unit naming")
    s = env.lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    s = s[:64]
    if not _SLUG_RE.match(s):
        raise ValueError(f"Environment name cannot be slugified for OS scheduler: {env!r}")
    return s


def slug_for(env: str) -> str:
    """Public alias used by callers that want to display the unit name."""
    return _slugify(env)


def unit_basename(env: str) -> str:
    """Human-friendly identifier for the active backend.

    On Linux: the systemd unit basename (``platform-atlas-continuous-<slug>``).
    On macOS: the launchd label (``com.itential.platform-atlas.continuous-<slug>``).
    """
    if _IS_DARWIN:
        return f"{LAUNCHD_LABEL_PREFIX}{_slugify(env)}"
    return f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}"


def _assert_under_root(path: Path, root: Path) -> Path:
    """Resolve and assert ``path`` lives directly under ``root``."""
    root_resolved = root.resolve()
    resolved = path.resolve()
    if not resolved.parent.is_relative_to(root_resolved) and resolved.parent != root_resolved:
        raise PermissionError(f"Refusing to write outside {root_resolved}: {resolved}")
    return resolved


def _atomic_write_bytes(path: Path, content: bytes, root: Path, mode: int = 0o644) -> None:
    """Atomically write bytes under ``root`` with ``mode`` perms."""
    target = _assert_under_root(path, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        if os.name == "posix":
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, content: str, root: Path, mode: int = 0o644) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), root, mode)


def _atlas_argv() -> list[str]:
    """Return argv for invoking ``platform-atlas``.

    Returns the absolute binary path when on PATH, falling back to
    ``[sys.executable, "-m", "platform_atlas"]`` for dev installs / virtualenvs
    not wired into the user's PATH.
    """
    found = shutil.which("platform-atlas")
    if found:
        return [found]
    return [sys.executable, "-m", "platform_atlas"]


# =====================================================================
# systemd backend (Linux)
# =====================================================================

def _systemd_service_path(env: str) -> Path:
    return SYSTEMD_USER_ROOT / f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}.service"


def _systemd_timer_path(env: str) -> Path:
    return SYSTEMD_USER_ROOT / f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}.timer"


def _systemctl_bin() -> str | None:
    return shutil.which("systemctl")


def _run_systemctl(*args: str) -> tuple[int, str, str]:
    binary = _systemctl_bin()
    if binary is None:
        return 127, "", "systemctl not found on PATH"
    cmd = [binary, "--user", *args]
    logger.debug("os_scheduler exec: %s", cmd)
    try:
        proc = subprocess.run(  # nosec B603 — argv list, no shell, fixed binary
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"systemctl timed out after {SUBPROCESS_TIMEOUT}s"
    except OSError as exc:
        return 1, "", f"systemctl invocation failed: {exc}"


def _systemd_quote(value: str) -> str:
    """Wrap ``value`` in double quotes with C-style escaping for systemd unit content.

    Systemd's parser dequotes ``"…"`` in both ``ExecStart`` argv and
    ``Environment=`` values. Escaping ``\\`` and ``"`` inside is enough — the
    env-name validator already rejects newlines and nulls, so we don't have
    to worry about line injection here. This is defense-in-depth: if a future
    validator change loosens that, the unit content stays well-formed.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_service_unit(env: str) -> str:
    argv = _atlas_argv()
    quoted_env = _systemd_quote(env)
    # ExecStart goes through systemd's argv splitter, which honors double
    # quotes — so an env name with spaces survives intact rather than being
    # split into two argparse positionals.
    exec_start = " ".join(argv) + f" --env {quoted_env} continuous-audit run-once"
    return (
        "[Unit]\n"
        f"Description=Platform Atlas continuous audit (env={env})\n"
        "Documentation=https://github.com/itential/platform-atlas\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=ATLAS_ENV={quoted_env}\n"
        f"ExecStart={exec_start}\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
    )


def _systemd_timer_unit(env: str, interval_seconds: int) -> str:
    interval = max(60, int(interval_seconds))
    # Persistent=false: don't replay every missed run after long downtime
    # (e.g. laptop closed for two weeks → 336 catch-up OAuth fetches in a
    # burst). RandomizedDelaySec spreads multi-host fleet starts so they
    # don't all hit Platform on the exact same wall-clock second.
    return (
        "[Unit]\n"
        f"Description=Schedule for Platform Atlas continuous audit (env={env})\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=60\n"
        f"OnUnitActiveSec={interval}\n"
        "Persistent=false\n"
        "RandomizedDelaySec=120\n"
        "AccuracySec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _systemd_linger_enabled() -> bool:
    loginctl = shutil.which("loginctl")
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if loginctl is None or not user:
        return False
    try:
        proc = subprocess.run(  # nosec B603
            [loginctl, "show-user", user, "--property=Linger", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return proc.stdout.strip().lower() == "yes"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _systemd_next_elapse(timer_unit: str) -> str:
    rc, out, _ = _run_systemctl("show", timer_unit, "--property=NextElapseUSecRealtime", "--value")
    return out if rc == 0 else ""


def _systemd_is_available() -> bool:
    if _systemctl_bin() is None:
        return False
    rc, _, _ = _run_systemctl("--version")
    return rc == 0


def _systemd_is_installed(env: str) -> bool:
    try:
        return _systemd_service_path(env).is_file() and _systemd_timer_path(env).is_file()
    except ValueError:
        return False


def _systemd_install(env: str, interval_seconds: int) -> TimerStatus:
    try:
        _atomic_write_text(_systemd_service_path(env), _systemd_service_unit(env), SYSTEMD_USER_ROOT)
        _atomic_write_text(_systemd_timer_path(env), _systemd_timer_unit(env, interval_seconds), SYSTEMD_USER_ROOT)
    except (OSError, PermissionError, ValueError) as exc:
        logger.warning("Failed to write systemd units for env=%s: %s", env, exc)
        return TimerStatus(
            available=True, installed=False, active=False, enabled=False,
            linger_enabled=_systemd_linger_enabled(),
            detail=f"Could not write unit files: {exc}",
            persistence_hint="sudo loginctl enable-linger $USER",
            backend="systemd",
        )

    rc, _, err = _run_systemctl("daemon-reload")
    if rc != 0:
        logger.warning("daemon-reload failed: %s", err)

    timer_unit = f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}.timer"
    rc, _, err = _run_systemctl("enable", "--now", timer_unit)
    if rc != 0:
        return TimerStatus(
            available=True, installed=True, active=False, enabled=False,
            linger_enabled=_systemd_linger_enabled(),
            detail=f"Wrote unit files, but enable failed: {err or 'unknown error'}",
            persistence_hint="sudo loginctl enable-linger $USER",
            backend="systemd",
        )
    return _systemd_status(env)


def _systemd_uninstall(env: str) -> TimerStatus:
    timer_unit = f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}.timer"
    _run_systemctl("disable", "--now", timer_unit)
    for path in (_systemd_service_path(env), _systemd_timer_path(env)):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            logger.debug("Could not remove %s: %s", path, exc)
    _run_systemctl("daemon-reload")
    return TimerStatus(
        available=True, installed=False, active=False, enabled=False,
        linger_enabled=_systemd_linger_enabled(),
        detail="OS timer uninstalled.",
        persistence_hint="sudo loginctl enable-linger $USER",
        backend="systemd",
    )


def _systemd_status(env: str) -> TimerStatus:
    avail = _systemd_is_available()
    installed = _systemd_is_installed(env)
    if not avail:
        return TimerStatus(
            available=False, installed=installed, active=False, enabled=False,
            linger_enabled=False,
            detail="systemd --user not available; the WebUI scheduler is the only runner.",
            persistence_hint="sudo loginctl enable-linger $USER",
            backend="systemd",
        )
    if not installed:
        return TimerStatus(
            available=True, installed=False, active=False, enabled=False,
            linger_enabled=_systemd_linger_enabled(),
            detail="No OS timer installed for this environment.",
            persistence_hint="sudo loginctl enable-linger $USER",
            backend="systemd",
        )
    timer_unit = f"{SYSTEMD_NAME_PREFIX}{_slugify(env)}.timer"
    rc_active, out_active, _ = _run_systemctl("is-active", timer_unit)
    rc_enabled, out_enabled, _ = _run_systemctl("is-enabled", timer_unit)
    return TimerStatus(
        available=True,
        installed=True,
        active=(rc_active == 0 and out_active == "active"),
        enabled=(rc_enabled == 0 and out_enabled in ("enabled", "static")),
        linger_enabled=_systemd_linger_enabled(),
        next_elapse=_systemd_next_elapse(timer_unit),
        detail="OS timer installed.",
        persistence_hint="sudo loginctl enable-linger $USER",
        backend="systemd",
    )


# =====================================================================
# launchd backend (macOS)
# =====================================================================

def _launchd_label(env: str) -> str:
    return f"{LAUNCHD_LABEL_PREFIX}{_slugify(env)}"


def _launchd_plist_path(env: str) -> Path:
    return LAUNCHD_AGENT_ROOT / f"{_launchd_label(env)}.plist"


def _launchctl_bin() -> str | None:
    return shutil.which("launchctl")


def _launchd_uid() -> int:
    """Numeric UID of the current user — required for the gui/$UID domain target."""
    return os.getuid()  # macOS is POSIX; getuid always present


def _run_launchctl(*args: str) -> tuple[int, str, str]:
    binary = _launchctl_bin()
    if binary is None:
        return 127, "", "launchctl not found on PATH"
    cmd = [binary, *args]
    logger.debug("os_scheduler exec: %s", cmd)
    try:
        proc = subprocess.run(  # nosec B603 — argv list, no shell, fixed binary
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"launchctl timed out after {SUBPROCESS_TIMEOUT}s"
    except OSError as exc:
        return 1, "", f"launchctl invocation failed: {exc}"


def _launchd_log_paths(env: str) -> tuple[Path, Path]:
    """Where launchd stdout/stderr land. Co-located with the run reports."""
    from platform_atlas.core.paths import ATLAS_HOME
    base = ATLAS_HOME / "continuous" / _slugify(env)
    base.mkdir(parents=True, exist_ok=True)
    return base / "launchd.out", base / "launchd.err"


def _launchd_plist_content(env: str, interval_seconds: int) -> bytes:
    """Render the launch agent plist as bytes (plistlib does the XML escaping)."""
    label = _launchd_label(env)
    interval = max(60, int(interval_seconds))
    out_path, err_path = _launchd_log_paths(env)
    program_arguments = _atlas_argv() + ["--env", env, "continuous-audit", "run-once"]
    payload: dict = {
        "Label": label,
        "ProgramArguments": program_arguments,
        # Don't fire when the agent is loaded — let the interval drive everything,
        # so install never produces a surprise immediate run.
        "RunAtLoad": False,
        "StartInterval": interval,
        "EnvironmentVariables": {
            "ATLAS_ENV": env,
            # Launch agents inherit a minimal PATH; restore enough to find common tooling.
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(out_path),
        "StandardErrorPath": str(err_path),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def _launchd_is_available() -> bool:
    if _launchctl_bin() is None:
        return False
    # ``launchctl version`` is a printable verb that succeeds on any modern macOS.
    rc, _, _ = _run_launchctl("version")
    return rc == 0


def _launchd_is_installed(env: str) -> bool:
    try:
        return _launchd_plist_path(env).is_file()
    except ValueError:
        return False


def _launchd_is_loaded(env: str) -> bool:
    """``launchctl print gui/$UID/<label>`` returns 0 when the agent is loaded."""
    target = f"gui/{_launchd_uid()}/{_launchd_label(env)}"
    rc, _, _ = _run_launchctl("print", target)
    return rc == 0


def _launchd_install(env: str, interval_seconds: int) -> TimerStatus:
    plist_path = _launchd_plist_path(env)
    try:
        _atomic_write_bytes(plist_path, _launchd_plist_content(env, interval_seconds), LAUNCHD_AGENT_ROOT)
    except (OSError, PermissionError, ValueError) as exc:
        logger.warning("Failed to write launch agent plist for env=%s: %s", env, exc)
        return TimerStatus(
            available=True, installed=False, active=False, enabled=False,
            # Launch agents only run while a GUI session exists for the user.
            linger_enabled=False,
            detail=f"Could not write plist: {exc}",
            persistence_hint="(macOS launch agents only run while you're logged in. For unattended scheduling, run Atlas on a Linux host with systemd, or convert to a launch daemon — sudo required.)",
            backend="launchd",
        )

    # Confirm plist landed on disk before touching launchctl. _atomic_write_bytes
    # uses os.replace, but a hostile filesystem (read-only mount, full disk after
    # rename, ENOENT on resolved parent) could leave us without a file.
    if not plist_path.is_file():
        logger.warning("Launch agent plist write reported success but file is missing: %s", plist_path)
        return TimerStatus(
            available=True, installed=False, active=False, enabled=False,
            linger_enabled=False,
            detail=f"Plist write reported success but {plist_path} is missing on disk.",
            persistence_hint="(Check disk space and permissions on ~/Library/LaunchAgents.)",
            backend="launchd",
        )

    target_domain = f"gui/{_launchd_uid()}"
    plist = str(plist_path)
    label = _launchd_label(env)

    # Idempotent re-install: bootout first (capture rc — may legitimately not
    # have been loaded; rc 36 / 113 means "no such service"), then bootstrap.
    bootout_rc, _, bootout_err = _run_launchctl("bootout", f"{target_domain}/{label}")
    rc, _, err = _run_launchctl("bootstrap", target_domain, plist)
    if rc != 0:
        # Bootstrap failed AFTER plist was on disk and prior agent was bootout'd.
        # Surface both the bootstrap and the (possibly-helpful) bootout error.
        bootout_note = ""
        if bootout_rc not in (0, 36, 113):  # 36 / 113 = "no such service"
            bootout_note = f" (prior bootout rc={bootout_rc}: {bootout_err or 'no detail'})"
        return TimerStatus(
            available=True, installed=True, active=False, enabled=False,
            linger_enabled=False,
            detail=f"Wrote plist, but bootstrap failed: {err or 'unknown error'}{bootout_note}",
            persistence_hint=(
                "(macOS: launch agents need a logged-in user session. "
                "Try `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<plist>` manually for full diagnostics.)"
            ),
            backend="launchd",
        )
    # Mark agent enabled in case it was previously disabled.
    _run_launchctl("enable", f"{target_domain}/{label}")
    return _launchd_status(env)


def _launchd_uninstall(env: str) -> TimerStatus:
    label = _launchd_label(env)
    target = f"gui/{_launchd_uid()}/{label}"
    _run_launchctl("bootout", target)  # ignore rc — may not be loaded
    try:
        path = _launchd_plist_path(env)
        if path.is_file():
            path.unlink()
    except OSError as exc:
        logger.debug("Could not remove launch agent plist: %s", exc)
    return TimerStatus(
        available=True, installed=False, active=False, enabled=False,
        linger_enabled=False,
        detail="Launch agent uninstalled.",
        persistence_hint="(macOS: launch agents only run while you're logged in.)",
        backend="launchd",
    )


def _launchd_status(env: str) -> TimerStatus:
    avail = _launchd_is_available()
    installed = _launchd_is_installed(env)
    if not avail:
        return TimerStatus(
            available=False, installed=installed, active=False, enabled=False,
            linger_enabled=False,
            detail="launchctl not available; the WebUI scheduler is the only runner.",
            persistence_hint="",
            backend="launchd",
        )
    if not installed:
        return TimerStatus(
            available=True, installed=False, active=False, enabled=False,
            linger_enabled=False,
            detail="No launch agent installed for this environment.",
            persistence_hint="(macOS: launch agents only run while you're logged in.)",
            backend="launchd",
        )
    loaded = _launchd_is_loaded(env)
    return TimerStatus(
        available=True,
        installed=True,
        active=loaded,
        enabled=loaded,  # launchd doesn't separate "enabled at boot" the way systemd does for user agents
        linger_enabled=False,  # user agents stop firing on logout
        detail="Launch agent installed.",
        persistence_hint="(macOS: launch agents only run while you're logged in. Convert to a launch daemon for unattended scheduling — sudo required.)",
        backend="launchd",
    )


# =====================================================================
# Public dispatch
# =====================================================================

def is_available() -> bool:
    """Whether an OS-level scheduler is reachable on this host."""
    if _IS_LINUX:
        return _systemd_is_available()
    if _IS_DARWIN:
        return _launchd_is_available()
    return False


def is_installed(env: str) -> bool:
    if _IS_LINUX:
        return _systemd_is_installed(env)
    if _IS_DARWIN:
        return _launchd_is_installed(env)
    return False


def install(env: str, interval_seconds: int) -> TimerStatus:
    _cache_invalidate(env)
    if _IS_LINUX:
        return _systemd_install(env, interval_seconds)
    if _IS_DARWIN:
        return _launchd_install(env, interval_seconds)
    return TimerStatus(
        available=False, installed=False, active=False, enabled=False,
        linger_enabled=False,
        detail=f"No OS scheduler backend for platform: {_PLATFORM}. WebUI scheduler will run while the server is up.",
        backend="",
    )


def uninstall(env: str) -> TimerStatus:
    _cache_invalidate(env)
    if _IS_LINUX:
        return _systemd_uninstall(env)
    if _IS_DARWIN:
        return _launchd_uninstall(env)
    return TimerStatus(
        available=False, installed=False, active=False, enabled=False,
        linger_enabled=False,
        detail=f"No OS scheduler backend for platform: {_PLATFORM}.",
        backend="",
    )


def status(env: str) -> TimerStatus:
    """Return the per-env scheduler status, cached for ~30 s.

    Each call without a cache hit shells out 4–5 times to systemctl/launchctl
    with a 15 s timeout. The WebUI topbar renders this on every page, so we
    memoize. ``install`` and ``uninstall`` invalidate the cache so user
    actions reflect immediately.
    """
    cached = _cache_get(env)
    if cached is not None:
        return cached
    result = _status_uncached(env)
    _cache_put(env, result)
    return result


def _status_uncached(env: str) -> TimerStatus:
    if _IS_LINUX:
        return _systemd_status(env)
    if _IS_DARWIN:
        return _launchd_status(env)
    return TimerStatus(
        available=False, installed=False, active=False, enabled=False,
        linger_enabled=False,
        detail=f"No OS scheduler backend for platform: {_PLATFORM}. WebUI scheduler is the only runner.",
        backend="",
    )
