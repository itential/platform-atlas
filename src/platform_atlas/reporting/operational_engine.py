# pylint: disable=line-too-long
"""
ATLAS // Operational Report Engine

Discovers and executes MongoDB aggregation pipelines from the user's
pipeline directory, collecting results into an OperationalReport that
can be serialized to JSON or rendered as HTML.

Each pipeline JSON file defines a single aggregation — the engine
runs them all against the connected database and records per-pipeline
results (including partial failures).

Usage:
    from platform_atlas.reporting.operational_engine import run_operational_pipelines

    with MongoCollector.from_config() as collector:
        report = run_operational_pipelines(collector)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from rich.console import Console

from platform_atlas.capture.collectors.mongo import MongoCollector
from platform_atlas.capture.utils import Pipeline, discover_pipelines
from platform_atlas.core.paths import ATLAS_PIPELINES_DIR
from platform_atlas.core import ui

logger = logging.getLogger(__name__)

console = Console()
theme = ui.theme


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────

@dataclass(slots=True)
class PipelineResult:
    """Result of a single pipeline execution."""
    name: str
    description: str
    collection: str
    rows: list[dict[str, Any]]
    row_count: int
    duration_ms: float
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def columns(self) -> list[str]:
        """Derive column names from the first row of results."""
        if not self.rows:
            return []
        return list(self.rows[0].keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "collection": self.collection,
            "rows": self.rows,
            "row_count": self.row_count,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(slots=True)
class OperationalReport:
    """Aggregated results from all pipeline executions."""
    results: list[PipelineResult] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    pipeline_count: int = 0
    success_count: int = 0
    error_count: int = 0

    def add(self, result: PipelineResult) -> None:
        """Register a pipeline result and update counters."""
        self.results.append(result)
        self.pipeline_count += 1
        if result.succeeded:
            self.success_count += 1
        else:
            self.error_count += 1

    @property
    def total_rows(self) -> int:
        """Total data rows across all successful pipelines."""
        return sum(r.row_count for r in self.results if r.succeeded)

    # Set by the engine if the user cancels mid-run
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "pipeline_count": self.pipeline_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "total_rows": self.total_rows,
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self, path: Path, indent: int = 2) -> None:
        """Serialize the full report to a JSON file."""
        path.write_text(
            json.dumps(self.to_dict(), indent=indent, default=str, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def from_json(cls, path: Path) -> "OperationalReport":
        """Deserialize a previously saved operational report from JSON."""
        data = json.loads(path.read_text(encoding="utf-8"))
        results = [
            PipelineResult(
                name=r["name"],
                description=r.get("description", ""),
                collection=r.get("collection", ""),
                rows=r.get("rows", []),
                row_count=r.get("row_count", 0),
                duration_ms=r.get("duration_ms", 0.0),
                error=r.get("error"),
            )
            for r in data.get("results", [])
        ]
        report = cls(results=results)
        report.pipeline_count = data.get("pipeline_count", len(results))
        report.success_count = data.get("success_count", sum(1 for r in results if r.succeeded))
        report.error_count = data.get("error_count", sum(1 for r in results if not r.succeeded))
        report.generated_at = data.get("generated_at", report.generated_at)
        return report


# ─────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────

def run_operational_pipelines(
    collector: MongoCollector,
    pipeline_dir: Path | None = None,
) -> OperationalReport:
    """
    Discover and execute all operational pipelines.

    Shows per-pipeline progress to the user and handles Ctrl+C
    gracefully — any pipelines that completed before the interrupt
    are kept in the report. The user is informed they can cancel
    at any time.

    Args:
        collector: An already-connected MongoCollector instance.
        pipeline_dir: Override pipeline directory (defaults to ATLAS_PIPELINES_DIR).

    Returns:
        OperationalReport with results from each pipeline.
        Check report.cancelled to know if the run was interrupted.
    """
    directory = pipeline_dir or ATLAS_PIPELINES_DIR
    pipelines = discover_pipelines(directory)

    if not pipelines:
        logger.warning("No operational pipelines found in %s", directory)
        return OperationalReport()

    total = len(pipelines)
    logger.info("Found %d operational pipeline(s) in %s", total, directory)

    console.print(
        f"  [{theme.text_dim}]Running {total} pipeline(s) from {directory}[/{theme.text_dim}]"
    )
    console.print(
        f"  [{theme.text_dim}]Press Ctrl+C to cancel — completed pipelines will be kept[/{theme.text_dim}]\n"
    )

    report = OperationalReport()

    for idx, pipeline in enumerate(pipelines, 1):
        prefix = f"  [{idx}/{total}]"

        try:
            result = _execute_pipeline(collector, pipeline, prefix)
        except KeyboardInterrupt:
            console.print(
                f"\n  [{theme.warning}]Cancelled by user after "
                f"{report.pipeline_count}/{total} pipeline(s)[/{theme.warning}]"
            )
            report.cancelled = True
            break

        report.add(result)

    logger.info(
        "Operational pipelines complete: %d/%d succeeded (%d total rows)",
        report.success_count,
        report.pipeline_count,
        report.total_rows,
    )

    return report


def _friendly_error(error: Exception) -> str:
    """Extract a clean, user-facing message from a pipeline exception.

    MongoDB errors wrap a details dict with 'errmsg' and 'codeName'.
    Atlas exceptions wrap those further with a prefix like
    "Pipeline 'X' failed: {...}". This function digs through the
    layers and returns just the meaningful part.
    """
    msg = str(error)

    # Atlas exceptions wrap the raw dict — try to pull errmsg from it
    # Pattern: "Pipeline '...' failed: {'ok': 0.0, 'errmsg': '...', ...}"
    if "errmsg" in msg:
        import re
        match = re.search(r"'errmsg':\s*'([^']+)'", msg)
        if match:
            errmsg = match.group(1)
            # Strip the verbose MongoDB prefix
            for prefix in (
                "PlanExecutor error during aggregation :: caused by :: ",
                "command failed :: caused by :: ",
            ):
                if errmsg.startswith(prefix):
                    errmsg = errmsg[len(prefix):]
            return errmsg

    # QueryTimeoutError — already has a clean message
    if "exceeded" in msg and "timeout" in msg.lower():
        return msg

    # Fallback: strip the "Pipeline 'X' failed: " wrapper if present
    if "failed:" in msg:
        return msg.split("failed:", 1)[1].strip()

    return msg


def _execute_pipeline(
    collector: MongoCollector,
    pipeline: Pipeline,
    prefix: str = "",
) -> PipelineResult:
    """Execute a single pipeline with a spinner and status output.

    While the pipeline is running, the user sees a spinner with the
    pipeline name. On completion, a summary line replaces the spinner
    showing row count and duration. If the user presses Ctrl+C during
    execution, the KeyboardInterrupt propagates up to the main loop
    which decides whether to keep partial results.
    """
    start = perf_counter()

    with console.status(
        f"{prefix} [bold]{pipeline.name}[/bold]  "
        f"[{theme.text_dim}]→ {pipeline.collection}[/{theme.text_dim}]",
        spinner="dots",
    ):
        try:
            rows = collector.run_pipeline(pipeline)
            duration = (perf_counter() - start) * 1000

            logger.debug(
                "Pipeline '%s' returned %d rows in %.1f ms",
                pipeline.name, len(rows), duration,
            )

            console.print(
                f"{prefix} [{theme.success}]✓[/{theme.success}] {pipeline.name}  "
                f"[{theme.text_dim}]{len(rows)} rows · {duration:.0f} ms[/{theme.text_dim}]"
            )

            return PipelineResult(
                name=pipeline.name,
                description=pipeline.desc,
                collection=pipeline.collection,
                rows=rows,
                row_count=len(rows),
                duration_ms=round(duration, 2),
            )

        except KeyboardInterrupt:
            # Re-raise so the outer loop handles it — print a
            # cancellation note so the spinner doesn't leave a gap
            duration = (perf_counter() - start) * 1000
            console.print(
                f"{prefix} [{theme.warning}]⚠[/{theme.warning}] {pipeline.name}  "
                f"[{theme.text_dim}]cancelled after {duration:.0f} ms[/{theme.text_dim}]"
            )
            raise

        except Exception as e:
            duration = (perf_counter() - start) * 1000
            friendly = _friendly_error(e)

            # Full details go to the log file only — not the console
            logger.debug("Pipeline '%s' failed after %.1f ms: %s", pipeline.name, duration, e)

            console.print(
                f"{prefix} [{theme.error}]✗[/{theme.error}] {pipeline.name}  "
                f"[{theme.text_dim}]{friendly} ({duration:.0f} ms)[/{theme.text_dim}]"
            )

            return PipelineResult(
                name=pipeline.name,
                description=pipeline.desc,
                collection=pipeline.collection,
                rows=[],
                row_count=0,
                duration_ms=round(duration, 2),
                error=friendly,
            )
