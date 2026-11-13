#!/usr/bin/env python3
"""
Builds `viewer.html`, the single-file session viewer for a `support-bundle --browser-session` recording.

Itential support agents open viewer.html in any browser (no server, no install, no network) and get:
  Overview, unified Timeline, Video player with jump-to-moment, Screenshot gallery + lightbox,
  Console, Network (issues + every request when --har was used), Notes, and Files.

Stdlib-only. recorder.py calls build_viewer() automatically after a session; you can also rebuild the viewer for
any extracted session folder:

    python3 -m platform_atlas.browser_session.viewer <session-folder>

Design: the page is rendered from viewer_template.html (in assets/) using the same design tokens as
Atlas' standalone report.html (Modern light/dark, brass/sky accents, navy ink rail, drawer + modal patterns).
All session data is embedded in the page as JSON; only the video, screenshots and other artifacts are referenced
by relative path, so keep viewer.html in the session folder (or extracted ZIP) next to them.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ASSET_DIR = Path(__file__).resolve().parent / "assets"
TEMPLATE = ASSET_DIR / "viewer_template.html"
INTRO_HOST = "atlas-recorder.invalid"
MAX_HAR_ENTRIES = 4000
HEADER_VALUE_CAP = 600


def _ndjson(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _secs(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.is_file() else ""


def _condense_har(har_path: Path, start_ts: float | None) -> tuple[list[dict], int]:
    """Reduce the (already scrubbed) HAR to what the viewer shows. Returns (entries, total_before_cap)."""
    if not har_path.is_file():
        return [], 0
    try:
        entries = json.loads(har_path.read_text(encoding="utf-8")).get("log", {}).get("entries", [])
    except ValueError:
        return [], 0
    rows = []
    for e in entries:
        req, res = e.get("request", {}), e.get("response", {})
        url = req.get("url", "")
        if INTRO_HOST in url:
            continue
        ts = _secs(e.get("startedDateTime"))
        if start_ts is not None and ts is not None and ts < start_ts - 0.5:
            continue  # welcome-screen traffic from before the user pressed Start

        def hdrs(items):
            return [[h.get("name", ""), str(h.get("value", ""))[:HEADER_VALUE_CAP]] for h in items or []]

        size = res.get("bodySize", -1)
        if size is None or size < 0:
            size = res.get("content", {}).get("size", -1)
        rows.append({
            "ts": e.get("startedDateTime"), "t": None if ts is None or start_ts is None else round(ts - start_ts, 2),
            "method": req.get("method", ""), "url": url, "status": res.get("status", 0), "statusText": res.get("statusText", ""),
            "ms": round(e.get("time", 0) or 0), "type": e.get("_resourceType", ""), "mime": res.get("content", {}).get("mimeType", ""),
            "size": size, "http": res.get("httpVersion", ""),
            "reqHeaders": hdrs(req.get("headers")), "resHeaders": hdrs(res.get("headers")),
        })
    total = len(rows)
    return rows[:MAX_HAR_ENTRIES], total


def _file_kind(name: str) -> str:
    n = name.lower()
    if n.endswith(".webm"):
        return "Screen recording"
    if n.endswith(".png"):
        return "Screenshot"
    if n.endswith(".har"):
        return "Network capture (HAR, scrubbed)"
    if n == "console.ndjson":
        return "Console log (structured)"
    if n == "console.log":
        return "Console log (plain text)"
    if n == "network_issues.ndjson":
        return "Failed / slow / error requests"
    if n == "timeline.ndjson":
        return "Event timeline"
    if n == "summary.md":
        return "Plain-text summary"
    if n == "manifest.json":
        return "Session manifest"
    return "File"


def collect(session_dir: Path) -> dict:
    manifest = {}
    mpath = session_dir / "manifest.json"
    if mpath.is_file():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    timeline = _ndjson(session_dir / "timeline.ndjson")
    console = _ndjson(session_dir / "console.ndjson")
    net = _ndjson(session_dir / "network_issues.ndjson")

    start_ev = next((e for e in timeline if e.get("kind") == "session_start"), None)
    start_ts = _secs(start_ev["ts"]) if start_ev else (_secs(timeline[0]["ts"]) if timeline else None)
    video_origin = float(start_ev.get("video_t", 0.0)) if start_ev else 0.0

    def rel(ts: str | None) -> float | None:
        s = _secs(ts)
        return None if s is None or start_ts is None else round(max(0.0, s - start_ts), 2)

    for rec in timeline:
        rec["rel"] = rel(rec.get("ts"))
        rec["video"] = round(video_origin + (rec["rel"] or 0), 2) if rec["rel"] is not None else rec.get("video_t")
    for rec in console + net:
        rec["rel"] = rel(rec.get("ts"))
        rec["video"] = round(video_origin + rec["rel"], 2) if rec["rel"] is not None else None

    shots = []
    for e in timeline:
        if e.get("kind") == "screenshot" and (session_dir / "screenshots" / e.get("file", "")).is_file():
            shots.append({"file": "screenshots/" + e["file"], "reason": e.get("reason", ""), "page": e.get("page", ""),
                          "rel": e["rel"], "video": e["video"], "ts": e["ts"]})
    known = {s["file"] for s in shots}
    for p in sorted((session_dir / "screenshots").glob("*.png")) if (session_dir / "screenshots").is_dir() else []:
        if f"screenshots/{p.name}" not in known:
            shots.append({"file": f"screenshots/{p.name}", "reason": "", "page": "", "rel": None, "video": None, "ts": None})

    notes = [{"text": e.get("text", ""), "page": e.get("page", ""), "screenshot": ("screenshots/" + e["screenshot"]) if e.get("screenshot") else None,
              "rel": e["rel"], "video": e["video"], "ts": e["ts"]} for e in timeline if e.get("kind") == "note"]
    marks = [{"label": e.get("label", ""), "page": e.get("page", ""), "rel": e["rel"], "video": e["video"]} for e in timeline if e.get("kind") == "mark"]

    har_rows, har_total = _condense_har(session_dir / "session.har", start_ts)
    videos = sorted(p.name for p in session_dir.glob("*.webm"))

    files = []
    for a in manifest.get("artifacts", []):
        files.append({**a, "kind": _file_kind(a["file"])})
    if not files:  # manifest missing - list what is on disk
        for p in sorted(session_dir.rglob("*")):
            if p.is_file() and p.name != "viewer.html":
                files.append({"file": str(p.relative_to(session_dir)), "bytes": p.stat().st_size, "sha256": "", "kind": _file_kind(p.name)})
    if (session_dir / "manifest.json").is_file() and not any(f["file"] == "manifest.json" for f in files):
        files.append({"file": "manifest.json", "bytes": (session_dir / "manifest.json").stat().st_size, "sha256": "", "kind": "Session manifest"})

    return {
        "session": session_dir.name,
        "manifest": {k: manifest.get(k) for k in ("target_url", "started", "ended", "recorded_seconds", "stop_reason", "browser", "options",
                                                  "counts", "redaction", "downloads_excluded_from_zip", "tool")},
        "description": (start_ev or {}).get("description", ""),
        "timeline": timeline, "console": console, "net": net,
        "har": har_rows, "har_total": har_total,
        "screenshots": shots, "notes": notes, "marks": marks, "videos": videos, "files": files,
    }


def build_viewer(session_dir: Path) -> Path:
    session_dir = Path(session_dir)
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"viewer template missing: {TEMPLATE}")
    data = collect(session_dir)
    # JSON inside <script type="application/json">: neutralise anything that could end the block or open a comment.
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = TEMPLATE.read_text(encoding="utf-8")
    subs = {
        "/*__SORA__*/": _b64(ASSET_DIR / "sora-latin.woff2"),
        "/*__ONEST__*/": _b64(ASSET_DIR / "onest-latin.woff2"),
        "/*__HANKEN__*/": _b64(ASSET_DIR / "hanken-grotesk-latin.woff2"),
        "/*__LOGO__*/": _b64(ASSET_DIR / "itential-logo.png"),
        "/*__TITLE__*/": re.sub(r"[<>&\"]", "", data["session"]),
        "/*__DATA__*/": blob,
    }
    for key, val in subs.items():
        if key not in html:
            raise ValueError(f"template placeholder missing: {key}")
        html = html.replace(key, val)
    out = session_dir / "viewer.html"
    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    d = Path(sys.argv[1]).expanduser().resolve()
    if not d.is_dir():
        print(f"Not a folder: {d}")
        return 1
    out = build_viewer(d)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
