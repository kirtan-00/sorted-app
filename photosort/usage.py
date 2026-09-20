"""The on-device usage log: what the tester did, what failed and how long things took, kept on this Mac.

One JSON line per event in app_home()/usage/events.jsonl: {ts (local ISO), session (one id per server
start), ev, ...fields}. Fields are facts, never content: durations, counts, sizes, which button, which
tab, category names, error strings. Never a photo path beyond the shoot's name, never a person's name,
never more than the first QUERY_CHARS characters of a search query (plus its length). scrub() enforces
that at the append boundary for strings the callers did not write themselves (error messages from
elsewhere in the server carry paths and quoted names).

Nothing here opens a socket. The report is a zip on the Desktop the tester sends by hand.
"""
from __future__ import annotations
import datetime as _dt
import json
import os
import platform
import re
import subprocess
import threading
import time
import traceback
import uuid
import zipfile
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from .config import app_home, ROOT

EVENTS_NAME = "events.jsonl"
ROTATE_BYTES = 20 * 1024 * 1024     # events.jsonl is renamed to events.1.jsonl past this; two files are kept
QUERY_CHARS = 60                    # of a search query, plus its length
FIELD_CHARS = 200                   # any other string field
TOP_QUERIES = 20
ERRORS_KEPT = 50
DEFAULT_LOG = Path.home() / "Library" / "Logs" / "photosort.log"

_lock = threading.Lock()
_session = {"id": None, "started": None}
counters: Counter = Counter()       # events this session, by ev

_PATH_RE = re.compile(r"(?<![\w.])(?:~|/)(?:[^\s'\"()]+/)*[^\s'\"()]*")   # /Volumes/SSD/x.jpg, ~/Desktop, /x
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def usage_dir() -> Path:
    return app_home() / "usage"


def events_path() -> Path:
    return usage_dir() / EVENTS_NAME


def event_files() -> list[Path]:
    """Every log file, current first (events.jsonl, events.1.jsonl, events.2.jsonl)."""
    d = usage_dir()
    out = [d / EVENTS_NAME, d / "events.1.jsonl", d / "events.2.jsonl"]
    return [p for p in out if p.is_file()]


def start_session() -> str:
    """A fresh session id (one per server start); the caller logs server_start after this."""
    sid = uuid.uuid4().hex[:12]
    with _lock:
        _session["id"] = sid
        _session["started"] = time.time()
        counters.clear()
    return sid


def session_id() -> str:
    return _session["id"] or start_session()


def scrub(value, chars: int = FIELD_CHARS):
    """Strings only: absolute or home paths become <path>, quoted spans (the server quotes names and
    folder names with !r) become '?', and the whole is cut to `chars`. Other values pass through."""
    if not isinstance(value, str):
        return value
    s = _QUOTED_RE.sub("'?'", value)
    s = _PATH_RE.sub("<path>", s)
    return s[:chars]


def _clean(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in ("ts", "session", "ev"):
            continue
        if k == "q":
            if isinstance(v, str):
                out["q_len"] = len(v)
                out["q"] = v[:QUERY_CHARS]
            continue
        if k == "route":                 # a route template such as /api/people/references/{name}/find, never a raw URL
            out[k] = str(v)[:FIELD_CHARS]
            continue
        if isinstance(v, dict):
            out[k] = {str(kk)[:FIELD_CHARS]: scrub(vv) for kk, vv in list(v.items())[:100]}
        elif isinstance(v, (list, tuple)):
            out[k] = [scrub(x) for x in list(v)[:100]]
        else:
            out[k] = scrub(v)
    return out


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _rotate(p: Path) -> None:
    if not p.is_file() or p.stat().st_size < ROTATE_BYTES:
        return
    one, two = p.with_name("events.1.jsonl"), p.with_name("events.2.jsonl")
    if one.is_file():
        os.replace(one, two)
    os.replace(p, one)


def log(ev: str, **fields) -> dict | None:
    """Append one event. Never raises: a full disk or an unwritable home must not break the app."""
    try:
        rec = {"ts": _now(), "session": session_id(), "ev": str(ev)[:40]}
        rec.update(_clean(fields))
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with _lock:
            counters[rec["ev"]] += 1
            p = events_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            _rotate(p)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line)
        return rec
    except Exception:
        return None


def log_exception(ev: str = "exception", **fields) -> None:
    """The current exception's last traceback line, never the whole trace."""
    last = traceback.format_exc().strip().splitlines()
    log(ev, error=last[-1] if last else "unknown", **fields)


# ===== system info (once per process) =====

def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


@lru_cache(maxsize=1)
def system_info() -> dict:
    """App version (git short sha when the checkout has one, else the package version), macOS version,
    chip, RAM, python, ffmpeg. Every probe is optional: the .app bundle has no .git, a Mac may have no ffmpeg."""
    info = {"app": None, "macos": None, "chip": None, "ram_gb": None, "python": platform.python_version(), "ffmpeg": None}
    sha = _run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"])
    if sha:
        info["app"] = sha
    else:
        try:
            from importlib.metadata import version
            info["app"] = version("photosort")
        except Exception:
            info["app"] = "unknown"
    try:
        info["macos"] = platform.mac_ver()[0] or platform.platform()
    except Exception:
        pass
    info["chip"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.machine()
    try:
        info["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3)
    except Exception:
        pass
    try:
        from .video import _bin
        first = (_run([_bin("ffmpeg"), "-version"]) or "").splitlines()
        if first:
            m = re.search(r"ffmpeg version (\S+)", first[0])
            info["ffmpeg"] = m.group(1) if m else first[0][:40]
    except Exception:
        pass
    return info


# ===== reading back =====

def read_events() -> list[dict]:
    """Every event on disk, oldest first, bad lines skipped."""
    out = []
    for p in reversed(event_files()):
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out


def summary(events: list[dict] | None = None) -> dict:
    """Session count, first and last ts, counters per ev, top queries, errors, total indexed items and
    seconds by feature, over every log file plus this session's in-memory counters."""
    evs = read_events() if events is None else events
    sessions = []
    seen = set()
    by_ev: Counter = Counter()
    queries: Counter = Counter()
    errors = []
    indexed = 0
    seconds: defaultdict = defaultdict(float)
    for e in evs:
        ev = e.get("ev", "?")
        by_ev[ev] += 1
        s = e.get("session")
        if s and s not in seen:
            seen.add(s); sessions.append(s)
        if ev == "search" and e.get("q"):
            queries[e["q"]] += 1
        if ev in ("api_error", "ui_error", "exception") or e.get("error"):
            errors.append({"ts": e.get("ts"), "ev": ev, "where": e.get("route") or e.get("what") or e.get("source"),
                           "status": e.get("status"), "error": e.get("error") or e.get("message")})
        if ev == "index_done":
            indexed += int(e.get("indexed") or 0)
        if ev in ("tab_time", "ui_tab_time") and e.get("tab"):
            seconds["tab:" + str(e["tab"])] += float(e.get("seconds") or 0)
        if ev == "index_done":
            seconds["indexing"] += float(e.get("seconds") or 0)
        if ev == "classify_done":
            seconds["categorising"] += float(e.get("seconds") or 0)
        if ev == "focus_run":
            seconds["focus check"] += float(e.get("seconds") or 0)
        if ev == "export_done":
            seconds["exporting"] += float(e.get("seconds") or 0)
        if ev in ("reorganise_apply", "reorganise_undo"):
            seconds["reorganising"] += float(e.get("seconds") or 0)
        if ev == "search":
            seconds["searching"] += float(e.get("ms") or 0) / 1000.0
        if ev == "people_group":
            seconds["grouping faces"] += float(e.get("ms") or 0) / 1000.0
    return {
        "generated": _now(),
        "session": _session["id"],
        "sessions": len(sessions),
        "events": len(evs),
        "first_ts": evs[0].get("ts") if evs else None,
        "last_ts": evs[-1].get("ts") if evs else None,
        "counters": dict(sorted(by_ev.items())),
        "this_session": dict(sorted(counters.items())),
        "top_queries": [{"q": q, "n": n} for q, n in queries.most_common(TOP_QUERIES)],
        "errors": errors[-ERRORS_KEPT:],
        "indexed_items": indexed,
        "seconds_by_feature": {k: round(v, 1) for k, v in sorted(seconds.items())},
        "system": system_info(),
        "log_files": [p.name for p in event_files()],
    }


def summary_text(s: dict | None = None) -> str:
    s = summary() if s is None else s
    lines = ["sorted beta report", "generated " + str(s["generated"]), ""]
    sysd = s.get("system") or {}
    lines.append(f"app {sysd.get('app')}  macOS {sysd.get('macos')}  {sysd.get('chip')}  {sysd.get('ram_gb')} GB  "
                 f"python {sysd.get('python')}  ffmpeg {sysd.get('ffmpeg')}")
    lines.append(f"{s['sessions']} session(s), {s['events']} events, {s['first_ts']} to {s['last_ts']}")
    lines.append(f"indexed items {s['indexed_items']}")
    lines.append("")
    lines.append("time by feature (seconds)")
    for k, v in s["seconds_by_feature"].items():
        lines.append(f"  {k:<20} {v:>8.1f}")
    lines.append("")
    lines.append("events")
    for k, v in s["counters"].items():
        lines.append(f"  {k:<24} {v:>6}")
    lines.append("")
    lines.append("top queries")
    for row in s["top_queries"]:
        lines.append(f"  {row['n']:>4}  {row['q']}")
    lines.append("")
    lines.append(f"errors (last {ERRORS_KEPT})")
    for e in s["errors"]:
        lines.append(f"  {e.get('ts')}  {e.get('ev')}  {e.get('status') or ''}  {e.get('where') or ''}  {e.get('error')}")
    return "\n".join(lines) + "\n"


# ===== the report zip =====

def log_path() -> Path:
    return Path(os.environ.get("PHOTOSORT_LOG") or DEFAULT_LOG)


def report_dir() -> Path:
    return Path(os.environ.get("PHOTOSORT_REPORT_DIR") or (Path.home() / "Desktop"))


def write_report(dest_dir: Path | None = None) -> Path:
    """~/Desktop/sorted-report-<date>.zip: events*.jsonl, the launcher log if there is one, summary.json,
    summary.txt and system.json. No photos, no thumbnails, no index.db."""
    dest_dir = Path(dest_dir) if dest_dir else report_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d")
    out = dest_dir / f"sorted-report-{stamp}.zip"
    n = 2
    while out.exists():
        out = dest_dir / f"sorted-report-{stamp}-{n}.zip"; n += 1
    s = summary()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in event_files():
            z.write(p, "usage/" + p.name)
        lp = log_path()
        if lp.is_file():
            z.write(lp, lp.name)
        z.writestr("summary.json", json.dumps(s, indent=2, ensure_ascii=False))
        z.writestr("summary.txt", summary_text(s))
        z.writestr("system.json", json.dumps(system_info(), indent=2))
    return out
