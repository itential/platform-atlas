# pylint: disable=line-too-long
"""
Dispatch Handler ::: Sessions

Session create now interactively binds an environment, ruleset, and profile.
Session switch atomically restores the full context (env, ruleset, profile).
Session edit allows changing bindings before capture begins.
"""

import os
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
from platform_atlas.core.exceptions import AtlasError

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
    REPORT_TEMPLATE, OPERATIONAL_TEMPLATE,
    DIFF_TEMPLATE, ATLAS_HOME_DIFF,
)
from platform_atlas.core.init_setup import QSTYLE

theme = ui.theme
console = Console()

logger = logging.getLogger(__name__)


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
            org = env.organization_name
            uri = env.platform_uri
            # Build a descriptive label
            parts = []
            if org:
                parts.append(org)
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
        style=QSTYLE,
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

    default = preselect if preselect in [r.id for r in available] else (active_id or available[0].id)

    selected = questionary.select(
        "Select ruleset:",
        choices=choices,
        default=default,
        style=QSTYLE,
    ).ask()

    return selected


def _pick_profile(preselect: str | None = None) -> str | None:
    """
    Interactive profile picker. Returns profile ID or None if canceled.
    The user can also choose 'No profile' to use ruleset defaults.
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

    choices.append(questionary.Choice(
        title="── No profile (use ruleset defaults)",
        value="_none",
    ))

    default = preselect if preselect in [p.id for p in available] else (active_id or available[0].id)

    selected = questionary.select(
        "Select profile:",
        choices=choices,
        default=default,
        style=QSTYLE,
    ).ask()

    if selected is None:
        return None
    if selected == "_none":
        return ""

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

        # ── Resolve environment ──────────────────────────────────
        env_name = getattr(args, "env", None)
        if env_name is None:
            env_name = _pick_environment()
            if env_name is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

        # Load environment to get org name
        org_name = ""
        try:
            from platform_atlas.core.environment import get_environment_manager
            env_mgr = get_environment_manager()
            if env_mgr.exists(env_name):
                env = env_mgr.load(env_name)
                org_name = env.organization_name or ""
        except Exception:
            pass

        # Fall back to global config org name
        if not org_name:
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

        # ── Create session ───────────────────────────────────────
        session = manager.create(
            name=session_name,
            description=getattr(args, "description", "") or "",
            target=getattr(args, "target", "") or "",
            organization_name=org_name,
            environment=env_name,
            ruleset_id=ruleset_id,
            ruleset_profile=profile_id,
        )

        # Activate the session (also restores env + ruleset context)
        manager.activate_session_context(session.name)

        _show_session_status(session)
        return 0

    except SessionError as e:
        console.print(f"[red]✗[/red] {e.message}")
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
                f"\n  [{theme.error}]✘ Session '{session.name}' cannot be edited[/{theme.error}]"
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
            # Build choices showing current bindings
            org_display = meta.organization_name or "[not set]"
            env_display = meta.environment or "[not set]"
            ruleset_display = meta.ruleset_id or "[not set]"
            profile_display = meta.ruleset_profile or "[none]"

            choices = [
                questionary.Choice(
                    title=f"Organization       {org_display}",
                    value="organization_name",
                ),
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
                style=QSTYLE,
            ).ask()

            if selected is None or selected == "_done":
                break

            if selected == "organization_name":
                new_org = questionary.text(
                    "Organization name:",
                    default=meta.organization_name,
                    style=QSTYLE,
                ).ask()
                if new_org is not None and new_org != meta.organization_name:
                    meta.organization_name = new_org.strip()
                    changed = True

            elif selected == "environment":
                new_env = _pick_environment(preselect=meta.environment)
                if new_env is not None and new_env != meta.environment:
                    meta.environment = new_env
                    changed = True
                    # Update org name from new environment
                    try:
                        from platform_atlas.core.environment import get_environment_manager
                        env_obj = get_environment_manager().load(new_env)
                        if env_obj.organization_name:
                            meta.organization_name = env_obj.organization_name
                    except Exception:
                        pass

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
        console.print(f"[red]✗[/red] {e.message}")
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

        try:
            logger.info("Starting capture for session '%s'", session.name)

            # Ensure ruleset and profile are loaded
            rm = get_ruleset_manager()
            if not rm.get_active_ruleset_id():
                console.print(f"[{theme.error}]✘[/{theme.error}] No ruleset loaded")
                console.print(f"[{theme.text_dim}]Load one first: platform-atlas ruleset load <id>[/{theme.text_dim}]")
                return 1
            if not rm.get_active_profile_id():
                console.print(f"[{theme.error}]✘[/{theme.error}] No profile set")
                console.print(f"[{theme.text_dim}]Set one first: platform-atlas ruleset profile set <id>[/{theme.text_dim}]")
                console.print(f"[{theme.text_dim}]View options: platform-atlas ruleset profile list[/{theme.text_dim}]")
                return 1

            # ── Confirm before capture ────────────────────────────────
            headless = getattr(args, "headless", False)

            if not headless:
                meta = session.metadata
                console.print()
                console.print(f"  [{theme.text_dim}]Session[/{theme.text_dim}]       [bold]{session.name}[/bold]")
                if meta.environment:
                    console.print(f"  [{theme.text_dim}]Environment[/{theme.text_dim}]   [{theme.primary}]{meta.environment}[/{theme.primary}]")
                if meta.organization_name:
                    console.print(f"  [{theme.text_dim}]Organization[/{theme.text_dim}]  {meta.organization_name}")
                console.print(f"  [{theme.text_dim}]Ruleset[/{theme.text_dim}]       [{theme.secondary}]{rm.get_active_ruleset_id()}[/{theme.secondary}]")
                console.print(f"  [{theme.text_dim}]Profile[/{theme.text_dim}]       [{theme.accent}]{rm.get_active_profile_id()}[/{theme.accent}]")
                console.print()

                proceed = questionary.confirm(
                    "Ready to start capture?",
                    default=False,
                ).ask()

                if proceed is None:
                    raise KeyboardInterrupt
                if not proceed:
                    console.print(f"\n  [{theme.text_dim}]Capture cancelled.[/{theme.text_dim}]\n")
                    return 0

            # Update status
            session.update_status(SessionStatus.CAPTURING)

            # ── Branch: Manual vs Automated ──────────────────────────
            manual_mode = hasattr(args, 'manual') and args.manual

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

                structured = reshape_capture(captured_data)
                captured_data = finalize_capture(
                    structured_data=structured,
                    rules=ctx().rules,
                    ruleset=ctx().ruleset,
                    config=ctx().config,
                    modules_ran=list(captured_data.keys()),
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

                # Build log parser config from CLI flags
                from platform_atlas.capture.log_parser import ParserConfig, set_parser_config

                log_config = ParserConfig(
                    search_type=getattr(args, 'log_mode', 'top'),
                    top_n=getattr(args, 'log_top_n', 25),
                    levels=getattr(args, 'log_levels', ['error', 'warn']),
                )
                set_parser_config(log_config)

                try:
                    captured_data = run_capture(
                        modules,
                        skip_guided=skip_guided,
                        skip_logs=skip_logs,
                        headless=headless
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

            # ── Common path: arch questions, save, metadata, done ────────────────────

            # Architecture Validation Questions
            skip_arch = hasattr(args, 'skip_architecture') and args.skip_architecture

            if not skip_arch:
                from platform_atlas.capture.collectors.manual import (
                    run_architecture_collection,
                    load_architecture_progress
                )

                try:
                    arch_data = run_architecture_collection()
                    captured_data.setdefault("checks", {})["architecture_validation"] = arch_data["architecture_validation"]
                    logger.info(
                        "Architecture validation collected %d sections",
                        len(arch_data["architecture_validation"])
                    )
                except KeyboardInterrupt:
                    console.print(
                        f"\n[{theme.warning}]Architecture questions paused[/{theme.warning}]"
                    )
                    # Partial progress is already saved by the collector
                    # Pull in whatever sections completed before the interrupt
                    from platform_atlas.capture.collectors.manual import (
                        load_architecture_progress
                    )
                    partial = load_architecture_progress()
                    if partial:
                        captured_data.setdefault("checks", {})["architecture_validation"] = partial["architecture_validation"]
            else:
                # Try loading from completed architecture progress file first
                from platform_atlas.capture.collectors.manual import (
                    load_architecture_progress
                )
                arch_from_progress = load_architecture_progress()

                if arch_from_progress:
                    captured_data.setdefault("checks", {})["architecture_validation"] = arch_from_progress["architecture_validation"]
                    console.print(
                        f"[{theme.text_dim}]Using architecture data from "
                        f"previous collection.[/{theme.text_dim}]"
                    )
                elif session.capture_file.exists():
                    # Fall back to pulling from a previous capture file
                    try:
                        import json as _json
                        existing = _json.loads(
                            session.capture_file.read_text(encoding="utf-8")
                        )
                        if "architecture_validation" in existing.get("checks", {}):
                            captured_data.setdefault("checks", {})["architecture_validation"] = existing["checks"]["architecture_validation"]
                            console.print(
                                f"[{theme.text_dim}]Using architecture data from "
                                f"previous capture.[/{theme.text_dim}]"
                            )
                    except Exception:
                        logger.debug("No previous architecture data to reuse")

            # Save to session directory
            import json
            session.capture_file.write_text(
                json.dumps(captured_data, indent=2, default=str, ensure_ascii=False),
                encoding='utf-8'
            )
            os.chmod(session.capture_file, 0o600) # owner-only read/write

            # Extract log data into separate file to keep capture JSON lean
            import json as _json
            platform_data = captured_data.get("platform", {})
            mongo_data = captured_data.get("mongo", {})
            logs_payload = {}

            if "log_analysis" in platform_data:
                logs_payload["log_analysis"] = platform_data.pop("log_analysis")
            if "webserver_logs" in platform_data:
                logs_payload["webserver_logs"] = platform_data.pop("webserver_logs")
            if "log_analysis" in mongo_data:
                logs_payload["mongo_log_analysis"] = mongo_data.pop("log_analysis")

            if logs_payload:
                session.logs_file.write_text(
                    _json.dumps(logs_payload, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.chmod(session.logs_file, 0o600)
                logger.info("Log analysis saved separately (%d keys)", len(logs_payload))

                # Rewrite capture file without the log data
                session.capture_file.write_text(
                    _json.dumps(captured_data, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )

            # Stamp current context (ruleset, version, profile, environment)
            session.metadata.stamp_context()
            session.metadata.modules_ran = captured_data.get('_atlas', {}).get('metadata', {}).get('modules_ran', [])
            session.mark_stage_complete(SessionStage.CAPTURE)

            console.print(f"\n[{theme.success}]✓[/{theme.success}] Capture complete")
            console.print(f"  Saved to: {session.capture_file}")
            console.print()
            ui.next_step("platform-atlas session run validate")
            return 0
        finally:
            detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[red]✗[/red] {e.message}")
        if isinstance(e, NoActiveSessionError):
            console.print()
            ui.hint_panel(
                f"Create a session with: [bold {theme.primary}]platform-atlas session create <n>[/bold {theme.primary}]",
                title="No Active Session",
                style=theme.warning,
            )
        return 1
    except AtlasError as e:
        console.print(f"[red]✗[/red] {e.message}")
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
                console.print(f"[{theme.error}]✘[/{theme.error}] No ruleset loaded")
                console.print(f"[{theme.text_dim}]Load one first: platform-atlas ruleset load <id>[/{theme.text_dim}]")
                return 1

            # Ensure profile is set
            if not get_ruleset_manager().get_active_profile_id():
                console.print(f"[{theme.error}]✘[/{theme.error}] No profile set")
                console.print(f"[{theme.text_dim}]Set one first: platform-atlas ruleset profile set <id>[/{theme.text_dim}]")
                console.print(f"[{theme.text_dim}]View options: platform-atlas ruleset profile list[/{theme.text_dim}]")
                return 1

            # Run validation
            console.print(f"[{theme.primary}]Running validation for session:[/{theme.primary}] {session.name}\n")
            df = validate_from_files(session.capture_file)

            # Save company name to DataFrame metadata
            organization_name = str(ctx().config.organization_name)
            df.attrs["organization_name"] = organization_name.title()

            # Save validation results to parquet file
            df.to_parquet(session.validation_file, engine="pyarrow", compression="snappy")
            os.chmod(session.validation_file, 0o600)

            # Update metadata with stats
            session.metadata.total_rules = len(df)
            session.metadata.pass_count = len(df[df['status'].str.upper() == 'PASS'])
            session.metadata.fail_count = len(df[df['status'].str.upper() == 'FAIL'])
            session.metadata.skip_count = len(df[df['status'].str.upper() == 'SKIP'])
            session.mark_stage_complete(SessionStage.VALIDATE)

            console.print(f"\n[{theme.success}]✓[/{theme.success}] Validation complete")
            console.print(f"  Results: {session.metadata.pass_count} passed, {session.metadata.fail_count} failed")
            console.print(f"  Saved to: {session.validation_file}")
            console.print()
            ui.next_step("platform-atlas session run report")
            return 0
        finally:
            detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1
    except AtlasError as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1

@registry.register("session", "run", "report", description="Generate report from validation results")
def handle_session_run_report(args: Namespace) -> int:
    """Generate report from validation results"""

    # Route to operational report if --operational flag is set
    if getattr(args, 'operational', False):
        return _handle_operational_report(args)

    try:
        manager = get_session_manager()

        # Get session (specified or active)
        if hasattr(args, 'session') and args.session:
            session = manager.get(args.session)
        else:
            session = manager.get_active()

        # Attach session log
        session_handler = attach_session_log(session.log_file)

        try:
            # Check that validation is complete
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

            # Load validation results
            import pandas as pd
            df = pd.read_parquet(session.validation_file)

            # ── Rehydrate attrs from capture JSON (Parquet loses them) ──
            _rehydrate_attrs(df, session)

            # Handle non-HTML export formats
            fmt = getattr(args, 'format', 'html')
            if fmt != 'html':
                export_path = session.directory / f"report.{fmt}"

                if fmt == 'csv':
                    df.to_csv(export_path, index=False)

                elif fmt in ('json', 'md'):
                    # Load extended validation results
                    extended_results = df.attrs.get('extended_results', [])

                    # Load architecture data
                    architecture_data = {}
                    try:
                        from platform_atlas.capture.collectors.manual import load_architecture_progress
                        arch = load_architecture_progress()
                        if arch:
                            architecture_data = arch.get("architecture_validation", {})
                    except Exception as e:
                        logger.debug("Could not load architecture data: %s", e)

                    from platform_atlas.reporting.reporting_engine import (
                        export_json_report,
                        export_markdown_report,
                    )

                    if fmt == 'json':
                        export_json_report(
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

                session.mark_stage_complete(SessionStage.REPORT)

                # Clean up log analysis file after report is generated
                if session.logs_file.exists():
                    session.logs_file.unlink()
                    logger.info("Removed log analysis file: %s", session.logs_file)
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Exported → {export_path}")
                return 0

            # Determine output path
            if hasattr(args, 'output') and args.output:
                output_path = Path(args.output)
            else:
                output_path = session.report_file

            # Generate report
            console.print(f"[{theme.primary}]Generating report for session:[/{theme.primary}] {session.name}\n")

            from platform_atlas.reporting.report_renderer import render_html_report

            # Set Report Template Path
            active_template = REPORT_TEMPLATE

            # Ruleset Metadata
            ruleset_id = df.attrs.get('ruleset_id', 'unknown')
            ruleset_ver = df.attrs.get('ruleset_version', 'unknown')
            ruleset_profile = df.attrs.get('ruleset_profile', '')
            organization_name = df.attrs.get('organization_name', "unknown")

            # Load knowledgebase for remediation fixes (default: on, disable with --no-fixes)
            knowledgebase = {}
            if not getattr(args, "no_fixes", False):
                from platform_atlas.core.knowledgebase import load_knowledgebase
                knowledgebase = load_knowledgebase()
                if knowledgebase:
                    logger.info("Loaded %d rules from knowledge base", len(knowledgebase))
                else:
                    logger.debug("Knowledge base not found or empty — fix instructions will not be included")

            # Load architecture data from global store
            architecture_data = {}
            try:
                from platform_atlas.capture.collectors.manual import load_architecture_progress
                arch = load_architecture_progress()
                if arch:
                    architecture_data = arch.get("architecture_validation", {})
            except Exception as e:
                logger.debug("Could not load architecture data: %s", e)

            render_html_report(
                df,
                active_template,
                output_path=output_path,
                title="Platform Health Report",
                subtitle=session.name,
                organization_name=organization_name,
                ruleset_version=f"{ruleset_ver} ({ruleset_profile})" if ruleset_profile else ruleset_ver,
                target_system=ruleset_id,
                modules_ran=session.metadata.modules_ran,
                knowledgebase=knowledgebase,
                architecture_data=architecture_data,
            )

            # Mark stage complete
            session.mark_stage_complete(SessionStage.REPORT)

            # Clean up log analysis file after report is generated
            if session.logs_file.exists():
                session.logs_file.unlink()
                logger.info("Removed log analysis file: %s", session.logs_file)

            console.print(f"\n[{theme.success}]✓[/{theme.success}] Report generated")
            console.print(f"  Location: {output_path}")

            # Auto-open if requested
            if not (hasattr(args, 'no_open') and args.no_open):
                import webbrowser
                webbrowser.open(f"file://{output_path.absolute()}")
                console.print(f"  [{theme.text_dim}]Opened in browser[/{theme.text_dim}]")
            return 0
        finally:
            detach_handler(session_handler)

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1
    except Exception as e:
        console.print(f"[red]✗[/red] {type(e).__name__}: {e}")
        return 1

def _handle_operational_report(args: Namespace) -> int:
    """Generate operational metrics report from MongoDB pipelines"""
    try:
        manager = get_session_manager()

        # Get session (specified or active)
        if hasattr(args, 'session') and args.session:
            session = manager.get(args.session)
        else:
            session = manager.get_active()

        # Attach session log
        session_handler = attach_session_log(session.log_file)

        try:
            console.print(f"[{theme.primary}]Generating operational report for session:[/{theme.primary}] {session.name}\n")

            # Load config
            config = ctx().config

            # Build MongoCollector
            from platform_atlas.capture.collectors.mongo import MongoCollector
            collector = MongoCollector.from_config()

            if collector is None:
                console.print(f"[{theme.error}]✗[/{theme.error}] No mongo_uri configured — cannot run operational pipelines")
                console.print(f"  [{theme.text_dim}]Set a MongoDB URI in your environment credentials[/{theme.text_dim}]")
                return 1

            # Run pipelines
            from platform_atlas.reporting.operational_engine import run_operational_pipelines

            with collector:
                report = run_operational_pipelines(collector)

            if report.pipeline_count == 0:
                console.print(f"[{theme.warning}]⚠[/{theme.warning}] No operational pipelines found")
                console.print(f"  [{theme.text_dim}]Place pipeline JSON files in ~/.atlas/pipelines/[/{theme.text_dim}]")
                return 1

            # Save raw data for potential re-rendering
            report.to_json(session.operational_data_file)
            logger.info("Saved operational data: %s", session.operational_data_file)

            # Set Operational Report Template Path
            active_template = OPERATIONAL_TEMPLATE

            # Render HTML
            from platform_atlas.reporting.operational_renderer import render_operational_report

            # Try to get hostname from capture data
            hostname = "Unknown"
            if session.capture_file.exists():
                try:
                    import json
                    with open(session.capture_file, "r", encoding="utf-8") as f:
                        capture_data = json.load(f)
                    system_data = capture_data.get("system", {})
                    hostname = system_data.get("host", {}).get("hostname", "Unknown")
                except Exception as e:
                    logger.debug("Could not read hostname from capture data: %s", e)

            render_operational_report(
                report,
                template_path=active_template,
                output_path=session.operational_file,
                title="Operational Metrics Report",
                subtitle=session.name,
                organization_name=config.organization_name,
                hostname=hostname,
            )

            # Summary output
            if report.cancelled:
                console.print(
                    f"\n[{theme.warning}]⚠[/{theme.warning}] Partial operational report generated (cancelled by user)"
                )
            else:
                console.print(f"\n[{theme.success}]✓[/{theme.success}] Operational report generated")

            console.print(f"  Location: {session.operational_file}")
            console.print(f"  Pipelines: {report.success_count}/{report.pipeline_count} succeeded ({report.total_rows} rows)")

            # Auto-open if requested
            if not (hasattr(args, 'no_open') and args.no_open):
                try:
                    import webbrowser
                    webbrowser.open(f"file://{session.operational_file}")
                except Exception:
                    pass

            return 0

        finally:
            detach_handler(session_handler)

    except NoActiveSessionError:
        console.print(f"\n[{theme.error}]✗[/{theme.error}] No active session")
        console.print(f"  [{theme.text_dim}]Create one with: platform-atlas session create <n>[/{theme.text_dim}]")
        return 1
    except SessionError as e:
        console.print(f"\n[{theme.error}]✗ Session Error:[/{theme.error}] {e}")
        return 1
    except Exception as e:
        logger.debug("Operational report error: %s", e, exc_info=True)
        console.print(f"\n[{theme.error}]✗ Operational report failed:[/{theme.error}] {e}")
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
            console.print(f"[{theme.text_dim}]Create one with: platform-atlas session create <n>[/{theme.text_dim}]")
            return 0

        active_name = manager.get_active_session_name()

        table = Table(
            title=f"Audit Sessions ({len(sessions)})",
            box=box.ROUNDED
        )
        table.add_column("", width=2)
        table.add_column("Name", style="cyan")
        table.add_column("Environment", style=theme.accent)
        table.add_column("Organization", style=theme.text_dim)
        table.add_column("Ruleset", style=theme.secondary)
        table.add_column("Profile", style=theme.text_dim)
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="dim")
        table.add_column("Progress", justify="center")
        table.add_column("Results", justify="right")

        for session in sessions:
            # Active marker
            marker = "✓" if session.name == active_name else ""

            # Status with color
            status_colors = {
                "created": theme.text_dim,
                "capturing": theme.primary,
                "captured": theme.info,
                "validating": theme.warning,
                "validated": theme.success,
                "reported": theme.success_glow,
                "failed": theme.error,
            }
            status_style = status_colors.get(session.metadata.status.value, "white")
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
        console.print(f"[red]✗[/red] {e}")
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
                f"[red]{session.metadata.fail_count} failed[/red], "
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

        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[red]✗[/red] {e.message}")
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
                style=QSTYLE,
            ).ask()

            if selected is None:
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 1

            session = manager.activate_session_context(selected)
            _show_session_status(session)

        return 0

    except SessionError as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1

@registry.register("session", "switch", description="Switch the active session")
def handle_session_switch(args: Namespace) -> int:
    """Switch the active session (alias for session active)"""
    return handle_session_active(args)


@registry.register("session", "export", description="Export session for delivery")
def handle_session_export(args: Namespace) -> int:
    """Export session for delivery"""
    try:
        manager = get_session_manager()

        # Get session
        if args.session_name:
            session = manager.get(args.session_name)
        else:
            session = manager.get_active()

        # Determine output path
        if args.output:
            output_path = Path(args.output)
        else:
            # Default: ATLAS-<session>-<date>.zip in current directory
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d")
            filename = f"ATLAS-{session.name}-{timestamp}.{args.format}"
            output_path = Path(filename)

        # Export
        console.print(f"[{theme.primary}]Exporting session:[/{theme.primary}] {session.name}\n")

        exported = manager.export(
            session.name,
            output_path,
            archive_format=args.format,
            include_debug=args.include_debug,
            redact=args.redact
        )

        size_mb = exported.stat().st_size / (1024 * 1024)
        console.print(f"[{theme.success}]✓[/{theme.success}] Exported to: {exported}")
        console.print(f"  Size: {size_mb:.2f} MB")

        return 0

    except (SessionError, NoActiveSessionError) as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1

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
                style=QSTYLE,
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
        console.print(f"[red]✗[/red] {e.message}")
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
            sessions = manager.list()
            if len(sessions) < 2:
                console.print(f"\n  [{theme.warning}]Need at least 2 sessions to compare.[/{theme.warning}]\n")
                return 1

            choices = [
                questionary.Choice(
                    title=f"{s.name}  ({s.metadata.status.value})",
                    value=s.name,
                )
                for s in sessions
            ]

            if baseline_name is None:
                baseline_name = questionary.select(
                    "Select baseline session:",
                    choices=choices,
                    style=QSTYLE,
                ).ask()

                if baseline_name is None:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 1

            if latest_name is None:
                remaining = [c for c in choices if c.value != baseline_name]
                latest_name = questionary.select(
                    "Select latest session:",
                    choices=remaining,
                    style=QSTYLE,
                ).ask()

                if latest_name is None:
                    console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                    return 1

        # Get both sessions
        baseline_session = manager.get(baseline_name)
        latest_session = manager.get(latest_name)

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

        baseline_df = pd.read_parquet(baseline_session.validation_file)
        latest_df = pd.read_parquet(latest_session.validation_file)

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
            import webbrowser
            webbrowser.open(f"file://{output_path.absolute()}")

        return 0

    except SessionError as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1


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
        console.print(f"[red]✗[/red] {e.message}")
        return 1


@registry.register("session", "prune", description="Delete uncaptured sessions older than N days")
def handle_session_prune(args: Namespace) -> int:
    """
    Bulk-delete sessions that were created but never captured,
    older than --older-than DAYS days.

    The active session is always skipped even if it qualifies.
    Use --dry-run to preview without deleting. Use --force to skip confirmation.
    """
    from datetime import datetime, timezone, timedelta
    from rich.table import Table
    from rich import box

    try:
        manager = get_session_manager()
        older_than: int = args.older_than
        dry_run: bool = getattr(args, "dry_run", False)
        force: bool = getattr(args, "force", False)

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=older_than)
        active_name = manager.get_active_session_name()

        all_sessions = manager.list()
        candidates = []
        skipped_active = False

        for session in all_sessions:
            # Only sessions that were never captured
            if session.metadata.capture_completed:
                continue
            # Age check against created_at
            created = session.metadata.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue
            # Never touch the active session
            if session.name == active_name:
                skipped_active = True
                continue
            candidates.append(session)

        if not candidates:
            console.print(f"\n  [{theme.text_dim}]No uncaptured sessions older than {older_than} days.[/{theme.text_dim}]\n")
            if skipped_active:
                console.print(
                    f"  [{theme.warning}]Note: active session '{active_name}' was skipped "
                    f"(deactivate it first to include it).[/{theme.warning}]\n"
                )
            return 0

        # Build preview table
        table = Table(
            title=f"{'[dim]Dry run — [/dim]' if dry_run else ''}Uncaptured sessions to prune ({len(candidates)})",
            box=box.ROUNDED,
        )
        table.add_column("Name", style="cyan")
        table.add_column("Environment", style=theme.accent)
        table.add_column("Created", style="dim")
        table.add_column("Age (days)", justify="right", style=theme.warning)

        today = datetime.now(tz=timezone.utc)
        for session in candidates:
            created = session.metadata.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (today - created).days
            env_display = session.metadata.environment or f"[{theme.text_ghost}]—[/{theme.text_ghost}]"
            table.add_row(
                session.name,
                env_display,
                created.strftime("%Y-%m-%d"),
                str(age_days),
            )

        console.print()
        console.print(table)

        if skipped_active:
            console.print(
                f"\n  [{theme.warning}]Note: active session '{active_name}' was skipped.[/{theme.warning}]"
            )

        if dry_run:
            console.print(
                f"\n  [{theme.text_dim}]Dry run — nothing deleted. "
                f"Remove --dry-run to prune these {len(candidates)} session(s).[/{theme.text_dim}]\n"
            )
            return 0

        # Confirm unless --force
        if not force:
            console.print()
            if not Confirm.ask(
                f"Permanently delete {len(candidates)} session(s)?", default=False
            ):
                console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
                return 0

        # Delete
        deleted = 0
        failed = 0
        for session in candidates:
            try:
                manager.delete(session.name, force=True)
                console.print(f"  [{theme.success}]✓[/{theme.success}] Deleted: {session.name}")
                deleted += 1
            except SessionError as e:
                console.print(f"  [red]✗[/red] {session.name}: {e.message}")
                failed += 1

        console.print()
        console.print(
            f"  [{theme.success}]✓[/{theme.success}] {deleted} session(s) deleted"
            + (f", {failed} failed" if failed else "")
        )
        console.print()
        return 0 if not failed else 1

    except SessionError as e:
        console.print(f"[red]✗[/red] {e.message}")
        return 1


# =================================================
# Shared Helpers
# =================================================

def _rehydrate_attrs(df, session) -> None:
    """
    Rehydrate DataFrame .attrs from the capture JSON file.

    Parquet round-trips lose .attrs (lesson #3 from the reference doc).
    This reads the _atlas metadata block from the capture file and
    restores the attrs that the reporting engine needs.
    """
    import json as _json

    if not session.capture_file.exists():
        # Fall back to session metadata for what we can
        meta = session.metadata
        df.attrs.setdefault("organization_name", meta.organization_name or "Unknown")
        df.attrs.setdefault("environment", meta.environment or "")
        df.attrs.setdefault("ruleset_id", meta.ruleset_id or "")
        df.attrs.setdefault("ruleset_version", meta.ruleset_version or "")
        df.attrs.setdefault("ruleset_profile", meta.ruleset_profile or "")
        return

    try:
        with open(session.capture_file, encoding="utf-8") as f:
            capture = _json.load(f)

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

    except Exception as e:
        logger.debug("Failed to rehydrate attrs from capture JSON: %s", e)
        # Fall back to session metadata
        meta = session.metadata
        df.attrs.setdefault("organization_name", meta.organization_name or "Unknown")
        df.attrs.setdefault("environment", meta.environment or "")
