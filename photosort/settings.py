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
