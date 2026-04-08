"""
ATLAS // Ruleset File Loader Dataclass
"""

import json
from pathlib import Path
from typing import Any
from dataclasses import dataclass

# ATLAS imports
from platform_atlas.core.json_utils import load_json
from platform_atlas.core.exceptions import RulesetError

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
    """Load ruleset from file"""
    global _ruleset

    data = load_json(path, error_class=RulesetError,
                     required_keys=["rules", "ruleset"])

    _ruleset = Ruleset(
        schema=data.get("$schema"),
        ruleset=data["ruleset"],
        rules=data["rules"],
    )
    return _ruleset

def load_rules_from_dict(data: dict) -> Ruleset:
    """Load ruleset from an already-parsed dict for profile-resolution"""
    global _ruleset

    if "rules" not in data or "ruleset" not in data:
        raise RulesetError(
            "Invalid ruleset data: missing 'rules' or 'ruleset' keys",
            details={"keys_found": list(data.keys())}
        )

    _ruleset = Ruleset(
        schema=data.get("$schema"),
        ruleset=data["ruleset"],
        rules=data["rules"],
    )
    return _ruleset

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
