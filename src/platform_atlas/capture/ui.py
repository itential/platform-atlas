"""
Platform Atlas // Capture Engine
"""

from __future__ import annotations

import json
import logging
import warnings
from time import time
from typing import Any

from rich.console import Group
from rich.spinner import Spinner
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.syntax import Syntax
from rich.progress_bar import ProgressBar

# ATLAS Imports
from platform_atlas.core._version import __version__

from platform_atlas.capture.models import (
    ModuleStatus,
    ModuleResult,
    CaptureState
)

from platform_atlas.core import ui

logger = logging.getLogger(__name__)

theme = ui.theme

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UI Rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CaptureUI:
    """Handles all Rich rendering for the capture process"""

    def __init__(self, state: CaptureState):
        self.state = state
        self.theme = theme
        self._icons = {
            ModuleStatus.PENDING: ("○", self.theme.text_ghost),
            ModuleStatus.RUNNING: ("●", self.theme.primary_glow),
            ModuleStatus.SUCCESS: ("✓", self.theme.success_glow),
            ModuleStatus.FAILED: ("✗", self.theme.error_glow),
            ModuleStatus.SKIPPED: ("◌", self.theme.warning_dim),
            ModuleStatus.DEFERRED: ("◌", self.theme.text_dim),
    }

    def __repr__(self) -> str:
        progress = f"{self.state.completed_count}/{self.state.total_count}"
        return f"<CaptureUI progress={progress}>"

    def _panel_height(self) -> int:
        """Calculate consistent panel height based on module content"""
        base = 8
        modules = self.state.total_count
        return base + modules

    def _render_module_row(self, module: ModuleResult) -> Text:
        """Render a single module's status row"""
        text = Text()

        # Module name
        name_style = f"bold {self.theme.primary_glow}" if module.status == ModuleStatus.RUNNING else ""
        text.append(f"{module.name:<20}", style=name_style)

        # Transport badge
        badge = module.transport_type.upper()
        host = module.target_name or ""

        match module.transport_type:
            case "ssh":
                text.append(f" {badge} ", style=f"bold {self.theme.secondary}")
                if host and host != "local":
                    text.append(f"({host}) ", style=f"dim {self.theme.secondary_dim}")
            case "pymongo" | "redis-py" | "oauth/http":
                text.append(f" {badge} ", style=f"bold {self.theme.accent}")
            case t if "/" in t:
                # Alt_path protocol fallback (e.g., "redis-py/config", "pymongo/config")
                text.append(f" {badge} ", style=f"bold {self.theme.accent}")
            case "manual":
                text.append(f" {badge} ", style=f"{self.theme.warning}")
            case _:
                text.append(f" {badge} ", style=f"{self.theme.text_dim}")

        match module.status:
            case ModuleStatus.PENDING:
                text.append("Pending", style=self.theme.text_muted)
            case ModuleStatus.RUNNING:
                text.append("Collecting...", style=f"bold {self.theme.primary_glow}")
            case ModuleStatus.SUCCESS:
                text.append("Complete ", style=self.theme.success_glow)
                if module.duration_ms:
                    if module.duration_ms < 10000:
                        dur_style = self.theme.success_glow
                    elif module.duration_ms < 30000:
                        dur_style = self.theme.warning
                    else:
                        dur_style = self.theme.error
                    text.append(f"({module.duration_ms:.0f}ms)", style=dur_style)
            case ModuleStatus.FAILED:
                text.append("✘", style=self.theme.error)
            case ModuleStatus.SKIPPED:
                text.append("◌", style=self.theme.warning_dim)
            case ModuleStatus.DEFERRED:
                text.append("SSH unavailable", style=f"italic {self.theme.text_dim}")

        return text

    def _render_status_footer(self) -> Panel:
        """Always-present footer - shows errors/warnings or a clean status line"""
        rows = []
        if self.state.errors:
            for module_name, error_msg in self.state.errors:
                display_msg = error_msg if len(error_msg) <= 100 else error_msg[:97] + "..."
                row = Text()
                row.append("✘ ", style=self.theme.error_glow)
                row.append(f"{module_name:<20}", style=self.theme.error_glow)
                row.append(display_msg, style=self.theme.text_primary)
                rows.append(row)

        if self.state.warnings:
            seen = set()
            for category, msg in self.state.warnings:
                key = (category, msg)
                if key in seen:
                    continue
                seen.add(key)
                display_msg = msg if len(msg) <= 100 else msg[:97] + "..."
                row = Text()
                row.append("⚠ ", style=self.theme.warning)
                row.append(f"{category:<20}", style=self.theme.warning_dim)
                row.append(display_msg, style=self.theme.text_primary)
                rows.append(row)

        if not rows:
            rows.append(Text("No issues detected", style=self.theme.text_dim))

        content = Group(*rows)

        title = "⚠ ISSUES" if (self.state.errors or self.state.warnings) else "STATUS"
        border = self.theme.border_secondary if self.state.errors else self.theme.border_primary

        return Panel(
            content,
            title=f"[bold]{title}[/bold]",
            title_align="left",
            border_style=border,
            padding=(0, 2),
            height=6,
        )

    def render_progress_panel(self) -> Panel:
        """Build the main progress panel showing all modules"""
        table = Table.grid(padding=(0, 1))
        table.add_column("spinner", width=2)
        table.add_column("status")

        for module in self.state.modules.values():
            if module.status == ModuleStatus.RUNNING:
                # Create a new spinner for the running module
                spinner = Spinner("dots", style=f"bold {self.theme.spinner_color}")
                table.add_row(spinner, self._render_module_row(module))
            else:
                icon, style = self._icons[module.status]
                table.add_row(
                    Text(f"{icon}", style=style),
                    self._render_module_row(module)
                )

        footer = Text()
        footer.append(
            f"\n{self.state.completed_count}/{self.state.total_count} modules complete",
            style=self.theme.text_muted
        )
        if self.state.failed_count > 0:
            footer.append(f"  •  ", style=self.theme.text_muted)
            footer.append(f"{self.state.failed_count} failed", style=self.theme.error)
        content = Group(
            self.render_progress_bar(),
            Text(),
            table,
            footer
        )

        return Panel(
            content,
            title="[bold]⧗ CAPTURE PROGRESS[bold]",
            title_align="left",
            border_style=self.theme.border_primary,
            padding=(1, 2),
        )

    def render_error_panel(self) -> Panel | None:
        """Build the error panel. Returns None if no errors"""
        if not self.state.errors:
            return None

        error_table = Table.grid(padding=(0, 1))
        error_table.add_column("module", style=self.theme.error_glow, width=20)
        error_table.add_column("message", style=self.theme.text_primary)

        for module_name, error_msg in self.state.errors:
            display_msg = error_msg if len(error_msg) <= 120 else error_msg[:117] + "..."
            error_table.add_row(module_name, display_msg)

        return Panel(
            error_table,
            title=f"[bold]⚠ ERRORS ({len(self.state.errors)})[/bold]",
            title_align="left",
            border_style=self.theme.border_secondary,
            padding=(0, 2),
        )

    def render_warning_panel(self) -> Panel | None:
        """Build the warning panel. Returns None if no warnings"""
        if not self.state.warnings:
            return None

        warning_table = Table.grid(padding=(0, 1))
        warning_table.add_column("module", style=self.theme.error_glow, width=20)
        warning_table.add_column("message", style=self.theme.text_primary)

        # Deduplicate warnings (same message might fire multiple times)
        seen = set()
        unique_warnings = []
        for category, msg in self.state.warnings:
            key = (category, msg)
            if key not in seen:
                seen.add(key)
                unique_warnings.append((category, msg))

        for category, warning_msg in unique_warnings:
            display_msg = warning_msg if len(warning_msg) <= 120 else warning_msg[:117] + "..."
            warning_table.add_row(category, display_msg)

        return Panel(
            warning_table,
            title=f"[bold]⚠ WARNINGS ({len(unique_warnings)})[/bold]",
            title_align="left",
            border_style=self.theme.border_secondary,
            padding=(0, 2),
        )

    def render_preview_panel(self) -> Panel:
        """Show a previous of the last collected data"""
        name, data = self.state.last_result

        # Wrap non-dict results so the preview always shows valid JSON
        if not isinstance(data, (dict, list)):
            data = {"result": data}

        # Create a truncated preview of the data
        def truncate_data(obj: Any, max_depth: int = 2, current_depth: int = 0) -> Any:
            """Recursively truncate nested data for preview"""
            if current_depth >= max_depth:
                if isinstance(obj, dict):
                    return f"{{...{len(obj)} keys}}"
                elif isinstance(obj, list):
                    return f"[...{len(obj)} items]"
                return obj

            if isinstance(obj, dict):
                truncated = {}
                for i, (k, v) in enumerate(obj.items()):
                    if i > 5: # Max 5 keys shown
                        truncated["..."] = f"+{len(obj) - 5} more"
                        break
                    truncated[k] = truncate_data(v, max_depth, current_depth + 1)
                return truncated
            elif isinstance(obj, list):
                if len(obj) > 3:
                    return [truncate_data(obj[0], max_depth, current_depth + 1), "...", f"+{len(obj) - 1} more"]
                return [truncate_data(item, max_depth, current_depth + 1) for item in obj]
            elif isinstance(obj, str) and len(obj) > 50:
                return obj[:47] + "..."
            return obj

        preview_data = truncate_data(data)

        try:
            preview_text = json.dumps(preview_data, indent=2, default=str)
            # Limit total lines
            lines = preview_text.split('\n')
            if len(lines) > 12:
                preview_text = '\n'.join(lines[:12]) + '\n  ...\n}'
        except Exception:
            preview_text = str(preview_data)[:200]

        syntax = Syntax(preview_text, "json", theme="monokai", line_numbers=False)

        return Panel(
            syntax,
            title=f"[bold]⛁ LAST COLLECTED: {name}[/bold]",
            title_align="left",
            border_style=self.theme.border_primary,
            padding=(0, 1),
            height=self._panel_height(),
        )

    def render_progress_bar(self) -> Table:
        """Render progress bar with Rich ProgressBar widget"""
        completed = self.state.completed_count
        total = self.state.total_count or 1

        bar_table = Table.grid(padding=(0, 1))
        bar_table.add_column(width=50)  # Progress bar
        bar_table.add_column(width=50)  # Percentage
        bar_table.add_column(width=50)  # Count
        bar_table.add_column()          # Elapsed

        progress_bar = ProgressBar(
            total=total,
            completed=completed,
            width=50,
            complete_style=self.theme.progress_complete,
            finished_style=self.theme.progress_success,
        )

        percent = (completed / total) * 100 if total > 0 else 0
        elapsed = time() - self.state.start_time if self.state.start_time else 0

        bar_table.add_row(
            progress_bar,
            Text(f"{percent:>3.0f}%", style=f"bold {self.theme.primary}"),
            Text(f"({completed}/{total})", style=self.theme.text_muted),
            Text(f"{elapsed:.1f}s", style=f"dim {self.theme.primary_dim}"),
        )

        return bar_table

    def render(self) -> Group:
        """Render the complete UI layout"""
        components = []

        # === TOP ROW: Progress + Preview ===
        main_layout = Table.grid(padding=(1, 2))
        main_layout.add_column(width=80) # Progress panel (left)
        main_layout.add_column(width=80) # Preview panel (right)

        # Left: Progress panel
        progress_panel = self.render_progress_panel()

        # Right: Preview panel
        if self.state.last_result:
            preview_panel = self.render_preview_panel()
        else:
            # Empty placeholder to maintain layout
            preview_panel = Panel(
                Text("Waiting for data...", style=self.theme.text_dim),
                title="[bold]LIVE PREVIEW[/bold]",
                title_align="left",
                border_style=self.theme.border_primary,
                padding=(1, 2),
                height=self._panel_height()
            )
        main_layout.add_row(progress_panel, preview_panel)
        components.append(main_layout)

        # === BOTTOM ROW: Warnings and Errors ===
        components.append(self._render_status_footer())

        return Group(*components)

class WarningCapture:
    """Context manager to capture warnings and add them to CaptureState"""

    def __init__(self, state: CaptureState):
        self.state = state
        self._catch_warnings = None

    def __enter__(self):
        self._catch_warnings = warnings.catch_warnings(record=True)
        self._caught = self._catch_warnings.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Process any caught warnings before exiting
        self.process_warnings()
        return self._catch_warnings.__exit__(exc_type, exc_val, exc_tb)

    def process_warnings(self) -> None:
        """Transfer caught warnings to the CaptureState"""
        for w in self._caught:
            category = w.category.__name__
            message = str(w.message)
            self.state.add_warning(category, message)
        # Clear processed warnings
        self._caught.clear()
