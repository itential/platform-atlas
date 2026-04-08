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

from platform_atlas.core.config import Config, load_config
from platform_atlas.core.theme import Theme, get_theme_by_id
from platform_atlas.core.paths import ATLAS_CONFIG_FILE
from platform_atlas.core.exceptions import AtlasError, RulesetError

if TYPE_CHECKING:
    from platform_atlas.core.ruleset_manager import RulesetManager
    from platform_atlas.core.rules import Ruleset

logger = logging.getLogger(__name__)

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

_ctx: AtlasContext | None = None

def init_context(
    config_path: Path = ATLAS_CONFIG_FILE,
    env_override: str | None = None,
) -> AtlasContext:
    """
    Initialization of all Atlas subsystems.

    Args:
        config_path: Path to the global config.json.
        env_override: If set, forces this environment name regardless of
                      ATLAS_ENV or the persisted active_environment.
    """
    global _ctx

    # 0. Ensure the active environment still exists on disk
    #    (recovers interactively if the file was deleted)
    from platform_atlas.core.environment import ensure_valid_environment
    ensure_valid_environment(env_override=env_override)

    # 1. Config (with environment overlay if applicable)
    config = load_config(config_path, env_override=env_override)

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
    logger.info("Atlas context initialized (theme=%s, env=%s, ruleset=%s)",
                config.theme, env_label, "loaded" if ruleset else "none")

    return _ctx

def ctx() -> AtlasContext:
    """
    Get the active Atlas context.

    Safe to call from anywhere any init_context() runs in main()
    """
    if _ctx is None:
        raise ContextNotInitializedError()
    return _ctx
