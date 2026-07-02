"""
Ruleset Manager for handling loading between different rulesets
"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
import json
import logging
import os
import re
import tempfile

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
    # Optional tier scope from the profile file's "tier" field. Profiles
    # marked "saas" are visible ONLY under the SaaS tier — and the SaaS
    # tier sees ONLY those profiles. None = a regular (Standard/Extended)
    # profile.
    tier: str | None = None

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
                   any case where the filename and internal ID diverge.
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
            }, f, indent=4, ensure_ascii=False)

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
                # Snapshot the pre-override enabled state so we can mark rules
                # the profile is *actively* disabling (vs ones the base ruleset
                # had already disabled). The marker drives a distinct pill in
                # the WebUI ruleset view so users know where the disable came
                # from — and which file they should edit to flip it back.
                was_enabled = rule.get("enabled", True)
                # Patch top-level fields (enabled, severity, etc.)
                for key, value in override.items():
                    if key == "validation":
                        # Merge validation sub-fields instead of replacing
                        rule.setdefault("validation", {}).update(value)
                    else:
                        rule[key] = value
                if was_enabled and not rule.get("enabled", True):
                    rule["disabled_by_profile"] = True

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

    def discover_rulesets(self, include_legacy: bool | None = None) -> list[RulesetMetadata]:
        """Scan directory and return metadata for visible rulesets.

        Legacy (2023.x) rulesets are hidden unless the active environment
        is marked as a legacy deployment (its ``legacy_profile`` field) —
        the files stay on disk and keep syncing/updating, they just never
        appear in listings or pickers. ``include_legacy`` overrides the
        environment-resolved default (True = show everything, e.g. for
        internal lookups of an already-active ruleset).
        """
        if not self.RULESETS_DIR.exists():
            return []

        metadata_list = []
        for json_file in self.RULESETS_DIR.glob("*.json"):
            try:
                metadata_list.append(self._extract_metadata(json_file))
            except (json.JSONDecodeError, KeyError):
                continue

        allow_legacy = include_legacy if include_legacy is not None else self._resolve_allow_legacy()
        if not allow_legacy:
            metadata_list = [
                m for m in metadata_list
                if not self.ruleset_is_legacy(m.id, m.target_product)
            ]

        return sorted(metadata_list, key=lambda m: m.id)

    def discover_profiles(
        self,
        tier: str | None = None,
        include_all_tiers: bool = False,
        include_legacy: bool | None = None,
    ) -> list[ProfileMetadata]:
        """Scan profiles directory and return metadata for visible profiles.

        Two orthogonal visibility filters apply:

        * Tier scope — SaaS-marked profiles (``"tier": "saas"`` in the
          profile file) appear ONLY when the active tier is SaaS, and the
          SaaS tier sees ONLY those profiles. ``tier`` overrides the
          context-resolved tier; ``include_all_tiers=True`` skips this
          filter. When no tier can be resolved at all (context not
          initialized), the tier filter is skipped rather than guessed.
        * Legacy scope — 2023.x profiles are hidden unless the active
          environment is marked as a legacy deployment (``legacy_profile``
          field). ``include_legacy`` overrides the environment-resolved
          default.
        """
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
                    tier=data.get("tier"),
                ))
            except (json.JSONDecodeError, KeyError):
                continue

        allow_legacy = include_legacy if include_legacy is not None else self._resolve_allow_legacy()
        if not allow_legacy:
            profiles = [p for p in profiles if not self.profile_is_legacy(p.id)]

        if not include_all_tiers:
            active_tier = tier if tier is not None else self._resolve_active_tier()
            if active_tier is not None:
                profiles = [
                    p for p in profiles
                    if self.profile_visible_for_tier(p.tier, active_tier)
                ]

        # Gateway-kind filter.
        # gw4-gw5 profiles are ALWAYS hidden unless the env explicitly confirms
        # both gateways — we never infer this from missing data. For other kinds,
        # filter to the matching suffix when a selection is set; show all
        # non-gw4-gw5 profiles when no selection exists (backward compat).
        gw_kind = self._resolve_gateway_kind()
        if gw_kind:
            profiles = [
                p for p in profiles
                if self._profile_gateway_kind(p.id) in (None, gw_kind)
            ]
        else:
            profiles = [
                p for p in profiles
                if self._profile_gateway_kind(p.id) != "gw4-gw5"
            ]

        return sorted(profiles, key=lambda p: p.id)

    @staticmethod
    def _resolve_active_tier() -> str | None:
        """Best-effort active tier from the context singleton.

        Lazy import (mirrors get_ruleset_manager) — ruleset_manager loads
        before context during init, so a module-level import would cycle.
        Returns None when no context is initialized yet; discovery then
        stays unfiltered rather than guessing a tier.
        """
        try:
            from platform_atlas.core.context import ctx
            return ctx().tier
        except Exception:
            return None

    @staticmethod
    def _resolve_allow_legacy() -> bool:
        """Best-effort legacy marker from the active config.

        True only when the active environment (or global config) carries a
        ``legacy_profile`` value — i.e. the user really runs a 2023.x
        deployment. Fails CLOSED: with no context, legacy stays hidden —
        that is the correct default for every fresh install, and explicit
        ``include_legacy=True`` exists for internal lookups.
        """
        try:
            from platform_atlas.core.context import ctx
            return bool(ctx().config.legacy_profile)
        except Exception:
            return False

    @staticmethod
    def ruleset_is_legacy(ruleset_id: str | None, target_product: str | None = None) -> bool:
        """True when the ruleset targets the legacy 2023.x product line."""
        return (
            "2023" in (ruleset_id or "").lower()
            or "2023" in (target_product or "").lower()
        )

    @staticmethod
    def profile_is_legacy(profile_id: str | None) -> bool:
        """True when the profile is scoped to the legacy 2023.x ruleset."""
        return (profile_id or "").lower().startswith("2023")

    @staticmethod
    def _resolve_gateway_kind() -> str | None:
        """Best-effort gateway kind from the active context.

        For SaaS, reads ``saas_gateway_kind``; for Standard/Extended, reads
        the ``gateway_kind`` field. Returns None when no context is available
        or no explicit selection has been made — the caller treats None as
        "skip the gateway filter".
        """
        try:
            from platform_atlas.core.context import ctx
            cfg = ctx().config
            if cfg.tier == "saas":
                return (cfg.saas_gateway_kind or "").strip().lower() or None
            return (getattr(cfg, "gateway_kind", None) or "").strip().lower() or None
        except Exception:
            return None

    @staticmethod
    def _profile_gateway_kind(profile_id: str) -> str | None:
        """Extract the gateway kind encoded in a profile ID's suffix.

        Returns "gateway4", "gateway5", "gw4-gw5", or "no-gateway" when
        the profile ID ends with one of those suffixes; None otherwise
        (unknown profiles are never filtered out).
        """
        pid = (profile_id or "").lower()
        for suffix in ("-gw4-gw5", "-gateway4", "-gateway5", "-no-gateway"):
            if pid.endswith(suffix):
                return suffix.lstrip("-")
        return None

    @staticmethod
    def profile_visible_for_tier(profile_tier: str | None, active_tier: str | None) -> bool:
        """SaaS-scoped profiles and the SaaS tier are visible only to each other.

        One boolean covers both directions: a ``tier: "saas"`` profile is
        hidden from Standard/Extended, and a SaaS environment hides every
        profile that is NOT SaaS-scoped.
        """
        profile_is_saas = (profile_tier or "").strip().lower() == "saas"
        tier_is_saas = (active_tier or "").strip().lower() == "saas"
        return profile_is_saas == tier_is_saas

    def ensure_ruleset_allowed(self, ruleset_id: str, allow_legacy: bool | None = None) -> None:
        """Raise ValueError when ``ruleset_id`` is a hidden legacy ruleset.

        Guards explicit activation (``ruleset load``, the WebUI activate
        endpoint) — session switching bypasses this so a legacy session's
        bindings keep restoring. Unknown IDs pass through; the caller's
        FileNotFoundError handling stays authoritative.
        """
        resolved = allow_legacy if allow_legacy is not None else self._resolve_allow_legacy()
        if resolved:
            return
        target_product = ""
        try:
            path = self._resolve_ruleset_path(ruleset_id)
            if path is not None:
                target_product = self._extract_metadata(path).target_product
        except Exception:
            pass
        if self.ruleset_is_legacy(ruleset_id, target_product):
            raise ValueError(
                f"Ruleset '{ruleset_id}' targets the legacy 2023.x platform — it is "
                f"available only when the active environment is marked as a legacy "
                f"deployment (its 'legacy_profile' field)."
            )

    def ensure_profile_allowed(
        self,
        profile_id: str,
        tier: str | None = None,
        allow_legacy: bool | None = None,
    ) -> None:
        """Raise ValueError when ``profile_id`` is hidden from this environment.

        Two checks, matching discover_profiles() visibility: the profile's
        tier scope (SaaS profiles only under SaaS, and vice versa) and the
        legacy scope (2023.x profiles only for legacy-marked environments).

        Guards the EXPLICIT activation paths (``ruleset profile set``,
        ``ruleset load --profile``, the WebUI activate endpoint) against
        IDs typed or posted directly — the pickers already filter their
        listings. Session switching deliberately bypasses this: a session's
        bindings (tier + ruleset + profile) restore atomically, and the
        pre-switch context must not veto them. Unknown profile IDs pass
        through here so the caller's FileNotFoundError handling stays
        authoritative.
        """
        resolved_legacy = allow_legacy if allow_legacy is not None else self._resolve_allow_legacy()
        if not resolved_legacy and self.profile_is_legacy(profile_id):
            raise ValueError(
                f"Profile '{profile_id}' targets the legacy 2023.x platform — it is "
                f"available only when the active environment is marked as a legacy "
                f"deployment (its 'legacy_profile' field)."
            )

        active_tier = tier if tier is not None else self._resolve_active_tier()
        if active_tier is None:
            return
        try:
            profile_tier = self._load_profile(profile_id).get("tier")
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if self.profile_visible_for_tier(profile_tier, active_tier):
            return
        if (profile_tier or "").strip().lower() == "saas":
            raise ValueError(
                f"Profile '{profile_id}' is a SaaS-tier profile — it applies only "
                f"to SaaS (single-gateway) environments, not the {active_tier} tier."
            )
        saas_ids = ", ".join(p.id for p in self.discover_profiles(tier="saas")) or "saas-gateway4, saas-gateway5"
        raise ValueError(
            f"Profile '{profile_id}' is not available in the SaaS tier — "
            f"a SaaS environment uses its own gateway profiles ({saas_ids})."
        )
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

    def disable_rule_in_profile(self, rule_number: str, profile_id: str) -> None:
        """Set enabled=False for rule_number in the given profile file."""
        profile_path = self.PROFILES_DIR / f"{profile_id}.json"
        if not profile_path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        with open(profile_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rules_dict = data.setdefault("rules", {})
        entry = rules_dict.setdefault(rule_number, {})
        entry["enabled"] = False
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(self.PROFILES_DIR))
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, profile_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def enable_rule_in_profile(self, rule_number: str, profile_id: str) -> None:
        """Remove the enabled=False override for rule_number in the given profile file."""
        profile_path = self.PROFILES_DIR / f"{profile_id}.json"
        if not profile_path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_id}")
        with open(profile_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rules_dict = data.get("rules", {})
        if rule_number in rules_dict:
            rules_dict[rule_number].pop("enabled", None)
            if not rules_dict[rule_number]:
                del rules_dict[rule_number]
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(self.PROFILES_DIR))
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, profile_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
