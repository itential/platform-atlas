"""
Platform Atlas // Guided Manual Collector
"""

from __future__ import annotations

import json
import shlex
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from rich.console import Console
from rich.prompt import Confirm
from rich.panel import Panel
from rich.table import Table
from rich import box

from platform_atlas.core import ui

logger = logging.getLogger(__name__)
console = Console()
theme = ui.theme

def _try_json(text: str) -> dict[str, Any] | list | None:
    """Attempt to parse text as JSON. Returns None on failure"""
    try:
        data = json.loads(text.strip())
        if isinstance(data, (dict, list)):
            return data
        return None  # Bare scalars (int, str, bool) fall through to step parser
    except (json.JSONDecodeError, ValueError):
        return None


def _try_yaml(text: str) -> dict[str, Any] | None:
    """Attempt to parse text as YAML. Returns None on failure"""
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
        return None
    except yaml.YAMLError:
        return None


def parse_redis_info(text: str) -> dict[str, Any]:
    """Parse raw 'redis-cli INFO ALL' output into a dict"""
    info: dict[str, Any] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue

        key, _, value = line.partition(":")

        # Coerce obvious numeric types
        if value.isdigit():
            info[key] = int(value)
        else:
            try:
                info[key] = float(value)
            except ValueError:
                info[key] = value

    return info


def parse_redis_acl(text: str) -> list[str]:
    """Parse raw 'redis-cli ACL LIST' output into a list of usernames"""
    users = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # ACL LIST returns full rules; ACL USERS returns just names
        # Handle both: if line starts with "user ", extract the username
        if line.startswith("user "):
            users.append(line.split()[1])
        else:
            users.append(line)
    return users


def _coerce_value(val: str) -> Any:
    """Coerce a string config value to its appropriate Python type"""
    if val.lower() in ("yes", "true", "on"):
        return True
    if val.lower() in ("no", "false", "off"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


COMPOUND_CONFIG_KEYS = frozenset({
    "client-output-buffer-limit",
    "save",
    "rename-command",
})


def parse_unformatted_config(text: str) -> dict[str, Any]:
    """Parse a key-value config file (redis.conf, platform.properties).

    Handles both '=' delimited and space-delimited formats.
    Matches the logic in FileSystemInfoCollector.get_unformatted_config().
    """
    config: dict[str, Any] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            if "=" in line:
                key, _, rest = line.partition("=")
                key = key.strip()
                rest = rest.strip()
                tokens = shlex.split(rest) if rest else []
            else:
                parts = shlex.split(line)
                if not parts:
                    continue
                key = parts[0]
                tokens = parts[1:]
        except ValueError:
            continue

        # Handle compound keys
        if key in COMPOUND_CONFIG_KEYS and len(tokens) >= 1:
            sub_key = tokens[0]
            sub_values = tokens[1:]
            if key not in config:
                config[key] = {}
            if len(sub_values) == 1:
                config[key][sub_key] = _coerce_value(sub_values[0])
            elif sub_values:
                config[key][sub_key] = [_coerce_value(v) for v in sub_values]
            else:
                config[key][sub_key] = None
        elif len(tokens) == 0:
            config[key] = None
        elif len(tokens) == 1:
            config[key] = _coerce_value(tokens[0])
        else:
            config[key] = [_coerce_value(t) for t in tokens]

    return config

def _parse_gateway5_file(text: str) -> dict[str, Any] | None:
    """Parse a docker-compose.yml or helm values.yaml for Gateway5 env vars"""
    from platform_atlas.capture.collectors.gateway5 import _VAR_NAMES

    data = _try_yaml(text)
    if data is None:
        return None

    found: dict[str, str] = {}
    source = "manual"

    # Try docker-compose format: services.*.environment
    services = data.get("services", {})
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        env = svc_def.get("environment")
        if env is None:
            continue

        source = f"docker-compose:{svc_name}"

        if isinstance(env, dict):
            for k, v in env.items():
                if k in _VAR_NAMES and v is not None:
                    found.setdefault(k, str(v))

        elif isinstance(env, list):
            for entry in env:
                entry_str = str(entry)
                if "=" in entry_str:
                    k, _, v = entry_str.partition("=")
                    k = k.strip()
                    if k in _VAR_NAMES:
                        found.setdefault(k, v.strip())

    # Try helm format: gateway.env / gateway.extraEnv / etc
    if not found:
        source = "helm-values"
        for top_key in ("gateway", "gateway5", "itential-gateway", "automation-gateway"):
            section = data.get(top_key)
            if not isinstance(section, dict):
                continue
            for env_key in ("env", "extraEnv", "environment"):
                env_block = section.get(env_key)
                if env_block is None:
                    continue

                if isinstance(env_block, dict):
                    for k, v in env_block.items():
                        if k in _VAR_NAMES and v is not None:
                            found.setdefault(k, str(v))

                elif isinstance(env_block, list):
                    for item in env_block:
                        if isinstance(item, dict) and "name" in item:
                            k = item["name"]
                            v = item.get("value", "")
                            if k in _VAR_NAMES and v is not None:
                                found.setdefault(k, str(v))

    if not found:
        return None

    # Build the same structure as Gateway5Collector.collect_env()
    variables = {name: found.get(name) for name in _VAR_NAMES}
    sources = {k: source for k in found}
    resolved = {k: v for k, v in variables.items() if v is not None}
    unresolved = {k for k, v in variables.items() if v is None}

    return {
        "variables": variables,
        "sources": sources,
        "summary": {
            "total": len(variables),
            "resolved": len(resolved),
            "unresolved": len(unresolved),
            "unresolved_keys": unresolved,
        }
    }

@dataclass(frozen=True, slots=True)
class FileStep:
    """A single file to collect within a blueprint"""
    key: str                                      # Key in assembled dict ("" = use file as entire module value)
    label: str                                    # Display label
    command: str                                  # Exact command for customer to run
    parser: Callable[[str], Any] | None = None    # Optional: raw text → parsed value
    optional: bool = False                        # Can this step be skipped without skipping the whole module?


@dataclass(frozen=True, slots=True)
class CollectionBlueprint:
    """Defines how to manually collect data for one capture module"""
    module: str             # Top-level capture key (e.g., "mongo", "redis")
    name: str               # Human-readable name
    description: str        # What this data is
    steps: list[FileStep]   # One or more files to collect
    required: bool = True   # Whether this module is required by the ruleset
    ruleset_key: str = ""   # First path segment used in ruleset rules (defaults to module if empty).
                            # Set this on *_conf modules whose rules are filed under the parent key.
                            # e.g. redis_conf → ruleset_key="redis" because rules use redis.config_file.*

    @property
    def effective_ruleset_key(self) -> str:
        """The ruleset path prefix this blueprint satisfies"""
        return self.ruleset_key or self.module


def parse_python_version(text: str) -> dict[str, Any] | None:
    """Parse 'python3 --version' or 'python3.11 --version' output"""
    import re
    match = re.search(r"Python\s+(\S+)", text.strip())
    if match:
        return {"version": match.group(1)}
    return None


def parse_agmanager_size(text: str) -> int | None:
    """Parse raw byte count from 'stat -c %s' or 'wc -c' output"""
    # 'wc -c' may output "12345 filename" — take the first token
    parts = text.strip().split()
    if parts:
        try:
            return int(parts[0])
        except ValueError:
            pass
    return None


def parse_gateway4_sync_config(text: str) -> dict[str, Any]:
    """Parse 'systemctl cat automation-gateway' output for the --sync-config flag"""
    for line in text.splitlines():
        if "ExecStart" in line:
            return {
                "sync_config_enabled": "--sync-config" in line,
                "exec_start": line.strip(),
            }
    return {"sync_config_enabled": False, "exec_start": ""}


def parse_gateway4_db_sizes(text: str) -> dict[str, Any] | None:
    """Parse three lines of 'stat -c %s <file>' output into a db sizes dict.

    Expects one raw byte count per line in the order: main, audit, exec_history.
    If the user runs the provided python3 one-liner instead, JSON is handled
    automatically by the standard _try_json loader before this parser runs.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    keys = ["main", "audit", "exec_history"]
    result: dict[str, Any] = {}
    for key, line in zip(keys, lines):
        try:
            result[key] = int(line.split()[0])
        except (ValueError, IndexError):
            pass
    return result or None


def parse_log_lines(text: str) -> dict[str, Any]:
    """Store a log file as a list of non-empty lines for validator consumption"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"lines": lines, "count": len(lines)}


# ── Master Blueprint List ───────────────────────────────────────────────────────
BLUEPRINTS: list[CollectionBlueprint] = [

    # ── MongoDB ──────────────────────────────────────────────────────────
    CollectionBlueprint(
        module="mongo",
        name="MongoDB Status",
        description="MongoDB server status and database statistics",
        steps=[
            FileStep(
                key="server_status",
                label="Server Status",
                command='mongosh --quiet --eval "JSON.stringify(db.adminCommand({serverStatus: 1}))" > mongo_server_status.json',
            ),
            FileStep(
                key="db_stats",
                label="Database Stats",
                command='mongosh --quiet --eval "JSON.stringify(db.adminCommand({dbStats: 1}))" > mongo_db_stats.json',
                optional=True,
            ),
        ],
    ),

    CollectionBlueprint(
        module="mongo_conf",
        name="MongoDB Configuration",
        description="MongoDB configuration file (mongod.conf)",
        ruleset_key="mongo",
        steps=[
            FileStep(
                key="",
                label="mongod.conf",
                command="cat /etc/mongod.conf > mongo_conf.yml",
                parser=_try_yaml,
            ),
        ],
    ),

    CollectionBlueprint(
        module="mongo_repl_status",
        name="MongoDB Replica Set Status",
        description="Replica set member status (rs.status()) — HA2 deployments only",
        ruleset_key="mongo",
        required=False,
        steps=[
            FileStep(
                key="",
                label="rs.status() output",
                command='mongosh --quiet --eval "JSON.stringify(db.adminCommand({replSetGetStatus: 1}))" > mongo_repl_status.json',
            ),
        ],
    ),

    CollectionBlueprint(
        module="mongo_repl_config",
        name="MongoDB Replica Set Config",
        description="Replica set configuration (rs.conf()) — HA2 deployments only",
        ruleset_key="mongo",
        required=False,
        steps=[
            FileStep(
                key="",
                label="rs.conf() output",
                command='mongosh --quiet --eval "JSON.stringify(db.adminCommand({replSetGetConfig: 1}))" > mongo_repl_config.json',
            ),
        ],
    ),

    # ── Redis ────────────────────────────────────────────────────────────
    CollectionBlueprint(
        module="redis",
        name="Redis Info & ACL",
        description="Redis INFO output and ACL user list",
        steps=[
            FileStep(
                key="info",
                label="Redis INFO",
                command="redis-cli INFO ALL > redis_info.txt",
                parser=parse_redis_info,
            ),
            FileStep(
                key="acl_users",
                label="Redis ACL Users",
                command="redis-cli ACL USERS > redis_acl.txt",
                parser=parse_redis_acl,
                optional=True,
            ),
        ],
    ),

    CollectionBlueprint(
        module="redis_conf",
        name="Redis Configuration",
        description="Redis configuration file",
        ruleset_key="redis",
        steps=[
            FileStep(
                key="",
                label="redis.conf",
                command="cat /etc/redis/redis.conf > redis_conf.txt",
                parser=parse_unformatted_config,
            ),
        ],
    ),

    CollectionBlueprint(
        module="redis_sentinel_conf",
        name="Redis Sentinel Configuration",
        description="Redis Sentinel configuration file",
        ruleset_key="redis",
        required=False,
        steps=[
            FileStep(
                key="",
                label="sentinel.conf",
                command="cat /etc/redis/sentinel.conf > sentinel_conf.txt",
                parser=parse_unformatted_config,
            ),
        ],
    ),

    # ── Platform ─────────────────────────────────────────────────────────
    CollectionBlueprint(
        module="platform",
        name="Platform API Data",
        description="Platform health, configuration, and adapter information",
        steps=[
            FileStep(
                key="config",
                label="Platform Configs",
                command="curl -sk https://<platform-host>:3443/server/config?token=TOKEN > platform_config.json",
            ),
            FileStep(
                key="health_server",
                label="Platform Health Server",
                command="curl -sk https://<platform-host>:3443/health/server?token=TOKEN > platform_health_server.json",
            ),
            FileStep(
                key="health_status",
                label="Platform Health Status",
                command="curl -sk https://<platform-host>:3443/health/status?token=TOKEN > platform_health_status.json",
                optional=True,
            ),
            FileStep(
                key="adapter_status",
                label="Adapter Health Status",
                command="curl -sk https://<platform-host>:3443/health/adapters?token=TOKEN > platform_adapter_status.json",
                optional=True,
            ),
            FileStep(
                key="application_status",
                label="Application Health Status",
                command="curl -sk https://<platform-host>:3443/health/applications?token=TOKEN > platform_application_status.json",
                optional=True,
            ),
            FileStep(
                key="adapter_props",
                label="Adapter Properties",
                command="curl -sk https://<platform-host>:3443/adapters?token=TOKEN > platform_adapter_props.json",
                optional=True,
            ),
            FileStep(
                key="application_props",
                label="Application Properties",
                command="curl -sk https://<platform-host>:3443/applications?token=TOKEN > platform_application_props.json",
                optional=True,
            ),
            FileStep(
                key="profile",
                label="Platform Profile (2023.x only)",
                command="curl -sk https://<platform-host>:3443/profiles/<PROFILE_NAME>?token=TOKEN > platform_profile.json",
                optional=True,
            ),
        ],
    ),

    CollectionBlueprint(
        module="platform_conf",
        name="Platform Properties File",
        description="Platform properties configuration file",
        ruleset_key="platform",
        steps=[
            FileStep(
                key="",
                label="platform.properties",
                command="cat /etc/itential/platform.properties > platform_conf.txt",
                parser=parse_unformatted_config,
            ),
        ],
    ),

    # ── Platform: Supplemental SSH Modules ───────────────────────────────
    CollectionBlueprint(
        module="agmanager_size",
        name="AGManager Pronghorn JSON Size",
        description="File size of the AGManager pronghorn.json task registry (used by PLAT-038)",
        ruleset_key="platform",
        steps=[
            FileStep(
                key="",
                label="pronghorn.json size",
                command=(
                    "stat -c %s /opt/itential/platform/server/services/app-ag_manager/pronghorn.json > agmanager_size.txt\n"
                ),
                parser=parse_agmanager_size,
            ),
        ],
    ),

    CollectionBlueprint(
        module="python_version",
        name="Python Version",
        description="Python 3.11 installation check on the Platform server (used by PLAT-040)",
        ruleset_key="platform",
        steps=[
            FileStep(
                key="",
                label="Python version",
                command="python3.11 --version > python_version.txt 2>&1",
                parser=parse_python_version,
            ),
        ],
    ),

    CollectionBlueprint(
        module="platform_logs",
        name="Platform Log File",
        description="Recent entries from the Platform application log",
        required=False,
        steps=[
            FileStep(
                key="",
                label="platform.log",
                command=(
                    "# Collect the most recent 10000 lines of the platform log:\n"
                    "   tail -n 10000 /var/log/itential/platform/itential-platform.log > platform_logs.txt"
                ),
                parser=parse_log_lines,
            ),
        ],
    ),

    CollectionBlueprint(
        module="webserver_logs",
        name="Webserver Log File",
        description="Recent entries from the Platform webserver log",
        required=False,
        steps=[
            FileStep(
                key="",
                label="webserver.log",
                command=(
                    "# Collect the most recent 10000 lines of the webserver log:\n"
                    "   tail -n 10000 /var/log/itential/platform/webserver.log > webserver_logs.txt"
                ),
                parser=parse_log_lines,
            ),
        ],
    ),

    # ── Gateway4 ─────────────────────────────────────────────────────────
    CollectionBlueprint(
        module="gateway4",
        name="Gateway4 Packages",
        description="Python packages installed in the Gateway4 virtual environment",
        steps=[
            FileStep(
                key="",
                label="pip package list",
                command="/opt/automation-gateway/venv/bin/pip list --format=json > gateway4_packages.json",
            ),
        ],
    ),

    CollectionBlueprint(
        module="gateway4_conf",
        name="Gateway4 Configuration",
        description="Gateway4 properties YAML configuration",
        ruleset_key="gateway4",
        steps=[
            FileStep(
                key="",
                label="properties.yml",
                command="cat /etc/automation-gateway/properties.yml > gateway4_conf.yml",
                parser=_try_yaml,
            ),
        ],
    ),

    CollectionBlueprint(
        module="gateway4_db_sizes",
        name="Gateway4 Database Sizes",
        description="SQLite database file sizes for Gateway4 (main, audit, exec_history — used by IAG-009/010/011)",
        ruleset_key="gateway4",
        steps=[
            FileStep(
                key="",
                label="Database file sizes",
                command=(
                    "# Three stat lines (main / audit / exec_history order):\n"
                    "   stat -c %s /opt/automation-gateway/data/automation_gateway.db > gw4_db_sizes.txt\n"
                    "   stat -c %s /opt/automation-gateway/data/audit.db >> gw4_db_sizes.txt\n"
                    "   stat -c %s /opt/automation-gateway/data/exec_history.db >> gw4_db_sizes.txt"
                ),
                parser=parse_gateway4_db_sizes,
            ),
        ],
    ),

    CollectionBlueprint(
        module="gateway4_sync_config",
        name="Gateway4 Sync Config Flag",
        description="Whether --sync-config is present in the Gateway4 systemd ExecStart line (used by IAG-008)",
        ruleset_key="gateway4",
        steps=[
            FileStep(
                key="",
                label="Systemd service file",
                command="systemctl cat automation-gateway > gateway4_sync_config.txt",
                parser=parse_gateway4_sync_config,
            ),
        ],
    ),

    CollectionBlueprint(
        module="gateway5",
        name="Gateway5 Environment",
        description="Gateway5 environment variables from Docker Compose or Helm values",
        steps=[
            FileStep(
                key="",
                label="Docker Compose or Helm values file",
                command=(
                    "# Provide ONE of the following:\n"
                    "   cp /path/to/docker-compose.yml ./gateway5_config.yml\n"
                    "   cp /path/to/values.yaml ./gateway5_config.yml\n"
                ),
                parser=_parse_gateway5_file,
            ),
        ],
    ),

    # ── System ───────────────────────────────────────────────────────────
    CollectionBlueprint(
        module="system",
        name="System Information",
        description="OS, CPU, memory, disk information (optional — Atlas can derive basics without it)",
        required=False,
        steps=[
            FileStep(
                key="",
                label="System info JSON",
                command=(
                    "# Provide a JSON file with keys: cpu, memory, disk, host, os\n"
                    "    #   Or skip this — Atlas will use defaults for system_facts"
                ),
            ),
        ],
    ),
]

def get_blueprints_for_ruleset(
    rules_doc: dict[str, Any],
    attempted_modules: set[str] | None = None,
) -> list[CollectionBlueprint]:
    """Return the blueprints relevant to this capture session.

    Two distinct modes:

    1. **Runtime mode** (``attempted_modules`` provided): The modules_registry
       already knows exactly which modules were registered for this node. Trust
       that list completely — include every blueprint whose module appears in it.
       This is the correct path for full manual captures and post-failure recovery,
       because it mirrors precisely what the automated capture would have done.

    2. **Static mode** (``attempted_modules`` is None): No runtime info is
       available, so derive needed modules from the ruleset rule paths. Blueprints
       whose ``effective_ruleset_key`` appears in the ruleset are included, plus
       any optional blueprints (e.g. ``system``) which are surfaced regardless.

    Args:
        rules_doc:         The loaded ruleset document (``{"rules": [...], ...}``).
        attempted_modules: Set of module names from ``modules_registry`` for this
                           node. Pass this whenever it is available at the call site.
    """
    if attempted_modules is not None:
        # Runtime info is the ground truth — match blueprints to it directly.
        return [bp for bp in BLUEPRINTS if bp.module in attempted_modules]

    # Static fallback: derive needed keys from ruleset rule paths.
    rules = rules_doc.get("rules", [])
    needed_keys: set[str] = set()
    for rule in rules:
        path = rule.get("path", "")
        if path:
            needed_keys.add(path.split(".")[0])

    result = []
    for blueprint in BLUEPRINTS:
        if blueprint.effective_ruleset_key in needed_keys:
            result.append(blueprint)
        elif not blueprint.required:
            # Always surface optional blueprints (e.g. system) even when the
            # ruleset has no specific rules for them.
            result.append(blueprint)

    return result

PROGRESS_FILENAME = "manual_progress.json"


@dataclass
class ManualProgress:
    """Tracks which modules have been collected in a guided session"""
    completed: dict[str, str] = field(default_factory=dict)     # module → source description
    skipped: list[str] = field(default_factory=list)
    capture_data: dict[str, Any] = field(default_factory=dict)  # module → assembled data

    def is_done(self, module: str) -> bool:
        return module in self.completed or module in self.skipped

    def save(self, session_dir: Path) -> None:
        """Persist progress and collected data to disk"""
        path = session_dir / PROGRESS_FILENAME
        serializable = {
            "completed": self.completed,
            "skipped": self.skipped,
            "capture_data": self.capture_data,
        }
        path.write_text(
            json.dumps(serializable, indent=2, default=str),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, session_dir: Path) -> ManualProgress:
        """Load progress from disk, or return fresh if none exists"""
        path = session_dir / PROGRESS_FILENAME
        if not path.exists():
            return cls()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                completed=data.get("completed", {}),
                skipped=data.get("skipped", []),
                capture_data=data.get("capture_data", {}),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Corrupt progress file, starting fresh: %s", e)
            return cls()

class GuidedCollector:
    """Interactive CLI walkthrough for manual data collection"""

    def __init__(
        self,
        session_dir: Path,
        blueprints: list[CollectionBlueprint],
    ):
        self.session_dir = session_dir
        self.blueprints = blueprints
        self.progress = ManualProgress.load(session_dir)

    @property
    def pending_blueprints(self) -> list[CollectionBlueprint]:
        """Blueprints that haven't been completed or skipped yet"""
        return [r for r in self.blueprints if not self.progress.is_done(r.module)]

    @property
    def is_complete(self) -> bool:
        """All required blueprints are done"""
        return all(
            self.progress.is_done(r.module)
            for r in self.blueprints
            if r.required
        )

    def show_status(self) -> None:
        """Display current collection progress"""
        table = Table(
            title="Manual Collection Progress",
            box=box.ROUNDED,
        )
        table.add_column("", width=3)
        table.add_column("Module", style="cyan")
        table.add_column("Name")
        table.add_column("Status", justify="center")

        for blueprint in self.blueprints:
            if blueprint.module in self.progress.completed:
                status = f"[{theme.success}]✓ Done[/{theme.success}]"
                marker = f"[{theme.success}]✓[/{theme.success}]"
            elif blueprint.module in self.progress.skipped:
                status = f"[{theme.text_dim}]Skipped[/{theme.text_dim}]"
                marker = f"[{theme.text_dim}]—[/{theme.text_dim}]"
            else:
                if blueprint.required:
                    status = f"[{theme.warning}]Pending[/{theme.warning}]"
                else:
                    status = f"[{theme.text_dim}]Optional[/{theme.text_dim}]"
                marker = " "

            table.add_row(marker, blueprint.module, blueprint.name, status)

        console.print(table)

    # ── File Loading ─────────────────────────────────────────────────────

    @staticmethod
    def _load_file(file_path: Path, step: FileStep) -> Any:
        """Load a data file — tries JSON first, then the step's parser"""
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as e:
            console.print(f"    [red]Cannot read file: {e}[/red]")
            return None

        # Try JSON first (works for any module)
        json_result = _try_json(text)
        if json_result is not None:
            size_kb = file_path.stat().st_size / 1024
            console.print(f"    [{theme.text_dim}]Loaded JSON ({size_kb:.1f} KB)[/{theme.text_dim}]")
            return json_result

        # Fall back to the step's parser for raw text formats
        if step.parser is not None:
            try:
                parsed = step.parser(text)
                if parsed is not None:
                    size_kb = file_path.stat().st_size / 1024
                    console.print(f"    [{theme.text_dim}]Parsed raw text ({size_kb:.1f} KB)[/{theme.text_dim}]")
                    return parsed
                console.print(f"    [red]Parser returned empty result[/red]")
            except Exception as e:
                console.print(f"    [red]Parser failed: {e}[/red]")
                return None

        console.print(f"    [red]Not valid JSON and no parser available for this file type[/red]")
        return None

    # ── Per-Step Prompting ───────────────────────────────────────────────

    def _prompt_for_step(self, step: FileStep) -> Any:
        """Prompt for a single file step. Returns parsed data, 'skip', or 'quit'"""

        opt_tag = f" [{theme.text_dim}](optional)[/{theme.text_dim}]" if step.optional else ""
        console.print(f"\n  [bold]{step.label}[/bold]{opt_tag}")

        for cmd_line in step.command.split("\n"):
            cmd_line = cmd_line.strip()
            if cmd_line:
                console.print(f"    [cyan]$ {cmd_line}[/cyan]")

        while True:
            prompt_hint = "skip" if step.optional else "skip/quit"
            response = console.input(
                f"\n    Path to file ([bold]{prompt_hint}[/bold]): "
            ).strip()

            if response.lower() in ("skip", "s"):
                return "skip"

            if response.lower() in ("quit", "q"):
                return "quit"

            if not response:
                if step.optional:
                    return "skip"
                continue

            file_path = Path(response).expanduser().resolve()

            if not file_path.exists():
                console.print(f"    [red]File not found: {file_path}[/red]")
                continue

            if not file_path.is_file():
                console.print(f"    [red]Not a file: {file_path}[/red]")
                continue

            data = self._load_file(file_path, step)
            if data is not None:
                console.print(f"    [{theme.success}]✓ Loaded {step.label}[/{theme.success}]")
                return data

            # Load failed — loop back to let them try again
            console.print(f"    [{theme.text_dim}]Try again or type 'skip'[/{theme.text_dim}]")

    # ── Per-Module Prompting ─────────────────────────────────────────────

    def _prompt_for_module(self, blueprint: CollectionBlueprint) -> bool:
        """Guide the customer through collecting one module"""
        req_tag = (
            f"[{theme.warning}]Required[/{theme.warning}]" if blueprint.required
            else f"[{theme.text_dim}]Optional[/{theme.text_dim}]"
        )

        console.print(f"\n{'─' * 60}")
        console.print(
            f"[bold]{blueprint.name}[/bold]  ({blueprint.module})  {req_tag}"
        )
        console.print(f"[{theme.text_dim}]{blueprint.description}[/{theme.text_dim}]")

        # Single step with key="" → the file IS the module data
        # Multiple steps or keyed steps → assemble into a dict
        module_data: dict[str, Any] = {}
        has_any_data = False

        for step in blueprint.steps:
            result = self._prompt_for_step(step)

            if result == "quit":
                raise KeyboardInterrupt("User requested quit")

            if result == "skip":
                if not step.optional:
                    # Skipping a required step — offer to skip the whole module
                    if blueprint.required:
                        if not Confirm.ask(
                            "    This step is required. Skip entire module?",
                            default=False,
                        ):
                            # Re-prompt this step
                            result = self._prompt_for_step(step)
                            if result in ("quit",):
                                raise KeyboardInterrupt("User requested quit")
                            if result == "skip":
                                break  # Skip the whole module
                            # Got data on retry
                            if step.key:
                                module_data[step.key] = result
                            else:
                                module_data = result
                            has_any_data = True
                            continue
                    break  # Skip the whole module
                continue  # Skip just this optional step

            # Got data
            has_any_data = True
            if step.key:
                module_data[step.key] = result
            else:
                # key="" means the file data IS the entire module value
                module_data = result

        if not has_any_data:
            # Entire module was skipped
            self.progress.skipped.append(blueprint.module)
            self.progress.save(self.session_dir)
            console.print(f"  [{theme.text_dim}]Skipped {blueprint.module}[/{theme.text_dim}]")
            return False

        # Store the assembled data
        self.progress.completed[blueprint.module] = f"{len(module_data)} keys collected"
        self.progress.capture_data[blueprint.module] = module_data
        self.progress.save(self.session_dir)

        console.print(
            f"\n  [{theme.success}]✓ {blueprint.name} complete[/{theme.success}]"
        )
        return True

    # ── Main Collection Entry Point ──────────────────────────────────────

    def collect(self) -> dict[str, Any]:
        """Run the interactive guided collection"""
        pending = self.pending_blueprints

        if not pending:
            console.print(
                f"\n[{theme.success}]All modules already collected.[/{theme.success}]"
            )
            self.show_status()
            return self.progress.capture_data

        required_remaining = sum(1 for r in pending if r.required)
        optional_remaining = len(pending) - required_remaining

        console.print(
            Panel(
                f"[bold]Guided Manual Collection[/bold]\n\n"
                f"Atlas will walk you through providing data for each module.\n"
                f"For each step, run the listed command on your platform host,\n"
                f"save the output to a file, then provide the file path here.\n\n"
                f"For manually collecting Platform Endpoints, please see the helper\n"
                f"script 'collect_platform.sh' to streamline this process.\n\n"
                f"Remaining: {required_remaining} required, {optional_remaining} optional\n"
                f"Progress is saved — you can [bold]quit[/bold] and resume anytime.",
                border_style=theme.primary,
            )
        )

        # Show what's already been collected
        if self.progress.completed:
            console.print(
                f"[{theme.text_dim}]Already collected: "
                f"{', '.join(self.progress.completed.keys())}[/{theme.text_dim}]"
            )

        try:
            for blueprint in pending:
                self._prompt_for_module(blueprint)
        except KeyboardInterrupt:
            console.print(
                f"\n\n[{theme.warning}]Collection paused. Progress saved.[/{theme.warning}]"
            )
            console.print(
                f"[{theme.text_dim}]Run the same command again to resume.[/{theme.text_dim}]\n"
            )
            self.show_status()
            raise

        console.print()
        self.show_status()
        return self.progress.capture_data

    def reset(self) -> None:
        """Clear all progress and start over"""
        self.progress = ManualProgress()
        progress_file = self.session_dir / PROGRESS_FILENAME
        if progress_file.exists():
            progress_file.unlink()
        logger.info("Manual collection progress reset")

def _get_blueprint_for_module(module_name: str) -> CollectionBlueprint | None:
    """Find a blueprint matching a failed capture module name"""
    for blueprint in BLUEPRINTS:
        if blueprint.module == module_name:
            return blueprint
    return None

def recover_failed_modules(
    failed_modules: list[str],
    results: dict[str, Any],
) -> int:
    """Offer guided file-based recovery for failed capture modules"""
    recoverable = [
        (name, blueprint)
        for name in failed_modules
        if (blueprint := _get_blueprint_for_module(name)) is not None
    ]

    if not recoverable:
        return 0

    console.print(
        f"\n[{theme.warning}]{len(recoverable)} failed module(s) can be "
        f"provided manually:[/{theme.warning}]"
    )
    for name, blueprint in recoverable:
        console.print(f"  • {blueprint.name} ({name})")
    console.print()

    recovered = 0

    for name, blueprint in recoverable:
        if not Confirm.ask(
            f"Would you like to enter [bold]{blueprint.name}[/bold] data manually?",
            default=False,
        ):
            continue

        console.print(
            f"\n[{theme.text_dim}]{blueprint.description}[/{theme.text_dim}]"
        )

        module_data: dict[str, Any] = {}
        has_any_data = False

        for step in blueprint.steps:
            opt_tag = f" [{theme.text_dim}](optional)[/{theme.text_dim}]" if step.optional else ""
            console.print(f"\n  [bold]{step.label}[/bold]{opt_tag}")

            for cmd_line in step.command.split("\n"):
                cmd_line = cmd_line.strip()
                if cmd_line:
                    console.print(f"    [{theme.primary}]$ {cmd_line}[/{theme.primary}]")

            while True:
                response = console.input(
                    f"\n    Path to file ([bold]skip[/bold] to skip): "
                ).strip()

                if response.lower() in ("skip", "s", ""):
                    break

                file_path = Path(response).expanduser().resolve()

                if not file_path.exists():
                    console.print(f"    [{theme.error}]File not found: {file_path}[/{theme.error}]")
                    continue

                if not file_path.is_file():
                    console.print(f"    [{theme.error}]Not a file: {file_path}[/{theme.error}]")
                    continue

                data = GuidedCollector._load_file(file_path, step)
                if data is not None:
                    console.print(
                        f"    [{theme.success}]✓ Loaded {step.label}[/{theme.success}]"
                    )
                    has_any_data = True

                    if step.key:
                        module_data[step.key] = data
                    else:
                        module_data = data
                    break

                console.print(
                    f"    [{theme.text_dim}]Try again or type 'skip'[/{theme.text_dim}]"
                )

        if has_any_data:
            results[name] = module_data
            recovered += 1
            console.print(
                f"\n  [{theme.success}]✓ {blueprint.name} recovered[/{theme.success}]"
            )
        else:
            console.print(
                f"\n  [{theme.text_dim}]Skipped {blueprint.name}[/{theme.text_dim}]"
            )

    if recovered:
        console.print(
            f"\n[{theme.success}]Recovered {recovered} module(s) "
            f"via guided input[/{theme.success}]\n"
        )

    return recovered
