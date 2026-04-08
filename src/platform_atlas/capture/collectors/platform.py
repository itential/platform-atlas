from __future__ import annotations

from typing import Any, Iterable, Mapping

import logging
import warnings
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from concurrent.futures import ThreadPoolExecutor, as_completed

from ipsdk import platform_factory
from ipsdk.exceptions import HTTPStatusError, RequestError, IpsdkError

from platform_atlas.core.context import ctx
if TYPE_CHECKING:
    from platform_atlas.core.config import PlatformNode
from platform_atlas.core.preflight import CheckResult

from platform_atlas.core.exceptions import SecurityWarning

logger = logging.getLogger(__name__)

######## GLOBALS ##########

MAX_REDACT_DEPTH = 100

INDEX_COLLECTIONS = [
    "accounts",
    "group_mappings",
    "groups",
    "iap_profiles",
    "roles",
    "service_configs",
    "integration_models",
    "ae_artifacts",
    "sso_configs",
    "projects",
    "ucm_configs",
    "ucm_compliance_reports",
    "forms",
    "transformations",
    "mop_templates",
    "mop_analytic_templates",
    "automations",
    "triggers",
    "job_data.files",
    "job_history",
    "job_data",
    "job_output",
    "jobs",
    "task_mocks",
    "wfe_job_metrics",
    "wfe_task_metrics",
    "tasks",
    "workflows",
    "tags",
    "automation_services",
    "iagc_requests",
    "resource_action_executions",
    "resource_instances",
    "resource_instance_groups",
    "resource_models",
    "inventory_nodes",
    "inventory_tags"
]

PLATFORM_API_ENDPOINTS: dict[str, str] = {
    "health_status": "/health/status",
    "health_server": "/health/server",
    "config": "/server/config",
    "adapter_status": "/health/adapters",
    "application_status": "/health/applications",
    "adapter_props": "/adapters",
    "application_props": "/applications",
}

# Redact Sensitive JSON Keys from Platform adapterProps
SENSITIVE_KEYS = (
    "password",
    "pass",
    "passwd",
    "token",
    "client_id",
    "client_secret",
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "secretkey",
    "auth_token",
    "authtoken",
    "encryption_key",
    "aws_secret_access_key",
    "oauth_secret",
    "access_token",
    "refresh_token",
)

def redact(obj: Any,
           sensitive_keys: Iterable[str] = ("password",),
           mask: str = "*****",
           _depth: int = 0
           ) -> Any:
    """Removes Sensitive Keys from JSON Objects"""
    if _depth > MAX_REDACT_DEPTH:
        # At max depth, return masked placeholder instead of recursing
        return "[MAX_DEPTH_EXCEEDED]"

    if _depth == 0: # Use frozenset ensures normalized set is immutable and hashable
        keys = frozenset(k.lower() for k in sensitive_keys)
    else:
        keys = sensitive_keys

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(keys, (set, frozenset)) and k.lower() in keys:
                out[k] = mask
            elif not isinstance(keys, (set, frozenset)) and k.lower() in {x.lower() for x in keys}:
                out[k] = mask
            else:
                out[k] = redact(v, keys, mask, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(x, keys, mask, _depth + 1) for x in obj]
    return obj

class PlatformCollector:
    """API Endpoint collector for Itential Platform"""
    def __init__(
            self,
            *,
            platform_uri: str,
            platform_client_id: str,
            platform_client_secret: str,
            timeout: int = 30,
            verify_ssl: bool = True,
            metrics_debug: bool = False,
    ) -> None:
        if not verify_ssl:
            warnings.warn(
                "SSL verification is disabled. This can be enabled in the configuration file if needed.",
                SecurityWarning,
                stacklevel=2
            )
        self.platform_uri = platform_uri.rstrip("/")
        self.metrics_debug = metrics_debug

        # Parse URI components for ipsdk platform_factory
        parsed = urlparse(self.platform_uri)

        self._client = platform_factory(
            host=parsed.hostname or "localhost",
            port=parsed.port or 0,
            use_tls=(parsed.scheme == "https"),
            verify=verify_ssl,
            client_id=platform_client_id,
            client_secret=platform_client_secret,
            timeout=timeout,
        )

    def __repr__(self) -> str:
        # Show host without credentials
        host = urlparse(self.platform_uri).hostname or self.platform_uri
        return f"<PlatformCollector host={host!r}>"

    def close(self) -> None:
        """Close the underlying HTTP client and release connections"""
        if self._client and hasattr(self._client, 'client'):
            self._client.client.close()

    def __enter__(self) -> "PlatformCollector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def from_config(
        cls,
        *,
        timeout: int = 30,
        verify_ssl: bool = True,
        metrics_debug: bool = False,
    ) -> "PlatformCollector":
        cfg = ctx().config

        from platform_atlas.core.credentials import credential_store, CredentialKey
        store = credential_store()

        return cls(
            platform_uri=str(cfg.platform_uri),
            platform_client_id=str(cfg.platform_client_id),
            platform_client_secret=store.get_required(CredentialKey.PLATFORM_SECRET),
            timeout=timeout,
            verify_ssl=bool(cfg.verify_ssl),
            metrics_debug=bool(cfg.debug),
        )

    def _fetch_indexes(
            self,
            max_workers: int = 4,
    ) -> dict:
        """Fetch index status per-collection, skipping any that fail"""
        combined = {}
        failed = []

        def _fetch_one(collection: str) -> tuple[str, dict | None]:
            try:
                r = self._client.get("/indexes/status", params={"collections": collection})
                return collection, r.json()
            except (HTTPStatusError, RequestError) as e:
                logger.debug("Index check failed for '%s': %s", collection, e)
                return collection, None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_fetch_one, coll): coll
                for coll in INDEX_COLLECTIONS
            }
            for future in as_completed(futures):
                collection, data = future.result()
                if data is not None:
                    # Merge results - API may return a dict or list per collection
                    if isinstance(data, dict):
                        combined.update(data)
                    elif isinstance(data, list):
                        combined[collection] = data
                else:
                    failed.append(collection)

        if failed:
            logger.debug("Index checks skipped %d/%d collections: %s",
                         len(failed), len(INDEX_COLLECTIONS), failed)

        return combined

    def _fetch_endpoint(
            self,
            name: str,
            endpoint: str,
            sensitive_keys: Iterable[str],
            redact_endpoint_names: set[str],
    ) -> tuple[str, dict]:
        """Fetch a single Platform API endpoint. Returns (name, data)"""
        try:
            url = endpoint if endpoint.startswith('/') else f'/{endpoint}'
            r = self._client.get(url)
            data = r.json()

            if name in redact_endpoint_names:
                data = redact(data, sensitive_keys=sensitive_keys)
            return name, data
        except (HTTPStatusError, RequestError) as e:
            logger.debug("Platform endpoint error [%s], %s: %s", name, endpoint, e)
            return name, {"error": str(e), "status": "failed"}

    def get_platform_info(
            self,
            endpoints: Mapping[str, str] | None = None,
            *,
            sensitive_keys: Iterable[str] = SENSITIVE_KEYS,
            redact_endpoint_names: set[str] | None = None,
            max_workers: int = 4,
            ) -> dict:
        """Fetch all endpoints in parallel and return dict[name] = json"""

        config = ctx().config
        endpoints = dict(endpoints or PLATFORM_API_ENDPOINTS)

        if config.legacy_profile:
            profile_name = config.legacy_profile
            endpoints["profile"] = f"/profiles/{profile_name}"

        endpoints = endpoints or PLATFORM_API_ENDPOINTS
        redact_endpoint_names = redact_endpoint_names or {"adapter_props"}

        # Verify authentication by triggering first request early.
        # ipsdk authenticates lazily on the first API call; if auth fails,
        # the exception surfaces here rather than inside the thread pool.
        try:
            self._client.get("/health/status")
        except (HTTPStatusError, RequestError, IpsdkError) as e:
            raise ConnectionError(f"Failed to authenticate with Platform API: {e}")

        results = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._fetch_endpoint,
                    name, endpoint,
                    sensitive_keys, redact_endpoint_names,
                ): name
                for name, endpoint in endpoints.items()
            }
            for future in as_completed(futures):
                name, data = future.result()
                results[name] = data

        # Fetch index status per-collection (graceful degradation)
        try:
            indexes = self._fetch_indexes(max_workers=max_workers)
            if indexes:
                results["indexes_status"] = indexes
        except Exception as e:
            logger.debug("Index status collection failed entirely: %s", e)

        return results

    @staticmethod
    def preflight() -> CheckResult:
        """Test Platform API OAuth2 authentication"""
        service_name = "Platform API"
        try:
            config = ctx().config

            if not getattr(config, "platform_uri", None):
                return CheckResult.skip(service_name, "Not configured (platform_uri empty)")

            # Suppress SSL warnings during preflight check
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # ipsdk authenticates lazily on first request;
                # hit a lightweight endpoint to trigger and verify auth
                with PlatformCollector.from_config() as collector:
                    collector._client.get("/health/status")

            return CheckResult.ok(service_name, "OAuth2 authentication successful")
        except HTTPStatusError as e:
            return CheckResult.fail(
                service_name,
                f"HTTP {e.response.status_code}" if e.response else "HTTP error",
                str(e)
            )
        except (RequestError, IpsdkError) as e:
            return CheckResult.fail(service_name, "Connection failed", str(e))
        except Exception as e:
            return CheckResult.fail(service_name, "Connection failed", str(e))

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
