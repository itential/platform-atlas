# pylint: disable=line-too-long
"""
Dispatch Handler ::: Sessions

Session create now interactively binds an environment, ruleset, and profile.
Session switch atomically restores the full context (env, ruleset, profile).
Session edit allows changing bindings before capture begins.
"""

import os
import re
import logging
from pathlib import Path
from argparse import Namespace

import questionary
from rich.console import Console
from rich.prompt import Confirm

# ATLAS Core
from platform_atlas.core.registry import registry
from platform_atlas.core.context import ctx
from platform_atlas.core.log_config import attach_session_log, detach_handler
from platform_atlas.core.exceptions import AtlasError, CaptureAborted

from platform_atlas.core import ui

# ATLAS Session Management
from platform_atlas.core.session_manager import (
    get_session_manager,
    SessionError,
    NoActiveSessionError,
    SessionStatus,
    SessionStage,
)

# ATLAS Management
from platform_atlas.core.ruleset_manager import get_ruleset_manager
from platform_atlas.core.paths import (
    REPORT_TEMPLATE,
    DIFF_TEMPLATE, ATLAS_HOME_DIFF,
)
from platform_atlas.core.init_setup import get_qstyle

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)


def _atomic_write_json_text(target: Path, data, *, mode: int = 0o600) -> None:
    """Serialize ``data`` to JSON and atomically replace ``target``.

    Writes to a same-directory temp file, fsyncs, sets 0600, then
    ``os.replace`` — on a SIGINT or disk-full mid-write the original
    file remains intact rather than being left half-written.
    """
    import json as _j
    import tempfile
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix="_" + target.name, dir=str(parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _j.dump(data, fh, indent=2, default=str, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# =================================================
# Session Binding Helpers
# =================================================

def _pick_environment(preselect: str | None = None) -> str | None:
    """
    Interactive environment picker for session creation/edit.

    Returns the chosen environment name, or None if canceled.
    Offers a 'Create new environment...' escape hatch.
    """
    from platform_atlas.core.environment import get_environment_manager

    mgr = get_environment_manager()
    env_names = mgr.list_names()

    if not env_names:
        console.print(
            f"\n  [{theme.warning}]No environments configured.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Let's create one now.[/{theme.text_dim}]\n"
        )
        from platform_atlas.core.init_setup import create_environment_wizard
        new_env = create_environment_wizard()
        if new_env is None:
            return None
        return new_env.name

    # Build choices with useful context
    choices = []
    active_env = mgr.get_active_name()

    for name in env_names:
        try:
            env = mgr.load(name)
            uri = env.platform_uri
            # Build a descriptive label
            parts = []
            if uri:
                parts.append(uri)
            detail = " — ".join(parts) if parts else env.description or ""

            suffix = " (active)" if name == active_env else ""
            label = f"{name}{suffix}"
            if detail:
                label += f"  ({detail})"
        except Exception:
            label = name

        choices.append(questionary.Choice(title=label, value=name))

    choices.append(questionary.Choice(
        title="── Create new environment...",
        value="_create_new",
    ))

    default = preselect if preselect in env_names else (active_env or env_names[0])

    selected = questionary.select(
        "Select environment:",
        choices=choices,
        default=default,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        return None

    if selected == "_create_new":
        from platform_atlas.core.init_setup import create_environment_wizard
        new_env = create_environment_wizard()
        if new_env is None:
            return None
        return new_env.name

    return selected


def _pick_ruleset(preselect: str | None = None) -> str | None:
    """
    Interactive ruleset picker. Returns ruleset ID or None if canceled.
    """
    rm = get_ruleset_manager()
    available = rm.discover_rulesets()

    if not available:
        console.print(
            f"\n  [{theme.warning}]No rulesets found.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Run 'platform-atlas ruleset list' or check "
            f"~/.atlas/rules/rulesets/[/{theme.text_dim}]\n"
        )
        return None

    active_id = rm.get_active_ruleset_id()
    choices = []

    for rs in available:
        suffix = " (active)" if rs.id == active_id else ""
        label = f"{rs.id}{suffix}  (v{rs.version} — {rs.rule_count} rules)"
        choices.append(questionary.Choice(title=label, value=rs.id))

    # Clamp the default to the offered choices — the preselect (a session's
    # saved binding) or the active ruleset may be hidden from the filtered
    # listing (e.g. a 2023 ruleset on a non-legacy environment), and
    # questionary raises ValueError on a default outside the choices.
    valid_ids = [r.id for r in available]
    if preselect in valid_ids:
        default = preselect
    elif active_id in valid_ids:
        default = active_id
    else:
        default = valid_ids[0]

    selected = questionary.select(
        "Select ruleset:",
        choices=choices,
        default=default,
        style=get_qstyle(),
    ).ask()

    return selected


def _pick_profile(preselect: str | None = None) -> str | None:
    """
    Interactive profile picker. Returns profile ID, or None when cancelled
    (or when no profile is visible at all — callers treat that as a cancel).

    There is deliberately no "no profile" choice — an audit always runs
    with a profile.
    """
    rm = get_ruleset_manager()
    available = rm.discover_profiles()

    if not available:
        console.print(
            f"\n  [{theme.warning}]No profiles found.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Place profile JSON files in "
            f"~/.atlas/rules/rulesets/profiles/[/{theme.text_dim}]\n"
        )
        return None

    active_id = rm.get_active_profile_id()
    choices = []

    for p in available:
        suffix = " (active)" if p.id == active_id else ""
        label = f"{p.id}{suffix}  ({p.description or f'{p.override_count} overrides'})"
        choices.append(questionary.Choice(title=label, value=p.id))

    # Clamp the default to the offered choices — the preselect (a session's
    # saved binding) or the active profile may be hidden from the filtered
    # listing (e.g. a platform profile while a SaaS environment is active),
    # and questionary raises ValueError on a default outside the choices.
    valid_ids = [p.id for p in available]
    if preselect in valid_ids:
        default = preselect
    elif active_id in valid_ids:
        default = active_id
    else:
        default = valid_ids[0]

    selected = questionary.select(
        "Select profile:",
        choices=choices,
        default=default,
        style=get_qstyle(),
    ).ask()

    return selected


def _show_session_status(session, *, show_bindings: bool = True) -> None:
    """
    Display a compact status summary after switching to a session.
    Shows bindings (env, ruleset, profile) and pipeline progress.
    """
    meta = session.metadata
    status_colors = {
        "created": theme.text_dim,
        "capturing": theme.primary,
        "captured": theme.info,
        "validating": theme.warning,
        "validated": theme.success,
        "reported": theme.success_glow,
        "failed": theme.error,
    }
    sc = status_colors.get(str(meta.status), theme.text_dim)

    def _dot(done: bool) -> str:
        return f"[{theme.success}]●[/{theme.success}]" if done else f"[{theme.text_ghost}]○[/{theme.text_ghost}]"

    pipeline = (
        f"{_dot(meta.capture_completed)} Capture  "
        f"{_dot(meta.validation_completed)} Validate  "
        f"{_dot(meta.report_completed)} Report"
    )

    console.print(
        f"\n  [{theme.success}]✓[/{theme.success}] Active session: "
        f"[{theme.accent} bold]{session.name}[/{theme.accent} bold]"
    )
    console.print(f"    Status: [{sc}]{meta.status}[/{sc}]")
    console.print(f"    Pipeline: {pipeline}")

    if show_bindings:
        org = meta.organization_name
        if org:
            console.print(f"    Organization: [bold]{org}[/bold]")
        # Tier badge — Standard (blue) / Extended (orange) / SaaS (pink)
        session_tier = getattr(meta, "tier", "") or "standard"
        if session_tier == "saas":
            tier_color = theme.tier_saas
        elif session_tier == "standard":
            tier_color = theme.primary
        else:
            tier_color = theme.accent
        console.print(
            f"    Mode: [{tier_color} bold]{session_tier.upper()}[/{tier_color} bold]"
        )
        if meta.environment:
            console.print(f"    Environment: [{theme.accent}]{meta.environment}[/{theme.accent}]")
        if meta.ruleset_id:
            profile_part = f" + {meta.ruleset_profile}" if meta.ruleset_profile else ""
            console.print(f"    Ruleset: [{theme.secondary}]{meta.ruleset_id}{profile_part}[/{theme.secondary}]")

    # Show next step hint
    label, cmd = meta.next_step_label
    console.print(
        f"\n    [{theme.accent}]→[/{theme.accent}] Next: {label}  "
        f"[bold {theme.primary}]{cmd}[/bold {theme.primary}]"
    )
    console.print()


_RESERVED_NAMES = frozenset({"latest", "current", "active", "temp", "tmp"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$|^[a-z]{1}[a-z0-9]{1,63}$")


def _validate_session_name(raw: str) -> tuple[bool, str, str]:
    """Validate a session name. Returns (is_valid, cleaned_suggestion, reason)."""
    if len(raw) < 3:
        return False, _derive_name_suggestion(raw, ""), "name must be at least 3 characters"
    if len(raw) > 64:
        return False, _derive_name_suggestion(raw, ""), "name must be at most 64 characters"
    if raw in _RESERVED_NAMES:
        return False, _derive_name_suggestion(raw, ""), f"'{raw}' is a reserved name"
    if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", raw) and not re.match(r"^[a-z][a-z0-9]?$", raw):
        return False, _derive_name_suggestion(raw, ""), (
            "name must start with a letter, end with a letter or digit, "
            "contain only lowercase letters, digits, and hyphens"
        )
    return True, raw, ""


def _derive_name_suggestion(raw: str, env_name: str) -> str:
    """Derive a cleaned session name suggestion from an invalid raw name."""
    import re as _re
    from datetime import date
    s = raw.lower()
    s = _re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) < 3:
        date_str = date.today().strftime("%Y-%m-%d")
        env_part = env_name.lower()[:20].strip("-") if env_name else "session"
        s = f"audit-{env_part}-{date_str}" if env_part else f"audit-{date_str}"
        s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > 64:
        s = s[:64].rstrip("-")
    return s


# =================================================
# Session Command Handlers - FULLY INTEGRATED
# =================================================

@registry.register("session", "create", description="Create a new audit session")
def handle_session_create(args: Namespace) -> int:
    """
    Create a new audit session with bound environment, ruleset, and profile.

    The interactive wizard prompts for each binding. Flags can bypass prompts:
        --env, --ruleset, --profile
    """
    try:
        manager = get_session_manager()

        session_name = args.session_name

        # ── Session name validation ──────────────────────────────────
        import sys as _sys
        import os as _os
        _is_tty = _os.isatty(_sys.stdin.fileno()) if hasattr(_sys.stdin, "fileno") else False

        _name_valid, _name_suggestion, _name_reason = _validate_session_name(session_name)
        if not _name_valid:
            if not _is_tty:
                # Non-interactive (piped/scripted): fail immediately
                console.print(
                    f"  [{theme.error}]✗ Invalid session name '{session_name}': "
                    f"{_name_reason}[/{theme.error}]"
                )
                console.print(f"  [{theme.text_dim}]Suggestion: {_name_suggestion}[/{theme.text_dim}]")
                return 1
            # Interactive: offer suggestion
            _name_choices = [
                questionary.Choice(f"Use suggestion: {_name_suggestion}", value="suggest"),
                questionary.Choice("Enter a different name", value="rename"),
                questionary.Choice("Cancel", value="cancel"),
            ]
            console.print(
                f"\n  [{theme.warning}]⚠ Invalid name '{session_name}': {_name_reason}[/{theme.warning}]"
            )
            _name_action = questionary.select(
                "How would you like to proceed?",
                choices=_name_choices,
                style=get_qstyle(),
            ).ask()
            if _name_action is None or _name_action == "cancel":
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1
            if _name_action == "rename":
                _new_raw = questionary.text(
                    "Enter session name:",
                    validate=lambda v: _validate_session_name(v)[0] or _validate_session_name(v)[2],
                    style=get_qstyle(),
                ).ask()
                if _new_raw is None:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 1
                session_name = _new_raw.strip()
            else:
                session_name = _name_suggestion

        # ── Collision detection ──────────────────────────────────────
        from platform_atlas.core.session_manager import ATLAS_HOME_SESSIONS as _SESSIONS_DIR
        if (_SESSIONS_DIR / session_name).exists():
            from datetime import datetime as _dt
            _ts = _dt.now().strftime("%H%M")
            _collision_choices = [
                questionary.Choice(f"Append timestamp: {session_name}-{_ts}", value="timestamp"),
                questionary.Choice("Replace existing (moves old to .bak)", value="replace"),
                questionary.Choice("Cancel", value="cancel"),
            ]
            _coll_action = questionary.select(
                f"Session '{session_name}' already exists — what should we do?",
                choices=_collision_choices,
                style=get_qstyle(),
            ).ask()
            if _coll_action is None or _coll_action == "cancel":
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1
            if _coll_action == "timestamp":
                session_name = f"{session_name}-{_ts}"
            elif _coll_action == "replace":
                _bak_name = f"{session_name}.bak-{_ts}"
                import shutil as _shutil
                _old_dir = _SESSIONS_DIR / session_name
                _bak_dir = _SESSIONS_DIR / _bak_name
                if _old_dir.exists():
                    _shutil.move(str(_old_dir), str(_bak_dir))
                    console.print(
                        f"  [{theme.text_dim}]Moved existing session to: {_bak_name}[/{theme.text_dim}]"
                    )

        # ── Resolve environment ──────────────────────────────────
        env_name = getattr(args, "env", None)
        if env_name is None:
            env_name = _pick_environment()
            if env_name is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

        # Organization name is global — always the single config.json value.
        org_name = ""
        try:
            org_name = ctx().config.organization_name or ""
        except Exception:
            pass

        # ── Resolve ruleset ──────────────────────────────────────
        ruleset_id = getattr(args, "ruleset", None)
        if ruleset_id is None:
            ruleset_id = _pick_ruleset()
            if ruleset_id is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

        # ── Resolve profile ──────────────────────────────────────
        profile_id = getattr(args, "profile", None)
        if profile_id is None:
            profile_id = _pick_profile()
            if profile_id is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

        # ── Resolve tier ─────────────────────────────────────────
        # Explicit --tier wins; otherwise defer to manager.create()'s
        # active-config fallback so existing scripts keep working.
        tier_arg = (getattr(args, "tier", None) or "").strip().lower()

        # ── Create session ───────────────────────────────────────
        session = manager.create(
            name=session_name,
            description=getattr(args, "description", "") or "",
            target=getattr(args, "target", "") or "",
            organization_name=org_name,
            environment=env_name,
            ruleset_id=ruleset_id,
            ruleset_profile=profile_id,
            tier=tier_arg,
        )

        # Activate the session (also restores env + ruleset context)
        manager.activate_session_context(session.name)

        _show_session_status(session)
        return 0

    except SessionError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


@registry.register("session", "edit", description="Edit session bindings (before capture)")
def handle_session_edit(args: Namespace) -> int:
    """
    Edit a session's environment, ruleset, or profile bindings.

    Only allowed before capture begins — once capture starts, the
    session is locked to its bindings.
    """
    try:
        manager = get_session_manager()

        if args.session_name:
            session = manager.get(args.session_name)
        else:
            session = manager.get_active()

        meta = session.metadata

        if not meta.is_editable:
            console.print(
                f"\n  [{theme.error}]✗ Session '{session.name}' cannot be edited[/{theme.error}]"
            )
            console.print(
                f"  [{theme.text_dim}]Sessions are locked after capture begins. "
                f"Create a new session to use different bindings.[/{theme.text_dim}]\n"
            )
            return 1

        console.print(
            f"\n[bold {theme.primary_glow}]Edit Session:[/bold {theme.primary_glow}] "
            f"[bold]{session.name}[/bold]\n"
        )

        changed = False

        while True:
            # Build choices showing current bindings. The organization name is
            # global (config.json, changed via `config edit`) — not a per-session
            # binding, so it isn't editable here.
            env_display = meta.environment or "[not set]"
            ruleset_display = meta.ruleset_id or "[not set]"
            profile_display = meta.ruleset_profile or "[none]"

            choices = [
                questionary.Choice(
                    title=f"Environment        {env_display}",
                    value="environment",
                ),
                questionary.Choice(
                    title=f"Ruleset            {ruleset_display}",
                    value="ruleset",
                ),
                questionary.Choice(
                    title=f"Profile            {profile_display}",
                    value="profile",
                ),
                questionary.Choice(title="Done", value="_done"),
            ]

            selected = questionary.select(
                "Select a binding to change:",
                choices=choices,
                style=get_qstyle(),
            ).ask()

            if selected is None or selected == "_done":
                break

            if selected == "environment":
                new_env = _pick_environment(preselect=meta.environment)
                if new_env is not None and new_env != meta.environment:
                    meta.environment = new_env
                    changed = True

            elif selected == "ruleset":
                new_rs = _pick_ruleset(preselect=meta.ruleset_id)
                if new_rs is not None and new_rs != meta.ruleset_id:
                    meta.ruleset_id = new_rs
                    changed = True

            elif selected == "profile":
                new_prof = _pick_profile(preselect=meta.ruleset_profile)
                if new_prof is not None and new_prof != meta.ruleset_profile:
                    meta.ruleset_profile = new_prof
                    changed = True

        if changed:
            session.save_metadata()
            # Re-activate so the global state matches
            manager.activate_session_context(session.name)
            console.print(
                f"  [{theme.success}]✓[/{theme.success}] Session bindings updated\n"
            )
        else:
            console.print(
                f"  [{theme.text_dim}]No changes made[/{theme.text_dim}]\n"
            )

        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


@registry.register("session", "run", "capture", description="Run capture stage within a session")
def handle_session_run_capture(args: Namespace) -> int:
    """Run capture stage within a session"""
    from platform_atlas.capture.capture_engine import run_capture
    try:
        manager = get_session_manager()

        # Get session (specified or active)
        if hasattr(args, 'session') and args.session:
            session = manager.get(args.session)
        else:
            session = manager.get_active()

        # Attach session log
        session_handler = attach_session_log(session.log_file)

        # Hold an exclusive POSIX lock for the entire capture so concurrent
        # CLI runs against the same session can't trample each other's
        # outputs (parquet/JSON corruption). Released in the finally below.
        _capture_lock = session.exclusive_lock()
        _capture_lock.__enter__()

        try:
            logger.info("Starting capture for session '%s'", session.name)

            # Ensure ruleset and profile are loaded
            rm = get_ruleset_manager()
            if not rm.get_active_ruleset_id():
                console.print(f"[{theme.error}]✗[/{theme.error}] No ruleset loaded")
                console.print(f"[{theme.text_dim}]Load one first: platform-atlas ruleset load <id>[/{theme.text_dim}]")
                return 1
            if not rm.get_active_profile_id():
                console.print(f"[{theme.error}]✗[/{theme.error}] No profile set")
                console.print(f"[{theme.text_dim}]Set one first: platform-atlas ruleset profile set <id>[/{theme.text_dim}]")
                console.print(f"[{theme.text_dim}]View options: platform-atlas ruleset profile list[/{theme.text_dim}]")
                return 1

            # ── Confirm before capture ────────────────────────────────
            headless = getattr(args, "headless", False)

            # Architecture answers are managed OUTSIDE the capture flow now
            # (a pre-capture notice below, or `env architecture`). Resolve the
            # env once — scoped to the env this session was created against,
            # never the (possibly different) currently-active env.
            arch_env = session.metadata.environment or ""
            skip_arch = getattr(args, "skip_architecture", False)

            # MongoDB operational pipelines are also chosen up front (below) and
            # executed after capture, so nothing interrupts the capture itself.
            _run_operational = False
            _operational_pipelines: list[str] | None = None

            # ── ControlMaster socket preflight ───────────────────────
            # Runs before the confirm prompt so a missing socket is caught early,
            # even in headless mode where there is no interactive gate.
            try:
                _cm_nodes = [
                    n for n in ctx().config.topology.nodes
                    if n.transport == "control_master"
                    and not getattr(n, "protocol_only", False)
                ]
            except Exception:  # pylint: disable=broad-except
                _cm_nodes = []

            # Jumphost tunnel (MongoDB/Redis, Extended tier, advanced/opt-in)
            # reuses the same ControlMaster socket mechanism as a topology
            # node — model it as one so it's covered by the exact same
            # status-check / auto-open / final-recheck flow below, instead
            # of duplicating all of it for one extra socket. It has no
            # collector modules of its own, so it's harmless if it ever ends
            # up in _skip_ssh_nodes — nothing will match that label.
            _jumphost = ctx().config.jumphost_tunnel
            if _jumphost is not None:
                from platform_atlas.core.topology import NodeRole, TargetNode
                _cm_nodes.append(TargetNode(
                    role=NodeRole.CUSTOM,
                    label="jumphost (Mongo/Redis tunnel)",
                    transport="control_master",
                    ssh_control_socket=_jumphost.control_socket,
                    ssh_control_target=_jumphost.ssh_target,
                    ssh_port=_jumphost.port,
                ))

            _skip_ssh_nodes: frozenset[str] | None = None

            if _cm_nodes:
                import stat as _stat_mod
                import subprocess as _subprocess

                _cm_persist = f"{ctx().config.control_persist_minutes}m"

                def _auto_open_node_inline(node) -> bool:
                    _port_args = ["-p", str(node.ssh_port)] if node.ssh_port != 22 else []
                    _p = Path(node.ssh_control_socket)
                    _p.parent.mkdir(parents=True, exist_ok=True)
                    if _p.exists():
                        try:
                            _p.unlink()
                        except OSError:
                            pass
                    _cmd = [
                        "ssh", "-M", "-S", node.ssh_control_socket, *_port_args,
                        "-o", f"ControlPersist={_cm_persist}",
                        "-o", "StrictHostKeyChecking=accept-new",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-fN", node.ssh_control_target,
                    ]
                    try:
                        return _subprocess.run(_cmd).returncode == 0  # stdio inherited
                    except Exception:  # pylint: disable=broad-except
                        return False

                def _socket_status(node) -> str:
                    """Return 'ok', 'stale', 'missing', or 'unconfigured'."""
                    if not node.ssh_control_target:
                        return "unconfigured"
                    path = node.ssh_control_socket
                    if not path:
                        return "missing"
                    p = Path(path)
                    if not p.exists():
                        return "missing"
                    if os.name == "posix" and not _stat_mod.S_ISSOCK(p.stat().st_mode):
                        return "stale"
                    # ssh -O check verifies the master is still alive without
                    # running a remote command.
                    try:
                        _chk = _subprocess.run(
                            ["ssh", "-O", "check", "-S", path, node.ssh_control_target],
                            capture_output=True, text=True, timeout=5, check=False,
                        )
                        return "ok" if _chk.returncode == 0 else "stale"
                    except Exception:  # pylint: disable=broad-except
                        return "stale"

                _cm_status = {n.label: _socket_status(n) for n in _cm_nodes}
                _cm_problem = [n for n in _cm_nodes if _cm_status.get(n.label) != "ok"]

                if _cm_problem:
                    console.print()
                    console.print(
                        f"  [{theme.warning}]⚠  Some ControlMaster sockets are not ready:[/{theme.warning}]"
                    )
                    console.print()
                    for _n in _cm_problem:
                        _st = _cm_status[_n.label]
                        _status_label = {
                            "missing": f"[{theme.error}]not found[/{theme.error}]",
                            "stale": f"[{theme.warning}]stale (exists but master not responding)[/{theme.warning}]",
                            "unconfigured": f"[{theme.text_dim}]SSH destination not configured[/{theme.text_dim}]",
                        }.get(_st, _st)
                        console.print(
                            f"  [{theme.text_dim}]Node[/{theme.text_dim}]  "
                            f"[{theme.primary}]{_n.label}[/{theme.primary}]  —  {_status_label}"
                        )
                    console.print()

                    _env_name = getattr(ctx().config, "active_environment", None) or "<env-name>"
                    _actionable = [n for n in _cm_problem if _cm_status[n.label] in ("missing", "stale") and n.ssh_control_target]
                    _stale = [n for n in _cm_problem if _cm_status[n.label] == "stale"]
                    _unconfigured = [n for n in _cm_problem if _cm_status[n.label] == "unconfigured"]

                    # Prominently surface the auto-open command so users know it exists
                    console.print(
                        f"  [{theme.primary_glow}]Tip:[/{theme.primary_glow}]  "
                        f"[bold]platform-atlas env sockets {_env_name} --open[/bold]  "
                        f"[{theme.text_dim}]opens all sockets automatically[/{theme.text_dim}]"
                    )
                    console.print()

                    def _ssh_open_cmd_inline(node) -> str:
                        _pf = f"-p {node.ssh_port} " if node.ssh_port != 22 else ""
                        return (f"ssh -M -S {node.ssh_control_socket} {_pf}-o ControlPersist={_cm_persist} "
                                f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null "
                                f"-fN {node.ssh_control_target}")

                    if headless:
                        console.print(
                            f"  [{theme.warning}]⚠  Headless mode — proceeding with capture. "
                            f"Nodes with closed sockets will fail individually.[/{theme.warning}]\n"
                        )
                    elif _actionable:
                        # Three-way prompt: auto-open / show commands / proceed anyway
                        _choices = [
                            questionary.Choice("Open them automatically  (Atlas runs SSH, you enter credentials)", value="auto"),
                            questionary.Choice("Show me the commands to open manually", value="show"),
                            questionary.Choice("Proceed anyway  (affected nodes may fail to connect)", value="proceed"),
                        ]
                        _action = questionary.select(
                            "What would you like to do?",
                            choices=_choices,
                            style=get_qstyle(),
                        ).ask()
                        if _action is None:
                            return 1

                        if _action == "show":
                            console.print()
                            for _n in _actionable:
                                console.print(f"  [{theme.text_dim}]{_n.label}:[/{theme.text_dim}]")
                                console.print(f"  {_ssh_open_cmd_inline(_n)}")
                                console.print()
                            if _unconfigured:
                                console.print(
                                    f"  [{theme.text_dim}]Nodes with no SSH destination: run "
                                    f"platform-atlas env edit {_env_name} → Edit a node[/{theme.text_dim}]"
                                )
                                console.print()
                            _ready = questionary.confirm(
                                "Open those sessions in another terminal, then press Enter when ready to capture",
                                default=True,
                                style=get_qstyle(),
                            ).ask()
                            if _ready is None or not _ready:
                                return 1

                        elif _action == "auto":
                            # Clean stale sockets first
                            for _n in _stale:
                                _sp = Path(_n.ssh_control_socket)
                                try:
                                    _sp.unlink(missing_ok=True)
                                except OSError:
                                    pass
                            console.print()
                            for _n in _actionable:
                                console.print(f"  [{theme.text_dim}]Opening {_n.label} — complete authentication when prompted...[/{theme.text_dim}]")
                                _ok = _auto_open_node_inline(_n)
                                if _ok:
                                    _chk_st = _socket_status(_n)
                                    if _chk_st == "ok":
                                        console.print(f"  [{theme.success}]✓[/{theme.success}] {_n.label} — socket open")
                                    else:
                                        console.print(f"  [{theme.warning}]⚠[/{theme.warning}] {_n.label} — SSH exited but socket not responding; capture may fail for this node")
                                else:
                                    console.print(f"  [{theme.error}]✗[/{theme.error}] {_n.label} — SSH command failed; capture may fail for this node")
                                console.print()
                        # "proceed" falls through to capture

                        if _unconfigured:
                            console.print(
                                f"  [{theme.text_dim}]Note: {len(_unconfigured)} node(s) have no SSH destination configured — "
                                f"they will fail individually. Run platform-atlas env edit {_env_name} → Edit a node to fix.[/{theme.text_dim}]"
                            )
                            console.print()
                    else:
                        # Only unconfigured nodes — nothing to open, just warn and confirm
                        console.print(
                            f"  [{theme.text_dim}]These nodes have no SSH destination configured. "
                            f"Edit them with: platform-atlas env edit {_env_name} → Deployment Topology → Edit a node[/{theme.text_dim}]"
                        )
                        console.print()
                        _proceed = questionary.confirm(
                            "Proceed with capture? (unconfigured nodes will fail individually)",
                            default=True,
                            style=get_qstyle(),
                        ).ask()
                        if _proceed is None or not _proceed:
                            return 1

                # Final socket health check immediately before capture.
                # Catches sockets that were OK at preflight but expired
                # during a long setup/troubleshooting session.
                _env_name_final = getattr(ctx().config, "active_environment", None) or "<env-name>"
                _final_not_ok = [n for n in _cm_nodes if _socket_status(n) != "ok"]
                if _final_not_ok:
                    console.print()
                    console.print(
                        f"  [{theme.warning}]⚠  {len(_final_not_ok)} socket(s) expired since preflight:[/{theme.warning}]"
                    )
                    for _n in _final_not_ok:
                        console.print(f"  [{theme.text_dim}]  • {_n.label}[/{theme.text_dim}]")
                    console.print(
                        f"\n  [{theme.text_dim}]Re-open:  "
                        f"platform-atlas env sockets {_env_name_final} --open[/{theme.text_dim}]\n"
                    )
                    if headless:
                        _skip_ssh_nodes = frozenset(n.label for n in _final_not_ok)
                    else:
                        _expire_choice = questionary.select(
                            "Some sockets expired during setup. What would you like to do?",
                            choices=[
                                questionary.Choice("Re-open them now  (Atlas runs SSH)", value="reopen"),
                                questionary.Choice("Skip SSH for those nodes  (SSH data omitted)", value="skip"),
                                questionary.Choice("Abort capture", value="abort"),
                            ],
                            style=get_qstyle(),
                        ).ask()
                        if _expire_choice is None or _expire_choice == "abort":
                            return 1
                        if _expire_choice == "reopen":
                            console.print()
                            for _n in _final_not_ok:
                                console.print(f"  [{theme.text_dim}]Opening {_n.label}...[/{theme.text_dim}]")
                                _auto_open_node_inline(_n)
                            _still_bad = [n for n in _final_not_ok if _socket_status(n) != "ok"]
                            if _still_bad:
                                console.print(
                                    f"\n  [{theme.warning}]⚠  {len(_still_bad)} socket(s) still not responding — "
                                    f"SSH will be skipped for those nodes.[/{theme.warning}]\n"
                                )
                                _skip_ssh_nodes = frozenset(n.label for n in _still_bad)
                            else:
                                console.print(f"  [{theme.success}]✓  All sockets open[/{theme.success}]\n")
                        else:
                            _skip_ssh_nodes = frozenset(n.label for n in _final_not_ok)

            if not headless:
                # Only show the "all open" hint when ALL sockets are verified OK.
                _cm_open = [n for n in _cm_nodes if not n.ssh_control_target or (
                    n.ssh_control_socket and _socket_status(n) == "ok"
                )] if _cm_nodes else []
                if _cm_open:
                    _cm_lines = "\n".join(
                        f"  [{theme.success}]✓[/{theme.success}]  {_n.label}  "
                        f"[{theme.text_dim}]→  {_n.ssh_control_socket}[/{theme.text_dim}]"
                        for _n in _cm_open
                    )
                    ui.hint_panel(
                        f"[{theme.text_dim}]Capture will use ControlMaster SSH for the following nodes. "
                        f"Keep these sessions open for the duration of the capture.[/{theme.text_dim}]\n\n"
                        + _cm_lines,
                        title="ControlMaster sessions",
                        style=theme.warning,
                    )

                meta = session.metadata
                console.print()
                console.print(f"  [{theme.text_dim}]Session[/{theme.text_dim}]       [bold]{session.name}[/bold]")
                if meta.environment:
                    console.print(f"  [{theme.text_dim}]Environment[/{theme.text_dim}]   [{theme.primary}]{meta.environment}[/{theme.primary}]")
                if meta.organization_name:
                    console.print(f"  [{theme.text_dim}]Organization[/{theme.text_dim}]  {meta.organization_name}")
                console.print(f"  [{theme.text_dim}]Ruleset[/{theme.text_dim}]       [{theme.secondary}]{rm.get_active_ruleset_id()}[/{theme.secondary}]")
                console.print(f"  [{theme.text_dim}]Profile[/{theme.text_dim}]       [{theme.accent}]{rm.get_active_profile_id()}[/{theme.accent}]")
                _session_tier = (getattr(meta, "tier", "") or "standard").lower()
                if _session_tier == "saas":
                    _tier_color = theme.tier_saas
                    _tier_note = "Single gateway audit — no Platform/Mongo/Redis"
                elif _session_tier == "standard":
                    _tier_color = theme.primary
                    _tier_note = "Platform only — no SSH/Mongo/Redis"
                else:
                    _tier_color = theme.accent
                    _tier_note = "Full infrastructure audit"
                console.print(
                    f"  [{theme.text_dim}]Mode[/{theme.text_dim}]          "
                    f"[{_tier_color} bold]{_session_tier.upper()}[/{_tier_color} bold]  "
                    f"[{theme.text_dim}]{_tier_note}[/{theme.text_dim}]"
                )
                console.print()

                # ── Architecture form notice (managed outside capture) ──
                # The interview no longer interrupts the capture flow. If this
                # env's form isn't finished, offer to do it now; either way
                # capture proceeds afterward. Extended + SaaS (gateway-scoped);
                # never Standard.
                if not skip_arch and not ctx().is_standard:
                    from platform_atlas.capture.collectors.manual import (
                        architecture_status,
                        run_architecture_collection,
                    )
                    _arch_state, _arch_done, _arch_total = architecture_status(arch_env)
                    if _arch_state != "complete":
                        _env_label = arch_env or "default"
                        if _arch_state == "empty":
                            _arch_msg = (
                                f"No architecture information has been collected for "
                                f"environment '{_env_label}' yet."
                            )
                        else:
                            _arch_msg = (
                                f"The architecture form for '{_env_label}' is incomplete "
                                f"({_arch_done} of {_arch_total} sections)."
                            )
                        _arch_cmd = "platform-atlas env architecture" + (f" {arch_env}" if arch_env else "")
                        ui.hint_panel(
                            f"[{theme.text_dim}]{_arch_msg}\n"
                            f"Manage it anytime with [bold]{_arch_cmd}[/bold].[/{theme.text_dim}]",
                            title="Architecture information",
                            style=theme.accent,
                        )
                        _fill_now = questionary.confirm(
                            "Fill out the architecture form now?",
                            default=(_arch_state == "empty"),
                            style=get_qstyle(),
                        ).ask()
                        if _fill_now is None:
                            raise KeyboardInterrupt
                        if _fill_now:
                            try:
                                run_architecture_collection(environment=arch_env)
                            except KeyboardInterrupt:
                                console.print(
                                    f"\n[{theme.warning}]Architecture form paused — "
                                    f"continuing with capture.[/{theme.warning}]"
                                )
                    console.print()

                # ── Optional: MongoDB operational pipelines (asked up front) ──
                # Capture the user's choice now; the pipelines themselves run
                # after capture so the capture is never interrupted. Mirrors the
                # WebUI, where all inputs are gathered before the run. Extended
                # tier only (Standard and SaaS have no MongoDB collector).
                if ctx().is_extended:
                    _run_operational, _operational_pipelines = _prompt_operational_choice()

                proceed = questionary.confirm(
                    "Ready to start capture?",
                    default=False,
                    style=get_qstyle(),
                ).ask()

                if proceed is None:
                    raise KeyboardInterrupt
                if not proceed:
                    console.print(f"\n  [{theme.text_dim}]Capture cancelled.[/{theme.text_dim}]\n")
                    return 2

            # Update status
            session.update_status(SessionStatus.CAPTURING)

            # Initialize log date range vars so the common post-capture path
            # can reference them regardless of whether manual or automated mode ran
            log_since_str: str | None = None
            log_until_str: str | None = None

            # ── Branch: Manual vs Automated ──────────────────────────
            manual_mode = hasattr(args, 'manual') and args.manual
            _capture_checkpoint = None  # Set for automated captures only

            if manual_mode:
                from platform_atlas.capture.guided_collector import (
                    GuidedCollector,
                    get_blueprints_for_ruleset,
                )
                from platform_atlas.capture.capture_engine import reshape_capture, finalize_capture

                blueprints = get_blueprints_for_ruleset(ctx().rules)
                import_dir = getattr(args, 'import_dir', None)

                if import_dir:
                    # ── Batch import from directory ──
                    from platform_atlas.capture.batch_import import batch_import, prompt_import_context

                    console.print(
                        f"[{theme.primary}]Batch import for session:"
                        f"[/{theme.primary}] {session.name}\n"
                    )

                    # Auto-detect IAP version from config, ask about gateways
                    blueprints = prompt_import_context(blueprints)

                    captured_data = batch_import(
                        directory=import_dir,
                        session_dir=session.directory,
                        blueprints=blueprints,
                    )

                    if not captured_data:
                        session.update_status(SessionStatus.CREATED)
                        return 1

                    # Check completeness (uses the same filtered blueprints)
                    collector = GuidedCollector(session.directory, blueprints)
                    if not collector.is_complete:
                        console.print(
                            f"\n[{theme.warning}]Not all required modules collected.[/{theme.warning}]"
                        )
                        from rich.prompt import Confirm as RConfirm
                        if not RConfirm.ask("Save partial capture anyway?", default=False):
                            session.update_status(SessionStatus.CREATED)
                            return 0

                else:
                    # ── Interactive guided collection ──
                    console.print(
                        f"[{theme.primary}]Guided manual capture for session:"
                        f"[/{theme.primary}] {session.name}\n"
                    )

                    collector = GuidedCollector(session.directory, blueprints)

                    try:
                        captured_data = collector.collect()
                    except KeyboardInterrupt:
                        console.print(
                            f"\n[{theme.warning}]Collection paused — progress saved.[/{theme.warning}]"
                        )
                        console.print(
                            f"[{theme.text_dim}]Run the same command again to resume.[/{theme.text_dim}]"
                        )
                        session.update_status(SessionStatus.CREATED)
                        return 0

                    # Check completeness
                    if not collector.is_complete:
                        console.print(
                            f"\n[{theme.warning}]Not all required modules collected.[/{theme.warning}]"
                        )
                        from rich.prompt import Confirm as RConfirm
                        if not RConfirm.ask("Save partial capture anyway?", default=False):
                            session.update_status(SessionStatus.CREATED)
                            return 0

                _modules_ran = list(captured_data.keys())
                structured = reshape_capture(captured_data)
                captured_data = finalize_capture(
                    structured_data=structured,
                    rules=ctx().rules,
                    ruleset=ctx().ruleset,
                    config=ctx().config,
                    modules_ran=_modules_ran,
                )

                logger.info("Manual capture returned %d modules", len(captured_data))

            else:
                # Automated capture  - run_capture() already finalizes internally
                console.print(
                    f"[{theme.primary}]Running capture for session:"
                    f"[/{theme.primary}] {session.name}\n"
                )
                modules = args.modules if hasattr(args, 'modules') else None
                logger.debug("Requested modules: %s", modules or "all")
                skip_guided = hasattr(args, 'skip_guided') and args.skip_guided
                skip_logs = getattr(args, "skip_logs", False)
                headless = getattr(args, "headless", False)

                # Parse optional log date range
                from datetime import datetime, timedelta, timezone
                from platform_atlas.capture.log_parser import ParserConfig, set_parser_config

                log_since: datetime | None = None
                log_until: datetime | None = None

                log_days = getattr(args, 'log_days', None)
                log_since_str = getattr(args, 'log_since', None)
                log_until_str = getattr(args, 'log_until', None)

                if log_days is not None and log_since_str:
                    console.print(
                        f"[bold {theme.error}]--log-days and --log-since cannot be used together. "
                        f"Use one or the other.[/bold {theme.error}]"
                    )
                    return 1

                if log_days is not None:
                    if not 1 <= log_days <= 30:
                        console.print(
                            f"[bold {theme.error}]--log-days must be between 1 and 30 "
                            f"(got {log_days})[/bold {theme.error}]"
                        )
                        return 1
                    log_since = datetime.now(timezone.utc) - timedelta(days=log_days)
                    log_since_str = log_since.strftime('%Y-%m-%d')

                if log_since_str:
                    try:
                        log_since = datetime.strptime(log_since_str, '%Y-%m-%d').replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        console.print(
                            f"[bold {theme.error}]Invalid --log-since: "
                            f"'{log_since_str}' — expected YYYY-MM-DD[/bold {theme.error}]"
                        )
                        return 1

                if log_until_str:
                    try:
                        # Set to end-of-day; LogParser's until is exclusive so
                        # add one day to include entries on the specified date.
                        log_until = (
                            datetime.strptime(log_until_str, '%Y-%m-%d').replace(
                                tzinfo=timezone.utc
                            ) + timedelta(days=1)
                        )
                    except ValueError:
                        console.print(
                            f"[bold {theme.error}]Invalid --log-until: "
                            f"'{log_until_str}' — expected YYYY-MM-DD[/bold {theme.error}]"
                        )
                        return 1

                # Build log parser config from CLI flags
                log_config = ParserConfig(
                    search_type=getattr(args, 'log_mode', 'top'),
                    top_n=getattr(args, 'log_top_n', 25),
                    levels=getattr(args, 'log_levels', ['error', 'warn']),
                    since=log_since,
                    until=log_until,
                )
                set_parser_config(log_config)

                # ── Capture checkpoint (resume interrupted runs) ──────────────
                from platform_atlas.capture.checkpoint import CaptureCheckpoint
                _capture_checkpoint = CaptureCheckpoint(session.directory)
                if _capture_checkpoint.exists:
                    _already_done = _capture_checkpoint.completed_modules()
                    if headless:
                        logger.info(
                            "Headless: resuming capture from checkpoint "
                            "(%d module(s) already collected)",
                            len(_already_done),
                        )
                    else:
                        console.print(
                            f"\n  [{theme.warning}]⚠[/{theme.warning}]  "
                            f"An incomplete capture was found "
                            f"({len(_already_done)} module(s) already collected)."
                        )
                        _do_resume = questionary.confirm(
                            "Resume from where it left off?",
                            default=True,
                            style=get_qstyle(),
                        ).ask()
                        if _do_resume is None:
                            raise KeyboardInterrupt
                        if not _do_resume:
                            _capture_checkpoint.clear()

                # Raw-capture debug export: env-persistent flag OR per-run CLI flag.
                # Writes the reshaped pre-filter capture to 01_raw_capture.json
                # so rule authors can browse the full dot-path tree.
                _raw_debug_on = (
                    bool(getattr(ctx().config, "debug_export_raw_capture", False))
                    or bool(getattr(args, "debug_raw_capture", False))
                )
                _raw_callback = None
                if _raw_debug_on:
                    import json as _json_raw
                    _raw_path = session.directory / "01_raw_capture.json"

                    def _write_raw(structured_data: dict) -> None:
                        _raw_path.parent.mkdir(parents=True, exist_ok=True)
                        _raw_path.write_text(
                            _json_raw.dumps(
                                structured_data,
                                indent=2,
                                default=str,
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                        console.print(
                            f"  [{theme.text_dim}][debug] Raw capture written → "
                            f"{_raw_path.name}[/{theme.text_dim}]"
                        )
                    _raw_callback = _write_raw

                try:
                    captured_data = run_capture(
                        modules,
                        skip_guided=skip_guided,
                        skip_logs=skip_logs,
                        headless=headless,
                        log_since=log_since,
                        log_until=log_until,
                        on_raw_capture=_raw_callback,
                        checkpoint=_capture_checkpoint,
                        skip_ssh_nodes=_skip_ssh_nodes,
                    )
                    logger.info("Capture returned %d top-level keys", len(captured_data))
                except ConnectionError as e:
                    console.print(
                        f"\n    [bold {theme.error}]Credential Backend failed:[/bold {theme.error}] {e}"
                    )
                    console.print(
                        f"\n    [{theme.text_dim}]Check Vault connectivity and credentials, "
                        f"then retry.[/{theme.text_dim}]\n"
                    )
                    session.update_status(SessionStatus.FAILED)
                    return 1

                # Guard: run_capture returns {"errors": [...]} when no modules ran
                if "errors" in captured_data and "_atlas" not in captured_data:
                    session.update_status(SessionStatus.FAILED)
                    return 1

            # ── Common path: attach architecture, save, metadata, done ──

            # Architecture answers are collected outside capture now (the
            # pre-capture notice above, or `env architecture`). Here we only
            # attach whatever is saved for this env — complete or partial — so
            # the report reflects exactly what the user has entered. Skipped in
            # Standard tier, where architecture review does not apply.
            if not ctx().is_standard:
                from platform_atlas.capture.collectors.manual import load_architecture_data
                arch_data = load_architecture_data(arch_env)
                if arch_data:
                    captured_data.setdefault("checks", {})["architecture_validation"] = arch_data
                    logger.info(
                        "Architecture data attached for env=%s (%d sections)",
                        arch_env or "_default", len(arch_data),
                    )
                elif session.capture_file.exists():
                    # Fall back to architecture from this session's prior capture.
                    try:
                        import json as _json
                        existing = _json.loads(
                            session.capture_file.read_text(encoding="utf-8")
                        )
                        prior = existing.get("checks", {}).get("architecture_validation")
                        if prior:
                            captured_data.setdefault("checks", {})["architecture_validation"] = prior
                            logger.debug(
                                "Reused architecture data from prior capture for env=%s",
                                arch_env or "_default",
                            )
                    except Exception:
                        logger.debug("No previous architecture data to reuse")

            # Save to session directory.
            # Pop logs and RBAC data out of captured_data first so we serialize
            # once — previously we wrote the file, popped, then re-wrote, leaving
            # a half-written file on SIGINT/disk-full between the two writes.
            import json
            platform_data = captured_data.get("platform", {})
            mongo_data = captured_data.get("mongo", {})
            logs_payload: dict[str, object] = {}
            if "log_analysis" in platform_data:
                logs_payload["log_analysis"] = platform_data.pop("log_analysis")
            if "webserver_logs" in platform_data:
                logs_payload["webserver_logs"] = platform_data.pop("webserver_logs")
            if "log_analysis" in mongo_data:
                logs_payload["mongo_log_analysis"] = mongo_data.pop("log_analysis")

            # RBAC capture is large — store it in its own sidecar file and
            # remove it from the main capture so 01_capture.json stays lean.
            rbac_payload = captured_data.pop("authorization", None)

            _atomic_write_json_text(session.capture_file, captured_data)

            if logs_payload:
                _atomic_write_json_text(session.logs_file, logs_payload)
                logger.info("Log analysis saved separately (%d keys)", len(logs_payload))

            if rbac_payload:
                _atomic_write_json_text(session.rbac_file, rbac_payload)
                logger.info("RBAC authorization data saved separately: %s", session.rbac_file)

            # Persist log date range in session metadata so it survives
            # across separate report runs and logs file cleanup
            if log_since_str:
                session.metadata.log_since = log_since_str
            if log_until_str:
                session.metadata.log_until = log_until_str

            # Stamp current context (ruleset, version, profile, environment)
            session.metadata.stamp_context()
            session.metadata.modules_ran = captured_data.get('_atlas', {}).get('metadata', {}).get('modules_ran', [])
            session.mark_stage_complete(SessionStage.CAPTURE)

            # Clear checkpoint — capture saved successfully, no resume needed
            if _capture_checkpoint is not None:
                _capture_checkpoint.clear()

            # Configuration capture is saved. If the user opted into operational
            # pipelines they run next, so don't announce "complete" yet — it
            # reads as contradictory when more work immediately follows.
            if _run_operational:
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Configuration capture saved")
            else:
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Capture complete")
            console.print(f"  Saved to: {session.capture_file}")

            # ── Optional: MongoDB Operational Pipelines ──────────────────────
            # The choice was made up front (before capture started); run the
            # selected pipelines now so the capture itself finished without any
            # interruption. ``_run_operational`` is only set in interactive,
            # Extended-tier runs, so headless/Standard naturally skip this.
            if _run_operational:
                _collect_operational_pipelines(session, pipeline_names=_operational_pipelines)
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Capture complete")

            console.print()
            ui.next_step("platform-atlas session run validate")
            return 0
        except CaptureAborted:
            from platform_atlas.core.shutdown import run_cleanups as _run_cleanups
            _run_cleanups()
            try:
                manager.set_status(session.name, "aborted")
            except Exception:
                pass
            console.print(f"\n  [{theme.warning}]⚠ Capture aborted.[/{theme.warning}]")
            console.print(
                f"  [{theme.text_dim}]Progress checkpoint saved — re-run capture to pick up where it left off.[/{theme.text_dim}]"
            )
            console.print(
                f"  [{theme.text_dim}]To resume: platform-atlas session run capture[/{theme.text_dim}]"
            )
            return 130
        finally:
            try:
                _capture_lock.__exit__(None, None, None)
            finally:
                detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        if isinstance(e, NoActiveSessionError):
            console.print()
            ui.hint_panel(
                f"Create a session with: [bold {theme.primary}]platform-atlas session create <name>[/bold {theme.primary}]",
                title="No Active Session",
                style=theme.warning,
            )
        return 1
    except AtlasError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1

@registry.register("session", "run", "validate", description="Run validation stage within a session")
def handle_session_run_validate(args: Namespace) -> int:
    """Run validation stage within a session"""
    from platform_atlas.validation.validation_engine import validate_from_files
    try:
        manager = get_session_manager()

        # Get session (specified or active)
        if hasattr(args, 'session') and args.session:
            session = manager.get(args.session)
        else:
            session = manager.get_active()

        # Attach session log
        session_handler = attach_session_log(session.log_file)

        # Serialize concurrent runs against the same session — see capture
        # handler for rationale. Released in the finally below.
        _validate_lock = session.exclusive_lock()
        _validate_lock.__enter__()

        try:
            # Check that capture is complete
            if not session.metadata.capture_completed:
                raise SessionError(
                    "Capture not complete",
                    details={"suggestion": "Run 'platform-atlas session run capture' first"}
                )

            if not session.capture_file.exists():
                raise SessionError(
                    "Capture file not found",
                    details={"expected": str(session.capture_file)}
                )

            # Update status
            session.update_status(SessionStatus.VALIDATING)

            # Ensure ruleset is loaded
            if not get_ruleset_manager().get_active_ruleset_id():
                console.print(f"[{theme.error}]✗[/{theme.error}] No ruleset loaded")
                console.print(f"[{theme.text_dim}]Load one first: platform-atlas ruleset load <id>[/{theme.text_dim}]")
                return 1

            # Ensure profile is set
            if not get_ruleset_manager().get_active_profile_id():
                console.print(f"[{theme.error}]✗[/{theme.error}] No profile set")
                console.print(f"[{theme.text_dim}]Set one first: platform-atlas ruleset profile set <id>[/{theme.text_dim}]")
                console.print(f"[{theme.text_dim}]View options: platform-atlas ruleset profile list[/{theme.text_dim}]")
                return 1

            # Run validation
            console.print(f"[{theme.primary}]Running validation for session:[/{theme.primary}] {session.name}\n")
            skip_adapter_check = getattr(args, "skip_adapter_check", False)
            df = validate_from_files(session.capture_file, skip_adapter_check=skip_adapter_check)

            # Atomic parquet write — pyarrow's writer is not crash-safe on its
            # own; render to a temp file, fsync, then os.replace.
            import tempfile as _tf
            _parquet_dir = session.validation_file.parent
            _parquet_dir.mkdir(parents=True, exist_ok=True)
            _pq_fd, _pq_tmp = _tf.mkstemp(
                prefix=".tmp_", suffix="_" + session.validation_file.name, dir=str(_parquet_dir)
            )
            os.close(_pq_fd)
            try:
                df.to_parquet(_pq_tmp, engine="pyarrow", compression="snappy")
                if os.name == "posix":
                    os.chmod(_pq_tmp, 0o600)
                os.replace(_pq_tmp, session.validation_file)
            except Exception:
                try:
                    os.unlink(_pq_tmp)
                except OSError:
                    pass
                raise

            # Additional Kubernetes namespaces (rare — most environments have
            # none): validate each one against its own small rule subset and
            # write results to a sibling JSON file. DataFrame.attrs would not
            # survive the Parquet round-trip above, so this can't ride along
            # on df — see kubernetes_namespaces_file's docstring.
            try:
                from platform_atlas.core.json_utils import load_json
                from platform_atlas.validation.validation_engine import validate_multi_target_namespaces

                _captured_data = load_json(session.capture_file)
                _ns_results = validate_multi_target_namespaces(ctx().rules, _captured_data)
                if _ns_results:
                    import json as _json
                    with open(session.kubernetes_namespaces_file, "w", encoding="utf-8") as _f:
                        _json.dump(_ns_results, _f, ensure_ascii=False, indent=2)
                elif session.kubernetes_namespaces_file.exists():
                    # A previous validate run had extra namespaces; this one
                    # doesn't (env was edited) — don't leave stale results.
                    session.kubernetes_namespaces_file.unlink()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Multi-namespace Kubernetes validation skipped: %s", exc)

            # Update metadata with stats
            session.metadata.total_rules = len(df)
            session.metadata.pass_count = len(df[df['status'].str.upper() == 'PASS'])
            session.metadata.fail_count = len(df[df['status'].str.upper() == 'FAIL'])
            session.metadata.skip_count = len(df[df['status'].str.upper() == 'SKIP'])
            session.mark_stage_complete(SessionStage.VALIDATE)

            passed  = session.metadata.pass_count
            failed  = session.metadata.fail_count
            skipped = session.metadata.skip_count
            evaluated = passed + failed
            pct = round(passed / evaluated * 100, 1) if evaluated else 0.0

            if pct >= 90:
                score_color = theme.success
            elif pct >= 70:
                score_color = theme.warning
            else:
                score_color = theme.error

            from rich.table import Table as _Table
            from rich import box as _box
            from rich.panel import Panel as _Panel

            score_table = _Table(box=_box.SIMPLE, show_header=False, pad_edge=False)
            score_table.add_column(style=theme.text_dim, min_width=10)
            score_table.add_column(justify="right", min_width=6)
            score_table.add_row("Compliant",      f"[{theme.success}]{passed}[/{theme.success}]")
            score_table.add_row("Non-Compliant", f"[{theme.error}]{failed}[/{theme.error}]")
            if skipped:
                score_table.add_row("Skip", f"[{theme.text_dim}]{skipped}[/{theme.text_dim}]")
            score_table.add_row("Score", f"[bold {score_color}]{pct:.1f}%[/bold {score_color}]")

            console.print()
            console.print(_Panel(
                score_table,
                title=f"[bold {theme.primary_glow}]Validation Results[/bold {theme.primary_glow}]",
                border_style=score_color,
                box=_box.ROUNDED,
                expand=False,
            ))
            console.print(f"  [{theme.text_dim}]Saved to: {session.validation_file}[/{theme.text_dim}]")
            console.print()
            ui.next_step("platform-atlas session run report")
            return 0
        finally:
            try:
                _validate_lock.__exit__(None, None, None)
            finally:
                detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1
    except AtlasError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1

def _emit_report_summary(session, df, output_path, *, args: Namespace) -> None:
    """Finalize a report run: mark complete, print the score panel + file
    path, and open the report in a browser.
    """
    session.mark_stage_complete(SessionStage.REPORT)
    _cleanup_logs_file(session)
    _cleanup_rbac_file(session)

    # ── Score summary ────────────────────────────────────────────────
    # Reuse the report's own calculation so the printed score matches the
    # report exactly: pass rate over EVALUATED rules (skipped excluded),
    # identical to report_renderer's ``{{PASS_PERCENT}}`` (= int(pass_percent)).
    from platform_atlas.reporting.report_renderer import calculate_stats
    stats     = calculate_stats(df)
    total     = stats["total"]
    passed    = stats["pass_count"]
    failed    = stats["fail_count"]
    skipped   = stats["skip_count"]
    evaluated = passed + failed + stats["error_count"]
    pct       = int(stats["pass_percent"])

    if pct >= 90:
        score_color = theme.success
    elif pct >= 70:
        score_color = theme.warning
    else:
        score_color = theme.error

    from rich import box as _box
    from rich.table import Table as _Table
    score_table = _Table(box=_box.SIMPLE, show_header=False, pad_edge=False)
    score_table.add_column(style=theme.text_dim, min_width=10)
    score_table.add_column(justify="right", min_width=6)
    score_table.add_row("Compliant",      f"[{theme.success}]{passed}[/{theme.success}]")
    score_table.add_row("Non-Compliant", f"[{theme.error}]{failed}[/{theme.error}]")
    if skipped:
        score_table.add_row("Skip", f"[{theme.text_dim}]{skipped}[/{theme.text_dim}]")
    score_table.add_row("Total",   str(total))
    if skipped:
        # Score denominator is the evaluated set, so surface it explicitly.
        score_table.add_row("Evaluated", str(evaluated))
    score_table.add_row("Score",   f"[bold {score_color}]{pct}%[/bold {score_color}]")

    from rich.panel import Panel as _Panel
    console.print()
    console.print(_Panel(
        score_table,
        title=f"[bold {theme.primary_glow}]Audit Score[/bold {theme.primary_glow}]",
        border_style=score_color,
        box=_box.ROUNDED,
        expand=False,
    ))

    # ── Report file path (SCP-friendly) ─────────────────────────────
    console.print(f"\n[{theme.primary_glow}]Report file[/{theme.primary_glow}]")
    if output_path.exists():
        console.print(f"  [{theme.text_dim}]{'Report':<14}[/{theme.text_dim}]  {output_path.absolute()}")

    if not (hasattr(args, 'no_open') and args.no_open):
        if ui.maybe_open_html(output_path.as_uri()):
            console.print(f"\n  [{theme.text_dim}]Opened report in browser[/{theme.text_dim}]")
        else:
            console.print(
                f"\n  [{theme.text_dim}]Server environment detected — "
                f"open the report manually: {output_path}[/{theme.text_dim}]"
            )
    console.print()
    ui.next_step("platform-atlas", label="Audit Complete — View Dashboard")


@registry.register("session", "run", "report", description="Generate all reports from validation results")
def handle_session_run_report(args: Namespace) -> int:
    """Generate report.html — Compliance, Operational, and Architecture as
    top-bar pages in a single file, rendered client-side from the same
    viewmodel that powers the WebUI. Tier-aware: Standard/SaaS sessions have
    no Operational content (logs/MongoDB pipelines require Extended); the
    06 WebUI viewmodel is written for every tier.
    """
    try:
        manager = get_session_manager()

        if hasattr(args, 'session') and args.session:
            session = manager.get(args.session)
        else:
            session = manager.get_active()

        session_handler = attach_session_log(session.log_file)

        # Serialize concurrent runs against the same session — see capture
        # handler for rationale. Released in the finally below.
        _report_lock = session.exclusive_lock()
        _report_lock.__enter__()

        try:
            if not session.metadata.validation_completed:
                raise SessionError(
                    "Validation not complete",
                    details={"suggestion": "Run 'platform-atlas session run validate' first"}
                )

            if not session.validation_file.exists():
                raise SessionError(
                    "Validation file not found",
                    details={"expected": str(session.validation_file)}
                )

            import pandas as pd
            df = pd.read_parquet(session.validation_file, engine="pyarrow")
            _rehydrate_attrs(df, session)

            # Handle non-HTML export formats (unchanged behaviour)
            fmt = getattr(args, 'format', 'html')
            if fmt != 'html':
                export_path = session.directory / f"report.{fmt}"
                if fmt == 'csv':
                    df.to_csv(export_path, index=False)
                elif fmt in ('json', 'md'):
                    extended_results = _load_extended_results(df, session)
                    architecture_data = _load_architecture_data(session.metadata.environment, session.capture_file)
                    from platform_atlas.reporting.reporting_engine import (
                        export_json_report,
                        export_markdown_report,
                    )
                    if fmt == 'json':
                        _, schema_valid, schema_errors = export_json_report(
                            df, export_path,
                            extended_results=extended_results,
                            architecture_data=architecture_data,
                            session_name=session.name,
                            modules_ran=session.metadata.modules_ran,
                        )
                    else:
                        export_markdown_report(
                            df, export_path,
                            extended_results=extended_results,
                            architecture_data=architecture_data,
                            session_name=session.name,
                            modules_ran=session.metadata.modules_ran,
                        )
                else:
                    # Falling through here used to write nothing, then still
                    # mark the stage complete and announce a file that was
                    # never created. Unreachable from the CLI (argparse
                    # constrains --format), but reachable from any caller that
                    # builds its own args — so validate here too.
                    raise SessionError(
                        f"Unsupported report format: {fmt!r}",
                        details={"supported": "html, csv, json, md"},
                    )
                session.mark_stage_complete(SessionStage.REPORT)
                _cleanup_logs_file(session)
                _cleanup_rbac_file(session)
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Exported → {export_path}")
                if fmt == 'json':
                    if schema_valid:
                        console.print(f"[{theme.success}]✓[/{theme.success}] JSON schema validated")
                    else:
                        console.print(f"[{theme.warning}]⚠ JSON schema validation failed ({len(schema_errors)} issue(s)):[/{theme.warning}]")
                        for err in schema_errors:
                            console.print(f"  [{theme.text_dim}]• {err}[/{theme.text_dim}]")
                return 0

            console.print(f"[{theme.primary}]Generating reports for session:[/{theme.primary}] {session.name}\n")

            # ── Shared data ──────────────────────────────────────────────────
            extended_results = _load_extended_results(df, session)
            architecture_data = _load_architecture_data(session.metadata.environment, session.capture_file)
            rbac_data = _load_rbac_data(session.capture_file, extended_results, rbac_file=session.rbac_file)
            kubernetes_namespaces_data = _load_kubernetes_namespaces_data(session)

            config = ctx().config

            # Tier comes from the captured session, not the active config —
            # session tier is immutable once captured.
            session_tier = getattr(session.metadata, "tier", None) or df.attrs.get("tier") or "extended"

            try:
                _topo = config.topology
            except Exception:
                _topo = None
            _topo_mode = _topo.mode.value if _topo and getattr(_topo, "mode", None) else ""

            # ── report.html — Compliance, Operational, Architecture ─────────
            # One standalone HTML, rendered client-side from the same
            # viewmodel that powers the WebUI, so the numbers stay in
            # lockstep with it.
            from platform_atlas.reporting.webui_viewmodel import build_webui_viewmodel, write_webui_viewmodel
            from platform_atlas.reporting.unified_renderer import render_unified_report
            from platform_atlas.reporting.operational_engine import OperationalReport

            output_path = Path(args.output) if getattr(args, 'output', None) else session.report_file

            # MongoDB pipelines (Extended only) feed the Operational page.
            mongo_report = None
            if session_tier == "standard":
                console.print(
                    f"  [{theme.text_dim}]–[/{theme.text_dim}] "
                    f"Operational section not included (Standard tier — logs and MongoDB pipelines require Extended)"
                )
            elif session_tier == "saas":
                console.print(
                    f"  [{theme.text_dim}]–[/{theme.text_dim}] "
                    f"Operational section not included (SaaS tier — no Platform/MongoDB data in a gateway audit)"
                )
            elif session.operational_data_file.exists():
                mongo_report = OperationalReport.from_json(session.operational_data_file)

            if session_tier == "saas":
                console.print(
                    f"  [{theme.text_dim}]–[/{theme.text_dim}] "
                    f"Architecture Overview merged into the Compliance page (SaaS tier — single-report audit)"
                )

            viewmodel = build_webui_viewmodel(
                df,
                extended_results=extended_results,
                architecture_data=architecture_data,
                operational_report=mongo_report,
                rbac_data=rbac_data,
                kubernetes_namespaces_data=kubernetes_namespaces_data,
                session_name=session.name,
                modules_ran=session.metadata.modules_ran,
                tier=session_tier,
                platform_uri=config.platform_uri,
                deployment_mode=_topo_mode,
            )
            # --no-fixes parity: strip the knowledgebase fix steps.
            if getattr(args, "no_fixes", False):
                viewmodel.get("compliance", {})["fixes"] = {}

            render_unified_report(viewmodel, REPORT_TEMPLATE, output_path=output_path)
            console.print(f"  [{theme.success}]✓[/{theme.success}] Report → {output_path.name}")

            # ── 06_webui_viewmodel.json — WebUI tabbed experience ───────────
            # Typed JSON contract consumed by the WebUI. Built from the same
            # in-scope inputs as report.html so the numbers stay in lockstep.
            # Failure here never blocks the report — the WebUI route falls
            # back to building on the fly.
            try:
                write_webui_viewmodel(
                    session.webui_viewmodel_file,
                    df,
                    extended_results=extended_results,
                    architecture_data=architecture_data,
                    operational_report=mongo_report,
                    rbac_data=rbac_data,
                    kubernetes_namespaces_data=kubernetes_namespaces_data,
                    session_name=session.name,
                    modules_ran=session.metadata.modules_ran,
                    tier=session_tier,
                    platform_uri=ctx().config.platform_uri,
                    deployment_mode=_topo_mode,
                )
                console.print(f"  [{theme.success}]✓[/{theme.success}] WebUI viewmodel    → {session.webui_viewmodel_file.name}")
            except Exception as exc:  # noqa: BLE001 — never block reporting on viewmodel failure
                logger.warning("WebUI viewmodel write failed: %s", exc)
                console.print(
                    f"  [{theme.warning}]⚠[/{theme.warning}] WebUI viewmodel skipped ({exc}) — "
                    f"WebUI will rebuild on first request"
                )

            _emit_report_summary(session, df, output_path, args=args)
            return 0

        finally:
            try:
                _report_lock.__exit__(None, None, None)
            finally:
                detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1
    except Exception as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {type(e).__name__}: {e}")
        return 1

@registry.register("session", "run", "all", description="Run all capture stages within a session")
def handle_session_run_all(args: Namespace) -> int:
    """Run all capture stages within a session"""
    # --headless implies all skip/no-prompt flags
    if getattr(args, "headless", False):
        args.skip_architecture = True
        args.skip_guided = True
        args.no_open = True
        args.headless = True

    rc = handle_session_run_capture(args)
    if rc == 2:
        # User declined the capture prompt — stop cleanly without running validate/report
        return 0
    if rc != 0:
        return rc
    rc = handle_session_run_validate(args)
    if rc != 0:
        return rc
    return handle_session_run_report(args)

@registry.register("session", "list", description="List all audit sessions")
def handle_session_list(args: Namespace) -> int:
    """List all audit sessions"""
    from rich.table import Table
    from rich import box

    try:
        manager = get_session_manager()
        sessions = manager.list(limit=args.limit, sort_by=args.sort)

        if not sessions:
            console.print(f"[{theme.warning}]No sessions found[/{theme.warning}]")
            console.print(f"[{theme.text_dim}]Create one with: platform-atlas session create <name>[/{theme.text_dim}]")
            return 0

        active_name = manager.get_active_session_name()

        table = Table(
            title=f"Audit Sessions ({len(sessions)})",
            box=box.ROUNDED
        )
        table.add_column("", width=2)
        table.add_column("Name", style=theme.primary)
        table.add_column("Environment", style=theme.accent)
        table.add_column("Organization", style=theme.text_dim)
        table.add_column("Ruleset", style=theme.secondary)
        table.add_column("Profile", style=theme.text_dim)
        table.add_column("Status")
        table.add_column("Created", style="dim")
        table.add_column("Progress", justify="center")
        table.add_column("Results", justify="right")

        for session in sessions:
            # Active marker
            marker = "✓" if session.name == active_name else ""

            # Status with color
            status_colors = {
                "created":    theme.text_dim,
                "capturing":  theme.primary,
                "captured":   theme.info,
                "validating": theme.warning,
                "validated":  theme.success,
                "reported":   theme.success_glow,
                "failed":     theme.error,
                "aborted":    theme.warning,
            }
            status_style = status_colors.get(session.metadata.status.value, theme.text_dim)
            status_text = f"[{status_style}]{session.metadata.status.value}[/{status_style}]"

            # Created date
            created = session.metadata.created_at.strftime("%Y-%m-%d")

            # Environment, org, ruleset, profile
            env_display = session.metadata.environment or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            org_display = session.metadata.organization_name or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            ruleset_display = session.metadata.ruleset_id or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            profile_display = session.metadata.ruleset_profile or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"

            # Progress indicator
            stages = []
            if session.metadata.capture_completed:
                stages.append(f"[{theme.success}]C[/{theme.success}]")
            else:
                stages.append(f"[{theme.text_dim}]C[/{theme.text_dim}]")

            if session.metadata.validation_completed:
                stages.append(f"[{theme.success}]V[/{theme.success}]")
            else:
                stages.append(f"[{theme.text_dim}]V[/{theme.text_dim}]")

            if session.metadata.report_completed:
                stages.append(f"[{theme.success}]R[/{theme.success}]")
            else:
                stages.append(f"[{theme.text_dim}]R[/{theme.text_dim}]")

            progress = "".join(stages)

            # Results summary
            if session.metadata.validation_completed:
                results = f"{session.metadata.pass_count}✓ {session.metadata.fail_count}✗"
            else:
                results = "-"

            table.add_row(
                marker,
                session.name,
                env_display,
                org_display,
                ruleset_display,
                profile_display,
                status_text,
                created,
                progress,
                results
            )

        console.print(table)
        console.print(f"\n[{theme.text_dim}]Progress: [{theme.success}]C[/{theme.success}]=Capture [{theme.success}]V[/{theme.success}]=Validate [{theme.success}]R[/{theme.success}]=Report[/{theme.text_dim}]")

        if active_name:
            console.print(f"[{theme.text_dim}]Active session: {active_name}[/{theme.text_dim}]")

        return 0

    except Exception as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e}")
        return 1

@registry.register("session", "show", description="Show session details")
def handle_session_show(args: Namespace) -> int:
    """Show session details"""
    from rich.table import Table
    from rich import box

    try:
        manager = get_session_manager()

        # Get session name
        if args.session_name:
            session = manager.get(args.session_name)
        else:
            session = manager.get_active()

        # Session info table
        table = Table(
            title=f"Session: {session.name}",
            show_header=False,
            box=box.ROUNDED
        )
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Status", str(session.metadata.status))
        table.add_row("Created", session.metadata.created_at.strftime("%Y-%m-%d %H:%M UTC"))
        table.add_row("Updated", session.metadata.updated_at.strftime("%Y-%m-%d %H:%M UTC"))

        if session.metadata.description:
            table.add_row("Description", session.metadata.description)

        if session.metadata.organization_name:
            table.add_row("Organization", session.metadata.organization_name)

        if session.metadata.environment:
            table.add_row("Environment", session.metadata.environment)

        if session.metadata.target:
            table.add_row("Target", session.metadata.target)

        if session.metadata.ruleset_id:
            ruleset_display = f"{session.metadata.ruleset_id}"
            if session.metadata.ruleset_version:
                ruleset_display += f" v{session.metadata.ruleset_version}"
            table.add_row("Ruleset", ruleset_display)

        if session.metadata.ruleset_profile:
            table.add_row("Profile", session.metadata.ruleset_profile)

        # Editable indicator
        if session.metadata.is_editable:
            table.add_row("Editable", f"[{theme.success}]Yes[/{theme.success}] (bindings can be changed)")
        else:
            table.add_row("Editable", f"[{theme.text_dim}]Locked (capture started)[/{theme.text_dim}]")

        # Progress
        stages_complete = sum([
            session.metadata.capture_completed,
            session.metadata.validation_completed,
            session.metadata.report_completed
        ])
        table.add_row("Progress", f"{stages_complete}/3 stages complete")

        # Results
        if session.metadata.validation_completed:
            results = (
                f"[{theme.success}]{session.metadata.pass_count} passed[/{theme.success}], "
                f"[{theme.error}]{session.metadata.fail_count} failed[/{theme.error}], "
                f"[{theme.text_dim}]{session.metadata.skip_count} skipped[/{theme.text_dim}]"
            )
            table.add_row("Results", results)

        # Location
        size_mb = session.get_size() / (1024 * 1024)
        table.add_row("Location", str(session.directory))
        table.add_row("Size", f"{size_mb:.2f} MB ({session.get_file_count()} files)")

        console.print(table)

        # Show files if requested
        if args.files:
            console.print("\n[bold]Files:[/bold]")
            for file in sorted(session.directory.iterdir()):
                if file.is_file():
                    size_kb = file.stat().st_size / 1024
                    console.print(f"  • {file.name} ({size_kb:.1f} KB)")

        # Wayfinding — show what to run next (same hint the dashboard/status give)
        label, cmd = session.metadata.next_step_label
        if cmd:
            console.print(
                f"\n  [{theme.accent}]→[/{theme.accent}] Next: {label}  "
                f"[bold {theme.primary}]{cmd}[/bold {theme.primary}]"
            )

        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1

@registry.register("session", "active", description="Show or set active session")
def handle_session_active(args: Namespace) -> int:
    """Show or set active session — restores full context (env, ruleset, profile)"""
    import questionary
    try:
        manager = get_session_manager()

        if args.session_name:
            session = manager.activate_session_context(args.session_name)
            _show_session_status(session)
        else:
            sessions = manager.list()
            if not sessions:
                console.print(f"\n  [{theme.warning}]No sessions found.[/{theme.warning}]")
                console.print(f"  [{theme.text_dim}]Run 'platform-atlas session create' to set one up.[/{theme.text_dim}]\n")
                return 0

            active_name = manager.get_active_session_name()
            choices = []
            for s in sessions:
                suffix = " (active)" if s.name == active_name else ""
                env_label = f"  [{s.metadata.environment}]" if s.metadata.environment else ""
                org_label = f"  ({s.metadata.organization_name})" if s.metadata.organization_name else ""
                label = f"{s.name}{env_label}{org_label} ({s.metadata.status.value}){suffix}"
                choices.append(questionary.Choice(title=label, value=s.name))

            selected = questionary.select(
                "Switch to session:",
                choices=choices,
                default=active_name if active_name else sessions[0].name,
                style=get_qstyle(),
            ).ask()

            if selected is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

            session = manager.activate_session_context(selected)
            _show_session_status(session)

        return 0

    except SessionError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1

@registry.register("session", "switch", description="Switch the active session")
def handle_session_switch(args: Namespace) -> int:
    """Switch the active session (alias for session active)"""
    return handle_session_active(args)


@registry.register("session", "export", description="Export session for delivery")
def handle_session_export(args: Namespace) -> int:
    """Export a session as a delivery archive for an Itential ER ticket."""
    try:
        manager = get_session_manager()

        session = _resolve_export_session(manager, args)
        if session is None:
            return 1  # cancelled, or nothing to export

        # --include-debug is the single gate for troubleshooting files
        # (session.log, 01_capture.json, debug.log). --no-redact is kept as a
        # silent alias for back-compat and folded in here.
        include_debug = (
            bool(getattr(args, "include_debug", False))
            or not getattr(args, "redact", True)
        )

        # Organization-aware archive naming: ATLAS-<org>-<session>-<date>.
        # Used for both the archive filename and the folder inside it.
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d")
        org_slug = _slugify(session.metadata.organization_name) or "Unknown-Org"
        base_name = f"ATLAS-{org_slug}-{session.name}-{timestamp}"

        if args.output:
            output_path = Path(args.output)
        else:
            output_path = Path(f"{base_name}.{args.format}")

        console.print(f"\n[{theme.primary}]Exporting session:[/{theme.primary}] {session.name}")

        # Generate report.json (same structured output as 'report --format
        # json') into a short-lived temp dir, then hand it to the packager.
        import tempfile
        with tempfile.TemporaryDirectory() as gen_dir:
            report_json_path = _generate_report_json(session, Path(gen_dir))
            splash_path = _generate_report_splash(
                session, Path(gen_dir), manager.EXPORT_SUBDIR
            )
            exported = manager.export(
                session.name,
                output_path,
                archive_format=args.format,
                include_debug=include_debug,
                report_json_path=report_json_path,
                arc_dir_name=base_name,
                splash_path=splash_path,
            )
            report_json_included = report_json_path is not None

        from platform_atlas.core import architecture_store
        try:
            architecture_included = architecture_store.path_for(session.metadata.environment).exists()
        except ValueError:
            architecture_included = False

        _print_export_summary(
            session,
            exported,
            include_debug=include_debug,
            report_json_included=report_json_included,
            architecture_included=architecture_included,
        )
        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


def _slugify(text: str) -> str:
    """Sanitize a string for safe use in filenames and archive folder names."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip())
    return cleaned.strip("-")


def _resolve_export_session(manager, args):
    """Resolve which session to export.

    - Explicit name (``session export my-session``) → that session directly,
      even if it's too old to appear in the recent list.
    - Otherwise, interactively confirm the active session; on 'no' (or when
      there is no active session) offer the 10 most recent *reported*
      sessions to pick from with arrow-key navigation — export needs a
      report to bundle, so 'created'/'captured'/'validated' sessions aren't
      useful picks here.
    - Non-interactive shells fall back to the active session.

    Returns the resolved ``Session``, or ``None`` if the user cancelled.
    """
    if args.session_name:
        return manager.get(args.session_name)

    import sys as _sys
    import os as _os
    is_tty = _os.isatty(_sys.stdin.fileno()) if hasattr(_sys.stdin, "fileno") else False

    active_name = manager.get_active_session_name()

    if not is_tty:
        # No prompt available — use the active session (raises if none).
        return manager.get_active()

    # Confirm the current/active session first.
    if active_name and Confirm.ask(
        f"Export the current session '[{theme.primary}]{active_name}[/{theme.primary}]'?",
        default=True,
        console=console,
    ):
        return manager.get(active_name)

    # No active session, or the user declined → pick from the 10 most
    # recent sessions that actually have a report to export.
    recent = manager.list(limit=10, status_filter=SessionStatus.REPORTED)
    if not recent:
        console.print(f"\n  [{theme.warning}]No reported sessions found.[/{theme.warning}]\n")
        return None

    choices = [
        questionary.Choice(
            title=f"{s.name}  ({s.metadata.status.value}, {s.metadata.created_at:%Y-%m-%d})",
            value=s.name,
        )
        for s in recent
    ]
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice("Cancel Export", value="__cancel__"))

    selected = questionary.select(
        "Select a session to export:",
        choices=choices,
        style=get_qstyle(),
    ).ask()

    if selected in (None, "__cancel__"):  # Ctrl-C / Esc — questionary returns None
        console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
        return None
    return manager.get(selected)


def _generate_report_json(session, dest_dir: Path):
    """Generate ``report.json`` (identical to ``report --format json``) into
    ``dest_dir`` for bundling.

    Returns the path, or ``None`` when the session has no validation results
    to render (partial failure stays a successful export).
    """
    if not session.validation_file.exists():
        logger.info(
            "No validation results for '%s' — report.json omitted from export",
            session.name,
        )
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(session.validation_file, engine="pyarrow")
        _rehydrate_attrs(df, session)
        extended_results = _load_extended_results(df, session)
        architecture_data = _load_architecture_data(
            session.metadata.environment, session.capture_file
        )
        from platform_atlas.reporting.reporting_engine import export_json_report
        out_path = dest_dir / "report.json"
        export_json_report(
            df,
            out_path,
            extended_results=extended_results,
            architecture_data=architecture_data,
            session_name=session.name,
            modules_ran=session.metadata.modules_ran,
        )
        return out_path
    except Exception as e:
        logger.warning("Failed to generate report.json for export: %s", e)
        return None


def _generate_report_splash(session, dest_dir: Path, subdir: str):
    """Render the export splash / cover page (``REPORT.html``) into ``dest_dir``.

    The splash becomes the top-level landing page of the exported archive; its
    "Enter Report" link points into ``subdir/report.html``. Returns the
    path, or ``None`` when the session has no report to link to (a
    capture-only export skips the splash — ``export()`` guards on this too,
    so a failure here stays a successful export).
    """
    if not session.report_file.exists():
        return None
    try:
        from platform_atlas.reporting.report_renderer import render_splash_page
        from platform_atlas.core.paths import REPORT_SPLASH_TEMPLATE
        from platform_atlas.core._version import __version__
        meta = session.metadata
        out_path = dest_dir / "REPORT.html"
        render_splash_page(
            REPORT_SPLASH_TEMPLATE,
            out_path,
            organization_name=meta.organization_name or "Unknown Organization",
            session_name=session.name,
            tier=getattr(meta, "tier", "extended") or "extended",
            report_link=f"{subdir}/report.html",
            atlas_version=meta.atlas_version or __version__,
            timestamp=meta.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        )
        return out_path
    except Exception as e:
        logger.warning("Failed to generate splash page for export: %s", e)
        return None


def _print_export_summary(session, exported: Path, *,
                          include_debug: bool, report_json_included: bool,
                          architecture_included: bool = False) -> None:
    """Render the post-export Rich panel with delivery guidance."""
    from rich.panel import Panel
    from rich.table import Table
    from rich import box

    size_mb = exported.stat().st_size / (1024 * 1024)
    dash = f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
    org = session.metadata.organization_name or dash
    env = session.metadata.environment or dash

    # Accurate contents summary, derived from what actually exists / was bundled.
    contents = []
    if session.report_file.exists():
        contents.append("Report")
    if report_json_included:
        contents.append("report.json")
    contents.append("metadata")
    if architecture_included:
        contents.append("architecture")
    if include_debug:
        contents.append("debug (logs + raw capture)")

    detail = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    detail.add_column(style=theme.text_dim, min_width=13)
    detail.add_column(overflow="fold")
    detail.add_row("Organization", org)
    detail.add_row("Session", session.name)
    detail.add_row("Environment", env)
    detail.add_row("Contents", " · ".join(contents))
    detail.add_row("Archive", str(exported.absolute()))
    detail.add_row("Size", f"{size_mb:.2f} MB")
    # Point the user at the splash cover page that fronts the bundle.
    if session.report_file.exists():
        detail.add_row("Open", "REPORT.html (cover page, inside the archive)")

    body = Table.grid(padding=0)
    body.add_row(detail)
    body.add_row("")
    body.add_row(
        f"[bold {theme.primary_glow}]Attach this archive to your Itential "
        f"Enablement Request (ER) ticket[/bold {theme.primary_glow}]"
    )
    body.add_row(
        f"[{theme.text_dim}]so the Itential team can review your audit results.[/{theme.text_dim}]"
    )

    console.print()
    console.print(Panel(
        body,
        title=f"[bold {theme.success}]✓ Session Exported[/bold {theme.success}]",
        border_style=theme.success,
        box=box.ROUNDED,
        expand=False,
    ))

@registry.register("session", "delete", description="Delete an audit session")
def handle_session_delete(args: Namespace) -> int:
    """Delete an audit session"""
    try:
        manager = get_session_manager()
        target = args.session_name

        if target is None:
            sessions = manager.list()
            if not sessions:
                console.print(f"\n  [{theme.warning}]No sessions found.[/{theme.warning}]\n")
                return 0

            choices = [
                questionary.Choice(
                    title=f"{s.name}  ({s.metadata.status.value})",
                    value=s.name,
                )
                for s in sessions
            ]

            target = questionary.select(
                "Select session to delete:",
                choices=choices,
                style=get_qstyle(),
            ).ask()

            if target is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

        session = manager.get(target)

        # Confirm unless force
        if not args.force:
            console.print(f"[{theme.warning}]⚠ This will permanently delete:[/{theme.warning}]")
            console.print(f"  Session: {session.name}")
            console.print(f"  Location: {session.directory}")
            console.print(f"  Files: {session.get_file_count()}")

            if not Confirm.ask("Continue?", default=False):
                console.print("Cancelled")
                return 0

        # Delete
        manager.delete(target, force=args.force)
        console.print(f"[{theme.success}]✓[/{theme.success}] Deleted session: {target}")

        return 0

    except SessionError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1

@registry.register("session", "diff", description="Compare two sessions")
def handle_session_diff(args: Namespace) -> int:
    """Compare two sessions"""
    from platform_atlas.reporting.diff_engine import diff_reports, render_diff_report
    try:
        manager = get_session_manager()

        baseline_name = args.baseline_session
        latest_name = args.latest_session

        # Interactive picker when no arguments provided
        if baseline_name is None or latest_name is None:
            # Only fully-reported sessions are eligible. A diff compares
            # validation results, so created / captured / validated-only
            # sessions can't be diffed — listing them just clutters the picker.
            # Cross-environment diffs aren't meaningful, so the picker is also
            # scoped to the active environment (when one is set).
            current_env = ctx().config.active_environment
            sessions = [s for s in manager.list() if s.metadata.report_completed]
            if current_env:
                sessions = [s for s in sessions if (s.metadata.environment or "") == current_env]
            if len(sessions) < 2:
                console.print(
                    f"\n  [{theme.warning}]Need at least 2 reported sessions"
                    f"{f' for environment {current_env!r}' if current_env else ''} to compare.[/{theme.warning}]"
                )
                console.print(
                    f"  [{theme.text_dim}]Only sessions that have completed capture, validation, and "
                    f"reporting — in the same environment — can be diffed.[/{theme.text_dim}]\n"
                )
                return 1

            choices = [
                questionary.Choice(
                    title=f"{s.name}  ({s.metadata.environment or '—'} · {s.metadata.created_at:%Y-%m-%d})",
                    value=s.name,
                )
                for s in sessions
            ]

            if baseline_name is None:
                baseline_name = questionary.select(
                    "Select baseline session:",
                    choices=choices,
                    style=get_qstyle(),
                ).ask()

                if baseline_name is None:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 1

            if latest_name is None:
                remaining = [c for c in choices if c.value != baseline_name]
                latest_name = questionary.select(
                    "Select latest session:",
                    choices=remaining,
                    style=get_qstyle(),
                ).ask()

                if latest_name is None:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 1

        # Get both sessions
        baseline_session = manager.get(baseline_name)
        latest_session = manager.get(latest_name)

        # Diffing across environments isn't meaningful — enforce this even when
        # session names are passed explicitly, not just in the interactive picker.
        baseline_env = baseline_session.metadata.environment or ""
        latest_env = latest_session.metadata.environment or ""
        if baseline_env != latest_env:
            raise SessionError(
                f"Cannot diff sessions from different environments: "
                f"'{baseline_session.name}' ({baseline_env or '—'}) vs "
                f"'{latest_session.name}' ({latest_env or '—'})"
            )

        # Check both have validation results
        if not baseline_session.validation_file.exists():
            raise SessionError(
                f"Baseline session has no validation results: {baseline_session.name}"
            )

        if not latest_session.validation_file.exists():
            raise SessionError(
                f"Latest session has no validation results: {latest_session.name}"
            )

        # Load validation DataFrames
        import json
        import pandas as pd

        baseline_df = pd.read_parquet(baseline_session.validation_file, engine="pyarrow")
        latest_df = pd.read_parquet(latest_session.validation_file, engine="pyarrow")

        # Parquet doesn't preserve df.attrs — rehydrate from capture JSON
        for df, session in [(baseline_df, baseline_session), (latest_df, latest_session)]:
            _rehydrate_attrs(df, session)

        # Generate diff
        console.print(f"[{theme.primary}]Comparing sessions...[/{theme.primary}]")
        console.print(f"  Baseline: {baseline_session.name}")
        console.print(f"  Latest: {latest_session.name}\n")

        diff_df = diff_reports(baseline_df, latest_df)

        # Attach session-level metadata for the diff template
        diff_df.attrs["baseline_name"] = baseline_session.name
        diff_df.attrs["baseline_date"] = baseline_df.attrs.get("captured_at", "")
        diff_df.attrs["current_name"] = latest_session.name
        diff_df.attrs["current_date"] = latest_df.attrs.get("captured_at", "")
        diff_df.attrs["organization_name"] = (
            latest_df.attrs.get("organization_name")
            or baseline_df.attrs.get("organization_name")
            or ""
        )

        # Determine output path
        if args.output:
            output_path = Path(args.output)
        else:
            ATLAS_HOME_DIFF.mkdir(parents=True, exist_ok=True)
            output_path = ATLAS_HOME_DIFF / f"ATLAS-diff-{baseline_session.name}-vs-{latest_session.name}.html"

        # Render diff report
        render_diff_report(
            diff_df,
            DIFF_TEMPLATE,
            output_path=output_path,
            title="Configuration Change Report",
            subtitle=f"{baseline_session.name} → {latest_session.name}"
        )

        console.print(f"[{theme.success}]✓[/{theme.success}] Diff report generated")
        console.print(f"  Location: {output_path}")

        # Auto-open if requested
        if not args.no_open:
            if ui.maybe_open_html(output_path.as_uri()):
                console.print(f"  [{theme.text_dim}]Opened diff report in browser[/{theme.text_dim}]")
            else:
                console.print(
                    f"  [{theme.text_dim}]Server environment detected — "
                    f"open the diff report manually: {output_path}[/{theme.text_dim}]"
                )

        return 0

    except SessionError as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


# Normal pipeline progression, lowest first. Used by `session repair` to
# decide whether a status may be advanced to match the artifacts on disk.
_STATUS_RANK = {
    "created": 0, "capturing": 1, "captured": 2,
    "validating": 3, "validated": 4, "reported": 5,
}
# Deliberate end states — never re-derived from files on disk.
_TERMINAL_STATUS = {"failed", "aborted", "archived"}


@registry.register("session", "repair", description="Backfill missing metadata on older sessions")
def handle_session_repair(args: Namespace) -> int:
    """
    Scan sessions and backfill missing metadata from capture JSON files.

    For sessions created before v1.5 (before session binding), this reads
    the _atlas.metadata block from the capture file and fills in:
      - organization_name
      - environment
      - ruleset_id
      - ruleset_profile

    It also reconciles the pipeline stage flags against the artifacts that
    actually exist in the session directory, so a run interrupted between
    writing its output and saving session.json can be recovered.

    Safe to run multiple times — only fills in blank fields, never
    overwrites existing values.
    """
    import json as _json

    try:
        manager = get_session_manager()
        target_name = getattr(args, "session_name", None)
        dry_run = getattr(args, "dry_run", False)

        if target_name:
            sessions = [manager.get(target_name)]
        else:
            sessions = manager.list()

        if not sessions:
            console.print(f"\n  [{theme.text_dim}]No sessions found.[/{theme.text_dim}]\n")
            return 0

        if dry_run:
            console.print(f"\n  [{theme.warning}]Dry run — no files will be modified[/{theme.warning}]\n")

        repaired = 0
        skipped = 0

        for session in sessions:
            meta = session.metadata
            changes: list[str] = []

            # Only process sessions that have capture data
            if not session.capture_file.exists():
                skipped += 1
                continue

            # Read the capture JSON metadata block
            try:
                with open(session.capture_file, "r", encoding="utf-8") as f:
                    capture = _json.load(f)
                atlas_meta = capture.get("_atlas", {}).get("metadata", {})
            except Exception as e:
                console.print(
                    f"  [{theme.text_dim}]⊘ {session.name} — could not read capture file: {e}[/{theme.text_dim}]"
                )
                skipped += 1
                continue

            # Backfill each field only if currently blank
            if not meta.organization_name and atlas_meta.get("organization_name"):
                changes.append(f"organization_name = {atlas_meta['organization_name']}")
                if not dry_run:
                    meta.organization_name = atlas_meta["organization_name"]

            if not meta.environment and atlas_meta.get("environment"):
                changes.append(f"environment = {atlas_meta['environment']}")
                if not dry_run:
                    meta.environment = atlas_meta["environment"]

            if not meta.ruleset_id and atlas_meta.get("ruleset_id"):
                changes.append(f"ruleset_id = {atlas_meta['ruleset_id']}")
                if not dry_run:
                    meta.ruleset_id = atlas_meta["ruleset_id"]

            if not meta.ruleset_version and atlas_meta.get("ruleset_version"):
                changes.append(f"ruleset_version = {atlas_meta['ruleset_version']}")
                if not dry_run:
                    meta.ruleset_version = atlas_meta["ruleset_version"]

            if not meta.ruleset_profile and atlas_meta.get("ruleset_profile"):
                changes.append(f"ruleset_profile = {atlas_meta['ruleset_profile']}")
                if not dry_run:
                    meta.ruleset_profile = atlas_meta["ruleset_profile"]

            # ── Reconcile stage flags with the artifacts on disk ──
            # A stage killed between writing its output file and saving
            # session.json leaves the flag false, and every later command
            # then refuses to run ("Capture not complete") with no way
            # back. The files that exist are the truth.
            reached: str = ""
            for flag, file_field, artifact, status in (
                ("capture_completed",    "capture_file",    session.capture_file,    "captured"),
                ("validation_completed", "validation_file", session.validation_file, "validated"),
                ("report_completed",     "report_file",     session.report_file,     "reported"),
            ):
                if not artifact.exists():
                    continue
                reached = status
                if getattr(meta, flag):
                    continue
                changes.append(f"{flag} = True")
                if not dry_run:
                    setattr(meta, flag, True)
                    setattr(meta, file_field, artifact.name)

            current = str(meta.status)
            if (
                reached
                and current not in _TERMINAL_STATUS
                and _STATUS_RANK.get(current, 0) < _STATUS_RANK[reached]
            ):
                changes.append(f"status = {reached}")
                if not dry_run:
                    meta.status = SessionStatus(reached)

            if changes:
                repaired += 1
                verb = "Would update" if dry_run else "Updated"
                console.print(
                    f"  [{theme.success}]✓[/{theme.success}] {verb} [bold]{session.name}[/bold]"
                )
                for change in changes:
                    console.print(f"      [{theme.text_dim}]{change}[/{theme.text_dim}]")

                if not dry_run:
                    session.save_metadata()
            else:
                skipped += 1

        # Summary
        console.print()
        if repaired:
            verb = "would be repaired" if dry_run else "repaired"
            console.print(
                f"  [{theme.success}]✓[/{theme.success}] {repaired} session(s) {verb}, "
                f"{skipped} already complete"
            )
        else:
            console.print(
                f"  [{theme.text_dim}]All {skipped} session(s) already have complete metadata[/{theme.text_dim}]"
            )
        console.print()

        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


@registry.register("session", "prune", description="Prune old sessions matching filter criteria")
def handle_session_prune(args: Namespace) -> int:
    """
    Bulk-delete sessions matching the filter criteria.

    Filters AND together. Dry-run is the default — pass --no-dry-run to delete.
    The active session is always skipped even if it matches.
    """
    from datetime import datetime, timezone, timedelta
    from rich.table import Table
    from rich import box
    import questionary

    try:
        manager = get_session_manager()
        older_than_secs: int = args.older_than      # duration in seconds from argparse
        keep_last: int | None = getattr(args, "keep_last", None)
        prune_status: str | None = getattr(args, "prune_status", None)
        prune_env: str | None = getattr(args, "prune_env", None)
        dry_run: bool = getattr(args, "dry_run", True)
        yes: bool = getattr(args, "yes", False)

        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_secs)
        active_name = manager.get_active_session_name()

        all_sessions = manager.list()

        # Apply --keep-last: sort by updated_at descending, mark the first N as keepers
        keep_names: set[str] = set()
        if keep_last is not None and keep_last > 0:
            sorted_sessions = sorted(
                all_sessions,
                key=lambda s: (s.metadata.updated_at or s.metadata.created_at),
                reverse=True,
            )
            keep_names = {s.name for s in sorted_sessions[:keep_last]}

        _STATUS_MAP = {
            "ok": "reported",
            "warn": "reported",
            "fail": "failed",
            "aborted": "aborted",
            "empty": "created",
        }

        candidates = []
        skipped_active = False

        for session in all_sessions:
            if session.name == active_name:
                skipped_active = True
                continue
            if session.name in keep_names:
                continue

            # Age filter
            ts = session.metadata.updated_at or session.metadata.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                continue

            # Env filter
            if prune_env and session.metadata.environment != prune_env:
                continue

            # Status filter
            if prune_status:
                sess_status = str(session.metadata.status)
                if prune_status == "ok":
                    # "ok" = reported with no failures (we can't tell from status alone, map to reported)
                    if sess_status != "reported":
                        continue
                elif prune_status == "empty":
                    if sess_status not in ("created",):
                        continue
                else:
                    if sess_status != _STATUS_MAP.get(prune_status, prune_status):
                        continue

            candidates.append(session)

        if not candidates:
            console.print(f"\n  [{theme.text_dim}]No sessions matched the filter.[/{theme.text_dim}]\n")
            if skipped_active:
                console.print(
                    f"  [{theme.warning}]Note: active session '{active_name}' "
                    f"was excluded from the filter.[/{theme.warning}]\n"
                )
            return 0

        # Compute total size
        total_bytes = sum(s.get_size() for s in candidates)
        total_mb = total_bytes / (1024 * 1024)

        # Build preview table (cap at 10, show "N more")
        cap = 10
        table = Table(
            title=f"{'[dim]Dry run — [/dim]' if dry_run else ''}Sessions to prune ({len(candidates)})",
            box=box.ROUNDED,
        )
        table.add_column("Name", style=theme.primary)
        table.add_column("Environment", style=theme.accent)
        table.add_column("Last Activity", style="dim")
        table.add_column("Status", style=theme.warning)
        table.add_column("Size", justify="right", style="dim")

        today = datetime.now(tz=timezone.utc)
        for session in candidates[:cap]:
            ts = session.metadata.updated_at or session.metadata.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (today - ts).days
            env_display = session.metadata.environment or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            size_kb = session.get_size() / 1024
            size_display = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            table.add_row(
                session.name,
                env_display,
                f"{ts.strftime('%Y-%m-%d')} ({age_days}d ago)",
                str(session.metadata.status),
                size_display,
            )

        console.print()
        console.print(table)

        if len(candidates) > cap:
            console.print(
                f"  [{theme.text_dim}]… {len(candidates) - cap} more not shown[/{theme.text_dim}]"
            )

        console.print(
            f"\n  {len(candidates)} session(s) matched · "
            f"{total_mb:.1f} MB total"
        )

        if skipped_active:
            console.print(
                f"  [{theme.warning}]Note: active session '{active_name}' was excluded.[/{theme.warning}]"
            )

        if dry_run:
            if len(candidates) > cap:
                _show_all = questionary.confirm(
                    f"Show all {len(candidates)} sessions?",
                    default=False,
                    style=get_qstyle(),
                ).ask()
                if _show_all is None:
                    raise KeyboardInterrupt
                if _show_all:
                    all_table = Table(
                        title=f"All {len(candidates)} sessions to prune",
                        box=box.ROUNDED,
                    )
                    all_table.add_column("Name", style=theme.primary)
                    all_table.add_column("Environment", style=theme.accent)
                    all_table.add_column("Last Activity", style="dim")
                    all_table.add_column("Status", style=theme.warning)
                    all_table.add_column("Size", justify="right", style="dim")
                    for session in candidates:
                        ts2 = session.metadata.updated_at or session.metadata.created_at
                        if ts2.tzinfo is None:
                            ts2 = ts2.replace(tzinfo=timezone.utc)
                        age_days2 = (today - ts2).days
                        size_kb2 = session.get_size() / 1024
                        size_display2 = f"{size_kb2:.0f} KB" if size_kb2 < 1024 else f"{size_kb2 / 1024:.1f} MB"
                        all_table.add_row(
                            session.name,
                            session.metadata.environment or "—",
                            f"{ts2.strftime('%Y-%m-%d')} ({age_days2}d ago)",
                            str(session.metadata.status),
                            size_display2,
                        )
                    console.print()
                    console.print(all_table)
            console.print(
                f"\n  [{theme.text_dim}]Dry run — nothing deleted. "
                f"Pass --no-dry-run to delete.[/{theme.text_dim}]\n"
            )
            return 0

        # Confirm unless --yes
        if not yes:
            console.print()
            _confirm_choices = [
                questionary.Choice("Yes, delete these sessions", value="yes"),
                questionary.Choice("Show all sessions first", value="show_all"),
                questionary.Choice("Cancel", value="cancel"),
            ]
            _confirm = questionary.select(
                f"Run for real? Delete {len(candidates)} session(s)?",
                choices=_confirm_choices,
                style=get_qstyle(),
            ).ask()
            if _confirm is None or _confirm == "cancel":
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 0
            if _confirm == "show_all":
                # Show full table without cap
                all_table = Table(title=f"All {len(candidates)} sessions to prune", box=box.ROUNDED)
                all_table.add_column("Name", style=theme.primary)
                all_table.add_column("Environment", style=theme.accent)
                all_table.add_column("Last Activity", style="dim")
                all_table.add_column("Status", style=theme.warning)
                for session in candidates:
                    ts2 = session.metadata.updated_at or session.metadata.created_at
                    if ts2.tzinfo is None:
                        ts2 = ts2.replace(tzinfo=timezone.utc)
                    age_days2 = (today - ts2).days
                    all_table.add_row(
                        session.name,
                        session.metadata.environment or "—",
                        f"{ts2.strftime('%Y-%m-%d')} ({age_days2}d ago)",
                        str(session.metadata.status),
                    )
                console.print()
                console.print(all_table)
                _delete_confirm = questionary.confirm(
                    f"Delete all {len(candidates)} session(s)?", default=False, style=get_qstyle()
                ).ask()
                if _delete_confirm is None:
                    raise KeyboardInterrupt
                if not _delete_confirm:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 0

        # Delete
        deleted = 0
        failed_count = 0
        for session in candidates:
            try:
                manager.delete(session.name, force=True)
                console.print(f"  [{theme.success}]✓[/{theme.success}] Deleted: {session.name}")
                deleted += 1
            except SessionError as e:
                console.print(f"  [{theme.error}]✗[/{theme.error}] {session.name}: {e.message}")
                failed_count += 1

        console.print()
        console.print(
            f"  [{theme.success}]✓[/{theme.success}] {deleted} session(s) deleted"
            + (f", {failed_count} failed" if failed_count else "")
        )
        console.print()
        return 0 if not failed_count else 1

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e.message}")
        return 1


# =================================================
# Shared Helpers
# =================================================

def _prompt_operational_choice() -> tuple[bool, list[str] | None]:
    """Ask up front whether (and which) MongoDB operational pipelines to run.

    This is the user-input half only — the pipelines themselves run later,
    after capture completes, via :func:`_collect_operational_pipelines`, so the
    capture is never interrupted. Returns ``(should_run, pipeline_names)``:

    - ``(False, None)`` — user declined; nothing to run.
    - ``(True, None)``  — run every pipeline in ``~/.atlas/pipelines/``.
    - ``(True, [names])`` — run only the selected pipelines.
    """
    from rich.panel import Panel
    from platform_atlas.capture.utils import discover_pipelines
    from platform_atlas.core.paths import ATLAS_PIPELINES_DIR

    console.print()
    console.print(Panel(
        f"[bold white]Optional: MongoDB Operational Pipelines[/bold white]\n\n"
        f"Atlas can run MongoDB aggregation pipelines against your environment "
        f"to collect operational metrics (top workflows, adapter activity, error rates, etc.).\n\n"
        f"[{theme.text_dim}]This requires a live MongoDB connection and may take 30–60 seconds. "
        f"You choose now; the pipelines run after capture completes.[/{theme.text_dim}]",
        title=f"[bold {theme.primary}]Operational Report Data Collection[/bold {theme.primary}]",
        border_style=theme.primary,
        padding=(1, 2),
    ))

    run_now = questionary.confirm(
        "Collect MongoDB operational pipelines for this capture?",
        default=False,
        style=get_qstyle(),
    ).ask()

    if run_now is None:
        raise KeyboardInterrupt
    if not run_now:
        console.print(
            f"  [{theme.text_dim}]Skipped — the Operational Report will be generated "
            f"without MongoDB data.[/{theme.text_dim}]"
        )
        return (False, None)

    # Discover pipelines so the user can choose which ones to run.
    available = discover_pipelines(ATLAS_PIPELINES_DIR)

    # With 0 or 1 pipeline there's nothing meaningful to choose between — run all.
    if len(available) <= 1:
        return (True, None)

    console.print()
    mode = questionary.select(
        f"Found {len(available)} pipelines. Which would you like to run?",
        choices=[
            questionary.Choice(
                title=f"Run all ({len(available)} pipelines)",
                value="all",
            ),
            questionary.Choice(
                title="Select specific pipelines…",
                value="select",
            ),
        ],
        style=get_qstyle(),
    ).ask()

    if mode is None:
        raise KeyboardInterrupt

    if mode == "all":
        return (True, None)

    # Build a checkbox list — all pre-checked so the user only needs to
    # uncheck the ones they want to skip.
    choices = []
    for p in available:
        label = p.name
        if p.collection:
            label += f"  [{p.collection}]"
        if p.desc:
            label += f"  — {p.desc}"
        choices.append(questionary.Choice(title=label, value=p.name, checked=False))

    selected = questionary.checkbox(
        "Select pipelines to run  (Space = toggle, Enter = confirm):",
        choices=choices,
        style=get_qstyle(),
    ).ask()

    if selected is None:
        raise KeyboardInterrupt

    if not selected:
        console.print(
            f"  [{theme.text_dim}]No pipelines selected — skipped.[/{theme.text_dim}]"
        )
        return (False, None)

    return (True, selected)


def _collect_operational_pipelines(
    session,
    pipeline_names: list[str] | None = None,
) -> None:
    """Run MongoDB aggregation pipelines and save results to 04_operational.json.

    Progress runs under a single live spinner and the outcome is rendered in a
    Rich panel, so the operational step reads as a framed part of the capture
    flow rather than loose lines. When ``pipeline_names`` is provided only those
    pipelines are executed; pass ``None`` to run every pipeline in
    ~/.atlas/pipelines/.
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from platform_atlas.capture.collectors.mongo import MongoCollector
    from platform_atlas.reporting.operational_engine import run_operational_pipelines

    title = f"[bold {theme.primary}]MongoDB Operational Pipelines[/bold {theme.primary}]"

    def _notice_panel(message: str, *, border: str) -> None:
        console.print()
        console.print(Panel(
            message, title=title, border_style=border,
            box=box.ROUNDED, padding=(1, 2), expand=False,
        ))

    collector = MongoCollector.from_config()
    if collector is None:
        _notice_panel(
            f"[{theme.warning}]No MongoDB URI configured — skipping operational "
            f"pipeline collection.[/{theme.warning}]",
            border=theme.warning,
        )
        return

    scope = f"{len(pipeline_names)} selected" if pipeline_names else "all"
    try:
        # Suppress the engine's own per-pipeline streaming (use_console=False)
        # and surface progress through one live spinner instead, so everything
        # lands as a single framed step.
        with console.status(
            f"[{theme.primary}]Running MongoDB operational pipelines ({scope})…[/{theme.primary}]",
            spinner="dots",
        ) as status:
            def _on_start(idx: int, total: int, pipeline) -> None:
                status.update(
                    f"[{theme.primary}]Operational pipelines[/{theme.primary}]  "
                    f"[{theme.text_dim}]({idx}/{total})[/{theme.text_dim}]  "
                    f"[bold]{pipeline.name}[/bold] "
                    f"[{theme.text_dim}]→ {pipeline.collection}[/{theme.text_dim}]"
                )
            with collector:
                report = run_operational_pipelines(
                    collector,
                    pipeline_names=pipeline_names,
                    use_console=False,
                    on_pipeline_start=_on_start,
                )
    except Exception as e:
        logger.debug("Operational pipeline collection failed: %s", e, exc_info=True)
        _notice_panel(
            f"[{theme.warning}]Operational pipeline collection failed: {e}[/{theme.warning}]",
            border=theme.warning,
        )
        return

    if report.pipeline_count == 0:
        _notice_panel(
            f"[{theme.warning}]No pipeline files found in ~/.atlas/pipelines/[/{theme.warning}]",
            border=theme.warning,
        )
        return

    report.to_json(session.operational_data_file)

    # Per-pipeline results table, rendered inside the panel.
    table = Table(box=box.SIMPLE, show_header=True, pad_edge=False, expand=False)
    table.add_column("Pipeline")
    table.add_column("Collection", style=theme.text_dim)
    table.add_column("Rows", justify="right")
    table.add_column("Status")
    for r in report.results:
        if r.succeeded:
            rows_cell = f"{r.row_count:,}"
            status_cell = (
                f"[{theme.success}]✓[/{theme.success}] "
                f"[{theme.text_dim}]{r.duration_ms / 1000:.1f}s[/{theme.text_dim}]"
            )
        else:
            rows_cell = f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            status_cell = (
                f"[{theme.error}]✗[/{theme.error}] "
                f"[{theme.text_dim}]{r.error or 'failed'}[/{theme.text_dim}]"
            )
        table.add_row(r.name, r.collection, rows_cell, status_cell)

    all_ok = report.success_count == report.pipeline_count and not report.cancelled
    summary_color = theme.success if all_ok else theme.warning
    summary = (
        f"[{summary_color}]{report.success_count}/{report.pipeline_count} "
        f"pipelines succeeded[/{summary_color}]"
        f"  [{theme.text_dim}]·[/{theme.text_dim}]  {report.total_rows:,} rows"
    )
    if report.cancelled:
        summary += f"  [{theme.warning}](cancelled — partial results kept)[/{theme.warning}]"

    body = Table.grid(padding=0)
    body.add_row(table)
    body.add_row("")
    body.add_row(summary)
    body.add_row(f"[{theme.text_dim}]Saved to: {session.operational_data_file}[/{theme.text_dim}]")

    console.print()
    console.print(Panel(
        body, title=title,
        border_style=theme.success if report.success_count else theme.warning,
        box=box.ROUNDED, padding=(1, 2), expand=False,
    ))


def _load_extended_results(df, session) -> list:
    """
    Return extended validation results.

    Tries df.attrs first (available when validate + report run in the same process).
    Falls back to re-running extended validation from captured files if attrs are empty.
    """
    import json as _json
    results = df.attrs.get('extended_results', [])
    if results:
        return results

    # Re-run extended validation from stored files
    if not session.capture_file.exists():
        return []
    try:
        from platform_atlas.validation.extended_validation import run_extended_validation
        from platform_atlas.core.json_utils import load_json

        capture_data = load_json(session.capture_file)

        if session.logs_file.exists():
            logs_data = _json.loads(session.logs_file.read_text(encoding="utf-8"))
            platform = capture_data.setdefault("platform", {})
            if "log_analysis" in logs_data:
                platform["log_analysis"] = logs_data["log_analysis"]
            if "webserver_logs" in logs_data:
                platform["webserver_logs"] = logs_data["webserver_logs"]
            if "mongo_log_analysis" in logs_data:
                mongo = capture_data.setdefault("mongo", {})
                mongo["log_analysis"] = logs_data["mongo_log_analysis"]

        if session.rbac_file.exists():
            capture_data["authorization"] = load_json(session.rbac_file)

        check_results = run_extended_validation(capture_data)
        extended = [r.to_dict() for r in check_results]
        logger.debug("Re-ran extended validation for report generation: %d results", len(extended))
        return extended
    except Exception as e:
        logger.warning("Failed to re-run extended validation: %s", e)
        return []


def _load_architecture_data(environment: str = "", capture_file=None) -> dict:
    """Load architecture overview data for ``environment``.

    Resolution order:
      1. Per-env architecture store (``~/.atlas/architecture/<env>.json``) — authoritative,
         reflects any post-capture edits the user made via the form.
      2. ``01_capture.json`` — fallback for sessions whose environment has no store file
         yet (e.g., a new env that hasn't had the architecture form filled in).

    Empty ``environment`` falls back to the ``_default`` bucket (matches
    architecture_store's resolution). Includes partial data — any completed
    sections are used in the report even if the form is not yet marked
    status='complete'.
    """
    try:
        from platform_atlas.capture.collectors.manual import ArchitectureProgress
        progress = ArchitectureProgress.load(environment)
        if progress.completed:
            return progress.completed
    except Exception as e:
        logger.debug("Could not load architecture data for env=%s: %s",
                     environment or "_default", e)

    # Fallback: read architecture data captured during session capture
    if capture_file is not None:
        try:
            import json as _json
            from pathlib import Path as _Path
            cap_path = _Path(capture_file)
            if cap_path.exists():
                cap = _json.loads(cap_path.read_text(encoding="utf-8"))
                arch = cap.get("checks", {}).get("architecture_validation")
                if arch:
                    logger.debug(
                        "Architecture data for env=%s loaded from capture file (no store file for env)",
                        environment or "_default",
                    )
                    return arch
        except Exception as fb_exc:
            logger.debug("Architecture fallback from capture file failed: %s", fb_exc)

    return {}


def _load_kubernetes_namespaces_data(session) -> dict:
    """Load additional-Kubernetes-namespace validation results, if any.

    Written by handle_session_run_validate as its own JSON file (not
    DataFrame.attrs — those don't survive the Parquet round-trip). Absent
    for the common single-namespace case, so this returns {} and the report
    section is simply omitted."""
    ns_file = session.kubernetes_namespaces_file
    if not ns_file.exists():
        return {}
    try:
        import json as _json
        return _json.loads(ns_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not load Kubernetes namespaces data: %s", e)
        return {}


def _load_rbac_data(capture_file, extended_results: list | None = None, rbac_file=None) -> dict:
    """Load the RBAC authorization summary for report rendering.

    Fast path: the ``rbac_authorization`` extended validation check stores its
    full summary in ``details`` — extract it directly from ``extended_results``
    when available (no file I/O needed).

    Fallback: reads ``01_capture_rbac.json`` (the dedicated RBAC sidecar) and
    calls ``build_rbac_summary`` directly.  Falls back to ``capture_file`` for
    sessions captured before the sidecar was introduced (the ``authorization``
    key may still be embedded in the main capture in those older sessions).

    Returns ``{}`` when RBAC collection was not enabled or data is absent.
    """
    # Fast path — already computed by the extended validation check
    for result in (extended_results or []):
        if isinstance(result, dict) and result.get("check_id") == "rbac_authorization":
            details = result.get("details") or {}
            if details:
                return details
            break

    # Fallback — recompute from the RBAC sidecar file
    from platform_atlas.core.json_utils import load_json
    from platform_atlas.validation.rbac_engine import build_rbac_summary

    auth_data: dict = {}
    if rbac_file and rbac_file.exists():
        try:
            auth_data = load_json(rbac_file)
        except Exception as exc:
            logger.warning("Failed to read RBAC sidecar file %s: %s", rbac_file, exc)
    elif capture_file and capture_file.exists():
        # Older session: authorization key may still be in 01_capture.json
        try:
            auth_data = load_json(capture_file).get("authorization", {})
        except Exception as exc:
            logger.warning("Failed to read authorization data from capture file: %s", exc)

    if not auth_data:
        return {}
    try:
        return build_rbac_summary(auth_data) or {}
    except Exception as exc:
        logger.warning("Failed to build RBAC summary: %s", exc)
        return {}


def _cleanup_logs_file(session) -> None:
    """Delete 01_logs.json unless keep_logs_file is set in config."""
    try:
        keep = ctx().config.keep_logs_file
    except Exception:
        keep = False

    if not keep and session.logs_file.exists():
        session.logs_file.unlink()
        logger.info("Removed log analysis file: %s", session.logs_file)


def _cleanup_rbac_file(session) -> None:
    """Delete 01_capture_rbac.json after the report has been generated.

    The RBAC authorization graph can be large (accounts, groups, roles, 6000+
    methods).  Once the enriched summary has been written into the extended
    validation results and embedded in the report, the raw capture file is no
    longer needed.  Always deleted — no keep flag (unlike logs, there is no
    audit-trail use-case for keeping the raw RBAC graph around).
    """
    if session.rbac_file.exists():
        session.rbac_file.unlink()
        logger.info("Removed RBAC capture file: %s", session.rbac_file)


def _rehydrate_attrs(df, session) -> None:
    """Thin shim around session_manager.rehydrate_validation_attrs.

    The shared helper now lives in session_manager so the WebUI's diff
    route can use the same code path — see CLAUDE.md hard-won lesson #3
    for the parquet-attrs-don't-survive background.
    """
    from platform_atlas.core.session_manager import rehydrate_validation_attrs
    rehydrate_validation_attrs(df, session)


@registry.register("session", "trend", description="Show compliance trends across sessions")
def handle_session_trend(args: Namespace) -> int:
    """Display a category heat matrix of pass rates across sessions over time."""
    import pandas as pd
    from rich.table import Table
    from rich.text import Text
    from rich import box

    _CATEGORY_LABELS = {
        "platform": "Platform",
        "mongo":    "MongoDB",
        "redis":    "Redis",
        "gateway4": "Gateway4",
        "gateway5": "Gateway5",
    }
    _CATEGORY_ORDER = ["platform", "gateway4", "gateway5", "mongo", "redis"]

    def _score_color(score: float) -> str:
        if score >= 85:
            return theme.success
        if score >= 75:
            return theme.warning
        return theme.error

    def _heat_cell(rate: float | None) -> Text:
        if rate is None:
            return Text("  N/A  ", style=theme.text_ghost)
        bar_len = 6
        filled = int(round(rate / 100 * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        color = _score_color(rate)
        t = Text()
        t.append(f"{bar} ", style=color)
        t.append(f"{rate:3.0f}%", style=f"bold {color}")
        return t

    try:
        manager = get_session_manager()

        # Resolve the environment filter
        all_envs: bool = getattr(args, "all_envs", False)
        env_filter: str | None = getattr(args, "env", None)
        limit: int = getattr(args, "limit", 20)

        if not all_envs and env_filter is None:
            # Default to the active environment
            try:
                env_filter = ctx().config.active_environment
            except Exception:
                env_filter = None

        # Load all sessions, oldest first, filter to those with validation data
        all_sessions = manager.list(sort_by="created_at", reverse=False)

        trend_rows = []
        for session in all_sessions:
            if not session.metadata.validation_completed:
                continue
            if not all_envs:
                sess_env = session.metadata.environment or ""
                if env_filter and sess_env != env_filter:
                    continue

            vf = session.validation_file
            if not vf.exists():
                continue

            try:
                df = pd.read_parquet(vf)
            except Exception:
                continue

            total = len(df)
            passed = int((df["status"] == "PASS").sum())
            failed = int((df["status"] == "FAIL").sum())
            skipped = int((df["status"] == "SKIP").sum())
            score = round(passed / (passed + failed) * 100, 1) if (passed + failed) > 0 else 0.0

            cat_rates: dict[str, float | None] = {}
            if "category" in df.columns:
                for cat in df["category"].unique():
                    cdf = df[df["category"] == cat]
                    cp = int((cdf["status"] == "PASS").sum())
                    cf = int((cdf["status"] == "FAIL").sum())
                    cat_rates[cat] = round(cp / (cp + cf) * 100, 0) if (cp + cf) > 0 else None

            trend_rows.append({
                "name":     session.name,
                "date":     session.metadata.created_at.strftime("%Y-%m-%d"),
                "env":      session.metadata.environment or "—",
                "tier":     session.metadata.tier or "extended",
                "total":    total,
                "passed":   passed,
                "failed":   failed,
                "skipped":  skipped,
                "score":    score,
                "cat_rates": cat_rates,
            })

        # Apply limit (keep the most-recent N after sorting oldest→newest)
        if len(trend_rows) > limit:
            trend_rows = trend_rows[-limit:]

        if not trend_rows:
            env_hint = f" for environment [bold]{env_filter}[/bold]" if env_filter else ""
            console.print(f"\n  [{theme.warning}]No validated sessions found{env_hint}.[/{theme.warning}]")
            console.print(f"  [{theme.text_dim}]Run [bold]session run validate[/bold] on a session first, or use [bold]--all-envs[/bold] to broaden the search.[/{theme.text_dim}]\n")
            return 0

        # Determine which categories appear
        all_cats: set[str] = set()
        for row in trend_rows:
            all_cats.update(row["cat_rates"].keys())
        ordered_cats = [c for c in _CATEGORY_ORDER if c in all_cats]

        # Build title
        count_label = f"{len(trend_rows)} session" + ("" if len(trend_rows) == 1 else "s")
        if all_envs:
            title = f"Compliance Trend — All Environments ({count_label})"
        else:
            title = f"Compliance Trend — {env_filter or 'All'} ({count_label})"

        table = Table(
            title=title,
            box=box.SIMPLE,
            show_header=True,
            header_style=f"bold {theme.primary}",
            padding=(0, 1),
        )
        table.add_column("Session", style="bold", min_width=16, no_wrap=True)
        table.add_column("Date", style=theme.text_dim, min_width=12)
        if all_envs:
            table.add_column("Env", style=theme.accent, min_width=10)
        table.add_column("Overall", min_width=14, justify="right")
        for cat in ordered_cats:
            table.add_column(_CATEGORY_LABELS.get(cat, cat), min_width=14)

        for row in trend_rows:
            overall = Text()
            overall.append(f"{row['passed']}✓ ", style=theme.success)
            overall.append(f"{row['failed']}✗ ", style=theme.error)
            overall.append(f" {row['score']:.1f}%", style=f"bold {_score_color(row['score'])}")

            cells = [row["name"], row["date"]]
            if all_envs:
                cells.append(row["env"])
            cells.append(overall)
            for cat in ordered_cats:
                cells.append(_heat_cell(row["cat_rates"].get(cat)))

            table.add_row(*cells)

        console.print()
        console.print(table)
        console.print(
            f"  [{theme.text_dim}]Color key: "
            f"[{theme.success}]≥85%[/{theme.success}]  "
            f"[{theme.warning}]75–84%[/{theme.warning}]  "
            f"[{theme.error}]<75%[/{theme.error}]  "
            f"N/A = category not in ruleset for that session[/{theme.text_dim}]"
        )
        console.print(
            f"  [{theme.text_dim}]Showing oldest → newest · "
            f"Use [bold]--limit N[/bold] or [bold]--all-envs[/bold] to adjust scope.[/{theme.text_dim}]\n"
        )
        return 0

    except Exception as e:
        console.print(f"[{theme.error}]✗[/{theme.error}] {e}")
        return 1
