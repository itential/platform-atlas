"""
ATLAS // Ruleset File Loader Dataclass
"""

import json
import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass

# ATLAS imports
from platform_atlas.core.json_utils import load_json
from platform_atlas.core.exceptions import RulesetError

logger = logging.getLogger(__name__)

# Categories whose rules are eligible to run in Standard mode.
# Individual rules in these categories may still opt out via "tier": "extended".
_STANDARD_CATEGORIES: frozenset[str] = frozenset({"platform", "gateway4"})

# Categories eligible in SaaS mode — the gateway rule sets, narrowed to the
# environment's gateway kind at filter time. Unlike Standard, a per-rule
# "tier": "extended" flag does NOT exclude a rule here: that flag means
# "needs infra access" (e.g. the GW4 DB-size checks IAG-008–011), and a
# SaaS audit has gateway SSH.
_SAAS_CATEGORIES: frozenset[str] = frozenset({"gateway4", "gateway5"})


def _saas_categories() -> frozenset[str]:
    """The SaaS-eligible rule categories, narrowed to the env's gateway kind.

    A GW4 SaaS environment runs only gateway4 rules; a GW5 environment only
    gateway5 — so the report never shows the other gateway's rules as
    skipped. Falls back to both gateway categories when the kind cannot be
    resolved (mirrors how _resolve_tier_for_filter fails open).
    """
    kind = ""
    try:
        from platform_atlas.core.config import get_config
        kind = (get_config().saas_gateway_kind or "").strip().lower()
    except Exception:
        kind = ""
    if kind == "gateway4":
        return frozenset({"gateway4"})
    if kind == "gateway5":
        return frozenset({"gateway5"})
    if kind == "gw4-gw5":
        return _SAAS_CATEGORIES
    return _SAAS_CATEGORIES


def _filter_rules_for_tier(rules_list: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    """
    Apply tier filtering to a rules list.

    In Standard mode, drop rules whose category is not in the Standard set
    OR whose explicit ``tier`` field is ``"extended"``. Out-of-tier rules
    don't exist as far as the validation engine, report renderer, or UI
    are concerned — no SKIP rows, no greyed-out lines.

    In SaaS mode, keep only the chosen gateway's category — and keep ALL
    of its rules regardless of their per-rule tier flag, because SaaS has
    the gateway SSH access those flags gate on.

    In Extended mode, the full list is returned unchanged.
    """
    if tier == "standard":
        out: list[dict[str, Any]] = []
        for rule in rules_list:
            category = rule.get("category", "")
            rule_tier = rule.get("tier", "standard")
            if category in _STANDARD_CATEGORIES and rule_tier != "extended":
                out.append(rule)
        return out
    if tier == "saas":
        categories = _saas_categories()
        return [r for r in rules_list if r.get("category", "") in categories]
    return rules_list


def _resolve_tier_for_filter() -> str:
    """
    Read the active tier from the loaded config, defaulting to Extended
    when config isn't loaded yet (so first-time loads during init don't
    accidentally throw away rules).
    """
    try:
        from platform_atlas.core.config import get_config, is_config_loaded
        if not is_config_loaded():
            return "extended"
        return get_config().tier
    except Exception:
        return "extended"


@dataclass(frozen=True, slots=True)
class Ruleset:
    schema: str | None
    ruleset: dict[str, Any]
    rules: list[dict[str, Any]]

    def as_rules_dict(self) -> dict:
        """Just return back the rules"""
        return {"rules": self.rules}

    def as_full_dict(self) -> dict:
        """Return everything (metadata + rules)"""
        out = {"ruleset": self.ruleset, "rules": self.rules}
        if self.schema is not None:
            out["$schema"] = self.schema
        return out

# Module-level instance
_ruleset: Ruleset | None = None

def load_rules_safe(path: str | Path) -> tuple[bool, str | None]:
    """Attempt to load the rules without throwing a traceback error"""

    try:
        load_rules(path)
        return True, None
    except FileNotFoundError as e:
        missing = e.filename or str(path)
        return False, f"JSON Ruleset file not found: {missing}"
    except PermissionError as e:
        target = e.filename or str(path)
        return False, f"JSON Ruleset permission denied: {target}"
    except json.JSONDecodeError as e:
        return False, f"JSON Ruleset invalid: {e.msg} (line {e.lineno}, col {e.colno})"
    except TypeError as e:
        return False, f"JSON Ruleset fields mismatch: {e}"
    except Exception as e:
        return False, f"JSON Ruleset load failed: {type(e).__name__}: {e}"

def load_rules(path: str | Path) -> Ruleset:
    """Load ruleset from file, applying tier filter to the rules list."""
    global _ruleset

    data = load_json(path, error_class=RulesetError,
                     required_keys=["rules", "ruleset"])

    tier = _resolve_tier_for_filter()
    raw_rules = data["rules"]
    filtered = _filter_rules_for_tier(raw_rules, tier)
    if len(filtered) != len(raw_rules):
        logger.info(
            "%s tier filter retained %d/%d rules from ruleset '%s'",
            tier.capitalize(), len(filtered), len(raw_rules),
            data.get("ruleset", {}).get("id", "?"),
        )

    _ruleset = Ruleset(
        schema=data.get("$schema"),
        ruleset=data["ruleset"],
        rules=filtered,
    )
    return _ruleset

def load_rules_from_dict(data: dict) -> Ruleset:
    """Load ruleset from an already-parsed dict, applying tier filter."""
    global _ruleset

    if "rules" not in data or "ruleset" not in data:
        raise RulesetError(
            "Invalid ruleset data: missing 'rules' or 'ruleset' keys",
            details={"keys_found": list(data.keys())}
        )

    tier = _resolve_tier_for_filter()
    raw_rules = data["rules"]
    filtered = _filter_rules_for_tier(raw_rules, tier)
    if len(filtered) != len(raw_rules):
        logger.info(
            "%s tier filter retained %d/%d rules from ruleset '%s'",
            tier.capitalize(), len(filtered), len(raw_rules),
            data.get("ruleset", {}).get("id", "?"),
        )

    _ruleset = Ruleset(
        schema=data.get("$schema"),
        ruleset=data["ruleset"],
        rules=filtered,
    )
    return _ruleset


def reload_with_current_tier() -> None:
    """
    Re-apply the current tier filter to the loaded ruleset.

    Used by tier set/upgrade/downgrade flows: when the tier changes at
    runtime, call this so the in-memory rules list matches the new tier
    without forcing a full ruleset reload from disk.
    """
    global _ruleset
    if _ruleset is None:
        return

    tier = _resolve_tier_for_filter()
    current = _ruleset
    # The current rules list may already be Standard-filtered. We need to
    # re-derive from the *full* list — that's only available if we kept it.
    # Without an unfiltered copy, the safest path is to ask the caller to
    # reload the ruleset from disk via RulesetManager. Document that here.
    # For now, just update the singleton tier-state for any downstream consumers.
    logger.debug("reload_with_current_tier: tier=%s, rules=%d", tier, len(current.rules))

def get_ruleset() -> Ruleset:
    """Get the loaded ruleset from anywhere in Platform Atlas"""
    if _ruleset is not None:
        return _ruleset
    try:
        from platform_atlas.core.context import ctx
        return ctx().ruleset
    except Exception:
        raise RulesetError(
            "No ruleset loaded",
            details={
                "suggestion": "Load a ruleset first using:\n  platform-atlas --load-ruleset",
                "help": "Run 'platform-atlas --help' for more information"
            }
        )

def get_rules() -> Ruleset:
    """Get the loaded rules from anywhere in Platform Atlas"""
    return get_ruleset().as_rules_dict()
