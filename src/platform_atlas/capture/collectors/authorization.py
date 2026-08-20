"""Authorization (RBAC) collector for Itential Platform 6.

Pulls the complete RBAC graph from the five /authorization/* endpoints:
accounts, groups, roles, methods, views. Uses the same Platform OAuth
client as PlatformCollector — no extra credentials needed.

Opt-in only: only registered when "rbac_authorization" is not in
``config.disabled_extended_checks`` (an Additional Validation Module,
toggled via `config edit` > Advanced or the WebUI equivalent — opt-in by
default, unlike most other modules). Not available under SaaS tier.
"""
from __future__ import annotations

import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from ipsdk import platform_factory
from ipsdk.exceptions import HTTPStatusError, RequestError, IpsdkError

from platform_atlas.core.context import ctx
from platform_atlas.core.exceptions import SecurityWarning

logger = logging.getLogger(__name__)

# Fields redacted at write boundary (per spec §10)
_REDACT_ACCOUNT_KEYS = frozenset({"gitTokens", "sso"})

# Large page size — intended to capture everything in a single request for most installs.
# The skip-loop in _paginate handles the rare case where total exceeds this.
_PAGE_SIZE = 1000


def _redact_accounts(accounts: list) -> list:
    """Remove sensitive keys from account records before storing."""
    out = []
    for acct in accounts:
        if isinstance(acct, dict):
            out.append({k: "*****" if k in _REDACT_ACCOUNT_KEYS else v for k, v in acct.items()})
        else:
            out.append(acct)
    return out


def _paginate(client, endpoint: str, page_size: int = _PAGE_SIZE, extra_params: dict | None = None) -> list:
    """Collect all pages from a paginated /authorization/* endpoint.

    With ``_PAGE_SIZE`` set to 1000 most installs land in a single request.
    The skip-loop is the safety net for unusually large datasets.
    """
    results = []
    skip = 0
    total = None
    base_params = dict(extra_params or {})

    while True:
        try:
            r = client.get(endpoint, params={**base_params, "limit": page_size, "skip": skip})
            data = r.json()
        except (HTTPStatusError, RequestError) as exc:
            logger.debug("Authorization pagination failed [%s skip=%d]: %s", endpoint, skip, exc)
            break

        page = data.get("results", [])
        results.extend(page)

        if total is None:
            total = data.get("total", 0)

        skip += len(page)
        if not page or (total is not None and len(results) >= total):
            break

    return results


class AuthorizationCollector:
    """RBAC graph collector for Itential Platform 6.

    Fetches accounts, groups, roles, methods, and views in parallel using
    the Platform OAuth client. Redacts sensitive account fields before
    returning. Only constructed when "rbac_authorization" is not in
    ``config.disabled_extended_checks``.
    """

    def __init__(
        self,
        *,
        platform_uri: str,
        platform_client_id: str,
        platform_client_secret: str,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        from platform_atlas.core.context import forbid_in_saas
        forbid_in_saas(
            "AuthorizationCollector",
            hint="SaaS audits are gateway-only — RBAC collection is not available.",
        )
        if not verify_ssl:
            warnings.warn(
                "SSL verification is disabled for authorization collection.",
                SecurityWarning,
                stacklevel=2,
            )

        parsed = urlparse(platform_uri.rstrip("/"))
        self._client = platform_factory(
            host=parsed.hostname or "localhost",
            port=parsed.port or 0,
            use_tls=(parsed.scheme == "https"),
            verify=verify_ssl,
            client_id=platform_client_id,
            client_secret=platform_client_secret,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._client and hasattr(self._client, "client"):
            self._client.client.close()

    def __enter__(self) -> "AuthorizationCollector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def from_config(cls) -> "AuthorizationCollector":
        cfg = ctx().config
        from platform_atlas.core.credentials import credential_store, CredentialKey
        store = credential_store()
        timeout = max(5, min(int(getattr(cfg, "platform_api_timeout_s", 30) or 30), 300))
        return cls(
            platform_uri=str(cfg.platform_uri),
            platform_client_id=str(cfg.platform_client_id),
            platform_client_secret=store.get_required(CredentialKey.PLATFORM_SECRET),
            timeout=timeout,
            verify_ssl=bool(cfg.verify_ssl),
        )

    def collect(self) -> dict:
        """Fetch the full RBAC graph in parallel. Returns raw collections dict.

        Accounts are fetched as two separate calls matching how the Platform UI
        separates them — active users and active service accounts — then merged.
        All other collections use a single paginated fetch.
        """
        # Trigger auth early (lazy OAuth — first request issues /oauth/token)
        try:
            self._client.get("/health/status")
        except (HTTPStatusError, RequestError, IpsdkError) as exc:
            raise ConnectionError(f"Authorization collector: Platform auth failed: {exc}") from exc

        # Active regular users and active service accounts fetched separately,
        # matching the Platform UI's own account queries.
        _ACCT = "/authorization/accounts"
        _ACCT_BASE = {"inactive": "false"}
        fetch_tasks: list[tuple[str, str, dict]] = [
            ("users",            _ACCT, {**_ACCT_BASE, "isServiceAccount": "false"}),
            ("service_accounts", _ACCT, {**_ACCT_BASE, "isServiceAccount": "true"}),
            ("groups",  "/authorization/groups",  {}),
            ("roles",   "/authorization/roles",   {}),
            ("methods", "/authorization/methods", {}),
            ("views",   "/authorization/views",   {}),
        ]

        raw: dict[str, list] = {}

        def _fetch_one(name: str, endpoint: str, params: dict) -> tuple[str, list]:
            return name, _paginate(self._client, endpoint, extra_params=params or None)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_one, n, ep, p): n for n, ep, p in fetch_tasks}
            for future in as_completed(futures):
                name, data = future.result()
                raw[name] = data

        # Merge users + service accounts into a single "accounts" list, redacted.
        accounts = _redact_accounts(raw.get("users", []) + raw.get("service_accounts", []))

        results = {
            "accounts": accounts,
            "groups":   raw.get("groups", []),
            "roles":    raw.get("roles", []),
            "methods":  raw.get("methods", []),
            "views":    raw.get("views", []),
        }

        logger.debug(
            "Authorization collected: users=%d service_accounts=%d groups=%d roles=%d methods=%d views=%d",
            len(raw.get("users", [])),
            len(raw.get("service_accounts", [])),
            len(results["groups"]),
            len(results["roles"]),
            len(results["methods"]),
            len(results["views"]),
        )
        return results
