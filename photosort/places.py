"""Where a shoot was shot. Latitude and longitude are the file's own: EXIF GPS on a photo, the ISO 6709 tag
on a clip, read once while indexing. This module is the catch-up pass for a shoot indexed before locations
were stored, and the grouping the map draws over them. Nothing here looks anything up: the app makes no
network call, so a place has a name only when it is near one of the cities shipped with the app, and a file
whose camera wrote no fix simply has no place."""
from __future__ import annotations
import math
import time
from pathlib import Path
from typing import Callable
from . import db
from .features import exif_info

READ_KEY = "places_read_at"          # meta: when the catch-up pass last finished, ISO, local time

def status(root: Path) -> dict:
    """{located, unlocated, total, read_at}: how much of the shoot the map can draw. unlocated counts both
    "the camera wrote no fix" and "never looked", which the row cannot tell apart; read_at says whether the
    pass has been run, and is what the UI offers the button on."""
    conn = db.connect(Path(root))
    r = conn.execute("SELECT COUNT(*), SUM(lat IS NOT NULL) FROM photos WHERE status='ok'").fetchone()
    total, located = int(r[0] or 0), int(r[1] or 0)
    return {"located": located, "unlocated": total - located, "total": total,
            "read_at": db.get_meta(conn, READ_KEY)}

def read_locations(root: Path, progress: Callable[[dict], None] | None = None) -> dict:
    """Read the location out of every ok row that has none, straight from the file's metadata. Header reads
    only: a photo is never decoded and a clip is only probed. Returns {found, read, missing}, missing being
    files that are not on the disk right now (the shoot disk unplugged)."""
    root = Path(root); conn = db.connect(root)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, rel, kind FROM photos WHERE status='ok' AND lat IS NULL ORDER BY id")]
    total = len(rows)
    emit = progress or (lambda d: None)
    job = db.start_job(conn, "places", {"stage": "places", "done": 0, "total": total})
    found = missing = 0
    written = [0.0]
    try:
        emit({"stage": "places", "done": 0, "total": total})
        for i, r in enumerate(rows, 1):
            path = root / r["rel"]
            lat = lon = None
            if not path.is_file():
                missing += 1
            elif r["kind"] == "video":
                try:
                    from . import video
                    info = video.probe(path)
                    lat, lon = info.get("lat"), info.get("lon")
                except Exception:
                    pass                                   # an unreadable clip keeps no location, like an unreadable photo
            else:
                info = exif_info(path)
                lat, lon = info.get("lat"), info.get("lon")
            if lat is not None and lon is not None:
                conn.execute("UPDATE photos SET lat=?, lon=? WHERE id=?", (lat, lon, r["id"]))
                found += 1
            if i % 200 == 0 or i == total:
                conn.commit()
            now = time.time()
            if now - written[0] >= 2.0 or i == total:
                written[0] = now
                emit({"stage": "places", "done": i, "total": total})
                db.job_progress(conn, job, {"stage": "places", "done": i, "total": total})
        conn.commit()
    except BaseException as e:
        db.finish_job(conn, job, "failed", error=f"{type(e).__name__}: {e}")
        raise
    db.set_meta(conn, READ_KEY, time.strftime("%Y-%m-%d %H:%M:%S"))
    db.finish_job(conn, job, "done")
    return {"found": found, "read": total, "missing": missing}

# ===== grouping: the points the map draws, and the name to put on a pile of them =====

def cluster(points: list[tuple[float, float]], km: float = 25.0) -> list[dict]:
    """Group points that are within about km of each other into piles: [{lat, lon, n, south, west, north,
    east}], biggest first. A grid, not k-means: the map needs this to be instant on every zoom, and a grid
    cell at 25 km is finer than any shoot is wide."""
    if not points:
        return []
    deg = km / 111.0
    cells: dict[tuple[int, int], dict] = {}
    for lat, lon in points:
        key = (int(math.floor(lat / deg)), int(math.floor(lon / deg)))
        c = cells.get(key)
        if c is None:
            cells[key] = {"n": 1, "slat": lat, "slon": lon, "south": lat, "north": lat, "west": lon, "east": lon}
        else:
            c["n"] += 1; c["slat"] += lat; c["slon"] += lon
            c["south"] = min(c["south"], lat); c["north"] = max(c["north"], lat)
            c["west"] = min(c["west"], lon); c["east"] = max(c["east"], lon)
    out = [{"lat": round(c["slat"] / c["n"], 5), "lon": round(c["slon"] / c["n"], 5), "n": c["n"],
            "south": round(c["south"], 5), "north": round(c["north"], 5),
            "west": round(c["west"], 5), "east": round(c["east"], 5)} for c in cells.values()]
    out.sort(key=lambda c: (-c["n"], c["lat"], c["lon"]))
    return out
