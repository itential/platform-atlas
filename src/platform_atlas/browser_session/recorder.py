"""
Troubleshooting browser recorder for ``support-bundle --browser-session``.

Flow
    1. A branded Chromium window opens on an Atlas welcome page (NOT on Platform). It explains what the
       window is for and what will be recorded. NOTHING is recorded until the user clicks "Start".
    2. Start -> the window navigates to Platform. The user logs in and reproduces the issue as normal.
       The window has a colored frame, a branded tab title/favicon, and a floating toolbar:
           Screenshot | Note | Mark | Pause | Finish        (keyboard: Alt+Shift+S / N / M / P / F)
    3. Recorded across every tab while recording (not while paused):
         console messages + JS errors, failed/slow/4xx-5xx requests, navigation timeline, screenshots
         (manual, on error, final), notes, marks, optional video, optional scrubbed HAR.
    4. Finish -> manifest.json (SHA-256 per artifact), SUMMARY.md, viewer.html and a .zip of the session.

Atlas credentials are never used: the human logs in, so SSO/MFA works. Playwright drives its own Chromium
build (one-time ``playwright install chromium``), which is what lets the Atlas browser theme load.

Privacy
    - Console text / URLs / notes are scrubbed for bearer tokens, JWTs and secret-looking query params.
    - HAR: bodies omitted, request POST data always dropped (login forms), auth/cookie headers redacted.
    - Screenshots and video can NOT be redacted - they show whatever is on screen. The intro explains this,
      the Pause button exists for it, and screenshots never include the Atlas toolbar itself.
    - Downloads the user triggers are saved under downloads/ but excluded from the zip unless
      ``include_downloads`` is set.
    - Native browser dialogs (alert/confirm/prompt) cannot be handed to the user by automation: alerts are
      accepted, confirm/prompt are DISMISSED (the safe choice) and the user is toasted + it is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:  # playwright is imported lazily in run_session() so the CLI never pays for it at startup
    from playwright.async_api import ConsoleMessage, Dialog, Download, Page, Request, Response

INTRO_URL = "https://atlas-recorder.invalid/welcome"
HOST_ID = "__atlas_host"
HIDE_OVERLAY_CSS = f"#{HOST_ID}{{display:none !important}}"

# ---------------------------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------------------------

_JWT_RE = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_QS = re.compile(r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|auth|session|sid|code)")
_SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-auth-token",
    "x-csrf-token", "x-xsrf-token", "x-csrftoken",
}
# Headers that carry a URL, which can hold a code/token in its query string.
_URL_HEADERS = {"referer", "location"}


def scrub_text(text: str) -> str:
    text = _JWT_RE.sub("[REDACTED-JWT]", text or "")
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", text)


def scrub_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.query:
            return scrub_text(url)
        query = [(k, "[REDACTED]" if _SECRET_QS.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return scrub_text(urlunsplit(parts._replace(query=urlencode(query, safe="[]-"))))
    except ValueError:
        return scrub_text(url)


def scrub_har(path: Path) -> None:
    """Rewrite a Playwright HAR in place with credentials and request bodies removed."""
    har = json.loads(path.read_text(encoding="utf-8"))

    def clean_headers(headers: list[dict]) -> None:
        for h in headers:
            name = h.get("name", "").lower()
            if name in _SENSITIVE_HEADERS:
                h["value"] = "[REDACTED]"
            elif name in _URL_HEADERS:
                h["value"] = scrub_url(h.get("value", ""))

    for entry in har.get("log", {}).get("entries", []):
        req, res = entry.get("request", {}), entry.get("response", {})
        req["url"] = scrub_url(req.get("url", ""))
        req["queryString"] = [
            {**q, "value": "[REDACTED]" if _SECRET_QS.search(q.get("name", "")) else q.get("value", "")}
            for q in req.get("queryString", [])
        ]
        clean_headers(req.get("headers", []))
        clean_headers(res.get("headers", []))
        req["cookies"], res["cookies"] = [], []
        if "postData" in req:  # login forms, token exchanges, anything with a body
            req["postData"] = {"mimeType": req["postData"].get("mimeType", ""), "text": "[OMITTED]"}
        if "redirectURL" in res:
            res["redirectURL"] = scrub_url(res["redirectURL"])
    path.write_text(json.dumps(har, indent=1, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Browser-side: intro page, overlay (frame + toolbar + modal), theme extension
# ---------------------------------------------------------------------------------------------

INTRO_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Atlas Troubleshooting Browser</title>
<style>html,body{height:100%;margin:0;background:#0A101E;
background-image:radial-gradient(1000px 520px at 12% 0%,rgba(77,180,234,.16),transparent 62%),
radial-gradient(760px 520px at 100% 100%,rgba(224,168,103,.09),transparent 60%)}</style></head><body></body></html>"""

THEME_MANIFEST = {
    "manifest_version": 3,
    "name": "Atlas Troubleshooting Browser Theme",
    "version": "1.0",
    "theme": {
        "colors": {
            "frame": [13, 20, 36], "frame_inactive": [17, 26, 44],
            "toolbar": [17, 26, 44], "tab_text": [233, 237, 245], "tab_background_text": [170, 181, 202],
            "bookmark_text": [233, 237, 245], "ntp_background": [10, 16, 30], "ntp_text": [233, 237, 245],
            "button_background": [224, 168, 103],
        }
    },
}

OVERLAY_JS = r"""
(() => {
  'use strict';
  if (window !== window.top || window.__atlasOverlay) return;
  window.__atlasOverlay = true;
  const HOST_ID = '__atlas_host';
  const TITLE_PREFIX = '● ATLAS · ';
  const SVGNS = 'http://www.w3.org/2000/svg';
  const call = async (action, payload) => {
    try { return await window.__atlas(action, payload || {}); } catch (e) { return null; }
  };

  const ICONS = {
    camera: [['path', {d: 'M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z'}], ['circle', {cx: 12, cy: 13, r: 3.5}]],
    note: [['path', {d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'}]],
    flag: [['path', {d: 'M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z'}], ['line', {x1: 4, y1: 22, x2: 4, y2: 15}]],
    pause: [['rect', {x: 6, y: 4, width: 4, height: 16}], ['rect', {x: 14, y: 4, width: 4, height: 16}]],
    play: [['polygon', {points: '6 3 20 12 6 21 6 3'}]],
    stop: [['rect', {x: 5, y: 5, width: 14, height: 14, rx: 2}]],
    min: [['line', {x1: 5, y1: 12, x2: 19, y2: 12}]],
    grip: [['circle', {cx: 9, cy: 6, r: 1}], ['circle', {cx: 15, cy: 6, r: 1}], ['circle', {cx: 9, cy: 12, r: 1}], ['circle', {cx: 15, cy: 12, r: 1}], ['circle', {cx: 9, cy: 18, r: 1}], ['circle', {cx: 15, cy: 18, r: 1}]],
  };
  function icon(name, size) {
    const s = document.createElementNS(SVGNS, 'svg');
    for (const [k, v] of Object.entries({viewBox: '0 0 24 24', width: size || 17, height: size || 17, fill: 'none', stroke: 'currentColor',
      'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) s.setAttribute(k, v);
    for (const [tag, attrs] of ICONS[name]) {
      const n = document.createElementNS(SVGNS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      s.appendChild(n);
    }
    return s;
  }
  // DOM helper (no innerHTML: sites with Trusted Types CSP would reject it)
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) { if (kid == null) continue; el.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
    return el;
  }

  // Bundled brand assets (fonts + Itential logo) arrive base64 from Python as window.__ATLAS_ASSETS.
  // Fonts go in through FontFace(ArrayBuffer) and the logo through createImageBitmap -> <canvas>: neither is
  // subject to the page's font-src / img-src CSP, so this works on any site.
  const ASSETS = window.__ATLAS_ASSETS || {};
  const b64bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let logoBitmap = null; const logoWaiters = [];
  function loadAssets() {
    try {
      for (const [fam, f] of Object.entries(ASSETS.fonts || {})) {
        new FontFace(fam, b64bytes(f.data).buffer, {weight: f.weight}).load().then((ff) => document.fonts.add(ff)).catch(() => {});
      }
      if (ASSETS.logo) {
        createImageBitmap(new Blob([b64bytes(ASSETS.logo)], {type: 'image/png'})).then((bm) => {
          logoBitmap = bm; logoWaiters.splice(0).forEach((c) => paintLogo(c));
        }).catch(() => {});
      }
    } catch (e) { /* assets are cosmetic; never break the page */ }
  }
  function paintLogo(canvas) {
    if (!logoBitmap) { logoWaiters.push(canvas); return; }
    const r = Math.max(2, Math.ceil(window.devicePixelRatio || 1));
    const w = parseInt(canvas.dataset.w, 10), hgt = Math.round(w * logoBitmap.height / logoBitmap.width);
    canvas.width = w * r; canvas.height = hgt * r;
    canvas.getContext('2d').drawImage(logoBitmap, 0, 0, canvas.width, canvas.height);
  }
  function logo(w) { const c = h('canvas', {class: 'mark', 'data-w': String(w), role: 'img', 'aria-label': 'Itential'}); paintLogo(c); return c; }

  const CSS = `
  :host{all:initial;
    --bg:#0A101E;--bg2:#0D1424;--s1:#111A2C;--s2:#16213A;--s3:#1D2A46;
    --line:rgba(150,172,214,.10);--line2:rgba(150,172,214,.19);
    --t1:#E9EDF5;--t2:#AAB5CA;--t3:#7B879F;--t4:#56627B;
    --brass:#E0A867;--brass-ink:#1C1206;--brass-hover:#ECBC84;--sky:#4DB4EA;
    --ok:#5CCB8F;--warn:#F2B544;--bad:#F2737C;
    --body:'AtlasHanken','Hanken Grotesk',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
    --display:'AtlasSora','Sora','AtlasHanken',system-ui,-apple-system,sans-serif}
  *{box-sizing:border-box;font-family:var(--body)}
  .disp{font-family:var(--display)}
  .frame{position:fixed;inset:0;pointer-events:none;z-index:2147483640;border:4px solid var(--accent);border-radius:0;transition:border-color .2s}
  .tag{position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:2147483641;pointer-events:none;background:var(--accent);color:var(--brass-ink);
       font-family:var(--display);font-size:10.5px;font-weight:600;letter-spacing:.09em;padding:3px 14px 4px;border-radius:0 0 10px 10px;white-space:nowrap;transition:background .2s}
  .tag.hidden,.dock.hidden,.modal.hidden,.panel.hidden,.frame.hidden{display:none}
  .dock{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483645;display:flex;align-items:center;gap:4px;
        background:var(--s1);color:var(--t1);border:1px solid var(--line2);border-radius:14px;padding:6px 8px;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 14px 36px -10px rgba(0,0,0,.7);user-select:none}
  .dock .grip{cursor:grab;color:var(--t4);display:flex;padding:4px 2px}
  .dock .grip:active{cursor:grabbing}
  .dock canvas.mark{width:20px;height:23px;margin:0 4px 0 2px;flex:none}
  .rec{display:flex;align-items:center;gap:7px;font-family:var(--display);font-weight:600;font-size:12.5px;padding:0 8px;font-variant-numeric:tabular-nums;min-width:86px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--bad);box-shadow:0 0 0 3px rgba(242,115,124,.18);animation:pulse 1.3s infinite}
  .paused .dot{background:var(--t3);box-shadow:none;animation:none}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  .sep{width:1px;align-self:stretch;background:var(--line2);margin:3px 3px}
  button{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:7px;font-family:var(--body);font-size:12.5px;font-weight:600;color:var(--t1);
         padding:8px 11px;border-radius:9px;transition:background .12s,transform .12s}
  button:hover{background:var(--s3)}
  button:active{transform:scale(.97)}
  button:focus-visible{outline:2px solid var(--sky);outline-offset:1px}
  button.primary,button.go{background:var(--brass);color:var(--brass-ink)}
  button.primary:hover,button.go:hover{background:var(--brass-hover)}
  button.danger{background:var(--bad);color:#2A0A0D;box-shadow:0 0 0 1px rgba(242,115,124,.55),0 4px 16px -4px rgba(242,115,124,.55)}
  button.danger:hover{background:#F78C93}
  button[disabled]{opacity:.38;cursor:not-allowed}
  button[disabled]:hover{background:inherit}
  button.go[disabled]:hover,button.primary[disabled]:hover{background:var(--brass)}
  .stats{display:flex;gap:11px;font-size:11.5px;color:var(--t3);padding:0 6px;font-variant-numeric:tabular-nums}
  .stats span{white-space:nowrap}.stats b{color:var(--t1);font-weight:600}.stats .bad b{color:var(--bad)}
  .dock.min .full{display:none}
  .toast{position:fixed;left:50%;top:34px;transform:translateX(-50%);z-index:2147483646;background:var(--s2);color:var(--t1);border:1px solid var(--line2);
         border-left:3px solid var(--ok);font-weight:600;font-size:13px;padding:10px 16px;border-radius:10px;box-shadow:0 12px 30px -8px rgba(0,0,0,.7);
         opacity:0;pointer-events:none;transition:opacity .2s;max-width:70vw}
  .toast.show{opacity:1}.toast.warn{border-left-color:var(--warn)}
  .panel{position:fixed;left:50%;bottom:86px;transform:translateX(-50%);z-index:2147483646;width:min(440px,92vw);background:var(--s1);color:var(--t1);
         border:1px solid var(--line2);border-radius:14px;padding:16px;box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 24px 60px -16px rgba(0,0,0,.75)}
  .panel h3{margin:0 0 4px;font-family:var(--display);font-size:15px;font-weight:600;letter-spacing:-.01em;color:var(--t1)}
  .panel p{margin:0 0 10px;font-size:12.5px;color:var(--t2);line-height:1.45}
  textarea{width:100%;min-height:78px;resize:vertical;background:var(--bg2);color:var(--t1);border:1px solid var(--line2);border-radius:10px;padding:10px;font-size:13px;line-height:1.45}
  textarea::placeholder{color:var(--t4)}
  textarea:focus{outline:2px solid var(--sky);outline-offset:0;border-color:transparent}
  label.chk{display:flex;gap:9px;align-items:flex-start;font-size:12.5px;color:var(--t2);margin:10px 0;line-height:1.4;cursor:pointer}
  label.chk input{margin-top:2px;accent-color:var(--brass)}
  .row{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}
  .modal{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;padding:18px;overflow:auto;background:rgba(5,8,16,.62)}
  .card{width:min(800px,100%);background:var(--s1);color:var(--t1);border:1px solid var(--line2);border-radius:20px;overflow:hidden;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 40px 90px -30px rgba(0,0,0,.85);max-height:calc(100vh - 36px);overflow-y:auto}
  .hdr{display:flex;align-items:center;gap:14px;padding:18px 32px;border-bottom:1px solid var(--line);
       background:radial-gradient(120% 160% at 0% 0%,rgba(77,180,234,.10),transparent 60%),var(--bg2)}
  .hdr canvas.mark{width:36px;height:42px;flex:none}
  .hdr .name{display:flex;flex-direction:column;line-height:1.15;min-width:0}
  .hdr strong{font-family:var(--display);font-weight:600;font-size:21px;letter-spacing:-.015em;color:var(--t1)}
  .hdr small{font-size:12px;font-weight:500;letter-spacing:.02em;color:var(--t3);margin-top:3px}
  .body{padding:22px 32px 26px}
  .lead{font-size:14px;line-height:1.6;color:var(--t2);margin:0 0 16px}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media (max-width:640px){.cols{grid-template-columns:1fr}}
  .box{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
  .box h2{margin:0 0 8px;font-family:var(--display);font-size:10.5px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--sky)}
  .box ul{margin:0;padding-left:17px;font-size:12.8px;line-height:1.6;color:var(--t2)}
  .box li::marker{color:var(--t4)}
  .steps{display:flex;gap:12px;margin:16px 0 4px;flex-wrap:wrap}
  .step{flex:1 1 150px;display:flex;gap:10px;align-items:flex-start;font-size:12.5px;color:var(--t2);line-height:1.45}
  .step i{flex:none;width:22px;height:22px;border-radius:8px;background:rgba(224,168,103,.14);color:var(--brass);font-style:normal;font-family:var(--display);font-weight:600;font-size:12px;display:flex;align-items:center;justify-content:center}
  .note-warn{margin-top:14px;font-size:12.5px;line-height:1.5;color:var(--warn);background:rgba(242,181,68,.10);border:1px solid rgba(242,181,68,.28);border-radius:10px;padding:10px 12px}
  .target{font-family:ui-monospace,'JetBrains Mono',Menlo,Consolas,monospace;font-size:11.5px;color:var(--sky)}
  .actions{display:flex;gap:10px;justify-content:flex-end;align-items:center;margin-top:16px}
  .actions button{padding:10px 18px;font-size:13.5px;border-radius:10px}
  `;

  let host, root, S = null, built = false, offsetTimer = null;
  const el = {};

  function build() {
    host = document.getElementById(HOST_ID);
    if (host) return;
    host = h('div', {id: HOST_ID});
    root = host.attachShadow({mode: 'open'});
    if (root.adoptedStyleSheets !== undefined && typeof CSSStyleSheet !== 'undefined') {
      const sheet = new CSSStyleSheet(); sheet.replaceSync(CSS); root.adoptedStyleSheets = [sheet];  // CSP-proof
    } else { root.appendChild(h('style', {}, CSS)); }

    el.frame = h('div', {class: 'frame hidden'});
    el.tag = h('div', {class: 'tag hidden'});
    el.timer = h('span', {}, '00:00');
    el.dot = h('span', {class: 'dot'});
    el.state = h('span', {}, 'REC');
    el.grip = h('div', {class: 'grip', title: 'Drag to move'}, icon('grip', 16));
    el.btnShot = h('button', {title: 'Screenshot (Alt+Shift+S)', onclick: () => doShot()}, icon('camera'), h('span', {class: 'full'}, 'Screenshot'));
    el.btnNote = h('button', {title: 'Add a note (Alt+Shift+N)', onclick: () => togglePanel('note')}, icon('note'), h('span', {class: 'full'}, 'Note'));
    el.btnMark = h('button', {title: 'Mark this moment (Alt+Shift+M)', onclick: () => doMark()}, icon('flag'), h('span', {class: 'full'}, 'Mark'));
    el.pauseIcon = h('span', {}, icon('pause'));
    el.pauseLbl = h('span', {class: 'full'}, 'Pause');
    el.btnPause = h('button', {title: 'Pause / resume recording (Alt+Shift+P)', onclick: () => doPause()}, el.pauseIcon, el.pauseLbl);
    el.btnFinish = h('button', {class: 'danger', title: 'Finish and save (Alt+Shift+F)', onclick: () => togglePanel('finish')}, icon('stop'), h('span', {class: 'full'}, 'Finish'));
    el.btnMin = h('button', {title: 'Minimize toolbar', onclick: () => el.dock.classList.toggle('min')}, icon('min'));
    el.sErr = h('b', {}, '0'); el.sNet = h('b', {}, '0'); el.sShot = h('b', {}, '0');
    el.stats = h('div', {class: 'stats full'},
      h('span', {class: 'bad'}, 'errors ', el.sErr), h('span', {class: 'bad'}, 'req issues ', el.sNet), h('span', {}, 'shots ', el.sShot));
    el.dock = h('div', {class: 'dock hidden'},
      el.grip, logo(20), h('div', {class: 'rec'}, el.dot, el.state, ' ', el.timer),
      h('div', {class: 'sep full'}), el.btnShot, el.btnNote, el.btnMark, el.btnPause,
      h('div', {class: 'sep full'}), el.stats, h('div', {class: 'sep full'}), el.btnFinish, el.btnMin);
    el.toast = h('div', {class: 'toast'});
    el.panel = h('div', {class: 'panel hidden'});
    el.modal = h('div', {class: 'modal hidden'});
    root.append(el.frame, el.tag, el.dock, el.panel, el.toast, el.modal);
    makeDraggable();
    (document.documentElement || document).appendChild(host);
  }

  function makeDraggable() {
    let sx, sy, ox, oy, drag = false;
    el.grip.addEventListener('pointerdown', (e) => {
      const r = el.dock.getBoundingClientRect();
      drag = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
      el.dock.style.transform = 'none'; el.dock.style.left = ox + 'px'; el.dock.style.top = oy + 'px'; el.dock.style.bottom = 'auto';
      el.grip.setPointerCapture(e.pointerId);
    });
    el.grip.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const x = Math.max(0, Math.min(window.innerWidth - 80, ox + e.clientX - sx));
      const y = Math.max(0, Math.min(window.innerHeight - 50, oy + e.clientY - sy));
      el.dock.style.left = x + 'px'; el.dock.style.top = y + 'px';
    });
    el.grip.addEventListener('pointerup', () => { drag = false; });
  }

  function toast(msg, warn) {
    if (!msg) return;
    el.toast.textContent = msg; el.toast.classList.toggle('warn', !!warn); el.toast.classList.add('show');
    clearTimeout(offsetTimer); offsetTimer = setTimeout(() => el.toast.classList.remove('show'), 2600);
  }
  const fmt = (s) => { s = Math.max(0, Math.floor(s)); const m = Math.floor(s / 60); return String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };

  function apply(res) {
    if (!res) return;
    if (res.state) S = res.state;
    if (res.toast) toast(res.toast, res.warn);
    if (!S) return;
    const phase = S.phase, rec = phase === 'recording', paused = phase === 'paused', live = rec || paused;
    const accent = rec ? '#E0A867' : paused ? '#7B879F' : '#4DB4EA';
    host.style.setProperty('--accent', accent);
    host.style.cssText = '--accent:' + accent + ';all:initial';
    el.frame.classList.toggle('hidden', !(live || phase === 'finishing'));
    el.tag.classList.toggle('hidden', !(live || phase === 'finishing'));
    el.tag.textContent = 'PLATFORM ATLAS · TROUBLESHOOTING BROWSER · ' + (phase === 'finishing' ? 'SAVING…' : paused ? 'PAUSED' : 'RECORDING');
    el.dock.classList.toggle('hidden', !live);
    el.dock.classList.toggle('paused', paused);
    el.state.textContent = paused ? 'PAUSED' : 'REC';
    el.timer.textContent = fmt(S.elapsed);
    el.sErr.textContent = S.counts.errors; el.sNet.textContent = S.counts.net_issues; el.sShot.textContent = S.counts.screenshots;
    el.pauseIcon.replaceChildren(icon(paused ? 'play' : 'pause')); el.pauseLbl.textContent = paused ? 'Resume' : 'Pause';
    el.btnShot.disabled = el.btnNote.disabled = el.btnMark.disabled = paused;
    if (phase === 'intro') showIntro(); else if (!el.modal.classList.contains('hidden') && phase !== 'intro') el.modal.classList.add('hidden');
    if (!live) closePanel();
  }

  async function doShot() { apply(await call('screenshot')); }
  async function doMark() { apply(await call('mark', {})); }
  async function doPause() { apply(await call(S && S.phase === 'paused' ? 'resume' : 'pause')); }

  function closePanel() { el.panel.classList.add('hidden'); el.panel.replaceChildren(); el.panelKind = null; }
  function togglePanel(kind) {
    if (el.panelKind === kind) return closePanel();
    closePanel(); el.panelKind = kind; el.panel.classList.remove('hidden');
    if (kind === 'note') {
      const ta = h('textarea', {placeholder: 'e.g. "Clicked Save on the workflow and got a red banner"', maxlength: '2000'});
      const shot = h('input', {type: 'checkbox', checked: ''});
      const save = async () => { const t = ta.value.trim(); if (!t) return ta.focus(); closePanel(); apply(await call('note', {text: t, screenshot: shot.checked})); };
      ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') save(); e.stopPropagation(); });
      ta.addEventListener('keyup', (e) => e.stopPropagation());
      el.panel.append(h('h3', {}, 'Add a note'), h('p', {}, 'Describe what you just did or what looks wrong. It is saved with a timestamp and the current page.'), ta,
        h('label', {class: 'chk'}, shot, 'Attach a screenshot of this page'),
        h('div', {class: 'row'}, h('button', {onclick: closePanel}, 'Cancel'), h('button', {class: 'primary', onclick: save}, 'Save note')));
      setTimeout(() => ta.focus(), 30);
    } else {
      const c = S ? S.counts : {errors: 0, net_issues: 0, screenshots: 0, notes: 0};
      el.panel.append(h('h3', {}, 'Finish and save this session?'),
        h('p', {}, 'Captured so far: ' + c.errors + ' errors, ' + c.net_issues + ' request issues, ' + c.screenshots + ' screenshots, ' + c.notes + ' notes. The browser will close and the session will be saved on your computer.'),
        h('div', {class: 'row'}, h('button', {onclick: closePanel}, 'Keep recording'), h('button', {class: 'go', onclick: async () => { closePanel(); apply(await call('finish')); }}, 'Finish & save')));
    }
  }

  function list(items) { return h('ul', {}, items.map((t) => h('li', {}, t))); }
  function showIntro() {
    if (el.introBuilt) { el.modal.classList.remove('hidden'); return; }
    el.introBuilt = true;
    const cfg = S.config;
    const rec = ['Browser console messages and JavaScript errors', 'Failed, slow and error (4xx/5xx) network requests', 'Pages you visit, with timestamps',
      'Screenshots — yours, plus automatic ones when an error occurs', 'Notes and marks you add'];
    if (cfg.video) rec.push('A video of this window');
    if (cfg.har) rec.push('Network activity (headers redacted, request bodies removed)');
    const never = ['Nothing is recorded until you press Start', 'Passwords and tokens are scrubbed from logs', 'Nothing is uploaded — files stay on this computer until you share them',
      'Other windows and apps are never captured', 'Pause at any time before opening anything sensitive'];
    const desc = h('textarea', {placeholder: 'Optional — briefly describe the problem you are seeing', maxlength: '2000'});
    const ack = h('input', {type: 'checkbox'});
    const start = h('button', {class: 'go', disabled: '', onclick: async () => { start.disabled = true; apply(await call('start', {description: desc.value.trim()})); }}, 'Start recording');
    ack.addEventListener('change', () => { start.disabled = !ack.checked; });
    desc.addEventListener('keydown', (e) => e.stopPropagation());
    el.modal.append(h('div', {class: 'card', role: 'dialog', 'aria-label': 'Atlas Troubleshooting Browser'},
      h('div', {class: 'hdr'}, logo(36), h('div', {class: 'name'}, h('strong', {}, 'Platform Atlas'), h('small', {}, 'Troubleshooting Browser'))),
      h('div', {class: 'body'},
      h('p', {class: 'lead'}, 'This window was opened by Platform Atlas to help you show Itential Support exactly what is going wrong in Itential Platform. Use it like any browser \u2014 Atlas quietly keeps a diagnostic record so you do not have to describe or reproduce the issue twice.'),
      h('div', {class: 'cols'}, h('div', {class: 'box'}, h('h2', {}, 'What gets recorded'), list(rec)), h('div', {class: 'box'}, h('h2', {}, 'Your control'), list(never))),
      h('div', {class: 'steps'},
        h('div', {class: 'step'}, h('i', {}, '1'), h('span', {}, ['Press Start. Platform opens here (', h('span', {class: 'target'}, cfg.target), ') — sign in as usual.'])),
        h('div', {class: 'step'}, h('i', {}, '2'), h('span', {}, 'Reproduce the problem, step by step.')),
        h('div', {class: 'step'}, h('i', {}, '3'), h('span', {}, 'Use the toolbar: Screenshot, Note, Mark, Pause.')),
        h('div', {class: 'step'}, h('i', {}, '4'), h('span', {}, 'Press Finish. Atlas saves everything for your support case.'))),
      h('div', {class: 'note-warn'}, 'Screenshots' + (cfg.video ? ' and video' : '') + ' show whatever is on screen, including customer or personal data, and cannot be redacted automatically. Press Pause before viewing anything you do not want captured.'),
      h('div', {style: 'margin-top:14px'}, desc),
      h('label', {class: 'chk'}, ack, 'I understand what will be recorded and that I choose what to share.'),
      h('div', {class: 'actions'}, h('button', {onclick: async () => { await call('cancel'); }}, 'Cancel'), start))));
    el.modal.classList.remove('hidden');
  }

  // keep tab title / favicon branded so the window is recognisable in the tab strip and Dock/taskbar
  const FAVICON = ASSETS.favicon || ('data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="12" fill="#E0A867"/></svg>'));
  function brandTab() {
    try {
      if (document.title && !document.title.startsWith(TITLE_PREFIX) && !document.title.startsWith('Atlas Troubleshooting')) document.title = TITLE_PREFIX + document.title;
      const head = document.head; if (!head) return;
      let ours = head.querySelector('link[data-atlas-icon]');
      if (!ours) { ours = document.createElement('link'); ours.rel = 'icon'; ours.type = 'image/svg+xml'; ours.href = FAVICON; ours.setAttribute('data-atlas-icon', '1'); head.appendChild(ours); }
      head.querySelectorAll('link[rel~="icon"]:not([data-atlas-icon]),link[rel="shortcut icon"]').forEach((n) => n.remove());
    } catch (e) { /* never break the page */ }
  }

  async function tick() {
    if (!host || !host.isConnected) { host = null; build(); }
    apply(await call('state'));
    brandTab();
  }
  document.addEventListener('keydown', (e) => {
    if (!(e.altKey && e.shiftKey) || !S) return;
    const live = S.phase === 'recording', paused = S.phase === 'paused';
    const map = {KeyS: () => live && doShot(), KeyM: () => live && doMark(), KeyN: () => live && togglePanel('note'), KeyP: () => (live || paused) && doPause(), KeyF: () => (live || paused) && togglePanel('finish')};
    if (map[e.code]) { e.preventDefault(); map[e.code](); }
  }, true);

  function boot() { loadAssets(); build(); tick(); setInterval(tick, 1000); }
  if (document.documentElement) boot(); else document.addEventListener('readystatechange', () => { if (!host) boot(); });
  document.addEventListener('DOMContentLoaded', () => { if (!host || !host.isConnected) { host = null; build(); } tick(); });
})();
"""


ASSET_DIR = Path(__file__).resolve().parent / "assets"


def load_overlay_assets() -> str:
    """JS prelude exposing the bundled brand assets (Itential logo + Sora/Onest/Hanken Grotesk, from the WebUI's
    Modern theme) as base64. Missing files just mean a plainer overlay - assets are cosmetic."""
    import base64

    def b64(name: str) -> str | None:
        f = ASSET_DIR / name
        return base64.b64encode(f.read_bytes()).decode("ascii") if f.is_file() else None

    fonts = {fam: {"data": data, "weight": weight} for fam, name, weight in (
        ("AtlasSora", "sora-latin.woff2", "100 800"), ("AtlasHanken", "hanken-grotesk-latin.woff2", "100 900"),
        ("AtlasOnest", "onest-latin.woff2", "100 900")) if (data := b64(name))}
    logo = b64("itential-logo.png")
    assets = {"fonts": fonts, "logo": logo, "favicon": f"data:image/png;base64,{logo}" if logo else None}
    return f"window.__ATLAS_ASSETS = {json.dumps(assets)};\n"


def make_theme_dir(parent: Path) -> Path:
    theme = parent / "atlas_theme"
    theme.mkdir(parents=True, exist_ok=True)
    (theme / "manifest.json").write_text(json.dumps(THEME_MANIFEST), encoding="utf-8")
    return theme


# ---------------------------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Recorder:
    """Owns session state (intro/recording/paused/finishing), all event sinks and the overlay protocol."""

    MAX_AUTO_SHOTS = 20
    AUTO_SHOT_MIN_GAP = 5.0

    def __init__(self, out_dir: Path, args: SessionOptions, target_url: str) -> None:
        self.out = out_dir
        self.args = args
        self.target = target_url
        self.shots_dir = out_dir / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir = out_dir / "downloads"
        self.phase = "intro"  # intro -> recording <-> paused -> finishing | cancelled
        self.description = ""
        self.pages: list[Page] = []
        self.finish_event = asyncio.Event()
        self.t0_ctx = monotonic()  # video clock origin
        self._rec_started: float | None = None
        self._elapsed_before_pause = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._shot_n = self._auto_n = self._mark_n = 0
        self._last_auto = 0.0
        self.stop_reason = "browser_closed"
        self.counts = Counter()
        self.notes: list[dict] = []
        self.visited: list[str] = []
        self._fh = {n: (out_dir / f).open("w", encoding="utf-8") for n, f in
                    {"console": "console.ndjson", "net": "network_issues.ndjson", "timeline": "timeline.ndjson"}.items()}
        self._console_txt = (out_dir / "console.log").open("w", encoding="utf-8")

    # -- state ------------------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.phase == "recording"

    @property
    def elapsed(self) -> float:
        extra = (monotonic() - self._rec_started) if (self._rec_started and self.phase == "recording") else 0.0
        return self._elapsed_before_pause + extra

    def public_state(self) -> dict:
        c = self.counts
        return {
            "phase": self.phase, "elapsed": round(self.elapsed, 1),
            "counts": {"errors": c["console_errors"] + c["pageerror"], "net_issues": c["http_errors"] + c["failed_requests"] + c["slow_requests"],
                       "screenshots": c["screenshots"], "notes": c["notes"], "marks": c["marks"]},
            "config": {"video": self.args.video, "har": self.args.har, "target": urlsplit(self.target).netloc or self.target},
        }

    def _set_phase(self, phase: str) -> None:
        if phase == "recording" and self.phase != "recording":
            self._rec_started = monotonic()
        elif self.phase == "recording" and phase != "recording":
            self._elapsed_before_pause = self.elapsed
            self._rec_started = None
        self.phase = phase

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- overlay protocol (window.__atlas(action, payload)) -----------------------------------
    async def on_overlay_call(self, source: dict, action: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        page: Page = source["page"]
        toast: str | None = None
        warn = False
        if action == "start" and self.phase == "intro":
            self.description = scrub_text(payload.get("description", ""))[:2000]
            self._set_phase("recording")
            self.timeline("session_start", description=self.description)
            if self.description:
                self.notes.append({"ts": _now(), "text": self.description, "page": "(intro)", "kind": "description"})
                self.counts["notes"] += 1
            self._spawn(self._goto_target(page))
        elif action == "cancel" and self.phase == "intro":
            self.stop_reason = "cancelled"
            self._set_phase("cancelled")
            self.finish_event.set()
        elif action == "screenshot" and self.recording:
            path = await self.screenshot(page, "manual")
            toast = f"Screenshot saved ({path.name})" if path else "Screenshot failed"
            warn = path is None
        elif action == "mark" and self.recording:
            self._mark_n += 1
            self.counts["marks"] += 1
            label = payload.get("label") or f"Mark {self._mark_n}"
            self.timeline("mark", label=label, page=scrub_url(page.url))
            toast = f"{label} added"
        elif action == "note" and self.recording:
            text = scrub_text(str(payload.get("text", "")).strip())[:2000]
            if text:
                shot = await self.screenshot(page, "note") if payload.get("screenshot") else None
                note = {"ts": _now(), "text": text, "page": scrub_url(page.url), "screenshot": shot.name if shot else None}
                self.notes.append(note)
                self.counts["notes"] += 1
                self.timeline("note", **{k: v for k, v in note.items() if k != "ts"})
                toast = "Note saved" + (" with screenshot" if shot else "")
        elif action == "pause" and self.recording:
            self._set_phase("paused")
            self.timeline("pause")
            toast, warn = "Recording paused — nothing is being logged", True
        elif action == "resume" and self.phase == "paused":
            self._set_phase("recording")
            self.timeline("resume")
            toast = "Recording resumed"
        elif action == "finish" and self.phase in ("recording", "paused"):
            if self.phase == "recording":
                await self.screenshot(page, "final")
            self.timeline("session_finish")
            self.stop_reason = "user_finished"
            self._set_phase("finishing")
            self.finish_event.set()
        return {"state": self.public_state(), "toast": toast, "warn": warn}

    async def _goto_target(self, page: Page) -> None:
        try:
            await page.goto(self.target, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:
            self.timeline("navigation_error", url=scrub_url(self.target), error=str(exc)[:300])

    # -- sinks ------------------------------------------------------------------------------
    def _write(self, name: str, record: dict) -> None:
        fh = self._fh[name]
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()

    def timeline(self, kind: str, **fields) -> None:
        self._write("timeline", {"ts": _now(), "t": round(self.elapsed, 1), "video_t": round(monotonic() - self.t0_ctx, 1), "kind": kind, **fields})

    def attach(self, page: Page) -> None:
        if page in self.pages:
            return
        self.pages.append(page)
        page.on("console", lambda m: self._on_console(page, m))
        page.on("pageerror", lambda e: self._on_pageerror(page, e))
        page.on("requestfailed", self._on_request_failed)
        page.on("requestfinished", self._on_request_finished)
        page.on("response", self._on_response)
        page.on("framenavigated", lambda f: self._on_nav(page, f))
        page.on("crash", lambda _p: self.timeline("page_crash", page=scrub_url(page.url)) if self.recording else None)
        page.on("popup", lambda p: self.timeline("popup", url=scrub_url(p.url)) if self.recording else None)
        page.on("dialog", lambda d: self._spawn(self._on_dialog(page, d)))
        page.on("download", lambda d: self._spawn(self._on_download(d)))
        page.on("close", lambda _p: self.pages.remove(page) if page in self.pages else None)

    def _on_console(self, page: Page, msg: ConsoleMessage) -> None:
        if not self.recording:
            return
        loc = msg.location or {}
        rec = {"ts": _now(), "type": msg.type, "text": scrub_text(msg.text), "page": scrub_url(page.url),
               "source": f"{scrub_url(loc.get('url', ''))}:{loc.get('lineNumber', '')}:{loc.get('columnNumber', '')}"}
        self.counts["console"] += 1
        if msg.type == "error":
            self.counts["console_errors"] += 1
        elif msg.type == "warning":
            self.counts["console_warnings"] += 1
        self._write("console", rec)
        self._console_txt.write(f"{rec['ts']} [{rec['type'].upper():7}] {rec['text']}\n")
        self._console_txt.flush()

    def _on_pageerror(self, page: Page, err) -> None:
        if not self.recording:
            return
        rec = {"ts": _now(), "type": "pageerror", "text": scrub_text(str(getattr(err, "message", err))),
               "stack": scrub_text(getattr(err, "stack", "") or ""), "page": scrub_url(page.url)}
        self.counts["pageerror"] += 1
        self._write("console", rec)
        self._console_txt.write(f"{rec['ts']} [PAGEERR] {rec['text']}\n")
        self._console_txt.flush()
        self._maybe_auto_shot(page)

    def _on_request_failed(self, req: Request) -> None:
        if not self.recording or req.url.startswith(INTRO_URL):
            return
        self.counts["failed_requests"] += 1
        self._write("net", {"ts": _now(), "kind": "failed", "method": req.method, "url": scrub_url(req.url),
                            "resource": req.resource_type, "failure": req.failure})

    def _on_response(self, res: Response) -> None:
        if not self.recording or res.status < 400:
            return
        self.counts["http_errors"] += 1
        self._write("net", {"ts": _now(), "kind": "http_error", "status": res.status, "statusText": res.status_text,
                            "method": res.request.method, "url": scrub_url(res.url), "resource": res.request.resource_type})

    def _on_request_finished(self, req: Request) -> None:
        if not self.recording or not self.args.slow_ms:
            return
        total = (req.timing or {}).get("responseEnd", -1)
        if total and total >= self.args.slow_ms:
            self.counts["slow_requests"] += 1
            self._write("net", {"ts": _now(), "kind": "slow", "ms": round(total), "method": req.method,
                                "url": scrub_url(req.url), "resource": req.resource_type})

    def _on_nav(self, page: Page, frame) -> None:
        if frame != page.main_frame or not self.recording or frame.url.startswith(INTRO_URL) or frame.url == "about:blank":
            return
        url = scrub_url(frame.url)
        if not self.visited or self.visited[-1] != url:
            self.visited.append(url)
            self.timeline("navigation", url=url)

    async def _on_dialog(self, page: Page, dialog: Dialog) -> None:
        # Automation cannot hand a native dialog to the user. alert/beforeunload: accept. confirm/prompt: dismiss (safe).
        accept = dialog.type in ("alert", "beforeunload")
        if self.recording:
            self.timeline("native_dialog", dialog_type=dialog.type, message=scrub_text(dialog.message)[:300], action="accepted" if accept else "dismissed")
        try:
            await (dialog.accept() if accept else dialog.dismiss())
        except Exception:
            pass

    async def _on_download(self, download: Download) -> None:
        self.downloads_dir.mkdir(exist_ok=True)
        name = download.suggested_filename
        try:
            await download.save_as(self.downloads_dir / name)
            if self.recording:
                self.timeline("download", file=name)
        except Exception as exc:
            if self.recording:
                self.timeline("download_failed", file=name, error=str(exc)[:200])

    # -- screenshots ------------------------------------------------------------------------
    def _maybe_auto_shot(self, page: Page) -> None:
        if not self.args.auto_shots or self._auto_n >= self.MAX_AUTO_SHOTS:
            return
        now = monotonic()
        if now - self._last_auto < self.AUTO_SHOT_MIN_GAP:
            return
        self._last_auto = now
        self._auto_n += 1
        self._spawn(self.screenshot(page, "pageerror"))

    async def screenshot(self, page: Page | None, reason: str) -> Path | None:
        page = page or (self.pages[-1] if self.pages else None)
        if page is None or page.is_closed() or page.url.startswith(INTRO_URL):
            return None
        self._shot_n += 1
        path = self.shots_dir / f"{self._shot_n:03d}_{reason}.png"
        try:
            await page.screenshot(path=str(path), style=HIDE_OVERLAY_CSS)  # the Atlas toolbar/frame never appear in screenshots
        except Exception as exc:
            print(f"  ! screenshot failed ({reason}): {exc}")
            return None
        self.counts["screenshots"] += 1
        self.timeline("screenshot", file=path.name, reason=reason, page=scrub_url(page.url))
        print(f"  + screenshot: {path.relative_to(self.out)}")
        return path

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def close(self) -> None:
        for fh in (*self._fh.values(), self._console_txt):
            fh.close()


# ---------------------------------------------------------------------------------------------
# SUMMARY.md
# ---------------------------------------------------------------------------------------------

def _path_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc}{p.path}" if p.netloc else url


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_summary(out: Path, manifest: dict, rec: Recorder) -> Path:
    console, net = _read_ndjson(out / "console.ndjson"), _read_ndjson(out / "network_issues.ndjson")
    timeline = _read_ndjson(out / "timeline.ndjson")
    L: list[str] = ["# Atlas Troubleshooting Session", ""]
    L += [f"- **Target:** {manifest['target_url']}", f"- **Started:** {manifest['started']}  \n- **Ended:** {manifest['ended']}",
          f"- **Recorded time:** {int(rec.elapsed // 60)}m {int(rec.elapsed % 60)}s  \n- **Stop reason:** {manifest['stop_reason']}",
          f"- **Browser:** {manifest['browser']['version']} ({manifest['browser']['user_agent']})", ""]
    if rec.notes:
        L += ["## Reporter notes", ""]
        for n in rec.notes:
            extra = f" _(screenshot: {n['screenshot']})_" if n.get("screenshot") else ""
            L.append(f"- `{n['ts'][11:19]}` {n['text']}{extra}  \n  <sub>{n['page']}</sub>")
        L.append("")
    errs = Counter((r["type"], r["text"][:200]) for r in console if r["type"] in ("error", "pageerror"))
    L += ["## Console errors", ""]
    L += [f"- ×{c} `{t}` {txt}" for (t, txt), c in errs.most_common(15)] or ["_None recorded._"]
    L += ["", "## HTTP errors", ""]
    http = Counter((r["status"], r["method"], _path_of(r["url"])) for r in net if r["kind"] == "http_error")
    L += [f"- ×{c} **{s}** {m} `{p}`" for (s, m, p), c in http.most_common(20)] or ["_None recorded._"]
    failed = Counter((r["method"], _path_of(r["url"]), r.get("failure")) for r in net if r["kind"] == "failed")
    L += ["", "## Failed requests", ""]
    L += [f"- ×{c} {m} `{p}` — {f}" for (m, p, f), c in failed.most_common(20)] or ["_None recorded._"]
    slow = sorted((r for r in net if r["kind"] == "slow"), key=lambda r: -r["ms"])[:10]
    L += ["", f"## Slow requests (>= {rec.args.slow_ms} ms)", ""]
    L += [f"- {r['ms']} ms {r['method']} `{_path_of(r['url'])}`" for r in slow] or ["_None recorded._"]
    L += ["", "## Timeline", ""]
    interesting = {"session_start", "navigation", "note", "mark", "screenshot", "pause", "resume", "native_dialog", "download", "page_crash", "session_finish"}
    for t in [e for e in timeline if e["kind"] in interesting][:200]:
        detail = t.get("url") or t.get("text") or t.get("label") or t.get("file") or t.get("message") or ""
        L.append(f"- `+{t['t']}s` **{t['kind']}** {detail}")
    L += ["", "## Files", ""] + [f"- `{a['file']}` ({a['bytes']:,} bytes)" for a in manifest["artifacts"]]
    L += ["", "> Screenshots and video are not redacted — review before sharing."]
    path = out / "SUMMARY.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------------
# Browser availability + options
# ---------------------------------------------------------------------------------------------

def unavailable_reason() -> str | None:
    """Why a browser session cannot run on this machine, or None when it can.

    Safety net for run_session(); the install prompt in the support-bundle handler normally resolves a
    missing runtime first (see installer.py).
    """
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return (
            "No graphical display detected. The browser session opens a browser window, so run it from a "
            "workstation or a jump box with a desktop session (not a headless SSH shell)."
        )
    from platform_atlas.browser_session.installer import MANUAL_HINT, runtime_status
    if not runtime_status().ready:
        return f"The browser runtime is not installed. Install it with: {MANUAL_HINT}"
    return None


@dataclass(slots=True)
class SessionOptions:
    """Everything a browser session needs. Field names are read directly by the Recorder."""
    url: str
    out_dir: Path
    zip_path: Path | None = None
    video: bool = False
    har: bool = False
    slow_ms: int = 5000
    auto_shots: bool = True
    insecure: bool = False  # ignore TLS errors (mirrors Atlas verify_ssl=False)
    include_downloads: bool = False
    width: int = 1440
    height: int = 900
    # Testing only: no window / skip the consent screen / auto-stop / CDP port for the test harness
    headless: bool = False
    auto_start: bool = False
    duration: int = 0
    debug_port: int = 0
    theme: bool = True  # Atlas-colored browser chrome (headed runs only)


@dataclass(slots=True)
class BrowserSessionResult:
    """Outcome of a browser session. Like MongoDumpResult, any non-ok state is a soft failure for the bundle."""
    ok: bool = False
    session_dir: Path | None = None
    zip_path: Path | None = None
    stop_reason: str = ""
    recorded_seconds: float = 0.0
    counts: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    cancelled: bool = False
    skipped_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------------------------
# Session driver
# ---------------------------------------------------------------------------------------------

def _start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Daemon thread, so a blocked readline() can never keep the process alive after the session ends."""
    def reader() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.strip().lower())
    threading.Thread(target=reader, daemon=True).start()


def run_session(opts: SessionOptions, say: Callable[[str], None] = print) -> BrowserSessionResult:
    """Run one interactive browser session (blocking). ``say`` receives progress lines for the terminal."""
    reason = unavailable_reason()
    if reason:
        return BrowserSessionResult(skipped_reason=reason)
    try:
        return asyncio.run(_run(opts, say))
    except KeyboardInterrupt:
        return BrowserSessionResult(error="Interrupted before the session could be saved.")
    except Exception as exc:  # the bundle must still be produced
        return BrowserSessionResult(error=f"{type(exc).__name__}: {exc}")


async def _run(opts: SessionOptions, say: Callable[[str], None]) -> BrowserSessionResult:
    # The throwaway Chromium profile holds the Platform login cookies, so it is
    # removed on every exit path, including a failed browser launch.
    tmp = Path(tempfile.mkdtemp(prefix="atlas-browser-"))
    try:
        return await _run_session(opts, say, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _run_session(opts: SessionOptions, say: Callable[[str], None], tmp: Path) -> BrowserSessionResult:
    from playwright.async_api import async_playwright

    url = opts.url
    out = Path(opts.out_dir) / f"browser-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out, opts, url)
    started = _now()
    loop = asyncio.get_running_loop()
    interrupted = asyncio.Event()
    try:
        loop.add_signal_handler(signal.SIGINT, interrupted.set)
        loop.add_signal_handler(signal.SIGTERM, interrupted.set)
    except (NotImplementedError, RuntimeError):  # Windows
        pass

    browser_version, user_agent = "", ""
    har_path = out / "session.har"
    async with async_playwright() as pw:
        launch_args = ["--no-first-run", "--no-default-browser-check"]
        ignore_defaults: list[str] = []
        themed = False
        if not opts.headless:
            launch_args.append(f"--window-size={opts.width},{opts.height}")
            if opts.theme:
                theme_dir = make_theme_dir(tmp)
                launch_args += [f"--disable-extensions-except={theme_dir}", f"--load-extension={theme_dir}"]
                ignore_defaults = ["--disable-extensions"]
                themed = True
        if opts.debug_port:
            launch_args.append(f"--remote-debugging-port={opts.debug_port}")

        ctx_kwargs: dict = {"ignore_https_errors": opts.insecure, "accept_downloads": True}
        if opts.headless:
            ctx_kwargs["viewport"] = {"width": opts.width, "height": opts.height}
        else:
            ctx_kwargs["no_viewport"] = True
        if opts.video:
            ctx_kwargs["record_video_dir"] = str(out / "_video_tmp")
            ctx_kwargs["record_video_size"] = {"width": opts.width, "height": opts.height}
        if opts.har:
            ctx_kwargs.update(record_har_path=str(har_path), record_har_content="omit")

        # Throwaway profile: the user's own Chrome profile, cookies and extensions are never touched.
        context = await pw.chromium.launch_persistent_context(
            str(tmp / "profile"), headless=opts.headless, args=launch_args,
            ignore_default_args=ignore_defaults or None, **ctx_kwargs)
        rec.t0_ctx = monotonic()
        browser_version = context.browser.version if context.browser else ""

        closed = asyncio.Event()
        context.on("close", lambda _c: closed.set())
        await context.expose_binding("__atlas", rec.on_overlay_call)
        await context.add_init_script(load_overlay_assets() + OVERLAY_JS)
        await context.route(INTRO_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=INTRO_HTML))
        context.on("page", rec.attach)
        for existing in context.pages:
            rec.attach(existing)
        page = context.pages[0] if context.pages else await context.new_page()

        say(f"Browser: Chromium {browser_version}")
        if opts.auto_start:
            rec._set_phase("recording")
            rec.timeline("session_start", description="(auto-start)")
            await rec._goto_target(page)
        else:
            await page.goto(INTRO_URL)
            say("A welcome window is open. Read it, then press Start recording to begin.")
        say("In the browser use the toolbar, or here: s + Enter = screenshot, q + Enter = finish.")
        try:
            user_agent = await page.evaluate("navigator.userAgent")
        except Exception:
            pass

        queue: asyncio.Queue = asyncio.Queue()
        _start_stdin_reader(loop, queue)
        deadline = loop.time() + opts.duration if opts.duration else None

        while not closed.is_set() and not rec.finish_event.is_set():
            if interrupted.is_set():
                rec.stop_reason = "interrupted"
                break
            if deadline and loop.time() >= deadline:
                rec.stop_reason = "duration"
                break
            try:
                cmd = await asyncio.wait_for(queue.get(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            if cmd == "s" and rec.recording:
                await rec.screenshot(None, "manual")
            elif cmd == "q":
                rec.stop_reason = "user_quit"
                break

        if rec.stop_reason in ("duration", "user_quit", "interrupted") and rec.recording and not closed.is_set():
            await rec.screenshot(None, "final")
        if not rec.finish_event.is_set() and rec.phase in ("recording", "paused"):
            rec.timeline("session_finish", reason=rec.stop_reason)
        rec._set_phase("finishing" if rec.phase != "cancelled" else "cancelled")
        await rec.drain()
        try:
            await context.close()  # flushes HAR + finalizes video
        except Exception:
            pass
        rec.close()

    # -- post-processing --------------------------------------------------------------------
    if rec.phase == "cancelled":
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
        return BrowserSessionResult(cancelled=True, stop_reason="cancelled")

    artifacts: list[Path] = [out / n for n in ("console.ndjson", "console.log", "network_issues.ndjson", "timeline.ndjson")]
    artifacts += sorted(rec.shots_dir.glob("*.png"))
    video_tmp = out / "_video_tmp"
    if video_tmp.is_dir():
        for i, webm in enumerate(sorted(video_tmp.glob("*.webm")), 1):
            dest = out / f"session_{i}.webm"
            webm.replace(dest)
            artifacts.append(dest)
        shutil.rmtree(video_tmp, ignore_errors=True)
    if opts.har and har_path.is_file():
        scrub_har(har_path)
        artifacts.append(har_path)
    downloads = sorted(rec.downloads_dir.glob("*")) if rec.downloads_dir.is_dir() else []

    def describe(paths: list[Path]) -> list[dict]:
        return [{"file": str(p.relative_to(out)), "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in paths if p.is_file()]

    option_summary = {"video": opts.video, "har": opts.har, "auto_screenshots": opts.auto_shots, "slow_ms": opts.slow_ms}
    manifest = {
        "tool": "Platform Atlas troubleshooting browser",
        "target_url": scrub_url(url), "started": started, "ended": _now(),
        "recorded_seconds": round(rec.elapsed, 1), "stop_reason": rec.stop_reason,
        "browser": {"version": browser_version, "channel": "chromium", "headless": opts.headless, "user_agent": user_agent, "atlas_theme": themed},
        "options": option_summary,
        "consent": {"intro_acknowledged": not opts.auto_start, "auto_start": opts.auto_start},
        "counts": dict(rec.counts), "notes": len(rec.notes),
        "redaction": {
            "console_urls_notes": "JWT/bearer/secret-query-params scrubbed",
            "har": "bodies omitted, postData dropped, auth/cookie headers redacted" if opts.har else "n/a",
            "screenshots_and_video": "NOT redactable - review before sharing",
        },
        "artifacts": describe(artifacts),
        "downloads_excluded_from_zip": [d.name for d in downloads] if not opts.include_downloads else [],
    }
    summary = write_summary(out, manifest, rec)
    manifest["artifacts"] += describe([summary])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    try:  # single-file HTML viewer for support agents
        from platform_atlas.browser_session.viewer import build_viewer
        build_viewer(out)
    except Exception as exc:  # the viewer is a convenience - never lose a session over it
        say(f"(viewer.html not generated: {exc})")

    zip_path = opts.zip_path
    if zip_path is not None:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out.rglob("*")):
                if f.is_file() and (opts.include_downloads or rec.downloads_dir not in f.parents):
                    zf.write(f, f.relative_to(out.parent))
    shutil.rmtree(tmp, ignore_errors=True)
    return BrowserSessionResult(
        ok=True, session_dir=out, zip_path=zip_path, stop_reason=rec.stop_reason,
        recorded_seconds=round(rec.elapsed, 1), counts=dict(rec.counts), options=option_summary,
    )
