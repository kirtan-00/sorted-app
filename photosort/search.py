from __future__ import annotations
import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from . import db
import math
from .config import (GROUP_MIN_FACES, CATEGORY_FALLBACK, SURE_MIN, BURST_SIM, BURST_GAP_S, ANGLE_WINDOW_S, SLOWMO_MIN_FPS,
                     DURATION_BUCKETS, BRACKET_GAP_S, BRACKET_MIN_STEP, BRACKET_FRAMING_MIN, BRACKET_MIN_FRAMES, CAMERA_BUCKETS,
                     BRACKET_MIN_SPAN, BURST_MAX_STEP)
GROUP_KINDS = ("burst", "bracket", "copies")           # the group filter's values; "copies" is a duplicate set
CAMERA_AXES = ("focal", "iso", "shutter", "aperture", "flash")   # bucketed camera facets (config.CAMERA_BUCKETS); lens is exact
LOOKS = ("log", "standard")            # the two answers the picture profile filter takes (see _passes)
DURATION_KEYS = tuple(k for k, _, _ in DURATION_BUCKETS)

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
    # close | medium | wide | "unknown" (scored, no clear answer). A row the pass has never scored
    # (shot_size NULL) is never in any of the four: it is not a fifth framing, it is an absence.
    shot_size: str | None = None
    slowmo: bool | None = None      # True: clips at SLOWMO_MIN_FPS or above, False: clips below it (see _passes)
    camera: str | None = None       # photos.camera exactly, stills and clips alike: one body shoots both
    look: str | None = None         # "log": an S-Log clip, the footage that needs grading; "standard": any other named profile
    duration: str | None = None     # a config.DURATION_BUCKETS key; clips only
    hide_bad: bool = False          # drop rows the focus pass labelled bad (photos.focus); unchecked rows stay
    hide_soft: bool = False         # drop soft and bad
    fold: bool = False              # one tile per duplicate set and per burst (the first in order shows; see Index.groups)
    # ===== marks (see _passes_marks) =====
    flag: str | None = None         # "pick" | "reject" | "none" (neither)
    stars_min: int | None = None    # at least this many stars, 1 to 5
    label: str | None = None        # one of db.LABELS, or "none" (no label)
    # ===== end marks =====
    pile: bool = False              # only what the editor has picked (photosort.pile, the pile table in the index)
    # ===== shoot modes (docs/shoot-modes.md) =====
    group: str | None = None        # "burst" | "bracket" | "copies": only rows inside a set of that kind (see Index.groups)
    lens: str | None = None         # photos.lens exactly, or "unknown" (a still whose file names no lens, or never read)
    focal: str | None = None        # a CAMERA_BUCKETS["focal"] key or "unknown"; same for the three below
    iso: str | None = None
    shutter: str | None = None
    aperture: str | None = None
    flash: str | None = None        # "fired", "none" or "unknown"
    flicker: str | None = None      # "pulsing", "banding" or "none": what flicker.check_clip found in a clip
    product: int | None = None      # a product from products.find_products (the products table)
    variant: int | None = None      # one colour variant inside that product
    # ===== end shoot modes =====

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
    # Every other category the row fits (classify.SECOND_MIN_PROB, photo_categories). A runner-up took less
    # of the softmax than the winner did, so it sorts into the less sure band by its own probability.
    s = (p.get("also") or {}).get(cat)
    if s is not None:
        return float(s) >= SURE_MIN, float(s)
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

def angles(rows: list[dict], window_s: float = ANGLE_WINDOW_S) -> dict:
    """The same moment from the other cameras, out of an already ranked result (backlog task D, search
    diversity).

    Nearest neighbour on an embedding answers "what looks like this", and what looks most like a frame is
    the next frame from the same body: the top of a similarity list is a burst, fifty tiles of the shot you
    already had. Nobody asks that. What a production team asks is who else was covering this, so the answer
    is a set of angles, not a list of near misses.

    Two cuts of the same result, because they answer different halves of the question:

      cameras  one row per camera, its best scoring match, ranked by that score. This is the answer when
               the reference came off a phone or a client's deck and there is no moment to anchor to.
      moment   the best match overall is the anchor; everything shot within window_s either side of it by
               a camera that is not the anchor's is the same moment from somewhere else. Time is the only
               honest way to say "the same moment": two bodies fifteen metres apart on the same action do
               not look alike to an embedding, which is exactly why similarity alone keeps returning the
               burst instead. ANGLE_WINDOW_S says where 300 s comes from.

    A NULL camera is an unknown camera, not a distinct one, and there are a lot of them: 739 of 1,627 rows
    on the client's shoot. Every one of them is a clip, and the shape is worth knowing: all 487 stills
    carry a camera, and so do 401 of the 1,140 clips (an iPhone .MOV names itself), but a DJI or GoPro
    .MP4 does not. So they are never a group in cameras and never counted as another angle, which would
    be the UI stating something nobody measured. They are not thrown away either, since half the shoot is
    not an acceptable thing to hide: they come back in their own bucket, labelled unknown, and separately
    inside the moment window, where a clip rolling across the same minute is often the best answer on the
    page. When the anchor itself has no camera, anchor_camera comes back null and every named camera in the
    window lands in others: "a camera we can name" is the most that can be said, and the null says so.

    Pass the WHOLE ranked result, not the first page: a camera whose best frame ranks four hundredth is
    precisely the angle this exists to surface, and paging it away first would rebuild the burst problem.
    Rows are copied, not marked in place: the dicts belong to the Index.
    """
    known = [r for r in rows if r.get("camera")]
    unknown = [r for r in rows if not r.get("camera")]
    best: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for r in known:
        cam = r["camera"]
        counts[cam] = counts.get(cam, 0) + 1
        if cam not in best or r.get("score", 0.0) > best[cam].get("score", 0.0):
            best[cam] = r
    cameras = [{"camera": cam, "n": counts[cam], "best": dict(best[cam])} for cam in
               sorted(best, key=lambda c: -best[c].get("score", 0.0))]
    out = {"cameras": cameras, "window_s": window_s,
           "unknown": ({"n": len(unknown), "best": dict(unknown[0])} if unknown else None),
           "moment": None}
    if not rows:
        return out
    anchor = rows[0]
    at = _taken(anchor)
    if at is None:                      # nothing to anchor to: a row with no capture time has no moment
        return out
    near, near_unknown = [], []
    for r in rows:
        if r["id"] == anchor["id"]:
            continue
        t = _taken(r)
        if t is None:
            continue
        dt = (t - at).total_seconds()
        if abs(dt) > window_s:
            continue
        row = dict(r, dt_s=round(dt, 1))
        if not r.get("camera"):
            near_unknown.append(row)
        elif r["camera"] != anchor.get("camera"):
            near.append(row)
    near.sort(key=lambda r: -r.get("score", 0.0))
    near_unknown.sort(key=lambda r: -r.get("score", 0.0))
    out["moment"] = {"anchor_id": anchor["id"], "anchor_camera": anchor.get("camera"),
                     "taken_at": anchor.get("taken_at"), "others": near, "unknown": near_unknown}
    return out

# ===== shoot modes: exposure arithmetic, the brightness-blind framing compare, and the camera buckets =====
def ev100(p: dict) -> float | None:
    """The exposure a frame was given, in stops at ISO 100: log2(N^2 / t) - log2(ISO / 100). Lower is brighter.
    None unless the file carried all three; a bracket off a camera that wrote none of them falls back to ev_bias."""
    t, n, iso = p.get("exposure_s"), p.get("fnumber"), p.get("iso")
    if not (t and n and iso):
        return None
    return math.log2(n * n / t) - math.log2(iso / 100.0)

def _brightness(p: dict) -> float | None:
    """How bright a frame was made, in stops, higher is brighter: -ev100, else the EV compensation it was shot at."""
    e = ev100(p)
    if e is not None:
        return -e
    b = p.get("ev_bias")
    return float(b) if b is not None else None

def ev_label(x: float) -> str:
    """Stops as a photographer writes them, to the nearest third: "0", "+2", "-1.3", "+0.7"."""
    v = round(x * 3) / 3
    if abs(v) < 1e-6:
        return "0"
    txt = f"{v:+.0f}" if abs(v - round(v)) < 1e-6 else f"{v:+.1f}"
    return txt

def bucket(axis: str, p: dict) -> str:
    """The camera facet value a still counts under on axis: a CAMERA_BUCKETS key, or "unknown"."""
    if axis == "flash":
        f = p.get("flash")
        return "unknown" if f is None or f < 0 else "fired" if f & 1 else "none"
    v = {"focal": p.get("focal_mm"), "iso": p.get("iso"), "shutter": p.get("exposure_s"), "aperture": p.get("fnumber")}[axis]
    if not v:
        return "unknown"
    return next(k for k, hi, _ in CAMERA_BUCKETS[axis] if float(v) <= hi)

_FIELD_N = 32         # the framing compare's grid: 32 x 21 cells off the grid thumb
_FIELD_SHIFT = 2      # best over +-2 cells of drift, about 6% of the frame: a handheld bracket wanders

def framing_field(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """Horizontal and vertical gradients of log luminance on a 32 px copy of an image: what is in the frame and where,
    with the brightness taken out (a stop up or down is a constant added to log luminance, which a gradient drops)."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            g = np.log1p(np.asarray(im.convert("L").resize((_FIELD_N, _FIELD_N * 2 // 3), Image.BILINEAR), np.float32))
    except Exception:
        return None
    return np.diff(g, axis=1)[:-1], np.diff(g, axis=0)[:, :-1]

def framing_similarity(fa, fb) -> float:
    """Correlation of two framing fields, the best over a small shift. 1 is the same frame at any exposure."""
    (ax, ay), (bx, by) = fa, fb
    h, w = ax.shape
    best = -1.0
    for dy in range(-_FIELD_SHIFT, _FIELD_SHIFT + 1):
        for dx in range(-_FIELD_SHIFT, _FIELD_SHIFT + 1):
            def cut(m, oy, ox):
                return m[max(oy, 0):h + min(oy, 0), max(ox, 0):w + min(ox, 0)]
            va = np.concatenate([cut(ax, dy, dx).ravel(), cut(ay, dy, dx).ravel()])
            vb = np.concatenate([cut(bx, -dy, -dx).ravel(), cut(by, -dy, -dx).ravel()])
            va = va - va.mean(); vb = vb - vb.mean()
            den = float(np.linalg.norm(va) * np.linalg.norm(vb))
            if den > 0:
                best = max(best, float(va @ vb) / den)
    return best
# ===== end shoot modes helpers =====


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
            p["dup"] = None; p["burst"] = None; p["bracket"] = None; p["ev"] = None
            if p.get("qhash"):
                by_hash.setdefault(p["qhash"], []).append(p["id"])
        for qh, ids in by_hash.items():
            if len(ids) > 1:
                for i in ids: self.photos[i]["dup"] = qh
        stills = [p for p in self.photos.values() if p.get("kind") != "video" and _taken(p) is not None and _frame_no(p["rel"]) is not None]
        stills.sort(key=lambda p: (str(Path(p["rel"]).parent), _taken(p), p.get("subsec") or 0.0, p["rel"]))
        self._find_brackets(stills)
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

    # ===== shoot modes: exposure brackets =====
    def _framing(self, a: dict, b: dict) -> float:
        grid = db.index_dir(self.root) / "grid"
        fields = []
        for p in (a, b):
            if p["qhash"] not in self._fields:
                self._fields[p["qhash"]] = framing_field(grid / f"{p['qhash']}.jpg")
            fields.append(self._fields[p["qhash"]])
        return framing_similarity(*fields) if fields[0] is not None and fields[1] is not None else -1.0

    def _bracket_pair(self, a: dict, b: dict, run: list[dict] | None = None) -> bool:
        """a then b could be neighbours in one bracket: same folder and body, the next frame number, within
        BRACKET_GAP_S plus a's own shutter time, a different exposure by BRACKET_MIN_STEP or more, the same framing
        as some frame already in the run (a Sony brackets 0, -2, +2: the -2 and the +2 are four stops apart and
        share little detail, but each matches the 0)."""
        if Path(a["rel"]).parent != Path(b["rel"]).parent or a.get("camera") != b.get("camera"): return False
        na, nb = _frame_no(a["rel"]), _frame_no(b["rel"])
        if na is None or nb is None or nb != na + 1: return False
        ta, tb = _taken(a), _taken(b)
        if ta is None or tb is None: return False
        gap = (tb - ta).total_seconds() + (b.get("subsec") or 0.0) - (a.get("subsec") or 0.0)
        if not 0 <= gap <= BRACKET_GAP_S + float(a.get("exposure_s") or 0.0): return False
        ea, eb = _brightness(a), _brightness(b)
        if ea is None or eb is None or abs(ea - eb) < BRACKET_MIN_STEP: return False
        return any(self._framing(q, b) >= BRACKET_FRAMING_MIN for q in (run or [a]))

    def _find_brackets(self, stills: list[dict]) -> None:
        """Marks p["bracket"] (an int id) and p["ev"] (its exposure against the set's middle frame, in stops) on every
        frame of an exposure bracket. A run breaks where a pair fails _bracket_pair and also where an exposure comes
        round again, so two brackets of the same view shot back to back (0 -2 +2 0 -2 +2) stay two sets."""
        self._fields: dict = {}
        gid = 0; run: list[dict] = []
        def close():
            nonlocal gid
            said = all(p.get("exp_mode") == 2 for p in run)          # the camera wrote "auto bracket"
            br0 = [_brightness(p) or 0.0 for p in run]
            wide = bool(run) and max(br0) - min(br0) >= BRACKET_MIN_SPAN - 1e-6   # not auto exposure drifting by thirds
            if (len(run) >= BRACKET_MIN_FRAMES and (wide or said)) or (len(run) >= 2 and said):
                gid += 1
                br = [_brightness(p) for p in run]
                mid = sorted(br)[len(br) // 2]
                for p, b in zip(run, br):
                    p["bracket"] = gid; p["ev"] = round(b - mid, 2)
            run.clear()
        prev = None
        for p in stills:
            repeat = any(abs((_brightness(p) or 0.0) - (_brightness(q) or 0.0)) < BRACKET_MIN_STEP for q in run)
            if prev is not None and run and not repeat and self._bracket_pair(prev, p, run):
                run.append(p)
            else:
                close(); run.append(p)
            prev = p
        close()
    # ===== end exposure brackets =====

    def _burst_pair(self, a: dict, b: dict) -> bool:
        if Path(a["rel"]).parent != Path(b["rel"]).parent: return False
        # a frame of an exposure bracket is never also a burst frame, and two frames a whole stop or more apart are not
        # a burst either; auto exposure drifting by thirds inside a burst is still a burst (config.BURST_MAX_STEP)
        if a.get("bracket") or b.get("bracket"): return False
        ea, eb = ev100(a), ev100(b)
        if ea is not None and eb is not None and abs(ea - eb) >= BURST_MAX_STEP - 1e-6: return False
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
            key = Index._group_key(p)
            if key is not None:
                sets.setdefault(key, []).append(p)
        reps: dict[tuple, dict] = {}
        for key, members in sets.items():
            if len(members) < 2:
                continue
            best = max(members, key=lambda m: (m.get("sharp") or 0.0, -m["id"]))
            kind = {"burst": "burst", "bracket": "bracket", "dup": "copies"}[key[0]]
            info = {"kind": kind, "n": len(members),
                    "members": [{"id": m["id"], "rel": m["rel"], "qhash": m["qhash"], "sharp_pct": round(m["sharp_pct"], 1)} for m in members],
                    "sharpest": best["id"]}
            if kind == "burst":
                # eyes: every frame had a face, so sharp is the eye crop's sharpness (features.eye_sharpness) and
                # "sharpest" is the frame whose eyes are sharpest; detail: the best tiles of the frame
                info["best_by"] = "eyes" if all((m.get("n_faces") or 0) > 0 for m in members) else "detail"
            if kind == "bracket":
                order = sorted(members, key=lambda m: (_frame_no(m["rel"]) or 0, m["rel"]))
                info["ev"] = [ev_label(m["ev"] or 0.0) for m in order]
                for d, m in zip(info["members"], members):
                    d["ev"] = ev_label(m["ev"] or 0.0)
                info["base"] = min(members, key=lambda m: (abs(m["ev"] or 0.0), m["id"]))["id"]
                reps[key] = next(m for m in members if m["id"] == info["base"])
            for m in members: m["group"] = info
        if not fold:
            return cands
        seen: set = set(); out = []
        for p in cands:
            g = p["group"]
            if g is None: out.append(p); continue
            key = Index._group_key(p)
            if key in seen: continue
            # a folded bracket shows its middle exposure, never the darkest frame that happens to come first
            seen.add(key); out.append(reps.get(key, p))
        return out

    @staticmethod
    def _group_key(p: dict) -> tuple | None:
        """The set a row folds into: a bracket before a burst before a duplicate set."""
        if p.get("bracket"): return ("bracket", p["bracket"])
        if p.get("burst"): return ("burst", p["burst"])
        if p.get("dup"): return ("dup", p["dup"])
        return None
    # ===== end duplicates and bursts =====

    def refresh(self):
        # Open a connection local to the calling thread: sqlite3 connections
        # (check_same_thread=True by default) can't cross threads, and this
        # Index is often built on one thread (app startup) then queried from
        # FastAPI's worker threadpool.
        conn = db.connect(self.root)
        # "+status" refuses the (status, ...) covering indexes for this one read. ANALYZE used to be
        # enough on its own: knowing status='ok' is nearly every row, the planner scanned. SQLite 3.50
        # picks photos_status_faces plus a temp b-tree sort even with fresh statistics, which is 43 ms
        # against 38 ms at 20k rows. Small, but this is the whole table in id order, so a scan is simply
        # the right plan and the unary plus says so in a way no planner version can drift away from.
        rows = conn.execute("SELECT id, rel, size, mtime, lat, lon, qhash, sharp, n_faces, taken_at, width, height, category, category_score, category_guess, category_guess_score, cluster, cluster_score, kind, duration, camera, aerial, focus, shot_size, shot_size_score, fps, capture_fps, gamma, people, face_h, flag, rating, label, exposure_s, fnumber, iso, ev_bias, focal_mm, lens, exp_mode, subsec, flash, flicker FROM photos WHERE +status='ok' ORDER BY id").fetchall()
        self.photos = {r["id"]: dict(r) for r in rows}
        also = db.load_photo_categories(conn)
        for p in self.photos.values():
            p["when"] = when_of(p)
            p["rating"] = p["rating"] or 0          # ===== marks: NULL on a row from before the column =====
            p["also"] = also.get(p["id"], {})
        sharp = np.array([r["sharp"] or 0.0 for r in rows], float)
        order = sharp.argsort().argsort()
        for r, rank in zip(rows, order):
            self.photos[r["id"]]["sharp_pct"] = float(rank) / max(len(rows) - 1, 1) * 100
        self.ids, self.M = db.load_embeds(conn)
        self.pos = {pid: i for i, pid in enumerate(self.ids.tolist())}
        for pid, prod, var in conn.execute("SELECT photo_id, product, variant FROM products"):   # products.find_products
            if pid in self.photos:
                self.photos[pid]["product"], self.photos[pid]["variant"] = prod, var
        self._find_groups()

    def _person_photo_ids(self, person_id: int) -> set[int]:
        conn = db.connect(self.root)
        return {r[0] for r in conn.execute("SELECT DISTINCT photo_id FROM faces WHERE person_id=?", (person_id,))}

    def _allowed_ids(self, f: Filters) -> set[int] | None:
        """The ids a person filter and the pile filter leave, both read fresh per query (the pile changes with
        every click, the people with every merge); None when neither is on."""
        out = self._person_photo_ids(f.person_id) if f.person_id is not None else None
        if f.pile:
            from . import pile
            picked = pile.ids(db.connect(self.root))
            out = picked if out is None else out & picked
        return out

    def _passes(self, p: dict, f: Filters, person_ids: set[int] | None) -> bool:
        if f.sharp_min_pct is not None and p["sharp_pct"] < f.sharp_min_pct: return False
        # A clip's head count is photos.people (faces on its frames), never n_faces, which stays 0 on a clip so
        # classify's face rule leaves interviews alone. A clip nobody has looked at has no count and so passes
        # no faces filter: before clips had faces every one of them passed "No people", which was a lie.
        if p.get("kind") == "video":
            if f.faces and p.get("people") is None: return False
            n = p.get("people") or 0
        else:
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
        if f.shot_size is not None and p.get("shot_size") != f.shot_size: return False
        # Slow motion is a fact about a clip and nothing else. A still has no frame rate, so it fails
        # both halves of this filter rather than counting as "normal speed", and a clip whose fps was
        # never read (an index from before the column, ffprobe unable to say) fails both too: the
        # honest answer there is "not known to be", not "is not".
        # The rate is the larger of the container's and the sidecar's capture rate, so an S&Q clip conformed
        # in camera (shot at 100, written at 25) is slow motion too.
        if f.slowmo is not None:
            rates = [float(x) for x in (p.get("fps"), p.get("capture_fps")) if x is not None]
            if p.get("kind") != "video" or not rates: return False
            if (max(rates) >= SLOWMO_MIN_FPS) != f.slowmo: return False
        if f.camera is not None and p.get("camera") != f.camera: return False
        # The profile and the length are facts about a clip; a still is in neither half of either. A clip whose
        # sidecar was never read (gamma NULL) or that has none ('') is not "standard": nobody knows what it is.
        if f.look is not None:
            g = (p.get("gamma") or "").lower()
            if p.get("kind") != "video" or not g: return False
            if (f.look == "log") != g.startswith("s-log"): return False
        if f.flicker is not None and (p.get("kind") != "video" or p.get("flicker") != f.flicker): return False
        if f.duration is not None:
            d = p.get("duration")
            if p.get("kind") != "video" or d is None: return False
            lo, hi = next((lo, hi) for k, lo, hi in DURATION_BUCKETS if k == f.duration)
            if not (lo <= float(d) < hi): return False
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
        if not self._passes_shoot(p, f): return False
        return self._passes_marks(p, f)

    # ===== shoot modes: groups, the camera facets and products =====
    @staticmethod
    def _passes_shoot(p: dict, f: Filters) -> bool:
        if f.group is not None:
            if f.group == "burst" and not p.get("burst"): return False
            if f.group == "bracket" and not p.get("bracket"): return False
            if f.group == "copies" and not p.get("dup"): return False
        # The camera facts are facts about a still; a clip is in none of their values, "unknown" included.
        if f.lens is not None:
            if p.get("kind") == "video" or (p.get("lens") or "unknown") != f.lens: return False
        for axis in CAMERA_AXES:
            want = getattr(f, axis)
            if want is not None and (p.get("kind") == "video" or bucket(axis, p) != want): return False
        if f.product is not None and p.get("product") != f.product: return False
        if f.variant is not None and p.get("variant") != f.variant: return False
        return True
    # ===== end shoot modes =====

    # ===== marks: flag, minimum stars and colour label, all set by hand (db.set_marks) =====
    @staticmethod
    def _passes_marks(p: dict, f: Filters) -> bool:
        if f.flag is not None and (p.get("flag") or "none") != f.flag: return False
        if f.stars_min and (p.get("rating") or 0) < f.stars_min: return False
        if f.label is not None and (p.get("label") or "none") != f.label: return False
        return True

    def set_marks(self, marks: dict[int, dict]) -> None:
        """Patch the live rows after db.set_marks, so a keystroke in the grid does not cost a whole refresh."""
        for pid, m in marks.items():
            p = self.photos.get(int(pid))
            if p is not None:
                p.update(flag=m.get("flag"), rating=m.get("rating") or 0, label=m.get("label"))
    # ===== end marks =====

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
              sort: str | None = None, vector: np.ndarray | None = None) -> list[dict]:
        """Every photo that passes the filters, in sort order, or by similarity when there is a text or image
        query (a query scores every photo, so ranking it any other way throws the query away: the UI greys
        the sort control out while one is running). With a category or cluster filter the sure ones come
        first in that order, then the "less sure" band (sure=False) sorted by confidence desc; sure_only
        drops the band. Every row in a duplicate set or a burst carries group (see _fold); fold keeps the
        first of each set only.

        vector is an already-encoded query embedding, for a reference image that is not in this shoot: the
        upload endpoint encodes the dropped file and hands the result here. It scores through exactly the
        same matrix product as image_id, because a second scorer would be a second thing to keep in step
        with the encoder, the normalisation and the sure band."""
        person_ids = self._allowed_ids(filters)
        cands = [p for p in self.photos.values() if self._passes(p, filters, person_ids)]
        for p in cands: self._confidence(p, filters)
        if filters.sure_only:
            cands = [p for p in cands if p["sure"]]
        if text or image_id is not None or vector is not None:
            if vector is not None:
                q = np.asarray(vector, np.float32).reshape(-1)
                if q.shape[0] != self.M.shape[1]:
                    # The encoder that filled this index and the one that made the vector disagree, which
                    # can only happen if PHOTOSORT_CLIP_MODEL changed under a shoot. Say so here rather
                    # than let numpy raise a shape error from inside the dot product.
                    raise LookupError(f"query embedding has {q.shape[0]} dims, this index has {self.M.shape[1]}")
            elif image_id is not None:
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

    # ===== live facet counts: what each filter would leave, given the others (Q4) =====
    # Each axis is counted under every filter in force except its own, the usual faceted search rule: with
    # videos and one camera on, the camera list still says what every other camera would show (of the videos),
    # so ticking one is never a guess. The axes and the Filters fields behind them:
    FACET_FIELDS = {"kind": ("kind",), "faces": ("faces",), "shot_size": ("shot_size",), "slowmo": ("slowmo",),
                    "cameras": ("camera",), "look": ("look",), "duration": ("duration",), "drone": ("aerial",),
                    "categories": ("category",), "days": ("day",),
                    "groups": ("group",), "lens": ("lens",), "focal": ("focal",), "iso": ("iso",), "shutter": ("shutter",),
                    "aperture": ("aperture",), "flash": ("flash",), "flicker": ("flicker",)}

    @staticmethod
    def _facet_values(p: dict, axis: str) -> list[str]:
        """The values row p counts under on one axis, by exactly the rules _passes and category_match use: a
        clip nobody checked for faces has no head count, a still has no frame rate, profile or length, a row
        with a secondary category counts under that too."""
        video = p.get("kind") == "video"
        if axis == "kind":
            return ["videos" if video else "photos"]
        if axis == "faces":
            if video and p.get("people") is None:
                return []
            n = (p.get("people") if video else p.get("n_faces")) or 0
            return ["none" if n == 0 else "one" if n == 1 else "two" if n == 2 else "group" if n >= GROUP_MIN_FACES else ""]
        if axis == "shot_size":
            return [p["shot_size"]] if p.get("shot_size") else []
        if axis == "slowmo":
            rates = [float(x) for x in (p.get("fps"), p.get("capture_fps")) if x is not None]
            return ["slow" if max(rates) >= SLOWMO_MIN_FPS else "normal"] if video and rates else []
        if axis == "cameras":
            return [p["camera"]] if p.get("camera") else []
        if axis == "flicker":
            return [p["flicker"]] if video and p.get("flicker") in ("pulsing", "banding", "none") else []
        if axis == "look":
            g = (p.get("gamma") or "").lower()
            return ["log" if g.startswith("s-log") else "standard"] if video and g else []
        if axis == "duration":
            d = p.get("duration")
            if not video or d is None:
                return []
            return [k for k, lo, hi in DURATION_BUCKETS if lo <= float(d) < hi]
        if axis == "drone":
            return ["drone"] if p.get("aerial") else []
        if axis == "categories":
            cat = p.get("category")
            if cat is None:
                return ["unclassified"]
            out = [cat]
            if cat in (CATEGORY_FALLBACK, "people") and p.get("category_guess") and p["category_guess"] != cat:
                out.append(p["category_guess"])
            return out + [c for c in (p.get("also") or {}) if c not in out]
        if axis == "days":
            day = (p.get("when") or "")[:10]
            return [day] if len(day) == 10 else []
        if axis == "groups":
            return [k for k, on in (("burst", p.get("burst")), ("bracket", p.get("bracket")), ("copies", p.get("dup"))) if on]
        if axis == "lens":
            return [] if video else [p.get("lens") or "unknown"]
        if axis in CAMERA_AXES:
            return [] if video else [bucket(axis, p)]
        raise ValueError(axis)

    def facet_counts(self, filters: Filters = Filters()) -> dict:
        """{total, kind, faces, shot_size, slowmo, cameras, look, duration, drone, categories, days}: total is what
        query() would return with these filters; every axis is {value: rows} counted with its own filter taken
        off (drone is one number). One pass for the filters that are not axes, one per axis filter in force."""
        from dataclasses import replace
        person_ids = self._allowed_ids(filters)
        base = replace(filters, **{f: None for fs in self.FACET_FIELDS.values() for f in fs})
        rows = [p for p in self.photos.values() if self._passes(p, base, person_ids)]
        on = {axis: fs for axis, fs in self.FACET_FIELDS.items() if any(getattr(filters, f) is not None for f in fs)}
        fails: dict[str, set[int]] = {}
        for axis, fs in on.items():
            only = Filters(**{f: getattr(filters, f) for f in fs})
            fails[axis] = {p["id"] for p in rows if not self._passes(p, only, None)}
        out: dict = {"total": sum(1 for p in rows if not any(p["id"] in s for s in fails.values()))}
        # how many sets, not rows, each group kind has among what passes: "bursts 12 in 3"
        live = [p for p in rows if not any(p["id"] in s for a, s in fails.items() if a != "groups")]
        out["group_sets"] = {k: len({p[f] for p in live if p.get(f)}) for k, f in (("burst", "burst"), ("bracket", "bracket"), ("copies", "dup"))}
        for axis in self.FACET_FIELDS:
            others = [s for a, s in fails.items() if a != axis]
            counts: dict[str, int] = {}
            for p in rows:
                if any(p["id"] in s for s in others):
                    continue
                for v in self._facet_values(p, axis):
                    if v:
                        counts[v] = counts.get(v, 0) + 1
            if axis in ("cameras", "lens"):
                counts = dict(sorted(counts.items(), key=lambda kv: (kv[0] == "unknown", -kv[1], kv[0])))
            elif axis == "days":
                counts = dict(sorted(counts.items()))
            out[axis] = counts.get("drone", 0) if axis == "drone" else counts
        return out
    # ===== end live facet counts =====

    def search(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(),
               limit: int = 200, offset: int = 0, sort: str | None = None,
               vector: np.ndarray | None = None) -> list[dict]:
        return [dict(p) for p in self.query(text, image_id, filters, sort, vector)[offset:offset + limit]]
