from __future__ import annotations
import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from . import db
from .config import GROUP_MIN_FACES, CATEGORY_FALLBACK, SURE_MIN, BURST_SIM, BURST_GAP_S

SORTS = ("oldest", "newest", "biggest", "smallest", "name", "longest")

def when_of(p: dict) -> str:
    """The moment a row is filed under: its EXIF capture time when the file carries one, else the file's own
    mtime. A screenshot, a WhatsApp copy or an export has no capture time, and hiding those at the end of the
    shoot is not what anyone means by "by date"."""
    t = p.get("taken_at")
    if t:
        return str(t)
    m = p.get("mtime")
    if m is None:
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(m)).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""

@dataclass
class Filters:
    sharp_min_pct: float | None = None
    faces: str | None = None
    person_id: int | None = None
    taken_from: str | None = None
    taken_to: str | None = None
    day: str | None = None          # one calendar day, YYYY-MM-DD, matched against when_of (see Index.days)
    bbox: tuple[float, float, float, float] | None = None   # south, west, north, east: a box dragged on the map
    category: str | None = None
    kind: str | None = None         # "photos" | "videos" | None for both
    cluster: str | None = None      # a discovered category (photos.cluster)
    sure_only: bool = False         # drop the "less sure" band (exports want only what the model is sure of)
    aerial: bool | None = None      # True: drone shots only (photos.aerial), False: none of them, None: both
    hide_bad: bool = False          # drop rows the focus pass labelled bad (photos.focus); unchecked rows stay
    hide_soft: bool = False         # drop soft and bad
    fold: bool = False              # one tile per duplicate set and per burst (the first in order shows; see Index.groups)

def category_match(p, cat: str) -> tuple[bool, float] | None:
    """(sure, confidence) for a photo row against fixed category cat, or None when it is not there at all.
    A row filed under cat is as sure as its category_score (NULL, from before scores were stored, is
    sure). "other" and "unclassified" are bins, not guesses: always sure. A row filed under "other"
    whose best real guess was cat also belongs to cat, but never as sure, whatever its probability:
    the gates already said no. Its guess probability is the confidence it is sorted by."""
    if cat == "unclassified":
        return (True, 1.0) if p["category"] is None else None
    if p["category"] == cat:
        s = p.get("category_score")
        if cat == CATEGORY_FALLBACK or s is None:
            return True, 1.0
        return float(s) >= SURE_MIN, float(s)
    if p["category"] == CATEGORY_FALLBACK and p.get("category_guess") == cat:
        return False, float(p.get("category_guess_score") or 0.0)
    # A people photo keeps the scene it was shot in as its guess (see classify._classify_all); that
    # scene's tile owns it too, as sure as the scene score says.
    if p["category"] == "people" and cat != "people" and p.get("category_guess") == cat:
        s = float(p.get("category_guess_score") or 0.0)
        return s >= SURE_MIN, s
    return None

def cluster_match(p, name: str) -> tuple[bool, float] | None:
    """(sure, confidence) against discovered category name: the photo's cosine to its cluster centroid
    rescaled to 0..1 across the cluster, sure from SURE_MIN up. NULL (never scored) is sure."""
    if p["cluster"] != name:
        return None
    s = p.get("cluster_score")
    return (True, 1.0) if s is None else (float(s) >= SURE_MIN, float(s))

_NUM_RE = re.compile(r"(\d+)(?!.*\d)")     # the last run of digits in a filename stem: DSC01234 -> 1234, IMG_0007 -> 7

def _frame_no(rel: str) -> int | None:
    m = _NUM_RE.search(Path(rel).stem)
    return int(m.group(1)) if m else None

def _taken(p: dict) -> _dt.datetime | None:
    t = p.get("taken_at")
    if not t:
        return None
    try:
        return _dt.datetime.fromisoformat(str(t)[:19])
    except ValueError:
        return None

class Index:
    def __init__(self, root: Path):
        self.root = Path(root); self.refresh()

    # ===== duplicates and bursts: computed once per refresh over the whole shoot, folded per query =====
    def _find_groups(self) -> None:
        """Marks every photo dict with dup (the qhash, when the same file sits on the disk more than once) and
        burst (an int id, when a run of photos in one folder was shot as a burst: consecutive frame numbers,
        each within BURST_GAP_S of the last, and, where both have embeddings, cosine BURST_SIM or closer).
        Clips never burst; a clip can be a duplicate."""
        by_hash: dict[str, list[int]] = {}
        for p in self.photos.values():
            p["dup"] = None; p["burst"] = None
            if p.get("qhash"):
                by_hash.setdefault(p["qhash"], []).append(p["id"])
        for qh, ids in by_hash.items():
            if len(ids) > 1:
                for i in ids: self.photos[i]["dup"] = qh
        stills = [p for p in self.photos.values() if p.get("kind") != "video" and _taken(p) is not None and _frame_no(p["rel"]) is not None]
        stills.sort(key=lambda p: (str(Path(p["rel"]).parent), _taken(p), p["rel"]))
        gid = 0; run: list[dict] = []
        def close():
            nonlocal gid
            if len(run) > 1:
                gid += 1
                for p in run: p["burst"] = gid
            run.clear()
        prev = None
        for p in stills:
            if prev is not None and self._burst_pair(prev, p):
                run.append(p)
            else:
                close(); run.append(p)
            prev = p
        close()

    def _burst_pair(self, a: dict, b: dict) -> bool:
        if Path(a["rel"]).parent != Path(b["rel"]).parent: return False
        if a.get("camera") != b.get("camera"): return False
        ta, tb = _taken(a), _taken(b)
        if ta is None or tb is None or (tb - ta).total_seconds() > BURST_GAP_S: return False
        na, nb = _frame_no(a["rel"]), _frame_no(b["rel"])
        if na is None or nb is None or nb != na + 1: return False
        ia, ib = self.pos.get(a["id"]), self.pos.get(b["id"])
        if ia is not None and ib is not None:
            va, vb = self.M[ia], self.M[ib]
            den = float(np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0
            if float(va @ vb) / den < BURST_SIM: return False
        return True

    @staticmethod
    def _fold(cands: list[dict], fold: bool) -> list[dict]:
        """Every row that shares a duplicate set or a burst with another row in cands gets group = {kind, n,
        members, sharpest}; with fold on only the first of each set (in the order given) is kept."""
        sets: dict[tuple, list[dict]] = {}
        for p in cands:
            p["group"] = None
            key = ("burst", p["burst"]) if p.get("burst") else (("dup", p["dup"]) if p.get("dup") else None)
            if key is not None:
                sets.setdefault(key, []).append(p)
        for key, members in sets.items():
            if len(members) < 2:
                continue
            best = max(members, key=lambda m: (m.get("sharp") or 0.0, -m["id"]))
            info = {"kind": "burst" if key[0] == "burst" else "copies", "n": len(members),
                    "members": [{"id": m["id"], "rel": m["rel"], "qhash": m["qhash"], "sharp_pct": round(m["sharp_pct"], 1)} for m in members],
                    "sharpest": best["id"]}
            for m in members: m["group"] = info
        if not fold:
            return cands
        seen: set = set(); out = []
        for p in cands:
            g = p["group"]
            if g is None: out.append(p); continue
            key = ("burst", p["burst"]) if p.get("burst") else ("dup", p["dup"])
            if key in seen: continue
            seen.add(key); out.append(p)
        return out
    # ===== end duplicates and bursts =====

    def refresh(self):
        # Open a connection local to the calling thread: sqlite3 connections
        # (check_same_thread=True by default) can't cross threads, and this
        # Index is often built on one thread (app startup) then queried from
        # FastAPI's worker threadpool.
        conn = db.connect(self.root)
        rows = conn.execute("SELECT id, rel, size, mtime, lat, lon, qhash, sharp, n_faces, taken_at, width, height, category, category_score, category_guess, category_guess_score, cluster, cluster_score, kind, duration, camera, aerial, focus FROM photos WHERE status='ok' ORDER BY id").fetchall()
        self.photos = {r["id"]: dict(r) for r in rows}
        for p in self.photos.values():
            p["when"] = when_of(p)
        sharp = np.array([r["sharp"] or 0.0 for r in rows], float)
        order = sharp.argsort().argsort()
        for r, rank in zip(rows, order):
            self.photos[r["id"]]["sharp_pct"] = float(rank) / max(len(rows) - 1, 1) * 100
        self.ids, self.M = db.load_embeds(conn)
        self.pos = {pid: i for i, pid in enumerate(self.ids.tolist())}
        self._find_groups()

    def _person_photo_ids(self, person_id: int) -> set[int]:
        conn = db.connect(self.root)
        return {r[0] for r in conn.execute("SELECT DISTINCT photo_id FROM faces WHERE person_id=?", (person_id,))}

    def _passes(self, p: dict, f: Filters, person_ids: set[int] | None) -> bool:
        if f.sharp_min_pct is not None and p["sharp_pct"] < f.sharp_min_pct: return False
        n = p["n_faces"] or 0
        if f.faces == "none" and n != 0: return False
        if f.faces == "one" and n != 1: return False
        if f.faces == "two" and n != 2: return False
        if f.faces == "group" and n < GROUP_MIN_FACES: return False
        if person_ids is not None and p["id"] not in person_ids: return False
        if f.kind is not None:
            is_video = p.get("kind") == "video"          # a NULL kind (row from before videos) is a photo
            if f.kind == "videos" and not is_video: return False
            if f.kind == "photos" and is_video: return False
        if f.aerial is not None and bool(p.get("aerial")) != f.aerial: return False
        focus = p.get("focus")
        if f.hide_bad and focus == "bad": return False
        if f.hide_soft and focus in ("soft", "bad"): return False
        # "unclassified" mirrors db.category_counts' label for a NULL category (never classified).
        if f.category is not None and category_match(p, f.category) is None: return False
        if f.cluster is not None and cluster_match(p, f.cluster) is None: return False
        t = p["taken_at"] or ""
        if f.taken_from and t < f.taken_from: return False
        if f.taken_to and t > f.taken_to: return False
        # The day rows in the sidebar are built from when_of, so the filter behind them has to be too, or a
        # screenshot counted on 18 Sep would vanish the moment you clicked 18 Sep.
        if f.day and (p["when"] or "")[:10] != f.day: return False
        # The map's box. A file with no location is never in a box: it is not "somewhere else", it is nowhere.
        if f.bbox is not None:
            lat, lon = p.get("lat"), p.get("lon")
            if lat is None or lon is None: return False
            south, west, north, east = f.bbox
            if not (south <= lat <= north): return False
            if west <= east:
                if not (west <= lon <= east): return False
            elif not (lon >= west or lon <= east):   # a box dragged across the date line
                return False
        return True

    def _confidence(self, p: dict, f: Filters) -> None:
        """Sets p["sure"] and p["confidence"] for the category/cluster filters in force (both must be sure;
        the lower confidence wins). Without either filter every photo is sure."""
        sure, conf = True, 1.0
        for m in ((category_match(p, f.category) if f.category is not None else None),
                  (cluster_match(p, f.cluster) if f.cluster is not None else None)):
            if m is not None:
                sure, conf = sure and m[0], min(conf, m[1])
        p["sure"], p["confidence"] = sure, round(conf, 4)

    def _order(self, cands: list[dict], sort: str | None) -> None:
        """The order of an unsearched grid. Capture time, oldest first, is the default: a shoot reads in the
        order it happened. The others are the sort control, and all of them settle ties on rel so paging is
        stable. A folded set shows its first row in this order, so "biggest" shows the biggest copy."""
        if sort == "newest":
            cands.sort(key=lambda p: (p["when"], p["rel"]), reverse=True)
        elif sort == "biggest":
            cands.sort(key=lambda p: (-(p["size"] or 0), p["rel"]))
        elif sort == "smallest":
            cands.sort(key=lambda p: ((p["size"] or 0), p["rel"]))
        elif sort == "name":
            cands.sort(key=lambda p: (p["rel"].lower(), p["rel"]))
        elif sort == "longest":
            cands.sort(key=lambda p: (-(p["duration"] or 0.0), p["rel"]))
        else:
            cands.sort(key=lambda p: (p["when"], p["rel"]))

    def points(self) -> list[tuple[float, float]]:
        """(lat, lon) for every ok row that carries one. The map draws these and nothing else."""
        return [(float(p["lat"]), float(p["lon"])) for p in self.photos.values()
                if p.get("lat") is not None and p.get("lon") is not None]

    def days(self) -> list[dict]:
        """One row per calendar day with anything in it, oldest first: {day, n, videos, bytes}. Built from
        when_of, so files with no EXIF time still land on the day they were written."""
        out: dict[str, dict] = {}
        for p in self.photos.values():
            day = (p["when"] or "")[:10]
            if len(day) != 10:
                continue
            d = out.setdefault(day, {"day": day, "n": 0, "videos": 0, "bytes": 0})
            d["n"] += 1
            d["bytes"] += int(p["size"] or 0)
            if p.get("kind") == "video":
                d["videos"] += 1
        return [out[k] for k in sorted(out)]

    def query(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(),
              sort: str | None = None) -> list[dict]:
        """Every photo that passes the filters, in sort order, or by similarity when there is a text or image
        query (a query scores every photo, so ranking it any other way throws the query away: the UI greys
        the sort control out while one is running). With a category or cluster filter the sure ones come
        first in that order, then the "less sure" band (sure=False) sorted by confidence desc; sure_only
        drops the band. Every row in a duplicate set or a burst carries group (see _fold); fold keeps the
        first of each set only."""
        person_ids = self._person_photo_ids(filters.person_id) if filters.person_id is not None else None
        cands = [p for p in self.photos.values() if self._passes(p, filters, person_ids)]
        for p in cands: self._confidence(p, filters)
        if filters.sure_only:
            cands = [p for p in cands if p["sure"]]
        if text or image_id is not None:
            if image_id is not None:
                i = self.pos.get(image_id)
                if i is None:
                    raise LookupError(f"no embedding for photo {image_id}")
                q = self.M[i]
            else:
                from .embed import get_embedder
                q = get_embedder().encode_text([text])[0]
            scores = self.M @ q
            for p in cands:
                i = self.pos.get(p["id"]); p["score"] = float(scores[i]) if i is not None else -1.0
            cands.sort(key=lambda p: -p["score"])
        else:
            for p in cands: p["score"] = 0.0
            self._order(cands, sort)
        unsure = [p for p in cands if not p["sure"]]
        if unsure:
            unsure.sort(key=lambda p: -p["confidence"])     # stable: ties keep the order above
            cands = [p for p in cands if p["sure"]] + unsure
        return self._fold(cands, filters.fold)

    def search(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(),
               limit: int = 200, offset: int = 0, sort: str | None = None) -> list[dict]:
        return [dict(p) for p in self.query(text, image_id, filters, sort)[offset:offset + limit]]
