"""
ATLAS // Config File Loader

Loads the global config from ~/.atlas/config.json and optionally
merges in the active environment's connection/deployment fields.

When an environment is active, environment-specific fields (platform_uri,
platform_client_id, credential_backend, deployment, legacy_profile) are
overlaid on top of the global config. When no environment is active,
config.json is used as-is for full backward compatibility.
"""
from __future__ import annotations

import os
import stat
import json
import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass, fields

from platform_atlas.core.topology import DeploymentTopology, DeploymentMode, CaptureScope
from platform_atlas.core.exceptions import SecurityError, ConfigError
from platform_atlas.core.paths import ATLAS_CONFIG_FILE

__all__ = ["Config", "load_config", "load_config_safe", "get_config", "is_config_loaded"]

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Config:
    """Frozen configuration loaded from ~/.atlas/config.json (+ active environment)"""
    organization_name: str
    platform_uri: str
    platform_client_id: str
    verify_ssl: bool = True
    dark_mode: bool = True
    theme: str = "horizon-prism"
    debug: bool = False
    legacy_profile: str | None = ""
    extended_validation_checks: bool = True
    multi_tenant_mode: bool = False
    deployment: dict | None = None
    skip_rules: list[str] | None = None
    credential_backend: str = "keyring"
    active_environment: str | None = None
    gateway4_uri: str = ""
    gateway4_username: str = ""
    # Kubernetes-specific
    values_yaml_path: str = ""
    iag5_values_yaml_path: str = ""
    kubectl_context: str = ""
    kubectl_namespace: str = ""
    use_kubectl: bool = False
    # Collector UX — "html" (default) opens the browser form; "cli" uses terminal prompts
    manual_input_mode: str = "html"

    @property
    def platform_client_secret(self) -> str:
        from platform_atlas.core.credentials import credential_store, CredentialKey
        return credential_store().get_required(CredentialKey.PLATFORM_SECRET)

    @property
    def mongo_uri(self) -> str | None:
        from platform_atlas.core.credentials import credential_store, CredentialKey
        return credential_store().get(CredentialKey.MONGO_URI)

    @property
    def redis_uri(self) -> str | None:
        from platform_atlas.core.credentials import credential_store, CredentialKey
        return credential_store().get(CredentialKey.REDIS_URI)

    @property
    def gateway4_password(self) -> str | None:
        from platform_atlas.core.credentials import credential_store, CredentialKey
        return credential_store().get(CredentialKey.GATEWAY4_PASSWORD)

    @property
    def topology(self) -> DeploymentTopology:
        """Parsed deployment topology with validation"""
        if self.deployment:
            return DeploymentTopology.from_dict(self.deployment)
        # No deployment block = no topology defined yet
        raise ConfigError(
            "No 'deployment' section in config. "
            "Run 'platform-atlas config init' to configure your target environment.",
        )

    @property
    def capture_scope(self) -> str:
        """The active capture scope string from config."""
        return (self.deployment or {}).get("capture_scope", "primary_only")

    @property
    def targets(self) -> tuple[dict, ...]:
        """Target list for the capture engine, filtered by capture scope."""
        topo = self.topology
        scope_str = (self.deployment or {}).get("capture_scope", "primary_only")

        try:
            scope = CaptureScope(scope_str)
        except ValueError:
            logger.warning(
                "Unknown capture_scope '%s', defaulting to primary_only",
                scope_str,
            )
            scope = CaptureScope.PRIMARY_ONLY

        return tuple(topo.capture_targets(scope))

    @property
    def all_targets(self) -> tuple[dict, ...]:
        """Full target list ignoring scope — used by preflight to check all nodes."""
        return tuple(self.topology.capture_targets(CaptureScope.ALL_NODES))

    @property
    def has_environment(self) -> bool:
        """True if the config was loaded with an active environment overlay."""
        return self.active_environment is not None

    @property
    def is_kubernetes(self) -> bool:
        """True if the deployment mode is Kubernetes."""
        try:
            return self.topology.mode == DeploymentMode.KUBERNETES
        except ConfigError:
            return False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            logger.warning("Ignoring unknown config fields: %s", unknown)
        return cls(**{k: v for k, v in data.items() if k in known})

# Module-level instance
_config: Config | None = None

def is_config_loaded() -> bool:
    """True if load_config() has successfully run and stored a Config"""
    return _config is not None

def load_config_safe(path: str | Path = ATLAS_CONFIG_FILE) -> tuple[bool, str | None]:
    """Attempt to load the config without throwing a traceback error"""
    try:
        load_config(path)
        return True, None
    except FileNotFoundError as e:
        missing = e.filename or str(path)
        return False, f"Config file not found: {missing}"
    except PermissionError as e:
        target = e.filename or str(path)
        return False, f"Config permission denied: {target}"
    except json.JSONDecodeError as e:
        return False, f"Config JSON invalid: {e.msg} (line {e.lineno}, col {e.colno})"
    except TypeError as e:
        return False, f"Config fields mismatch: {e}"
    except Exception as e:
        return False, f"Config load failed: {type(e).__name__}: {e}"

def load_config(
    path: str | Path = ATLAS_CONFIG_FILE,
    env_override: str | None = None,
) -> Config:
    """
    Load configuration into Atlas.

    Reads the global config.json, then overlays the active environment's
    fields if one is set. Resolution order for the active environment:

        1. ``env_override`` argument (from --env CLI flag)
        2. ``ATLAS_ENV`` environment variable
        3. ``active_environment`` field in config.json
        4. No overlay (backward-compat: config.json used as-is)
    """
    global _config
    path = Path(path)
    logger.debug("Loading config from %s", path)

    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}",
            details={"suggestion": "Run 'platform-atlas config init' to create one"}
        )

    # Permissions check (chmod 600)
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecurityError(
                f"Config file {str(path)} has insecure permissions ({oct(mode)}). "
                f"Run chmod 600 {str(path)}",
                details={"path": str(path), "mode": oct(mode)}
            )

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Remove any legacy credential fields that may still be present
    for legacy_key in ("platform_client_secret", "mongo_uri", "redis_uri"):
        data.pop(legacy_key, None)

    # ── Resolve and apply active environment ──────────────────────
    env_name = _resolve_env_name(env_override, data)

    if env_name:
        try:
            from platform_atlas.core.environment import get_environment_manager
            mgr = get_environment_manager()
            if mgr.exists(env_name):
                env = mgr.load(env_name)
                overlay = env.as_config_overlay()
                data.update(overlay)
                data["active_environment"] = env_name
                logger.debug("Applied environment overlay: %s (%d fields)", env_name, len(overlay))
            else:
                logger.warning("Active environment '%s' not found — using config.json as-is", env_name)
                data["active_environment"] = None
        except Exception as e:
            logger.warning("Failed to load environment '%s': %s — using config.json as-is", env_name, e)
            data["active_environment"] = None
    else:
        data["active_environment"] = None

    _config = Config.from_dict(data)
    logger.debug("Config loaded: theme=%s, debug=%s, env=%s",
                 _config.theme, _config.debug, _config.active_environment)
    return _config


def _resolve_env_name(
    cli_override: str | None,
    config_data: dict[str, Any],
) -> str | None:
    """
    Determine which environment to activate.
    Returns None if no environment should be applied.
    """
    # 1. Explicit CLI override
    if cli_override:
        return cli_override

    # 2. Environment variable
    env_var = os.environ.get("ATLAS_ENV")
    if env_var:
        return env_var

    # 3. Persisted in config.json
    return config_data.get("active_environment")


def get_config() -> Config:
    """Get the loaded config from anywhere in Platform Atlas"""
    # Delegate to context if available, fall back to module-level
    if _config is not None:
        return _config
    try:
        from platform_atlas.core.context import ctx
        return ctx().config
    except Exception:
        raise RuntimeError("Config not loaded! Call load_config() first in main()")
