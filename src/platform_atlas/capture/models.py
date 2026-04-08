"""
ATLAS // Capture Dataclasses
"""

from __future__ import annotations

import logging
import platform
from time import time
from socket import gethostname
from typing import Any, Callable
from dataclasses import dataclass, asdict, field
from enum import Enum, auto

import psutil

# ATLAS Imports
from platform_atlas.core._version import __version__

logger = logging.getLogger(__name__)

@dataclass
class SystemFacts:
    """Basic system information captured for validation"""
    hostname: str
    cpu_count: int
    cpu_count_logical: int
    total_memory_bytes: int
    platform: str
    architecture: str

    @classmethod
    def capture_facts(cls, system_data: dict | None = None) -> "SystemFacts":
        """Capture system facts from collected data or local psutil"""
        if system_data:
            # Pull from already-collected system module data
            cpu = system_data.get("cpu", {})
            mem = system_data.get("memory", {})
            host = system_data.get("host", {})
            virtual = mem.get("virtual", {})

            return cls(
                hostname=host.get("hostname", "unknown"),
                cpu_count=cpu.get("cores_physical") or 1,
                cpu_count_logical=cpu.get("cores_logical") or 1,
                total_memory_bytes=virtual.get("total", 0),
                platform=system_data.get("os", {}).get("system", "unknown").lower(),
                architecture=system_data.get("os", {}).get("machine", "unknown"),
            )

        # Local fallback
        return cls(
            hostname=str(gethostname()),
            cpu_count=psutil.cpu_count(logical=False) or 1,
            cpu_count_logical=psutil.cpu_count(logical=True) or 1,
            total_memory_bytes=psutil.virtual_memory().total,
            platform=platform.system().lower(),
            architecture=platform.machine(),
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
