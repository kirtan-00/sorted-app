"""The app's own version and the opt-in update check.

VERSION at the repo root is the number (0.3.0). BUILD next to it is the git short sha of the tree that is running:
app-publish.sh stamps it from the source checkout, the installer writes it when it is missing and git can say,
and a plain checkout without one asks git. Shown as "0.3.0 (a12ec7a)".

The update check is off until the tester ticks it in How it works. On, the app reads one static file on the site
(UPDATE_URL, version.json: {"version", "notes"}) at most once a day, with a plain GET and nothing in it, and keeps
the answer in settings.json. Nothing is sent anywhere."""
from __future__ import annotations
import datetime as _dt
import json
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from . import settings
from .config import ROOT

VERSION_FILE = ROOT / "VERSION"
BUILD_FILE = ROOT / "BUILD"
DEFAULT_UPDATE_URL = "https://kirtan-00.github.io/sorted/version.json"
DOWNLOAD_URL = "https://kirtan-00.github.io/sorted/download/"
CHECK_EVERY_S = 24 * 3600
FETCH_TIMEOUT_S = 6

_lock = threading.Lock()
_checking = False


def update_url() -> str:
    return os.environ.get("SORTED_UPDATE_URL") or DEFAULT_UPDATE_URL


def app_version() -> str:
    try:
        v = VERSION_FILE.read_text().strip()
    except OSError:
        v = ""
    return v or "0.0.0"


def app_build() -> str | None:
    """BUILD next to VERSION, else git's short sha for this checkout, else None (a tree with neither)."""
    try:
        b = BUILD_FILE.read_text().strip()
        if b:
            return b.split()[0][:12]
    except OSError:
        pass
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def version_string() -> str:
    b = app_build()
    return f"{app_version()} ({b})" if b else app_version()


def parse_version(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v or "").strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit(): digits += ch
            else: break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def fetch_latest(timeout: float = FETCH_TIMEOUT_S) -> dict:
    """{version, notes} from the site. A plain GET of one file: no query, no body, no cookie."""
    url = update_url()
    if url.startswith("file://"):
        data = Path(url[7:]).read_bytes()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "sorted/" + app_version(), "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(64 * 1024)
    d = json.loads(data.decode("utf-8"))
    if not isinstance(d, dict) or not isinstance(d.get("version"), str):
        raise ValueError("version.json has no version")
    return {"version": d["version"].strip(), "notes": str(d.get("notes") or "").strip()[:2000]}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def due(prefs: dict | None = None) -> bool:
    """True when the check is on and the last one is older than a day (or never ran)."""
    p = prefs if prefs is not None else settings.get_update_prefs()
    if not p.get("enabled"):
        return False
    at = p.get("checked_at")
    if not at:
        return True
    try:
        then = _dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds() >= CHECK_EVERY_S


def status() -> dict:
    """What the UI shows: the version, the build, whether the check is on, what it last found."""
    p = settings.get_update_prefs()
    latest = p.get("latest")
    return {"version": app_version(), "build": app_build(), "enabled": bool(p.get("enabled")),
            "checked_at": p.get("checked_at"), "latest": latest, "notes": p.get("notes") or "",
            "newer": bool(latest) and is_newer(latest, app_version()), "error": p.get("error"),
            "checking": _checking, "download_url": DOWNLOAD_URL}


def check_now() -> dict:
    """One fetch, the answer (or the error) kept in settings.json with the time. Returns status()."""
    global _checking
    with _lock:
        _checking = True
    try:
        try:
            latest = fetch_latest()
            settings.set_update_result(latest["version"], latest["notes"], _now(), None)
        except Exception as e:
            settings.set_update_result(None, None, _now(), f"{type(e).__name__}: {e}"[:300])
    finally:
        with _lock:
            _checking = False
    return status()


def check_in_background() -> bool:
    """Start one check on a thread when it is due and none is running. True when one was started."""
    global _checking
    with _lock:
        if _checking or not due():
            return False
        _checking = True
    def run():
        global _checking
        try:
            check_now()
        finally:
            with _lock:
                _checking = False
    threading.Thread(target=run, daemon=True).start()
    return True
