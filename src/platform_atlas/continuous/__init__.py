"""
Platform Atlas // Continuous Audit

A scheduled, narrow re-run of the audit pipeline that:
    - captures Platform OAuth only (no SSH, Mongo, Redis, IG, K8s),
    - validates against the active ruleset (rules with no Platform data are SKIP),
    - writes a bare JSON report consumable by external alert systems,
    - diffs this run's observed values against the previous run and surfaces
      drift events as internal alerts (bell icon in WebUI, banner in CLI).

The engine is callable from both surfaces:
    - CLI:     platform-atlas continuous-audit run-once
    - WebUI:   asyncio scheduler invokes ``run_once()`` in-process on an interval

Per-environment toggle. Always Platform-only regardless of the active tier.
"""

from platform_atlas.continuous.scope import Scope, require_platform_only
from platform_atlas.continuous.models import (
    ContinuousSettings,
    RunSummary,
    RunResult,
    DriftEvent,
    Alert,
    AlertStatus,
)
from platform_atlas.continuous.os_scheduler import TimerStatus

__all__ = [
    "Scope",
    "require_platform_only",
    "ContinuousSettings",
    "RunSummary",
    "RunResult",
    "DriftEvent",
    "Alert",
    "AlertStatus",
    "TimerStatus",
]
