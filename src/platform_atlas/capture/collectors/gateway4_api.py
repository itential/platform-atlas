"""
Gateway4 API Collector — Protocol-based config collection via ipsdk

Connects to the Gateway4 REST API using ipsdk.gateway_factory() to
fetch runtime configuration and server status. Provides alt_path
fallback data for rules IAG-001 through IAG-007 when SSH is unavailable.

Authentication uses basic username/password (default: admin@itential).
The password is stored in the Atlas credential store (keyring or Vault).

Example:
    >>> collector = Gateway4ApiCollector.from_config()
    >>> data = collector.collect()
    >>> data["runtime_config"]["logging_level"]
    'INFO'
"""

from __future__ import annotations

import logging
import warnings
from typing import Any
from urllib.parse import urlparse

from ipsdk import gateway_factory
from ipsdk.exceptions import HTTPStatusError, RequestError, IpsdkError

from platform_atlas.core.context import ctx
from platform_atlas.core.exceptions import SecurityWarning
from platform_atlas.core.preflight import CheckResult

__all__ = ["Gateway4ApiCollector", "GATEWAY4_AUDITED_CONFIG_KEYS"]

logger = logging.getLogger(__name__)

# Audited settings kept from GET /config — everything else is dropped.
#
# The endpoint returns the gateway's entire runtime configuration, which can
# include LDAP bind credentials, the Flask session secret and device secrets.
# ``gateway4.runtime_config`` is a wholesale passthrough in finalize_capture()
# (so rules can distinguish "section captured, leaf missing" from "section
# never captured"), which means anything the collector returns survives into
# 01_capture.json — and that file is packaged by ``session export`` for
# sharing with support. Minimize at the source instead, the same way
# GATEWAY5_VARIABLES does for IAG5.
#
# Keys here are the ones IAG-001..IAG-007 read via path/alt_path. Add a key
# when a rule starts reading it, not before.
GATEWAY4_AUDITED_CONFIG_KEYS: frozenset[str] = frozenset({
    "logging_level",         # IAG-001
    "http_logging_level",    # IAG-002
    "http_server_threads",   # IAG-003
    "audit_retention_days",  # IAG-005
    "ansible_debug",         # IAG-006
    "ldap_auth_enabled",     # IAG-007
})


def _select_audited_config(config_data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the audited keys from a GET /config payload.

    Preserves the flat ``key → value`` shape the rule paths resolve against.
    A key that the gateway simply doesn't report is left absent rather than
    nulled, so ``_parent_section_exists()`` can still tell an unset value from
    an uncollected section.
    """
    return {
        key: value
        for key, value in config_data.items()
        if key in GATEWAY4_AUDITED_CONFIG_KEYS
    }


class Gateway4ApiCollector:
    """REST API collector for Itential Automation Gateway 4.

    Uses ipsdk.gateway_factory() to authenticate and fetch:
    - GET /config  → runtime config from the AG database
    - GET /status  → server version and feature flags
    """

    def __init__(
        self,
        *,
        gateway_uri: str,
        gateway_username: str,
        gateway_password: str,
        timeout: int = 15,
        verify_ssl: bool = True,
    ) -> None:
        if not verify_ssl:
            warnings.warn(
                "SSL verification is disabled for Gateway4 API.",
                SecurityWarning,
                stacklevel=2,
            )

        self.gateway_uri = gateway_uri.rstrip("/")
        parsed = urlparse(self.gateway_uri)

        host = parsed.hostname or "localhost"
        port = parsed.port or 8083
        use_tls = (parsed.scheme == "https")

        logger.info(
            "Gateway4 API: connecting to %s:%s (tls=%s, user=%s)",
            host, port, use_tls, gateway_username,
        )

        try:
            self._client = gateway_factory(
                host=host,
                port=port,
                use_tls=use_tls,
                verify=verify_ssl,
                user=gateway_username,
                password=gateway_password,
                timeout=timeout,
            )
            logger.debug("Gateway4 API: gateway_factory() created client successfully")
        except Exception as e:
            logger.error(
                "Gateway4 API: gateway_factory() failed: %s: %s",
                type(e).__name__, e,
            )
            raise

    def __repr__(self) -> str:
        host = urlparse(self.gateway_uri).hostname or self.gateway_uri
        return f"<Gateway4ApiCollector host={host!r}>"

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and hasattr(self._client, "client"):
            try:
                self._client.client.close()
            except Exception:
                pass

    def __enter__(self) -> "Gateway4ApiCollector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def from_config(cls) -> "Gateway4ApiCollector | None":
        """Build a collector from the active Atlas configuration.

        Returns None if Gateway4 API credentials are not configured,
        allowing the capture engine to skip this collector gracefully.
        """
        config = ctx().config

        gateway_uri = getattr(config, "gateway4_uri", None) or ""
        gateway_username = getattr(config, "gateway4_username", None) or ""

        if not gateway_uri:
            logger.debug("Gateway4 API not configured (gateway4_uri empty)")
            return None

        logger.debug(
            "Gateway4 API from_config: uri=%s, username=%s",
            gateway_uri, gateway_username or "(default)",
        )

        # Password from credential store
        gateway_password = None
        try:
            from platform_atlas.core.credentials import credential_store, CredentialKey
            store = credential_store()
            gateway_password = store.get(CredentialKey.GATEWAY4_PASSWORD)
            if gateway_password:
                logger.debug("Gateway4 API: password loaded from credential store")
            else:
                logger.info(
                    "Gateway4 API: password not found in credential store "
                    "(key: %s). Run 'platform-atlas config credentials' to configure.",
                    CredentialKey.GATEWAY4_PASSWORD.value,
                )
                return None
        except Exception as e:
            logger.warning(
                "Gateway4 API: failed to read password from credential store: %s: %s",
                type(e).__name__, e,
            )
            return None

        try:
            return cls(
                gateway_uri=gateway_uri,
                gateway_username=gateway_username or "admin@itential",
                gateway_password=gateway_password,
                timeout=15,
                verify_ssl=bool(config.verify_ssl),
            )
        except Exception as e:
            logger.warning(
                "Gateway4 API: collector initialization failed: %s: %s",
                type(e).__name__, e,
            )
            return None

    def _fetch(self, endpoint: str) -> dict[str, Any]:
        """Fetch a single API endpoint, returning JSON or empty dict."""
        try:
            logger.debug("Gateway4 API: GET %s", endpoint)
            r = self._client.get(endpoint)
            data = r.json()
            logger.debug(
                "Gateway4 API: GET %s -> %d keys",
                endpoint, len(data) if isinstance(data, dict) else 0,
            )
            return data
        except HTTPStatusError as e:
            status = e.response.status_code if e.response else "unknown"
            logger.warning(
                "Gateway4 API: GET %s failed - HTTP %s: %s",
                endpoint, status, e,
            )
            return {}
        except RequestError as e:
            logger.warning(
                "Gateway4 API: GET %s - connection error: %s: %s",
                endpoint, type(e).__name__, e,
            )
            return {}
        except IpsdkError as e:
            logger.warning(
                "Gateway4 API: GET %s - SDK error: %s: %s",
                endpoint, type(e).__name__, e,
            )
            return {}
        except Exception as e:
            logger.warning(
                "Gateway4 API: GET %s - unexpected error: %s: %s",
                endpoint, type(e).__name__, e,
            )
            return {}

    def collect(self) -> dict[str, Any]:
        """Collect runtime config and server status from Gateway4 API.

        Returns a dict with two top-level keys:
        - "runtime_config": the audited subset of GET /config
          (see GATEWAY4_AUDITED_CONFIG_KEYS — unaudited keys are dropped
          at the source so gateway secrets never enter the capture)
        - "api_status": server status from GET /status
        """
        # Verify connectivity with a lightweight endpoint.
        # ipsdk authenticates lazily on the first API call; if auth
        # fails, the exception surfaces here.
        logger.debug("Gateway4 API: testing connectivity with GET /status")
        try:
            r = self._client.get("/status")
            logger.info(
                "Gateway4 API: connected successfully (HTTP %s)",
                r.status_code,
            )
        except HTTPStatusError as e:
            status = e.response.status_code if e.response else "unknown"
            logger.warning(
                "Gateway4 API: authentication or connection failed - HTTP %s: %s",
                status, e,
            )
            return {}
        except (RequestError, IpsdkError) as e:
            logger.warning(
                "Gateway4 API: connection failed - %s: %s",
                type(e).__name__, e,
            )
            return {}
        except Exception as e:
            logger.warning(
                "Gateway4 API: unexpected connection error - %s: %s",
                type(e).__name__, e,
            )
            return {}

        config_data = self._fetch("/config")
        status_data = self._fetch("/status")

        result: dict[str, Any] = {}
        if config_data:
            audited = _select_audited_config(config_data)
            result["runtime_config"] = audited
            logger.info(
                "Gateway4 API: collected %d audited config properties "
                "(%d returned, %d dropped as unaudited)",
                len(audited), len(config_data), len(config_data) - len(audited),
            )
        else:
            logger.warning("Gateway4 API: GET /config returned no data")

        if status_data:
            result["api_status"] = status_data
        else:
            logger.warning("Gateway4 API: GET /status returned no data")

        return result

    @staticmethod
    def preflight() -> CheckResult:
        """Test Gateway4 API connectivity."""
        service_name = "Gateway4 API"
        try:
            config = ctx().config
            if not getattr(config, "gateway4_uri", None):
                return CheckResult.skip(service_name, "Not configured (gateway4_uri empty)")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                collector = Gateway4ApiCollector.from_config()
                if collector is None:
                    return CheckResult.skip(
                        service_name, "Credentials not configured"
                    )
                try:
                    collector._client.get("/status")
                    return CheckResult.ok(
                        service_name, "Connected successfully"
                    )
                finally:
                    collector.close()

        except HTTPStatusError as e:
            status = e.response.status_code if e.response else "unknown"
            return CheckResult.fail(
                service_name,
                f"HTTP {status}",
                str(e),
            )
        except (RequestError, IpsdkError) as e:
            return CheckResult.fail(service_name, "Connection failed", str(e))
        except Exception as e:
            return CheckResult.fail(
                service_name, f"Unexpected error: {type(e).__name__}", str(e)
            )
