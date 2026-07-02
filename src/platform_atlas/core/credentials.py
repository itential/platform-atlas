"""
ATLAS // Credential Store

Manages sensitive credentials via a pluggable backend system.

Supported backends:
  • Keyring  — OS keyring (macOS Keychain, Windows Credential Locker,
               Linux Secret Service). Default for single-user deployments.
               Full read/write.
  • Vault   — HashiCorp Vault KV v2 secrets engine. READ-ONLY from Atlas.
               Credentials are managed externally in Vault; Atlas only
               consumes them. Connection settings for Vault itself are
               stored in the OS keyring, keeping everything off disk.

When an environment is active, ALL keyring data is scoped under
``platform-atlas/<env_name>`` — this includes both regular credentials
(platform secret, mongo URI, etc.) and Vault connection settings
(vault URL, token, AppRole). Each environment is fully isolated so
switching environments never stomps on another's credentials.

When no environment is active (legacy mode), the flat ``platform-atlas``
service name is used for backward compatibility.

Callers interact exclusively with CredentialStore / CredentialKey.
The active backend is determined by the ``credential_backend`` field
in config.json or the active environment ("keyring" or "vault").
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

from platform_atlas.core.exceptions import (
    CredentialError,
    TierViolationError,
)

__all__ = [
    "CredentialBackend",
    "CredentialBackendType",
    "CredentialKey",
    "CredentialStore",
    "FileSecretStore",
    "FileStoreHealth",
    "KeyringBackend",
    "KeyringSecretStore",
    "SecretStore",
    "VaultAuthMethod",
    "VaultBackend",
    "VaultConfig",
    "active_secret_store",
    "applicable_keys",
    "credential_store",
    "migrate_legacy_credentials",
    "required_keys",
    "reset_credential_store",
    "scoped_service_name",
    "verify_keyring_backend",
]

logger = logging.getLogger(__name__)

SERVICE_NAME = "platform-atlas"

@unique
class CredentialKey(Enum):
    """
    Known credential slots managed by Atlas.

    Each value becomes the lookup key in whichever backend is active.
    For Keyring this is the "username" under the service namespace.
    For Vault this is a key inside the KV v2 secret dict.
    """
    PLATFORM_SECRET = "platform_client_secret"  # nosec B105 — lookup key, not a credential
    MONGO_URI       = "mongo_uri"
    REDIS_URI       = "redis_uri"
    SSH_PASSPHRASE  = "ssh_key_passphrase"      # nosec B105
    SSH_PASSWORD    = "ssh_password"             # nosec B105
    GATEWAY4_PASSWORD = "gateway4_password"      # nosec B105

    @property
    def display_name(self) -> str:
        """Human-readable name for CLI output."""
        names = {
            "platform_client_secret": "Platform Client Secret",   # nosec B105
            "mongo_uri":              "MongoDB URI",
            "redis_uri":              "Redis URI",
            "ssh_key_passphrase":     "SSH Key Passphrase",       # nosec B105
            "ssh_password":           "SSH Password",             # nosec B105
            "gateway4_password":      "Gateway4 API Password",    # nosec B105
        }
        return names.get(self.value, self.value)

    @property
    def required(self) -> bool:
        """True if this credential must exist under the active tier.

        Tier-aware: PLATFORM_SECRET is required in the platform-anchored
        tiers (standard/extended) but not in SaaS, which never talks to
        the Platform.
        """
        return self in required_keys()

    @property
    def collector_module(self) -> str | None:
        """The collector module that needs this credential, or None if always required."""
        return _KEY_MODULE_MAP.get(self)

# Maps optional credentials to the collector module that needs them
_KEY_MODULE_MAP: dict[CredentialKey, str] = {
    CredentialKey.MONGO_URI:         "mongo",
    CredentialKey.REDIS_URI:         "redis",
    CredentialKey.GATEWAY4_PASSWORD: "gateway4",
}

# ── Tier-aware credential gating (defense 3 of the hard mode boundary) ──
# Which keys are usable per tier. The credential store refuses to write any
# key outside the active tier's set and silently returns None/absent on
# read, so there is no leakage path between tier switches (alongside
# registry pruning and the require_extended()/require_infra() guards).
#
#   standard — app-only audit: Platform OAuth + optional Gateway4 API. No SSH.
#   saas     — single-gateway audit: gateway SSH + Gateway4 API. No Platform.
#   extended — full infrastructure audit: everything.
_TIER_APPLICABLE_KEYS: dict[str, frozenset[CredentialKey]] = {
    "standard": frozenset({CredentialKey.PLATFORM_SECRET, CredentialKey.GATEWAY4_PASSWORD}),
    "saas":     frozenset({CredentialKey.SSH_PASSPHRASE, CredentialKey.SSH_PASSWORD, CredentialKey.GATEWAY4_PASSWORD}),
    "extended": frozenset(CredentialKey),
}

# Keys that must exist before a capture can run, per tier. PLATFORM_SECRET
# is required only where the audit is platform-anchored — a SaaS audit never
# talks to the Platform, so nothing is statically required there (the
# Gateway4 API password is needed only when a GW4 API target is configured,
# which preflight checks contextually).
_TIER_REQUIRED_KEYS: dict[str, frozenset[CredentialKey]] = {
    "standard": frozenset({CredentialKey.PLATFORM_SECRET}),
    "saas":     frozenset(),
    "extended": frozenset({CredentialKey.PLATFORM_SECRET}),
}

_TIER_LABELS: dict[str, str] = {
    "standard": "Standard",
    "saas": "SaaS",
    "extended": "Extended",
}

# Back-compat export: the keys hidden while tier=standard (everything the
# app-only tier has no use for). Store gating itself resolves through
# applicable_keys() now; this set remains for callers that present
# Standard's reduced credential surface (WebUI routes, tests).
#
# GATEWAY4_PASSWORD is intentionally NOT in this set — Gateway4 API auth
# works in Standard via ipsdk over HTTPS.
EXTENDED_ONLY_KEYS: frozenset[CredentialKey] = frozenset({
    CredentialKey.MONGO_URI,
    CredentialKey.REDIS_URI,
    CredentialKey.SSH_PASSPHRASE,
    CredentialKey.SSH_PASSWORD,
})


def _active_tier() -> str | None:
    """
    The active tier, or None when it cannot be determined yet (no config
    loaded and no context initialized). Returning None keeps init/setup
    paths unblocked — credential gating only activates once Atlas has
    resolved its tier.
    """
    try:
        from platform_atlas.core.config import get_config
        return get_config().tier
    except Exception:
        return None


def applicable_keys(tier: str | None = None) -> frozenset[CredentialKey]:
    """
    The credential keys usable under *tier* (default: the active tier).

    Unknown or not-yet-resolved tiers fail open to the full set so
    early-startup and setup paths are never blocked before config exists.
    """
    resolved = tier if tier is not None else _active_tier()
    if resolved is None:
        return frozenset(CredentialKey)
    return _TIER_APPLICABLE_KEYS.get(resolved, frozenset(CredentialKey))


def required_keys(tier: str | None = None) -> frozenset[CredentialKey]:
    """
    The credential keys that must exist under *tier* (default: the active
    tier). Falls back to the extended set (PLATFORM_SECRET) when the tier
    cannot be resolved, preserving the historical always-required behavior.
    """
    resolved = tier if tier is not None else _active_tier()
    if resolved is None:
        return _TIER_REQUIRED_KEYS["extended"]
    return _TIER_REQUIRED_KEYS.get(resolved, _TIER_REQUIRED_KEYS["extended"])


def scoped_service_name(env_name: str | None = None) -> str:
    """
    Return the keyring service name, scoped to the active environment.

    When an environment is active:  ``platform-atlas/<env_name>``
    When no environment is active:  ``platform-atlas`` (backward compat)

    This scoping applies to ALL keyring data — both regular credentials
    and Vault connection settings — so each environment is fully isolated.
    """
    if env_name:
        return f"{SERVICE_NAME}/{env_name}"
    return SERVICE_NAME


@unique
class CredentialBackendType(Enum):
    """Which credential backend is active — persisted in config.json."""
    KEYRING = "keyring"
    FILE    = "file"
    VAULT   = "vault"


@unique
class VaultAuthMethod(Enum):
    """Supported HashiCorp Vault authentication methods."""
    TOKEN           = "token"           # Static token stored in keyring
    APPROLE         = "approle"         # role_id + static secret_id
    APPROLE_WRAPPED = "approle_wrapped" # role_id + response-wrapped secret_id (one-time-use)
    TOKEN_FILE      = "token_file"      # Token read from file at runtime (Vault Agent sink)
    TOKEN_ENV       = "token_env"       # Token read from VAULT_TOKEN env var at runtime


@runtime_checkable
class CredentialBackend(Protocol):
    """Contract that any credential backend must satisfy."""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...

    @property
    def read_only(self) -> bool: ...

class KeyringBackend:
    """Local-store credential backend for the five Atlas secrets.

    The underlying substrate is resolved by :func:`active_secret_store` — the
    OS keyring (macOS Keychain, Windows Credential Locker, Linux Secret
    Service) under normal conditions, or the encrypted local file store
    (:class:`FileSecretStore`) transparently when the keyring is unavailable or
    the file store has been forced. The class name is historical: this is the
    "local" backend, as opposed to :class:`VaultBackend`.
    """

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self._service = service

    @property
    def service(self) -> str:
        """The keyring service name in use."""
        return self._service

    @property
    def read_only(self) -> bool:
        return False

    def get(self, key: str) -> str | None:
        return active_secret_store().get(self._service, key)

    def set(self, key: str, value: str) -> None:
        active_secret_store().set(self._service, key, value)

    def delete(self, key: str) -> None:
        active_secret_store().delete(self._service, key)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def __repr__(self) -> str:
        return f"KeyringBackend(service={self._service!r}, store={active_secret_store()!r})"


@dataclass(frozen=True)
class VaultConfig:
    """Connection parameters for HashiCorp Vault."""
    url: str                                        # https://vault.example.com:8200
    auth_method: VaultAuthMethod = VaultAuthMethod.TOKEN
    token: str | None = None                        # For token auth
    role_id: str | None = None                      # For AppRole / AppRole-wrapped auth
    secret_id: str | None = None                    # For AppRole auth (static)
    wrapping_token: str | None = None               # For AppRole-wrapped auth (one-time-use)
    token_file_path: str | None = None              # For token_file auth (Vault Agent sink path)
    mount_point: str = "secret"                     # KV v2 mount path
    secret_path: str = "platform-atlas"             # Path under mount
    verify_ssl: bool = True
    namespace: str | None = None                    # Vault Enterprise namespace

    @property
    def display_url(self) -> str:
        """URL suitable for log / UI output."""
        return self.url

    @property
    def full_path(self) -> str:
        """The full Vault KV path for display/error messages."""
        return f"{self.mount_point}/data/{self.secret_path}"


# Refresh the token when fewer than this many seconds remain on its TTL.
# Five minutes gives plenty of runway without hammering Vault with renewals.
_VAULT_REFRESH_MARGIN = 300


class VaultBackend:
    """
    HashiCorp Vault backend — READ-ONLY access to a KV v2 secrets engine.

    Atlas never writes credentials to Vault. Secrets are managed
    externally (Vault UI, CLI, Terraform, etc.) and Atlas only reads
    them at runtime.

    Vault *connection* settings (URL, token/role_id, mount path) are
    stored in the OS keyring under a ``vault_`` prefix. When an
    environment is active, these are scoped to the environment's
    keyring namespace (``platform-atlas/<env_name>``) so each
    environment can point to a different Vault instance or path.
    """

    # Keys used to persist Vault connection settings in the OS keyring
    _VAULT_KEYS: tuple[str, ...] = (
        "vault_url",
        "vault_auth_method",
        "vault_token",
        "vault_role_id",
        "vault_secret_id",
        "vault_wrapping_token",
        "vault_token_file_path",
        "vault_mount_point",
        "vault_secret_path",
        "vault_verify_ssl",
        "vault_namespace",
    )

    def __init__(
        self,
        vault_config: VaultConfig | None = None,
        service: str = SERVICE_NAME,
    ) -> None:
        self._hvac = self._import_hvac()
        self._service = service

        if vault_config is None:
            vault_config = self._load_config_from_keyring(service=service)

        self._config = vault_config
        self._token_ttl: int = 0
        self._token_renewable: bool = False
        self._token_expires_at: float = 0.0   # monotonic timestamp; 0 = unknown
        self._refresh_lock: threading.Lock = threading.Lock()
        self._client = self._connect(vault_config)
        self._cached_data: dict[str, str] | None = None

    @property
    def read_only(self) -> bool:
        return True

    # --- hvac import (lazy, so non-Vault users don't need it) ---

    @staticmethod
    def _import_hvac():
        """Import hvac at runtime so it's only required when Vault is selected."""
        try:
            import hvac  # type: ignore[import-untyped]
            return hvac
        except ImportError:
            raise CredentialError(
                "HashiCorp Vault support requires the 'hvac' package",
                details={"fix": "Run: poetry add hvac  (or pip install hvac)"},
            )

    # --- Keyring persistence for Vault connection settings ---

    @classmethod
    def save_config_to_keyring(
        cls,
        config: VaultConfig,
        service: str = SERVICE_NAME,
        store: SecretStore | None = None,
    ) -> None:
        """Persist Vault connection settings into a local secret store.

        ``store`` defaults to :func:`active_secret_store` (the env's configured
        local store). The setup wizard passes an explicit store because the new
        environment's choice is not yet the active config. The method name is
        historical.

        Args:
            config: The Vault connection configuration to save.
            service: The store service name. Pass a scoped name
                     (``scoped_service_name(env_name)``) to isolate Vault
                     settings per environment.
            store: The local secret store to write into; defaults to the active.
        """
        mapping: dict[str, str] = {
            "vault_url":              config.url,
            "vault_auth_method":      config.auth_method.value,
            "vault_token":            config.token or "",
            "vault_role_id":          config.role_id or "",
            "vault_secret_id":        config.secret_id or "",
            "vault_wrapping_token":   config.wrapping_token or "",
            "vault_token_file_path":  config.token_file_path or "",
            "vault_mount_point":      config.mount_point,
            "vault_secret_path":      config.secret_path,
            "vault_verify_ssl":       str(config.verify_ssl),
            "vault_namespace":        config.namespace or "",
        }
        store = store or active_secret_store()
        for k, v in mapping.items():
            if v:
                store.set(service, k, v)
            else:
                # Clean up empty values so _load doesn't pick up stale data
                store.delete(service, k)

    @classmethod
    def _load_config_from_keyring(
        cls,
        service: str = SERVICE_NAME,
    ) -> VaultConfig:
        """Reconstruct VaultConfig from OS keyring entries.

        Args:
            service: The keyring service name to read from.
        """
        # Vault's own connection settings live in the local secret store chosen
        # for this environment (active_secret_store() — OS keyring or encrypted
        # file; deterministic, no probing or fallback).
        store = active_secret_store()

        def _get(key: str) -> str | None:
            try:
                return store.get(service, key)
            except Exception:
                return None

        url = _get("vault_url")
        if not url:
            raise CredentialError(
                "Vault URL not found in the credential store. Run "
                "'platform-atlas config init' and select Vault as the credential backend.",
                details={
                    "service": service,
                    "fix": "Run 'platform-atlas config init' and select Vault as the credential backend",
                },
            )

        auth_str = _get("vault_auth_method") or "token"
        try:
            auth_method = VaultAuthMethod(auth_str)
        except ValueError:
            logger.warning("Unknown Vault auth method '%s', defaulting to token", auth_str)
            auth_method = VaultAuthMethod.TOKEN

        return VaultConfig(
            url=url,
            auth_method=auth_method,
            token=_get("vault_token"),
            role_id=_get("vault_role_id"),
            secret_id=_get("vault_secret_id"),
            wrapping_token=_get("vault_wrapping_token"),
            token_file_path=_get("vault_token_file_path"),
            mount_point=_get("vault_mount_point") or "secret",
            secret_path=_get("vault_secret_path") or "platform-atlas",
            verify_ssl=(_get("vault_verify_ssl") or "true").lower() == "true",
            namespace=_get("vault_namespace") or None,
        )

    @classmethod
    def config_exists_in_keyring(cls, service: str = SERVICE_NAME) -> bool:
        """Check whether Vault connection settings have been stored."""
        try:
            return active_secret_store().get(service, "vault_url") is not None
        except Exception:
            return False

    @classmethod
    def clear_config_from_keyring(cls, service: str = SERVICE_NAME) -> None:
        """Remove all Vault connection settings from the active secret store."""
        store = active_secret_store()
        for k in cls._VAULT_KEYS:
            try:
                store.delete(service, k)
            except Exception as e:
                logger.warning("Failed to delete vault key '%s': %s", k, e)

    # --- Connection ---

    def _connect(self, config: VaultConfig):
        """Authenticate and return an hvac.Client."""
        hvac = self._hvac

        try:
            client = hvac.Client(
                url=config.url,
                verify=config.verify_ssl,
                namespace=config.namespace,
            )
        except Exception as e:
            raise CredentialError(
                f"Failed to create Vault client for {config.url}",
                details={
                    "url": config.url,
                    "error": str(e),
                    "fix": "Verify Vault URL is correct and accessible",
                },
            ) from e

        if config.auth_method == VaultAuthMethod.TOKEN:
            if not config.token:
                raise CredentialError(
                    "Vault token auth selected but no token provided",
                    details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                )
            client.token = config.token

        elif config.auth_method == VaultAuthMethod.APPROLE:
            if not config.role_id or not config.secret_id:
                raise CredentialError(
                    "Vault AppRole auth requires both role_id and secret_id",
                    details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                )
            try:
                resp = client.auth.approle.login(
                    role_id=config.role_id,
                    secret_id=config.secret_id,
                )
                client.token = resp["auth"]["client_token"]
            except ConnectionError as e:
                raise CredentialError(
                    f"Vault unreachable at {config.url}",
                    details={
                        "url": config.url,
                        "error": str(e),
                        "fix": "Verify Vault is running and accessible",
                    },
                ) from e
            except Exception as e:
                raise CredentialError(
                    "Vault AppRole authentication failed",
                    details={"url": config.url, "error": str(e)},
                ) from e

        elif config.auth_method == VaultAuthMethod.APPROLE_WRAPPED:
            if not config.role_id or not config.wrapping_token:
                raise CredentialError(
                    "Vault AppRole (wrapped) auth requires both role_id and a wrapping token",
                    details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                )
            try:
                # Unwrap the one-time-use wrapping token to retrieve the actual secret_id
                unwrap_resp = client.sys.unwrap(wrapping_token=config.wrapping_token)
                secret_id = unwrap_resp.get("data", {}).get("secret_id")
                if not secret_id:
                    raise CredentialError(
                        "Vault unwrap response did not contain a secret_id",
                        details={
                            "url": config.url,
                            "fix": "Verify the wrapping token was generated for an AppRole secret_id",
                        },
                    )
                resp = client.auth.approle.login(
                    role_id=config.role_id,
                    secret_id=secret_id,
                )
                client.token = resp["auth"]["client_token"]
            except CredentialError:
                raise
            except ConnectionError as e:
                raise CredentialError(
                    f"Vault unreachable at {config.url}",
                    details={
                        "url": config.url,
                        "error": str(e),
                        "fix": "Verify Vault is running and accessible",
                    },
                ) from e
            except Exception as e:
                raise CredentialError(
                    "Vault AppRole (wrapped) authentication failed — "
                    "the wrapping token may have already been used or has expired",
                    details={
                        "url": config.url,
                        "error": str(e),
                        "fix": "Obtain a new wrapping token and run 'platform-atlas config credentials'",
                    },
                ) from e

        elif config.auth_method == VaultAuthMethod.TOKEN_FILE:
            if not config.token_file_path:
                raise CredentialError(
                    "Vault token_file auth selected but no file path configured",
                    details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                )
            try:
                from pathlib import Path
                token = Path(config.token_file_path).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                raise CredentialError(
                    f"Vault token file not found: {config.token_file_path}",
                    details={
                        "path": config.token_file_path,
                        "fix": "Verify Vault Agent is running and writing to the configured sink path",
                    },
                )
            except OSError as e:
                raise CredentialError(
                    f"Could not read Vault token file: {config.token_file_path}",
                    details={"path": config.token_file_path, "error": str(e)},
                ) from e
            if not token:
                raise CredentialError(
                    f"Vault token file is empty: {config.token_file_path}",
                    details={"fix": "Verify Vault Agent has written a valid token to the sink file"},
                )
            client.token = token

        elif config.auth_method == VaultAuthMethod.TOKEN_ENV:
            import os
            token = os.environ.get("VAULT_TOKEN", "").strip()
            if not token:
                raise CredentialError(
                    "VAULT_TOKEN environment variable is not set or is empty",
                    details={
                        "fix": "Set VAULT_TOKEN before running platform-atlas, "
                               "or switch to a different auth method via 'platform-atlas config credentials'",
                    },
                )
            client.token = token

        try:
            authenticated = client.is_authenticated()
        except ConnectionError as e:
            raise CredentialError(
                f"Vault unreachable at {config.url}",
                details={
                    "url": config.url,
                    "error": str(e),
                    "fix": "Verify Vault is running and accessible",
                },
            ) from e
        except Exception as e:
            raise CredentialError(
                f"Vault authentication check failed at {config.url}",
                details={
                    "url": config.url,
                    "error": str(e),
                    "fix": "Verify Vault connectivity and credentials",
                },
            ) from e

        if not authenticated:
            details: dict[str, str] = {"url": config.url, "method": config.auth_method.value}
            if config.auth_method == VaultAuthMethod.TOKEN:
                details["fix"] = (
                    "The token may have expired — obtain a new token and run "
                    "'platform-atlas config credentials' to update"
                )
            raise CredentialError(
                "Vault authentication failed — client is not authenticated",
                details=details,
            )

        # Inspect token metadata to surface TTL and renewability
        try:
            info = client.auth.token.lookup_self()
            self._token_ttl = int(info["data"].get("ttl", 0))
            self._token_renewable = bool(info["data"].get("renewable", False))
            logger.info(
                "Vault token TTL: %ds, renewable: %s (auth=%s)",
                self._token_ttl, self._token_renewable, config.auth_method.value,
            )
            if self._token_ttl > 0:
                self._token_expires_at = time.monotonic() + self._token_ttl
            if config.auth_method == VaultAuthMethod.TOKEN and 0 < self._token_ttl < 60:
                raise CredentialError(
                    "Vault token has expired or is about to expire",
                    details={
                        "ttl_remaining": f"{self._token_ttl}s",
                        "fix": "Obtain a new token and run 'platform-atlas config credentials' to update",
                    },
                )
        except CredentialError:
            raise
        except Exception as e:
            logger.debug("Could not inspect Vault token metadata: %s", e)

        logger.info("Connected to Vault at %s (auth=%s)", config.url, config.auth_method.value)
        return client

    # --- Token refresh (transparent, for long-running processes) ---

    def _is_token_near_expiry(self) -> bool:
        """True when the token has less than _VAULT_REFRESH_MARGIN seconds remaining."""
        if self._token_expires_at == 0.0:
            return False  # TTL unknown — assume still valid
        return time.monotonic() >= (self._token_expires_at - _VAULT_REFRESH_MARGIN)

    def _refresh_token(self) -> None:
        """Re-authenticate to get a fresh token. Thread-safe; clears the secret cache.

        Called automatically by _read_all() when the token is near expiry so that
        long-running processes (WebUI) never see a mid-session 403 from Vault.

        Auth-method behaviour:
          APPROLE       — calls login() again with stored role_id + secret_id
          TOKEN_FILE    — re-reads the file Vault Agent keeps current
          TOKEN_ENV     — re-reads VAULT_TOKEN from the environment
          TOKEN         — calls renew_self() if the token is renewable
          APPROLE_WRAPPED / others — raises; these cannot be automatically refreshed
        """
        with self._refresh_lock:
            # Another thread may have refreshed while we waited for the lock
            if not self._is_token_near_expiry():
                return

            config = self._config
            logger.info(
                "Vault token nearing expiry — refreshing (auth=%s, ttl=%ds)",
                config.auth_method.value, self._token_ttl,
            )

            try:
                if config.auth_method == VaultAuthMethod.APPROLE:
                    if not config.role_id or not config.secret_id:
                        raise CredentialError(
                            "Vault AppRole re-authentication failed — role_id or secret_id missing",
                            details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                        )
                    resp = self._client.auth.approle.login(
                        role_id=config.role_id,
                        secret_id=config.secret_id,
                    )
                    self._client.token = resp["auth"]["client_token"]

                elif config.auth_method == VaultAuthMethod.TOKEN_FILE:
                    from pathlib import Path
                    if not config.token_file_path:
                        raise CredentialError(
                            "Vault token file path not configured",
                            details={"fix": "Run 'platform-atlas config credentials' to reconfigure"},
                        )
                    token = Path(config.token_file_path).read_text(encoding="utf-8").strip()
                    if not token:
                        raise CredentialError(
                            f"Vault token file is empty: {config.token_file_path}",
                            details={"fix": "Verify Vault Agent is running and writing to the sink file"},
                        )
                    self._client.token = token

                elif config.auth_method == VaultAuthMethod.TOKEN_ENV:
                    import os
                    token = os.environ.get("VAULT_TOKEN", "").strip()
                    if not token:
                        raise CredentialError(
                            "VAULT_TOKEN environment variable is not set or is empty",
                            details={"fix": "Ensure VAULT_TOKEN is set in the environment"},
                        )
                    self._client.token = token

                elif config.auth_method == VaultAuthMethod.TOKEN:
                    if not self._token_renewable:
                        raise CredentialError(
                            "Vault token has expired and is not renewable — manual update required",
                            details={
                                "fix": "Obtain a new token and run 'platform-atlas config credentials' to update",
                            },
                        )
                    self._client.auth.token.renew_self()

                else:
                    raise CredentialError(
                        f"Vault auth method '{config.auth_method.value}' does not support "
                        "automatic token refresh",
                        details={
                            "method": config.auth_method.value,
                            "fix": (
                                "Use AppRole, Token (file), or Token (env) for long-running "
                                "deployments. AppRole (Wrapped) tokens are one-time-use and "
                                "cannot be automatically refreshed."
                            ),
                        },
                    )

            except CredentialError:
                raise
            except Exception as e:
                raise CredentialError(
                    f"Vault token refresh failed ({config.auth_method.value})",
                    details={"error": str(e)},
                ) from e

            # Re-inspect the new token's TTL and reset the expiry clock
            try:
                info = self._client.auth.token.lookup_self()
                self._token_ttl = int(info["data"].get("ttl", 0))
                self._token_renewable = bool(info["data"].get("renewable", False))
                if self._token_ttl > 0:
                    self._token_expires_at = time.monotonic() + self._token_ttl
                logger.info(
                    "Vault token refreshed — new TTL: %ds, renewable: %s",
                    self._token_ttl, self._token_renewable,
                )
            except CredentialError:
                raise
            except Exception as e:
                logger.debug("Could not inspect refreshed token metadata: %s", e)

            # Clear cached secrets so the next read fetches a fresh copy from Vault.
            # Time has passed since the last read; secrets may have been rotated.
            self._cached_data = None

    # --- CredentialBackend interface (read-only) ---

    def _read_all(self) -> dict[str, str]:
        """Read the full secret dict from Vault KV v2, cached per instance.

        Automatically refreshes the token when it is near expiry so long-running
        processes (WebUI) never hit a mid-session 403.
        """
        if self._is_token_near_expiry():
            self._refresh_token()

        if self._cached_data is not None:
            return self._cached_data
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*raise_on_deleted_version.*", category=DeprecationWarning)
                resp = self._client.secrets.kv.v2.read_secret_version(
                    path=self._config.secret_path,
                    mount_point=self._config.mount_point,
                )
            self._cached_data = resp.get("data", {}).get("data", {})
            return self._cached_data
        except ConnectionError as e:
            raise CredentialError(
                f"Vault unreachable at {self._config.url}",
                details={
                    "url": self._config.url,
                    "error": str(e),
                    "fix": "Verify Vault is running and accessible",
                },
            ) from e
        except Exception as e:
            raise CredentialError(
                "Vault secret read failed — token may have expired or lacks read permission",
                details={
                    "url": self._config.url,
                    "path": self._config.full_path,
                    "error": str(e),
                    "fix": "Verify your Vault token is valid and has not expired; "
                           "run 'platform-atlas config credentials' to update",
                },
            ) from e

    def get(self, key: str) -> str | None:
        data = self._read_all()
        value = data.get(key)
        return value if value else None  # Treat empty strings as missing

    def set(self, key: str, value: str) -> None:
        """Vault is read-only from Atlas — credentials are managed externally."""
        raise CredentialError(
            f"Cannot write '{key}' — Vault backend is read-only",
            details={
                "fix": f"Add this secret directly in Vault at {self._config.full_path}",
            },
        )

    def delete(self, key: str) -> None:
        """Vault is read-only from Atlas — credentials are managed externally."""
        raise CredentialError(
            f"Cannot delete '{key}' — Vault backend is read-only",
            details={
                "fix": f"Remove this secret directly in Vault at {self._config.full_path}",
            },
        )

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    @property
    def config(self) -> VaultConfig:
        """Expose the active Vault configuration (for UI display)."""
        return self._config

    @property
    def token_ttl(self) -> int:
        """Remaining token TTL in seconds at connect time (0 if unknown or not applicable)."""
        return self._token_ttl

    @property
    def token_renewable(self) -> bool:
        """True if the active token can be renewed via renew_self()."""
        return self._token_renewable

    def revoke_token(self) -> None:
        """Proactively revoke the Vault token. Safe to call on cleanup — never raises."""
        try:
            self._client.auth.token.revoke_self()
            logger.info("Vault token revoked")
        except Exception as e:
            logger.debug("Vault token revocation failed (non-critical): %s", e)

    def __repr__(self) -> str:
        return f"VaultBackend(url={self._config.url!r}, path={self._config.secret_path!r})"

class CredentialStore:
    """
    Unified credential facade — delegates to whichever backend is active.

    The public API is identical regardless of backend, so callers like
    Config properties, collectors, and preflight checks never need to
    know whether credentials live in the OS keyring or in Vault.

    When an environment is active, the keyring service is scoped to
    ``platform-atlas/<env_name>`` for full isolation. This applies to
    both regular credentials (keyring backend) and Vault connection
    settings (vault backend loads its config from the scoped namespace).

    When the Vault backend is active, the store is **read-only** —
    ``set()``, ``delete()``, and ``clear_all()`` will raise or no-op.

    Usage:
        store = CredentialStore()
        secret = store.get(CredentialKey.PLATFORM_SECRET)
        # Writes only work on Keyring backend:
        store.set(CredentialKey.PLATFORM_SECRET, "my-secret")
    """

    def __init__(
        self,
        service: str = SERVICE_NAME,
        backend_type: CredentialBackendType = CredentialBackendType.KEYRING,
        env_name: str | None = None,
    ) -> None:
        # Scope the service name to the active environment (if any)
        self._env_name = env_name
        self._service = scoped_service_name(env_name) if env_name else service
        self._backend_type = backend_type
        self._backend: CredentialBackend = self._init_backend(backend_type)

    def _init_backend(self, backend_type: CredentialBackendType) -> CredentialBackend:
        """Instantiate the appropriate backend."""
        if backend_type == CredentialBackendType.VAULT:
            # Pass the scoped service so Vault connection config is
            # loaded from the environment's local-store namespace
            return VaultBackend(service=self._service)
        # KEYRING and FILE both use the local backend; whether secrets land in
        # the OS keyring or the encrypted file is decided by active_secret_store().
        return KeyringBackend(self._service)

    # --- Properties ---

    @property
    def backend_type(self) -> CredentialBackendType:
        """The active backend type enum."""
        return self._backend_type

    @property
    def backend_name(self) -> str:
        """Human-readable backend description for UI display."""
        if self._backend_type == CredentialBackendType.VAULT:
            vault = self._backend
            if isinstance(vault, VaultBackend):
                return f"HashiCorp Vault ({vault.config.display_url})"
            return "HashiCorp Vault"
        env_suffix = f" [{self._env_name}]" if self._env_name else ""
        return f"{active_secret_store().display_name}{env_suffix}"

    @property
    def env_name(self) -> str | None:
        """The environment this store is scoped to, or None for legacy mode."""
        return self._env_name

    @property
    def service(self) -> str:
        """The keyring service name in use (for display/debugging)."""
        return self._service

    @property
    def is_vault(self) -> bool:
        """Convenience check for Vault mode."""
        return self._backend_type == CredentialBackendType.VAULT

    @property
    def is_read_only(self) -> bool:
        """True if the active backend does not support writes."""
        return self._backend.read_only

    # --- Core operations ---

    def get(self, key: CredentialKey) -> str | None:
        """
        Retrieve a credential. Returns None if not found.

        Keys outside the active tier's applicable set silently return None —
        Extended-only keys in Standard, Platform/Mongo/Redis keys in SaaS.
        Those code paths should never ask in the first place, but if
        something does, it gets a clean miss rather than leaking a stale
        credential across the tier boundary.
        """
        tier = _active_tier()
        if tier is not None and key not in applicable_keys(tier):
            return None
        return self._backend.get(key.value)

    def set(self, key: CredentialKey, value: str) -> None:
        """
        Store a credential in the active backend.

        Raises CredentialError if the backend is read-only (Vault).
        Raises TierViolationError if writing a key outside the active
        tier's applicable set (e.g. an Extended-only key under standard,
        or a Platform/Mongo/Redis key under saas).
        """
        if not value:
            logger.debug("Skipping empty value for %s", key.value)
            return
        tier = _active_tier()
        if tier is not None and key not in applicable_keys(tier):
            raise TierViolationError(
                f"credential_store.set({key.value})",
                hint=f"{key.display_name} is not used in {_TIER_LABELS.get(tier, tier)} Mode.",
            )
        if self.is_read_only:
            raise CredentialError(
                f"Cannot store {key.display_name} — backend is read-only",
                details={
                    "backend": self._backend_type.value,
                    "fix": "Manage this credential directly in Vault",
                },
            )
        self._backend.set(key.value, value)

    def delete(self, key: CredentialKey) -> None:
        """
        Remove a credential from the active backend.

        Raises CredentialError if the backend is read-only (Vault).
        Tier-agnostic: deletes are always allowed so a downgrade flow can
        clean up Extended creds without bumping the boundary.
        """
        if self.is_read_only:
            raise CredentialError(
                f"Cannot delete {key.display_name} — backend is read-only",
                details={
                    "backend": self._backend_type.value,
                    "fix": "Manage this credential directly in Vault",
                },
            )
        self._backend.delete(key.value)

    def exists(self, key: CredentialKey) -> bool:
        """
        Check if a credential is stored.

        Mirrors get(): keys outside the active tier's applicable set
        appear absent so the public surface is consistent.
        """
        tier = _active_tier()
        if tier is not None and key not in applicable_keys(tier):
            return False
        return self._backend.exists(key.value)

    # --- Bulk operations ---

    def get_required(self, key: CredentialKey) -> str:
        """
        Retrieve a credential, raising if missing.
        Use this in collectors that cannot function without the credential.
        """
        value = self.get(key)
        if value is None:
            backend_label = self._backend_type.value
            if self.is_vault:
                vault = self._backend
                vault_path = ""
                if isinstance(vault, VaultBackend):
                    vault_path = vault.config.full_path
                fix = f"Add '{key.value}' to Vault at {vault_path}"
            else:
                # Reflect the real substrate so the message isn't misleading when
                # the encrypted local file store is active instead of the keyring.
                if active_secret_store().is_file:
                    backend_label = "encrypted local file"
                fix = "Run 'platform-atlas config credentials' to configure"

            raise CredentialError(
                f"{key.display_name} not found in {backend_label} backend",
                details={"key": key.value, "backend": backend_label, "fix": fix},
            )
        return value

    def status(self) -> dict[CredentialKey, bool]:
        """Return which credentials are stored (for preflight checks)."""
        return {key: self.exists(key) for key in CredentialKey}

    def clear_all(self) -> None:
        """Remove all Platform Atlas credentials from the active backend."""
        if self.is_read_only:
            logger.warning("clear_all() skipped — %s backend is read-only", self._backend_type.value)
            return
        for key in CredentialKey:
            self.delete(key)

    def __repr__(self) -> str:
        return (
            f"CredentialStore(backend={self._backend_type.value}, "
            f"env={self._env_name!r}, service={self._service!r}, "
            f"read_only={self.is_read_only}, impl={self._backend!r})"
        )

# Backends that cannot store credentials at all — operations silently no-op or error
_BROKEN_BACKENDS = frozenset({
    "NullKeyring",
    "FailKeyring",
})

# Backends that work but store without strong encryption
_INSECURE_BACKENDS = frozenset({
    "PlaintextKeyring",
    "ChainerBackend",
    *_BROKEN_BACKENDS,
})


_PROBE_SERVICE = "platform-atlas-probe"
_PROBE_KEY = "__atlas_probe__"


def _probe_keyring() -> bool:
    """Return True if the active keyring can actually write and read."""
    try:
        keyring.set_password(_PROBE_SERVICE, _PROBE_KEY, "ok")
        val = keyring.get_password(_PROBE_SERVICE, _PROBE_KEY)
        try:
            keyring.delete_password(_PROBE_SERVICE, _PROBE_KEY)
        except Exception:
            pass
        return val == "ok"
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Secret-store substrate  (OS keyring  ⇄  encrypted local file)
#
# The low-level (service, key) → value store that BOTH the local credential
# backend (KeyringBackend) and Vault's connection-settings persistence sit on
# top of. Two implementations: the OS keyring, and an encrypted machine-bound
# file used as a seamless fallback when the keyring is unavailable or forced.
# ═══════════════════════════════════════════════════════════════════════════


@runtime_checkable
class SecretStore(Protocol):
    """A namespaced ``(service, key) → value`` secret store."""

    #: True for the encrypted local file store; False for the OS keyring.
    is_file: bool
    #: Human-readable label for UI / preflight.
    display_name: str

    def get(self, service: str, key: str) -> str | None: ...
    def set(self, service: str, key: str, value: str) -> None: ...
    def delete(self, service: str, key: str) -> None: ...
    def exists(self, service: str, key: str) -> bool: ...


class KeyringSecretStore:
    """SecretStore backed by the OS keyring (keyring library)."""

    is_file = False

    def get(self, service: str, key: str) -> str | None:
        try:
            return keyring.get_password(service, key)
        except (keyring.errors.KeyringError, ValueError) as e:
            logger.warning("Keyring read failed for %s: %s", key, e)
            return None

    def set(self, service: str, key: str, value: str) -> None:
        try:
            keyring.set_password(service, key, value)
        except keyring.errors.KeyringError as e:
            raise CredentialError(
                f"Failed to store '{key}' in OS keyring",
                details={"key": key, "error": str(e)},
            ) from e
        except ValueError as e:
            raise CredentialError(
                "Incorrect keyring password. Re-run and enter the correct password "
                "for the encrypted keyring, or delete the keyring file to start fresh.",
                details={"key": key, "error": str(e)},
            ) from e

    def delete(self, service: str, key: str) -> None:
        try:
            keyring.delete_password(service, key)
        except keyring.errors.PasswordDeleteError:
            pass  # Already gone
        except keyring.errors.KeyringError as e:
            logger.warning("Keyring delete failed for %s: %s", key, e)

    def exists(self, service: str, key: str) -> bool:
        return self.get(service, key) is not None

    @property
    def display_name(self) -> str:
        _, _, name = verify_keyring_backend()
        return f"OS Keyring ({name})"

    def __repr__(self) -> str:
        return "KeyringSecretStore()"


# AES-GCM associated data — binds the ciphertext to this store's purpose+version
# so a blob can't be replayed into another context, and tampering fails loudly.
_FILE_STORE_AAD = b"platform-atlas-credential-store-v1"
_FILE_STORE_VERSION = 1
# scrypt cost parameters (n must be a power of two). 2**14 is fast (<100ms) yet
# meaningfully slows brute force; stored in the envelope so they can evolve.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _machine_id() -> str:
    """Best-effort stable per-host identifier. Empty string if unavailable."""
    import platform as _platform
    system = _platform.system()
    try:
        if system == "Linux":
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    val = Path(p).read_text(encoding="utf-8").strip()
                    if val:
                        return val
                except OSError:
                    continue
        elif system == "Darwin":
            import re
            import subprocess  # nosec B404 — fixed argv, no shell
            out = subprocess.run(  # nosec B603 B607
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', out)
            if match:
                return match.group(1)
        elif system == "Windows":
            import winreg  # type: ignore[import-not-found]  # pylint: disable=import-error
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
                if val:
                    return str(val)
    except Exception as e:
        logger.debug("machine-id lookup failed: %s", e)
    return ""


def _machine_identity() -> list[str]:
    """Host+user identity inputs mixed into the file-store key derivation.

    Degrades gracefully: any part may be empty (e.g. no machine-id in a minimal
    container). The random per-install salt is the always-present anchor, so a
    usable key is always produced.
    """
    import getpass
    import socket
    parts = [_machine_id()]
    try:
        parts.append(getpass.getuser())
    except Exception:
        parts.append("")
    if hasattr(os, "getuid"):
        parts.append(str(os.getuid()))
    try:
        parts.append(socket.gethostname())
    except Exception:
        parts.append("")
    return parts


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write text to ``path`` atomically with restrictive (0o600) permissions."""
    import tempfile
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-cred-")
    try:
        try:
            os.fchmod(fd, mode)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass  # Windows / unsupported — best effort; chmod below
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


def _check_perms(path: Path) -> None:
    """Tighten a credential file back to 0o600 if it became group/world accessible."""
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.debug("Tightening permissions on %s (was %s)", path, oct(mode))
            os.chmod(str(path), 0o600)
    except OSError:
        pass


@unique
class FileStoreHealth(Enum):
    """On-disk state of the encrypted local credential file."""
    EMPTY = "empty"            # no file yet (fresh install, or files deleted)
    OK = "ok"                  # file present and decrypts on this host
    UNREADABLE = "unreadable"  # file present but salt missing / wrong key / corrupt


# Serializes the whole-blob read-modify-write so concurrent writers (e.g. a
# continuous-notifications run racing an interactive `config credentials`, or
# the WebUI's worker threads) can't clobber each other's keys. In-process via
# this lock; cross-process via an advisory flock taken in `_write_lock`.
_FILE_STORE_WRITE_LOCK = threading.Lock()


class FileSecretStore:
    """Encrypted, machine-bound local credential store.

    The seamless fallback for when the OS keyring is unusable. All secrets live
    in ``~/.atlas/credentials.enc`` as a single AES-256-GCM ciphertext; the key
    is derived (scrypt) from host + user identity plus a random per-install salt
    kept separately in ``~/.atlas/.keysalt``. Consequences:

      • The file is **non-portable** — it won't decrypt on another host/user.
      • A **single leaked file** is useless without the salt file too.

    Honest about its limits (preflight reports it as a *warning*, never as a
    secure keyring): this protects against stolen disks/backups, leaked files,
    and casual inspection — NOT against code already running as the user. A
    decrypt failure (machine changed, salt lost, tamper) is treated as an empty
    store so the app keeps working and the user simply re-enters credentials.
    """

    is_file = True
    display_name = "Encrypted local file"

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, str]] | None = None
        self._key: bytes | None = None

    @staticmethod
    def _file() -> Path:
        from platform_atlas.core import paths
        return paths.ATLAS_HOME / "credentials.enc"

    @staticmethod
    def _salt_file() -> Path:
        from platform_atlas.core import paths
        return paths.ATLAS_HOME / ".keysalt"

    @staticmethod
    def _lock_file() -> Path:
        from platform_atlas.core import paths
        return paths.ATLAS_HOME / ".credentials.lock"

    @contextmanager
    def _write_lock(self):
        """Serialize the read-modify-write of the whole-blob file across threads
        and processes so concurrent writers can't drop each other's keys.

        In-process via the module lock; cross-process via an advisory flock where
        available (POSIX). Best-effort: if the OS lock can't be taken we still
        hold the in-process lock and proceed.
        """
        with _FILE_STORE_WRITE_LOCK:
            fd = None
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                try:
                    lp = self._lock_file()
                    lp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as e:
                    logger.debug("Could not acquire credential file lock: %s", e)
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fd = None
            try:
                yield
            finally:
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _load_or_create_salt(self) -> bytes:
        p = self._salt_file()
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8").strip()
                salt = base64.b64decode(raw) if raw else b""
            except (OSError, ValueError) as e:
                logger.debug("Key salt present but unreadable: %s", e)
                salt = b""
            if salt:
                return salt
            # The salt file exists but is empty/corrupt/unreadable. If an encrypted
            # blob also exists, regenerating now would derive a new key and the
            # next write would overwrite still-recoverable ciphertext — refuse and
            # let the recovery flow ('config credentials') recreate the pair.
            if self._file().exists():
                raise CredentialError(
                    "credential key salt (~/.atlas/.keysalt) is present but unreadable; "
                    "refusing to regenerate it while ~/.atlas/credentials.enc exists. "
                    "Run 'platform-atlas config credentials' to recreate the store."
                )
        salt = os.urandom(16)
        _atomic_write_text(p, base64.b64encode(salt).decode("ascii"))
        return salt

    def _derive_key(self) -> bytes:
        if self._key is not None:
            return self._key
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        salt = self._load_or_create_salt()
        material = "\x1f".join(_machine_identity()).encode("utf-8")
        kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        self._key = kdf.derive(material)
        return self._key

    def _try_decrypt(self) -> dict[str, dict[str, str]]:
        """Read + decrypt the file, raising on any problem. Never creates a salt
        (a read must not paper over a missing key). Raises FileNotFoundError if
        the file is absent, CredentialError if the salt is gone, or a crypto
        error if the key is wrong (moved host) or the ciphertext is corrupt.
        """
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        envelope = json.loads(self._file().read_text(encoding="utf-8"))
        salt_path = self._salt_file()
        if not salt_path.exists():
            raise CredentialError("credential key salt (~/.atlas/.keysalt) is missing")
        salt = base64.b64decode(salt_path.read_text(encoding="utf-8").strip())
        key = Scrypt(
            salt=salt, length=32,
            n=int(envelope.get("n", _SCRYPT_N)),
            r=int(envelope.get("r", _SCRYPT_R)),
            p=int(envelope.get("p", _SCRYPT_P)),
        ).derive("\x1f".join(_machine_identity()).encode("utf-8"))
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(envelope["nonce"]),
            base64.b64decode(envelope["ciphertext"]),
            _FILE_STORE_AAD,
        )
        return json.loads(plaintext.decode("utf-8"))

    def _read_all(self) -> dict[str, dict[str, str]]:
        if self._cache is not None:
            return self._cache
        if not self._file().exists():
            self._cache = {}
            return self._cache
        _check_perms(self._file())
        _check_perms(self._salt_file())   # tighten the salt too if it was loosened
        try:
            self._cache = self._try_decrypt()
        except FileNotFoundError:
            self._cache = {}
        except Exception as e:
            # Salt lost, host changed, or tampering. Treat as empty so the app
            # keeps working — never crash. health()/preflight surface it clearly,
            # and 'config credentials' offers to recreate the file.
            logger.warning(
                "Local credential file could not be read (%s) — treating as empty. "
                "Run 'platform-atlas config credentials' to recreate it.", type(e).__name__,
            )
            self._cache = {}
        return self._cache

    def _write_all(self, data: dict[str, dict[str, str]]) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(12)
            plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
            ciphertext = AESGCM(self._derive_key()).encrypt(nonce, plaintext, _FILE_STORE_AAD)
            envelope = {
                "version": _FILE_STORE_VERSION,
                "kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
            _atomic_write_text(self._file(), json.dumps(envelope, ensure_ascii=False))
        except CredentialError:
            raise
        except Exception as e:
            raise CredentialError(
                "Failed to write the local credential file",
                details={"path": str(self._file()), "error": str(e)},
            ) from e
        self._cache = data

    def get(self, service: str, key: str) -> str | None:
        val = self._read_all().get(service, {}).get(key)
        return val if val else None  # Treat empty strings as missing

    def set(self, service: str, key: str, value: str) -> None:
        with self._write_lock():
            self._cache = None                      # re-read current on-disk state
            data = self._read_all()
            data.setdefault(service, {})[key] = value
            self._write_all(data)

    def delete(self, service: str, key: str) -> None:
        with self._write_lock():
            self._cache = None
            data = self._read_all()
            bucket = data.get(service)
            if bucket and key in bucket:
                del bucket[key]
                if not bucket:
                    del data[service]
                self._write_all(data)

    def exists(self, service: str, key: str) -> bool:
        return self.get(service, key) is not None

    def health(self) -> FileStoreHealth:
        """Classify the on-disk state without modifying anything.

        EMPTY      → no file yet (fresh, or deleted) — just store credentials.
        OK         → file present and decrypts on this host.
        UNREADABLE → file present but the salt is missing, the key is wrong
                     (moved from another machine), or the ciphertext is corrupt
                     — the file must be recreated and credentials re-entered.
        """
        if not self._file().exists():
            return FileStoreHealth.EMPTY
        try:
            self._try_decrypt()
            return FileStoreHealth.OK
        except FileNotFoundError:
            return FileStoreHealth.EMPTY
        except Exception:
            return FileStoreHealth.UNREADABLE

    def reset(self) -> None:
        """Delete the credential file and its salt so a fresh, valid pair is
        created the next time a credential is stored. Used by the recovery flow
        when the existing file is unreadable.
        """
        for path in (self._file(), self._salt_file()):
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.debug("Could not remove %s: %s", path, e)
        self._cache = None
        self._key = None

    def __repr__(self) -> str:
        return "FileSecretStore(path=~/.atlas/credentials.enc)"


# --- Substrate resolution (explicit, config-driven — no probe, no fallback) ---

_secret_store: SecretStore | None = None


def active_secret_store() -> SecretStore:
    """Resolve (once per process) the local secret-store substrate.

    A pure read of the saved choice — there is NO probing and NO automatic
    fallback. Atlas only ever uses the store the user picked:
      • ``credential_backend == "file"``  → encrypted local file
      • ``credential_backend == "vault"`` → the env's ``vault_secret_store``
        (``keyring`` or ``file``; unset means keyring — the pre-2.0 location
        where existing Vault installs already keep their connection settings)
      • otherwise (``"keyring"``)          → OS keyring
    The deprecated ``use_file_store`` flag, if set, still maps to the file store.
    """
    global _secret_store
    if _secret_store is None:
        _secret_store = _resolve_secret_store()
    return _secret_store


def _resolve_secret_store() -> SecretStore:
    """Pick the local substrate from saved config — keyring or encrypted file."""
    backend = "keyring"
    vault_store: str | None = None
    use_file = False
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if is_config_loaded():
            cfg = get_config()
            backend = (cfg.credential_backend or "keyring").strip().lower()
            vault_store = getattr(cfg, "vault_secret_store", None)
            use_file = bool(getattr(cfg, "use_file_store", False))
    except Exception:
        pass  # Config not loaded yet — keyring is the safe default
    if backend == "file" or use_file:
        return FileSecretStore()
    if backend == "vault":
        # Vault's own connection settings need a local home. Default to the OS
        # keyring when unset (where pre-2.0 Vault installs already keep them);
        # honor an explicit "file" otherwise.
        if (vault_store or "keyring").strip().lower() == "file":
            return FileSecretStore()
        return KeyringSecretStore()
    return KeyringSecretStore()


def verify_keyring_backend() -> tuple[bool, bool, str]:
    """Check the active keyring backend.

    Returns:
        (is_secure, is_functional, name) where is_secure means an encrypted
        OS keyring is active, is_functional means credentials can be stored at
        all (even if unencrypted), and name is the backend class name.

    ChainerBackend is probed with a real write/read because it can appear
    functional while silently failing (e.g. SecretService requires a GUI
    unlock that isn't available). A probe failure is reported as
    is_functional=False; the caller (:func:`active_secret_store`) then falls
    back to the encrypted local file store. This function is a pure reporter —
    it never mutates keyring state.
    """
    backend = keyring.get_keyring()
    name = type(backend).__name__

    if name == "ChainerBackend" and not _probe_keyring():
        # Non-functional keyring — the file store takes over upstream.
        return False, False, name

    is_functional = name not in _BROKEN_BACKENDS
    is_secure = name not in _INSECURE_BACKENDS

    # ChainerBackend is a wrapper — its security depends on what it chains to.
    # On macOS it chains to macOS.Keyring (encrypted Keychain); on Windows to
    # WinVaultKeyring; on Linux to SecretService.  If any chained backend is a
    # native OS keyring, the credentials are actually encrypted even though the
    # top-level class name says "ChainerBackend".
    if name == "ChainerBackend":
        _SECURE_MODULE_LABELS = {
            "keyring.backends.macOS":          "macOS Keychain",
            "keyring.backends.Windows":        "Windows Credential Locker",
            "keyring.backends.SecretService":  "SecretService",
        }
        chained = getattr(backend, "backends", [])
        for b in chained:
            label = _SECURE_MODULE_LABELS.get(type(b).__module__)
            if label:
                is_secure = True
                name = label
                break

    return is_secure, is_functional, name


# ═══════════════════════════════════════════════════════════════════════════
# Legacy credential migration
# ═══════════════════════════════════════════════════════════════════════════

def migrate_legacy_credentials(env_name: str) -> int:
    """
    Copy credentials from the flat ``platform-atlas`` keyring namespace
    into the scoped ``platform-atlas/<env_name>`` namespace.

    Migrates both regular credential keys (platform secret, mongo URI, etc.)
    and Vault connection settings (vault URL, token, AppRole, etc.).

    Only copies keys that exist in the old namespace and are MISSING
    in the new one — never overwrites existing scoped credentials.

    Returns the number of keys migrated.
    """
    legacy_service = SERVICE_NAME
    scoped = scoped_service_name(env_name)
    migrated = 0
    store = active_secret_store()

    def _get(service: str, key: str) -> str | None:
        try:
            return store.get(service, key)
        except Exception:
            return None

    def _set(service: str, key: str, value: str) -> bool:
        try:
            store.set(service, key, value)
            return True
        except Exception as e:
            logger.debug("Migration failed for key '%s': %s", key, e)
            return False

    # Migrate regular credential keys
    for cred_key in CredentialKey:
        old_val = _get(legacy_service, cred_key.value)
        new_val = _get(scoped, cred_key.value)
        if old_val and not new_val:
            if _set(scoped, cred_key.value, old_val):
                logger.info("Migrated '%s' → %s", cred_key.value, scoped)
                migrated += 1

    # Migrate Vault connection settings
    for vault_key in VaultBackend._VAULT_KEYS:
        old_val = _get(legacy_service, vault_key)
        new_val = _get(scoped, vault_key)
        if old_val and not new_val:
            if _set(scoped, vault_key, old_val):
                logger.info("Migrated '%s' → %s", vault_key, scoped)
                migrated += 1

    return migrated


# ═══════════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════════

_store: CredentialStore | None = None

def credential_store() -> CredentialStore:
    """
    Get or create the module-level CredentialStore singleton.

    When an active environment is set, ALL keyring data is scoped to
    ``platform-atlas/<env_name>``. On first access with a new environment,
    credentials are auto-migrated from the legacy flat namespace if they
    exist there but not in the scoped namespace.
    """
    global _store
    if _store is None:
        # Determine backend and active environment from config (if loaded).
        # The local substrate (OS keyring vs encrypted file) is resolved lazily
        # by active_secret_store() from the saved credential_backend — no probe.
        backend_type = CredentialBackendType.KEYRING
        env_name: str | None = None
        try:
            from platform_atlas.core.config import get_config
            cfg = get_config()
            backend_type = CredentialBackendType(cfg.credential_backend)
            env_name = cfg.active_environment
        except ValueError as e:
            logger.warning(
                "Invalid credential_backend value in config: %s — defaulting to keyring. "
                "Run 'platform-atlas config set credential_backend keyring' or 'vault' to fix.",
                e,
            )
        except Exception:
            pass  # Config not loaded yet — keyring is a safe default

        # Auto-migrate legacy credentials on first access with an active env
        if env_name:
            try:
                count = migrate_legacy_credentials(env_name)
                if count:
                    logger.info(
                        "Auto-migrated %d credential(s) to environment '%s'",
                        count, env_name,
                    )
            except Exception as e:
                logger.debug("Credential migration check failed: %s", e)

        _store = CredentialStore(backend_type=backend_type, env_name=env_name)
    return _store


def reset_credential_store() -> None:
    """
    Reset the singletons so they are re-created on next access.

    Call this after changing ``credential_backend`` or ``vault_secret_store``
    in config so the store re-resolves both the backend type and the local
    secret-store substrate.
    """
    global _store, _secret_store
    _store = None
    _secret_store = None
