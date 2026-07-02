# pylint: disable=line-too-long
"""
Dispatch Handler ::: Support Bundle

Collects diagnostic data (Platform health endpoints, logs, system info)
and packs it into a single ZIP for support triage.

Standard tier: Platform OAuth health endpoints + Atlas config.
Extended tier: Above + SSH-based platform/webserver/MongoDB logs + system info.
"""

from __future__ import annotations

import io
import json
import logging
import shlex
import warnings
import zipfile
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from platform_atlas.core import ui
from platform_atlas.core.registry import registry
from platform_atlas.core.context import ctx

theme = ui.theme
console = Console()
logger = logging.getLogger(__name__)

_BUNDLE_HEALTH_ENDPOINTS: dict[str, str] = {
    "health_status":      "/health/status",
    "health_server":      "/health/server",
    "adapter_status":     "/health/adapters",
    "application_status": "/health/applications",
    "server_config":      "/server/config",
}


def _redact_config(config) -> dict:
    """Return a serialisable, redacted view of the active config."""
    import dataclasses
    if dataclasses.is_dataclass(config):
        raw = dataclasses.asdict(config)
    else:
        raw = dict(vars(config))
    _REDACTED = frozenset({
        "platform_client_secret", "mongo_password", "redis_password",
        "ssh_password", "vault_token", "vault_role_secret_id",
    })
    return {
        k: ("***" if k in _REDACTED else v)
        for k, v in raw.items()
    }


def _assert_credentials_available() -> None:
    """Confirm the active credential backend can produce what the bundle needs.

    Raises ``CredentialError`` (or another exception) when it cannot. For the
    Vault backend this forces a live connection, so an unreachable server or a
    failed/expired token surfaces here rather than being silently swallowed by
    each collector — which would otherwise emit an empty, useless bundle. For
    both backends it then confirms the Platform OAuth secret (required by every
    tier to reach the health endpoints) is actually retrievable.
    """
    from platform_atlas.core.credentials import credential_store, CredentialKey
    store = credential_store()                          # Vault: connects here
    store.get_required(CredentialKey.PLATFORM_SECRET)   # raises if unavailable


def _collect_platform_health(log_days: int) -> dict[str, object]:
    """Return health endpoint data from the Platform API.

    Returns a dict of name → response data.  Failed endpoints are
    included as ``{"error": "...", "status": "failed"}`` so the bundle
    is always complete.
    """
    from platform_atlas.capture.collectors.platform import PlatformCollector
    try:
        collector = PlatformCollector.from_config()
    except Exception as exc:
        return {k: {"error": str(exc), "status": "failed"} for k in _BUNDLE_HEALTH_ENDPOINTS}

    results: dict[str, object] = {}
    for name, endpoint in _BUNDLE_HEALTH_ENDPOINTS.items():
        try:
            r = collector._client.get(endpoint)
            results[name] = r.json()
        except Exception as exc:
            results[name] = {"error": str(exc), "status": "failed"}
    try:
        collector.close()
    except Exception:
        pass
    return results


def _collect_raw_logs(transport, since: datetime, progress_cb=None) -> dict[str, str]:
    """Transfer raw log files over the transport for the raw_logs/ bundle section.

    Returns a dict mapping zip sub-path → raw text content.  Failures on
    individual files are logged and skipped so a partial result is always
    returned.
    """
    from platform_atlas.capture.collectors.filesystem import (
        _build_grep_cmd, _grep_parts_apache, _grep_parts_iso, MAX_LOG_FILES, MAX_SSH_WORKERS,
    )
    from platform_atlas.core.paths import (
        MONGO_LOG_PATH, PLATFORM6_LOG_PATH_ROOT, PLATFORM6_WEBSERVER_LOG_PATH,
    )

    until = datetime.now(timezone.utc)
    raw: dict[str, str] = {}

    # ── Platform logs (multiple .log files) ───────────────────────────────
    log_dir = str(PLATFORM6_LOG_PATH_ROOT)
    if transport.is_exists(log_dir):
        find_cmd = (
            f"find {shlex.quote(log_dir)} -maxdepth 1"
            f" -name '*.log' -type f -size -3M"
        )
        find_result = transport.run_command(find_cmd)
        if find_result.return_code == 0 and find_result.stdout.strip():
            candidates = [
                f.strip() for f in find_result.stdout.strip().splitlines() if f.strip()
            ][:MAX_LOG_FILES]
            grep_cmd = _build_grep_cmd(_grep_parts_iso(since, until), *candidates, list_files=True)
            grep_result = transport.run_command(grep_cmd, timeout=30)
            matched = (
                [f.strip() for f in grep_result.stdout.strip().splitlines() if f.strip()]
                if grep_result.stdout.strip() else []
            )
            if matched:
                if progress_cb:
                    progress_cb(f"Transferring {len(matched)} platform log file(s) to raw_logs/…")

                def _read_one(filepath: str) -> tuple[str, str | None]:
                    try:
                        r = transport.run_command(f"cat {shlex.quote(filepath)}", timeout=30)
                        if r.ok and r.stdout:
                            return filepath, r.stdout
                    except Exception as exc:
                        logger.debug("raw_logs: skipping %s: %s", filepath, exc)
                    return filepath, None

                with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as pool:
                    fmap = {pool.submit(_read_one, fp): fp for fp in matched}
                    done = 0
                    for future in as_completed(fmap):
                        filepath, content = future.result()
                        if content:
                            done += 1
                            name = Path(filepath).name
                            raw[f"platform_logs/{name}"] = content
                            if progress_cb:
                                progress_cb(f"  [{done}/{len(matched)}] {name}")

    # ── Webserver log ──────────────────────────────────────────────────────
    ws_path = str(PLATFORM6_WEBSERVER_LOG_PATH)
    if transport.is_exists(ws_path):
        if progress_cb:
            progress_cb("Transferring webserver.log to raw_logs/…")
        cmd = _build_grep_cmd(_grep_parts_apache(since, until), ws_path)
        result = transport.run_command(cmd, timeout=60)
        if result.stdout:
            raw["webserver.log"] = result.stdout

    # ── MongoDB log ────────────────────────────────────────────────────────
    if transport.is_exists(MONGO_LOG_PATH):
        if progress_cb:
            progress_cb("Transferring mongod.log to raw_logs/…")
        cmd = _build_grep_cmd(_grep_parts_iso(since, until), MONGO_LOG_PATH)
        result = transport.run_command(cmd, timeout=60)
        if result.stdout:
            raw["mongod.log"] = result.stdout

    return raw


def _collect_logs_and_system(log_days: int, progress_cb=None) -> tuple[dict, dict, dict]:
    """Collect SSH-based log data and system info (Extended mode only).

    Returns (logs_dict, system_dict, raw_logs_dict).  Any component may be
    partially empty when individual collectors fail — partial failure is
    still useful.
    """
    from platform_atlas.core.exceptions import TierViolationError
    config = ctx().config

    # Find the first SSH-reachable IAP node
    target_dict: dict | None = None
    for t in (config.targets or []):
        if t.get("transport") in ("ssh", "paramiko") and t.get("name"):
            target_dict = t
            break
    if target_dict is None:
        for t in (config.targets or []):
            if t.get("transport") not in ("kubernetes", "local"):
                target_dict = t
                break

    if target_dict is None:
        return {}, {}, {}

    from platform_atlas.core.transport import transport_from_config

    transport = None
    logs: dict[str, object] = {}
    system: dict[str, object] = {}
    raw_logs: dict[str, str] = {}

    # Calculate date range (last log_days days)
    since = datetime.now(timezone.utc) - timedelta(days=log_days)

    try:
        transport = transport_from_config(target_dict)

        # ── Logs (parsed) ─────────────────────────────────────────
        try:
            from platform_atlas.capture.collectors.filesystem import FileSystemInfoCollector
            from platform_atlas.capture.log_parser import ParserConfig, set_parser_config
            set_parser_config(ParserConfig(since=since))
            fs = FileSystemInfoCollector(transport=transport)

            for module, method in (
                ("platform_logs",   "get_platform_logs"),
                ("webserver_logs",  "get_webserver_logs"),
                ("mongodb_logs",    "get_mongo_logs"),
            ):
                try:
                    logs[module] = getattr(fs, method)(since=since)
                except Exception as exc:
                    logs[module] = {"error": str(exc), "status": "failed"}
        except TierViolationError as exc:
            logs["_tier_error"] = str(exc)
        except Exception as exc:
            logs["_collection_error"] = str(exc)

        # ── System info ───────────────────────────────────────────
        try:
            from platform_atlas.capture.collectors.system import SystemInfoCollector
            sys_col = SystemInfoCollector(transport=transport)
            system = sys_col.get_system_info()
        except TierViolationError as exc:
            system["_tier_error"] = str(exc)
        except Exception as exc:
            system["_error"] = str(exc)

        # ── Raw log files ─────────────────────────────────────────
        try:
            raw_logs = _collect_raw_logs(transport, since, progress_cb)
        except Exception as exc:
            logger.debug("Raw log collection failed: %s", exc)

    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    return logs, system, raw_logs


def _get_atlas_version() -> str:
    try:
        from platform_atlas.core._version import __version__
        return __version__
    except Exception:
        return "unknown"


def _get_platform_url(config) -> str | None:
    host = getattr(config, "platform_host", None)
    if not host:
        return None
    protocol = getattr(config, "platform_protocol", "https") or "https"
    port = getattr(config, "platform_port", None)
    if port and str(port) not in ("443", "80"):
        return f"{protocol}://{host}:{port}"
    return f"{protocol}://{host}"


def _describe_connections(config, tier: str) -> list[tuple[str, str]]:
    """Return (label, value) rows describing every system the bundle will contact.

    Used to show the user exactly what environment and hosts are in scope
    *before* they confirm collection — so they can catch a wrong environment
    before any network connections are made.
    """
    rows: list[tuple[str, str]] = []

    if tier in ("standard", "extended"):
        url = _get_platform_url(config)
        if url:
            rows.append(("Platform API", url))

    if tier == "extended":
        for t in (config.targets or []):
            if t.get("transport") in ("ssh", "paramiko"):
                host = t.get("host") or t.get("name")
                if host:
                    rows.append(("SSH target", host))
                break

    if tier == "saas":
        gateway4_uri = getattr(config, "gateway4_uri", "") or ""
        if gateway4_uri:
            rows.append(("Gateway API", gateway4_uri))
        else:
            for t in (config.targets or []):
                if t.get("transport") in ("ssh", "paramiko"):
                    host = t.get("host") or t.get("name")
                    if host:
                        kind = getattr(config, "saas_gateway_kind", None) or "gateway"
                        label = "Gateway 5 host" if "5" in kind else "Gateway 4 host"
                        rows.append((label, host))
                    break

    return rows


def _build_html_viewer(
    platform_health: dict,
    logs: dict,
    system: dict,
    config_redacted: dict,
    manifest: dict,
) -> dict[str, str]:
    """Generate the HTML bundle viewer as multiple files.

    Returns a mapping of zip sub-path → content so the caller can write
    each file into the ZIP archive.  All data is embedded in index.html as
    a JS constant so the bundle works directly from the filesystem
    (file:// protocol) with no server required.
    """
    data = json.dumps(
        {"manifest": manifest, "config": config_redacted,
         "platform": platform_health, "logs": logs, "system": system},
        default=str, ensure_ascii=False, separators=(",", ":"),
    )
    # Prevent </script> in JSON data from closing the script tag prematurely.
    data = data.replace("</", "<\\/")
    ticket = manifest.get("ticket") or "Support Bundle"
    index = _VIEWER_HTML.replace("__BUNDLE_DATA__", data).replace("__TITLE__", ticket)
    return {
        "index.html": index,
        "assets/style.css": _VIEWER_CSS,
        "assets/app.js": _VIEWER_JS,
    }


_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlas Bundle — __TITLE__</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div id="accent-strip"></div>

<header id="topbar">
  <div class="logo">
    <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="2" width="14" height="10" rx="2"/><line x1="4" y1="14" x2="12" y2="14"/><line x1="8" y1="12" x2="8" y2="14"/></svg>
    <div class="logo-text">
      <div class="logo-name">Platform Atlas</div>
      <div class="logo-version" id="tb-ver"></div>
    </div>
  </div>
  <div id="tb-ticket"></div>
  <div id="tb-tier"></div>
  <div id="tb-env"></div>
  <div class="tb-sep"></div>
  <div class="tb-date" id="tb-date"></div>
</header>

<div id="shell">
  <nav id="sidebar">
    <div class="nav-section">Navigation</div>

    <div class="nav-item active" id="ni-overview" onclick="navTo('overview')">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="1" width="6" height="6" rx="1.5"/><rect x="9" y="1" width="6" height="6" rx="1.5"/><rect x="1" y="9" width="6" height="6" rx="1.5"/><rect x="9" y="9" width="6" height="6" rx="1.5"/></svg>
      <span class="nav-label">Overview</span>
    </div>

    <div class="nav-item" id="ni-health" onclick="navTo('health')">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="1,8 4,8 5.5,4 7.5,12 9.5,6 11,8 15,8"/></svg>
      <span class="nav-label">Platform Health</span>
      <span class="nav-badge" id="nb-health" style="display:none"></span>
    </div>

    <div class="nav-item" id="ni-config" onclick="navTo('config')">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41"/></svg>
      <span class="nav-label">Config</span>
    </div>

    <div class="nav-item" id="ni-logs" onclick="navTo('logs')">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="2" width="14" height="12" rx="2"/><polyline points="4,6 7,8 4,10"/><line x1="8" y1="10" x2="12" y2="10"/></svg>
      <span class="nav-label">SSH Logs</span>
      <span class="nav-badge" id="nb-logs" style="display:none"></span>
    </div>

    <div class="nav-item" id="ni-system" onclick="navTo('system')">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="2" width="14" height="5" rx="1.5"/><rect x="1" y="9" width="14" height="5" rx="1.5"/><circle cx="12.5" cy="4.5" r="1"/><circle cx="12.5" cy="11.5" r="1"/><line x1="3" y1="4.5" x2="9" y2="4.5"/><line x1="3" y1="11.5" x2="9" y2="11.5"/></svg>
      <span class="nav-label">System Info</span>
    </div>
  </nav>

  <main id="main">
    <div id="sec-overview" class="sec active"></div>
    <div id="sec-health"   class="sec"></div>
    <div id="sec-config"   class="sec"></div>
    <div id="sec-logs"     class="sec"></div>
    <div id="sec-system"   class="sec"></div>
  </main>
</div>

<script>const BUNDLE=__BUNDLE_DATA__;</script>
<script src="assets/app.js"></script>
</body>
</html>"""


_VIEWER_CSS = r"""*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f3f4f6;
  --bg2:#ffffff;
  --surf:#ffffff;
  --surf2:#f9fafb;
  --surf3:#f1f3f5;
  --bdr:rgba(0,0,0,0.08);
  --bdr2:rgba(0,0,0,0.12);
  --bdr3:rgba(0,0,0,0.18);
  --text:#111827;
  --text2:#6b7280;
  --text3:#9ca3af;
  --acc:#4f46e5;
  --acc2:#4338ca;
  --ok:#16a34a;
  --warn:#d97706;
  --bad:#dc2626;
  --info:#2563eb;
  --ok-s:rgba(22,163,74,0.1);
  --warn-s:rgba(217,119,6,0.1);
  --bad-s:rgba(220,38,38,0.1);
  --info-s:rgba(37,99,235,0.1);
  --mono:"Fragment Mono","JetBrains Mono","Cascadia Code",Consolas,monospace;
  --r:10px;--r2:6px;--r3:12px
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font:13.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Inter",system-ui,sans-serif;display:flex;flex-direction:column}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(0,0,0,0.18);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,0.28)}

/* ── Accent strip ── */
#accent-strip{height:4px;background:var(--acc);flex-shrink:0}

/* ── Topbar ── */
#topbar{height:56px;background:var(--bg2);border-bottom:1px solid var(--bdr);box-shadow:0 1px 3px rgba(0,0,0,0.06);display:flex;align-items:center;gap:10px;padding:0 24px;flex-shrink:0;position:relative;z-index:10}
.logo{display:flex;align-items:center;gap:9px;white-space:nowrap;margin-right:4px}
.logo-text{display:flex;flex-direction:column;gap:1px}
.logo-name{font-weight:800;font-size:16px;letter-spacing:-.03em;color:var(--acc);line-height:1.2}
.logo-version{font-size:10.5px;color:var(--text3);font-weight:400;line-height:1.2}
.chip{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:600;padding:3px 10px;border-radius:20px;letter-spacing:.03em;text-transform:uppercase;white-space:nowrap;line-height:1}
.chip-blue{background:rgba(79,70,229,.1);color:var(--acc);border:1px solid rgba(79,70,229,.2)}
.chip-ok{background:rgba(22,163,74,.1);color:var(--ok);border:1px solid rgba(22,163,74,.2)}
.chip-dim{background:rgba(0,0,0,.05);color:var(--text2);border:1px solid var(--bdr)}
.tb-sep{flex:1}
.tb-date{font-size:11.5px;color:var(--text3);white-space:nowrap}

/* ── Shell ── */
#shell{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
#sidebar{width:210px;background:var(--bg2);border-right:1px solid var(--bdr);flex-shrink:0;overflow-y:auto;display:flex;flex-direction:column;padding:16px 10px 28px}
.nav-section{font-size:9.5px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.14em;padding:14px 8px 6px}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 12px;font-size:13px;color:var(--text2);cursor:pointer;border-radius:8px;transition:all .15s ease;user-select:none;margin-bottom:2px}
.nav-item:hover{background:var(--surf3);color:var(--text)}
.nav-item.active{background:rgba(79,70,229,.1);color:var(--acc);font-weight:600}
.nav-icon{width:16px;height:16px;flex-shrink:0;opacity:.55;transition:opacity .15s ease}
.nav-item.active .nav-icon,.nav-item:hover .nav-icon{opacity:1}
.nav-label{flex:1}
.nav-badge{font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:10px;font-family:var(--mono);line-height:1.4}
.nav-badge.bad{background:var(--bad-s);color:var(--bad)}
.nav-badge.warn{background:var(--warn-s);color:var(--warn)}

/* ── Main ── */
#main{flex:1;overflow-y:auto;padding:32px 36px 64px}
.sec{display:none}.sec.active{display:block}

/* ── Section hero ── */
.section-hero{margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid var(--bdr)}
.section-hero .hero-ticket{font-size:2.5rem;font-weight:800;letter-spacing:-.05em;line-height:1.1;color:var(--text);margin-bottom:6px}
.section-hero .hero-meta{font-size:13.5px;color:var(--text2)}

/* ── Page header ── */
.ph{margin-bottom:24px}
.ph-title{font-size:22px;font-weight:700;letter-spacing:-.03em;line-height:1.2;color:var(--text)}
.ph-sub{font-size:13px;color:var(--text2);margin-top:5px}

/* ── Metric tiles ── */
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;margin-bottom:24px}
.tile{background:var(--surf);border:1px solid var(--bdr);border-radius:var(--r3);padding:20px 22px;cursor:pointer;transition:box-shadow .15s,border-color .15s;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
.tile:hover{box-shadow:0 4px 12px rgba(0,0,0,0.1);border-color:var(--bdr2)}
.tile-label{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.tile-value{font-size:2.25rem;font-weight:800;line-height:1;letter-spacing:-.04em;color:var(--text)}
.tile-value.ok{color:var(--ok)}.tile-value.warn{color:var(--warn)}.tile-value.bad{color:var(--bad)}.tile-value.dim{color:var(--text2)}
.tile-sub{font-size:12px;color:var(--text3);margin-top:6px}

/* ── Cards ── */
.card{background:var(--surf);border:1px solid var(--bdr);border-radius:var(--r3);margin-bottom:16px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
.card-head{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid var(--bdr);flex-wrap:wrap}
.card-head h3{font-size:13.5px;font-weight:600;margin:0;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}
.card-head .sub{font-size:11px;color:var(--text3);font-family:var(--mono)}
.card-head .ml{margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0}
.card-body{padding:18px 20px}
.card-body.np{padding:0}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;white-space:nowrap}
.badge .dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex-shrink:0}
.badge-ok{background:var(--ok-s);color:var(--ok)}
.badge-warn{background:var(--warn-s);color:var(--warn)}
.badge-bad{background:var(--bad-s);color:var(--bad)}
.badge-dim{background:rgba(0,0,0,.06);color:var(--text2)}
.badge-acc{background:rgba(79,70,229,.1);color:var(--acc)}
.badge-info{background:var(--info-s);color:var(--info)}

/* ── Tables ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead th{font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.06em;padding:10px 16px;border-bottom:1px solid var(--bdr2);text-align:left;white-space:nowrap;background:var(--surf2);position:sticky;top:0;z-index:1}
td{padding:10px 16px;border-bottom:1px solid var(--bdr);vertical-align:top;font-size:13px;color:var(--text)}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:rgba(79,70,229,.025)}
.td-mono{font-family:var(--mono);font-size:12px}
.td-dim{color:var(--text2);font-size:12px}
.td-break{word-break:break-all;max-width:400px}
.td-num{font-family:var(--mono);text-align:right}

/* ── KV list ── */
.kv-list{padding:2px 0}
.kv{display:flex;align-items:flex-start;gap:16px;padding:7px 0;border-bottom:1px solid var(--bdr)}
.kv:last-child{border-bottom:none}
.kk{font-size:12px;color:var(--text2);min-width:180px;flex-shrink:0;font-weight:500;padding-top:1px}
.kv-val{font-size:13px;color:var(--text);font-family:var(--mono);word-break:break-word;overflow-wrap:break-word;flex:1}

/* ── Code blocks ── */
.code-wrap{position:relative;margin-top:4px}
pre{font-family:var(--mono);font-size:12px;line-height:1.65;background:#f8f9fb;border:1px solid var(--bdr);border-radius:var(--r2);padding:14px 16px;color:#374151;white-space:pre-wrap;word-break:break-all;overflow-x:auto;max-height:420px;overflow-y:auto}
.copy-btn{position:absolute;top:8px;right:8px;background:var(--surf);border:1px solid var(--bdr2);color:var(--text2);font-size:10.5px;padding:3px 10px;border-radius:var(--r2);cursor:pointer;transition:all .13s;font-family:var(--mono)}
.copy-btn:hover{background:var(--acc);color:#fff;border-color:var(--acc)}
.copy-btn.copied{background:var(--ok);color:#fff;border-color:var(--ok)}

/* ── JSON highlight ── */
.jk{color:#1d4ed8}.jstr{color:#15803d}.jred{color:#b45309;font-weight:700}.jnum{color:#7c3aed}.jbool{color:#0891b2}.jnul{color:var(--text3)}

/* ── Expand toggle ── */
.xpand{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--acc);cursor:pointer;user-select:none;padding:4px 0;transition:color .15s ease}
.xpand:hover{color:var(--acc2)}
.xpand::before{content:"▶";font-size:9px;display:inline-block;transition:transform .13s;flex-shrink:0}
.xpand.open::before{transform:rotate(90deg)}
.xpand-body{display:none;margin-top:10px}

/* ── Error banner ── */
.err-banner{background:var(--bad-s);border:1px solid rgba(220,38,38,.2);border-radius:var(--r2);padding:13px 16px;margin-bottom:16px}
.err-banner-title{font-size:11.5px;font-weight:700;color:var(--bad);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.err-banner ul{padding-left:18px}.err-banner li{font-size:12.5px;color:#374151;margin-bottom:3px}

/* ── Progress bar ── */
.prog{margin-bottom:10px}
.prog-row{display:flex;justify-content:space-between;font-size:11.5px;color:var(--text2);margin-bottom:4px}
.prog-track{height:6px;background:rgba(0,0,0,.08);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;border-radius:3px;transition:width .4s ease}
.prog-ok{background:var(--ok)}.prog-warn{background:var(--warn)}.prog-bad{background:var(--bad)}.prog-acc{background:var(--acc)}

/* ── Stats strip ── */
.stats-strip{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--surf);border:1px solid var(--bdr);border-radius:var(--r2);padding:14px 18px;flex:1;min-width:90px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.stat-label{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em}
.stat-value{font-size:1.75rem;font-weight:800;color:var(--text);letter-spacing:-.03em;margin-top:4px;line-height:1}
.stat-value.ok{color:var(--ok)}.stat-value.warn{color:var(--warn)}.stat-value.bad{color:var(--bad)}

/* ── Tabs ── */
.tab-bar{display:flex;gap:0;border-bottom:1px solid var(--bdr);margin-bottom:20px}
.tab-btn{padding:10px 18px;font-size:13px;color:var(--text2);cursor:pointer;border-bottom:2px solid transparent;transition:color .15s ease;user-select:none;margin-bottom:-1px;font-weight:500}
.tab-btn:hover{color:var(--text)}.tab-btn.active{color:var(--acc);border-bottom-color:var(--acc);font-weight:600}
.tab-pane{display:none}.tab-pane.active{display:block}

/* ── Search bar ── */
.search-bar{display:flex;align-items:center;gap:8px;margin-bottom:16px}
.search-input{flex:1;background:var(--surf);border:1px solid var(--bdr2);border-radius:var(--r2);padding:8px 14px;font-size:13px;color:var(--text);font-family:inherit;outline:none;transition:border-color .15s ease;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.search-input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(79,70,229,.1)}.search-input::placeholder{color:var(--text3)}
.search-count{font-size:11.5px;color:var(--text3);white-space:nowrap;min-width:72px;text-align:right}

/* ── Log lines ── */
.log-list{max-height:500px;overflow-y:auto;font-family:var(--mono)}
.log-line{font-size:12px;line-height:1.65;padding:5px 0;border-bottom:1px solid var(--bdr);color:#374151;word-break:break-all}
.log-line:last-child{border-bottom:none}
.log-line mark{background:rgba(217,119,6,.15);color:var(--warn);border-radius:2px;padding:0 2px}
.kw-tag{display:inline-block;font-size:9px;background:var(--warn-s);color:var(--warn);border-radius:3px;padding:1px 5px;margin-right:5px;flex-shrink:0}

/* ── Empty state ── */
.empty{text-align:center;padding:40px 20px;color:var(--text3);font-size:13px}
.empty-icon{font-size:32px;margin-bottom:12px;display:block;opacity:.4}

/* ── Grids ── */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.g2{grid-template-columns:1fr}}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:860px){.g3{grid-template-columns:1fr 1fr}}

/* ── Info note ── */
.info-note{background:var(--info-s);border:1px solid rgba(37,99,235,.15);border-radius:var(--r2);padding:10px 14px;font-size:12.5px;color:#1e40af;margin-bottom:14px}

/* ── Misc ── */
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
hr{border:none;border-top:1px solid var(--bdr);margin:16px 0}

/* ── Hero card ── */
.hero-card{background:linear-gradient(135deg,var(--surf) 0%,rgba(79,70,229,.04) 100%);border:1px solid rgba(79,70,229,.12);border-radius:var(--r3);padding:22px 26px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
.hero-row{display:flex;flex-wrap:wrap;gap:20px 36px}
.hero-item{min-width:110px}
.hero-label{font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.hero-value{font-size:13.5px;font-weight:600;color:var(--text);font-family:var(--mono);word-break:break-all;line-height:1.4}

/* ── Endpoint status strip ── */
.ep-strip{display:flex;flex-wrap:wrap;gap:10px}
.ep-item{display:flex;align-items:center;gap:10px;background:var(--surf);border:1px solid var(--bdr);border-left-width:3px;border-radius:var(--r2);padding:12px 16px;flex:1;min-width:170px;cursor:pointer;transition:box-shadow .15s,border-color .15s;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.ep-item:hover{box-shadow:0 3px 8px rgba(0,0,0,0.1)}
.ep-item.ep-ok{border-left-color:var(--ok)}.ep-item.ep-bad{border-left-color:var(--bad)}.ep-item.ep-warn{border-left-color:var(--warn)}
.ep-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.bg-ok{background:var(--ok)}.bg-bad{background:var(--bad)}.bg-warn{background:var(--warn)}
.ep-name{font-size:13px;font-weight:600;color:var(--text);flex:1}
.ep-path{font-size:11px;color:var(--text3);font-family:var(--mono);margin-top:1px}

/* ── Inline link ── */
.link-btn{font-size:12.5px;color:var(--acc);cursor:pointer;background:none;border:none;padding:0;font-family:inherit;transition:color .15s ease;font-weight:500}
.link-btn:hover{color:var(--acc2);text-decoration:underline}

/* ── Inline nested KV (small sub-objects) ── */
.kv-nested{background:var(--surf2);border:1px solid var(--bdr);border-radius:var(--r2);padding:8px 12px;margin-top:4px;display:inline-block;min-width:200px;max-width:100%}
.kv-nested .kv{padding:4px 0;border-bottom:1px solid var(--bdr)}
.kv-nested .kv:last-child{border-bottom:none}
.kv-nested .kk{font-size:11px;min-width:120px;color:var(--text3)}
.kv-nested .kv-val{font-size:12px}

/* ── Adapter / Application card grid ── */
.adapter-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-top:4px}
.adapter-card{background:var(--surf);border:1px solid var(--bdr);border-left:3px solid var(--text3);border-radius:var(--r2);padding:14px 16px;transition:box-shadow .15s}
.adapter-card:hover{box-shadow:0 3px 8px rgba(0,0,0,0.08)}
.adapter-card.ok{border-left-color:var(--ok)}.adapter-card.warn{border-left-color:var(--warn)}.adapter-card.bad{border-left-color:var(--bad)}
.adapter-name{font-size:13.5px;font-weight:600;color:var(--text);line-height:1.3}
.adapter-meta{font-size:12px;color:var(--text2);margin-top:5px}

/* ── Overview org name hero ── */
.hero-org-name{font-size:2.4rem;font-weight:800;letter-spacing:-.04em;line-height:1.1;color:var(--text);margin-bottom:10px}
.hero-ticket-badge{display:inline-flex;align-items:center;background:rgba(79,70,229,.1);color:var(--acc);font-size:1rem;font-weight:700;letter-spacing:.05em;padding:5px 16px;border-radius:20px;border:1px solid rgba(79,70,229,.22);margin-bottom:10px;font-family:var(--mono)}

/* ── Scrollable description ── */
.kv-desc{max-height:180px;overflow-y:auto;padding-right:6px}
.kv-desc::-webkit-scrollbar{width:4px}
.kv-desc::-webkit-scrollbar-track{background:transparent}
.kv-desc::-webkit-scrollbar-thumb{background:rgba(0,0,0,0.2);border-radius:4px}
.kv-desc::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,0.35)}

/* ── Per-CPU usage grid ── */
.cpu-grid{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.cpu-core{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:36px}
.cpu-core-bar{width:100%;height:36px;background:var(--surf3);border-radius:4px;overflow:hidden;display:flex;align-items:flex-end}
.cpu-core-fill{width:100%;border-radius:4px;transition:height .3s ease}
.cpu-core-label{font-size:9px;color:var(--text3);font-family:var(--mono)}

/* ── Network interface table ── */
.net-family{font-size:10px;font-weight:600;padding:2px 6px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em;background:var(--info-s);color:var(--info)}"""


_VIEWER_JS = r"""const M  = BUNDLE.manifest || {};
const HP = BUNDLE.platform  || {};
const CF = BUNDLE.config    || {};
const LG = BUNDLE.logs      || {};
const SY = BUNDLE.system    || {};

/* ── Utilities ─────────────────────────────────────────────── */
function esc(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtDate(s){
  if(!s)return'—';
  try{return new Date(s).toLocaleString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'})}
  catch{return String(s)}
}
function fmtBytes(b){
  if(b==null||b===0)return'0 B';
  const u=['B','KB','MB','GB','TB'];
  const i=Math.min(Math.floor(Math.log(b)/Math.log(1024)),4);
  return(b/Math.pow(1024,i)).toFixed(i>0?1:0)+' '+u[i];
}
function fmtNum(n){return n==null?'—':Number(n).toLocaleString()}

/* ── Status badge ────────────────────────────────────────────── */
function badge(raw){
  if(raw==null)return'<span class="badge badge-dim">—</span>';
  const s=String(raw);const v=s.toLowerCase();
  if(/^(ok|up|running|active|healthy|true|1|yes|success|good|enabled)$/.test(v))
    return'<span class="badge badge-ok"><span class="dot"></span>'+esc(s)+'</span>';
  if(/^(fail|error|down|stopped?|crash|false|0|no|bad|critical|unavail)/.test(v))
    return'<span class="badge badge-bad"><span class="dot"></span>'+esc(s)+'</span>';
  if(/^(warn|degrad|start|pending|partial|unknown|disconnect)/.test(v))
    return'<span class="badge badge-warn"><span class="dot"></span>'+esc(s)+'</span>';
  return'<span class="badge badge-dim">'+esc(s)+'</span>';
}
function healthBadge(d){
  if(!d||typeof d!=='object')return badge(null);
  if(d.status==='failed')return'<span class="badge badge-bad"><span class="dot"></span>FAILED</span>';
  const v=d.status||d.healthStatus||d.health;
  if(v)return badge(v);
  return'<span class="badge badge-ok"><span class="dot"></span>OK</span>';
}

/* ── JSON syntax highlighter ─────────────────────────────────── */
function jHtml(v,d){
  d=d||0;
  if(v===null)return'<span class="jnul">null</span>';
  if(typeof v==='boolean')return'<span class="jbool">'+v+'</span>';
  if(typeof v==='number')return'<span class="jnum">'+v+'</span>';
  if(typeof v==='string'){return'<span class="'+(v==='***'?'jred':'jstr')+'">"'+esc(v)+'"</span>'}
  if(Array.isArray(v)){
    if(!v.length)return'[]';
    if(d>4)return'<span class="jnum">[…'+v.length+']</span>';
    const p='  '.repeat(d+1);
    return'[\n'+v.map(x=>p+jHtml(x,d+1)).join(',\n')+'\n'+'  '.repeat(d)+']';
  }
  if(typeof v==='object'){
    const ks=Object.keys(v);
    if(!ks.length)return'{}';
    if(d>4)return'<span class="jnum">{…'+ks.length+' keys}</span>';
    const p='  '.repeat(d+1);
    return'{\n'+ks.map(k=>p+'<span class="jk">"'+esc(k)+'"</span>: '+jHtml(v[k],d+1)).join(',\n')+'\n'+'  '.repeat(d)+'}';
  }
  return esc(String(v));
}

/* ── Code block ──────────────────────────────────────────────── */
let _cid=0,_ct={};
function codeBlock(obj,maxH){
  const id='cb'+(++_cid);
  const style=maxH?'max-height:'+maxH+'px':'';
  return'<div class="code-wrap">'
    +'<button class="copy-btn" onclick="cpBtn(\''+id+'\',this)">Copy</button>'
    +'<pre id="'+id+'"'+(style?' style="'+style+'"':'')+'>'+jHtml(obj)+'</pre>'
    +'</div>';
}
function cpBtn(id,btn){
  const el=document.getElementById(id);if(!el)return;
  const t=el.innerText||el.textContent;
  const done=()=>{btn.textContent='Copied!';btn.classList.add('copied');clearTimeout(_ct[id]);_ct[id]=setTimeout(()=>{btn.textContent='Copy';btn.classList.remove('copied')},1800)};
  if(navigator.clipboard)navigator.clipboard.writeText(t).then(done).catch(()=>{_fbCopy(t);done()});
  else{_fbCopy(t);done()}
}
function _fbCopy(t){const a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;opacity:0';document.body.appendChild(a);a.select();try{document.execCommand('copy')}finally{document.body.removeChild(a)}}

/* ── Expand toggle ───────────────────────────────────────────── */
let _xid=0;
function xBlock(label,inner){
  const id='x'+(++_xid);
  return'<span class="xpand" id="xt'+id+'" onclick="xTog(\''+id+'\')">'
    +esc(label)+'</span>'
    +'<div class="xpand-body" id="xb'+id+'">'+inner+'</div>';
}
function xTog(id){
  const t=document.getElementById('xt'+id),b=document.getElementById('xb'+id);
  if(!t||!b)return;
  const o=t.classList.toggle('open');
  b.style.display=o?'block':'none';
}

/* ── Progress bar ────────────────────────────────────────────── */
function progBar(pct,labelLeft,labelRight){
  const cls=pct>90?'prog-bad':pct>75?'prog-warn':'prog-ok';
  return'<div class="prog">'
    +'<div class="prog-row"><span>'+(labelLeft||pct.toFixed(1)+'%')+'</span>'+(labelRight?'<span>'+esc(labelRight)+'</span>':'')+'</div>'
    +'<div class="prog-track"><div class="prog-fill '+cls+'" style="width:'+Math.min(pct,100)+'%"></div></div>'
    +'</div>';
}

/* ── Navigation ──────────────────────────────────────────────── */
function navTo(id){
  document.querySelectorAll('.sec').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const s=document.getElementById('sec-'+id),n=document.getElementById('ni-'+id);
  if(s)s.classList.add('active');
  if(n)n.classList.add('active');
  document.getElementById('main').scrollTop=0;
}

/* ── Tab switcher ────────────────────────────────────────────── */
function switchTab(btns,panes,idx){
  document.querySelectorAll('.'+btns).forEach((t,i)=>t.classList.toggle('active',i===idx));
  document.querySelectorAll('.'+panes).forEach((p,i)=>p.classList.toggle('active',i===idx));
}

/* ═══════════════════════════════════════════════════════════════
   OVERVIEW
   ═══════════════════════════════════════════════════════════════ */
function renderOverview(){
  const el=document.getElementById('sec-overview');
  const errs=M.errors||[];

  // Aggregate counts
  const hKeys=Object.keys(HP);
  const hOK=hKeys.filter(k=>!(HP[k]&&HP[k].status==='failed')).length;
  let adTotal=0,adOK=0;
  const adArr=HP.adapter_status;
  if(Array.isArray(adArr)){adTotal=adArr.length;adOK=adArr.filter(a=>a&&/running|active|ok/i.test(String(a.status||''))).length}
  let appTotal=0,appOK=0;
  const appArr=HP.application_status;
  if(Array.isArray(appArr)){appTotal=appArr.length;appOK=appArr.filter(a=>a&&/running|active|ok|enabled/i.test(String(a.status||''))).length}
  let logMatches=0;
  Object.values(LG).forEach(src=>{if(src&&src.groups)Object.values(src.groups).forEach(g=>{logMatches+=(g.total_matched||0)})});

  // Build hero heading
  const tick = M.ticket || 'Support Bundle';
  const org = M.organization || (CF && CF.organization_name) || '';
  const tier = M.mode ? (M.mode.charAt(0).toUpperCase() + M.mode.slice(1) + ' Tier') : '';
  const env = M.environment || '';
  const metaParts = [tier, env].filter(Boolean).join(' · ');
  let h = '<div class="section-hero">';
  if(org){
    h += '<div class="hero-org-name">' + esc(org) + '</div>';
    if(tick && tick !== 'Support Bundle')
      h += '<div><span class="hero-ticket-badge">' + esc(tick) + '</span></div>';
    if(metaParts) h += '<div class="hero-meta">' + esc(metaParts) + '</div>';
  } else {
    h += '<div class="hero-ticket">' + esc(tick) + '</div>';
    if(metaParts) h += '<div class="hero-meta">' + esc(metaParts) + '</div>';
  }
  h += '</div>';

  // ── Hero card ──────────────────────────────────────────────────
  const heroItems=[
    ['Organization', M.organization],
    ['Environment',  M.environment],
    ['Collection Tier', M.mode?(M.mode.charAt(0).toUpperCase()+M.mode.slice(1)+' Tier'):null],
    ['Platform URL', M.platform_url],
    ['Atlas Version', M.atlas_version?'v'+M.atlas_version:null],
    ['Ruleset',      M.ruleset_id],
    ['Profile',      M.ruleset_profile],
    ['Python',       M.python_version],
  ].filter(([,v])=>v!=null);
  if(heroItems.length){
    h+='<div class="hero-card"><div class="hero-row">';
    heroItems.forEach(([k,v])=>{
      h+='<div class="hero-item"><div class="hero-label">'+esc(k)+'</div>'
        +'<div class="hero-value">'+esc(v)+'</div></div>';
    });
    h+='</div></div>';
  }

  // ── Metric tiles ───────────────────────────────────────────────
  h+='<div class="tiles">';
  const hCls=hOK<hKeys.length?(hOK===0?'bad':'warn'):'ok';
  h+='<div class="tile" onclick="navTo(\'health\')">'
    +'<div class="tile-label">Health Endpoints</div>'
    +'<div class="tile-value '+hCls+'">'+hOK+'/'+hKeys.length+'</div>'
    +'<div class="tile-sub">returned OK</div></div>';
  if(adTotal>0){
    const aCls=adOK<adTotal?(adOK===0?'bad':'warn'):'ok';
    h+='<div class="tile" onclick="navTo(\'health\')">'
      +'<div class="tile-label">Adapters</div>'
      +'<div class="tile-value '+aCls+'">'+adOK+'/'+adTotal+'</div>'
      +'<div class="tile-sub">running</div></div>';
  }
  if(appTotal>0){
    const apCls=appOK<appTotal?(appOK===0?'bad':'warn'):'ok';
    h+='<div class="tile" onclick="navTo(\'health\')">'
      +'<div class="tile-label">Applications</div>'
      +'<div class="tile-value '+apCls+'">'+appOK+'/'+appTotal+'</div>'
      +'<div class="tile-sub">running</div></div>';
  }
  if(Object.keys(LG).filter(k=>!k.startsWith('_')).length>0){
    const lCls=logMatches>50?'bad':logMatches>10?'warn':'ok';
    h+='<div class="tile" onclick="navTo(\'logs\')">'
      +'<div class="tile-label">Log Matches</div>'
      +'<div class="tile-value '+lCls+'">'+fmtNum(logMatches)+'</div>'
      +'<div class="tile-sub">error/warning patterns</div></div>';
  }
  const eCls=errs.length>0?'bad':'dim';
  h+='<div class="tile"><div class="tile-label">Collection Errors</div>'
    +'<div class="tile-value '+eCls+'">'+errs.length+'</div>'
    +'<div class="tile-sub">during collection</div></div>';
  h+='</div>';

  // ── Endpoint quick-status strip ────────────────────────────────
  if(hKeys.length){
    h+='<div class="card"><div class="card-head"><h3>Health Endpoint Status</h3>'
      +'<span class="sub">at time of collection</span>'
      +'<div class="ml"><button class="link-btn" onclick="navTo(\'health\')">View details →</button></div>'
      +'</div><div class="card-body"><div class="ep-strip">';
    hKeys.forEach(k=>{
      const d=HP[k];const failed=d&&typeof d==='object'&&d.status==='failed';
      h+='<div class="ep-item '+(failed?'ep-bad':'ep-ok')+'" onclick="navTo(\'health\')">'
        +'<div class="ep-dot '+(failed?'bg-bad':'bg-ok')+'"></div>'
        +'<div><div class="ep-name">'+esc(EP_LABEL[k]||k)+'</div>'
        +'<div class="ep-path">'+esc(EP_PATH[k]||'/'+k)+'</div></div>'
        +'</div>';
    });
    h+='</div></div></div>';
  }

  // ── Collection errors ──────────────────────────────────────────
  if(errs.length){
    h+='<div class="err-banner"><div class="err-banner-title">⚠ Collection Errors ('+errs.length+')</div><ul>';
    errs.forEach(e=>{h+='<li>'+esc(e)+'</li>'});
    h+='</ul></div>';
  }

  // ── Bundle info + collected items ──────────────────────────────
  h+='<div class="g2">';
  h+='<div class="card"><div class="card-head"><h3>Bundle Information</h3></div><div class="card-body"><div class="kv-list">';
  [['Ticket',M.ticket],['Description',M.description],
   ['Generated',fmtDate(M.generated_at)],
   ['Log Window',M.log_window_days!=null?M.log_window_days+' days':null],
   ['Python',M.python_version],
  ].filter(([,v])=>v!=null).forEach(([k,v])=>{
    if(k==='Description'){
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span>'
        +'<div class="kv-val kv-desc" style="white-space:pre-wrap;font-family:inherit;line-height:1.7">'+esc(v)+'</div></div>';
    } else {
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
    }
  });
  h+='</div></div></div>';
  const coll=M.collected||[];
  if(coll.length){
    h+='<div class="card"><div class="card-head"><h3>What Was Collected</h3>'
      +'<span class="badge badge-ok" style="margin-left:auto"><span class="dot"></span>'+coll.length+' item'+(coll.length!==1?'s':'')+'</span>'
      +'</div><div class="card-body"><div class="kv-list">';
    coll.forEach(item=>{
      h+='<div class="kv">'
        +'<span style="color:var(--ok);font-size:13px;flex-shrink:0;line-height:1.7">✓</span>'
        +'<span class="kv-val" style="color:var(--text2);margin-left:8px">'+esc(item)+'</span></div>';
    });
    h+='</div></div></div>';
  }
  h+='</div>';
  el.innerHTML=h;
}

/* ═══════════════════════════════════════════════════════════════
   PLATFORM HEALTH
   ═══════════════════════════════════════════════════════════════ */
const EP_LABEL={health_status:'IAP Health Status',health_server:'Server Health',adapter_status:'Adapter Status',application_status:'Application Status',server_config:'Server Configuration'};
const EP_PATH={health_status:'/health/status',health_server:'/health/server',adapter_status:'/health/adapters',application_status:'/health/applications',server_config:'/server/config'};

function renderHealth(){
  const el=document.getElementById('sec-health');
  const ks=Object.keys(HP);
  let errCnt=0;
  let h='<div class="ph"><div class="ph-title">Platform Health</div>'
    +'<div class="ph-sub">Responses from IAP health and status endpoints at time of collection.</div></div>';

  if(!ks.length){el.innerHTML=h+'<div class="empty"><span class="empty-icon">📡</span>No health data collected.</div>';return}

  ks.forEach(k=>{
    const d=HP[k];
    const failed=d&&typeof d==='object'&&d.status==='failed';
    if(failed)errCnt++;
    h+='<div class="card"><div class="card-head">'
      +'<h3>'+esc(EP_LABEL[k]||k)+'</h3>'
      +'<span class="sub">'+esc(EP_PATH[k]||'/'+k)+'</span>'
      +'<div class="ml">'+healthBadge(d)+'</div>'
      +'</div><div class="card-body">';

    if(failed){
      h+='<div style="display:flex;align-items:start;gap:10px;padding:4px 0">'
        +'<span style="color:var(--bad);font-size:15px;line-height:1.4">✗</span>'
        +'<span style="color:var(--bad);font-size:12.5px">'+esc(d.error||'Request failed')+'</span>'
        +'</div>';
    } else if(k==='adapter_status'){
      h+=_renderAdapters(d);
    } else if(k==='application_status'){
      h+=_renderApplications(d);
    } else if(k==='server_config'){
      h+=_renderServerConfig(d);
    } else {
      h+=_renderHealthKV(d);
    }

    h+=xBlock('View full JSON response',codeBlock(d,260));
    h+='</div></div>';
  });

  el.innerHTML=h;

  const nb=document.getElementById('nb-health');
  if(nb&&errCnt){nb.textContent=errCnt;nb.className='nav-badge bad';nb.style.display=''}
}

/* ── Flat key→value object as a compact scrollable 2-col table ── */
function _renderFlatObjectTable(obj){
  const keys=Object.keys(obj).sort();
  let h='<div class="tbl-wrap" style="margin-top:6px;max-height:260px;overflow-y:auto">'
    +'<table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>';
  keys.forEach(k=>{
    const v=obj[k];
    const isSt=/status|state|health$/i.test(k);
    h+='<tr><td class="td-mono td-dim" style="white-space:nowrap">'+esc(k)+'</td><td>';
    if(v===null||v===undefined)h+='<span style="color:var(--text3)">—</span>';
    else if(isSt||typeof v==='boolean')h+=badge(String(v));
    else h+='<span class="td-mono">'+esc(String(v))+'</span>';
    h+='</td></tr>';
  });
  return h+'</tbody></table></div>';
}

/* ── Mini table for arrays of objects ── */
function _renderMiniTable(arr){
  if(!arr||!arr.length)return'';
  const allKeys=[...new Set(arr.flatMap(o=>o&&typeof o==='object'?Object.keys(o):[]))];
  if(!allKeys.length)return'<span class="kv-val td-mono">'+esc(arr.map(String).join(', '))+'</span>';
  const keys=allKeys.slice(0,6);
  let h='<div class="tbl-wrap" style="margin-top:6px"><table><thead><tr>';
  keys.forEach(k=>{h+='<th>'+esc(k)+'</th>'});
  h+='</tr></thead><tbody>';
  arr.forEach(row=>{
    if(!row||typeof row!=='object')return;
    h+='<tr>';
    keys.forEach(k=>{
      const cv=row[k];
      const isSt=/status|health$/i.test(k);
      h+='<td>';
      if(cv===null||cv===undefined)h+='<span style="color:var(--text3)">—</span>';
      else if(isSt||typeof cv==='boolean')h+=badge(String(cv));
      else if(typeof cv==='object')h+='<span class="td-mono" style="color:var(--text3)">{…}</span>';
      else h+='<span class="td-mono">'+esc(String(cv))+'</span>';
      h+='</td>';
    });
    h+='</tr>';
  });
  return h+'</tbody></table></div>';
}

/* ── Generic render-all KV — priorityKeys shown first, rest alpha ── */
function _renderAllKV(d,priorityKeys,statusKeys){
  if(!d||typeof d!=='object')return'<span style="color:var(--text3);font-size:12px">No data available.</span>';
  const ks=Object.keys(d);
  if(!ks.length)return'<span style="color:var(--text3);font-size:12px">Empty response.</span>';
  const stSet=new Set(statusKeys||[]);
  const pri=priorityKeys||[];
  const sorted=[
    ...pri.filter(k=>d[k]!=null),
    ...ks.filter(k=>!pri.includes(k)&&d[k]!=null).sort()
  ];
  let h='<div class="kv-list">';
  sorted.forEach(k=>{
    const v=d[k];
    const isSt=stSet.has(k)||(/status|health$/i).test(k);
    h+='<div class="kv"><span class="kk">'+esc(k)+'</span>';
    if(v===null||v===undefined){
      h+='<span class="kv-val" style="color:var(--text3)">—</span>';
    } else if(Array.isArray(v)){
      if(!v.length){
        h+='<span class="kv-val" style="color:var(--text3)">empty</span>';
      } else if(v.every(x=>x===null||typeof x!=='object')){
        h+='<span class="kv-val td-mono">'+esc(v.join(', '))+'</span>';
      } else {
        h+='<div style="width:100%">'+_renderMiniTable(v)+'</div>';
      }
    } else if(typeof v==='object'){
      const vks=Object.keys(v);
      if(!vks.length){
        h+='<span class="kv-val" style="color:var(--text3)">{ }</span>';
      } else if(vks.every(vk=>v[vk]===null||typeof v[vk]!=='object')){
        // All values are primitive — small: inline nested KV, large: scrollable table
        if(vks.length<=8){
          h+='<div class="kv-nested">';
          vks.forEach(vk=>{
            const vv=v[vk];const visSt=/status|state|health$/i.test(vk);
            h+='<div class="kv"><span class="kk">'+esc(vk)+'</span>';
            if(vv===null||vv===undefined)h+='<span class="kv-val" style="color:var(--text3)">—</span>';
            else if(visSt||typeof vv==='boolean')h+=badge(String(vv));
            else h+='<span class="kv-val">'+esc(String(vv))+'</span>';
            h+='</div>';
          });
          h+='</div>';
        } else {
          h+='<div style="width:100%">'+_renderFlatObjectTable(v)+'</div>';
        }
      } else {
        h+='<div style="margin-top:6px;width:100%">'+codeBlock(v,200)+'</div>';
      }
    } else if(isSt||typeof v==='boolean'){
      h+=badge(String(v));
    } else {
      h+='<span class="kv-val">'+esc(String(v))+'</span>';
    }
    h+='</div>';
  });
  return h+'</div>';
}
function _renderHealthKV(d){
  return _renderAllKV(d,
    ['status','healthStatus','health','version','uptime','node','app_status','message'],
    ['status','healthStatus','health','app_status','dbStatus','redisStatus','mqStatus','haStatus']
  );
}

function _renderAdapters(raw){
  const d=Array.isArray(raw)?raw:(raw&&Array.isArray(raw.results)?raw.results:(raw&&Array.isArray(raw.adapters)?raw.adapters:null));
  if(!d||!d.length)return'<div class="empty" style="padding:16px 0"><span class="empty-icon" style="font-size:22px">🔌</span>No adapter data in response.</div>';
  const notRunning=d.filter(a=>a&&!/running|active|ok/i.test(String(a.status||a.state||'')));
  let h='<div style="margin-bottom:14px">';
  if(notRunning.length){
    h+='<span class="badge badge-bad" style="margin-right:8px">'+notRunning.length+' not running</span>';
  }
  h+='<span class="badge badge-dim">'+d.length+' total</span></div>';
  h+='<div class="adapter-grid">';
  d.forEach(a=>{
    if(!a)return;
    const stVal=a.state||a.status||'';
    const st=String(stVal).toLowerCase();
    const cls=(/running|active|ok/i.test(st))?'ok':(/warn|degrad|start|pending/i.test(st))?'warn':'bad';
    h+='<div class="adapter-card '+cls+'">'
      +'<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:6px">'
      +'<div class="adapter-name">'+esc(a.name||a.id||'?')+'</div>'
      +badge(stVal||'—')
      +'</div>'
      +'<div class="adapter-meta">';
    const meta=[];
    if(a.type||a.protocol)meta.push(esc(a.type||a.protocol));
    if(a.package_id)meta.push('<span class="td-mono" style="font-size:11px">'+esc(a.package_id)+'</span>');
    if(a.version||a.build)meta.push('<span class="td-mono">v'+esc(a.version||a.build)+'</span>');
    if(a.routePrefix)meta.push('<span style="font-size:11px">prefix: '+esc(a.routePrefix)+'</span>');
    if(meta.length)h+=meta.join(' · ');
    if(a.description)h+='<div style="margin-top:4px;font-size:11px;color:var(--text3)">'+esc(a.description)+'</div>';
    h+='</div></div>';
  });
  h+='</div>';
  return h;
}

function _renderApplications(raw){
  const d=Array.isArray(raw)?raw:(raw&&Array.isArray(raw.results)?raw.results:(raw&&Array.isArray(raw.applications)?raw.applications:null));
  if(!d||!d.length)return'<div class="empty" style="padding:16px 0"><span class="empty-icon" style="font-size:22px">📦</span>No application data in response.</div>';
  const notOK=d.filter(a=>a&&!/running|active|ok|enabled/i.test(String(a.state||a.status||'')));
  let h='<div style="margin-bottom:14px">';
  if(notOK.length){
    h+='<span class="badge badge-warn" style="margin-right:8px">'+notOK.length+' not running</span>';
  } else {
    h+='<span class="badge badge-ok" style="margin-right:8px"><span class="dot"></span>All running</span>';
  }
  h+='<span class="badge badge-dim">'+d.length+' total</span></div>';
  h+='<div class="adapter-grid">';
  d.forEach(a=>{
    if(!a)return;
    const stVal=a.state||a.status||'';
    const st=String(stVal).toLowerCase();
    const cls=(/running|active|ok|enabled/i.test(st))?'ok':(/warn|degrad|start|pending/i.test(st))?'warn':'bad';
    h+='<div class="adapter-card '+cls+'">'
      +'<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:6px">'
      +'<div class="adapter-name">'+esc(a.name||a.id||'?')+'</div>'
      +badge(stVal||'—')
      +'</div>'
      +'<div class="adapter-meta">';
    const meta=[];
    if(a.type)meta.push(esc(a.type));
    if(a.version||a.build)meta.push('<span class="td-mono">v'+esc(a.version||a.build)+'</span>');
    if(meta.length)h+=meta.join(' · ');
    h+='</div></div>';
  });
  h+='</div>';
  return h;
}

function _renderServerConfig(d){
  if(!d||typeof d!=='object')return'<span style="color:var(--text3);font-size:12px">No config data available.</span>';
  return _renderAllKV(d,
    ['https_port','http_port','host','log_level','session_timeout','max_workers','cluster','redisEnabled','mongoEnabled'],
    ['redisEnabled','mongoEnabled','cluster','ssl','tls','enabled']
  );
}

/* ═══════════════════════════════════════════════════════════════
   CONFIG
   ═══════════════════════════════════════════════════════════════ */
function renderConfig(){
  const el=document.getElementById('sec-config');
  let h='<div class="ph"><div class="ph-title">Atlas Config</div>'
    +'<div class="ph-sub">Redacted configuration from the Atlas environment. Fields marked *** have been redacted for security.</div></div>';

  if(!CF||!Object.keys(CF).length){
    el.innerHTML=h+'<div class="empty"><span class="empty-icon">⚙</span>No config data in bundle.</div>';return;
  }

  // ── Platform connection ────────────────────────────────────────
  const pUrl=M.platform_url||null;
  const pItems=[
    ['Platform URL', pUrl||(CF.platform_host?((CF.platform_protocol||'https')+'://'+CF.platform_host+(CF.platform_port?':'+CF.platform_port:'')):null)],
    ['Host',         CF.platform_host],
    ['Port',         CF.platform_port!=null?String(CF.platform_port):null],
    ['Protocol',     CF.platform_protocol],
    ['Client ID',    CF.platform_client_id],
    ['Client Secret',CF.platform_client_secret],
  ].filter(([,v])=>v!=null&&v!=='://');
  if(pItems.length){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.75"><circle cx="8" cy="8" r="6"/><path d="M8 2c0 0-2 2.5-2 6s2 6 2 6M8 2c0 0 2 2.5 2 6s-2 6-2 6M2 8h12"/></svg>'
      +'<h3>Platform Connection</h3>'
      +(pUrl?'<div class="ml"><span class="badge badge-acc">'+esc(pUrl)+'</span></div>':'')
      +'</div><div class="card-body"><div class="kv-list">';
    pItems.forEach(([k,v])=>{
      const red=v==='***';
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span>'
        +(red?'<span class="kv-val"><span class="jred">***</span> <span style="font-size:11px;color:var(--text3)">(redacted)</span></span>'
          :'<span class="kv-val">'+esc(String(v))+'</span>')
        +'</div>';
    });
    h+='</div></div></div>';
  }

  // ── Ruleset, profile, tier ─────────────────────────────────────
  const rItems=[
    ['Ruleset ID',         CF.ruleset_id||M.ruleset_id],
    ['Profile',            CF.ruleset_profile||M.ruleset_profile],
    ['Tier',               CF.tier||(M.mode?(M.mode.charAt(0).toUpperCase()+M.mode.slice(1)):null)],
    ['Credential Backend', CF.credential_backend],
    ['Deployment Mode',    CF.deployment_mode],
  ].filter(([,v])=>v!=null);
  if(rItems.length){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.75"><path d="M2 4h12v2H2zM2 10h12v2H2z"/><circle cx="5" cy="5" r="1.5"/><circle cx="11" cy="11" r="1.5"/></svg>'
      +'<h3>Ruleset &amp; Profile</h3>'
      +'</div><div class="card-body"><div class="kv-list">';
    rItems.forEach(([k,v])=>{
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(String(v))+'</span></div>';
    });
    h+='</div></div></div>';
  }

  // ── Organization & environment ─────────────────────────────────
  const orgItems=[
    ['Organization',      CF.organization_name||M.organization],
    ['Active Environment',CF.active_environment||M.environment],
    ['Capture Scope',     CF.capture_scope],
  ].filter(([,v])=>v!=null);
  if(orgItems.length){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.75"><rect x="2" y="7" width="12" height="8" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/><circle cx="8" cy="11" r="1.5"/></svg>'
      +'<h3>Organization &amp; Environment</h3>'
      +'</div><div class="card-body"><div class="kv-list">';
    orgItems.forEach(([k,v])=>{
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(String(v))+'</span></div>';
    });
    h+='</div></div></div>';
  }

  // ── Targets ────────────────────────────────────────────────────
  const tgts=CF.targets;
  if(Array.isArray(tgts)&&tgts.length){
    h+='<div class="card"><div class="card-head"><h3>Targets</h3>'
      +'<span class="badge badge-dim" style="margin-left:auto">'+tgts.length+'</span>'
      +'</div><div class="card-body np"><div class="tbl-wrap">'
      +'<table><thead><tr><th>Name</th><th>Role</th><th>Host / Address</th><th>Transport</th></tr></thead><tbody>';
    tgts.forEach(t=>{
      h+='<tr>'
        +'<td class="td-mono">'+esc(t.name||'?')+'</td>'
        +'<td class="td-dim">'+esc(t.role||'—')+'</td>'
        +'<td class="td-mono td-break">'+esc(t.host||t.hostname||t.address||'—')+'</td>'
        +'<td class="td-dim">'+esc(t.transport||'—')+'</td>'
        +'</tr>';
    });
    h+='</tbody></table></div></div></div>';
  }

  // ── Full JSON ──────────────────────────────────────────────────
  h+='<div class="card"><div class="card-head"><h3>Full Config (Redacted)</h3>'
    +'<span class="sub" style="margin-left:auto;color:var(--text3);font-size:11px">Credentials shown as ***</span>'
    +'</div><div class="card-body">'+codeBlock(CF)+'</div></div>';
  el.innerHTML=h;
}

/* ═══════════════════════════════════════════════════════════════
   LOGS
   ═══════════════════════════════════════════════════════════════ */
const SRC_LABEL={platform_logs:'Platform',webserver_logs:'Webserver',mongodb_logs:'MongoDB'};
let _logSt={};

function renderLogs(){
  const el=document.getElementById('sec-logs');
  let h='<div class="ph"><div class="ph-title">SSH Logs</div>'
    +'<div class="ph-sub">Log analysis from the primary IAP node. Available for Extended tier only.</div></div>';

  if(!LG||!Object.keys(LG).length){
    el.innerHTML=h+'<div class="card"><div class="card-body"><div class="empty" style="padding:20px 0">'
      +'<span class="empty-icon">📋</span>No log data — SSH log collection requires Extended tier.</div></div></div>';
    return;
  }

  const errKs=Object.keys(LG).filter(k=>k.startsWith('_'));
  const failKs=Object.keys(LG).filter(k=>!k.startsWith('_')&&LG[k]&&LG[k].status==='failed');
  const goodKs=Object.keys(LG).filter(k=>!k.startsWith('_')&&!(LG[k]&&LG[k].status==='failed'));

  if(errKs.length||failKs.length){
    h+='<div class="err-banner"><div class="err-banner-title">Collection Issues</div><ul>';
    errKs.forEach(k=>{h+='<li>'+esc(k.replace(/^_/,'')+': '+LG[k])+'</li>'});
    failKs.forEach(k=>{h+='<li>'+esc((SRC_LABEL[k]||k)+': '+(LG[k]&&LG[k].error||'Failed'))+'</li>'});
    h+='</ul></div>';
  }

  if(!goodKs.length){
    el.innerHTML=h+'<div class="empty"><span class="empty-icon">📋</span>No log data available.</div>';return;
  }

  // Tab bar
  h+='<div class="tab-bar">';
  goodKs.forEach((k,i)=>{
    let m=0;
    const d=LG[k];
    if(d&&d.groups)Object.values(d.groups).forEach(g=>{m+=(g.total_matched||0)});
    const cntHtml=m>0?'&nbsp;<span style="color:var(--warn);font-size:11px">('+fmtNum(m)+')</span>':'';
    h+='<div class="tab-btn lgtab'+(i===0?' active':'')+'" onclick="switchTab(\'lgtab\',\'lgpane\','+i+')">'
      +esc(SRC_LABEL[k]||k)+cntHtml+'</div>';
  });
  h+='</div>';

  // Build state and panes
  let totalMatches=0;
  goodKs.forEach((k,i)=>{
    _logSt['p'+i]={matches:[],raw:[]};
    const d=LG[k];
    if(d&&d.groups){
      Object.values(d.groups).forEach(g=>{
        (g.heuristic_matches||[]).forEach(m=>{_logSt['p'+i].matches.push(m);totalMatches++});
      });
    } else if(d&&d.entries){
      d.entries.forEach(e=>{_logSt['p'+i].raw.push(typeof e==='string'?e:JSON.stringify(e))});
    }
    h+='<div class="lgpane tab-pane'+(i===0?' active':'')+'" id="lgpane'+i+'">';
    h+=_renderLogSrc(k,d,i);
    h+='</div>';
  });

  el.innerHTML=h;

  const nb=document.getElementById('nb-logs');
  if(nb&&totalMatches){nb.textContent=fmtNum(totalMatches);nb.className='nav-badge warn';nb.style.display=''}
}

function _searchBar(pi,tot){
  return'<div class="search-bar">'
    +'<input class="search-input" id="lsi'+pi+'" type="text" placeholder="Search log lines… (press / to focus)" oninput="filterLogs('+pi+',this.value)">'
    +'<span class="search-count" id="lsc'+pi+'">'+fmtNum(tot)+' entries</span>'
    +'</div>';
}

function _renderLogSrc(key,d,pi){
  if(!d||typeof d!=='object')return'<div class="empty"><span class="empty-icon">📋</span>No data for this source.</div>';

  // Webserver / raw entries array
  if(d.entries){
    const ents=d.entries||[];
    let h='<div class="stats-strip">'
      +'<div class="stat"><div class="stat-label">Log Entries</div><div class="stat-value">'+fmtNum(ents.length)+'</div></div>'
      +'</div>';
    h+=_searchBar(pi,ents.length);
    h+='<div class="card"><div class="card-body" style="padding:12px 16px">'
      +'<div class="log-list" id="lgl'+pi+'">';
    ents.slice(0,300).forEach(e=>{
      const l=typeof e==='string'?e:JSON.stringify(e);
      h+='<div class="log-line">'+esc(l)+'</div>';
    });
    if(ents.length>300)h+='<div class="log-line" style="color:var(--text3);font-style:italic">…'+fmtNum(ents.length-300)+' more entries</div>';
    h+='</div></div></div>';
    return h;
  }

  // Platform / MongoDB — grouped LogGroupResult
  if(d.groups){
    const gs=d.groups,gks=Object.keys(gs);
    let totL=0,totM=0,allM=[],earl=null,late=null;
    gks.forEach(k=>{
      const g=gs[k];
      totL+=g.total_lines_parsed||0;
      totM+=g.total_matched||0;
      (g.heuristic_matches||[]).forEach(m=>allM.push(m));
      if(g.time_range){
        if(g.time_range.earliest&&(!earl||g.time_range.earliest<earl))earl=g.time_range.earliest;
        if(g.time_range.latest&&(!late||g.time_range.latest>late))late=g.time_range.latest;
      }
    });

    const matchCls=totM>50?'bad':totM>10?'warn':'ok';
    let h='<div class="stats-strip">'
      +'<div class="stat"><div class="stat-label">Lines Scanned</div><div class="stat-value">'+fmtNum(totL)+'</div></div>'
      +'<div class="stat"><div class="stat-label">Pattern Matches</div><div class="stat-value '+matchCls+'">'+fmtNum(totM)+'</div></div>'
      +'<div class="stat"><div class="stat-label">Files Parsed</div><div class="stat-value">'+fmtNum(d.file_count||d.files_parsed||gks.length)+'</div></div>';
    if(earl&&late){
      h+='<div class="stat"><div class="stat-label">Date Range</div>'
        +'<div style="font-size:11px;color:var(--text2);font-family:var(--mono);margin-top:4px;line-height:1.55">'
        +esc(earl.slice(0,10))+'<br><span style="color:var(--text3)">→</span> '+esc(late.slice(0,10))+'</div></div>';
    }
    h+='</div>';

    // Pattern matches card
    h+='<div class="card"><div class="card-head">'
      +'<h3>Pattern Matches</h3>'
      +'<span class="sub">Lines containing error/warning keywords</span>'
      +'<div class="ml">'
      +(totM>0?'<span class="badge badge-warn">'+fmtNum(totM)+' match'+(totM!==1?'es':'')+'</span>':'<span class="badge badge-ok"><span class="dot"></span>Clean</span>')
      +'</div></div><div class="card-body">';

    if(!allM.length){
      h+='<div style="display:flex;align-items:center;gap:10px;color:var(--ok);font-size:13px;padding:4px 0">'
        +'<span style="font-size:16px">✓</span><span>No error or warning patterns found in these log files.</span></div>';
    } else {
      h+=_searchBar(pi,allM.length);
      h+='<div class="log-list" id="lgl'+pi+'">';
      allM.slice(0,350).forEach(m=>{
        h+='<div class="log-line">';
        (m.keywords||[]).forEach(kw=>{h+='<span class="kw-tag">'+esc(kw)+'</span>'});
        h+=esc(m.line||'');
        h+='</div>';
      });
      if(allM.length>350)h+='<div class="log-line" style="color:var(--text3);font-style:italic">…'+fmtNum(allM.length-350)+' more — use search to filter</div>';
      h+='</div>';
    }
    h+='</div></div>';

    // Top messages by frequency
    const top=[];
    gks.forEach(k=>{(gs[k].top_messages||[]).forEach(m=>top.push(m))});
    top.sort((a,b)=>(b.count||0)-(a.count||0));
    if(top.length){
      h+='<div class="card"><div class="card-head"><h3>Top Messages by Frequency</h3>'
        +'<span class="sub">Most repeated messages across all log files</span>'
        +'</div><div class="card-body np"><div class="tbl-wrap">'
        +'<table><thead><tr><th style="width:80px">Count</th><th>Message</th></tr></thead><tbody>';
      top.slice(0,30).forEach(m=>{
        h+='<tr>'
          +'<td class="td-mono" style="color:var(--warn);font-weight:700">'+esc(m.count)+'</td>'
          +'<td class="td-mono td-break" style="font-size:11px">'+esc(m.message||'')+'</td>'
          +'</tr>';
      });
      h+='</tbody></table></div></div></div>';
    }

    // Per-file breakdown (if multiple files)
    if(gks.length>1){
      h+='<div class="card"><div class="card-head"><h3>File Breakdown</h3></div>'
        +'<div class="card-body np"><div class="tbl-wrap">'
        +'<table><thead><tr><th>File</th><th>Lines</th><th>Matches</th><th>Date Range</th></tr></thead><tbody>';
      gks.forEach(k=>{
        const g=gs[k],tr=g.time_range;
        const rng=tr?(esc((tr.earliest||'?').slice(0,10))+' → '+esc((tr.latest||'?').slice(0,10))):'—';
        h+='<tr>'
          +'<td class="td-mono td-break" style="font-size:11px;color:var(--text3)">'+esc(k)+'</td>'
          +'<td class="td-num td-dim">'+fmtNum(g.total_lines_parsed)+'</td>'
          +'<td class="td-num" style="color:'+(g.total_matched>0?'var(--warn)':'var(--text3)')+'">'+fmtNum(g.total_matched)+'</td>'
          +'<td class="td-mono" style="font-size:11px;color:var(--text3)">'+rng+'</td>'
          +'</tr>';
      });
      h+='</tbody></table></div></div></div>';
    }

    return h;
  }

  return'<div class="card"><div class="card-body">'+codeBlock(d)+'</div></div>';
}

function filterLogs(pi,q){
  const st=_logSt['p'+pi];if(!st)return;
  const cnt=document.getElementById('lsc'+pi);
  const box=document.getElementById('lgl'+pi);if(!box)return;
  const lo=q.trim().toLowerCase();
  const src=st.matches.length?st.matches:st.raw.map(l=>({line:l,keywords:[]}));
  const hits=lo?src.filter(m=>(m.line||'').toLowerCase().includes(lo)):src;
  if(cnt)cnt.textContent=fmtNum(hits.length)+' entries';
  box.innerHTML='';
  if(!hits.length){
    box.innerHTML='<div class="log-line" style="color:var(--text3)">No matches for "'+esc(lo)+'"</div>';return;
  }
  hits.slice(0,500).forEach(m=>{
    const div=document.createElement('div');div.className='log-line';
    let inner='';
    (m.keywords||[]).forEach(kw=>{inner+='<span class="kw-tag">'+esc(kw)+'</span>'});
    const line=m.line||'';
    if(lo){
      const idx=line.toLowerCase().indexOf(lo);
      if(idx>=0){
        inner+=esc(line.slice(0,idx))+'<mark>'+esc(line.slice(idx,idx+lo.length))+'</mark>'+esc(line.slice(idx+lo.length));
      } else inner+=esc(line);
    } else inner+=esc(line);
    div.innerHTML=inner;box.appendChild(div);
  });
  if(hits.length>500){
    const d=document.createElement('div');d.className='log-line';
    d.style.cssText='color:var(--text3);font-style:italic';
    d.textContent='…'+fmtNum(hits.length-500)+' more results';
    box.appendChild(d);
  }
}

/* ═══════════════════════════════════════════════════════════════
   SYSTEM INFO
   ═══════════════════════════════════════════════════════════════ */
function renderSystem(){
  const el=document.getElementById('sec-system');
  let h='<div class="ph"><div class="ph-title">System Info</div>'
    +'<div class="ph-sub">OS, CPU, memory, disk, and network statistics from the IAP node. Extended tier only.</div></div>';

  if(!SY||!Object.keys(SY).length||SY._tier_error||SY._error){
    el.innerHTML=h+'<div class="card"><div class="card-body"><div class="empty" style="padding:20px 0">'
      +'<span class="empty-icon">🖥</span>'
      +esc((SY&&(SY._tier_error||SY._error))||'No system info — Extended tier required.')
      +'</div></div></div>';
    return;
  }

  const host=SY.host||{},os=SY.os||{},cpu=SY.cpu||{},mem=SY.memory||{};
  const meta=SY.meta||{};

  // ── Host & OS + CPU side by side ──────────────────────────────
  h+='<div class="g2">';

  // Host & OS
  h+='<div class="card"><div class="card-head">'
    +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><rect x="1" y="2" width="14" height="10" rx="2"/><line x1="4" y1="14" x2="12" y2="14"/><line x1="8" y1="12" x2="8" y2="14"/></svg>'
    +'<h3>Host &amp; OS</h3></div><div class="card-body"><div class="kv-list">';
  const collectedAt=meta.ts?new Date(meta.ts*1000).toLocaleString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}):null;
  [
    ['Hostname',     host.hostname],
    ['FQDN',         host.fqdn],
    ['OS',           os.system],
    ['Kernel',       os.release],
    ['Architecture', os.machine],
    ['Platform',     os.platform],
    ['Version',      os.version],
    ['Uname',        os.uname],
    ['Collected At', collectedAt],
  ].filter(([,v])=>v).forEach(([k,v])=>{
    h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(String(v))+'</span></div>';
  });
  h+='</div></div></div>';

  // CPU
  h+='<div class="card"><div class="card-head">'
    +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><rect x="4" y="4" width="8" height="8" rx="1"/><path d="M6 1v3M10 1v3M6 12v3M10 12v3M1 6h3M1 10h3M12 6h3M12 10h3"/></svg>'
    +'<h3>CPU</h3>'
    +(cpu.percent_total!=null?'<span class="badge badge-'+(cpu.percent_total>90?'bad':cpu.percent_total>70?'warn':'ok')+'" style="margin-left:auto">'+Number(cpu.percent_total).toFixed(1)+'% used</span>':'')
    +'</div><div class="card-body"><div class="kv-list">';
  [
    ['Physical Cores', cpu.cores_physical],
    ['Logical Cores',  cpu.cores_logical],
  ].filter(([,v])=>v!=null).forEach(([k,v])=>{
    h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(String(v))+'</span></div>';
  });
  if(cpu.loadavg&&cpu.loadavg.length){
    h+='<div class="kv"><span class="kk">Load Avg (1/5/15m)</span><span class="kv-val">'+cpu.loadavg.map(n=>Number(n).toFixed(2)).join(' / ')+'</span></div>';
  }
  h+='</div>';
  if(cpu.percent_total!=null){
    const pct=Number(cpu.percent_total);
    h+='<hr>'+progBar(pct,pct.toFixed(1)+'% CPU');
  }
  // Per-core breakdown
  if(Array.isArray(cpu.percent_per_cpu)&&cpu.percent_per_cpu.length){
    h+='<div style="margin-top:12px"><div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Per-Core Usage</div>'
      +'<div class="cpu-grid">';
    cpu.percent_per_cpu.forEach((pct,i)=>{
      const p=Number(pct)||0;
      const fill=p>90?'var(--bad)':p>70?'var(--warn)':'var(--acc)';
      h+='<div class="cpu-core">'
        +'<div class="cpu-core-bar"><div class="cpu-core-fill" style="height:'+Math.max(2,p)+'%;background:'+fill+'"></div></div>'
        +'<div class="cpu-core-label">C'+i+'</div>'
        +'</div>';
    });
    h+='</div></div>';
  }
  h+='</div></div>';
  h+='</div>'; // close g2

  // ── Memory ─────────────────────────────────────────────────────
  const virt=mem.virtual;
  const swap=mem.swap;
  const meminfo=mem.meminfo; // remote SSH format

  if(virt&&virt.total){
    const pct=virt.percent!=null?Number(virt.percent):null;
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><rect x="1" y="4" width="14" height="8" rx="1.5"/><line x1="4" y1="7" x2="4" y2="9"/><line x1="7" y1="7" x2="7" y2="9"/><line x1="10" y1="7" x2="10" y2="9"/></svg>'
      +'<h3>Memory</h3>'
      +(pct!=null?'<span class="badge badge-'+(pct>90?'bad':pct>75?'warn':'ok')+'" style="margin-left:auto">'+pct.toFixed(1)+'% used</span>':'')
      +'</div><div class="card-body"><div class="g2">';

    // Virtual memory
    h+='<div><div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">RAM</div><div class="kv-list">';
    [
      ['Total',     fmtBytes(virt.total)],
      ['Used',      fmtBytes(virt.used)],
      ['Free',      fmtBytes(virt.free)],
      ['Available', fmtBytes(virt.available)],
      ['Buffers',   fmtBytes(virt.buffers)],
      ['Cached',    fmtBytes(virt.cached)],
      ['Shared',    fmtBytes(virt.shared)],
    ].filter(([,v])=>v&&v!=='0 B').forEach(([k,v])=>{
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
    });
    h+='</div></div>';

    // Swap
    h+='<div><div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Swap</div><div class="kv-list">';
    if(swap&&swap.total){
      const spct=swap.percent!=null?Number(swap.percent):null;
      [
        ['Total', fmtBytes(swap.total)],
        ['Used',  fmtBytes(swap.used)],
        ['Free',  fmtBytes(swap.free)],
        ['Usage', spct!=null?spct.toFixed(1)+'%':null],
      ].filter(([,v])=>v).forEach(([k,v])=>{
        h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
      });
    } else {
      h+='<div class="kv"><span class="kv-val" style="color:var(--text3)">No swap configured</span></div>';
    }
    h+='</div></div>';
    h+='</div>'; // close g2

    if(pct!=null){
      h+='<hr>'+progBar(pct,fmtBytes(virt.used)+' used',fmtBytes(virt.available)+' free');
    }
    if(swap&&swap.total&&swap.percent!=null){
      h+=progBar(Number(swap.percent),fmtBytes(swap.used)+' swap used',fmtBytes(swap.free)+' free');
    }
    h+='</div></div>';
  } else if(meminfo&&Object.keys(meminfo).length){
    // Remote SSH format — /proc/meminfo dict
    const mi=k=>meminfo[k]?meminfo[k].replace(/\s+/g,' '):null;
    const miKB=k=>{const v=(meminfo[k]||'').split(/\s+/)[0];return v&&/^\d+$/.test(v)?fmtBytes(parseInt(v)*1024):mi(k)};
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><rect x="1" y="4" width="14" height="8" rx="1.5"/><line x1="4" y1="7" x2="4" y2="9"/><line x1="7" y1="7" x2="7" y2="9"/><line x1="10" y1="7" x2="10" y2="9"/></svg>'
      +'<h3>Memory</h3></div><div class="card-body"><div class="g2">';
    h+='<div><div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">RAM</div><div class="kv-list">';
    [
      ['Total',     miKB('MemTotal')],
      ['Available', miKB('MemAvailable')],
      ['Free',      miKB('MemFree')],
      ['Buffers',   miKB('Buffers')],
      ['Cached',    miKB('Cached')],
      ['Active',    miKB('Active')],
      ['Inactive',  miKB('Inactive')],
      ['Dirty',     miKB('Dirty')],
      ['Slab',      miKB('Slab')],
    ].filter(([,v])=>v).forEach(([k,v])=>{
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
    });
    h+='</div></div>';
    h+='<div><div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Swap</div><div class="kv-list">';
    [
      ['Total',  miKB('SwapTotal')],
      ['Free',   miKB('SwapFree')],
      ['Cached', miKB('SwapCached')],
    ].filter(([,v])=>v).forEach(([k,v])=>{
      h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
    });
    h+='</div></div>';
    h+='</div></div></div>';
  }

  // ── Disk Usage ─────────────────────────────────────────────────
  const diskEntries=SY.disks?Object.entries(SY.disks).filter(([k])=>k!=='_io_counters'):[];
  const ioCounters=SY.disks&&SY.disks._io_counters;
  if(diskEntries.length||ioCounters){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><ellipse cx="8" cy="5" rx="6" ry="2"/><path d="M2 5v6c0 1.1 2.69 2 6 2s6-.9 6-2V5"/><path d="M2 8c0 1.1 2.69 2 6 2s6-.9 6-2"/></svg>'
      +'<h3>Disk</h3></div><div class="card-body">';

    if(diskEntries.length){
      h+='<div class="tbl-wrap" style="margin-bottom:16px"><table><thead><tr>'
        +'<th>Mount</th><th>Total</th><th>Used</th><th>Free</th><th style="width:160px">Usage</th>'
        +'</tr></thead><tbody>';
      diskEntries.forEach(([mnt,d])=>{
        if(!d||typeof d!=='object')return;
        const pct=d.percent!=null?Number(d.percent):null;
        const barCls=pct!=null?(pct>90?'bad':pct>75?'warn':'ok'):'ok';
        h+='<tr>'
          +'<td class="td-mono" style="font-weight:600">'+esc(mnt)+'</td>'
          +'<td class="td-mono td-dim">'+fmtBytes(d.total)+'</td>'
          +'<td class="td-mono">'+fmtBytes(d.used)+'</td>'
          +'<td class="td-mono td-dim">'+fmtBytes(d.free)+'</td>'
          +'<td><div style="display:flex;align-items:center;gap:8px">'
          +(pct!=null?'<div class="prog-track" style="flex:1;height:6px"><div class="prog-fill prog-'+barCls+'" style="width:'+Math.min(pct,100)+'%"></div></div>'
            +'<span style="font-size:11px;color:var(--text2);font-family:var(--mono);white-space:nowrap">'+pct.toFixed(1)+'%</span>':'—')
          +'</div></td>'
          +'</tr>';
      });
      h+='</tbody></table></div>';
    }

    if(ioCounters&&typeof ioCounters==='object'){
      h+='<div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">I/O Counters</div>'
        +'<div class="g2"><div class="kv-list">';
      [
        ['Reads',       fmtNum(ioCounters.read_count)],
        ['Read Bytes',  fmtBytes(ioCounters.read_bytes)],
        ['Read Time',   ioCounters.read_time!=null?fmtNum(ioCounters.read_time)+' ms':null],
      ].filter(([,v])=>v!=null).forEach(([k,v])=>{
        h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
      });
      h+='</div><div class="kv-list">';
      [
        ['Writes',       fmtNum(ioCounters.write_count)],
        ['Write Bytes',  fmtBytes(ioCounters.write_bytes)],
        ['Write Time',   ioCounters.write_time!=null?fmtNum(ioCounters.write_time)+' ms':null],
      ].filter(([,v])=>v!=null).forEach(([k,v])=>{
        h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
      });
      h+='</div></div>';
    }
    h+='</div></div>';
  }

  // ── Network ─────────────────────────────────────────────────────
  const netAddrs=SY.network_addrs;
  const netIO=SY.network_io;
  if((netAddrs&&Object.keys(netAddrs).length)||netIO){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><path d="M1 8h14M8 1l5 7-5 7M3 3l-2 5 2 5"/></svg>'
      +'<h3>Network</h3></div><div class="card-body">';

    if(netAddrs&&Object.keys(netAddrs).length){
      h+='<div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">Interfaces</div>'
        +'<div class="tbl-wrap" style="margin-bottom:16px"><table><thead><tr>'
        +'<th>Interface</th><th>Family</th><th>Address</th><th>Netmask</th><th>Broadcast</th>'
        +'</tr></thead><tbody>';
      Object.entries(netAddrs).forEach(([iface,addrs])=>{
        if(!Array.isArray(addrs))return;
        addrs.forEach((a,idx)=>{
          if(!a||!a.address||a.address==='None')return;
          const fam=String(a.family||'');
          const famLabel=fam.includes('2')||fam==='AF_INET'||fam==='AddressFamily.AF_INET'?'IPv4'
            :fam.includes('10')||fam==='AF_INET6'||fam==='AddressFamily.AF_INET6'?'IPv6'
            :fam.includes('17')||fam==='AF_PACKET'||fam==='AddressFamily.AF_PACKET'?'MAC':'Other';
          h+='<tr>'
            +(idx===0?'<td class="td-mono" style="font-weight:600" rowspan="'+addrs.filter(x=>x&&x.address&&x.address!=="None").length+'">'+esc(iface)+'</td>':'')
            +'<td><span class="net-family">'+esc(famLabel)+'</span></td>'
            +'<td class="td-mono">'+esc(a.address||'—')+'</td>'
            +'<td class="td-mono td-dim">'+esc(a.netmask||'—')+'</td>'
            +'<td class="td-mono td-dim">'+esc(a.broadcast||'—')+'</td>'
            +'</tr>';
        });
      });
      h+='</tbody></table></div>';
    }

    if(netIO&&typeof netIO==='object'){
      h+='<div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">I/O Counters (Totals)</div>'
        +'<div class="g2"><div class="kv-list">';
      [
        ['Bytes Sent',      fmtBytes(netIO.bytes_sent)],
        ['Packets Sent',    fmtNum(netIO.packets_sent)],
        ['Errors Out',      fmtNum(netIO.errout)],
        ['Dropped Out',     fmtNum(netIO.dropout)],
      ].filter(([,v])=>v!=null).forEach(([k,v])=>{
        h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
      });
      h+='</div><div class="kv-list">';
      [
        ['Bytes Received',  fmtBytes(netIO.bytes_recv)],
        ['Packets Received',fmtNum(netIO.packets_recv)],
        ['Errors In',       fmtNum(netIO.errin)],
        ['Dropped In',      fmtNum(netIO.dropin)],
      ].filter(([,v])=>v!=null).forEach(([k,v])=>{
        h+='<div class="kv"><span class="kk">'+esc(k)+'</span><span class="kv-val">'+esc(v)+'</span></div>';
      });
      h+='</div></div>';
    }
    h+='</div></div>';
  }

  // ── Top Processes ───────────────────────────────────────────────
  if(Array.isArray(SY.top_processes_rss)&&SY.top_processes_rss.length){
    h+='<div class="card"><div class="card-head">'
      +'<svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.7"><rect x="1" y="2" width="14" height="3" rx="1"/><rect x="1" y="7" width="10" height="3" rx="1"/><rect x="1" y="12" width="7" height="3" rx="1"/></svg>'
      +'<h3>Top Processes by Memory</h3>'
      +'<span class="badge badge-dim" style="margin-left:auto">'+SY.top_processes_rss.length+'</span>'
      +'</div><div class="card-body np"><div class="tbl-wrap"><table><thead><tr>'
      +'<th>PID</th><th>Process Name</th><th>User</th><th style="text-align:right">RSS</th>'
      +'</tr></thead><tbody>';
    SY.top_processes_rss.forEach(p=>{
      h+='<tr>'
        +'<td class="td-mono td-dim">'+esc(String(p.pid||'—'))+'</td>'
        +'<td class="td-mono" style="font-weight:500">'+esc(p.name||'?')+'</td>'
        +'<td class="td-dim">'+esc(p.username||'—')+'</td>'
        +'<td class="td-mono" style="text-align:right">'+fmtBytes(p.rss_bytes)+'</td>'
        +'</tr>';
    });
    h+='</tbody></table></div></div></div>';
  }

  el.innerHTML=h;
}

/* ═══════════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════════ */
const tick=M.ticket,tier=M.mode,env=M.environment,gen=M.generated_at;
const verEl=document.getElementById('tb-ver');
if(verEl&&M.atlas_version&&M.atlas_version!=='unknown')verEl.textContent='v'+M.atlas_version;
if(tick)document.getElementById('tb-ticket').innerHTML='<span class="chip chip-blue">'+esc(tick)+'</span>';
if(tier)document.getElementById('tb-tier').innerHTML='<span class="chip chip-dim">'+esc(tier.charAt(0).toUpperCase()+tier.slice(1))+'</span>';
if(env)document.getElementById('tb-env').innerHTML='<span class="chip chip-dim">'+esc(env)+'</span>';
if(gen)document.getElementById('tb-date').textContent=fmtDate(gen);

renderOverview();
renderHealth();
renderConfig();
renderLogs();
renderSystem();
// keyboard shortcut for / to focus search
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&document.activeElement.tagName!=='INPUT'&&document.activeElement.tagName!=='TEXTAREA'){
    e.preventDefault();
    const inp=document.querySelector('.sec.active .search-input');
    if(inp)inp.focus();
  }
});
"""



def _build_zip(
    platform_health: dict,
    logs: dict,
    system: dict,
    raw_logs: dict,
    config_redacted: dict,
    manifest: dict,
    folder: str = "atlas-support-bundle",
    exports: dict[str, bytes] | None = None,
) -> bytes:
    """Assemble and return the support bundle as raw ZIP bytes.

    All entries are placed under a top-level ``folder/`` directory so that
    ``unzip <bundle>.zip`` extracts into a single self-contained folder.

    ``exports`` is an optional mapping of ZIP-relative path -> raw bytes for
    Platform artifacts (workflows/JSTs/forms/projects exported via the WebUI).
    The CLI never passes it, so the basic support bundle is unchanged.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        def _write(path: str, obj: object) -> None:
            zf.writestr(f"{folder}/{path}", json.dumps(obj, indent=2, default=str, ensure_ascii=False))

        _write("manifest.json",         manifest)
        _write("atlas_config.json",     config_redacted)

        for name, data in platform_health.items():
            _write(f"platform/{name}.json", data)

        if logs:
            for name, data in logs.items():
                _write(f"logs/{name}.json", data)

        if system:
            _write("system/system_info.json", system)

        if raw_logs:
            for subpath, content in raw_logs.items():
                zf.writestr(f"{folder}/raw_logs/{subpath}", content)

        if exports:
            for subpath, content in exports.items():
                zf.writestr(f"{folder}/{subpath}", content)

        viewer_files = _build_html_viewer(platform_health, logs, system, config_redacted, manifest)
        for sub_path, content in viewer_files.items():
            zf.writestr(f"{folder}/{sub_path}", content)

    return buf.getvalue()


@registry.register("support-bundle", description="Collect a diagnostic support bundle ZIP")
def handle_support_bundle(args: Namespace) -> int:
    """Collect Platform health + logs and pack into a support bundle ZIP."""
    import questionary
    from platform_atlas.core.init_setup import get_qstyle

    config = ctx().config
    # Strict: only Extended collects infrastructure logs over SSH. SaaS has
    # no Platform/Mongo log sources, so it takes the config-snapshot path
    # like Standard.
    is_extended = ctx().is_extended
    log_days_arg = getattr(args, "log_days", None)
    output_path = getattr(args, "output", None)
    yes = getattr(args, "yes", False)

    env_name = getattr(config, "active_environment", None) or "—"
    mode_label = {
        "standard": "Standard — Platform health · Atlas config",
        "saas": "SaaS — Gateway audit · Atlas config snapshot",
    }.get(ctx().tier, "Extended — Platform health · logs · system info")

    # ── Intro panel ───────────────────────────────────────────────
    console.print()
    intro = Text()
    intro.append("A support bundle is a ZIP file containing diagnostic data\n", style=f"bold {theme.text_primary}")
    intro.append("from your Platform deployment — health endpoint responses,\n", style=theme.text_secondary)
    intro.append("logs, system info, and a redacted Atlas config snapshot.\n\n", style=theme.text_secondary)
    intro.append("Attach it to your Itential support ticket so the team can\n", style=theme.text_dim)
    intro.append("triage the issue without needing direct access.", style=theme.text_dim)
    console.print(Panel(
        intro,
        title=f"[bold {theme.primary_glow}] Platform Atlas Support Bundle [/]",
        border_style=theme.border_primary,
        padding=(1, 3),
        box=box.ROUNDED,
    ))
    console.print()

    # ── Credential backend pre-flight ─────────────────────────────
    # A bundle is only useful if Atlas can retrieve credentials: the Platform
    # OAuth secret drives every health endpoint (both tiers), and Extended log
    # collection needs SSH creds too. If the active backend can't produce them
    # — Vault unreachable / auth failed / secret missing, or a locked or empty
    # keyring — abort now instead of writing an empty ZIP. (Each collector
    # otherwise swallows the failure silently and the bundle still gets made.)
    # NOTE: the support bundle is curated — it writes a redacted config snapshot,
    # logs, and system info, and NEVER reads ~/.atlas files. The encrypted local
    # credential store (~/.atlas/credentials.enc) and its salt (~/.atlas/.keysalt)
    # are therefore never collected; keep it that way (no directory globbing here).
    cred_backend = (getattr(config, "credential_backend", "keyring") or "keyring").lower()
    if cred_backend == "vault":
        backend_label = "HashiCorp Vault"
    else:
        from platform_atlas.core.credentials import active_secret_store
        backend_label = "encrypted local file" if active_secret_store().is_file else "OS keyring"
    console.print(f"  [{theme.primary}]›[/{theme.primary}] Verifying {backend_label} credentials…")
    try:
        _assert_credentials_available()
    except Exception as exc:
        details = getattr(exc, "details", {}) or {}
        err = Text()
        err.append("No bundle was created.\n\n", style=f"bold {theme.error}")
        err.append(
            "Atlas could not retrieve the credentials needed to collect\n"
            "diagnostic data, so the support bundle was aborted before any\n"
            "data was gathered.\n\n",
            style=theme.text_secondary,
        )
        err.append("Backend:  ", style=theme.text_dim)
        err.append(f"{backend_label}\n", style=theme.text_secondary)
        if details.get("url"):
            err.append("Vault:    ", style=theme.text_dim)
            err.append(f"{details['url']}\n", style=theme.text_secondary)
        err.append("Reason:   ", style=theme.text_dim)
        err.append(f"{exc}\n", style=theme.text_primary)
        err.append("\nHow to fix\n", style=f"bold {theme.text_primary}")
        if details.get("fix"):
            err.append(f"  • {details['fix']}\n", style=theme.text_secondary)
        err.append("  • Run ", style=theme.text_secondary)
        err.append("platform-atlas config doctor", style=f"bold {theme.accent}")
        err.append(" to diagnose and repair credential access.", style=theme.text_secondary)
        console.print()
        console.print(Panel(
            err,
            title=f"[bold {theme.error}] Cannot Create Support Bundle [/]",
            border_style=theme.error,
            padding=(1, 3),
            box=box.ROUNDED,
        ))
        console.print()
        return 1
    console.print(f"  [{theme.success}]✓[/{theme.success}]  Credentials available ({backend_label})")

    # ── Intake questions ──────────────────────────────────────────
    ticket_number: str = ""
    issue_description: str = ""

    if not yes:
        def _validate_ticket(val: str) -> bool | str:
            import re
            v = val.strip().upper()
            if not v or v == "ISD-":
                return "Please enter a ticket number (e.g. ISD-1234)"
            if not re.fullmatch(r"ISD-\d{4,5}", v):
                return "Must be ISD- followed by 4 or 5 digits (e.g. ISD-1234 or ISD-12345)"
            return True

        raw_ticket = questionary.text(
            "Support ticket number:",
            default="ISD-",
            validate=_validate_ticket,
            instruction="(e.g. ISD-1234)",
            style=get_qstyle(),
        ).ask()
        if raw_ticket is None:
            raise KeyboardInterrupt
        ticket_number = raw_ticket.strip().upper()

        raw_desc = questionary.text(
            "Brief description of the issue:",
            default="",
            instruction="(optional — press Enter to skip)",
            style=get_qstyle(),
        ).ask()
        if raw_desc is None:
            raise KeyboardInterrupt
        issue_description = raw_desc.strip()

    # ── Log window (Extended only; flag wins, otherwise prompt) ───
    # Only Extended collects SSH logs, so the log window is meaningless in
    # Standard. An explicit --log-days wins (clamped to range); otherwise we
    # prompt, falling back to the default under --yes.
    default_days, min_days, max_days = 7, 1, 30
    if not is_extended:
        log_days = default_days  # Standard collects no logs; value is unused
        if log_days_arg is not None:
            console.print(
                f"  [{theme.text_dim}]Standard tier collects no logs; "
                f"--log-days ignored.[/{theme.text_dim}]"
            )
    elif log_days_arg is not None:
        log_days = max(min_days, min(max_days, log_days_arg))
        if log_days != log_days_arg:
            console.print(
                f"  [{theme.text_dim}]--log-days {log_days_arg} clamped to "
                f"{log_days} (valid range {min_days}-{max_days}).[/{theme.text_dim}]"
            )
    elif yes:
        log_days = default_days
    else:
        def _validate_days(val: str) -> bool | str:
            s = (val or "").strip()
            if not s:
                return True  # blank → use the default
            if not s.isdigit():
                return "Enter a whole number of days"
            if int(s) < min_days or int(s) > max_days:
                return f"Must be between {min_days} and {max_days}"
            return True

        raw_days = questionary.text(
            "Days of logs to collect:",
            default=str(default_days),
            validate=_validate_days,
            instruction=f"({min_days}-{max_days}, default {default_days})",
            style=get_qstyle(),
        ).ask()
        if raw_days is None:
            raise KeyboardInterrupt
        raw_days = raw_days.strip()
        log_days = int(raw_days) if raw_days else default_days

    # ── Resolve output path (now that we have the ticket number) ──
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ticket_slug = ticket_number.replace("/", "-").replace(" ", "-") if ticket_number else ""
    base_name = f"atlas-support-bundle-{ticket_slug}-{timestamp}.zip" if ticket_slug else f"atlas-support-bundle-{timestamp}.zip"
    if output_path:
        dest = Path(output_path)
        if dest.is_dir():
            dest = dest / base_name
    else:
        dest = Path.cwd() / base_name

    # ── Pre-collection summary ────────────────────────────────────
    connections = _describe_connections(config, ctx().tier)
    summary_table = Table(show_header=False, box=None, padding=(0, 1), show_edge=False)
    summary_table.add_column(style=f"dim {theme.text_dim}", no_wrap=True)
    summary_table.add_column(style=theme.text_secondary)
    summary_table.add_row("Ticket",      ticket_number or "—")
    summary_table.add_row("Environment", env_name)
    summary_table.add_row("Mode",        mode_label)
    for label, value in connections:
        summary_table.add_row(label, value)
    if is_extended:
        summary_table.add_row("Log window",  f"last {log_days} days")
    summary_table.add_row("Output",      str(dest))
    if issue_description:
        summary_table.add_row("Description", issue_description)

    console.print(Panel(
        summary_table,
        title=f"[bold {theme.accent}] Collection Plan [/]",
        border_style=theme.border_dim,
        padding=(1, 2),
        box=box.ROUNDED,
    ))
    console.print()

    if not yes:
        proceed = questionary.confirm("Collect and bundle now?", default=True, style=get_qstyle()).ask()
        if proceed is None:
            raise KeyboardInterrupt
        if not proceed:
            console.print(f"  [{theme.text_dim}]Cancelled.[/{theme.text_dim}]\n")
            return 0

    errors: list[str] = []
    collected: list[str] = []

    # ── Platform health endpoints ─────────────────────────────────
    console.print(f"\n  [{theme.primary}]›[/{theme.primary}] Collecting Platform health endpoints…")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            platform_health = _collect_platform_health(log_days)
        ok = [k for k, v in platform_health.items() if not (isinstance(v, dict) and v.get("status") == "failed")]
        collected.append(f"Platform health ({len(ok)}/{len(platform_health)} endpoints)")
        for name, v in platform_health.items():
            if isinstance(v, dict) and v.get("status") == "failed":
                errors.append(f"Platform/{name}: {v.get('error', 'failed')}")
        console.print(f"  [{theme.success}]✓[/{theme.success}]  {len(ok)}/{len(platform_health)} health endpoints collected")
    except Exception as exc:
        platform_health = {}
        errors.append(f"Platform health collection failed: {exc}")
        console.print(f"  [{theme.warning}]⚠[/{theme.warning}]  Platform health collection failed — {exc}")

    # ── Logs + system info (Extended only) ───────────────────────
    logs: dict = {}
    system: dict = {}
    raw_logs: dict = {}
    if is_extended:
        console.print(f"\n  [{theme.primary}]›[/{theme.primary}] Collecting SSH logs and system info…")

        def _cli_progress(msg: str) -> None:
            console.print(f"  [{theme.text_dim}]{msg}[/{theme.text_dim}]")

        try:
            logs, system, raw_logs = _collect_logs_and_system(log_days, progress_cb=_cli_progress)
            if logs:
                ok_logs = [k for k, v in logs.items() if not (isinstance(v, dict) and v.get("status") == "failed")]
                collected.append(f"Logs ({len(ok_logs)}/{len(logs)} sources)")
                console.print(f"  [{theme.success}]✓[/{theme.success}]  {len(ok_logs)}/{len(logs)} log sources collected")
            if system and "_error" not in system and "_tier_error" not in system:
                collected.append("System info")
                console.print(f"  [{theme.success}]✓[/{theme.success}]  System info collected")
            if raw_logs:
                collected.append(f"Raw logs ({len(raw_logs)} file(s))")
                console.print(f"  [{theme.success}]✓[/{theme.success}]  {len(raw_logs)} raw log file(s) transferred to raw_logs/")
        except Exception as exc:
            errors.append(f"Log/system collection failed: {exc}")
            console.print(f"  [{theme.warning}]⚠[/{theme.warning}]  Log/system collection failed — {exc}")

    # ── Atlas config (redacted) ───────────────────────────────────
    console.print(f"\n  [{theme.primary}]›[/{theme.primary}] Bundling Atlas config (redacted)…")
    try:
        config_redacted = _redact_config(config)
        collected.append("Atlas config (redacted)")
        console.print(f"  [{theme.success}]✓[/{theme.success}]  Config snapshot included (credentials redacted)")
    except Exception as exc:
        config_redacted = {"error": str(exc)}
        errors.append(f"Config redaction failed: {exc}")

    # ── Build & write ZIP ─────────────────────────────────────────
    console.print(f"\n  [{theme.primary}]›[/{theme.primary}] Writing bundle…")
    manifest = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "ticket":          ticket_number or None,
        "description":     issue_description or None,
        "organization":    getattr(config, "organization_name", None),
        "environment":     env_name,
        "log_window_days": log_days if is_extended else None,
        "mode":            ctx().tier,
        "atlas_version":   _get_atlas_version(),
        "platform_url":    _get_platform_url(config),
        "ruleset_id":      getattr(config, "ruleset_id", None),
        "ruleset_profile": getattr(config, "ruleset_profile", None),
        "python_version":  sys.version.split()[0],
        "collected":       collected,
        "errors":          errors,
    }

    try:
        bundle_bytes = _build_zip(platform_health, logs, system, raw_logs, config_redacted, manifest, folder=dest.stem)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bundle_bytes)
    except Exception as exc:
        console.print(f"\n  [{theme.error}]✘[/{theme.error}] Failed to write bundle: {exc}\n")
        return 1

    # ── Success panel ─────────────────────────────────────────────
    size_kb = len(bundle_bytes) / 1024
    console.print()

    result_table = Table(show_header=False, box=None, padding=(0, 1), show_edge=False)
    result_table.add_column(style=f"dim {theme.text_dim}", no_wrap=True)
    result_table.add_column(style=theme.text_secondary)
    result_table.add_row("File", str(dest))
    result_table.add_row("Size", f"{size_kb:.1f} KB")
    for item in collected:
        result_table.add_row(f"[{theme.success}]✓[/{theme.success}]", item)
    if errors:
        result_table.add_row("", "")
        result_table.add_row(f"[{theme.warning}]⚠[/{theme.warning}]", f"{len(errors)} collection error(s) logged in manifest.json")

    console.print(Panel(
        result_table,
        title=f"[bold {theme.success}] Bundle Ready [/]",
        border_style=theme.success,
        padding=(1, 2),
        box=box.ROUNDED,
    ))

    # ── Next steps ────────────────────────────────────────────────
    steps = Text()
    steps.append("1. ", style=f"bold {theme.primary}")
    steps.append("Open your support ticket", style=theme.text_secondary)
    if ticket_number:
        steps.append(f" ({ticket_number})", style=theme.text_dim)
    steps.append(" in the Itential support portal.\n", style=theme.text_secondary)
    steps.append("2. ", style=f"bold {theme.primary}")
    steps.append("Attach ", style=theme.text_secondary)
    steps.append(dest.name, style=f"bold {theme.accent}")
    steps.append(" to the ticket.\n", style=theme.text_secondary)
    steps.append("3. ", style=f"bold {theme.primary}")
    steps.append("Add any additional context or screenshots that help describe the issue.", style=theme.text_secondary)

    console.print(Panel(
        steps,
        title=f"[bold {theme.primary_glow}] Next Steps [/]",
        border_style=theme.border_dim,
        padding=(1, 3),
        box=box.ROUNDED,
    ))
    console.print()
    return 0
