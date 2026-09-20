from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from . import db
from .config import GROUP_MIN_FACES, CATEGORY_FALLBACK, SURE_MIN

@dataclass
class Filters:
    sharp_min_pct: float | None = None
    faces: str | None = None
    person_id: int | None = None
    taken_from: str | None = None
    taken_to: str | None = None
    category: str | None = None
    kind: str | None = None         # "photos" | "videos" | None for both
    cluster: str | None = None      # a discovered category (photos.cluster)
    sure_only: bool = False         # drop the "less sure" band (exports want only what the model is sure of)
    aerial: bool | None = None      # True: drone shots only (photos.aerial), False: none of them, None: both
    hide_bad: bool = False          # drop rows the focus pass labelled bad (photos.focus); unchecked rows stay
    hide_soft: bool = False         # drop soft and bad

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

class Index:
    def __init__(self, root: Path):
        self.root = Path(root); self.refresh()

    def refresh(self):
        # Open a connection local to the calling thread: sqlite3 connections
        # (check_same_thread=True by default) can't cross threads, and this
        # Index is often built on one thread (app startup) then queried from
        # FastAPI's worker threadpool.
        conn = db.connect(self.root)
        rows = conn.execute("SELECT id, rel, qhash, sharp, n_faces, taken_at, width, height, category, category_score, category_guess, category_guess_score, cluster, cluster_score, kind, duration, camera, aerial, focus FROM photos WHERE status='ok' ORDER BY id").fetchall()
        self.photos = {r["id"]: dict(r) for r in rows}
        sharp = np.array([r["sharp"] or 0.0 for r in rows], float)
        order = sharp.argsort().argsort()
        for r, rank in zip(rows, order):
            self.photos[r["id"]]["sharp_pct"] = float(rank) / max(len(rows) - 1, 1) * 100
        self.ids, self.M = db.load_embeds(conn)
        self.pos = {pid: i for i, pid in enumerate(self.ids.tolist())}

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

    def query(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters()) -> list[dict]:
        """Every photo that passes the filters, sorted by similarity (text or image query) or by capture time.
        With a category or cluster filter the sure ones come first in that order, then the "less sure"
        band (sure=False) sorted by confidence desc; sure_only drops the band."""
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
            cands.sort(key=lambda p: ((p["taken_at"] or "~"), p["rel"]))
        unsure = [p for p in cands if not p["sure"]]
        if unsure:
            unsure.sort(key=lambda p: -p["confidence"])     # stable: ties keep the order above
            cands = [p for p in cands if p["sure"]] + unsure
        return cands

    def search(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(),
               limit: int = 200, offset: int = 0) -> list[dict]:
        return [dict(p) for p in self.query(text, image_id, filters)[offset:offset + limit]]
