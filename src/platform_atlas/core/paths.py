"""Generates Main Root Path Location"""

from pathlib import Path

# Atlas Directories (bundled with the package)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_TEMPLATES = PROJECT_ROOT / "reporting" / "assets" / "templates"
PROJECT_RULESETS = PROJECT_ROOT / "rules" / "rulesets"
PROJECT_PROFILES = PROJECT_RULESETS / "profiles"
PROJECT_PIPELINES = PROJECT_ROOT / "pipelines"
PROJECT_GUIDES = PROJECT_ROOT / "guides"

# Atlas Home
ATLAS_HOME = Path.home() / ".atlas"
ATLAS_HOME_SESSIONS = ATLAS_HOME / "sessions"
ATLAS_HOME_DIFF = ATLAS_HOME / "diff"

# Atlas Environment Directory
ATLAS_ENVIRONMENTS_DIR = ATLAS_HOME / "environments"

# Browser-based guide/form pages synced from the packaged ``guides/`` dir
# (env-setup.html, tier-upgrade.html, architecture-form.html, whats-new.html)
ATLAS_HOME_GUIDES = ATLAS_HOME / "guides"

# Atlas Rules (local working copy — what Atlas actually loads from)
ATLAS_RULESETS_DIR = ATLAS_HOME / "rules" / "rulesets"
ATLAS_PROFILES_DIR = ATLAS_RULESETS_DIR / "profiles"

# Atlas Operational Pipelines (local working copy)
ATLAS_PIPELINES_DIR = ATLAS_HOME / "pipelines"

# Atlas Home Files
ATLAS_CONFIG_FILE = ATLAS_HOME / "config.json"
ATLAS_SETTINGS_FILE = ATLAS_HOME / "settings.json"

# Encrypted local credential file-store (used when credential_backend is "file",
# or when Vault keeps its connection settings in the file). credentials.py
# resolves these from ATLAS_HOME at runtime so tests that repoint ATLAS_HOME are
# honoured; these constants mirror them for reference / the support-bundle denylist.
ATLAS_CREDENTIALS_FILE = ATLAS_HOME / "credentials.enc"      # AES-GCM ciphertext
ATLAS_CREDENTIALS_SALT = ATLAS_HOME / ".keysalt"             # per-install KDF salt

# Architecture overview is now scoped per-environment under
# ``~/.atlas/architecture/<env>.json``. The legacy global file
# ``~/.atlas/architecture.json`` is kept as a constant only so the
# one-time migration in ``architecture_store.migrate_legacy()`` can
# locate it; new code should use the architecture_store helpers.
ATLAS_ARCHITECTURE_DIR = ATLAS_HOME / "architecture"
ATLAS_ARCHITECTURE_FILE = ATLAS_HOME / "architecture.json"  # legacy / migration only

# Atlas Rules Schema
ATLAS_RULE_SCHEMA_FILE = ATLAS_RULESETS_DIR / "rules.schema.json"

# Atlas USER GUIDE
ATLAS_USER_GUIDE = PROJECT_ROOT / "USER-GUIDE.md"

# Atlas Knowledge Base
KNOWLEDGEBASE_PATH = PROJECT_ROOT / "RULES_KNOWLEDGEBASE.md"

# Atlas Templates
DIFF_TEMPLATE = PROJECT_TEMPLATES / "diff.html"
REPORT_TEMPLATE = PROJECT_TEMPLATES / "report.html"
# Splash / cover page placed at the top level of an exported session archive
# (REPORT.html), linking into the bundled reports under session_files/.
REPORT_SPLASH_TEMPLATE = PROJECT_TEMPLATES / "report_splash.html"
REPORT_JSON_SCHEMA = PROJECT_ROOT / "reporting" / "assets" / "schemas" / "report.schema.json"
OPERATIONAL_TEMPLATE = PROJECT_TEMPLATES / "operational.html"
ARCH_TEMPLATE = PROJECT_TEMPLATES / "arch.html"
# Opt-in single-file report (``--unified``): Compliance + Operational +
# Architecture combined into one standalone HTML, rendered client-side from the
# embedded viewmodel JSON. Replaces 03_report.html when the flag is set.
UNIFIED_REPORT_TEMPLATE = PROJECT_TEMPLATES / "report_unified.html"

# Atlas Log File
ATLAS_LOG_FILE = ATLAS_HOME / "atlas.log"

# Ruleset update state — written when the user declines an available update,
# deleted when an update succeeds or the check shows everything is current.
# WebUI reads this file to render the update-available banner (read-only).
ATLAS_RULESET_UPDATE_STATE = ATLAS_HOME / ".ruleset_update_available.json"

# Platform 6 Paths
PLATFORM6_PATH_ROOT = Path("/opt/itential/platform")
PLATFORM6_LOG_PATH_ROOT = Path("/var/log/itential/platform")
PLATFORM6_WEBSERVER_LOG_PATH = Path("/var/log/itential/platform/webserver.log")
PLATFORM6_AGMANAGER_PRONGHORN = PLATFORM6_PATH_ROOT / "server" / "services" / "app-ag_manager" / "pronghorn.json"

# IAP 2023.x Paths
IAP_PATH_ROOT = Path("/opt/itential/current")
IAP_AGMANAGER_PRONGHORN = IAP_PATH_ROOT / "node_modules" / "@itential" / "app-ag_manager"

# Gateway4 Paths
CONF_FILE_GATEWAY4 = "/etc/automation-gateway/properties.yml"
GATEWAY4_DB_ROOT = Path("/var/lib/automation-gateway")
GATEWAY4_DB_MAIN = GATEWAY4_DB_ROOT / "automation-gateway.db"
GATEWAY4_DB_AUDIT = GATEWAY4_DB_ROOT / "automation-gateway_audit.db"
GATEWAY4_DB_EXEC_HISTORY = GATEWAY4_DB_ROOT / "automation-gateway_exec_history.db"

# Third-Party Paths
CONF_FILE_MONGO = "/etc/mongod.conf"
CONF_FILE_REDIS = "/etc/redis/redis.conf"
CONF_FILE_SENTINEL = "/etc/redis/sentinel.conf"
CONF_FILE_PLATFORM = "/etc/itential/platform.properties"
MONGO_LOG_PATH = "/var/log/mongodb/mongod.log"
