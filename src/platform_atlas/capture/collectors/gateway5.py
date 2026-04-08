"""
Gateway5 Collector - Environment variable collection for Itential Gateway5

Captures Gateway5 configuration variables via ``printenv`` over SSH.
If no environment variables are detected, returns an empty dict so
the guided collector can prompt the user for a Docker Compose or Helm
values file as a fallback.

Example:
    >>> collector = Gateway5Collector(transport=ssh_transport)
    >>> data = collector.collect_env()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.transport import Transport

logger = logging.getLogger(__name__)

class GW5Category(str, Enum):
    """Logical grouping for Gateway5 variables"""
    STORAGE = "storage"
    LOGGING = "logging"
    TLS = "tls"
    CERTIFICATES = "certificates"
    CONNECT = "connect"
    FEATURES = "features"
    SERVER = "server"
    RUNNER = "runner"

@dataclass(frozen=True, slots=True)
class GW5Variable:
    """Definition of a single Gateway5 environment variable"""
    name: str
    category: GW5Category
    label: str

GATEWAY5_VARIABLES: tuple[GW5Variable, ...] = (
    # Storage
    GW5Variable("GATEWAY_STORE_BACKEND",                  GW5Category.STORAGE,      "Store backend type"),
    # Logging
    GW5Variable("GATEWAY_LOG_CONSOLE_JSON",               GW5Category.LOGGING,      "Console JSON logging"),
    GW5Variable("GATEWAY_LOG_FILE_JSON",                  GW5Category.LOGGING,      "File JSON logging"),
    GW5Variable("GATEWAY_LOG_LEVEL",                      GW5Category.LOGGING,      "Log level"),
    # Client TLS
    GW5Variable("GATEWAY_CLIENT_USE_TLS",                 GW5Category.TLS,          "Client TLS enabled"),
    GW5Variable("GATEWAY_CLIENT_PRIVATE_KEY_FILE",        GW5Category.TLS,          "Client private key file"),
    # Certificates
    GW5Variable("GATEWAY_SERVER_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Server certificate file"),
    GW5Variable("GATEWAY_CLIENT_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Client certificate file"),
    GW5Variable("GATEWAY_CONNECT_CERTIFICATE_FILE",       GW5Category.CERTIFICATES, "Connect certificate file"),
    GW5Variable("GATEWAY_CONNECT_PRIVATE_KEY_FILE",       GW5Category.CERTIFICATES, "Connect private key file"),
    GW5Variable("GATEWAY_RUNNER_CERTIFICATE_FILE",        GW5Category.CERTIFICATES, "Runner certificate file"),
    # Connect / Gateway Manager
    GW5Variable("GATEWAY_CONNECT_ENABLED",                GW5Category.CONNECT,      "Gateway manager enabled"),
    GW5Variable("GATEWAY_CONNECT_INSECURE_TLS",           GW5Category.CONNECT,      "Connect insecure TLS"),
    GW5Variable("GATEWAY_CONNECT_SERVER_HA_ENABLED",      GW5Category.CONNECT,      "Connect HA enabled"),
    GW5Variable("GATEWAY_CONNECT_SERVER_HA_IS_PRIMARY",   GW5Category.CONNECT,      "Connect HA is primary"),
    # Features
    GW5Variable("GATEWAY_FEATURES_ANSIBLE_ENABLED",       GW5Category.FEATURES,     "Feature: Ansible"),
    GW5Variable("GATEWAY_FEATURES_HOSTKEYS_ENABLED",      GW5Category.FEATURES,     "Feature: Host Keys"),
    GW5Variable("GATEWAY_FEATURES_OPENTOFU_ENABLED",      GW5Category.FEATURES,     "Feature: OpenTofu"),
    GW5Variable("GATEWAY_FEATURES_PYTHON_ENABLED",        GW5Category.FEATURES,     "Feature: Python"),
    # Server
    GW5Variable("GATEWAY_SERVER_DISTRIBUTED_EXECUTION",   GW5Category.SERVER,       "Distributed execution"),
    GW5Variable("GATEWAY_SERVER_USE_TLS",                 GW5Category.SERVER,       "Server TLS enabled"),
    # Runner
    GW5Variable("GATEWAY_RUNNER_ANNOUNCEMENT_ADDRESS",    GW5Category.RUNNER,       "Runner announcement address"),
    GW5Variable("GATEWAY_RUNNER_USE_TLS",                 GW5Category.RUNNER,       "Runner TLS enabled"),
)

# Quick-access set for membership checks
_VAR_NAMES: frozenset[str] = frozenset(v.name for v in GATEWAY5_VARIABLES)

@dataclass
class _CollectedVars:
    """Internal accumulator for environment vars"""
    values: dict[str, str | None] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def seed(self) -> None:
        """Pre-populate every known variable as None (unresolved)."""
        for var in GATEWAY5_VARIABLES:
            self.values.setdefault(var.name, None)

    def set_if_missing(self, name: str, value: str, source: str) -> None:
        """Set a variable only if it hasn't already been resolved."""
        if name not in _VAR_NAMES:
            return
        if self.values.get(name) is not None:
            return  # already resolved by a higher-priority tier
        self.values[name] = value
        self.sources[name] = source

    @property
    def resolved(self) -> dict[str, str]:
        return {k: v for k, v in self.values.items() if v is not None}

    @property
    def unresolved_names(self) -> list[str]:
        return [k for k, v in self.values.items() if v is None]

    def to_dict(self) -> dict[str, Any]:
        """Build the capture-ready dict for the capture engine"""
        return {
            "variables": dict(self.values),
            "sources": dict(self.sources),
            "summary": {
                "total": len(self.values),
                "resolved": len(self.resolved),
                "unresolved": len(self.unresolved_names),
                "unresolved_keys": self.unresolved_names,
            },
        }

class Gateway5Collector:
    """Collects Gateway5 environment variables over SSH"""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def __repr__(self) -> str:
        transport = type(self._transport).__name__
        return f"<Gateway5Collector transport={transport}>"

    def collect_env(self) -> dict[str, Any]:
        """Run the full tiered collection and return capture-ready dict"""
        collected = _CollectedVars()
        collected.seed()

        self._collect_from_env(collected)

        if not collected.resolved:
            logger.debug("Gateway5: no env vars found - skipping")
            return {}

        logger.debug(
            "Gateway5: collection complete - %d/%d resolved",
            len(collected.resolved), len(collected.values),
        )
        return collected.to_dict()

    def preflight(self) -> CheckResult:
        """Verify we can reach the remote host and detect Gateway5 config."""
        service_name = "Gateway5"

        try:
            result = self._transport.run_command("hostname")
            result.check()
            hostname = result.stdout.strip()

            # Check if any env vars are set
            test_result = self._transport.run_command(
                "printenv GATEWAY_LOG_LEVEL"
            )
            if test_result.ok and test_result.stdout.strip():
                return CheckResult.ok(
                    service_name,
                    f"Gateway5 env vars detected on {hostname}",
                )

            return CheckResult.skip(
                service_name,
                "No Gateway5 env vars detected",
                hostname,
            )
        except Exception as e:
            return CheckResult.fail(
                service_name,
                f"Preflight failed: {type(e).__name__}",
                str(e),
            )

    def _collect_from_env(self, collected: _CollectedVars) -> None:
        """Read all GATEWAY_* variables in a single SSH command"""
        try:
            result = self._transport.run_command("printenv")
            if not result.ok:
                logger.debug("Gateway5: printenv returned no results")
                return

            for line in result.stdout.splitlines():
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if not key.startswith("GATEWAY_"):
                    continue
                if key in _VAR_NAMES and value:
                    collected.set_if_missing(key, value, "environment")
        except Exception as exc:
            logger.debug("Gateway5: failed to read env - %s", exc)

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
