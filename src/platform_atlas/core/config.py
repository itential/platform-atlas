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
from typing import Any, Literal
from dataclasses import dataclass, fields

from platform_atlas.core.topology import (
    DeploymentTopology,
    DeploymentMode,
    CaptureScope,
    synthesize_standard_targets,
)
from platform_atlas.core.exceptions import SecurityError, ConfigError
from platform_atlas.core.paths import ATLAS_CONFIG_FILE

__all__ = [
    "Config",
    "load_config",
    "load_config_safe",
    "get_config",
    "is_config_loaded",
    "resolve_tier",
    "Tier",
]

Tier = Literal["standard", "extended"]
_VALID_TIERS: frozenset[str] = frozenset({"standard", "extended"})

_VALID_WEBUI_THEMES: frozenset[str] = frozenset({"light", "dark"})
_VALID_WEBUI_ACCENTS: frozenset[str] = frozenset({
    "cyan", "amber", "violet", "lime", "mono",
})

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Config:
    """Frozen configuration loaded from ~/.atlas/config.json (+ active environment).

    ``platform_uri`` and ``platform_client_id`` only carry real values when an
    environment overlay is active — they live in ``~/.atlas/environments/<n>.json``,
    not in the root config. They default to ``""`` so the WebUI can boot and
    render its environment-management pages even before any env exists. Capture
    flows that actually need a live target check for non-empty values upstream.
    """
    organization_name: str = ""
    platform_uri: str = ""
    platform_client_id: str = ""
    verify_ssl: bool = True
    dark_mode: bool = True
    theme: str = "horizon-prism"
    debug: bool = False
    legacy_profile: str | None = ""
    extended_validation_checks: bool = True
    # WebUI appearance — independent of `theme` above, which is the CLI's
    # Rich terminal theme (e.g. "horizon-dark"). Validated against
    # _VALID_WEBUI_THEMES / _VALID_WEBUI_ACCENTS in load_config().
    #
    # Other ``webui_*`` fields (mode, upgrade_panel_dismissed, future ones)
    # are managed entirely by the optional WebUI package via raw-json reads
    # in ``platform_atlas_webui.services.config`` — they intentionally do
    # NOT live on this dataclass. ``Config.from_dict`` silently passes them
    # through so a CLI-only user never sees "unknown config field" warnings
    # about WebUI state, and WebUI can grow new fields without ever touching
    # the CLI core.
    webui_theme: str = "dark"
    webui_accent: str = "cyan"
    deployment: dict | None = None
    skip_rules: list[dict] | None = None
    credential_backend: str = "keyring"
    active_environment: str | None = None
    gateway4_uri: str = ""
    gateway4_username: str = ""
    # Tier — distinct from `extended_validation_checks` (which is the deep-checks
    # engine that runs in BOTH tiers). The migration shim in load_config() sets
    # this to "extended" for existing 1.6.x users (their config.json has no
    # tier field) so behavior is preserved on upgrade. Fresh installs go
    # through init_setup.py which writes "standard" explicitly.
    tier: Tier = "standard"
    # Kubernetes-specific
    values_yaml_path: str = ""
    iag5_values_yaml_path: str = ""
    kubectl_context: str = ""
    kubectl_namespace: str = ""
    use_kubectl: bool = False
    kubectl_binary_path: str = ""
    # Override the default Platform log directory for log capture. Falls back
    # to PLATFORM6_LOG_PATH_ROOT when empty. Captured interactively after a
    # failed log collection or set explicitly via env edit.
    log_path_override: str = ""
    # Override the default Platform webserver log file. Falls back to
    # PLATFORM6_WEBSERVER_LOG_PATH when empty.
    webserver_log_path_override: str = ""
    # Override the default MongoDB log file. Falls back to MONGO_LOG_PATH
    # when empty.
    mongo_log_path_override: str = ""
    # Debug: when True, capture also writes 01_raw_capture.json — the full
    # reshaped capture before ruleset-based filtering. The CLI flag
    # --debug-raw-capture overrides this per-run.
    debug_export_raw_capture: bool = False
    # Collector UX — "html" (default) opens the browser form; "cli" uses terminal prompts
    manual_input_mode: str = "html"
    # Whether to keep 01_logs.json after all reports are generated (default: delete)
    keep_logs_file: bool = False
    # Plain / compatibility mode — strips all Rich formatting (colors, Unicode
    # box-drawing, ANSI codes) for terminals that don't support them.
    # Set once via `platform-atlas --plain` or toggled via `config plain`.
    compatibility_mode: bool = False

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
        """Target list for the capture engine, filtered by capture scope.

        In Standard mode the topology is not consulted — targets are
        synthesized from ``platform_uri`` (and optional ``gateway4_uri``).
        """
        if self.tier == "standard":
            return tuple(synthesize_standard_targets(self))

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
        if self.tier == "standard":
            return tuple(synthesize_standard_targets(self))
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
        # WebUI is an optional add-on that stores its own state in the same
        # config.json. Drop ``webui_*`` extras silently — CLI-only users
        # should never see warnings about WebUI fields, and WebUI can add
        # new fields without forcing changes to this dataclass.
        unknown = {k for k in unknown if not k.startswith("webui_")}
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
    tier_override: str | None = None,
) -> Config:
    """
    Load configuration into Atlas.

    Reads the global config.json, then overlays the active environment's
    fields if one is set. Resolution order for the active environment:

        1. ``env_override`` argument (from --env CLI flag)
        2. ``ATLAS_ENV`` environment variable
        3. ``active_environment`` field in config.json
        4. No overlay (backward-compat: config.json used as-is)

    Tier resolution order (applied after env overlay):

        1. ``tier_override`` argument (from --tier CLI flag)
        2. ``ATLAS_TIER`` environment variable
        3. ``tier`` field in active environment overlay
        4. ``tier`` field in config.json (or migration shim default)
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

    # ── Tier migration shim (1.6.x → 1.7.x) ───────────────────────
    # Existing users on upgrade have no `tier` field in config.json — preserve
    # their full-Atlas experience by defaulting them to Extended. New users
    # who go through init_setup.py get tier="standard" written explicitly.
    # This shim is idempotent: once tier is in the config dict (either from
    # disk or from a prior call), this is a no-op.
    if "tier" not in data:
        data["tier"] = "extended"
        logger.debug("Tier migration shim: no tier field in config — defaulting to extended")

    # ── Resolve and apply active environment ──────────────────────
    env_name = _resolve_env_name(env_override, data)

    if env_name:
        from platform_atlas.core.environment import get_environment_manager
        mgr = get_environment_manager()
        if mgr.exists(env_name):
            # A real load failure (corrupt JSON, permission denied, schema
            # mismatch) used to be silently demoted to active_environment=None,
            # which silently caused the next capture to run against the
            # WRONG (or no) target. Surface it instead so the user can fix
            # the env file or clear active_environment in config.json.
            try:
                env = mgr.load(env_name)
            except Exception as e:
                raise ConfigError(
                    f"Failed to load active environment '{env_name}': {e}",
                    details={
                        "environment": env_name,
                        "hint": (
                            "Fix the env file under ~/.atlas/environments/, or clear "
                            "active_environment in ~/.atlas/config.json to fall back "
                            "to the global defaults."
                        ),
                    },
                ) from e
            overlay = env.as_config_overlay()
            # Merge env skip_rules additively — don't replace global list.
            env_skip = overlay.pop("env_skip_rules", None) or []
            data.update(overlay)
            if env_skip:
                global_skip = list(data.get("skip_rules") or [])
                # Merge by rule_number; env entries take precedence over global
                merged_map = {r["rule_number"]: r for r in global_skip if isinstance(r, dict)}
                for r in env_skip:
                    if isinstance(r, dict):
                        merged_map[r["rule_number"]] = r
                data["skip_rules"] = list(merged_map.values())
            data["active_environment"] = env_name
            logger.debug("Applied environment overlay: %s (%d fields)", env_name, len(overlay))
        else:
            # "active env name points at a file that doesn't exist" is a
            # recoverable state — likely the user deleted the env file
            # manually. Warn loudly but don't fail the whole CLI.
            logger.warning("Active environment '%s' not found — using config.json as-is", env_name)
            data["active_environment"] = None
    else:
        data["active_environment"] = None

    # ── Apply tier overrides (CLI flag / env var) ─────────────────
    # Tier resolution order, highest precedence first (matches CLAUDE.md):
    #   1. ``--tier`` flag        (passed in here as ``tier_override``)
    #   2. ``ATLAS_TIER`` env var (read below if no flag)
    #   3. environment overlay    (applied above via ``env.as_config_overlay``)
    #   4. ``tier`` in config.json (already in ``data`` from json load)
    #   5. dataclass default       (``Config.tier`` — currently "standard")
    # Steps 3 and 4 land in ``data`` *before* this block, so 1 and 2 just
    # overwrite. The env-var branch is skipped entirely when --tier is given.
    if tier_override is not None:
        normalized = tier_override.strip().lower()
        if normalized not in _VALID_TIERS:
            raise ConfigError(
                f"Invalid --tier '{tier_override}'",
                details={"valid": sorted(_VALID_TIERS)},
            )
        data["tier"] = normalized
    else:
        env_tier = os.environ.get("ATLAS_TIER")
        if env_tier:
            normalized = env_tier.strip().lower()
            if normalized not in _VALID_TIERS:
                raise ConfigError(
                    f"Invalid ATLAS_TIER='{env_tier}'",
                    details={"valid": sorted(_VALID_TIERS)},
                )
            data["tier"] = normalized

    # ── Sanitize WebUI appearance prefs ──────────────────────────
    # Stored values may be stale or hand-edited. Snap to defaults
    # rather than failing the whole load.
    if data.get("webui_theme") not in _VALID_WEBUI_THEMES:
        data["webui_theme"] = "dark"
    if data.get("webui_accent") not in _VALID_WEBUI_ACCENTS:
        data["webui_accent"] = "cyan"

    _config = Config.from_dict(data)
    logger.debug(
        "Config loaded: tier=%s, theme=%s, debug=%s, env=%s",
        _config.tier, _config.theme, _config.debug, _config.active_environment,
    )
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


def resolve_tier(cli_flag: str | None = None) -> Tier:
    """
    Resolve the active tier with proper precedence:
        1. ``cli_flag`` argument (--tier)
        2. ``ATLAS_TIER`` environment variable
        3. Loaded ``Config.tier`` (which already includes env-overlay merge)
        4. Default: ``"standard"``

    Raises ConfigError if the resolved value is not a valid tier.
    """
    candidates: list[tuple[str, str | None]] = [
        ("--tier", cli_flag),
        ("ATLAS_TIER", os.environ.get("ATLAS_TIER")),
    ]
    for source, value in candidates:
        if value is None:
            continue
        normalized = value.strip().lower()
        if normalized not in _VALID_TIERS:
            raise ConfigError(
                f"Invalid tier '{value}' from {source}",
                details={
                    "valid": sorted(_VALID_TIERS),
                    "source": source,
                },
            )
        return normalized  # type: ignore[return-value]

    if _config is not None:
        return _config.tier

    return "standard"


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
