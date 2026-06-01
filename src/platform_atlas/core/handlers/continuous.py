# pylint: disable=line-too-long
"""
Dispatch Handler ::: Continuous Audit

Subcommands:
    continuous-audit run-once   — execute one cycle synchronously
    continuous-audit status     — show enabled state + last run + counts
    continuous-audit alerts     — list current drift alerts
    continuous-audit ack        — acknowledge an alert (or all)
    continuous-audit enable     — enable for the active environment
    continuous-audit disable    — disable for the active environment
    continuous-audit policy     — show / set the alert policy (any | regression)
    continuous-audit watch      — manage the rule-number watchlist
"""

from __future__ import annotations

import logging
from argparse import Namespace

from rich.console import Console
from rich.table import Table

from platform_atlas.core import ui
from platform_atlas.core.context import ctx
from platform_atlas.core.registry import registry

from platform_atlas.continuous import alerts as alerts_mod, notifications, os_scheduler, storage
from platform_atlas.continuous.engine import run_once
from platform_atlas.continuous.models import (
    ALERT_POLICY_ANY,
    ALERT_POLICY_REGRESSION,
    AlertStatus,
    ContinuousSettings,
    # TODO: Log file watching — deferred to a later version of Atlas.
    # LogWatchEntry,
    VALID_ALERT_POLICIES,
    # VALID_LOG_WATCH_THRESHOLDS,
    # LOG_WATCH_SOURCES,
)
from platform_atlas.continuous.policy import describe_policy
from platform_atlas.continuous.runtime import can_enable, read_settings, write_settings

logger = logging.getLogger(__name__)
theme = ui.theme
console = Console()


# Mirrors _CONTINUOUS_INTERVAL_CHOICES in cli.py — kept here for display only.
_INTERVAL_LABELS: dict[int, str] = {
    3600: "1h", 7200: "2h", 21600: "6h",
    43200: "12h", 86400: "24h", 604800: "1w",
}


def _format_interval(seconds: int) -> str:
    """Render a stored interval as the friendly label, falling back to raw seconds."""
    return _INTERVAL_LABELS.get(seconds, f"{seconds}s")


def _active_env_or_complain() -> str | None:
    env = ctx().active_environment
    if not env:
        console.print(
            f"[{theme.warning}]⚠ No active environment.[/{theme.warning}] "
            f"Continuous audit is per-environment — set one with "
            f"[bold]platform-atlas env switch[/bold]."
        )
        return None
    return env


# ── continuous-audit run-once ─────────────────────────────────────────

@registry.register("continuous-audit", "run-once",
                   description="Execute one continuous-audit cycle and write the JSON report")
def handle_run_once(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    console.print(
        f"[{theme.text_dim}]Running continuous audit for env=[bold]{env}[/bold] "
        f"(Platform OAuth only)…[/{theme.text_dim}]"
    )
    run = run_once(environment=env)
    s = run.summary
    if run.capture_error:
        console.print(f"[bold {theme.error}]Capture error:[/bold {theme.error}] {run.capture_error}")
    console.print(
        f"  [{theme.success}]✓[/{theme.success}] {s.pass_count} pass  "
        f"[{theme.error}]✗[/{theme.error}] {s.fail_count} fail  "
        f"[{theme.text_dim}]– {s.skip_count} skip[/{theme.text_dim}]  "
        f"[bold]{s.drifted_count} drift[/bold]"
    )
    console.print(
        f"  [{theme.text_dim}]Run ID: {run.run_id} · "
        f"{run.duration_ms} ms · "
        f"report: {storage.run_path(env, run.run_id)}[/{theme.text_dim}]"
    )
    return 0 if not run.capture_error else 1


# ── continuous-audit status ───────────────────────────────────────────

@registry.register("continuous-audit", "status",
                   description="Show continuous-audit state for the active environment")
def handle_status(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1

    settings = read_settings(env)
    status = storage.read_status(env)
    counts = alerts_mod.counts(env)

    enabled_str = "[bold green]ENABLED[/bold green]" if settings.enabled else "[bold red]DISABLED[/bold red]"
    console.print(f"\n[bold]Continuous audit ({env})[/bold]  {enabled_str}\n")

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style=theme.text_dim, no_wrap=True)
    table.add_column()

    table.add_row("Interval",       _format_interval(settings.interval_seconds))
    table.add_row("Retain runs",    str(settings.retain_runs))
    table.add_row("Ruleset",        settings.ruleset_id or "[dim](active)[/dim]")
    table.add_row("Profile",        settings.profile_id or "[dim](active)[/dim]")
    table.add_row("Alert policy",   describe_policy(settings.alert_policy))
    if settings.watchlist:
        watch_display = ", ".join(settings.watchlist)
        if len(watch_display) > 80:
            watch_display = f"{len(settings.watchlist)} rules · {watch_display[:64]}…"
        table.add_row("Watchlist",  watch_display)
    else:
        table.add_row("Watchlist",  "[dim]all rules[/dim]")

    if status:
        table.add_row("Last run",       status.get("last_finished_at", "—"))
        table.add_row("Last status",    str(status.get("last_status", "—")).upper())
        table.add_row("Last duration",  f"{status.get('last_duration_ms', 0)} ms")
        if status.get("last_error"):
            table.add_row("Last error", f"[{theme.error}]{status['last_error']}[/{theme.error}]")
        summary = status.get("last_summary") or {}
        if summary:
            table.add_row(
                "Last summary",
                f"{summary.get('pass_count', 0)} pass · "
                f"{summary.get('fail_count', 0)} fail · "
                f"{summary.get('skip_count', 0)} skip · "
                f"{summary.get('drifted_count', 0)} drift",
            )
    else:
        table.add_row("Last run", "—")

    table.add_row(
        "Alerts",
        f"{counts.get('unacked', 0)} unacked · {counts.get('total', 0)} total",
    )

    timer = os_scheduler.status(env)
    backend_label = "launch agent" if timer.backend == "launchd" else "systemd timer"
    if timer.available:
        if timer.installed and timer.active:
            timer_str = f"[green]Active[/green] · {os_scheduler.unit_basename(env)}"
            if not timer.linger_enabled:
                timer_str += f" [{theme.warning}](won't survive logout)[/{theme.warning}]"
        elif timer.installed:
            timer_str = f"[{theme.warning}]Inactive[/{theme.warning}] · {timer.detail}"
        else:
            timer_str = f"[{theme.text_dim}]Not installed[/{theme.text_dim}]"
    else:
        timer_str = f"[{theme.text_dim}]{backend_label} not available — WebUI-only[/{theme.text_dim}]"
    table.add_row("OS scheduler", timer_str)

    console.print(table)
    console.print()
    return 0


# ── continuous-audit alerts ───────────────────────────────────────────

@registry.register("continuous-audit", "alerts",
                   description="List current drift alerts")
def handle_alerts(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1

    severity = getattr(args, "severity", None)
    only_unacked = bool(getattr(args, "unacked", False))
    items = alerts_mod.list_alerts(env, severity=severity, only_unacked=only_unacked)

    if not items:
        filt = " (filtered)" if (severity or only_unacked) else ""
        console.print(f"[{theme.text_dim}]No alerts{filt} for env=[bold]{env}[/bold].[/{theme.text_dim}]")
        return 0

    table = Table(show_header=True, header_style=f"bold {theme.accent}", row_styles=[""])
    table.add_column("ID", no_wrap=True, style=theme.text_dim)
    table.add_column("Severity")
    table.add_column("Rule")
    table.add_column("Path", overflow="fold")
    table.add_column("Previous → Current", overflow="fold")
    table.add_column("Last seen", style=theme.text_dim)
    table.add_column("State", no_wrap=True)
    table.add_column("Count", justify="right")

    for alert in items:
        sev = (alert.severity or "").lower()
        sev_style = {
            "critical": "bold red",
            "high": "red",
            "warning": "yellow",
            "info": theme.text_dim,
        }.get(sev, theme.text_dim)
        state_style = "green" if alert.status == AlertStatus.ACKED else "yellow"
        table.add_row(
            alert.alert_id,
            f"[{sev_style}]{sev or '—'}[/{sev_style}]",
            alert.rule_name or alert.rule_number,
            alert.path,
            f"{alert.latest_previous!r} → {alert.latest_current!r}",
            alert.last_seen,
            f"[{state_style}]{alert.status.value}[/{state_style}]",
            str(alert.occurrence_count),
        )

    console.print(table)
    console.print(
        f"\n[{theme.text_dim}]Acknowledge with: "
        f"[bold]platform-atlas continuous-audit ack <id>[/bold] "
        f"or [bold]--all[/bold].[/{theme.text_dim}]"
    )
    return 0


# ── continuous-audit ack ──────────────────────────────────────────────

@registry.register("continuous-audit", "ack",
                   description="Acknowledge a drift alert (or all of them)")
def handle_ack(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    if getattr(args, "all_alerts", False):
        flipped = alerts_mod.ack_all(env, actor="cli")
        console.print(f"[{theme.success}]✓[/{theme.success}] Acknowledged {flipped} alert(s).")
        return 0
    alert_id = getattr(args, "alert_id", None)
    if not alert_id:
        console.print(f"[{theme.warning}]⚠ Provide an alert ID, or pass --all.[/{theme.warning}]")
        return 1
    if alerts_mod.ack_alert(env, alert_id, actor="cli"):
        console.print(f"[{theme.success}]✓[/{theme.success}] Acknowledged {alert_id}.")
        return 0
    console.print(f"[{theme.warning}]⚠ Alert {alert_id} not found or already acked.[/{theme.warning}]")
    return 1


# ── continuous-audit enable / disable ─────────────────────────────────

@registry.register("continuous-audit", "enable",
                   description="Enable continuous audit for the active environment")
def handle_enable(args: Namespace) -> int:
    target_env = getattr(args, "target_env", None)
    if target_env:
        env = target_env
    else:
        env = _active_env_or_complain()
        if env is None:
            return 1
    settings = read_settings(env)

    # Gate: a successful test run must exist before enabling. Skip the check
    # when already enabled — this call becomes a settings update.
    if not settings.enabled:
        allowed, reason = can_enable(env)
        if not allowed:
            console.print(f"\n[bold {theme.error}]Cannot enable continuous audit:[/bold {theme.error}] {reason}")
            console.print(
                f"\n[{theme.text_dim}]Run a one-shot test first:  "
                f"[bold]platform-atlas continuous-audit run-once[/bold][/{theme.text_dim}]\n"
            )
            return 1

    interval = getattr(args, "interval", None)
    retain = getattr(args, "retain", None)
    ruleset_id = getattr(args, "ruleset_id", None)
    profile_id = getattr(args, "profile_id", None)
    new_settings = ContinuousSettings(
        enabled=True,
        interval_seconds=int(interval) if interval else settings.interval_seconds,
        retain_runs=int(retain) if retain else settings.retain_runs,
        ruleset_id=ruleset_id if ruleset_id is not None else settings.ruleset_id,
        profile_id=profile_id if profile_id is not None else settings.profile_id,
        alert_policy=settings.alert_policy,
        watchlist=settings.watchlist,
    )
    write_settings(env, new_settings)
    ruleset_display = new_settings.ruleset_id or "(active)"
    profile_display = new_settings.profile_id or "(active)"
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Continuous audit ENABLED\n"
        f"  env=[bold]{env}[/bold]  interval={_format_interval(new_settings.interval_seconds)}  "
        f"retain={new_settings.retain_runs}  ruleset={ruleset_display}  profile={profile_display}"
    )

    # Install OS-level scheduler (systemd --user on Linux, launchd on macOS)
    # so runs continue independently of the WebUI process. Idempotent — safe
    # to call again when only the interval changed.
    timer = os_scheduler.install(env, new_settings.interval_seconds)
    backend_label = "launch agent" if timer.backend == "launchd" else "systemd timer"
    if timer.available and timer.installed and timer.active:
        console.print(
            f"[{theme.success}]✓[/{theme.success}] {backend_label} installed: "
            f"[bold]{os_scheduler.unit_basename(env)}[/bold]"
        )
        if not timer.linger_enabled:
            console.print(
                f"[{theme.warning}]⚠ Scheduler will only run while you're logged in.[/{theme.warning}]"
            )
            if timer.persistence_hint:
                console.print(
                    f"[{theme.text_dim}]Persistence:  [bold]{timer.persistence_hint}[/bold][/{theme.text_dim}]"
                )
    elif timer.available and timer.installed:
        console.print(f"[{theme.warning}]⚠ {backend_label.capitalize()} installed but inactive:[/{theme.warning}] {timer.detail}")
    else:
        console.print(
            f"[{theme.warning}]⚠ OS scheduler not installed:[/{theme.warning}] {timer.detail}\n"
            f"[{theme.text_dim}]The WebUI scheduler will run while the server is up. "
            f"For persistence, schedule [bold]platform-atlas continuous-audit run-once[/bold] "
            f"externally.[/{theme.text_dim}]"
        )
    return 0


@registry.register("continuous-audit", "disable",
                   description="Disable continuous audit for the active environment")
def handle_disable(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    settings = read_settings(env)
    new_settings = ContinuousSettings(
        enabled=False,
        interval_seconds=settings.interval_seconds,
        retain_runs=settings.retain_runs,
        ruleset_id=settings.ruleset_id,
        profile_id=settings.profile_id,
        alert_policy=settings.alert_policy,
        watchlist=settings.watchlist,
    )
    write_settings(env, new_settings)
    timer = os_scheduler.uninstall(env)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Continuous audit DISABLED for env=[bold]{env}[/bold]. "
        f"Existing run history is preserved."
    )
    if timer.detail:
        console.print(f"[{theme.text_dim}]{timer.detail}[/{theme.text_dim}]")
    return 0


# ── continuous-audit policy ───────────────────────────────────────────

def _replace_settings(env: str, **changes) -> ContinuousSettings:
    """Persist a partial update to ContinuousSettings, preserving every field
    we don't explicitly override. Returns the new settings for display."""
    current = read_settings(env)
    fields = {
        "enabled":           current.enabled,
        "interval_seconds":  current.interval_seconds,
        "retain_runs":       current.retain_runs,
        "ruleset_id":        current.ruleset_id,
        "profile_id":        current.profile_id,
        "alert_policy":      current.alert_policy,
        "watchlist":         current.watchlist,
        "log_watch_enabled": current.log_watch_enabled,
        "log_watches":       current.log_watches,
    }
    fields.update(changes)
    new = ContinuousSettings(**fields)
    write_settings(env, new)
    return new


@registry.register("continuous-audit", "policy",
                   description="Show or set the alert policy (any | regression)")
def handle_policy(args: Namespace) -> int:
    """``continuous-audit policy`` — no arg: show. ``policy <value>``: set.

    ``any``        — surface every drift event (default; pre-1.7.x behavior).
    ``regression`` — surface only PASS → FAIL transitions ("something that
                     was working just broke").
    """
    env = _active_env_or_complain()
    if env is None:
        return 1

    new_value = (getattr(args, "policy_value", None) or "").strip().lower()

    if not new_value:
        # Display mode — show the current setting.
        settings = read_settings(env)
        console.print(
            f"\n[bold]Alert policy[/bold] (env=[bold]{env}[/bold]): "
            f"[bold]{settings.alert_policy}[/bold] · {describe_policy(settings.alert_policy)}\n"
        )
        return 0

    if new_value not in VALID_ALERT_POLICIES:
        console.print(
            f"[{theme.error}]Invalid policy:[/{theme.error}] {new_value!r}\n"
            f"  Choose one of: {', '.join(VALID_ALERT_POLICIES)}"
        )
        return 1

    settings = _replace_settings(env, alert_policy=new_value)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Alert policy set to "
        f"[bold]{settings.alert_policy}[/bold] · {describe_policy(settings.alert_policy)}\n"
        f"[{theme.text_dim}](Existing alerts are unaffected — this changes "
        f"which future drift events generate alerts.)[/{theme.text_dim}]"
    )
    return 0


# ── continuous-audit watch ────────────────────────────────────────────

def _normalize_rules(raw: list[str] | None) -> list[str]:
    """Accept rule numbers from argv ('PLAT-001', 'plat-002') and clean them."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        # Tolerate comma-separated batches: --watch add PLAT-001,PLAT-002
        for piece in str(token).replace(",", " ").split():
            norm = piece.strip().upper()
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


@registry.register("continuous-audit", "watch", "list",
                   description="Show the rule-number watchlist for the active environment")
def handle_watch_list(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    settings = read_settings(env)
    if not settings.watchlist:
        console.print(
            f"\n[{theme.text_dim}]No watchlist configured for env=[bold]{env}[/bold] — "
            f"every rule is eligible for alerts.[/{theme.text_dim}]\n"
            f"Add specific rules with: [bold]platform-atlas continuous-audit watch add PLAT-001 PLAT-042[/bold]\n"
        )
        return 0
    console.print(
        f"\n[bold]Watchlist[/bold] (env=[bold]{env}[/bold]) — "
        f"{len(settings.watchlist)} rule(s)\n"
    )
    for rule in settings.watchlist:
        console.print(f"  [{theme.accent}]•[/{theme.accent}] {rule}")
    console.print()
    return 0


@registry.register("continuous-audit", "watch", "add",
                   description="Add one or more rule numbers to the watchlist")
def handle_watch_add(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    rules = _normalize_rules(getattr(args, "rules", None) or [])
    if not rules:
        console.print(
            f"[{theme.error}]No rule numbers given.[/{theme.error}] "
            f"Pass at least one (e.g. [bold]PLAT-042[/bold])."
        )
        return 1
    current = read_settings(env)
    existing = set(current.watchlist)
    added = [r for r in rules if r not in existing]
    if not added:
        console.print(
            f"[{theme.text_dim}]Already on the watchlist — no change.[/{theme.text_dim}]"
        )
        return 0
    new_list = tuple(list(current.watchlist) + added)
    settings = _replace_settings(env, watchlist=new_list)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Added {len(added)} rule(s) to the "
        f"watchlist: [bold]{', '.join(added)}[/bold]\n"
        f"[{theme.text_dim}]Watchlist now contains {len(settings.watchlist)} rule(s).[/{theme.text_dim}]"
    )
    return 0


@registry.register("continuous-audit", "watch", "remove",
                   description="Remove one or more rule numbers from the watchlist")
def handle_watch_remove(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    rules = _normalize_rules(getattr(args, "rules", None) or [])
    if not rules:
        console.print(
            f"[{theme.error}]No rule numbers given.[/{theme.error}] "
            f"Pass at least one to remove."
        )
        return 1
    current = read_settings(env)
    drop = set(rules)
    new_list = tuple(r for r in current.watchlist if r not in drop)
    removed = [r for r in current.watchlist if r in drop]
    if not removed:
        console.print(
            f"[{theme.text_dim}]None of those rules were on the watchlist — no change.[/{theme.text_dim}]"
        )
        return 0
    settings = _replace_settings(env, watchlist=new_list)
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Removed {len(removed)} rule(s): "
        f"[bold]{', '.join(removed)}[/bold]\n"
        f"[{theme.text_dim}]Watchlist now contains {len(settings.watchlist)} rule(s)"
        f"{' · all rules eligible' if not settings.watchlist else ''}.[/{theme.text_dim}]"
    )
    return 0


@registry.register("continuous-audit", "watch", "clear",
                   description="Clear the rule-number watchlist (alert on all rules)")
def handle_watch_clear(args: Namespace) -> int:
    env = _active_env_or_complain()
    if env is None:
        return 1
    current = read_settings(env)
    if not current.watchlist:
        console.print(f"[{theme.text_dim}]Watchlist already empty — no change.[/{theme.text_dim}]")
        return 0
    _replace_settings(env, watchlist=())
    console.print(
        f"[{theme.success}]✓[/{theme.success}] Watchlist cleared for env=[bold]{env}[/bold]. "
        f"All rules are now eligible for alerts."
    )
    return 0


# ── continuous-audit notify ───────────────────────────────────────────

def _resolve_notify_env(args: Namespace) -> str | None:
    target = getattr(args, "target_env", None)
    if target:
        return target
    return _active_env_or_complain()


def _parse_header_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            console.print(f"[{theme.warning}]⚠ Ignoring header without '=' separator: {raw!r}[/{theme.warning}]")
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            console.print(f"[{theme.warning}]⚠ Ignoring header with empty key: {raw!r}[/{theme.warning}]")
            continue
        out[key] = value
    return out


@registry.register("continuous-audit", "notify", "add",
                   description="Add a Slack or webhook notification channel")
def handle_notify_add(args: Namespace) -> int:
    env = _resolve_notify_env(args)
    if env is None:
        return 1
    channel_type = str(getattr(args, "channel_type", "")).lower()
    url = str(getattr(args, "url", "")).strip()
    if channel_type not in notifications.SUPPORTED_TYPES:
        console.print(f"[{theme.error}]Unsupported channel type:[/{theme.error}] {channel_type!r}")
        return 1
    if not url:
        console.print(f"[{theme.error}]Channel URL is required.[/{theme.error}]")
        return 1
    # URL scheme + SSRF blocklist enforcement happens inside notifications.add_channel
    # so the CLI and WebUI share one validator (set ATLAS_ALLOW_PRIVATE_WEBHOOKS=1 to bypass).

    headers = _parse_header_pairs(getattr(args, "channel_headers", []) or [])
    if channel_type == notifications.CHANNEL_TYPE_SLACK and headers:
        console.print(f"[{theme.text_dim}](Slack channels ignore custom headers; recorded but unused.)[/{theme.text_dim}]")

    secret = str(getattr(args, "channel_secret", "") or "")
    if secret and channel_type != notifications.CHANNEL_TYPE_WEBHOOK:
        console.print(f"[{theme.warning}]⚠ --secret only applies to webhook channels; ignored for {channel_type}.[/{theme.warning}]")
        secret = ""

    channel_id = str(getattr(args, "channel_id", "") or "") or notifications.make_channel_id(channel_type[:4])
    name = str(getattr(args, "channel_name", "") or "") or channel_id

    channel = notifications.NotificationChannel(
        id=channel_id, type=channel_type, name=name, url=url,
        headers=headers, secret=secret, enabled=True,
    )
    try:
        notifications.add_channel(env, channel)
    except ValueError as exc:
        console.print(f"[{theme.error}]Could not add channel:[/{theme.error}] {exc}")
        return 1

    console.print(
        f"[{theme.success}]✓[/{theme.success}] Added {channel_type} channel "
        f"[bold]{name}[/bold] (id=[bold]{channel_id}[/bold]) for env=[bold]{env}[/bold]."
    )
    if channel_type == notifications.CHANNEL_TYPE_WEBHOOK and not secret:
        console.print(
            f"[{theme.text_dim}]No HMAC secret set — receiver cannot verify Atlas authorship. "
            f"Pass --secret <value> to enable X-Atlas-Signature signing.[/{theme.text_dim}]"
        )
    return 0


@registry.register("continuous-audit", "notify", "list",
                   description="List configured notification channels")
def handle_notify_list(args: Namespace) -> int:
    env = _resolve_notify_env(args)
    if env is None:
        return 1
    channels = notifications.list_channels(env)
    if not channels:
        console.print(f"[{theme.text_dim}]No notification channels for env=[bold]{env}[/bold].[/{theme.text_dim}]")
        return 0

    table = Table(show_header=True, header_style=f"bold {theme.accent}")
    table.add_column("ID", style=theme.text_dim, no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Name")
    table.add_column("URL", overflow="fold")
    table.add_column("Signed", no_wrap=True)
    table.add_column("Enabled", no_wrap=True)

    for ch in channels:
        signed = "yes" if (ch.type == notifications.CHANNEL_TYPE_WEBHOOK and ch.secret) else "—"
        enabled = "yes" if ch.enabled else f"[{theme.warning}]no[/{theme.warning}]"
        table.add_row(ch.id, ch.type, ch.name, ch.url, signed, enabled)

    console.print(table)
    return 0


@registry.register("continuous-audit", "notify", "remove",
                   description="Remove a notification channel by ID")
def handle_notify_remove(args: Namespace) -> int:
    env = _resolve_notify_env(args)
    if env is None:
        return 1
    channel_id = str(getattr(args, "channel_id", "") or "")
    if not channel_id:
        console.print(f"[{theme.error}]Channel ID is required.[/{theme.error}]")
        return 1
    if notifications.remove_channel(env, channel_id):
        console.print(f"[{theme.success}]✓[/{theme.success}] Removed channel [bold]{channel_id}[/bold] from env=[bold]{env}[/bold].")
        return 0
    console.print(f"[{theme.warning}]⚠ Channel {channel_id} not found in env={env}.[/{theme.warning}]")
    return 1


@registry.register("continuous-audit", "notify", "test",
                   description="Send a synthetic test payload to a notification channel")
def handle_notify_test(args: Namespace) -> int:
    env = _resolve_notify_env(args)
    if env is None:
        return 1
    channel_id = str(getattr(args, "channel_id", "") or "")
    if not channel_id:
        console.print(f"[{theme.error}]Channel ID is required.[/{theme.error}]")
        return 1
    record = notifications.test_channel(env, channel_id)
    if record["ok"]:
        console.print(
            f"[{theme.success}]✓[/{theme.success}] Test delivered to "
            f"[bold]{record.get('channel_name') or channel_id}[/bold] "
            f"(status {record['status_code']}, {record['duration_ms']} ms)."
        )
        return 0
    err = record.get("error") or "unknown error"
    console.print(
        f"[{theme.error}]✗ Test send failed[/{theme.error}] for [bold]{channel_id}[/bold] "
        f"(status {record.get('status_code', 0)}): {err}"
    )
    return 1


# =======================================================================
# TODO: Log file watching — deferred to a later version of Atlas.
# The handlers below are preserved but not registered. To re-enable:
#   1. Uncomment this entire section
#   2. Uncomment LogWatchEntry / VALID_LOG_WATCH_THRESHOLDS / LOG_WATCH_SOURCES imports above
#   3. Uncomment the log-watch subparser in cli.py
#   4. Uncomment the engine.py log-watch block
#   5. Uncomment the WebUI routes and template section
# =======================================================================

# def _active_env_for_lw() -> str | None:
#     env = _active_env_or_complain()
#     return env
#
#
# @registry.register("continuous-audit", "log-watch", "list",
#                    description="List all log-watch pattern entries")
# def handle_lw_list(args: Namespace) -> int:
#     env = _active_env_for_lw()
#     if env is None:
#         return 1
#     settings = read_settings(env)
#     state = f"[{theme.success}]enabled[/{theme.success}]" if settings.log_watch_enabled else f"[{theme.text_dim}]disabled[/{theme.text_dim}]"
#     console.print(f"\n  Log watching: {state}  [{theme.text_dim}](env: {env})[/{theme.text_dim}]")
#     if not settings.log_watches:
#         console.print(f"  [{theme.text_dim}]No log-watch entries configured.[/{theme.text_dim}]")
#         console.print(f"  [{theme.text_dim}]Add one: platform-atlas continuous-audit log-watch add <pattern>[/{theme.text_dim}]\n")
#         return 0
#     table = Table(show_header=True)
#     table.add_column("ID", style="cyan", width=12)
#     table.add_column("Name", style="white")
#     table.add_column("Pattern", style="bold")
#     table.add_column("Source", width=10)
#     table.add_column("Threshold", width=16)
#     table.add_column("Severity", width=10)
#     table.add_column("On", width=4, justify="center")
#     for e in settings.log_watches:
#         thr = e.threshold_mode
#         if thr in ("count", "window"):
#             thr += f" ≥{e.threshold_count}"
#         if e.threshold_mode == "window":
#             thr += f" / {e.threshold_window_minutes}m"
#         table.add_row(
#             e.id[:12],
#             e.name or "-",
#             e.pattern,
#             e.log_source,
#             thr,
#             e.severity,
#             f"[{theme.success}]✓[/{theme.success}]" if e.enabled else f"[{theme.text_dim}]✗[/{theme.text_dim}]",
#         )
#     console.print(table)
#     console.print()
#     return 0
#
#
# @registry.register("continuous-audit", "log-watch", "enable",
#                    description="Enable log watching for the active environment")
# def handle_lw_enable(args: Namespace) -> int:
#     env = _active_env_for_lw()
#     if env is None:
#         return 1
#     if ctx().is_standard:
#         console.print(f"[{theme.warning}]Log watching requires Extended mode (SSH access to read log files).[/{theme.warning}]")
#         return 1
#     _replace_settings(env, log_watch_enabled=True)
#     console.print(f"[{theme.success}]✓[/{theme.success}] Log watching enabled for env [bold]{env}[/bold]")
#     return 0
#
#
# @registry.register("continuous-audit", "log-watch", "disable",
#                    description="Disable log watching for the active environment")
# def handle_lw_disable(args: Namespace) -> int:
#     env = _active_env_for_lw()
#     if env is None:
#         return 1
#     _replace_settings(env, log_watch_enabled=False)
#     console.print(f"[{theme.success}]✓[/{theme.success}] Log watching disabled for env [bold]{env}[/bold] (entries preserved)")
#     return 0
#
#
# @registry.register("continuous-audit", "log-watch", "add",
#                    description="Add a log-watch pattern entry")
# def handle_lw_add(args: Namespace) -> int:
#     import hashlib
#     import time as _time
#
#     env = _active_env_for_lw()
#     if env is None:
#         return 1
#
#     pattern = str(getattr(args, "pattern", "") or "").strip()
#     if not pattern:
#         console.print(f"[{theme.error}]Pattern is required.[/{theme.error}]")
#         return 1
#
#     lw_id = str(getattr(args, "lw_id", "") or "").strip()
#     if not lw_id:
#         lw_id = hashlib.md5(f"{pattern}:{_time.time()}".encode()).hexdigest()[:8]
#
#     name = str(getattr(args, "lw_name", "") or "").strip() or pattern[:40]
#     source = str(getattr(args, "lw_source", "any") or "any")
#     severity = str(getattr(args, "severity", "warning") or "warning")
#     threshold = str(getattr(args, "lw_threshold", "any") or "any")
#     count = int(getattr(args, "lw_count", 1) or 1)
#     window = int(getattr(args, "lw_window", 60) or 60)
#
#     new_entry = LogWatchEntry(
#         id=lw_id,
#         name=name,
#         pattern=pattern,
#         log_source=source,
#         severity=severity,
#         threshold_mode=threshold,
#         threshold_count=count,
#         threshold_window_minutes=window,
#         enabled=True,
#     )
#
#     settings = read_settings(env)
#     existing_ids = {e.id for e in settings.log_watches}
#     if lw_id in existing_ids:
#         console.print(f"[{theme.warning}]⚠ ID {lw_id!r} already exists — use a different --id[/{theme.warning}]")
#         return 1
#
#     updated = settings.log_watches + (new_entry,)
#     _replace_settings(env, log_watches=updated)
#     console.print(f"[{theme.success}]✓[/{theme.success}] Added log-watch [bold]{lw_id}[/bold]: {pattern!r}")
#     console.print(f"  [{theme.text_dim}]source={source}  threshold={threshold}  severity={severity}[/{theme.text_dim}]")
#     if not settings.log_watch_enabled:
#         console.print(f"  [{theme.text_dim}]Log watching is disabled — enable with: platform-atlas continuous-audit log-watch enable[/{theme.text_dim}]")
#     return 0
#
#
# @registry.register("continuous-audit", "log-watch", "remove",
#                    description="Remove a log-watch entry by ID")
# def handle_lw_remove(args: Namespace) -> int:
#     env = _active_env_for_lw()
#     if env is None:
#         return 1
#     lw_id = str(getattr(args, "lw_id", "") or "").strip()
#     if not lw_id:
#         console.print(f"[{theme.error}]Watch ID is required.[/{theme.error}]")
#         return 1
#     settings = read_settings(env)
#     remaining = tuple(e for e in settings.log_watches if e.id != lw_id)
#     if len(remaining) == len(settings.log_watches):
#         console.print(f"[{theme.warning}]⚠ No log-watch with ID {lw_id!r} found in env [bold]{env}[/bold][/{theme.warning}]")
#         return 1
#     _replace_settings(env, log_watches=remaining)
#     console.print(f"[{theme.success}]✓[/{theme.success}] Removed log-watch [bold]{lw_id}[/bold] from env [bold]{env}[/bold]")
#     return 0
