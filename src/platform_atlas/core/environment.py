"""
ATLAS // Environment Manager

Manages named deployment environments (dev, staging, production, etc.).
Each environment is a JSON file under ~/.atlas/environments/ containing
all connection and deployment details for one target IAP deployment.

The active environment name is stored in the global config.json as
``active_environment``. Resolution order for the active environment:

    1. ``--env`` CLI flag
    2. ``ATLAS_ENV`` environment variable
    3. ``active_environment`` in config.json
    4. Prompt the user (interactive only)
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import logging
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR, ATLAS_CONFIG_FILE
from platform_atlas.core.exceptions import ConfigError
from platform_atlas.core.utils import atomic_write_json

__all__ = [
    "Environment",
    "EnvironmentManager",
    "get_environment_manager",
    "normalize_env_name",
    "propagate_ssh_key",
    "resolve_active_environment",
    "ensure_valid_environment",
]

logger = logging.getLogger(__name__)

# Valid environment name: lowercase alphanumeric start, then lowercase
# alphanumeric, hyphens, underscores, or dots. No uppercase, no spaces.
# Names appear in filesystem paths, keyring service names, and URL params
# where mixed case and spaces cause confusion or silent breakage.
_ENV_NAME_FORBIDDEN = ("/", "\\", "\x00", "..")


@dataclass
class Environment:
    """
    One named deployment target (e.g., 'production', 'staging', 'dev').

    Stored as ~/.atlas/environments/<n>.json.
    Contains all the connection and topology details that were previously
    embedded directly in config.json.

    Standard-tier environments only require ``platform_uri`` and
    ``platform_client_id`` (and optionally ``gateway4_uri`` /
    ``gateway4_username``). All Extended-only fields (deployment topology,
    Kubernetes paths, etc.) are left at their defaults and ignored by
    Standard-tier capture flows.
    """
    name: str
    description: str = ""
    organization_name: str = ""
    platform_uri: str = ""
    platform_client_id: str = ""
    credential_backend: str = "keyring"
    # Vault backend only: where Vault's own connection settings live locally —
    # "keyring" or "file" (None = keyring, the pre-2.0 default). Overlays config.
    vault_secret_store: str | None = None
    deployment: dict | None = None
    legacy_profile: str | None = ""
    gateway4_uri: str = ""
    gateway4_username: str = ""
    # Tier — overlays the global config.tier when this env is active.
    # Stored as None when not explicitly set so it stays out of the overlay
    # and the global default is preserved.
    tier: str | None = None
    # SaaS tier only: which gateway(s) this environment audits — "gateway4",
    # "gateway5", or "gw4-gw5" (both). Fixed at create time.
    saas_gateway_kind: str | None = None
    # Standard/Extended: the gateway(s) present in this environment — "gateway4",
    # "gateway5", "gw4-gw5", or "no-gateway". None = unset (backward compat).
    # Drives profile-discovery filtering so users only see relevant profiles.
    gateway_kind: str | None = None
    # SSH key path — applies to all SSH-connected nodes in this environment.
    # Stored here as a top-level convenience field; propagated into
    # deployment.ssh_defaults.key_path and each node's ssh_key when saved
    # so the transport layer can always read it directly from the node dict.
    ssh_key: str = ""
    # Override for the Platform log directory when the deployment uses a
    # non-default location (i.e. log4js-rotated to /opt/itential/log,
    # custom syslog targets, etc.). Falls back to PLATFORM6_LOG_PATH_ROOT
    # when empty. Set interactively via the post-capture log_path prompt
    # or by editing the env file directly.
    log_path_override: str = ""
    # Override for the Platform webserver log file (single file path, not
    # directory). Falls back to PLATFORM6_WEBSERVER_LOG_PATH when empty.
    # Set interactively via the post-capture retry prompt.
    webserver_log_path_override: str = ""
    # Override for the MongoDB log file (single file path, not directory).
    # Falls back to MONGO_LOG_PATH when empty. Set interactively via the
    # post-capture retry prompt.
    mongo_log_path_override: str = ""
    # Debug: when True, capture also writes 01_raw_capture.json — the full
    # reshaped capture before ruleset-based filtering. Used to trace
    # dot-notation paths when authoring new rules.
    debug_export_raw_capture: bool = False
    # Kubernetes-specific fields
    values_yaml_path: str = ""
    iag5_values_yaml_path: str = ""
    kubectl_context: str = ""
    kubectl_namespace: str = ""
    use_kubectl: bool = False
    kubectl_binary_path: str = ""
    env_tint: str | None = None
    # Per-environment rule suppression list. Rules in this list are skipped
    # during validation and appear in reports as "Suppressed by user".
    # Each entry is {"rule_number": str, "reason": str, "suppressed_at": str}.
    # Merges additively with the global config.skip_rules at load time.
    skip_rules: list[dict] | None = None
    # Set to True while an env create wizard is in progress. Allows
    # interrupted setups to be resumed via `env edit` rather than forcing
    # the user to re-enter everything from scratch.
    partial: bool = False

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON storage."""
        d = {k: v for k, v in asdict(self).items() if v is not None}
        # Only persist `partial` when True — keeps env files clean for fully
        # configured environments and avoids surprising downstream readers.
        if not d.get("partial"):
            d.pop("partial", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Environment:
        """Create an Environment from a dict, ignoring unknown fields."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def file_path(self) -> Path:
        """The path where this environment is (or would be) stored."""
        return ATLAS_ENVIRONMENTS_DIR / f"{self.name}.json"

    # ── Config overlay ────────────────────────────────────────────

    def as_config_overlay(self) -> dict[str, Any]:
        """
        Return the fields that should be merged into Config when this
        environment is active. These override the corresponding fields
        in the global config.json.
        """
        overlay: dict[str, Any] = {}
        if self.organization_name:
            overlay["organization_name"] = self.organization_name
        if self.platform_uri:
            overlay["platform_uri"] = self.platform_uri
        if self.platform_client_id:
            overlay["platform_client_id"] = self.platform_client_id
        if self.credential_backend:
            overlay["credential_backend"] = self.credential_backend
        if self.vault_secret_store:
            overlay["vault_secret_store"] = self.vault_secret_store
        if self.deployment is not None:
            overlay["deployment"] = self.deployment
        if self.legacy_profile is not None:
            overlay["legacy_profile"] = self.legacy_profile
        if self.gateway4_uri:
            overlay["gateway4_uri"] = self.gateway4_uri
        if self.gateway4_username:
            overlay["gateway4_username"] = self.gateway4_username
        # Tier — only overlay when explicitly set on the env file.
        if self.tier:
            overlay["tier"] = self.tier
        if self.saas_gateway_kind:
            overlay["saas_gateway_kind"] = self.saas_gateway_kind
        if self.gateway_kind:
            overlay["gateway_kind"] = self.gateway_kind
        if self.log_path_override:
            overlay["log_path_override"] = self.log_path_override
        if self.webserver_log_path_override:
            overlay["webserver_log_path_override"] = self.webserver_log_path_override
        if self.mongo_log_path_override:
            overlay["mongo_log_path_override"] = self.mongo_log_path_override
        if self.debug_export_raw_capture:
            overlay["debug_export_raw_capture"] = self.debug_export_raw_capture
        # Kubernetes-specific fields
        if self.values_yaml_path:
            overlay["values_yaml_path"] = self.values_yaml_path
        if self.iag5_values_yaml_path:
            overlay["iag5_values_yaml_path"] = self.iag5_values_yaml_path
        if self.kubectl_context:
            overlay["kubectl_context"] = self.kubectl_context
        if self.kubectl_namespace:
            overlay["kubectl_namespace"] = self.kubectl_namespace
        if self.use_kubectl:
            overlay["use_kubectl"] = self.use_kubectl
        if self.kubectl_binary_path:
            overlay["kubectl_binary_path"] = self.kubectl_binary_path
        # skip_rules is passed through as-is; load_config handles the additive
        # merge with the global list so both sources are respected.
        if self.skip_rules:
            overlay["env_skip_rules"] = list(self.skip_rules)
        return overlay

    def __repr__(self) -> str:
        return f"Environment(name={self.name!r}, platform_uri={self.platform_uri!r})"


def propagate_ssh_key(deployment: dict, ssh_key: str) -> dict:
    """Sync ssh_key into a deployment dict so the transport layer can read it.

    Updates two places:
      • ``deployment["ssh_defaults"]["key_path"]`` — fallback for any node
        that doesn't carry an explicit key (e.g. nodes added after initial setup)
      • ``deployment["nodes"][*]["ssh_key"]`` — per-node value for all
        SSH-transport nodes (transport != "kubernetes")

    Passing an empty string clears both, letting SSH fall back to the agent.
    """
    if not isinstance(deployment, dict):
        return deployment

    # -- ssh_defaults fallback ------------------------------------------------
    defaults = deployment.setdefault("ssh_defaults", {})
    if ssh_key:
        defaults["key_path"] = ssh_key
    else:
        defaults.pop("key_path", None)
    if not defaults:
        deployment.pop("ssh_defaults", None)

    # -- per-node values -------------------------------------------------------
    for node in deployment.get("nodes", []):
        if node.get("transport") in ("kubernetes", "local", "control_master", "gateway5_file"):
            continue  # K8s, local, ControlMaster, and file-source nodes don't use a SSH key path
        if ssh_key:
            node["ssh_key"] = ssh_key
        else:
            node.pop("ssh_key", None)

    return deployment


_ENV_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_.\-]*$")


def validate_env_name(name: str) -> bool:
    """Check if an environment name is valid.

    Rules: lowercase alphanumeric start, then lowercase alphanumeric,
    hyphens, underscores, or dots. No uppercase, no spaces. Max 128 chars.

    Names appear in filesystem paths, keyring service names, and URL params
    where mixed case and spaces cause silent breakage or escaping problems.
    """
    s = (name or "").strip()
    if not s or len(s) > 128:
        return False
    if any(token in s for token in _ENV_NAME_FORBIDDEN):
        return False
    return bool(_ENV_NAME_RE.match(s))


def normalize_env_name(name: str) -> str:
    """Return a best-effort valid name derived from *name*.

    Lowercases, replaces spaces with hyphens, and strips characters that
    ``validate_env_name`` would reject. The result is not guaranteed to be
    valid if the input is too short or starts with a non-alnum character.
    """
    import re as _re
    s = (name or "").strip().lower()
    s = s.replace(" ", "-")
    s = _re.sub(r"[^a-z0-9_.\-]", "", s)
    s = s[:128]
    return s


class EnvironmentManager:
    """
    Handles CRUD operations for environment files and tracks which
    environment is currently active.
    """

    def __init__(self, env_dir: Path = ATLAS_ENVIRONMENTS_DIR) -> None:
        self._dir = env_dir

    def ensure_dir(self) -> None:
        """Create the environments directory if it doesn't exist."""
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # ── Listing ───────────────────────────────────────────────────

    def list_names(self) -> list[str]:
        """Return sorted list of all environment names."""
        if not self._dir.is_dir():
            return []
        return sorted(
            p.stem for p in self._dir.glob("*.json")
            if p.is_file()
        )

    def list_all(self) -> list[Environment]:
        """Load and return all environments."""
        return [self.load(name) for name in self.list_names()]

    def has_any(self) -> bool:
        """True if at least one environment exists."""
        return bool(self.list_names())

    def exists(self, name: str) -> bool:
        """Check whether an environment file exists."""
        return (self._dir / f"{name}.json").is_file()

    # ── Load / Save / Delete ──────────────────────────────────────

    def load(self, name: str) -> Environment:
        """Load a single environment by name."""
        path = self._dir / f"{name}.json"
        if not path.is_file():
            raise ConfigError(
                f"Environment not found: {name}",
                details={
                    "path": str(path),
                    "suggestion": "Run 'platform-atlas env list' to see available environments",
                },
            )

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Ensure the name field matches the filename
        data["name"] = name
        return Environment.from_dict(data)

    def save(self, env: Environment) -> Path:
        """Write an environment to disk. Creates the directory if needed."""
        self.ensure_dir()
        path = self._dir / f"{env.name}.json"
        atomic_write_json(path, env.to_dict())
        logger.info("Saved environment: %s → %s", env.name, path)
        return path

    def remove(self, name: str) -> None:
        """Delete an environment file."""
        path = self._dir / f"{name}.json"
        if path.is_file():
            path.unlink()
            logger.info("Removed environment: %s", name)
        else:
            raise ConfigError(
                f"Environment not found: {name}",
                details={"path": str(path)},
            )

    def copy(self, source_name: str, dest_name: str) -> Environment:
        """Copy an existing environment to a new name."""
        source = self.load(source_name)
        new_env = Environment.from_dict(source.to_dict())
        new_env.name = dest_name
        self.save(new_env)
        return new_env

    # ── Active environment ────────────────────────────────────────

    def get_active_name(self) -> str | None:
        """
        Read the active environment name from config.json.
        Returns None if not set.
        """
        if not ATLAS_CONFIG_FILE.is_file():
            return None
        try:
            with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("active_environment")
        except (json.JSONDecodeError, OSError):
            return None

    def set_active(self, name: str) -> None:
        """
        Persist the active environment name into config.json.
        Raises ConfigError if the environment doesn't exist.
        """
        if not self.exists(name):
            raise ConfigError(
                f"Cannot activate environment '{name}' — not found",
                details={"suggestion": "Run 'platform-atlas env list' to see available environments"},
            )

        if not ATLAS_CONFIG_FILE.is_file():
            raise ConfigError(
                "Global config not found — run 'platform-atlas config init' first",
            )

        with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["active_environment"] = name
        atomic_write_json(ATLAS_CONFIG_FILE, data)
        logger.info("Active environment set to: %s", name)

    def clear_active(self) -> None:
        """Remove the active_environment key from config.json."""
        if not ATLAS_CONFIG_FILE.is_file():
            return

        with open(ATLAS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.pop("active_environment", None)
        atomic_write_json(ATLAS_CONFIG_FILE, data)
        logger.info("Active environment cleared")

    def get_active(self) -> Environment | None:
        """Load the active environment, or None if not set."""
        name = self.get_active_name()
        if name is None:
            return None
        if not self.exists(name):
            logger.warning(
                "Active environment '%s' not found on disk — ignoring", name
            )
            return None
        return self.load(name)


# ── Module-level singleton ────────────────────────────────────────

_manager: EnvironmentManager | None = None


def get_environment_manager() -> EnvironmentManager:
    """Get or create the module-level EnvironmentManager singleton."""
    global _manager
    if _manager is None:
        _manager = EnvironmentManager()
    return _manager


def resolve_active_environment(
    cli_env: str | None = None,
) -> str | None:
    """
    Determine the active environment name using the resolution chain:

        1. Explicit ``--env`` CLI flag
        2. ``ATLAS_ENV`` environment variable
        3. ``active_environment`` in config.json
        4. None (no environment active — legacy/backward-compat mode)

    Does NOT prompt the user. Returns None if no environment is resolvable.
    """
    # 1. CLI flag
    if cli_env:
        return cli_env

    # 2. Environment variable
    env_var = os.environ.get("ATLAS_ENV")
    if env_var:
        return env_var

    # 3. Persisted in config.json
    mgr = get_environment_manager()
    return mgr.get_active_name()


def ensure_valid_environment(env_override: str | None = None) -> None:
    """
    Pre-flight check: verify the active environment still exists on disk.

    If the persisted ``active_environment`` in config.json references a
    file that has been deleted, this function recovers gracefully:

        - Interactive + other environments available → prompt the user
          to pick a replacement and persist the choice.
        - Non-interactive or no environments on disk → clear the stale
          reference so downstream code sees ``active_environment = None``.

    Call this **before** ``load_config()`` so the config loader never
    encounters a dangling environment reference.

    Args:
        env_override: The ``--env`` CLI flag value. If set, the user is
                      explicitly choosing an environment so no recovery
                      is needed.
    """
    # If the user passed --env or ATLAS_ENV, they're overriding — skip check
    if env_override or os.environ.get("ATLAS_ENV"):
        return

    mgr = get_environment_manager()
    active_name = mgr.get_active_name()

    # No active environment set — nothing to validate
    if not active_name:
        return

    # Active environment exists on disk — all good
    if mgr.exists(active_name):
        return

    # Active environment is stale (file was deleted)
    available = mgr.list_names()

    # Non-interactive — can't prompt, just clear the stale reference.
    #
    # We treat both "no TTY" AND "already inside an asyncio loop" as
    # non-interactive. The WebUI is started from a terminal (so stdin is
    # a TTY) but its startup hook runs inside uvicorn's loop; firing up
    # questionary there explodes with "asyncio.run() cannot be called
    # from a running event loop". When we hit that, we want to silently
    # heal the stale reference and let the WebUI start in an
    # uninitialised state, not crash.
    in_running_loop = False
    try:
        import asyncio as _asyncio
        _asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        pass

    if in_running_loop or not sys.stdin.isatty():
        logger.warning(
            "Active environment '%s' not found (non-interactive) — clearing stale reference",
            active_name,
        )
        mgr.clear_active()
        return

    # Interactive recovery
    from rich.console import Console
    from platform_atlas.core import ui

    console = Console()
    theme = ui.theme

    console.print(
        f"\n  [{theme.warning}]⚠[/{theme.warning}]  Environment "
        f"[bold]{active_name}[/bold] no longer exists."
    )

    if not available:
        console.print(
            f"  [{theme.text_muted}]No other environments found — "
            f"run 'platform-atlas env create' to set one up.[/{theme.text_muted}]\n"
        )
        mgr.clear_active()
        return

    import questionary

    choice = questionary.select(
        "Select an environment to switch to:",
        choices=available,
    ).ask()

    if choice is None:
        raise KeyboardInterrupt

    mgr.set_active(choice)
    console.print(
        f"  [{theme.success}]✓[/{theme.success}] Switched to [bold]{choice}[/bold]\n"
    )
    logger.info(
        "Recovered from stale environment '%s' — switched to '%s'",
        active_name, choice,
    )
