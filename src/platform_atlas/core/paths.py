"""Generates Main Root Path Location"""

from pathlib import Path

# Atlas Directories (bundled with the package)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_TEMPLATES = PROJECT_ROOT / "reporting" / "assets" / "templates"
PROJECT_RULESETS = PROJECT_ROOT / "rules" / "rulesets"
PROJECT_PROFILES = PROJECT_RULESETS / "profiles"
PROJECT_PIPELINES = PROJECT_ROOT / "pipelines"

# Atlas Home
ATLAS_HOME = Path.home() / ".atlas"
ATLAS_HOME_SESSIONS = ATLAS_HOME / "sessions"
ATLAS_HOME_DIFF = ATLAS_HOME / "diff"

# Atlas Environment Directory
ATLAS_ENVIRONMENTS_DIR = ATLAS_HOME / "environments"

# Atlas Rules (local working copy — what Atlas actually loads from)
ATLAS_RULESETS_DIR = ATLAS_HOME / "rules" / "rulesets"
ATLAS_PROFILES_DIR = ATLAS_RULESETS_DIR / "profiles"

# Atlas Operational Pipelines (local working copy)
ATLAS_PIPELINES_DIR = ATLAS_HOME / "pipelines"

# Atlas Home Files
ATLAS_CONFIG_FILE = ATLAS_HOME / "config.json"
ATLAS_SETTINGS_FILE = ATLAS_HOME / "settings.json"
ATLAS_ARCHITECTURE_FILE = ATLAS_HOME / "architecture.json"

# Atlas Rules Schema
ATLAS_RULE_SCHEMA_FILE = ATLAS_RULESETS_DIR / "rules.schema.json"

# Atlas USER GUIDE
ATLAS_USER_GUIDE = PROJECT_ROOT / "USER-GUIDE.md"

# Atlas Knowledge Base
KNOWLEDGEBASE_PATH = PROJECT_ROOT / "RULES_KNOWLEDGEBASE.md"

# Atlas Customer Data
ATLAS_CUSTOMER_DATA = ATLAS_HOME / "customer-data"

# Atlas Templates
DIFF_TEMPLATE = PROJECT_TEMPLATES / "diff.html"
REPORT_TEMPLATE = PROJECT_TEMPLATES / "report.html"
OPERATIONAL_TEMPLATE = PROJECT_TEMPLATES / "operational.html"

# Atlas Log File
ATLAS_LOG_FILE = ATLAS_HOME / "atlas.log"

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