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
        try:
            _ext_targets = config.targets or ()
        except Exception:
            _ext_targets = ()
        for t in _ext_targets:
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
            try:
                _saas_targets = config.targets or ()
            except Exception:
                _saas_targets = ()
            for t in _saas_targets:
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
    from platform_atlas.reporting.assets.fonts import get_font_css as _get_font_css  # pylint: disable=import-outside-toplevel
    fonts_css = _get_font_css()
    data = json.dumps(
        {"manifest": manifest, "config": config_redacted,
         "platform": platform_health, "logs": logs, "system": system},
        default=str, ensure_ascii=False, separators=(",", ":"),
    )
    # Prevent </script> in JSON data from closing the script tag prematurely.
    data = data.replace("</", "<\\/")
    ticket = manifest.get("ticket") or "Support Bundle"
    index = _VIEWER_HTML.replace("__BUNDLE_DATA__", data).replace("__TITLE__", ticket).replace("__FONTS__", fonts_css)
    return {
        "index.html": index,
        "assets/style.css": _VIEWER_CSS,
        "assets/app.js": _VIEWER_JS,
        "assets/anime.min.js": _ANIME_JS,
    }


_ANIME_JS = r"""/*! anime.js v3.2.1 | (c) 2020 Julian Garnier | MIT */
!function(n,e){"object"==typeof exports&&"undefined"!=typeof module?module.exports=e():"function"==typeof define&&define.amd?define(e):n.anime=e()}(this,function(){"use strict";var n={update:null,begin:null,loopBegin:null,changeBegin:null,change:null,changeComplete:null,loopComplete:null,complete:null,loop:1,direction:"normal",autoplay:!0,timelineOffset:0},e={duration:1e3,delay:0,endDelay:0,easing:"easeOutElastic(1, .5)",round:0},t=["translateX","translateY","translateZ","rotate","rotateX","rotateY","rotateZ","scale","scaleX","scaleY","scaleZ","skew","skewX","skewY","perspective","matrix","matrix3d"],r={CSS:{},springs:{}};function a(n,e,t){return Math.min(Math.max(n,e),t)}function o(n,e){return n.indexOf(e)>-1}function u(n,e){return n.apply(null,e)}var i={arr:function(n){return Array.isArray(n)},obj:function(n){return o(Object.prototype.toString.call(n),"Object")},pth:function(n){return i.obj(n)&&n.hasOwnProperty("totalLength")},svg:function(n){return n instanceof SVGElement},inp:function(n){return n instanceof HTMLInputElement},dom:function(n){return n.nodeType||i.svg(n)},str:function(n){return"string"==typeof n},fnc:function(n){return"function"==typeof n},und:function(n){return void 0===n},nil:function(n){return i.und(n)||null===n},hex:function(n){return/(^#[0-9A-F]{6}$)|(^#[0-9A-F]{3}$)/i.test(n)},rgb:function(n){return/^rgb/.test(n)},hsl:function(n){return/^hsl/.test(n)},col:function(n){return i.hex(n)||i.rgb(n)||i.hsl(n)},key:function(t){return!n.hasOwnProperty(t)&&!e.hasOwnProperty(t)&&"targets"!==t&&"keyframes"!==t}};function c(n){var e=/\(([^)]+)\)/.exec(n);return e?e[1].split(",").map(function(n){return parseFloat(n)}):[]}function s(n,e){var t=c(n),o=a(i.und(t[0])?1:t[0],.1,100),u=a(i.und(t[1])?100:t[1],.1,100),s=a(i.und(t[2])?10:t[2],.1,100),f=a(i.und(t[3])?0:t[3],.1,100),l=Math.sqrt(u/o),d=s/(2*Math.sqrt(u*o)),p=d<1?l*Math.sqrt(1-d*d):0,v=1,h=d<1?(d*l-f)/p:-f+l;function g(n){var t=e?e*n/1e3:n;return t=d<1?Math.exp(-t*d*l)*(v*Math.cos(p*t)+h*Math.sin(p*t)):(v+h*t)*Math.exp(-t*l),0===n||1===n?n:1-t}return e?g:function(){var e=r.springs[n];if(e)return e;for(var t=0,a=0;;)if(1===g(t+=1/6)){if(++a>=16)break}else a=0;var o=t*(1/6)*1e3;return r.springs[n]=o,o}}function f(n){return void 0===n&&(n=10),function(e){return Math.ceil(a(e,1e-6,1)*n)*(1/n)}}var l,d,p=function(){var n=11,e=1/(n-1);function t(n,e){return 1-3*e+3*n}function r(n,e){return 3*e-6*n}function a(n){return 3*n}function o(n,e,o){return((t(e,o)*n+r(e,o))*n+a(e))*n}function u(n,e,o){return 3*t(e,o)*n*n+2*r(e,o)*n+a(e)}return function(t,r,a,i){if(0<=t&&t<=1&&0<=a&&a<=1){var c=new Float32Array(n);if(t!==r||a!==i)for(var s=0;s<n;++s)c[s]=o(s*e,t,a);return function(n){return t===r&&a===i?n:0===n||1===n?n:o(f(n),r,i)}}function f(r){for(var i=0,s=1,f=n-1;s!==f&&c[s]<=r;++s)i+=e;var l=i+(r-c[--s])/(c[s+1]-c[s])*e,d=u(l,t,a);return d>=.001?function(n,e,t,r){for(var a=0;a<4;++a){var i=u(e,t,r);if(0===i)return e;e-=(o(e,t,r)-n)/i}return e}(r,l,t,a):0===d?l:function(n,e,t,r,a){for(var u,i,c=0;(u=o(i=e+(t-e)/2,r,a)-n)>0?t=i:e=i,Math.abs(u)>1e-7&&++c<10;);return i}(r,i,i+e,t,a)}}}(),v=(l={linear:function(){return function(n){return n}}},d={Sine:function(){return function(n){return 1-Math.cos(n*Math.PI/2)}},Circ:function(){return function(n){return 1-Math.sqrt(1-n*n)}},Back:function(){return function(n){return n*n*(3*n-2)}},Bounce:function(){return function(n){for(var e,t=4;n<((e=Math.pow(2,--t))-1)/11;);return 1/Math.pow(4,3-t)-7.5625*Math.pow((3*e-2)/22-n,2)}},Elastic:function(n,e){void 0===n&&(n=1),void 0===e&&(e=.5);var t=a(n,1,10),r=a(e,.1,2);return function(n){return 0===n||1===n?n:-t*Math.pow(2,10*(n-1))*Math.sin((n-1-r/(2*Math.PI)*Math.asin(1/t))*(2*Math.PI)/r)}}},["Quad","Cubic","Quart","Quint","Expo"].forEach(function(n,e){d[n]=function(){return function(n){return Math.pow(n,e+2)}}}),Object.keys(d).forEach(function(n){var e=d[n];l["easeIn"+n]=e,l["easeOut"+n]=function(n,t){return function(r){return 1-e(n,t)(1-r)}},l["easeInOut"+n]=function(n,t){return function(r){return r<.5?e(n,t)(2*r)/2:1-e(n,t)(-2*r+2)/2}},l["easeOutIn"+n]=function(n,t){return function(r){return r<.5?(1-e(n,t)(1-2*r))/2:(e(n,t)(2*r-1)+1)/2}}}),l);function h(n,e){if(i.fnc(n))return n;var t=n.split("(")[0],r=v[t],a=c(n);switch(t){case"spring":return s(n,e);case"cubicBezier":return u(p,a);case"steps":return u(f,a);default:return u(r,a)}}function g(n){try{return document.querySelectorAll(n)}catch(n){return}}function m(n,e){for(var t=n.length,r=arguments.length>=2?arguments[1]:void 0,a=[],o=0;o<t;o++)if(o in n){var u=n[o];e.call(r,u,o,n)&&a.push(u)}return a}function y(n){return n.reduce(function(n,e){return n.concat(i.arr(e)?y(e):e)},[])}function b(n){return i.arr(n)?n:(i.str(n)&&(n=g(n)||n),n instanceof NodeList||n instanceof HTMLCollection?[].slice.call(n):[n])}function M(n,e){return n.some(function(n){return n===e})}function x(n){var e={};for(var t in n)e[t]=n[t];return e}function w(n,e){var t=x(n);for(var r in n)t[r]=e.hasOwnProperty(r)?e[r]:n[r];return t}function k(n,e){var t=x(n);for(var r in e)t[r]=i.und(n[r])?e[r]:n[r];return t}function O(n){return i.rgb(n)?(t=/rgb\((\d+,\s*[\d]+,\s*[\d]+)\)/g.exec(e=n))?"rgba("+t[1]+",1)":e:i.hex(n)?(r=n.replace(/^#?([a-f\d])([a-f\d])([a-f\d])$/i,function(n,e,t,r){return e+e+t+t+r+r}),a=/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(r),"rgba("+parseInt(a[1],16)+","+parseInt(a[2],16)+","+parseInt(a[3],16)+",1)"):i.hsl(n)?function(n){var e,t,r,a=/hsl\((\d+),\s*([\d.]+)%,\s*([\d.]+)%\)/g.exec(n)||/hsla\((\d+),\s*([\d.]+)%,\s*([\d.]+)%,\s*([\d.]+)\)/g.exec(n),o=parseInt(a[1],10)/360,u=parseInt(a[2],10)/100,i=parseInt(a[3],10)/100,c=a[4]||1;function s(n,e,t){return t<0&&(t+=1),t>1&&(t-=1),t<1/6?n+6*(e-n)*t:t<.5?e:t<2/3?n+(e-n)*(2/3-t)*6:n}if(0==u)e=t=r=i;else{var f=i<.5?i*(1+u):i+u-i*u,l=2*i-f;e=s(l,f,o+1/3),t=s(l,f,o),r=s(l,f,o-1/3)}return"rgba("+255*e+","+255*t+","+255*r+","+c+")"}(n):void 0;var e,t,r,a}function C(n){var e=/[+-]?\d*\.?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(%|px|pt|em|rem|in|cm|mm|ex|ch|pc|vw|vh|vmin|vmax|deg|rad|turn)?$/.exec(n);if(e)return e[1]}function P(n,e){return i.fnc(n)?n(e.target,e.id,e.total):n}function I(n,e){return n.getAttribute(e)}function D(n,e,t){if(M([t,"deg","rad","turn"],C(e)))return e;var a=r.CSS[e+t];if(!i.und(a))return a;var o=document.createElement(n.tagName),u=n.parentNode&&n.parentNode!==document?n.parentNode:document.body;u.appendChild(o),o.style.position="absolute",o.style.width=100+t;var c=100/o.offsetWidth;u.removeChild(o);var s=c*parseFloat(e);return r.CSS[e+t]=s,s}function B(n,e,t){if(e in n.style){var r=e.replace(/([a-z])([A-Z])/g,"$1-$2").toLowerCase(),a=n.style[e]||getComputedStyle(n).getPropertyValue(r)||"0";return t?D(n,a,t):a}}function T(n,e){return i.dom(n)&&!i.inp(n)&&(!i.nil(I(n,e))||i.svg(n)&&n[e])?"attribute":i.dom(n)&&M(t,e)?"transform":i.dom(n)&&"transform"!==e&&B(n,e)?"css":null!=n[e]?"object":void 0}function E(n){if(i.dom(n)){for(var e,t=n.style.transform||"",r=/(\w+)\(([^)]*)\)/g,a=new Map;e=r.exec(t);)a.set(e[1],e[2]);return a}}function F(n,e,t,r){var a,u=o(e,"scale")?1:0+(o(a=e,"translate")||"perspective"===a?"px":o(a,"rotate")||o(a,"skew")?"deg":void 0),i=E(n).get(e)||u;return t&&(t.transforms.list.set(e,i),t.transforms.last=e),r?D(n,i,r):i}function A(n,e,t,r){switch(T(n,e)){case"transform":return F(n,e,r,t);case"css":return B(n,e,t);case"attribute":return I(n,e);default:return n[e]||0}}function N(n,e){var t=/^(\*=|\+=|-=)/.exec(n);if(!t)return n;var r=C(n)||0,a=parseFloat(e),o=parseFloat(n.replace(t[0],""));switch(t[0][0]){case"+":return a+o+r;case"-":return a-o+r;case"*":return a*o+r}}function S(n,e){if(i.col(n))return O(n);if(/\s/g.test(n))return n;var t=C(n),r=t?n.substr(0,n.length-t.length):n;return e?r+e:r}function L(n,e){return Math.sqrt(Math.pow(e.x-n.x,2)+Math.pow(e.y-n.y,2))}function j(n){for(var e,t=n.points,r=0,a=0;a<t.numberOfItems;a++){var o=t.getItem(a);a>0&&(r+=L(e,o)),e=o}return r}function q(n){if(n.getTotalLength)return n.getTotalLength();switch(n.tagName.toLowerCase()){case"circle":return o=n,2*Math.PI*I(o,"r");case"rect":return 2*I(a=n,"width")+2*I(a,"height");case"line":return L({x:I(r=n,"x1"),y:I(r,"y1")},{x:I(r,"x2"),y:I(r,"y2")});case"polyline":return j(n);case"polygon":return t=(e=n).points,j(e)+L(t.getItem(t.numberOfItems-1),t.getItem(0))}var e,t,r,a,o}function H(n,e){var t=e||{},r=t.el||function(n){for(var e=n.parentNode;i.svg(e)&&i.svg(e.parentNode);)e=e.parentNode;return e}(n),a=r.getBoundingClientRect(),o=I(r,"viewBox"),u=a.width,c=a.height,s=t.viewBox||(o?o.split(" "):[0,0,u,c]);return{el:r,viewBox:s,x:s[0]/1,y:s[1]/1,w:u,h:c,vW:s[2],vH:s[3]}}function V(n,e,t){function r(t){void 0===t&&(t=0);var r=e+t>=1?e+t:0;return n.el.getPointAtLength(r)}var a=H(n.el,n.svg),o=r(),u=r(-1),i=r(1),c=t?1:a.w/a.vW,s=t?1:a.h/a.vH;switch(n.property){case"x":return(o.x-a.x)*c;case"y":return(o.y-a.y)*s;case"angle":return 180*Math.atan2(i.y-u.y,i.x-u.x)/Math.PI}}function $(n,e){var t=/[+-]?\d*\.?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g,r=S(i.pth(n)?n.totalLength:n,e)+"";return{original:r,numbers:r.match(t)?r.match(t).map(Number):[0],strings:i.str(n)||e?r.split(t):[]}}function W(n){return m(n?y(i.arr(n)?n.map(b):b(n)):[],function(n,e,t){return t.indexOf(n)===e})}function X(n){var e=W(n);return e.map(function(n,t){return{target:n,id:t,total:e.length,transforms:{list:E(n)}}})}function Y(n,e){var t=x(e);if(/^spring/.test(t.easing)&&(t.duration=s(t.easing)),i.arr(n)){var r=n.length;2===r&&!i.obj(n[0])?n={value:n}:i.fnc(e.duration)||(t.duration=e.duration/r)}var a=i.arr(n)?n:[n];return a.map(function(n,t){var r=i.obj(n)&&!i.pth(n)?n:{value:n};return i.und(r.delay)&&(r.delay=t?0:e.delay),i.und(r.endDelay)&&(r.endDelay=t===a.length-1?e.endDelay:0),r}).map(function(n){return k(n,t)})}function Z(n,e){var t=[],r=e.keyframes;for(var a in r&&(e=k(function(n){for(var e=m(y(n.map(function(n){return Object.keys(n)})),function(n){return i.key(n)}).reduce(function(n,e){return n.indexOf(e)<0&&n.push(e),n},[]),t={},r=function(r){var a=e[r];t[a]=n.map(function(n){var e={};for(var t in n)i.key(t)?t==a&&(e.value=n[t]):e[t]=n[t];return e})},a=0;a<e.length;a++)r(a);return t}(r),e)),e)i.key(a)&&t.push({name:a,tweens:Y(e[a],n)});return t}function G(n,e){var t;return n.tweens.map(function(r){var a=function(n,e){var t={};for(var r in n){var a=P(n[r],e);i.arr(a)&&1===(a=a.map(function(n){return P(n,e)})).length&&(a=a[0]),t[r]=a}return t.duration=parseFloat(t.duration),t.delay=parseFloat(t.delay),t}(r,e),o=a.value,u=i.arr(o)?o[1]:o,c=C(u),s=A(e.target,n.name,c,e),f=t?t.to.original:s,l=i.arr(o)?o[0]:f,d=C(l)||C(s),p=c||d;return i.und(u)&&(u=f),a.from=$(l,p),a.to=$(N(u,l),p),a.start=t?t.end:0,a.end=a.start+a.delay+a.duration+a.endDelay,a.easing=h(a.easing,a.duration),a.isPath=i.pth(o),a.isPathTargetInsideSVG=a.isPath&&i.svg(e.target),a.isColor=i.col(a.from.original),a.isColor&&(a.round=1),t=a,a})}var Q={css:function(n,e,t){return n.style[e]=t},attribute:function(n,e,t){return n.setAttribute(e,t)},object:function(n,e,t){return n[e]=t},transform:function(n,e,t,r,a){if(r.list.set(e,t),e===r.last||a){var o="";r.list.forEach(function(n,e){o+=e+"("+n+") "}),n.style.transform=o}}};function z(n,e){X(n).forEach(function(n){for(var t in e){var r=P(e[t],n),a=n.target,o=C(r),u=A(a,t,o,n),i=N(S(r,o||C(u)),u),c=T(a,t);Q[c](a,t,i,n.transforms,!0)}})}function _(n,e){return m(y(n.map(function(n){return e.map(function(e){return function(n,e){var t=T(n.target,e.name);if(t){var r=G(e,n),a=r[r.length-1];return{type:t,property:e.name,animatable:n,tweens:r,duration:a.end,delay:r[0].delay,endDelay:a.endDelay}}}(n,e)})})),function(n){return!i.und(n)})}function R(n,e){var t=n.length,r=function(n){return n.timelineOffset?n.timelineOffset:0},a={};return a.duration=t?Math.max.apply(Math,n.map(function(n){return r(n)+n.duration})):e.duration,a.delay=t?Math.min.apply(Math,n.map(function(n){return r(n)+n.delay})):e.delay,a.endDelay=t?a.duration-Math.max.apply(Math,n.map(function(n){return r(n)+n.duration-n.endDelay})):e.endDelay,a}var J=0;var K=[],U=function(){var n;function e(t){for(var r=K.length,a=0;a<r;){var o=K[a];o.paused?(K.splice(a,1),r--):(o.tick(t),a++)}n=a>0?requestAnimationFrame(e):void 0}return"undefined"!=typeof document&&document.addEventListener("visibilitychange",function(){en.suspendWhenDocumentHidden&&(nn()?n=cancelAnimationFrame(n):(K.forEach(function(n){return n._onDocumentVisibility()}),U()))}),function(){n||nn()&&en.suspendWhenDocumentHidden||!(K.length>0)||(n=requestAnimationFrame(e))}}();function nn(){return!!document&&document.hidden}function en(t){void 0===t&&(t={});var r,o=0,u=0,i=0,c=0,s=null;function f(n){var e=window.Promise&&new Promise(function(n){return s=n});return n.finished=e,e}var l,d,p,v,h,g,y,b,M=(d=w(n,l=t),p=w(e,l),v=Z(p,l),h=X(l.targets),g=_(h,v),y=R(g,p),b=J,J++,k(d,{id:b,children:[],animatables:h,animations:g,duration:y.duration,delay:y.delay,endDelay:y.endDelay}));f(M);function x(){var n=M.direction;"alternate"!==n&&(M.direction="normal"!==n?"normal":"reverse"),M.reversed=!M.reversed,r.forEach(function(n){return n.reversed=M.reversed})}function O(n){return M.reversed?M.duration-n:n}function C(){o=0,u=O(M.currentTime)*(1/en.speed)}function P(n,e){e&&e.seek(n-e.timelineOffset)}function I(n){for(var e=0,t=M.animations,r=t.length;e<r;){var o=t[e],u=o.animatable,i=o.tweens,c=i.length-1,s=i[c];c&&(s=m(i,function(e){return n<e.end})[0]||s);for(var f=a(n-s.start-s.delay,0,s.duration)/s.duration,l=isNaN(f)?1:s.easing(f),d=s.to.strings,p=s.round,v=[],h=s.to.numbers.length,g=void 0,y=0;y<h;y++){var b=void 0,x=s.to.numbers[y],w=s.from.numbers[y]||0;b=s.isPath?V(s.value,l*x,s.isPathTargetInsideSVG):w+l*(x-w),p&&(s.isColor&&y>2||(b=Math.round(b*p)/p)),v.push(b)}var k=d.length;if(k){g=d[0];for(var O=0;O<k;O++){d[O];var C=d[O+1],P=v[O];isNaN(P)||(g+=C?P+C:P+" ")}}else g=v[0];Q[o.type](u.target,o.property,g,u.transforms),o.currentValue=g,e++}}function D(n){M[n]&&!M.passThrough&&M[n](M)}function B(n){var e=M.duration,t=M.delay,l=e-M.endDelay,d=O(n);M.progress=a(d/e*100,0,100),M.reversePlayback=d<M.currentTime,r&&function(n){if(M.reversePlayback)for(var e=c;e--;)P(n,r[e]);else for(var t=0;t<c;t++)P(n,r[t])}(d),!M.began&&M.currentTime>0&&(M.began=!0,D("begin")),!M.loopBegan&&M.currentTime>0&&(M.loopBegan=!0,D("loopBegin")),d<=t&&0!==M.currentTime&&I(0),(d>=l&&M.currentTime!==e||!e)&&I(e),d>t&&d<l?(M.changeBegan||(M.changeBegan=!0,M.changeCompleted=!1,D("changeBegin")),D("change"),I(d)):M.changeBegan&&(M.changeCompleted=!0,M.changeBegan=!1,D("changeComplete")),M.currentTime=a(d,0,e),M.began&&D("update"),n>=e&&(u=0,M.remaining&&!0!==M.remaining&&M.remaining--,M.remaining?(o=i,D("loopComplete"),M.loopBegan=!1,"alternate"===M.direction&&x()):(M.paused=!0,M.completed||(M.completed=!0,D("loopComplete"),D("complete"),!M.passThrough&&"Promise"in window&&(s(),f(M)))))}return M.reset=function(){var n=M.direction;M.passThrough=!1,M.currentTime=0,M.progress=0,M.paused=!0,M.began=!1,M.loopBegan=!1,M.changeBegan=!1,M.completed=!1,M.changeCompleted=!1,M.reversePlayback=!1,M.reversed="reverse"===n,M.remaining=M.loop,r=M.children;for(var e=c=r.length;e--;)M.children[e].reset();(M.reversed&&!0!==M.loop||"alternate"===n&&1===M.loop)&&M.remaining++,I(M.reversed?M.duration:0)},M._onDocumentVisibility=C,M.set=function(n,e){return z(n,e),M},M.tick=function(n){i=n,o||(o=i),B((i+(u-o))*en.speed)},M.seek=function(n){B(O(n))},M.pause=function(){M.paused=!0,C()},M.play=function(){M.paused&&(M.completed&&M.reset(),M.paused=!1,K.push(M),C(),U())},M.reverse=function(){x(),M.completed=!M.reversed,C()},M.restart=function(){M.reset(),M.play()},M.remove=function(n){rn(W(n),M)},M.reset(),M.autoplay&&M.play(),M}function tn(n,e){for(var t=e.length;t--;)M(n,e[t].animatable.target)&&e.splice(t,1)}function rn(n,e){var t=e.animations,r=e.children;tn(n,t);for(var a=r.length;a--;){var o=r[a],u=o.animations;tn(n,u),u.length||o.children.length||r.splice(a,1)}t.length||r.length||e.pause()}return en.version="3.2.1",en.speed=1,en.suspendWhenDocumentHidden=!0,en.running=K,en.remove=function(n){for(var e=W(n),t=K.length;t--;)rn(e,K[t])},en.get=A,en.set=z,en.convertPx=D,en.path=function(n,e){var t=i.str(n)?g(n)[0]:n,r=e||100;return function(n){return{property:n,el:t,svg:H(t),totalLength:q(t)*(r/100)}}},en.setDashoffset=function(n){var e=q(n);return n.setAttribute("stroke-dasharray",e),e},en.stagger=function(n,e){void 0===e&&(e={});var t=e.direction||"normal",r=e.easing?h(e.easing):null,a=e.grid,o=e.axis,u=e.from||0,c="first"===u,s="center"===u,f="last"===u,l=i.arr(n),d=parseFloat(n[0]),p=l?parseFloat(n[1]):0,v=C(l?n[1]:n)||0,g=e.start||0+(l?d:0),m=[],y=0;return function(n,e,i){if(c&&(u=0),s&&(u=(i-1)/2),f&&(u=i-1),!m.length){for(var h=0;h<i;h++){if(a){var b=s?(a[0]-1)/2:u%a[0],M=s?(a[1]-1)/2:Math.floor(u/a[0]),x=b-h%a[0],w=M-Math.floor(h/a[0]),k=Math.sqrt(x*x+w*w);"x"===o&&(k=-x),"y"===o&&(k=-w),m.push(k)}else m.push(Math.abs(u-h));y=Math.max.apply(Math,m)}r&&(m=m.map(function(n){return r(n/y)*y})),"reverse"===t&&(m=m.map(function(n){return o?n<0?-1*n:-n:Math.abs(y-n)}))}return g+(l?(p-d)/y:d)*(Math.round(100*m[e])/100)+v}},en.timeline=function(n){void 0===n&&(n={});var t=en(n);return t.duration=0,t.add=function(r,a){var o=K.indexOf(t),u=t.children;function c(n){n.passThrough=!0}o>-1&&K.splice(o,1);for(var s=0;s<u.length;s++)c(u[s]);var f=k(r,w(e,n));f.targets=f.targets||n.targets;var l=t.duration;f.autoplay=!1,f.direction=t.direction,f.timelineOffset=i.und(a)?l:N(a,l),c(t),t.seek(f.timelineOffset);var d=en(f);c(d),u.push(d);var p=R(u,n);return t.delay=p.delay,t.endDelay=p.endDelay,t.duration=p.duration,t.seek(0),t.reset(),t.autoplay&&t.play(),t},t},en.easing=h,en.penner=v,en.random=function(n,e){return Math.floor(Math.random()*(e-n+1))+n},en});"""


_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atlas Bundle — __TITLE__</title>
<style>__FONTS__</style>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div id="accent-strip"></div>
<div class="scroll-prog" id="scroll-prog"></div>

<header id="topbar" class="darkzone">
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
<script src="assets/anime.min.js"></script>
<script src="assets/app.js"></script>
</body>
</html>"""


_VIEWER_CSS = r"""*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --itl-navy:#101625;--itl-blue:#1B93D2;--itl-orange:#FF6633;--itl-green:#99CA3C;--itl-pink:#C5258F;
  --font-display:"Sora",system-ui,sans-serif;
  --font-body:"Hanken Grotesk",system-ui,sans-serif;
  --font-mono:"JetBrains Mono","SF Mono","Cascadia Code",monospace;
  --bg-base:#f3f1ec;--bg-surface:#ffffff;--bg-elevated:#faf8f3;--bg-overlay:#ece8e0;
  --surface-grad:linear-gradient(180deg,#ffffff,#f7f5ef);
  --hairline:rgba(22,20,31,.09);--hairline-strong:rgba(22,20,31,.17);
  --text-primary:#16141f;--text-secondary:#4b4758;--text-muted:#87828f;--text-ghost:rgba(135,130,143,.55);
  --accent:#1b82bc;--accent-hover:#1670a4;--accent-ghost:rgba(27,147,210,.12);--accent-glow:rgba(27,147,210,.22);
  --status-pass:#6fa22c;--status-pass-bg:rgba(111,162,44,.12);
  --status-fail:#c0392b;--status-fail-bg:rgba(192,57,43,.10);
  --status-warn:#cf7320;--status-warn-bg:rgba(207,115,32,.12);
  --status-info:#1b82bc;--status-info-bg:rgba(27,130,188,.12);
  --status-skip:#6b7280;--status-skip-bg:rgba(107,114,128,.10);
  --shadow-sm:0 1px 3px rgba(22,20,31,.06);
  --shadow-md:0 12px 34px -14px rgba(22,20,31,.18);
  --r-sm:8px;--r-md:14px;--r-lg:20px;--r-pill:999px;
  --bg:var(--bg-base);--bg2:var(--bg-surface);--surf:var(--bg-surface);--surf2:var(--bg-elevated);--surf3:var(--bg-overlay);
  --bdr:var(--hairline);--bdr2:var(--hairline-strong);--bdr3:rgba(22,20,31,.26);
  --text:var(--text-primary);--text2:var(--text-secondary);--text3:var(--text-muted);
  --acc:var(--accent);--acc2:var(--accent-hover);--info:var(--status-info);--info-s:var(--status-info-bg);
  --ok:var(--status-pass);--ok-s:var(--status-pass-bg);
  --warn:var(--status-warn);--warn-s:var(--status-warn-bg);
  --bad:var(--status-fail);--bad-s:var(--status-fail-bg);
  --sh:var(--shadow-sm);--r:var(--r-sm);--r2:var(--r-sm);--r3:var(--r-md);
  --display:var(--font-display);--body:var(--font-body);--mono:var(--font-mono);
  --info:#1B93D2;
}
.darkzone{
  --bg-surface:#121a30;--bg-elevated:#1a2440;
  --hairline:rgba(255,255,255,.08);--hairline-strong:rgba(255,255,255,.15);
  --text-primary:#eef3fb;--text-secondary:#9fb0cc;--text-muted:#7186a6;--text-ghost:rgba(113,134,166,.5);
  --accent:var(--itl-blue);--accent-ghost:rgba(27,147,210,.12);--accent-glow:rgba(27,147,210,.32);
  color:var(--text-primary);
  --surf:var(--bg-surface);--surf2:var(--bg-elevated);
  --bdr:var(--hairline);--bdr2:var(--hairline-strong);
  --text:var(--text-primary);--text2:var(--text-secondary);--text3:var(--text-muted);
  --acc:var(--accent);
}

html,body{height:100%;overflow:hidden}
body{background:var(--bg-base);color:var(--text-primary);font:14px/1.6 var(--font-body);display:flex;flex-direction:column}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(22,20,31,.16);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(22,20,31,.26)}

/* ── Accent strip ── */
#accent-strip{height:3px;background:linear-gradient(90deg,var(--itl-blue) 0%,var(--itl-green) 100%);flex-shrink:0}

/* ── Topbar ── */
#topbar{height:58px;background:var(--itl-navy);box-shadow:0 1px 0 rgba(255,255,255,.05),0 4px 16px rgba(0,0,0,.3);display:flex;align-items:center;gap:12px;padding:0 28px;flex-shrink:0;position:relative;z-index:10}
.logo{display:flex;align-items:center;gap:10px;white-space:nowrap;margin-right:8px}
.logo-text{display:flex;flex-direction:column;gap:0}
.logo-name{font-weight:700;font-size:15px;letter-spacing:-.02em;color:#f1f5f9;font-family:var(--font-display);line-height:1.2}
.logo-version{font-size:10.5px;color:#3d5166;font-weight:500;line-height:1.3;font-family:var(--font-mono)}
.chip{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:600;padding:4px 11px;border-radius:var(--r-pill);letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;line-height:1;font-family:var(--font-body)}
.chip-blue{background:rgba(27,147,210,.16);color:#7dd3fc;border:1px solid rgba(27,147,210,.28)}
.chip-ok{background:rgba(22,163,74,.14);color:#4ade80;border:1px solid rgba(22,163,74,.25)}
.chip-dim{background:rgba(255,255,255,.06);color:#64748b;border:1px solid rgba(255,255,255,.1)}
.tb-sep{flex:1}
.tb-date{font-size:11.5px;color:#3d5166;white-space:nowrap;font-family:var(--font-mono)}

/* ── Shell ── */
#shell{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
#sidebar{width:220px;background:var(--bg-surface);border-right:1px solid var(--hairline);flex-shrink:0;overflow-y:auto;display:flex;flex-direction:column;padding:20px 12px 32px}
.nav-section{font-size:9px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.16em;padding:18px 10px 8px;font-family:var(--font-body)}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;font-size:13.5px;color:var(--text-secondary);cursor:pointer;border-radius:var(--r-sm);transition:background .1s,color .1s;user-select:none;margin-bottom:2px;font-family:var(--font-body)}
.nav-item:hover{background:var(--bg-overlay);color:var(--text-primary)}
.nav-item.active{background:var(--accent-ghost);color:var(--accent);font-weight:600}
.nav-icon{width:16px;height:16px;flex-shrink:0;opacity:.4;transition:opacity .1s}
.nav-item.active .nav-icon,.nav-item:hover .nav-icon{opacity:1}
.nav-label{flex:1}
.nav-badge{font-size:9.5px;font-weight:700;padding:2px 8px;border-radius:var(--r-pill);font-family:var(--font-mono);line-height:1.4}
.nav-badge.bad{background:var(--status-fail-bg);color:var(--status-fail)}
.nav-badge.warn{background:var(--status-warn-bg);color:var(--status-warn)}

/* ── Main ── */
#main{flex:1;overflow-y:auto;padding:32px 36px 72px}
.sec{display:none}.sec.active{display:block}

/* ── Status Hero (dark card) ── */
.status-hero{background:linear-gradient(135deg,#0f172a 0%,#162235 60%,#0c1d32 100%);border-radius:var(--r-lg);padding:30px 34px;margin-bottom:24px;position:relative;overflow:hidden}
.status-hero::before{content:'';position:absolute;top:-80px;right:-80px;width:320px;height:320px;background:radial-gradient(circle,rgba(27,147,210,.15) 0%,transparent 60%);pointer-events:none}
.status-hero::after{content:'';position:absolute;bottom:-60px;left:30%;width:240px;height:240px;background:radial-gradient(circle,rgba(153,202,60,.07) 0%,transparent 60%);pointer-events:none}
.sh-eyebrow{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.18em;color:rgba(255,255,255,.3);margin-bottom:12px;font-family:var(--font-body)}
.sh-title{font-size:2.35rem;font-weight:800;letter-spacing:-.04em;color:#f1f5f9;line-height:1.1;margin-bottom:6px;font-family:var(--font-display);text-wrap:balance}
.sh-status{font-size:1.05rem;font-weight:700;letter-spacing:-.01em;font-family:var(--font-display);margin-bottom:0}
.sh-status-ok{color:#4ade80}.sh-status-warn{color:#fb923c}.sh-status-bad{color:#f87171}
.sh-ticket{display:inline-block;font-size:10.5px;font-weight:600;font-family:var(--font-mono);color:rgba(255,255,255,.4);background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:var(--r-sm);padding:3px 10px;margin-top:10px}
.sh-stats{display:flex;gap:32px;flex-wrap:wrap;padding-top:22px;margin-top:22px;border-top:1px solid rgba(255,255,255,.08)}
.sh-sv{font-size:1.9rem;font-weight:800;letter-spacing:-.03em;font-variant-numeric:tabular-nums;color:#fff;font-family:var(--font-display);line-height:1}
.sh-sv.ok{color:#4ade80}.sh-sv.warn{color:#fb923c}.sh-sv.bad{color:#f87171}
.sh-sl{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:rgba(255,255,255,.3);margin-top:5px;font-family:var(--font-body)}

/* ── Page header ── */
.ph{margin-bottom:24px}
.ph-title{font-size:22px;font-weight:700;letter-spacing:-.03em;line-height:1.2;color:var(--text-primary);font-family:var(--font-display)}
.ph-sub{font-size:14px;color:var(--text-secondary);margin-top:5px}

/* ── Legacy section hero ── */
.section-hero{margin-bottom:24px;padding-bottom:20px;border-bottom:1px solid var(--hairline)}
.section-hero .hero-ticket{font-size:2.5rem;font-weight:800;letter-spacing:-.05em;line-height:1.1;color:var(--text-primary);margin-bottom:6px;font-family:var(--font-display)}
.section-hero .hero-meta{font-size:14px;color:var(--text-secondary)}

/* ── Metric tiles ── */
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:14px;margin-bottom:24px}
.tile{background:var(--bg-surface);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:22px 24px 18px;cursor:pointer;transition:box-shadow .15s,transform .13s;box-shadow:var(--shadow-md)}
.tile:hover{box-shadow:0 2px 6px rgba(22,20,31,.06),0 10px 28px rgba(22,20,31,.09);transform:translateY(-2px)}
.tile-label{font-size:10.5px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:12px;font-family:var(--font-body)}
.tile-value{font-size:2.75rem;font-weight:800;line-height:1;letter-spacing:-.04em;color:var(--text-primary);font-family:var(--font-display);font-variant-numeric:tabular-nums}
.tile-value.ok{color:var(--status-pass)}.tile-value.warn{color:var(--status-warn)}.tile-value.bad{color:var(--status-fail)}.tile-value.dim{color:var(--text-secondary)}
.tile-sub{font-size:12px;color:var(--text-muted);margin-top:9px}

/* ── Cards ── */
.card{background:var(--surface-grad);border:1px solid var(--hairline);border-radius:var(--r-lg);margin-bottom:16px;overflow:hidden;box-shadow:var(--shadow-md)}
.card-head{display:flex;align-items:center;gap:10px;padding:15px 22px;border-bottom:1px solid var(--hairline);flex-wrap:wrap}
.card-head h3{font-size:14px;font-weight:600;margin:0;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text-primary);font-family:var(--font-body)}
.card-head .sub{font-size:11px;color:var(--text-muted);font-family:var(--font-mono)}
.card-head .ml{margin-left:auto;display:flex;align-items:center;gap:8px;flex-shrink:0}
.card-body{padding:18px 22px}
.card-body.np{padding:0}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;padding:3px 10px;border-radius:var(--r-pill);white-space:nowrap;font-family:var(--font-body)}
.badge .dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex-shrink:0}
.badge-ok{background:var(--status-pass-bg);color:var(--status-pass)}
.badge-warn{background:var(--status-warn-bg);color:var(--status-warn)}
.badge-bad{background:var(--status-fail-bg);color:var(--status-fail)}
.badge-dim{background:rgba(22,20,31,.06);color:var(--text-secondary)}
.badge-acc{background:var(--accent-ghost);color:var(--accent)}
.badge-info{background:var(--status-info-bg);color:var(--status-info)}

/* ── Tables ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead th{font-size:10.5px;font-weight:700;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.08em;padding:10px 18px;border-bottom:1px solid var(--hairline-strong);text-align:left;white-space:nowrap;background:var(--bg-elevated);position:sticky;top:0;z-index:1;font-family:var(--font-body)}
td{padding:11px 18px;border-bottom:1px solid var(--hairline);vertical-align:top;font-size:13.5px;color:var(--text-primary)}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:rgba(27,147,210,.035)}
.td-mono{font-family:var(--font-mono);font-size:12px}
.td-dim{color:var(--text-secondary);font-size:13px}
.td-break{word-break:break-all;max-width:400px}
.td-num{font-family:var(--font-mono);text-align:right}

/* ── KV list ── */
.kv-list{padding:2px 0}
.kv{display:flex;align-items:flex-start;gap:16px;padding:9px 0;border-bottom:1px solid var(--hairline)}
.kv:last-child{border-bottom:none}
.kk{font-size:12.5px;color:var(--text-secondary);min-width:180px;flex-shrink:0;font-weight:500;padding-top:1px;font-family:var(--font-body)}
.kv-val{font-size:13.5px;color:var(--text-primary);font-family:var(--font-body);word-break:break-word;overflow-wrap:break-word;flex:1}

/* ── Code blocks ── */
.code-wrap{position:relative;margin-top:4px}
pre{font-family:var(--font-mono);font-size:12px;line-height:1.7;background:var(--bg-elevated);border:1px solid var(--hairline);border-radius:var(--r-sm);padding:14px 16px;color:var(--text-secondary);white-space:pre-wrap;word-break:break-all;overflow-x:auto;max-height:420px;overflow-y:auto}
.copy-btn{position:absolute;top:8px;right:8px;background:var(--bg-surface);border:1px solid var(--hairline-strong);color:var(--text-secondary);font-size:10.5px;padding:3px 10px;border-radius:var(--r-sm);cursor:pointer;transition:all .13s;font-family:var(--font-mono)}
.copy-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.copy-btn.copied{background:var(--status-pass);color:#fff;border-color:var(--status-pass)}

/* ── JSON highlight ── */
.jk{color:#1d4ed8}.jstr{color:#15803d}.jred{color:#b45309;font-weight:700}.jnum{color:#7c3aed}.jbool{color:#0891b2}.jnul{color:var(--text-muted)}

/* ── Expand toggle ── */
.xpand{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--accent);cursor:pointer;user-select:none;padding:4px 0;transition:color .15s ease}
.xpand:hover{color:var(--accent-hover)}
.xpand::before{content:"▶";font-size:9px;display:inline-block;transition:transform .13s;flex-shrink:0}
.xpand.open::before{transform:rotate(90deg)}
.xpand-body{display:none;margin-top:10px}

/* ── Error banner ── */
.err-banner{background:var(--status-fail-bg);border:1px solid rgba(192,57,43,.2);border-radius:var(--r-sm);padding:13px 16px;margin-bottom:16px}
.err-banner-title{font-size:11.5px;font-weight:700;color:var(--status-fail);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.err-banner ul{padding-left:18px}.err-banner li{font-size:12.5px;color:var(--text-primary);margin-bottom:3px}

/* ── Progress bar ── */
.prog{margin-bottom:10px}
.prog-row{display:flex;justify-content:space-between;font-size:12px;color:var(--text-secondary);margin-bottom:5px;font-family:var(--font-body)}
.prog-track{height:7px;background:var(--bg-overlay);border-radius:4px;overflow:hidden}
.prog-fill{height:100%;border-radius:4px;transition:width .4s ease}
.prog-ok{background:var(--status-pass)}.prog-warn{background:var(--status-warn)}.prog-bad{background:var(--status-fail)}.prog-acc{background:var(--accent)}

/* ── Stats strip ── */
.stats-strip{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--bg-surface);border:1px solid var(--hairline);border-radius:var(--r-sm);padding:16px 20px;flex:1;min-width:90px;box-shadow:var(--shadow-sm)}
.stat-label{font-size:10.5px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.09em;font-family:var(--font-body)}
.stat-value{font-size:2rem;font-weight:800;color:var(--text-primary);letter-spacing:-.03em;margin-top:5px;line-height:1;font-family:var(--font-display);font-variant-numeric:tabular-nums}
.stat-value.ok{color:var(--status-pass)}.stat-value.warn{color:var(--status-warn)}.stat-value.bad{color:var(--status-fail)}

/* ── Tabs ── */
.tab-bar{display:flex;gap:0;border-bottom:1px solid var(--hairline);margin-bottom:20px}
.tab-btn{padding:10px 18px;font-size:13.5px;color:var(--text-secondary);cursor:pointer;border-bottom:2px solid transparent;transition:color .13s;user-select:none;margin-bottom:-1px;font-weight:500;font-family:var(--font-body)}
.tab-btn:hover{color:var(--text-primary)}.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.tab-pane{display:none}.tab-pane.active{display:block}

/* ── Search bar ── */
.search-bar{display:flex;align-items:center;gap:8px;margin-bottom:16px}
.search-input{flex:1;background:var(--bg-surface);border:1px solid var(--hairline-strong);border-radius:var(--r-sm);padding:9px 14px;font-size:13.5px;color:var(--text-primary);font-family:var(--font-body);outline:none;transition:border-color .13s;box-shadow:0 1px 2px rgba(22,20,31,.04)}
.search-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ghost)}.search-input::placeholder{color:var(--text-muted)}
.search-count{font-size:12px;color:var(--text-muted);white-space:nowrap;min-width:72px;text-align:right;font-family:var(--font-body)}

/* ── Log lines ── */
.log-list{max-height:500px;overflow-y:auto;font-family:var(--font-mono)}
.log-line{font-size:12px;line-height:1.7;padding:5px 0;border-bottom:1px solid var(--hairline);color:var(--text-secondary);word-break:break-all}
.log-line:last-child{border-bottom:none}
.log-line mark{background:var(--status-warn-bg);color:var(--status-warn);border-radius:2px;padding:0 2px}
.kw-tag{display:inline-block;font-size:9px;background:var(--status-warn-bg);color:var(--status-warn);border-radius:3px;padding:1px 5px;margin-right:5px;flex-shrink:0;font-family:var(--font-body)}

/* ── Empty state ── */
.empty{text-align:center;padding:40px 20px;color:var(--text-muted);font-size:14px;font-family:var(--font-body)}
.empty-icon{font-size:32px;margin-bottom:12px;display:block;opacity:.4}

/* ── Grids ── */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.g2{grid-template-columns:1fr}}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:860px){.g3{grid-template-columns:1fr 1fr}}

/* ── Info note ── */
.info-note{background:var(--status-info-bg);border:1px solid rgba(27,147,210,.18);border-radius:var(--r-sm);padding:11px 16px;font-size:13.5px;color:#0369a1;margin-bottom:14px;font-family:var(--font-body)}

/* ── Misc ── */
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
hr{border:none;border-top:1px solid var(--hairline);margin:16px 0}

/* ── Hero card ── */
.hero-card{background:linear-gradient(135deg,var(--bg-surface) 0%,rgba(27,147,210,.04) 100%);border:1px solid rgba(27,147,210,.14);border-radius:var(--r-md);padding:22px 28px;margin-bottom:20px;box-shadow:var(--shadow-sm)}
.hero-row{display:flex;flex-wrap:wrap;gap:20px 36px}
.hero-item{min-width:110px}
.hero-label{font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.11em;margin-bottom:5px;font-family:var(--font-body)}
.hero-value{font-size:13.5px;font-weight:600;color:var(--text-primary);font-family:var(--font-body);word-break:break-all;line-height:1.4}

/* ── Endpoint status strip ── */
.ep-strip{display:flex;flex-wrap:wrap;gap:10px}
.ep-item{display:flex;align-items:center;gap:10px;background:var(--bg-surface);border:1px solid var(--hairline);border-left:3px solid var(--status-pass);border-radius:var(--r-sm);padding:12px 18px;flex:1;min-width:170px;cursor:pointer;transition:box-shadow .13s;box-shadow:var(--shadow-sm)}
.ep-item:hover{box-shadow:0 4px 14px rgba(22,20,31,.09)}
.ep-item.ep-ok{border-left-color:var(--status-pass)}.ep-item.ep-bad{border-left-color:var(--status-fail)}.ep-item.ep-warn{border-left-color:var(--status-warn)}
.ep-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.bg-ok{background:var(--status-pass)}.bg-bad{background:var(--status-fail)}.bg-warn{background:var(--status-warn)}
.ep-name{font-size:13.5px;font-weight:600;color:var(--text-primary);flex:1;font-family:var(--font-body)}
.ep-path{font-size:11px;color:var(--text-muted);font-family:var(--font-mono);margin-top:1px}

/* ── Inline link ── */
.link-btn{font-size:13px;color:var(--accent);cursor:pointer;background:none;border:none;padding:0;font-family:var(--font-body);transition:color .13s;font-weight:500}
.link-btn:hover{color:var(--accent-hover);text-decoration:underline}

/* ── Inline nested KV ── */
.kv-nested{background:var(--bg-elevated);border:1px solid var(--hairline);border-radius:var(--r-sm);padding:8px 12px;margin-top:4px;display:inline-block;min-width:200px;max-width:100%}
.kv-nested .kv{padding:4px 0;border-bottom:1px solid var(--hairline)}
.kv-nested .kv:last-child{border-bottom:none}
.kv-nested .kk{font-size:11px;min-width:120px;color:var(--text-muted)}
.kv-nested .kv-val{font-size:12px}

/* ── Adapter / Application card grid ── */
.adapter-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-top:4px}
.adapter-card{background:var(--bg-surface);border:1px solid var(--hairline);border-left:3px solid var(--text-muted);border-radius:var(--r-sm);padding:14px 16px;transition:box-shadow .13s}
.adapter-card:hover{box-shadow:0 4px 14px rgba(22,20,31,.09)}
.adapter-card.ok{border-left-color:var(--status-pass)}.adapter-card.warn{border-left-color:var(--status-warn)}.adapter-card.bad{border-left-color:var(--status-fail)}
.adapter-name{font-size:13.5px;font-weight:600;color:var(--text-primary);line-height:1.3;font-family:var(--font-body)}
.adapter-meta{font-size:12.5px;color:var(--text-secondary);margin-top:5px;font-family:var(--font-body)}

/* ── Legacy overview org name hero ── */
.hero-org-name{font-size:2.4rem;font-weight:800;letter-spacing:-.04em;line-height:1.1;color:var(--text-primary);margin-bottom:10px;font-family:var(--font-display)}
.hero-ticket-badge{display:inline-flex;align-items:center;background:var(--accent-ghost);color:var(--accent);font-size:1rem;font-weight:700;letter-spacing:.05em;padding:5px 16px;border-radius:var(--r-pill);border:1px solid rgba(27,147,210,.22);margin-bottom:10px;font-family:var(--font-mono)}

/* ── Scrollable description ── */
.kv-desc{max-height:180px;overflow-y:auto;padding-right:6px}
.kv-desc::-webkit-scrollbar{width:4px}
.kv-desc::-webkit-scrollbar-track{background:transparent}
.kv-desc::-webkit-scrollbar-thumb{background:rgba(22,20,31,.18);border-radius:4px}
.kv-desc::-webkit-scrollbar-thumb:hover{background:rgba(22,20,31,.3)}

/* ── Per-CPU usage grid ── */
.cpu-grid{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.cpu-core{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:36px}
.cpu-core-bar{width:100%;height:36px;background:var(--bg-overlay);border-radius:4px;overflow:hidden;display:flex;align-items:flex-end}
.cpu-core-fill{width:100%;border-radius:4px;transition:height .3s ease}
.cpu-core-label{font-size:9px;color:var(--text-muted);font-family:var(--font-mono)}

/* ── Network interface table ── */
.net-family{font-size:10px;font-weight:600;padding:2px 6px;border-radius:var(--r-pill);text-transform:uppercase;letter-spacing:.04em;background:var(--status-info-bg);color:var(--status-info)}

/* ── Animated accent bar on active nav item ── */
.nav-item{position:relative}
.nav-item.active::before{content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);width:3px;height:60%;background:var(--accent);border-radius:0 3px 3px 0}

/* ── Tile top-accent ── */
.tile{position:relative;overflow:hidden}
.tile::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--accent),transparent);opacity:0;transition:opacity .2s}
.tile:hover::before{opacity:1}
.tile-icon{width:36px;height:36px;border-radius:var(--r-sm);display:grid;place-items:center;background:var(--accent-ghost);color:var(--accent);margin-bottom:14px}
.tile-icon svg{width:18px;height:18px}

/* ── Card icon header cell ── */
.icd{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;flex:none;color:#fff}
.icd svg{width:17px;height:17px}

/* ── Log toolbar ── */
.log-toolbar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;align-items:flex-start}
.log-search-wrap{flex:1;min-width:200px;position:relative;display:flex;align-items:center}
.log-search-wrap svg{position:absolute;left:12px;width:15px;height:15px;color:var(--text-muted);pointer-events:none}
.log-search-input{width:100%;padding:9px 14px 9px 36px;background:var(--bg-surface);border:1px solid var(--hairline-strong);border-radius:var(--r-sm);font-size:13.5px;color:var(--text-primary);font-family:var(--font-body);outline:none;transition:border-color .15s,box-shadow .15s}
.log-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ghost)}
.log-search-input::placeholder{color:var(--text-muted)}
.log-date-wrap{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.log-date-label{font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;font-family:var(--font-body)}
.log-date-input{padding:8px 12px;background:var(--bg-surface);border:1px solid var(--hairline-strong);border-radius:var(--r-sm);font-size:12.5px;color:var(--text-primary);font-family:var(--font-body);outline:none;cursor:pointer;transition:border-color .15s}
.log-date-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ghost)}
.log-level-chips{display:flex;gap:5px;flex-wrap:wrap}
.log-chip{display:inline-flex;align-items:center;gap:5px;padding:7px 12px;border-radius:var(--r-pill);font-size:11.5px;font-weight:600;cursor:pointer;border:1px solid var(--hairline);background:var(--bg-elevated);color:var(--text-secondary);transition:all .15s;user-select:none;font-family:var(--font-body)}
.log-chip:hover{border-color:var(--hairline-strong);color:var(--text-primary)}
.log-chip.active{border-color:transparent;color:#fff}
.log-chip.active.chip-all{background:var(--accent)}
.log-chip.active.chip-error{background:var(--status-fail)}
.log-chip.active.chip-warn{background:var(--status-warn)}
.log-chip.active.chip-info{background:var(--status-info)}
.log-chip .chip-dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.log-toolbar-footer{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.log-result-count{font-size:12.5px;color:var(--text-muted);font-family:var(--font-body)}
.log-result-count b{color:var(--text-primary);font-weight:600}
.log-clear-btn{font-size:12px;color:var(--accent);cursor:pointer;background:none;border:none;font-family:var(--font-body);font-weight:600;padding:0;display:none}
.log-clear-btn.show{display:inline}
.log-clear-btn:hover{text-decoration:underline}

/* ── Log line enhanced ── */
.log-line{display:flex;align-items:baseline;gap:8px;font-size:12px;line-height:1.65;padding:6px 0;border-bottom:1px solid var(--hairline);color:var(--text-secondary);word-break:break-word}
.log-line:last-child{border-bottom:none}
.log-ts{font-size:10.5px;color:var(--text-muted);font-family:var(--font-mono);white-space:nowrap;flex-shrink:0}
.log-lvl{font-size:9px;font-weight:700;letter-spacing:.05em;padding:2px 7px;border-radius:4px;text-transform:uppercase;flex-shrink:0;font-family:var(--font-body)}
.log-lvl.error,.log-lvl.critical{background:var(--status-fail-bg);color:var(--status-fail)}
.log-lvl.warn,.log-lvl.warning{background:var(--status-warn-bg);color:var(--status-warn)}
.log-lvl.info{background:var(--status-info-bg);color:var(--status-info)}
.log-msg{flex:1;font-family:var(--font-mono);font-size:11.5px;white-space:pre-wrap}
.log-line mark{background:rgba(207,115,32,.18);color:var(--status-warn);border-radius:3px;padding:0 2px;font-style:normal}

/* ── SVG empty states ── */
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:52px 24px;text-align:center}
.empty-state svg{margin-bottom:20px;opacity:.85}
.empty-state .es-title{font-family:var(--font-display);font-weight:700;font-size:1.1rem;color:var(--text-secondary);margin-bottom:8px}
.empty-state .es-sub{font-size:.86rem;color:var(--text-muted);max-width:36ch;line-height:1.55}

/* ── Section page heading (improved) ── */
.sec-head{margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid var(--hairline)}
.sec-head-inner{display:flex;align-items:flex-start;gap:18px}
.sec-head-icon{width:46px;height:46px;border-radius:var(--r-md);display:grid;place-items:center;flex:none;color:#fff;box-shadow:var(--shadow-sm)}
.sec-head-icon svg{width:22px;height:22px}
.sec-head-title{font-family:var(--font-display);font-weight:700;font-size:1.42rem;letter-spacing:-.02em;line-height:1.15}
.sec-head-sub{font-size:.9rem;color:var(--text-muted);margin-top:5px}

/* ── Count-up wrapper ── */
.count-up{display:inline-block}

/* ── Modal ── */
.modal-scrim{position:fixed;inset:0;z-index:400;background:rgba(5,8,18,.52);backdrop-filter:blur(4px);opacity:0;visibility:hidden;transition:opacity .3s,visibility .3s}
.modal-scrim.open{opacity:1;visibility:visible}
.modal-box{position:fixed;z-index:401;left:50%;top:50%;transform:translate(-50%,-48%) scale(.97);opacity:0;visibility:hidden;width:min(720px,92vw);max-height:82vh;display:flex;flex-direction:column;background:var(--bg-surface);border:1px solid var(--hairline-strong);border-radius:var(--r-lg);box-shadow:0 30px 70px -20px rgba(22,20,31,.35);transition:opacity .3s,transform .3s,visibility .3s}
.modal-box.open{opacity:1;visibility:visible;transform:translate(-50%,-50%) scale(1)}
.modal-mhead{display:flex;align-items:center;gap:14px;padding:18px 22px;border-bottom:1px solid var(--hairline);flex-shrink:0}
.modal-mhead .icd{flex:none}
.modal-mtt{flex:1;min-width:0}
.modal-mtt .mt{font-family:var(--font-display);font-weight:700;font-size:1.12rem;line-height:1.2}
.modal-mtt .ms{font-size:.78rem;color:var(--text-muted)}
.modal-close{width:36px;height:36px;border-radius:var(--r-sm);border:1px solid var(--hairline);background:var(--bg-elevated);color:var(--text-secondary);cursor:pointer;display:grid;place-items:center;flex:none;transition:all .2s}
.modal-close:hover{color:var(--text-primary);transform:rotate(90deg);border-color:var(--hairline-strong)}
.modal-close svg{width:15px;height:15px}
.modal-mbody{flex:1;overflow-y:auto;padding:22px}

/* ── Scroll progress ── */
.scroll-prog{position:fixed;bottom:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--itl-blue),var(--itl-green));transform-origin:left;transform:scaleX(0);z-index:50;pointer-events:none}

/* ── Hero animated grid ── */
.hero-grid-overlay{position:absolute;inset:0;z-index:0;opacity:.4;background-image:linear-gradient(rgba(255,255,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.04) 1px,transparent 1px);background-size:44px 44px;-webkit-mask-image:radial-gradient(ellipse 70% 60% at 60% 50%,#000,transparent 75%);mask-image:radial-gradient(ellipse 70% 60% at 60% 50%,#000,transparent 75%);pointer-events:none}
.status-hero{position:relative}
.sh-inner{position:relative;z-index:1}"""


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
  const id='pb'+(++_cid);
  const cls=pct>90?'prog-bad':pct>75?'prog-warn':'prog-ok';
  setTimeout(function(){
    var el=document.getElementById(id);
    if(el&&window.anime)anime({targets:el,width:[0,Math.min(pct,100)+'%'],duration:700,easing:'easeOutCubic',delay:100});
    else if(el)el.style.width=Math.min(pct,100)+'%';
  },50);
  return'<div class="prog">'
    +'<div class="prog-row"><span>'+(labelLeft||pct.toFixed(1)+'%')+'</span>'+(labelRight?'<span>'+esc(labelRight)+'</span>':'')+'</div>'
    +'<div class="prog-track"><div class="prog-fill '+cls+'" id="'+id+'" style="width:0"></div></div>'
    +'</div>';
}

/* ── Navigation ──────────────────────────────────────────────── */
function navTo(id){
  var old=document.querySelector('.sec.active');
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  var s=document.getElementById('sec-'+id),n=document.getElementById('ni-'+id);
  if(n)n.classList.add('active');
  if(!s)return;
  if(window.anime&&old&&old!==s){
    anime({targets:old,opacity:[1,0],translateX:[0,-18],duration:180,easing:'easeInCubic',complete:function(){
      old.classList.remove('active');
      s.classList.add('active');
      document.getElementById('main').scrollTop=0;
      anime({targets:s,opacity:[0,1],translateX:[18,0],duration:280,easing:'easeOutCubic'});
      var cards=s.querySelectorAll('.card,.tile,.status-hero,.hero-card,.stat');
      if(cards.length)anime({targets:cards,opacity:[0,1],translateY:[12,0],duration:360,delay:anime.stagger(40,{start:60}),easing:'easeOutCubic'});
    }});
  } else {
    document.querySelectorAll('.sec').forEach(function(x){x.classList.remove('active')});
    s.classList.add('active');
    document.getElementById('main').scrollTop=0;
    if(window.anime){
      anime({targets:s,opacity:[0,1],translateX:[18,0],duration:280,easing:'easeOutCubic'});
      var cards=s.querySelectorAll('.card,.tile,.status-hero,.hero-card,.stat');
      if(cards.length)anime({targets:cards,opacity:[0,1],translateY:[12,0],duration:360,delay:anime.stagger(40,{start:80}),easing:'easeOutCubic'});
    }
  }
}

/* ── Tab switcher ────────────────────────────────────────────── */
function switchTab(btns,panes,idx){
  document.querySelectorAll('.'+btns).forEach((t,i)=>t.classList.toggle('active',i===idx));
  document.querySelectorAll('.'+panes).forEach((p,i)=>p.classList.toggle('active',i===idx));
}

/* ── SVG empty states ─────────────────────────────────────────── */
function _svgEmpty(kind){
  // Returns a small SVG illustration for empty states
  const c={
    search:'<svg width="64" height="64" viewBox="0 0 64 64" fill="none"><circle cx="28" cy="28" r="16" stroke="#1b82bc" stroke-width="2" stroke-dasharray="4 3" opacity=".4"/><line x1="40" y1="40" x2="54" y2="54" stroke="#1b82bc" stroke-width="2.5" stroke-linecap="round" opacity=".5"/><line x1="22" y1="28" x2="34" y2="28" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".5"/><line x1="22" y1="23" x2="30" y2="23" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".5"/><line x1="22" y1="33" x2="28" y2="33" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".5"/></svg>',
    logs:'<svg width="64" height="64" viewBox="0 0 64 64" fill="none"><rect x="10" y="8" width="36" height="48" rx="4" stroke="#1b82bc" stroke-width="2" opacity=".3"/><line x1="18" y1="22" x2="38" y2="22" stroke="#6fa22c" stroke-width="2" stroke-linecap="round" opacity=".6"/><line x1="18" y1="30" x2="34" y2="30" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".4"/><line x1="18" y1="37" x2="36" y2="37" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".4"/><line x1="18" y1="44" x2="30" y2="44" stroke="#87828f" stroke-width="1.5" stroke-linecap="round" opacity=".4"/><circle cx="48" cy="48" r="10" fill="#6fa22c" opacity=".15"/><path d="M44 48l3 3 6-6" stroke="#6fa22c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".7"/></svg>',
    health:'<svg width="64" height="64" viewBox="0 0 64 64" fill="none"><rect x="8" y="8" width="48" height="48" rx="8" stroke="#1b82bc" stroke-width="2" opacity=".2"/><polyline points="12,32 20,32 26,18 34,46 40,26 46,32 52,32" stroke="#1b82bc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".6"/></svg>',
    system:'<svg width="64" height="64" viewBox="0 0 64 64" fill="none"><rect x="6" y="14" width="52" height="36" rx="4" stroke="#87828f" stroke-width="2" opacity=".3"/><rect x="18" y="50" width="28" height="4" rx="2" fill="#87828f" opacity=".2"/><rect x="14" y="20" width="16" height="10" rx="2" fill="#1b82bc" opacity=".15"/><rect x="34" y="20" width="18" height="5" rx="1.5" fill="#87828f" opacity=".15"/><rect x="34" y="28" width="12" height="5" rx="1.5" fill="#87828f" opacity=".15"/><rect x="14" y="34" width="36" height="5" rx="1.5" fill="#87828f" opacity=".1"/></svg>',
    config:'<svg width="64" height="64" viewBox="0 0 64 64" fill="none"><circle cx="32" cy="32" r="10" stroke="#87828f" stroke-width="2" opacity=".3"/><path d="M32 10v6M32 48v6M10 32h6M48 32h6M16.7 16.7l4.2 4.2M43.1 43.1l4.2 4.2M16.7 47.3l4.2-4.2M43.1 20.9l4.2-4.2" stroke="#1b82bc" stroke-width="2" stroke-linecap="round" opacity=".4"/></svg>',
  };
  return c[kind]||c.search;
}

/* ── Modal ─────────────────────────────────────────────────────── */
var _modal={scrim:null,box:null};
function _openModal(titleHtml,subtitleHtml,iconColor,bodyHtml){
  if(!_modal.scrim){
    var s=document.createElement('div');s.className='modal-scrim';s.id='mscrim';
    s.onclick=function(e){if(e.target===s)_closeModal()};
    var b=document.createElement('div');b.className='modal-box';b.id='mbox';
    b.innerHTML='<div class="modal-mhead"><div class="icd" id="m-icd" style="background:'+esc(iconColor||'var(--accent)')+'"></div>'
      +'<div class="modal-mtt"><div class="mt" id="m-tt"></div><div class="ms" id="m-ts"></div></div>'
      +'<button class="modal-close" onclick="_closeModal()"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="3" y1="3" x2="13" y2="13"/><line x1="13" y1="3" x2="3" y2="13"/></svg></button>'
      +'</div><div class="modal-mbody" id="m-body"></div>';
    document.body.appendChild(s);document.body.appendChild(b);
    _modal.scrim=s;_modal.box=b;
  }
  document.getElementById('m-tt').innerHTML=titleHtml;
  document.getElementById('m-ts').innerHTML=subtitleHtml||'';
  document.getElementById('m-icd').style.background=iconColor||'var(--accent)';
  document.getElementById('m-body').innerHTML=bodyHtml;
  _modal.scrim.classList.add('open');_modal.box.classList.add('open');
  if(window.anime){
    anime({targets:_modal.box,scale:[0.96,1],opacity:[0,1],translateY:[-10,0],duration:320,easing:'easeOutCubic'});
  }
}
function _closeModal(){
  if(!_modal.box)return;
  if(window.anime){
    anime({targets:_modal.box,scale:[1,0.96],opacity:[1,0],duration:200,easing:'easeInCubic',complete:function(){
      _modal.scrim&&_modal.scrim.classList.remove('open');
      _modal.box&&_modal.box.classList.remove('open');
    }});
  } else {
    _modal.scrim&&_modal.scrim.classList.remove('open');
    _modal.box&&_modal.box.classList.remove('open');
  }
}
document.addEventListener('keydown',function(e){if(e.key==='Escape')_closeModal()});

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

  // Build dark status hero card
  const tick = M.ticket || 'Support Bundle';
  const org = M.organization || (CF && CF.organization_name) || '';
  const tier = M.mode ? (M.mode.charAt(0).toUpperCase() + M.mode.slice(1) + ' Tier') : '';
  const env = M.environment || '';
  const metaParts = [tier, env].filter(Boolean).join(' · ');
  const shAllOK = errs.length === 0 && (hKeys.length === 0 || hOK === hKeys.length);
  const shCritical = errs.length > 0 || (hKeys.length > 0 && hOK === 0);
  const shCls = shAllOK ? 'ok' : shCritical ? 'bad' : 'warn';
  const shLabel = shAllOK ? 'All Systems Normal.' : shCritical ? 'Issues Detected.' : 'Review Required.';
  const shEyebrow = ['Support Bundle', metaParts].filter(Boolean).join(' · ');
  let h = '<div class="status-hero"><div class="hero-grid-overlay"></div><div class="sh-inner">'
    + '<div class="sh-eyebrow">' + esc(shEyebrow) + '</div>'
    + '<div class="sh-title">' + esc(org || tick) + '</div>'
    + '<div class="sh-status sh-status-' + shCls + '">' + esc(shLabel) + '</div>'
    + (org && tick !== 'Support Bundle' ? '<div class="sh-ticket">' + esc(tick) + '</div>' : '')
    + '<div class="sh-stats">'
    + (hKeys.length ? '<div><div class="sh-sv ' + shCls + '">' + hOK + '<span style="font-size:1.1rem;font-weight:600;opacity:.5">/' + hKeys.length + '</span></div><div class="sh-sl">Health OK</div></div>' : '')
    + (adTotal > 0 ? '<div><div class="sh-sv ' + (adOK < adTotal ? 'warn' : 'ok') + '">' + adOK + '<span style="font-size:1.1rem;font-weight:600;opacity:.5">/' + adTotal + '</span></div><div class="sh-sl">Adapters</div></div>' : '')
    + (appTotal > 0 ? '<div><div class="sh-sv ' + (appOK < appTotal ? 'warn' : 'ok') + '">' + appOK + '<span style="font-size:1.1rem;font-weight:600;opacity:.5">/' + appTotal + '</span></div><div class="sh-sl">Apps</div></div>' : '')
    + '<div><div class="sh-sv ' + (logMatches > 50 ? 'bad' : logMatches > 0 ? 'warn' : 'ok') + '">' + (logMatches > 0 ? fmtNum(logMatches) : 'Clean') + '</div><div class="sh-sl">Log Matches</div></div>'
    + (errs.length > 0 ? '<div><div class="sh-sv bad">' + errs.length + '</div><div class="sh-sl">Errors</div></div>' : '')
    + '</div>'
    + '</div></div>';

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
        +'<div class="kv-val kv-desc" style="white-space:pre-wrap;line-height:1.7">'+esc(v)+'</div></div>';
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

  if(!ks.length){el.innerHTML=h+'<div class="empty-state">'+_svgEmpty("health")+'<div class="es-title">No health data collected</div><div class="es-sub">Run a capture with Platform credentials to populate this section.</div></div>';return}

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
  if(!d||!d.length)return'<div class="empty-state" style="padding:20px 0">'+_svgEmpty("health")+'<div class="es-title">No adapter data</div><div class="es-sub">Adapter list was not returned by the health endpoint.</div></div>';
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
  if(!d||!d.length)return'<div class="empty-state" style="padding:20px 0">'+_svgEmpty("health")+'<div class="es-title">No application data</div><div class="es-sub">Application list was not returned by the health endpoint.</div></div>';
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
    el.innerHTML=h+'<div class="empty-state">'+_svgEmpty("config")+'<div class="es-title">No config data</div><div class="es-sub">Configuration was not captured in this bundle.</div></div>';return;
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
  goodKs.forEach((k,i)=>filterLogs(i));

  const nb=document.getElementById('nb-logs');
  if(nb&&totalMatches){nb.textContent=fmtNum(totalMatches);nb.className='nav-badge warn';nb.style.display=''}
}

let _logFilters={};
function _searchBar(pi,tot){return _renderLogToolbar(pi,tot,[]);}
function _renderLogToolbar(pi,tot,allM){
  const fi=_logFilters['p'+pi]||{q:'',from:'',to:'',level:'all'};
  _logFilters['p'+pi]=fi;
  return'<div class="log-toolbar">'
    +'<div class="log-search-wrap">'
    +'<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="6.5" cy="6.5" r="4"/><line x1="10" y1="10" x2="14" y2="14"/></svg>'
    +'<input class="log-search-input" id="lsi'+pi+'" type="text" placeholder="Search log lines… (keywords, error messages, process names)" value="'+esc(fi.q||'')+'" oninput="_logFilters[\'p'+pi+'\'].q=this.value;filterLogs('+pi+')">'
    +'</div>'
    +'<div class="log-date-wrap">'
    +'<span class="log-date-label">From</span>'
    +'<input class="log-date-input" id="ldf'+pi+'" type="datetime-local" value="'+esc(fi.from||'')+'" title="Filter log entries after this time" onchange="_logFilters[\'p'+pi+'\'].from=this.value;filterLogs('+pi+')">'
    +'<span class="log-date-label">To</span>'
    +'<input class="log-date-input" id="ldt'+pi+'" type="datetime-local" value="'+esc(fi.to||'')+'" title="Filter log entries before this time" onchange="_logFilters[\'p'+pi+'\'].to=this.value;filterLogs('+pi+')">'
    +'</div>'
    +'<div class="log-level-chips">'
    +'<span class="log-chip chip-all'+(fi.level==='all'?' active':'')+'" onclick="_setLogLevel('+pi+',\'all\',this)">All</span>'
    +'<span class="log-chip chip-error'+(fi.level==='error'?' active':'')+'" onclick="_setLogLevel('+pi+',\'error\',this)"><span class="chip-dot"></span>Error</span>'
    +'<span class="log-chip chip-warn'+(fi.level==='warn'?' active':'')+'" onclick="_setLogLevel('+pi+',\'warn\',this)"><span class="chip-dot"></span>Warn</span>'
    +'<span class="log-chip chip-info'+(fi.level==='info'?' active':'')+'" onclick="_setLogLevel('+pi+',\'info\',this)"><span class="chip-dot"></span>Info</span>'
    +'</div>'
    +'</div>'
    +'<div class="log-toolbar-footer">'
    +'<span class="log-result-count" id="lsc'+pi+'"><b>'+fmtNum(tot)+'</b> entries</span>'
    +'<button class="log-clear-btn" id="lcl'+pi+'" onclick="_clearLogFilters('+pi+')">Clear filters</button>'
    +'</div>';
}
function _setLogLevel(pi,level,el){
  _logFilters['p'+pi]=_logFilters['p'+pi]||{};
  _logFilters['p'+pi].level=level;
  const wrap=el.closest('.log-level-chips');
  if(wrap)wrap.querySelectorAll('.log-chip').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  filterLogs(pi);
}
function _clearLogFilters(pi){
  _logFilters['p'+pi]={q:'',from:'',to:'',level:'all'};
  const si=document.getElementById('lsi'+pi);if(si)si.value='';
  const df=document.getElementById('ldf'+pi);if(df)df.value='';
  const dt=document.getElementById('ldt'+pi);if(dt)dt.value='';
  const wrap=document.getElementById('lsi'+pi);
  const chips=wrap&&wrap.closest('.log-toolbar')&&wrap.closest('.log-toolbar').querySelectorAll('.log-chip');
  if(chips){chips.forEach(c=>c.classList.remove('active'));const all=chips[0];if(all)all.classList.add('active');}
  filterLogs(pi);
}

function _renderLogSrc(key,d,pi){
  if(!d||typeof d!=='object')return'<div class="empty-state">'+_svgEmpty("logs")+'<div class="es-title">No log data</div><div class="es-sub">No entries were collected for this log source.</div></div>';

  // Webserver / raw entries array
  if(d.entries){
    const ents=d.entries||[];
    let h='<div class="stats-strip">'
      +'<div class="stat"><div class="stat-label">Log Entries</div><div class="stat-value">'+fmtNum(ents.length)+'</div></div>'
      +'</div>';
    h+=_searchBar(pi,ents.length);
    h+='<div class="card"><div class="card-body" style="padding:12px 16px">'
      +'<div class="log-list" id="lgl'+pi+'"></div>'
      +'</div></div>';
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

    h+=_searchBar(pi,allM.length);
    h+='<div class="log-list" id="lgl'+pi+'"></div>';
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

function _parseLogTs(line){
  // Try ISO-8601: 2024-01-15T14:23:45 or 2024-01-15 14:23:45
  const m=line.match(/(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})/);
  if(m)return new Date(m[1].replace(' ','T'));
  return null;
}
function _parseLogLevel(line,keywords){
  if(keywords&&keywords.length){
    const kl=(keywords[0]||'').toLowerCase();
    if(/error|critical|fatal|exception/.test(kl))return'error';
    if(/warn/.test(kl))return'warn';
    if(/info|notice|debug/.test(kl))return'info';
  }
  const ll=line.toLowerCase();
  if(/"level"\s*:\s*"(error|critical|fatal)"/i.test(line)||/\b(error|critical|fatal|exception)\b/.test(ll))return'error';
  if(/"level"\s*:\s*"(warn)/i.test(line)||/\b(warn|warning)\b/.test(ll))return'warn';
  if(/"level"\s*:\s*"(info|notice|debug)"/i.test(line))return'info';
  return null;
}
function _renderLogLine(m,lo,fromMs,toMs,level){
  const line=m.line||m||'';
  const kws=m.keywords||[];
  const ts=_parseLogTs(line);
  if(fromMs&&ts&&ts.getTime()<fromMs)return null;
  if(toMs&&ts&&ts.getTime()>toMs)return null;
  const lvl=_parseLogLevel(line,kws);
  if(level&&level!=='all'&&lvl!==level)return null;
  const lol=lo?lo.toLowerCase():'';
  if(lol&&!line.toLowerCase().includes(lol))return null;
  let inner='';
  if(ts){inner+='<span class="log-ts">'+ts.toISOString().slice(0,19).replace('T',' ')+'</span>';}
  if(lvl){inner+='<span class="log-lvl '+lvl+'">'+lvl+'</span>';}
  kws.forEach(kw=>{inner+='<span class="kw-tag">'+esc(kw)+'</span>'});
  let msg=line;
  // strip timestamp if we already rendered it
  if(ts)msg=msg.replace(/\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?/,'').replace(/^\s*[|]\s*/,'').trim();
  if(lol){
    const idx=msg.toLowerCase().indexOf(lol);
    if(idx>=0)msg=esc(msg.slice(0,idx))+'<mark>'+esc(msg.slice(idx,idx+lol.length))+'</mark>'+esc(msg.slice(idx+lol.length));
    else msg=esc(msg);
  } else {
    msg=esc(msg);
  }
  return'<div class="log-line"><span class="log-msg">'+inner+msg+'</span></div>';
}
function filterLogs(pi){
  const st=_logSt['p'+pi];if(!st)return;
  const fi=_logFilters['p'+pi]||{q:'',from:'',to:'',level:'all'};
  const cnt=document.getElementById('lsc'+pi);
  const box=document.getElementById('lgl'+pi);if(!box)return;
  const clr=document.getElementById('lcl'+pi);
  const lo=(fi.q||'').trim();
  const fromMs=fi.from?new Date(fi.from).getTime():0;
  const toMs=fi.to?new Date(fi.to).getTime():0;
  const level=fi.level||'all';
  const hasFilters=lo||fi.from||fi.to||(level&&level!=='all');
  if(clr)clr.classList.toggle('show',!!hasFilters);
  const src=st.matches.length?st.matches:st.raw.map(l=>({line:l,keywords:[]}));
  const parts=[];
  src.forEach(m=>{const r=_renderLogLine(m,lo,fromMs,toMs,level);if(r)parts.push(r)});
  if(cnt){cnt.innerHTML='<b>'+fmtNum(parts.length)+'</b>'+(hasFilters?' of <b>'+fmtNum(src.length)+'</b>':'')+' entries';}
  if(!parts.length){
    box.innerHTML='<div class="empty-state" style="padding:28px 0">'
      +_svgEmpty("search")
      +'<div class="es-title">No matching log entries</div>'
      +'<div class="es-sub">Try adjusting your search terms, date range, or level filter.</div>'
      +'</div>';
    return;
  }
  box.innerHTML=parts.slice(0,500).join('');
  if(parts.length>500){
    box.innerHTML+='<div class="log-line" style="color:var(--text-muted);font-style:italic;justify-content:center">…'+fmtNum(parts.length-500)+' more — refine filters to narrow results</div>';
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
    el.innerHTML=h+'<div class="card"><div class="card-body">'
      +'<div class="empty-state">'+_svgEmpty("system")
      +'<div class="es-title">System info unavailable</div>'
      +'<div class="es-sub">'+esc((SY&&(SY._tier_error||SY._error))||'Extended tier required to collect system info.')+'</div>'
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
function _initAnim(){
  if(!window.anime)return;
  // Scroll progress bar
  var prog=document.getElementById('scroll-prog');
  var main=document.getElementById('main');
  if(main&&prog){
    main.addEventListener('scroll',function(){
      var p=main.scrollTop/(main.scrollHeight-main.clientHeight)||0;
      prog.style.transform='scaleX('+Math.min(p,1)+')';
    });
  }
  // Hero entrance
  var hero=document.querySelector('.status-hero');
  if(hero){
    anime({targets:hero,opacity:[0,1],translateY:[24,0],duration:560,easing:'easeOutCubic'});
  }
  // Tile stagger
  var tiles=document.querySelectorAll('.tile');
  if(tiles.length){
    anime({targets:tiles,opacity:[0,1],translateY:[14,0],duration:400,delay:anime.stagger(60,{start:300}),easing:'easeOutCubic'});
  }
  // Hero card
  var hc=document.querySelector('.hero-card');
  if(hc){anime({targets:hc,opacity:[0,1],translateY:[10,0],duration:380,delay:500,easing:'easeOutCubic'});}
}

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
_initAnim();
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
        console.print(f"\n  [{theme.error}]✗[/{theme.error}] Failed to write bundle: {exc}\n")
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
