"""
ATLAS // Expected-Value Resolvers

Named Python functions that compute a rule's ``expected`` value from the
captured data at evaluation time. A rule opts in by pointing its
``validation.expected`` at a resolver instead of a literal value::

    "expected": { "resolver": "node_range_for_platform" }

The resolver decides *what to expect* given the circumstances (e.g. the
captured Platform version); the rule's normal operator still decides
pass/fail. This keeps the ``(actual, expected)`` operator contract intact and
keeps all branching logic in testable Python rather than in the ruleset JSON.

Add new resolvers here — never embed the logic in the rule.
"""

from typing import Any, Callable

from platform_atlas.validation.operators import parse_version

# Registry of resolver name -> function(data) -> expected value
RESOLVERS: dict[str, Callable[[dict], Any]] = {}


def resolver(name: str) -> Callable[[Callable], Callable]:
    """Register a resolver under ``name`` (mirrors the OPERATORS pattern)."""
    def _register(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        RESOLVERS[name] = fn
        return fn
    return _register


def _platform_version(data: dict) -> Any:
    """Captured Platform version, protocol path first, Kubernetes fallback.

    Mirrors PLAT-010's own path/alt_path so the resolver reads the same value
    the version rule reports.
    """
    # Lazy import: validation_engine imports this module, so importing it at
    # module load time would create a cycle.
    from platform_atlas.validation.validation_engine import extract_value

    return (
        extract_value(data, "platform.health_server.version")
        or extract_value(data, "system.kubernetes.platform_release_version")
    )


# Platform 6.6 bumped the bundled Node.js runtime from v20 to v22. Below 6.6
# the supported runtime is Node 20.x; at 6.6 and above it is Node 22.x.
_NODE_BUMP_PLATFORM_VERSION = "6.6"


@resolver("node_range_for_platform")
def node_range_for_platform(data: dict) -> list[str]:
    """Expected Node.js semver range, keyed on the captured Platform version.

    Returns the inclusive ``in_range`` [low, high] bounds: Node 22.x for
    Platform 6.6+, Node 20.x for anything below. ``semver in_range`` is
    inclusive at BOTH ends, so the upper bound must be the highest version of
    the supported major (``.9999.9999``, the same convention RDS-005 uses),
    not the next major's ``.0.0`` — otherwise Node 21.0.0 / 23.0.0 would pass.
    If the Platform version can't be determined, fall back to the newest
    supported range so a newer deployment isn't held to an older runtime by
    default.
    """
    version = _platform_version(data)

    if version is not None:
        try:
            if parse_version(str(version)) < parse_version(_NODE_BUMP_PLATFORM_VERSION):
                return ["20.0.0", "20.9999.9999"]
        except (ValueError, TypeError):
            pass  # Unparseable version — fall through to the current range.

    return ["22.0.0", "22.9999.9999"]
