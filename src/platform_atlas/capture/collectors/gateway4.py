"""
Gateway4 Collector - Read-only data collection for Itential Automation Gateway

Captures pip packages, sync-config status, and SQLite configuration
from the Gateway4 virtual environment. Paths are auto-discovered
via systemd service inspection with fallback to known defaults.

"""

from __future__ import annotations

import logging
import json
from pathlib import Path
import shlex

from platform_atlas.core.preflight import CheckResult
from platform_atlas.capture.collectors.systemd_discovery import discover_gateway
from platform_atlas.core.transport import Transport, LocalTransport

logger = logging.getLogger(__name__)

class Gateway4Collector:
    """Simple gateway4 collector"""

    def __init__(self,
                 venv_dir: Path | None = None,
                 config_path: Path | None = None,
                 transport: Transport | None = None
    ):
        self._transport = transport or LocalTransport()

        if venv_dir and config_path:
            self._venv_dir = venv_dir
            self._config_path = config_path
            self._sync_config = False
        else:
            paths = discover_gateway(transport=self._transport)
            if paths:
                self._venv_dir = paths.venv_dir
                self._config_path = paths.config_path
                self._sync_config = paths.sync_config
            else:
                # Fallback to known defaults
                self._venv_dir = Path("/opt/automation-gateway/venv")
                self._config_path = Path("/etc/automation-gateway/properties.yml")
                self._sync_config = False

    def __repr__(self) -> str:
        transport = type(self._transport).__name__
        venv = self._venv_dir.name if hasattr(self, '_venv_dir') else "unknown"
        return f"<Gateway4Collector venv={venv!r} transport={transport}>"

    def pip_list(self) -> dict:
        """Get pip package list from a virtual environment"""

        venv_dir = self._venv_dir
        python_bin = venv_dir / "bin" / "python"

        # Verify the python executable is actually a file
        if not self._transport.is_exists(str(python_bin)):
            raise FileNotFoundError(f"Python executable not found: {python_bin}")

        try:
            result = self._transport.run_command(f"{shlex.quote(str(python_bin))} -m pip list --format=json")
            result.check()

            pip_to_list = json.loads(result.stdout)
            return {"pip_list": pip_to_list}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON from pip: {e}")

    def sync_config(self) -> dict:
        """Check if --sync-config is enabled in the gateway service"""
        return {"sync_config_enabled": self._sync_config}

    def get_config(self, db_path: Path | None = None) -> dict:
        """Read config table from the Gateway4 SQLite database"""

        if db_path is None:
            # Use standard default location for database path
            db_path = Path("/var/lib/automation-gateway/automation-gateway.db")

        if not self._transport.is_exists(str(db_path)):
            raise FileNotFoundError(f"SQLite database not found: {db_path}")

        # SQLite SELECT Statement for Gateway4 Paths
        columns = "collection_path,module_path,playbook_path,role_path"
        query = f"SELECT {columns} from config" # nosec B608 - fully hardcoded, no user input

        # Read-only JSON output from sqlite
        cmd = f"sqlite3 -readonly -json {shlex.quote(str(db_path))} {shlex.quote(query)}"

        try:
            result = self._transport.run_command(cmd)
            result.check()

            rows = json.loads(result.stdout)
            for row in rows:
                for k, v in row.items():
                    if isinstance(v, str):
                        try:
                            row[k] = json.loads(v)
                        except (json.JSONDecodeError, ValueError):
                            pass
            return {"config": rows}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON from sqlite3: {e}")

    def preflight(self) -> CheckResult:
        """Test Gateway4 virtual environment exists"""
        service_name = "Gateway4"

        try:
            venv_dir = self._venv_dir
            python_bin = venv_dir / "bin" / "python"

            if not self._transport.is_exists(str(venv_dir)):
                return CheckResult.skip(
                    service_name,
                    "Virtual environment not found",
                    str(venv_dir)
                )

            if not self._transport.is_exists(str(python_bin)):
                return CheckResult.fail(
                    service_name,
                    "Python executable not found in venv",
                    str(python_bin)
                )

            return CheckResult.ok(service_name, "Virtual environment found", str(venv_dir))
        except Exception as e:
            return CheckResult.fail(service_name, f"Check failed: {type(e).__name__}", str(e))

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
