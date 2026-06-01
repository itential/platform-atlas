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

import logging
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

from platform_atlas.core.exceptions import (
    CredentialError,
    InsecureBackendError,
    TierViolationError,
)

__all__ = [
    "CredentialBackend",
    "CredentialBackendType",
    "CredentialKey",
    "CredentialStore",
    "KeyringBackend",
    "VaultAuthMethod",
    "VaultBackend",
    "VaultConfig",
    "credential_store",
    "migrate_legacy_credentials",
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
    GATEWAY4_PASSWORD = "gateway4_password"      # nosec B105

    @property
    def display_name(self) -> str:
        """Human-readable name for CLI output."""
        names = {
            "platform_client_secret": "Platform Client Secret",   # nosec B105
            "mongo_uri":              "MongoDB URI",
            "redis_uri":              "Redis URI",
            "ssh_key_passphrase":     "SSH Key Passphrase",       # nosec B105
            "gateway4_password":      "Gateway4 API Password",    # nosec B105
        }
        return names.get(self.value, self.value)

    @property
    def required(self) -> bool:
        """True if this credential is always required regardless of topology."""
        return self in _ALWAYS_REQUIRED

    @property
    def collector_module(self) -> str | None:
        """The collector module that needs this credential, or None if always required."""
        return _KEY_MODULE_MAP.get(self)

# Credentials that must always be present
_ALWAYS_REQUIRED: frozenset[CredentialKey] = frozenset({
    CredentialKey.PLATFORM_SECRET,
})

# Maps optional credentials to the collector module that needs them
_KEY_MODULE_MAP: dict[CredentialKey, str] = {
    CredentialKey.MONGO_URI:         "mongo",
    CredentialKey.REDIS_URI:         "redis",
    CredentialKey.GATEWAY4_PASSWORD: "gateway4",
}

# Credentials that require Extended Mode. The tier-aware credential store
# refuses to write these and silently returns None on read while tier=standard,
# so there is no leakage path between tier switches. Defense 3 of the hard
# mode boundary (alongside registry pruning and require_extended() guards).
#
# GATEWAY4_PASSWORD is intentionally NOT in this set — Gateway4 API auth
# works in Standard via ipsdk over HTTPS.
EXTENDED_ONLY_KEYS: frozenset[CredentialKey] = frozenset({
    CredentialKey.MONGO_URI,
    CredentialKey.REDIS_URI,
    CredentialKey.SSH_PASSPHRASE,
})


def _is_standard_tier() -> bool:
    """
    True if the active tier is Standard. Safe to call before config is
    loaded — returns False if the tier cannot be determined yet, which
    keeps init/setup paths unblocked.
    """
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if not is_config_loaded():
            return False
        return get_config().tier == "standard"
    except Exception:
        return False


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
    """OS keyring backend (macOS Keychain, Windows Credential Locker, etc.)."""

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
        try:
            return keyring.get_password(self._service, key)
        except keyring.errors.KeyringError as e:
            # ChainerBackend can fail at runtime even after a successful startup
            # probe (e.g. SecretService becomes unavailable mid-session). Switch
            # to a working alt backend and retry once before giving up.
            if type(keyring.get_keyring()).__name__ == "ChainerBackend":
                logger.debug("ChainerBackend failed get(%s), switching backend", key)
                _switch_to_alt_keyring()
                try:
                    return keyring.get_password(self._service, key)
                except Exception:
                    pass
            logger.warning("Keyring read failed for %s: %s", key, e)
            return None
        except ValueError as e:
            logger.warning("Keyring read failed for %s: %s", key, e)
            return None

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(self._service, key, value)
        except keyring.errors.KeyringError as e:
            # Same ChainerBackend runtime-failure guard as get().
            if type(keyring.get_keyring()).__name__ == "ChainerBackend":
                logger.debug("ChainerBackend failed set(%s), switching backend", key)
                _switch_to_alt_keyring()
                try:
                    keyring.set_password(self._service, key, value)
                    return
                except keyring.errors.KeyringError as retry_e:
                    e = retry_e
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

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(self._service, key)
        except keyring.errors.PasswordDeleteError:
            pass  # Already gone
        except keyring.errors.KeyringError as e:
            logger.warning("Keyring delete failed for %s: %s", key, e)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def __repr__(self) -> str:
        return f"KeyringBackend(service={self._service!r})"


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
    ) -> None:
        """Persist Vault connection settings in the OS keyring.

        Args:
            config: The Vault connection configuration to save.
            service: The keyring service name. Pass a scoped name
                     (``scoped_service_name(env_name)``) to isolate
                     Vault settings per environment.
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
        _MAX_KEYRING_ATTEMPTS = 3
        for attempt in range(_MAX_KEYRING_ATTEMPTS):
            try:
                for k, v in mapping.items():
                    if v:
                        keyring.set_password(service, k, v)
                    else:
                        # Clean up empty values so _load doesn't pick up stale data
                        try:
                            keyring.delete_password(service, k)
                        except keyring.errors.PasswordDeleteError:
                            pass
                return  # all writes succeeded
            except ValueError:
                remaining = _MAX_KEYRING_ATTEMPTS - attempt - 1
                if remaining > 0:
                    print(
                        f"\nIncorrect keyring password — {remaining} attempt(s) remaining.",
                        file=sys.stderr,
                    )
                    # keyrings.alt calls _lock() on failure, so the next set_password
                    # call will re-prompt for the password automatically.
                    continue
                raise CredentialError(
                    "Incorrect keyring password after 3 attempts. "
                    "Re-run and enter the correct password, or delete the keyring file to start fresh.",
                    details={"service": service},
                ) from None

    @classmethod
    def _load_config_from_keyring(
        cls,
        service: str = SERVICE_NAME,
    ) -> VaultConfig:
        """Reconstruct VaultConfig from OS keyring entries.

        Args:
            service: The keyring service name to read from.
        """
        def _get(key: str) -> str | None:
            try:
                return keyring.get_password(service, key)
            except (keyring.errors.KeyringError, ValueError):
                return None

        url = _get("vault_url")
        if not url:
            raise CredentialError(
                "Vault URL not found in OS keyring",
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
            val = keyring.get_password(service, "vault_url")
            return val is not None
        except keyring.errors.KeyringError:
            return False

    @classmethod
    def clear_config_from_keyring(cls, service: str = SERVICE_NAME) -> None:
        """Remove all Vault connection settings from the OS keyring."""
        for k in cls._VAULT_KEYS:
            try:
                keyring.delete_password(service, k)
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.KeyringError as e:
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
            # loaded from the environment's keyring namespace
            return VaultBackend(service=self._service)
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
        _, _, name = verify_keyring_backend()
        env_suffix = f" [{self._env_name}]" if self._env_name else ""
        return f"OS Keyring ({name}){env_suffix}"

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

        In Standard Mode, Extended-only keys silently return None — Standard
        code paths should never ask for these in the first place, but if
        something does, it gets a clean miss rather than leaking a stale
        Extended credential.
        """
        if key in EXTENDED_ONLY_KEYS and _is_standard_tier():
            return None
        return self._backend.get(key.value)

    def set(self, key: CredentialKey, value: str) -> None:
        """
        Store a credential in the active backend.

        Raises CredentialError if the backend is read-only (Vault).
        Raises TierViolationError if writing an Extended-only key under tier=standard.
        """
        if not value:
            logger.debug("Skipping empty value for %s", key.value)
            return
        if key in EXTENDED_ONLY_KEYS and _is_standard_tier():
            raise TierViolationError(
                f"credential_store.set({key.value})",
                hint=f"{key.display_name} is not used in Standard Mode.",
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

        Mirrors get(): in Standard Mode, Extended-only keys appear absent
        so the public surface is consistent.
        """
        if key in EXTENDED_ONLY_KEYS and _is_standard_tier():
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


def _persist_keyring_backend(module_path: str, class_name: str) -> None:
    """Write the chosen backend to keyringrc.cfg so future processes skip ChainerBackend.

    The keyring library reads this file at startup — persisting here means the
    next invocation goes directly to the working backend without probing.
    """
    try:
        import configparser
        import sys as _sys
        from pathlib import Path as _Path
        if _sys.platform == "win32":
            # keyring on Windows reads from %APPDATA%\Python\keyringrc.cfg
            _appdata = os.environ.get("APPDATA") or str(_Path.home())
            config_dir = _Path(_appdata) / "Python"
        else:
            # XDG base dir on Linux/macOS
            config_dir = _Path.home() / ".local" / "share" / "python_keyring"
        config_dir.mkdir(parents=True, exist_ok=True)
        cfg = configparser.ConfigParser()
        cfg_path = config_dir / "keyringrc.cfg"
        if cfg_path.exists():
            cfg.read(cfg_path)
        if "backend" not in cfg:
            cfg["backend"] = {}
        cfg["backend"]["default-keyring"] = f"{module_path}.{class_name}"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            cfg.write(fh)
        logger.debug("Persisted keyring backend %s.%s to %s", module_path, class_name, cfg_path)
    except Exception as exc:
        logger.debug("Could not write keyringrc.cfg: %s", exc)


def _switch_to_alt_keyring() -> None:
    """Switch away from ChainerBackend to a deterministic, working alt backend.

    Probes each candidate with a real write/read before committing. Persists
    the winner to keyringrc.cfg so future process invocations go directly to
    the working backend and never touch ChainerBackend again.

    Candidate order:
      1. CryptFileKeyring — AES-encrypted file (requires pycryptodome, bundled).
         Prompts for a master password on first use.
      2. PlaintextKeyring — unencrypted file, always works with no prompts.
         Falls back to this in headless environments where CryptFileKeyring
         cannot prompt interactively.
    """
    import importlib
    for module_path, class_name in [
        ("keyrings.alt.Crypter", "CryptFileKeyring"),
        ("keyrings.alt.file",    "PlaintextKeyring"),
    ]:
        try:
            mod = importlib.import_module(module_path)
            backend_cls = getattr(mod, class_name)
            keyring.set_keyring(backend_cls())
            if _probe_keyring():
                _persist_keyring_backend(module_path, class_name)
                logger.debug("Switched keyring to %s (probe passed, choice persisted)", class_name)
                return
        except Exception:
            continue
    logger.warning(
        "Could not switch to any alt keyring backend — credential operations may fail. "
        "Consider configuring HashiCorp Vault as the credential backend."
    )


def verify_keyring_backend() -> tuple[bool, bool, str]:
    """Check the active keyring backend.

    Returns:
        (is_secure, is_functional, name) where is_secure means an encrypted
        OS keyring is active, is_functional means credentials can be stored at
        all (even if unencrypted), and name is the backend class name.

    ChainerBackend is probed with a real write/read because it can appear
    functional while silently failing (e.g. SecretService requires a GUI
    unlock that isn't available). If the probe fails, we switch to
    CryptFileKeyring so the rest of the session uses a backend that works.

    If we end up on ChainerBackend AFTER probing+switching, the alt fallback
    failed too — the backend is genuinely broken. Report is_functional=False
    so callers can refuse to proceed instead of silently losing credentials.
    """
    backend = keyring.get_keyring()
    name = type(backend).__name__

    if name == "ChainerBackend" and not _probe_keyring():
        _switch_to_alt_keyring()
        backend = keyring.get_keyring()
        name = type(backend).__name__
        # If we still have ChainerBackend after the switch attempt, every alt
        # backend (CryptFileKeyring, PlaintextKeyring) failed to initialise.
        # Probe one more time before accepting the result so we don't report
        # functional=True for a backend that can't store data.
        if name == "ChainerBackend" and not _probe_keyring():
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

    def _get(service: str, key: str) -> str | None:
        try:
            return keyring.get_password(service, key)
        except keyring.errors.KeyringError:
            return None

    def _set(service: str, key: str, value: str) -> bool:
        try:
            keyring.set_password(service, key, value)
            return True
        except keyring.errors.KeyringError as e:
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
        # Probe ChainerBackend and switch to a working alt backend if needed.
        # keyring.set_keyring() is process-local, so this must run once per
        # invocation — not just during setup flows.
        verify_keyring_backend()

        # Determine backend and active environment from config (if loaded)
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
    Reset the singleton so it will be re-created on next access.

    Call this after changing ``credential_backend`` in config so the
    store picks up the new backend type.
    """
    global _store
    _store = None
