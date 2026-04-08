"""
Systemd Service Discovery

Discovers Gateway4 virtual environment and configuration paths by
inspecting the systemd unit file via ``systemctl cat``. Used by
Gateway4Collector to locate the correct venv and config without
hardcoding paths.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from platform_atlas.core.transport import Transport

logger = logging.getLogger(__name__)

SYSTEMCTL = "/usr/bin/systemctl"

class DiscoveryError(Exception):
    """Exception Class if Service Discovery fails"""

@dataclass(frozen=True, slots=True)
class ServicePaths:
    """Validated paths extracted from the gateway4 systemd service"""
    python_path: Path
    config_path: Path
    venv_dir: Path
    sync_config: bool = False

def _parse_exec_start(raw: str) -> list[str]:
    """Parse ExecStart into args. Handles systemctl's argv[] wrapper"""
    match = re.match(r"argv\[\]=(.*?)(?:\s*;|$)", raw)
    cmd = match.group(1).strip() if match else raw.strip().strip("{}")
    args = shlex.split(cmd) # safe tokenization, no shell execution
    if not args:
        raise DiscoveryError("ExecStart parsed to empty args")
    return args

def discover_service(
        service_name: str = "automation-gateway",
        config_flags: tuple[str, ...] = ("--properties-file",),
        *,
        transport: Transport,
) -> ServicePaths:
    """Discover venv python and config paths from a systemd service"""

    # Validate service name - only safe chars
    if not re.match(r"^[a-zA-Z0-9@._-]+$", service_name):
        raise DiscoveryError(f"Invalid service name: {service_name!r}")

    result = transport.run_command(f"{SYSTEMCTL} cat {service_name}")

    if not result.ok:
        raise DiscoveryError(f"systemctl failed (rc={result.return_code})")

    # Extract ExecStart value
    exec_start = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            exec_start = stripped[len("ExecStart="):]
            break

    if not exec_start:
        raise DiscoveryError("ExecStart not found in unit file")

    args = _parse_exec_start(exec_start)

    # Find config flag value (--properties-file=/path)
    config_str = None
    for i, arg in enumerate(args):
        for flag in config_flags:
            if arg.startswith(f"{flag}="):
                config_str = arg.split("=", 1)[1]
            elif arg == flag and i + 1 < len(args):
                config_str = args[i + 1]
        if config_str:
            break

    if not config_str:
        raise DiscoveryError(f"No config flag found in ExecStart: {args}")

    sync_config_enabled = any(
        arg == "--sync-config" or arg.startswith("--sync-config=")
        for arg in args
    )

    # Resolve on the target host (local or ssh)
    binary_real = transport.run_command(f"realpath {shlex.quote(args[0])}").stdout.strip()
    binary_path = Path(binary_real)
    config_real = transport.run_command(f"realpath {shlex.quote(config_str)}").stdout.strip()
    config_path = Path(config_real)

    if binary_path.parent.name != "bin":
        raise DiscoveryError(f"Binary not in a bin/ directory: {binary_path}")

    bin_dir = binary_path.parent
    venv_dir = bin_dir.parent
    python_path = bin_dir / "python"

    return ServicePaths(
        python_path=python_path,
        config_path=config_path,
        venv_dir=venv_dir,
        sync_config=sync_config_enabled,
    )

def discover_gateway(
        transport: Transport,
        service_name: str = "automation-gateway"
) -> ServicePaths | None:
    """Safe Entrypoint for Collectors"""
    try:
        return discover_service(transport=transport, service_name=service_name)
    except DiscoveryError as e:
        logger.debug("Gateway discovery failed for '%s': %s", service_name, e)
        return None
    except Exception as e:
        logger.debug(
            "Unexpected error during gateway discovery for '%s'",
            service_name,
            exc_info=True,
        )
        return None

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
