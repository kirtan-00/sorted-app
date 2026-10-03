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

CITY_LIST = Path(__file__).resolve().parent / "ui" / "places.json"   # the city list shipped with the app
MERGE_KM = 50.0          # two piles of the same name further apart than this are two places, not one
NAME_KM = 100.0          # further than this from every shipped city, a pile is nowhere rather than wrong

_CITIES: list[tuple[str, str, float, float]] | None = None

def cities() -> list[tuple[str, str, float, float]]:
    """(name, country, lat, lon) for every city the app ships, read once. The same file the map reads, so
    a pile named here carries the name the UI would have put on it."""
    global _CITIES
    if _CITIES is None:
        import json
        d = json.loads(CITY_LIST.read_text())
        sc = float(d.get("scale") or 1)
        _CITIES = [(r[0], r[1], r[2] / sc, r[3] / sc) for r in d["places"]]
    return _CITIES

def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle kilometres. Haversine rather than flat degrees: a degree of longitude is 111 km at the
    equator and 78 km in Ahmedabad, and this number decides whether two piles are one place."""
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))

_CITY_XYZ = None

def _city_vectors():
    """The city list as unit vectors on the sphere, built once. The nearest city is then the largest dot
    product, which is one matrix multiply instead of 7,342 haversines per pile: the grid exists to keep the
    map instant, and naming every pile is not allowed to give that away."""
    global _CITY_XYZ
    if _CITY_XYZ is None:
        import numpy as np
        lat = np.radians(np.array([c[2] for c in cities()])); lon = np.radians(np.array([c[3] for c in cities()]))
        _CITY_XYZ = np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=1)
    return _CITY_XYZ

def nearest_city(lat: float, lon: float, max_km: float = NAME_KM) -> tuple[str, str] | None:
    """(name, country) of the nearest shipped city, or None when the nearest one is further than max_km
    away. The app makes no network call, so the list is all there is: past that distance the honest answer
    is that this place has no name, not the name of a city most of a country away."""
    import numpy as np
    rows = cities()
    if not rows:
        return None
    a, o = math.radians(lat), math.radians(lon)
    v = np.array([math.cos(a) * math.cos(o), math.cos(a) * math.sin(o), math.sin(a)])
    i = int(np.argmax(_city_vectors() @ v))
    name, country, clat, clon = rows[i]
    return (name, country) if km_between(lat, lon, clat, clon) <= max_km else None

def cluster(points: list[tuple[float, float]], km: float = 25.0) -> list[dict]:
    """The piles the map draws: [{lat, lon, n, south, west, north, east, name, country}], biggest first.

    Two passes, because one pass cannot do both jobs. First a grid of km-wide cells, which is what keeps
    the map instant on every zoom, and which is emphatically NOT "points within km of each other": a grid
    has hard edges, so two points 100 m apart land in different cells when a boundary runs between them.
    Then a merge of cells that resolved to the same city. Ahmedabad sits almost exactly on a cell edge
    (lon 322 * 0.225225 = 72.5225) and came out of the first pass as two rows, 172 photos and 64, 8 km
    apart and unable to merge for as long as merging is a grid's job.

    The merge is by (name, country) and capped at MERGE_KM, so two of the five Springfields, or any two
    genuinely different towns that share a name, stay two rows. Cells too far from any city to be named
    are never merged with each other: without a name there is nothing to say they are one place, and
    guessing would weld together the one case the grid cannot argue about."""
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
    piles = _merge_by_name(list(cells.values()))
    out = [{"lat": round(c["slat"] / c["n"], 5), "lon": round(c["slon"] / c["n"], 5), "n": c["n"],
            "south": round(c["south"], 5), "north": round(c["north"], 5),
            "west": round(c["west"], 5), "east": round(c["east"], 5),
            "name": c["name"], "country": c["country"]} for c in piles]
    out.sort(key=lambda c: (-c["n"], c["lat"], c["lon"]))
    return out

def _merge_by_name(cells: list[dict]) -> list[dict]:
    """Fold every cell into the first accepted pile with the same name within MERGE_KM of it, biggest pile
    first so the answer does not depend on which corner of a city the walk happened to reach first. The
    box is unioned rather than recomputed, so a click on the merged row still filters to everything in
    it."""
    for c in cells:
        found = nearest_city(c["slat"] / c["n"], c["slon"] / c["n"])
        c["name"], c["country"] = found if found else (None, None)
    piles: list[dict] = []
    for c in sorted(cells, key=lambda c: (-c["n"], c["slat"] / c["n"], c["slon"] / c["n"])):
        for g in piles:
            if c["name"] is None or (g["name"], g["country"]) != (c["name"], c["country"]):
                continue
            if km_between(g["slat"] / g["n"], g["slon"] / g["n"], c["slat"] / c["n"], c["slon"] / c["n"]) > MERGE_KM:
                continue
            g["n"] += c["n"]; g["slat"] += c["slat"]; g["slon"] += c["slon"]
            g["south"] = min(g["south"], c["south"]); g["north"] = max(g["north"], c["north"])
            g["west"] = min(g["west"], c["west"]); g["east"] = max(g["east"], c["east"])
            break
        else:
            piles.append(c)
    return piles
