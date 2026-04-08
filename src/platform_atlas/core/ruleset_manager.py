"""
Ruleset Manager for handling loading between different rulesets
"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import json
import logging
import re

from platform_atlas.core.paths import (
    ATLAS_RULESETS_DIR,
    ATLAS_PROFILES_DIR,
    ATLAS_SETTINGS_FILE
)
from platform_atlas.core import rules
from platform_atlas.core.utils import secure_mkdir

RULESET_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class RulesetMetadata:
    """Metadata extracted from a ruleset file"""
    id: str
    name: str
    version: str
    description: str
    author: str
    target_product: str
    file_path: Path
    rule_count: int
    last_modified: datetime

@dataclass(frozen=True, slots=True)
class ProfileMetadata:
    """Metadata for a profile overlay"""
    id: str
    name: str
    description: str
    file_path: Path
    override_count: int

class RulesetManager:
    """Manages ruleset loading and active state"""

    SETTINGS_FILE = ATLAS_SETTINGS_FILE
    RULESETS_DIR = ATLAS_RULESETS_DIR
    PROFILES_DIR = ATLAS_PROFILES_DIR

    def __init__(self):
        secure_mkdir(self.SETTINGS_FILE.parent)
        secure_mkdir(self.RULESETS_DIR)
        secure_mkdir(self.PROFILES_DIR)
        self._restore_active()

    def _resolve_ruleset_path(self, ruleset_id: str) -> Path | None:
        """
        Resolve a ruleset ID to its file path.

        Fast path: checks for ``{id}.json`` directly.
        Fallback:  scans all JSON files in the rulesets directory for a
                   file whose internal ``ruleset.id`` matches. This handles
                   the case where the filename doesn't match the internal ID
                   (e.g., file is ``20231-master-ruleset.json`` but the
                   internal ID is ``2023-master-ruleset``).
        """
        # Fast path: filename matches ID
        direct = self.RULESETS_DIR / f"{ruleset_id}.json"
        if direct.is_file():
            return direct

        # Fallback: scan for matching internal ID
        if not self.RULESETS_DIR.is_dir():
            return None

        for json_file in self.RULESETS_DIR.glob("*.json"):
            if not json_file.is_file():
                continue
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("ruleset", {}).get("id") == ruleset_id:
                    logger.debug(
                        "Resolved ruleset '%s' via internal ID scan → %s",
                        ruleset_id, json_file.name,
                    )
                    return json_file
            except (json.JSONDecodeError, KeyError):
                continue

        return None

    def _restore_active(self) -> None:
        """Restore active ruleset from settings if it exists"""
        if self.SETTINGS_FILE.exists():
            try:
                with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                ruleset_id = settings.get('active_ruleset')
                profile_id = settings.get('active_profile')
                if ruleset_id:
                    ruleset_path = self._resolve_ruleset_path(ruleset_id)
                    if ruleset_path:
                        self._load_with_profile(ruleset_path, profile_id)
                    else:
                        logger.warning(
                            "Active ruleset '%s' not found in %s",
                            ruleset_id, self.RULESETS_DIR,
                        )
            except Exception as e:
                logger.debug("Failed to restore active ruleset: %s", e)
                pass

    def __repr__(self) -> str:
        active = self.get_active_ruleset_id()
        profile = self.get_active_profile_id()
        label = active or "none"
        if profile:
            label = f"{label} [profile: {profile}]"
        return f"<RulesetManager active={label!r}>"

    def _save_active(self, ruleset_id: str | None = None, profile_id: str | None = None) -> None:
        """Save active ruleset and profile IDs to settings"""
        with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                'active_ruleset': ruleset_id,
                'active_profile': profile_id,
            }, f, indent=4)

    def _load_profile(self, profile_id: str) -> dict:
        """Load a profile overlay from file"""
        profile_path = self.PROFILES_DIR / f"{profile_id}.json"
        if not profile_path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _apply_profile(self, ruleset_data: dict, profile_id: str) -> dict:
        """Apply profile overrides to a ruleset's rules list"""
        profile = self._load_profile(profile_id)
        overrides = profile.get("rules", {})

        if not overrides:
            return ruleset_data

        data = deepcopy(ruleset_data)
        for rule in data["rules"]:
            rule_id = rule["rule_number"]
            if rule_id in overrides:
                override = overrides[rule_id]
                # Patch top-level fields (enabled, severity, etc.)
                for key, value in override.items():
                    if key == "validation":
                        # Merge validation sub-fields instead of replacing
                        rule.setdefault("validation", {}).update(value)
                    else:
                        rule[key] = value

        applied = len([r for r in data["rules"] if r["rule_number"] in overrides])
        logger.info("Profile '%s': %d/%d overrides applied", profile_id, applied, len(overrides))
        return data

    def _load_with_profile(self, ruleset_path: Path, profile_id: str | None = None) -> None:
        """Load ruleset from file, optionally applying a profile overlay"""
        if profile_id:
            with open(ruleset_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = self._apply_profile(data, profile_id)
            rules.load_rules_from_dict(data)
        else:
            rules.load_rules(str(ruleset_path))

    def _extract_metadata(self, ruleset_path: Path) -> RulesetMetadata:
        """Extract metadata from ruleset file"""
        with open(ruleset_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        rs = data["ruleset"]
        stat = ruleset_path.stat()

        return RulesetMetadata(
            id=rs["id"],
            name=rs["name"],
            version=rs["version"],
            description=rs.get("description", ""),
            author=rs.get("author", ""),
            target_product=rs.get("target_product", ""),
            file_path=ruleset_path,
            rule_count=len(data.get("rules", [])),
            last_modified=datetime.fromtimestamp(stat.st_mtime)
        )

    def discover_rulesets(self) -> list[RulesetMetadata]:
        """Scan directory and return metadata for all valid rulesets"""
        if not self.RULESETS_DIR.exists():
            return []

        metadata_list = []
        for json_file in self.RULESETS_DIR.glob("*.json"):
            try:
                metadata_list.append(self._extract_metadata(json_file))
            except (json.JSONDecodeError, KeyError):
                continue

        return sorted(metadata_list, key=lambda m: m.id)

    def discover_profiles(self) -> list[ProfileMetadata]:
        """Scan profiles directory and return metadata for all valid profiles"""
        if not self.PROFILES_DIR.exists():
            return []

        profiles = []
        for json_file in self.PROFILES_DIR.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profiles.append(ProfileMetadata(
                    id=data.get("profile_id", json_file.stem),
                    name=data.get("profile_name", json_file.stem),
                    description=data.get("description", ""),
                    file_path=json_file,
                    override_count=len(data.get("rules", {})),
                ))
            except (json.JSONDecodeError, KeyError):
                continue

        return sorted(profiles, key=lambda p: p.id)
    # ──────────────────────────────────────────────────────────────

    def set_active_ruleset(self, ruleset_id: str, profile_id: str | None = None) -> None:
        """Load and set a ruleset as active, optionally with a profile"""

        # Validate ruleset_id to prevent path traversal
        if not ruleset_id or not RULESET_ID_PATTERN.match(ruleset_id):
            raise ValueError(
                f"Invalid ruleset ID '{ruleset_id}'."
                "Must contain only alphanumeric characters, hyphens, and underscores."
            )

        # Validate profile_id if provided
        if profile_id and not RULESET_ID_PATTERN.match(profile_id):
            raise ValueError(
                f"Invalid profile ID '{profile_id}'. "
                "Must contain only alphanumeric characters, hyphens, and underscores."
            )

        # Resolve the ruleset file (handles filename != internal ID)
        ruleset_path = self._resolve_ruleset_path(ruleset_id)
        if ruleset_path is None:
            raise FileNotFoundError(f"Ruleset not found: {ruleset_id}")

        # Ensure the resolved path is within the RULESETS_DIR
        if not str(ruleset_path.resolve()).startswith(str(self.RULESETS_DIR.resolve())):
            raise ValueError(f"Invalid ruleset ID: {ruleset_id}")

        # Validate profile exists if specified
        if profile_id:
            profile_path = (self.PROFILES_DIR / f"{profile_id}.json").resolve()
            if not profile_path.exists():
                raise FileNotFoundError(f"Profile not found: {profile_id}")

        # Load with optional profile overlay
        self._load_with_profile(ruleset_path, profile_id)
        self._save_active(ruleset_id, profile_id)

    def get_active_ruleset_id(self) -> str | None:
        """Return currently active ruleset ID from settings"""
        if self.SETTINGS_FILE.exists():
            try:
                with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f).get("active_ruleset")
            except (json.JSONDecodeError, KeyError):
                pass
        return None

    def get_active_profile_id(self) -> str | None:
        """Return currently active profile ID from settings"""
        if self.SETTINGS_FILE.exists():
            try:
                with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f).get("active_profile")
            except (json.JSONDecodeError, KeyError):
                pass
        return None

    def get_metadata(self, ruleset_id: str) -> RulesetMetadata:
        """Get metadata for a specific ruleset"""
        ruleset_path = self._resolve_ruleset_path(ruleset_id)
        if ruleset_path is None:
            raise FileNotFoundError(f"Ruleset not found: {ruleset_id}")
        return self._extract_metadata(ruleset_path)

    def clear_active_ruleset(self) -> None:
        """Clear the active ruleset"""
        self._save_active(None, None)

# Singleton accessor
_manager: RulesetManager | None = None

def get_ruleset_manager() -> RulesetManager:
    """Get the ruleset manager singleton"""
    global _manager
    # Delegate to context if available
    try:
        from platform_atlas.core.context import ctx
        return ctx().manager
    except Exception:
        pass
    # Legacy fallback: auto-create
    if _manager is None:
        _manager = RulesetManager()
    return _manager
