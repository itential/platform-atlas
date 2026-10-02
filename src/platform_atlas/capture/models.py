"""
ATLAS // Capture Dataclasses
"""

from __future__ import annotations

import logging
from time import time
from typing import Any, Callable
from dataclasses import dataclass, asdict, field
from enum import Enum, auto

# ATLAS Imports
from platform_atlas.core._version import __version__

logger = logging.getLogger(__name__)

@dataclass
class SystemFacts:
    """Basic system information about the TARGET, captured for validation.

    Only ever derived from collected target system data — never from the
    machine running Atlas (that would silently audit the auditor's laptop).
    Fields the target data did not provide are ``None`` so rules that
    reference them (e.g. IG-003's ``cpu_count_logical``) SKIP instead of
    comparing against a made-up value.
    """
    hostname: str | None
    cpu_count: int | None
    cpu_count_logical: int | None
    total_memory_bytes: int | None
    platform: str | None
    architecture: str | None

    @classmethod
    def capture_facts(cls, system_data: dict | None = None) -> "SystemFacts | None":
        """Build facts from collected system-module data; ``None`` if there is none."""
        if not system_data or not isinstance(system_data, dict):
            return None

        cpu = system_data.get("cpu") or {}
        mem = system_data.get("memory") or {}
        host = system_data.get("host") or {}
        virtual = mem.get("virtual") or {}
        os_info = system_data.get("os") or {}

        # A section holding only non-facts (e.g. system.kubernetes) is not system data
        if not (cpu or mem or host or os_info):
            return None

        system_name = os_info.get("system")
        return cls(
            hostname=host.get("hostname") or None,
            cpu_count=cpu.get("cores_physical") or None,
            cpu_count_logical=cpu.get("cores_logical") or None,
            total_memory_bytes=virtual.get("total") or None,
            platform=system_name.lower() if isinstance(system_name, str) and system_name else None,
            architecture=os_info.get("machine") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Returns dict of capture_facts"""
        return asdict(self)

class ModuleStatus(Enum):
    """Status states for capture modules"""
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    SKIPPED = auto()
    DEFERRED = auto()    # Awaiting protocol fallback — resolved post-capture

@dataclass(slots=True)
class ModuleResult:
    """Tracks the state the result of a single module"""
    name: str
    status: ModuleStatus = ModuleStatus.PENDING
    error_message: str | None = None
    duration_ms: float | None = None
    transport_type: str = "local"
    target_name: str | None = None

@dataclass(frozen=True, slots=True)
class ResolvedModules:
    """Result of resolving which modules to run"""
    modules: dict[str, Callable]
    transport_map: dict[str, tuple[str, str]]
    is_subset: bool
    deferred_ssh_modules: tuple[str, ...] = ()
    ssh_fallbacks: dict[str, Callable] = field(default_factory=dict)
    target_errors: list[tuple[str, str]] = field(default_factory=list)
    # Module callables demoted from the canonical `modules` slot because
    # another node of the same role (or an explicitly-namespaced Kubernetes
    # target) already claimed that module name. Keyed by target name, then
    # module name — these still run, but write to their own bucket instead
    # of overwriting/being overwritten in the single flat capture path.
    multi_target_modules: dict[str, dict[str, Callable]] = field(default_factory=dict)

@dataclass(slots=True)
class CaptureState:
    """Central state tracker for the capture process"""
    modules: dict[str, ModuleResult] = field(default_factory=dict)
    running_subset: bool = False # Track user-selected modules
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    current_module: str | None = None
    start_time: float | None = None
    last_result: tuple[str, dict] | None = None

    def begin(self) -> None:
        """Mark capture start time"""
        self.start_time = time()

    def register_module(
            self,
            name: str,
            transport_type: str = "local",
            target_name: str | None = None,
    ) -> None:
        """Register a module as pending"""
        self.modules[name] = ModuleResult(
            name=name,
            status=ModuleStatus.PENDING,
            transport_type=transport_type,
            target_name=target_name,
            )

    def start_module(self, name: str) -> None:
        """Mark a module as currently running"""
        self.current_module = name
        if name in self.modules:
            self.modules[name].status = ModuleStatus.RUNNING

    def complete_module(self, name: str, duration_ms: float, result: dict | None = None) -> None:
        """Mark a module as successfully completed"""
        if name in self.modules:
            self.modules[name].status = ModuleStatus.SUCCESS
            self.modules[name].duration_ms = duration_ms
        if self.current_module == name:
            self.current_module = None

        if result:
            self.last_result = (name, result)

    def fail_module(self, name: str, error: str, duration_ms: float) -> None:
        """Mark a module as failed and record the error"""
        if name in self.modules:
            self.modules[name].status = ModuleStatus.FAILED
            self.modules[name].error_message = error
            self.modules[name].duration_ms = duration_ms
        self.errors.append((name, error))
        if self.current_module == name:
            self.current_module = None

    def add_warning(self, category: str, message: str) -> None:
        """Add a warning to the state"""
        self.warnings.append((category, message))

    @property
    def completed_count(self) -> int:
        return sum(
            1 for m in self.modules.values()
            if m.status in (ModuleStatus.SUCCESS, ModuleStatus.FAILED,
                            ModuleStatus.SKIPPED, ModuleStatus.DEFERRED)
        )

    @property
    def total_count(self) -> int:
        return len(self.modules)

    @property
    def successful_module_names(self) -> list[str]:
        """List of successful module names for metadata"""
        successful = [
            name for name, result in self.modules.items()
            if result.status == ModuleStatus.SUCCESS
        ]

        # Only say "all" if we ran everything AND all succeeded
        if not self.running_subset and len(successful) == self.total_count:
            return ["all"]
        return successful

    @property
    def success_count(self) -> int:
        return sum(1 for m in self.modules.values() if m.status == ModuleStatus.SUCCESS)

    @property
    def failed_count(self) -> int:
        return sum(1 for m in self.modules.values() if m.status == ModuleStatus.FAILED)

    @property
    def failed_modules_summary(self) -> list[dict[str, str]]:
        """Name + error_message for every failed module.

        Persisted to ``_atlas.metadata.failed_modules`` so post-capture
        consumers (e.g. the WebUI session detail page) can introspect
        failures without re-running the engine.
        """
        return [
            {"name": name, "error_message": result.error_message or ""}
            for name, result in self.modules.items()
            if result.status == ModuleStatus.FAILED
        ]

    @property
    def module_manifest(self) -> list[dict[str, Any]]:
        """Every module's final name/status/duration/error/transport.

        Persisted to ``_atlas.metadata.module_manifest`` so a LATER phase
        (Validate/Report, in a separate process — capture's own live
        ``CaptureState`` doesn't survive past ``run_capture()`` returning)
        can show the finished Capture card with the exact same per-module
        detail it had while capturing, instead of a coarser reconstruction
        from the capture JSON's own top-level section keys (which aren't
        module-shaped — see ``pipeline_ui.capture_state_from_json``).
        """
        return [
            {
                "name": name,
                "status": result.status.name,
                "duration_ms": result.duration_ms,
                "error_message": result.error_message or "",
                "transport_type": result.transport_type,
            }
            for name, result in self.modules.items()
        ]
