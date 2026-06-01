"""Handler package - importing triggers decorator-based auto-registration"""

from platform_atlas.core.registry import registry

from platform_atlas.core.handlers import (
    session,
    ruleset,
    config,
    preflight,
    guide,
    env,
    tier,
    continuous,
    fleet,
    support_bundle,
)
