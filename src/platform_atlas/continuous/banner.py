"""
Always-on CLI banner.

Printed at the top of every Platform Atlas invocation while continuous audit
is enabled for the active environment. Single line, color-coded by status.
The user wanted a constant reminder — this is intentionally not dismissible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from rich.console import Console

from platform_atlas.core import ui
from platform_atlas.continuous import alerts, storage
from platform_atlas.continuous.runtime import read_settings

logger = logging.getLogger(__name__)
_console = Console()
theme = ui.theme


def _humanize_age(iso_ts: str | None) -> str:
    """Render '12m ago' / '2h ago' / 'just now' from an ISO 8601 UTC string."""
    if not iso_ts:
        return "never"
    try:
        # Accept both "...Z" and "...+00:00" forms.
        ts = iso_ts.rstrip("Z")
        when = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return iso_ts
    delta = datetime.now(timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 30:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _staleness(status: dict[str, Any], interval_seconds: int) -> str:
    """Decide ON / STALE / FAILING from the heartbeat."""
    if not status:
        return "PENDING"
    if status.get("last_status") == "error":
        return "FAILING"
    last_finished = status.get("last_finished_at")
    if not last_finished:
        return "PENDING"
    try:
        ts = last_finished.rstrip("Z")
        when = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return "PENDING"
    age = (datetime.now(timezone.utc) - when).total_seconds()
    # 2× interval is the threshold for "we expected another run by now".
    if age > max(2 * interval_seconds, 600):
        return "STALE"
    return "ON"


def print_banner(environment: str | None) -> None:
    """Print a one-line reminder when continuous audit is enabled for ``environment``.

    Quietly returns when:
        - the env is empty,
        - settings are not enabled,
        - any internal lookup raises (banner is decorative; never blocks startup).
    """
    if not environment:
        return
    try:
        settings = read_settings(environment)
        if not settings.enabled:
            return
        status = storage.read_status(environment)
        unacked = alerts.counts(environment).get("unacked", 0)
    except Exception as exc:  # noqa: BLE001 — banner failures must never crash the CLI
        logger.debug("Continuous banner skipped: %s", exc)
        return

    state = _staleness(status, settings.interval_seconds)
    last_age = _humanize_age(status.get("last_finished_at"))
    color = {
        "ON": theme.success,
        "STALE": theme.warning,
        "FAILING": theme.error,
        "PENDING": theme.info,
    }[state]
    alert_phrase = (
        f" · [bold {theme.error}]{unacked} unacked alert{'s' if unacked != 1 else ''}[/bold {theme.error}]"
        if unacked else ""
    )

    _console.print(
        f"[bold {color}]⚡ Continuous audit:[/bold {color}] "
        f"[{color}]{state}[/{color}] · env=[bold]{environment}[/bold] "
        f"· last run {last_age}{alert_phrase}",
        highlight=False,
    )
