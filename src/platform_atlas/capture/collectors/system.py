# pylint: disable=no-member
"""
System Collector - Lightweight, read-only data collection from the Linux OS

This module provides a small Linux client wrapper optimized for metrics gathering.

Example:
    >>> collector = SystemCollector.from_config()
    >>> with collector:
    ...     info = collector.get_info()
"""

from __future__ import annotations

import os
import platform
import socket
import time
import logging
from typing import Any, Callable

import psutil

from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.transport import Transport, LocalTransport

logger = logging.getLogger(__name__)

class SystemInfoCollector:
    """
    Simple system collector using psutil + platform
    """

    def __init__(
            self,
            *,
            transport: Transport | None = None,
            include_per_cpu: bool = False,
            include_disks: bool = True,
            include_network_addrs: bool = True,
            include_network_io: bool = True,
            include_top_processes: int = 0,
            cpu_percent_interval: float = 0.0,
    ) -> None:
        self._transport = transport or LocalTransport()
        self.include_per_cpu = include_per_cpu
        self.include_disks = include_disks
        self.include_network_addrs = include_network_addrs
        self.include_network_io = include_network_io
        self.include_top_processes = include_top_processes
        self.cpu_percent_interval = cpu_percent_interval

    def __repr__(self) -> str:
        transport = type(self._transport).__name__ if hasattr(self, '_transport') else "local"
        return f"<SystemInfoCollector transport={transport}>"

    def get_system_info(self) -> dict[str, Any]:
        if not isinstance(self._transport, LocalTransport):
            return self._collect_remote_system_info()

        def _safe(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception:
                return default

        def _asdict_or_value(x: Any) -> Any:
            # psutil returns namedtuples with _asdict(); convert those to JSON
            if hasattr(x, "_asdict"):
                return x._asdict()
            return x

        info: dict[str, Any] = {
            "meta": {
                "ts": time.time(),
                "pid": os.getpid(),
            },
            "host": {
                "hostname": _safe(socket.gethostname),
                "fqdn": _safe(socket.getfqdn),
            },
            "os": {
                "system": _safe(platform.system),
                "release": _safe(platform.release),
                "version": _safe(platform.version),
                "platform": _safe(platform.platform),
                "machine": _safe(platform.machine),
                "arch": _safe(platform.architecture),
            },
            "cpu": {
                "cores_physical": _safe(psutil.cpu_count, logical=False),
                "cores_logical": _safe(psutil.cpu_count, logical=True),
                "percent_total": _safe(psutil.cpu_percent, self.cpu_percent_interval, default=None),
                "loadavg": _safe(os.getloadavg, default=None),
            },
            "memory": {
                "virtual": _safe(lambda: _asdict_or_value(psutil.virtual_memory()), default=None),
                "swap": _safe(lambda: _asdict_or_value(psutil.swap_memory()), default=None),
            },
        }

        if self.include_per_cpu:
            info["cpu"]["percent_per_cpu"] = _safe(
                psutil.cpu_percent, self.cpu_percent_interval, percpu=True, default=None
            )

        if self.include_disks:
            info["disks"] = _safe(self._collect_disks, default=None)

        if self.include_network_addrs:
            info["network_addrs"] = _safe(self._collect_net_addrs, default=None)

        if self.include_network_io:
            info["network_io"] = _safe(lambda: _asdict_or_value(psutil.net_io_counters()), default=None)

        if self.include_top_processes and self.include_top_processes > 0:
            info["top_processes_rss"] = _safe(
                self._collect_top_processes_rss, self.include_top_processes, default=[]
            )

        return info

    def _collect_disks(self) -> dict[str, Any]:
        """Returns usage for mountpoints found"""

        disks: dict[str, Any] = {}
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint
            try:
                disks[mp] = psutil.disk_usage(mp)._asdict()
            except Exception:
                logger.debug("Cannot access mount point: %s", mp)
                continue
        # Add disk IO counters too
        try:
            disks["_io_counters"] = psutil.disk_io_counters()._asdict()
        except Exception:
            logger.debug("Failed to collect disk IO counters", exc_info=True)
        return disks

    def _collect_net_addrs(self) -> dict[str, Any]:
        """Returns interface addresses, skipping loopback if needed"""
        out: dict[str, Any] = {}
        stats = psutil.net_if_stats()

        for ifname, addrs in psutil.net_if_addrs().items():
            # Skip loopback
            try:
                if "loopback" in (stats.get(ifname).flags or ""):
                    continue
            except Exception:
                pass

            out[ifname] = []
            for a in addrs:
                try:
                    out[ifname].append(a._asdict())
                except Exception:
                    # if psutil changes type on some platforms
                    out[ifname].append(
                        {
                            "family": str(getattr(a, "family", None)),
                            "address": str(getattr(a, "address", None)),
                            "netmask": str(getattr(a, "netmask", None)),
                            "broadcast": str(getattr(a, "broadcast", None)),
                            "ptp": str(getattr(a, "ptp", None)),
                        }
                    )
        return out

    def _collect_top_processes_rss(self, n: int) -> list[dict[str, Any]]:
        """Top N processes by RSS memory"""
        procs: list[dict[str, Any]] = []
        for p in psutil.process_iter(attrs=["pid", "name", "username", "memory_info"]):
            try:
                mi = p.info.get("memory_info")
                rss = mi.rss if mi else 0
                procs.append(
                    {
                        "pid": p.info.get("pid"),
                        "name": p.info.get("name"),
                        "username": p.info.get("username"),
                        "rss_bytes": rss,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda x: x.get("rss_bytes", 0), reverse=True)
        return procs[:n]

    def _collect_remote_system_info(self) -> dict[str, Any]:
        """Collect system info via shel commands over transport"""
        def _cmd(*args: str) -> str:
            command = " ".join(args)
            result = self._transport.run_command(command)
            result.check()
            return result.stdout.strip()

        hostname = _cmd("hostname")
        fqdn = _cmd("hostname", "-f")
        uname = _cmd("uname", "-a")
        cpu_count = _cmd("nproc")
        os_system = _cmd("uname", "-s")
        machine = _cmd("uname", "-m")

        # Memory from /proc/meminfo
        meminfo_raw = self._transport.read_file("/proc/meminfo")
        meminfo = {}
        for line in meminfo_raw.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meminfo[key.strip()] = val.strip()

        # Extract total memory un bytes for parity with local psutil output
        mem_total_kb = meminfo.get("MemTotal", "").split()[0] if "MemTotal" in meminfo else ""
        mem_total_bytes = int(mem_total_kb) * 1024 if mem_total_kb.isdigit() else 0

        return {
            "meta": {"ts": None, "pid": None},
            "host": {"hostname": hostname, "fqdn": fqdn},
            "os": {"uname": uname, "system": os_system, "machine": machine},
            "cpu": {
                "cores_logical": int(cpu_count) if cpu_count.isdigit() else None,
            },
            "memory": {
                "meminfo": meminfo,
                "virtual": {"total": mem_total_bytes}
            },
        }

    def preflight(self) -> CheckResult:
        """Check system info collection is available via the configured transport"""
        service_name = "System Info"

        try:
            if isinstance(self._transport, LocalTransport):
                # Local: verify psutil is importable
                psutil.cpu_count()
                return CheckResult.ok(service_name, "psutil available (local)")

            # Remote: verify we can run a basic command over SSH
            result = self._transport.run_command("hostname")
            result.check()
            hostname = result.stdout.strip()
            return CheckResult.ok(
                service_name,
                f"Remote collection OK ({hostname})",
            )
        except ImportError:
            return CheckResult.fail(service_name, "psutil not installed")
        except Exception as e:
            return CheckResult.fail(service_name, f"{type(e).__name__}: {e}")

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
