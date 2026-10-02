"""
ATLAS // Application Context

Single initialization point for all Atlas subsystems.
Initialized once in main(), accessed everywhere via ctx()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from platform_atlas.core.config import Config, Tier, load_config
from platform_atlas.core.theme import Theme, get_theme_by_id
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.exceptions import AtlasError, RulesetError, TierViolationError

if TYPE_CHECKING:
    from platform_atlas.core.ruleset_manager import RulesetManager
    from platform_atlas.core.rules import Ruleset

logger = logging.getLogger(__name__)

# Module keys that are safe to register/run while tier=standard.
# Anything not in this set is Extended-only and must never appear in the
# live module registry when Standard is active. The capture engine reads
# this set at registry-build time (defense 1: registry pruning).
STANDARD_MODULE_KEYS: frozenset[str] = frozenset({
    "platform",       # Platform OAuth — always available
    "platform_conf",  # Platform conf via API/values fallbacks (no SSH)
    "gateway4_api",   # ipsdk — Gateway4 runtime via HTTPS
    "manual",         # User-supplied self-report; tier-agnostic
})

# Module keys that require Extended Mode. Used only for documentation
# and acceptance tests — the actual gate is `allowed_modules`.
EXTENDED_MODULE_KEYS: frozenset[str] = frozenset({
    "mongo", "redis", "system", "filesystem",
    "gateway4",   # SSH-based Gateway4 collector (NOT gateway4_api)
    "gateway5", "kubernetes", "kubernetes_helm",
})

# Module keys that may be registered while tier=saas: the Platform (limited
# OAuth pull — adapter/application AVC only), the gateway(s) the environment
# audits (API + SSH collectors), host facts, and the manual architecture form.
# Notably absent — platform_conf (SaaS runs no PLAT-* rules, so no Platform
# config data), mongo, redis, kubernetes. The Platform collector runs in a
# scoped mode under SaaS (adapter/application endpoints only, never indexes or
# SSH); which gateway's modules register is narrowed further by
# ``saas_gateway_kind`` in the modules registry.
SAAS_MODULE_KEYS: frozenset[str] = frozenset({
    "platform",       # Platform OAuth — SaaS-scoped: adapter/application data only
    "gateway4_api",   # ipsdk — Gateway4 runtime via HTTPS
    "gateway4",       # SSH-based Gateway4 collector
    "gateway5",       # Gateway5 env vars via SSH printenv or Compose/Helm file
    "system",         # Host facts — informational context in SaaS
    "filesystem",     # Config-file presence/permissions on the gateway host
    "manual",         # User-supplied self-report; tier-agnostic
})

class ContextNotInitializedError(AtlasError):
    """Raised when ctx() is called before init_context()"""

    def __init__(self) -> None:
        super().__init__(
            "Atlas context not initialized",
            details={"suggestion": "Call init_context() in main() before any other operations"}
        )

@dataclass
class AtlasContext:
    """Holds all initialized Atlas subsystems."""

    config: Config
    theme: Theme
    manager: RulesetManager
    _ruleset: Ruleset | None = field(default=None, repr=False)

    # Ruleset lifecycle
    @property
    def ruleset(self) -> Ruleset:
        """Return the active ruleset, or raise if none is loaded"""
        if self._ruleset is None:
            raise RulesetError(
                "No ruleset loaded",
                details={
                    "suggestion": "Load a ruleset first using:\n  platform-atlas ruleset load <id>",
                    "help": "Run 'platform-atlas ruleset list' to see available rulesets",
                },
            )
        return self._ruleset

    @property
    def has_ruleset(self) -> bool:
        """Check if a ruleset is currently loaded without raising"""
        return self._ruleset is not None

    @property
    def has_profile(self) -> bool:
        """Check if a profile is currently set"""
        return self.manager.get_active_profile_id() is not None

    def load_ruleset(self, ruleset_id: str) -> None:
        """Load and activate a ruleset by ID through the manager"""
        self.manager.set_active_ruleset(ruleset_id)
        # After manager loads rules into the rules modules, pull them out
        from platform_atlas.core.rules import get_ruleset
        self._ruleset = get_ruleset()
        logger.info("Activated ruleset: %s", ruleset_id)

    def clear_ruleset(self) -> None:
        """Deactivate the current ruleset"""
        self.manager.clear_active_ruleset()
        self._ruleset = None
        logger.info("Cleared active ruleset")

    @property
    def rules(self) -> dict:
        """Shortcut: return just the rules dict for the validation engine"""
        return self.ruleset.as_rules_dict()

    # Convenience Functions
    @property
    def organization_name(self) -> str:
        return self.config.organization_name

    @property
    def debug(self) -> bool:
        return self.config.debug

    @property
    def active_environment(self) -> str | None:
        """The name of the active environment, or None if running in legacy mode."""
        return self.config.active_environment

    # ── Tier ───────────────────────────────────────────────────────
    @property
    def tier(self) -> Tier:
        """The active tier ("standard", "extended", or "saas"). See core/config.py."""
        return self.config.tier

    @property
    def is_standard(self) -> bool:
        """True if the active tier is Standard."""
        return self.config.tier == "standard"

    @property
    def is_extended(self) -> bool:
        """True if the active tier is Extended."""
        return self.config.tier == "extended"

    @property
    def is_saas(self) -> bool:
        """True if the active tier is SaaS (Platform-anchored, limited audit)."""
        return self.config.tier == "saas"

    @property
    def saas_gateway_kind(self) -> str | None:
        """The SaaS environment's gateway kind ("gateway4"/"gateway5"/"gw4-gw5"),
        or None for a Platform-only SaaS environment (no gateway)."""
        return self.config.saas_gateway_kind

    @property
    def allowed_modules(self) -> frozenset[str]:
        """
        Module keys that may be registered for the current tier.

        Defense 1 of the hard mode boundary: the capture engine consults
        this set when building the live module registry. Modules outside
        this set are pruned before any collector class is instantiated.
        """
        if self.config.tier == "standard":
            return STANDARD_MODULE_KEYS
        if self.config.tier == "saas":
            return SAAS_MODULE_KEYS
        return STANDARD_MODULE_KEYS | EXTENDED_MODULE_KEYS

    def is_module_allowed(self, module_key: str) -> bool:
        """Convenience: check whether a module key is allowed under the active tier."""
        return module_key in self.allowed_modules


def require_extended(component: str, *, hint: str = "") -> None:
    """
    Guard helper. Call at the top of any Extended-only entry point.

    Raises ``TierViolationError`` unless the active tier is Extended — the
    platform-infrastructure collectors (Mongo, Redis, Kubernetes) have no
    place in either a Standard (app-only) or a SaaS (single-gateway) audit.
    This is defense 2 of the hard mode boundary (alongside registry pruning
    and the tier-aware credential store) — gated collectors invoke this in
    their __init__ / from_config / connect paths so any accidental call
    fails fast, *before* a network connection is attempted.

    If the context is not yet initialized (early-startup paths, tests that
    run without init_context), this is a no-op — the guard only activates
    once Atlas has resolved its tier.
    """
    if _ctx is None:
        return
    if _ctx.config.tier != "extended":
        raise TierViolationError(component, hint=hint)


def require_infra(component: str, *, hint: str = "") -> None:
    """
    Guard helper for components that need infrastructure access — SSH and
    the collectors built on it (system, filesystem, the SSH gateway
    collectors). Available in Extended AND SaaS (a SaaS audit reaches its
    gateway over SSH as needed), never in Standard.

    Raises ``TierViolationError`` if the active tier is Standard. No-op
    when the context is not initialized, mirroring ``require_extended``.
    """
    if _ctx is None:
        return
    if _ctx.config.tier == "standard":
        raise TierViolationError(component, hint=hint)


def forbid_in_saas(component: str, *, hint: str = "") -> None:
    """
    Guard helper for platform-anchored components that must never run in a
    SaaS (single-gateway) audit. Belt-and-suspenders alongside registry
    pruning, which already never registers them under SaaS.

    Raises ``TierViolationError`` if the active tier is SaaS. No-op when
    the context is not initialized.
    """
    if _ctx is None:
        return
    if _ctx.config.tier == "saas":
        raise TierViolationError(component, hint=hint)


_ctx: AtlasContext | None = None

def init_context(
    config_path: Path = ATLAS_CONFIG_FILE,
    env_override: str | None = None,
    tier_override: str | None = None,
) -> AtlasContext:
    """
    Initialization of all Atlas subsystems.

    Args:
        config_path: Path to the global config.json.
        env_override: If set, forces this environment name regardless of
                      ATLAS_ENV or the persisted active_environment.
        tier_override: If set, overrides the tier for this invocation
                       (from --tier CLI flag).
    """
    global _ctx

    # Flush the credential store so the next access re-scopes to the new
    # environment. Without this, the cached store retains the old env's keyring
    # scope across in-process environment switches.
    from platform_atlas.core.credentials import reset_credential_store
    reset_credential_store()

    # 0. Ensure the active environment still exists on disk
    #    (recovers interactively if the file was deleted)
    from platform_atlas.core.environment import ensure_valid_environment
    ensure_valid_environment(env_override=env_override)

    # 1. Config (with environment overlay + optional tier override)
    config = load_config(
        config_path,
        env_override=env_override,
        tier_override=tier_override,
    )

    # 2. Theme
    theme = get_theme_by_id(config.theme)

    # 3. Manager
    from platform_atlas.core.ruleset_manager import RulesetManager
    manager = RulesetManager()

    # 4. Ruleset
    from platform_atlas.core.rules import get_ruleset as _raw_get_ruleset
    try:
        ruleset = _raw_get_ruleset()
    except RulesetError:
        ruleset = None

    _ctx = AtlasContext(
        config=config,
        theme=theme,
        manager=manager,
        _ruleset=ruleset
    )

    env_label = config.active_environment or "none"
    logger.info("Atlas context initialized (tier=%s, theme=%s, env=%s, ruleset=%s)",
                config.tier, config.theme, env_label, "loaded" if ruleset else "none")

    return _ctx

def ctx() -> AtlasContext:
    """
    Get the active Atlas context.

    Safe to call from anywhere any init_context() runs in main()
    """
    if _ctx is None:
        raise ContextNotInitializedError()
    return _ctx
