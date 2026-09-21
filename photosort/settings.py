"""Small persisted settings under app_home() (settings.json). Nothing here touches the DB.
A missing or unreadable file reads as {} so a corrupt settings file never blocks the app."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from .config import settings_path

def load() -> dict:
    p = settings_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save(d: dict) -> None:
    """Write to a temp file in the same folder, then rename over settings.json, so a crash
    mid-write never leaves a half-written file behind."""
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d, indent=2))
        os.replace(tmp, p)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def get_export_base() -> Path | None:
    """Where exports go instead of export_root(), or None for the default."""
    v = load().get("export_base")
    return Path(v) if isinstance(v, str) and v else None

def set_export_base(p: Path | None) -> None:
    d = load()
    if p is None:
        d.pop("export_base", None)
    else:
        d["export_base"] = str(p)
    save(d)

def get_drive_prefs() -> dict:
    """The last Google Drive folder link pasted and the web-size choice: {link: str | None, web_size: int | None}."""
    d = load()
    link = d.get("drive_link"); ws = d.get("drive_web_size")
    return {"link": link if isinstance(link, str) and link else None,
            "web_size": ws if isinstance(ws, int) and not isinstance(ws, bool) and ws > 0 else None}

def set_drive_prefs(link: str | None, web_size: int | None) -> None:
    d = load()
    if link: d["drive_link"] = link
    else: d.pop("drive_link", None)
    if web_size: d["drive_web_size"] = int(web_size)
    else: d.pop("drive_web_size", None)
    save(d)

# ===== the opt-in update check (photosort/version.py): on or off, when it last ran, what it found =====
def get_update_prefs() -> dict:
    d = load()
    return {"enabled": bool(d.get("update_check")), "checked_at": d.get("update_checked_at"),
            "latest": d.get("update_latest"), "notes": d.get("update_notes"), "error": d.get("update_error")}

def set_update_enabled(enabled: bool) -> None:
    d = load()
    if enabled: d["update_check"] = True
    else:
        for k in ("update_check", "update_checked_at", "update_latest", "update_notes", "update_error"): d.pop(k, None)
    save(d)

def set_update_result(latest: str | None, notes: str | None, checked_at: str, error: str | None) -> None:
    d = load()
    d["update_checked_at"] = checked_at
    if latest:
        d["update_latest"] = latest; d["update_notes"] = notes or ""; d.pop("update_error", None)
    else:
        d["update_error"] = error or "unknown"       # the last good answer, if any, stays
    save(d)
