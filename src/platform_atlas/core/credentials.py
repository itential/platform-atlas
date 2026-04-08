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
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

from platform_atlas.core.exceptions import (
    CredentialError,
    InsecureBackendError,
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
    TOKEN   = "token"
    APPROLE = "approle"


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
            logger.warning("Keyring read failed for %s: %s", key, e)
            return None

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(self._service, key, value)
        except keyring.errors.KeyringError as e:
            raise CredentialError(
                f"Failed to store '{key}' in OS keyring",
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
    role_id: str | None = None                      # For AppRole auth
    secret_id: str | None = None                    # For AppRole auth
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
        self._client = self._connect(vault_config)

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
            "vault_url":          config.url,
            "vault_auth_method":  config.auth_method.value,
            "vault_token":        config.token or "",
            "vault_role_id":      config.role_id or "",
            "vault_secret_id":    config.secret_id or "",
            "vault_mount_point":  config.mount_point,
            "vault_secret_path":  config.secret_path,
            "vault_verify_ssl":   str(config.verify_ssl),
            "vault_namespace":    config.namespace or "",
        }
        for k, v in mapping.items():
            if v:
                keyring.set_password(service, k, v)
            else:
                # Clean up empty values so _load doesn't pick up stale data
                try:
                    keyring.delete_password(service, k)
                except keyring.errors.PasswordDeleteError:
                    pass

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
            except keyring.errors.KeyringError:
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
            raise CredentialError(
                "Vault authentication failed — client is not authenticated",
                details={"url": config.url, "method": config.auth_method.value},
            )

        logger.info("Connected to Vault at %s (auth=%s)", config.url, config.auth_method.value)
        return client

    # --- CredentialBackend interface (read-only) ---

    def _read_all(self) -> dict[str, str]:
        """Read the full secret dict from Vault KV v2."""
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*raise_on_deleted_version.*", category=DeprecationWarning)
                resp = self._client.secrets.kv.v2.read_secret_version(
                    path=self._config.secret_path,
                    mount_point=self._config.mount_point,
                )
            return resp.get("data", {}).get("data", {})
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
            logger.debug("Vault read failed at %s: %s", self._config.full_path, e)
            return {}

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
        _, name = verify_keyring_backend()
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
        """Retrieve a credential. Returns None if not found."""
        return self._backend.get(key.value)

    def set(self, key: CredentialKey, value: str) -> None:
        """
        Store a credential in the active backend.

        Raises CredentialError if the backend is read-only (Vault).
        """
        if not value:
            logger.debug("Skipping empty value for %s", key.value)
            return
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
        """Check if a credential is stored."""
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

# Backends that store in plaintext or do nothing
_INSECURE_BACKENDS = frozenset({
    "PlaintextKeyring",
    "NullKeyring",
    "ChainerBackend",
    "FailKeyring",
})


def verify_keyring_backend() -> tuple[bool, str]:
    """Check that the OS has a real (encrypted) keyring backend."""
    backend = keyring.get_keyring()
    name = type(backend).__name__

    if name in _INSECURE_BACKENDS:
        return False, name

    return True, name


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
        # Determine backend and active environment from config (if loaded)
        backend_type = CredentialBackendType.KEYRING
        env_name: str | None = None
        try:
            from platform_atlas.core.config import get_config
            cfg = get_config()
            backend_type = CredentialBackendType(cfg.credential_backend)
            env_name = cfg.active_environment
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
