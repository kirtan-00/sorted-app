"""Client pick: a product shoot cut down to what the client sees.

A product shoot is thousands of frames of a few dozen products: every product in several colours, every colour from
several angles, every angle shot a few times over. The client wants about ten a product. Client pick works on what
Find products (products.py) already grouped and does three things:

1. Measures every frame on the product, not on the frame. The product is the part of the picture that differs from
   the backdrop (a plane fitted to the frame's border in Lab, so a lit sweep that darkens towards the floor is still
   backdrop). Sharpness, motion, edges and clipping are all read inside that region, so a sharp linen tabletop under
   a soft product does not pass for focus, the way it does for the whole-frame check in focus.py.
2. Rejects with a reason in plain words: soft on the product, motion blur, cut off by the frame edge, blown out,
   crushed to black, a retake of an angle already kept. A frame is soft or shaken against the sharpest frame of its
   own angle, never against the shoot: a matte mug and a laced sneaker are not equally detailed.
3. Picks the best N a product: every colour first, then every angle, then quality, one frame per retake.

Reads the thumbs on this Mac only (thumbs/ for the measurements, grid/ for framing): the shoot disk can be unplugged.
Every threshold below was read off scripts/make_cull_set.py --products 8 --seed 1 and checked on the held-out
--products 30 --seed 2 set; the numbers are in docs/client-pick.md.

No eyes-open check: YuNet gives eye centres, not eyelids (docs/shoot-modes.md), so a blink is not detected.
"""
from __future__ import annotations
import json, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
import numpy as np
from . import db

# ===== thresholds (docs/client-pick.md has the distributions each one was read off) =====
MASK_EDGE = 160          # the product mask is found on a copy this many pixels on the long side
RING = 3                 # border rows the backdrop plane is fitted to, on that copy
FG_DE = 14.0             # CIE76 delta E from the backdrop plane above which a pixel is product
MIN_BLOB = 0.002         # a speck smaller than this share of the frame is dust or grain, not product
MIN_PRODUCT = 0.005      # less product than this and the frame has no product to measure
DIR_TOP = 40             # motion: the strongest this many edge pixels of each direction
TOP_SHARE = 0.01         # sharpness: the mean of the strongest 1% of colour gradients inside the product
SOFT_RATIO = 0.72        # soft: under this share of the sharpest frame of its own angle
BLUR_RATIO = 0.85        # motion: detail lost overall, at least down to this share
MOTION_ANISO = 0.525     # motion: one direction kept under this share of the other, against the sharp frame
MOTION_STRONG = 0.15     # motion even with the overall detail kept: one direction all but gone
CUT_INTENT = 3           # this many frames of one retake group off the same edges is how it was framed
CUT_SIDE = 0.04          # cut off: the product covers this share of one side of the frame (or two that meet)
BLOWN_SHARE = 0.14       # blown: this share more of the product at 250 or more (brightest channel) than its angle's least
CRUSHED_SHARE = 0.12     # crushed: this share more of the product at 8 or less in every channel than its angle's least
REF_FRAMING = 0.6        # the frames a frame is held against for focus: framed at least this like it
RETAKE_FRAMING = 0.78    # a retake: framing correlation (search.framing_similarity) at least this, same colour
ANGLE_FRAMING = 0.70     # the same angle in another colour: framing correlation at least this
ATTACHED_CAN_PICK = False  # a frame put back next to a product is never picked for you (76% land on the right one)
ATTACH_SIM = 0.80        # a stray frame joins its nearest product at this grey cosine or above
FRAGMENT_SHARE = 0.05    # a "product" under this share of the biggest one's frames is a fragment, not a product
FRAGMENT_MIN = 4         # and one of fewer frames than this always is
DEFECT_CLUSTER = 0.6     # a "product" whose frames are mostly rejects is a cluster of rejects, not a product
DEFAULT_PER_PRODUCT = 10

REASONS = {
    "blown": "blown out on the product",
    "crushed": "crushed to black on the product",
    "cut": "product cut off by the frame edge",
    "motion": "motion blur",
    "soft": "soft on the product",
    "stray": "not sure which product this is",
    "retake": "retake of this angle, a sharper one is kept",
}
SHORT = {"blown": "blown out", "crushed": "crushed to black", "cut": "cut off", "motion": "motion blur", "soft": "soft",
         "stray": "not matched", "retake": "retake"}
REASON_ORDER = ("blown", "crushed", "cut", "motion", "soft", "stray", "retake")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cull_measure(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, qhash TEXT, m TEXT);
CREATE TABLE IF NOT EXISTS cull(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, product INTEGER NOT NULL, variant INTEGER NOT NULL,
  angle INTEGER, retake INTEGER, score REAL, reason TEXT, pick INTEGER DEFAULT 0, manual INTEGER DEFAULT 0, attached INTEGER DEFAULT 0);
"""
TARGET_KEY = "cull_target"


def _ensure(conn) -> None:
    conn.executescript(SCHEMA)


# ===== measuring one frame =====
def _lab(rgb_u8: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(rgb_u8.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)


def _plane(lab: np.ndarray, ring: int) -> np.ndarray:
    """A linear backdrop (each Lab channel a + b x + c y) fitted to the frame's border, refitted on the border pixels
    that agree with the first fit, so a product touching the edge does not drag the backdrop towards itself."""
    h, w, _ = lab.shape
    yy, xx = np.mgrid[0:h, 0:w]
    edge = np.zeros((h, w), bool)
    edge[:ring] = edge[-ring:] = True; edge[:, :ring] = edge[:, -ring:] = True
    A = np.stack([np.ones(edge.sum()), xx[edge] / w, yy[edge] / h], 1)
    Y = lab[edge]
    coef = np.linalg.lstsq(A, Y, rcond=None)[0]
    for _ in range(2):
        res = np.sqrt(((A @ coef - Y) ** 2).sum(1))
        keep = res <= max(np.percentile(res, 60), 4.0)
        if keep.sum() < 8:
            break
        coef = np.linalg.lstsq(A[keep], Y[keep], rcond=None)[0]
    return coef


def measure_array(rgb: np.ndarray) -> dict:
    """The numbers a frame is judged on, from an RGB uint8 array (the 1024 px thumb):
    sharp: mean of the strongest TOP_SHARE colour gradients inside the product;
    dirs: that sharpness along eight directions, 0 to 157.5 degrees;
    sides: [top, right, bottom, left] share of each frame edge the product covers;
    blown, crushed: share of the product clipped white or black; area: share of the frame that is product."""
    import cv2
    h, w, _ = rgb.shape
    s = MASK_EDGE / max(h, w)
    sw, sh = max(int(round(w * s)), 8), max(int(round(h * s)), 8)
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    lab_s = _lab(small)
    coef = _plane(lab_s, RING)
    yy, xx = np.mgrid[0:sh, 0:sw]
    bg_s = coef[0] + np.multiply.outer(xx / sw, coef[1]) + np.multiply.outer(yy / sh, coef[2])
    D = np.sqrt(((lab_s - bg_s) ** 2).sum(-1))
    mask = (D > FG_DE).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, lab_cc, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= MIN_BLOB * sw * sh
    mask = keep[lab_cc]
    area = float(mask.mean())
    out = {"area": area, "sides": [0.0, 0.0, 0.0, 0.0], "sharp": 0.0, "dirs": [0.0] * 8, "blown": 0.0, "crushed": 0.0}
    if area < MIN_PRODUCT:
        # nothing stands off the backdrop: a black product crushed into a dark frame, a white one blown into a white
        # sweep. The middle of the frame is where the product is, so clipping is read there.
        mid = rgb[h // 4:h - h // 4, w // 4:w - w // 4].reshape(-1, 3)
        out["blown"] = float((mid.max(1) >= 250).mean()); out["crushed"] = float((mid.max(1) <= 8).mean())
        return out
    out["sides"] = [float(mask[:2].any(0).mean()), float(mask[:, -2:].any(1).mean()),
                    float(mask[-2:].any(0).mean()), float(mask[:, :2].any(1).mean())]
    # full resolution: the product is the upsampled mask AND the pixel itself off the backdrop, so the blocky edge of
    # the small mask never brings a strip of sharp linen grain in with it
    # only the product's box (and a few pixels round it) is worked at full size: a product is a tenth of most frames
    ys, xs = np.nonzero(mask)
    y0 = max(int(ys.min() / s) - 4, 0); y1 = min(int((ys.max() + 1) / s) + 4, h)
    x0 = max(int(xs.min() / s) - 4, 0); x1 = min(int((xs.max() + 1) / s) + 4, w)
    big = cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)[y0:y1, x0:x1]
    sub = np.ascontiguousarray(rgb[y0:y1, x0:x1])
    lab = _lab(sub)
    Y, X = np.mgrid[y0:y1, x0:x1]
    bg = coef[0] + np.multiply.outer(X / w, coef[1]) + np.multiply.outer(Y / h, coef[2])
    region = big & (np.sqrt(((lab - bg) ** 2).sum(-1)) > FG_DE)
    npx = int(region.sum())
    if npx < 50:
        return out
    gx = cv2.Sobel(lab, cv2.CV_32F, 1, 0, ksize=3) / 8.0
    gy = cv2.Sobel(lab, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    mag = np.sqrt((gx ** 2 + gy ** 2).sum(-1))[region]
    k = max(int(TOP_SHARE * mag.size), 10)
    top = np.partition(mag, mag.size - k)[mag.size - k:]
    out["sharp"] = float(top.mean())
    # the same sharpness by edge direction: every product pixel's gradient points somewhere, and the edges are binned
    # by that direction (eight bins over half a turn). Motion blur smears the edges that lie across the way the camera
    # moved and leaves the ones along it; defocus softens every bin alike.
    gxr, gyr = gx[region], gy[region]
    ang = (np.degrees(np.arctan2(gyr[:, 0], gxr[:, 0])) % 180.0) // 22.5
    dirs, counts = [], []
    for k8 in range(8):
        d = mag[ang == k8]
        kk = min(DIR_TOP, d.size)
        dirs.append(float(np.partition(d, d.size - kk)[d.size - kk:].mean()) if kk else 0.0)
        counts.append(int(d.size))
    out["dirs"] = dirs
    px = sub[region]
    out["blown"] = float((px.max(1) >= 250).mean())
    out["crushed"] = float((px.max(1) <= 8).mean())
    return out


def measure(path: Path) -> dict | None:
    from PIL import Image
    try:
        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"))
    except Exception:
        return None
    return measure_array(rgb)


def anisotropy(dirs: list[float], ref: list[float]) -> float:
    """How evenly a frame kept its edges in every direction against a sharp frame of the same angle: over the edge
    directions the sharp frame has real edges in (at least a quarter of its strongest), the weakest share kept
    (frame / sharp frame) divided by the strongest. 1 is even, focus or nothing lost; motion blur drags it down."""
    d, r = np.asarray(dirs, float), np.asarray(ref, float)
    use = r >= 0.25 * r.max() if r.size and r.max() > 0 else np.zeros(len(r), bool)
    if use.sum() < 2:
        return 1.0
    k = d[use] / r[use]
    return float(k.min() / max(k.max(), 1e-9))


def cut_off(sides: list[float]) -> bool:
    """The product runs off one edge, or off two edges that meet at a corner. Off two opposite edges, or three or
    four, is a framing choice (a strap shot tight, a detail that fills the frame), not a mistake."""
    on = [x >= CUT_SIDE for x in sides]
    k = sum(on)
    if k == 1:
        return True
    if k == 2:
        return not (on[0] and on[2]) and not (on[1] and on[3])
    return False
# ===== end measuring =====


# ===== grouping inside a product =====
def _avg_link(S: np.ndarray, cut: float) -> np.ndarray:
    """Average-linkage labels on a similarity matrix, joined while similarity >= cut."""
    n = len(S)
    if n == 1:
        return np.zeros(1, np.int64)
    from sklearn.cluster import AgglomerativeClustering
    D = np.clip(1.0 - S, 0, None); np.fill_diagonal(D, 0)
    return AgglomerativeClustering(n_clusters=None, distance_threshold=1.0 - cut, metric="precomputed",
                                   linkage="average").fit_predict(D)


def framing_matrix(fields: list) -> np.ndarray:
    """Pairwise search.framing_similarity, vectorised over pairs per shift (the same numbers, a hundred times faster)."""
    from .search import _FIELD_SHIFT
    n = len(fields)
    S = np.eye(n, dtype=np.float32)
    if n < 2:
        return S
    ax = np.stack([f[0] for f in fields]); ay = np.stack([f[1] for f in fields])
    h, w = ax.shape[1:]
    best = np.full((n, n), -1.0, np.float32)
    def cut(m, oy, ox):
        return m[:, max(oy, 0):h + min(oy, 0), max(ox, 0):w + min(ox, 0)]
    for dy in range(-_FIELD_SHIFT, _FIELD_SHIFT + 1):
        for dx in range(-_FIELD_SHIFT, _FIELD_SHIFT + 1):
            A = np.concatenate([cut(ax, dy, dx).reshape(n, -1), cut(ay, dy, dx).reshape(n, -1)], 1)
            B = np.concatenate([cut(ax, -dy, -dx).reshape(n, -1), cut(ay, -dy, -dx).reshape(n, -1)], 1)
            A = A - A.mean(1, keepdims=True); B = B - B.mean(1, keepdims=True)
            A /= np.linalg.norm(A, axis=1, keepdims=True).clip(1e-9); B /= np.linalg.norm(B, axis=1, keepdims=True).clip(1e-9)
            best = np.maximum(best, A @ B.T)
    S = np.maximum(best, best.T)
    np.fill_diagonal(S, 1.0)
    return S


def _baseline(xs: list[float]) -> float:
    """How clipped a frame's angle is anyway: the least clipped of its neighbours, unless most of them are clipped (a
    run of blown frames is a run of mistakes, not a glossy product), when it is 0 and the frame is judged as it is."""
    return min(xs) if xs and float(np.median(xs)) < 0.5 else 0.0


def judge(frames: list[dict]) -> list[dict]:
    """Reasons, retake groups, angles and scores for the frames of one product. frames: [{id, variant, m (measure),
    field (framing field or None), attached}]. Returns the same dicts with retake, angle, score and reason set
    (reason None for a frame the client could get). Pure: no database, no files."""
    byvar: dict[int, list[dict]] = defaultdict(list)
    for f in frames:
        byvar[f["variant"]].append(f)
    gid = 0
    for v, fs in byvar.items():
        ok = [f for f in fs if f["field"] is not None]
        S = framing_matrix([f["field"] for f in ok]) if ok else np.zeros((0, 0))
        lab = _avg_link(S, RETAKE_FRAMING) if ok else []
        for f, l in zip(ok, lab):
            f["retake"] = gid + int(l)
        gid += (max(lab) + 1) if len(ok) else 0
        for f in fs:
            if f["field"] is None:
                f["retake"] = gid; gid += 1
        # Clipping and framing are judged against the frame's own angle too. A glossy can has a clipped highlight in
        # every frame and a detail shot runs off the frame in every frame: that is the product and the framing, not a
        # mistake. A frame is blown (or crushed) by how much more of it clipped than the least clipped frame framed
        # like it, and off the edge only when fewer than CUT_INTENT frames of its retake group run off the same way.
        idx = {id(f): k for k, f in enumerate(ok)}
        members: dict[int, list[dict]] = defaultdict(list)
        for f in fs:
            members[f["retake"]].append(f)
        def neighbours(f):
            same = [g for g in members[f["retake"]] if g is not f and g["m"]]
            if same or id(f) not in idx:
                return same
            row = S[idx[id(f)]]
            return [g for g in ok if g is not f and g["m"] and row[idx[id(g)]] >= REF_FRAMING]
        for f in fs:
            m = f["m"]
            if not m:
                continue
            nb = neighbours(f)
            f["blown_x"] = m["blown"] - _baseline([g["m"]["blown"] for g in nb])
            f["crushed_x"] = m["crushed"] - _baseline([g["m"]["crushed"] for g in nb])
            sides = tuple(x >= CUT_SIDE for x in m["sides"])
            alike = sum(1 for g in members[f["retake"]] if g["m"] and tuple(x >= CUT_SIDE for x in g["m"]["sides"]) == sides)
            f["cut"] = cut_off(m["sides"]) and alike < CUT_INTENT
        def clipped(f):
            return f["blown_x"] >= BLOWN_SHARE or f["crushed_x"] >= CRUSHED_SHARE or f["cut"]
        sharp_v = np.array([f["m"]["sharp"] for f in fs if f["m"] and not clipped(f)], float)
        var_ref = float(np.percentile(sharp_v, 75)) if sharp_v.size else 0.0
        # every frame is held against the sharpest unclipped frame framed like it (its retake group and any frame a
        # shake or a defocus moved out of it): the same angle, so the same detail to lose
        for f in fs:
            m = f["m"]
            if not m:
                f["reason"], f["score"] = "soft", 0.0
                continue
            best = None
            if id(f) in idx:
                row = S[idx[id(f)]]
                near = [g for g in ok if g["m"] and not clipped(g) and g["retake"] == f["retake"]]
                if len(near) < 2:                       # alone in its retake group: the frames framed nearly like it
                    near = [g for g in ok if g["m"] and not clipped(g) and row[idx[id(g)]] >= REF_FRAMING]
                best = max(near, key=lambda g: g["m"]["sharp"]) if near else None
            if best is None or best is f and len(near) < 2:
                ref, refdirs = max(var_ref, m["sharp"]), None
            else:
                ref, refdirs = best["m"]["sharp"], best["m"]["dirs"]
            rel = m["sharp"] / ref if ref > 0 else 1.0
            f["score"] = float(min(rel, 1.0) - 0.5 * m["blown"] - 0.5 * m["crushed"])
            aniso = anisotropy(m["dirs"], refdirs) if refdirs is not None and best is not f else 1.0
            f["aniso"], f["rel"] = aniso, rel
            if f["blown_x"] >= BLOWN_SHARE:
                f["reason"] = "blown"
            elif f["crushed_x"] >= CRUSHED_SHARE:
                f["reason"] = "crushed"
            elif f["cut"]:
                f["reason"] = "cut"
            elif aniso < MOTION_ANISO and (rel < BLUR_RATIO or aniso < MOTION_STRONG):
                f["reason"] = "motion"
            elif rel < SOFT_RATIO:
                f["reason"] = "soft"
            elif f.get("attached") and not ATTACHED_CAN_PICK:
                f["reason"] = "stray"
            else:
                f["reason"] = None
        groups: dict[int, list[dict]] = defaultdict(list)
        for f in fs:
            groups[f["retake"]].append(f)
        for g in groups.values():
            keep = [f for f in g if f["reason"] is None]
            if keep:
                top = max(keep, key=lambda f: (f["score"], -f["id"]))
                for f in keep:
                    if f is not top:
                        f["reason"] = "retake"
    # angles across colours: the kept frame of every retake group, matched by framing
    reps = [f for f in frames if f["reason"] is None]
    lab = _avg_link(framing_matrix([f["field"] for f in reps]), ANGLE_FRAMING) if reps and all(f["field"] is not None for f in reps) else list(range(len(reps)))
    # angles numbered by how many colours were shot at them, most first
    sizes = defaultdict(int)
    for l in lab:
        sizes[int(l)] += 1
    order = {l: k + 1 for k, l in enumerate(sorted(sizes, key=lambda l: (-sizes[l], l)))}
    by_retake = {}
    for f, l in zip(reps, lab):
        f["angle"] = order[int(l)]
        by_retake[f["retake"]] = f["angle"]
    for f in frames:
        f.setdefault("angle", by_retake.get(f["retake"]))
        f.setdefault("score", 0.0)
    return frames
# ===== end grouping =====


# ===== the pick rule =====
def pick(cands: list[dict], n: int) -> list[int]:
    """Up to n ids from one product's keepable frames, [{id, variant, angle, score}] (one per retake group already).
    First every colour (biggest first), each at the angle the picks so far have least of; then every angle not yet
    picked; then round the colours again, fewest picks first. Inside a step the best score wins, lowest id on a tie."""
    if n <= 0 or not cands:
        return []
    left = sorted(cands, key=lambda c: (-c["score"], c["id"]))
    vsize = defaultdict(int)
    for c in cands:
        vsize[c["variant"]] += 1
    variants = sorted(vsize, key=lambda v: (-vsize[v], v))
    picked: list[dict] = []
    per_angle: dict = defaultdict(int); per_var: dict = defaultdict(int)
    def take(c):
        picked.append(c); left.remove(c); per_angle[c["angle"]] += 1; per_var[c["variant"]] += 1
    def best_in(pool):
        return min(pool, key=lambda c: (per_angle[c["angle"]], -c["score"], c["id"])) if pool else None
    for v in variants:                                              # every colour
        if len(picked) >= n:
            break
        c = best_in([c for c in left if c["variant"] == v])
        if c:
            take(c)
    for a in sorted({c["angle"] for c in cands} - set(per_angle), key=lambda a: (a is None, a)):   # every angle
        if len(picked) >= n:
            break
        pool = [c for c in left if c["angle"] == a]
        if pool:
            take(min(pool, key=lambda c: (per_var[c["variant"]], -c["score"], c["id"])))
    while len(picked) < n and left:                                 # then round the colours again
        v = min({c["variant"] for c in left}, key=lambda v: (per_var[v], -vsize[v], v))
        take(best_in([c for c in left if c["variant"] == v]))
    return [c["id"] for c in picked]


def split_total(total: int, weights: dict[int, int], caps: dict[int, int]) -> dict[int, int]:
    """total picks across products in proportion to weights (colours a product has), largest remainder, never more
    than a product's cap (how many it can give); what a capped product cannot take goes to the others."""
    out = {p: 0 for p in weights}
    open_ = {p for p in weights if caps.get(p, 0) > 0 and weights[p] > 0}
    left = max(int(total), 0)
    while left > 0 and open_:
        W = sum(weights[p] for p in open_)
        share = {p: left * weights[p] / W for p in open_}
        give = {p: min(int(share[p]), caps[p] - out[p]) for p in open_}
        rem = left - sum(give.values())
        for p in sorted(open_, key=lambda p: (-(share[p] - int(share[p])), p)):
            if rem <= 0:
                break
            if out[p] + give[p] < caps[p]:
                give[p] += 1; rem -= 1
        if sum(give.values()) == 0:
            break
        for p, g in give.items():
            out[p] += g
        left = total - sum(out.values())
        open_ = {p for p in open_ if out[p] < caps[p]}
    return out
# ===== end the pick rule =====


# ===== the run over a shoot =====
def get_target(conn) -> dict:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (TARGET_KEY,)).fetchone()
    try:
        t = json.loads(r[0]) if r else {}
    except ValueError:
        t = {}
    if t.get("total"):
        return {"total": int(t["total"])}
    return {"per_product": int(t.get("per_product") or DEFAULT_PER_PRODUCT)}


def set_target(conn, per_product: int | None = None, total: int | None = None) -> dict:
    t = {"total": int(total)} if total else {"per_product": int(per_product or DEFAULT_PER_PRODUCT)}
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (TARGET_KEY, json.dumps(t)))
    conn.commit()
    return t


def _measure_all(root: Path, conn, rows: list[tuple[int, str]], progress) -> dict[int, dict | None]:
    """Measurements for (id, qhash) rows, from cull_measure when the thumb is the same one, else from the thumb."""
    have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT photo_id, qhash, m FROM cull_measure")}
    out: dict[int, dict | None] = {}
    todo = []
    for pid, qh in rows:
        h = have.get(pid)
        if h and h[0] == qh:
            out[pid] = json.loads(h[1]) if h[1] else None
        else:
            todo.append((pid, qh))
    thumbs = db.index_dir(root) / "thumbs"
    done = [len(rows) - len(todo)]
    if progress:
        progress(done[0], len(rows))
    def one(r):
        return r, measure(thumbs / f"{r[1]}.jpg")
    with ThreadPoolExecutor(6) as ex:
        for k, ((pid, qh), m) in enumerate(ex.map(one, todo), 1):
            out[pid] = m
            conn.execute("INSERT OR REPLACE INTO cull_measure(photo_id, qhash, m) VALUES(?,?,?)", (pid, qh, json.dumps(m) if m else None))
            done[0] += 1
            if progress and (k % 25 == 0 or k == len(todo)):
                progress(done[0], len(rows))
    conn.commit()
    return out


def run(root: Path, progress: Callable[[int, int], None] | None = None) -> dict:
    """Measure (what is not measured yet), judge and pick the shoot's products from the last Find products. Rewrites
    table cull; keeps nothing a person chose by hand (a new run is a fresh pick). Returns summary(conn)."""
    from .products import load_grey
    from .search import framing_field
    root = Path(root); conn = db.connect(root); _ensure(conn)
    prod = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT photo_id, product, variant FROM products")}
    if not prod:
        conn.execute("DELETE FROM cull"); conn.commit()
        return summary(conn)
    grey = load_grey(root)
    scope = [int(i) for i in grey[0]] if grey is not None else list(prod)
    qh = {r[0]: r[1] for r in conn.execute("SELECT id, qhash FROM photos WHERE status='ok' AND qhash IS NOT NULL")}
    scope = [p for p in scope if p in qh]
    meas = _measure_all(root, conn, [(p, qh[p]) for p in scope], progress)
    t_judge = time.time()
    # Frames to put back next to a product. Find products leaves some frames out (a frame alike nothing else) and makes
    # small "products" of others: a blown or cut frame looks unlike its product and finds its own kind. Those small
    # clusters (fewer than FRAGMENT_SHARE of the biggest product's frames) and clusters that are mostly clipped or cut
    # are taken apart, and every frame joins the product its nearest frame belongs to (grey cosine ATTACH_SIM or
    # more), in that product's nearest colour. A frame near nothing goes to product 0, "not matched to a product".
    from .products import _lab as lab_of_rgb, subject_colour
    members: dict[int, list[int]] = defaultdict(list)
    for p, (k, v) in prod.items():
        members[k].append(p)
    def bad(p):
        m = meas.get(p)
        return m is None or m["blown"] >= BLOWN_SHARE or m["crushed"] >= CRUSHED_SHARE or cut_off(m["sides"])
    biggest = max(len(ps) for ps in members.values())
    broken = {k for k, ps in members.items()
              if len(ps) < max(FRAGMENT_MIN, FRAGMENT_SHARE * biggest) or sum(bad(p) for p in ps) >= DEFECT_CLUSTER * len(ps)}
    home = {p: kv for p, kv in prod.items() if kv[0] not in broken}
    strays = [p for p in scope if p not in home]
    attached: set[int] = set()
    unmatched: list[int] = []
    swatch = {}
    for r in conn.execute("SELECT product, variant, hex FROM product_sets"):
        if r[2]:
            swatch[(r[0], r[1])] = lab_of_rgb(np.array([int(r[2][i:i + 2], 16) for i in (1, 3, 5)], np.float64))
    grid = db.index_dir(root) / "grid"
    if strays and grey is not None and home:
        pos = {int(i): k for k, i in enumerate(grey[0])}
        X = grey[1]
        hp = sorted((p for p in home if p in pos), key=lambda p: home[p][0])
        keys = sorted({home[p][0] for p in hp})
        starts = np.searchsorted([home[p][0] for p in hp], keys)
        HX = X[[pos[p] for p in hp]]
        # a cluster of strays moves as one (the detail shots of a product stay together), to the product its frames
        # are nearest on average; a frame no product took moves on its own
        bunches: dict = defaultdict(list)
        for p in strays:
            bunches[prod[p][0] if p in prod else ("alone", p)].append(p)
        for bunch in bunches.values():
            inb = [p for p in bunch if p in pos]
            unmatched.extend(p for p in bunch if p not in pos)
            if not inb:
                continue
            best_by_product = np.maximum.reduceat(X[[pos[p] for p in inb]] @ HX.T, starts, axis=1)   # frames x products
            score = best_by_product.mean(0)
            j = int(np.argmax(score))
            if float(score[j]) < ATTACH_SIM:
                unmatched.extend(inb); continue
            k = keys[j]
            vs = sorted({vv for (kk, vv) in swatch if kk == k})
            for p in inb:
                v = vs[0] if vs else 1
                if len(vs) > 1:
                    c = subject_colour(grid / f"{qh[p]}.jpg")
                    if c is not None:
                        v = min(vs, key=lambda vv: float(np.linalg.norm(swatch[(k, vv)] - c)))
                home[p] = (k, v); attached.add(p)
    else:
        unmatched = list(strays)
    frames_by: dict[int, list[dict]] = defaultdict(list)
    for p, (k, v) in home.items():
        frames_by[k].append({"id": p, "variant": v, "m": meas.get(p), "field": framing_field(grid / f"{qh[p]}.jpg") if p in qh else None,
                             "attached": p in attached})
    conn.execute("DELETE FROM cull")
    target = get_target(conn)
    judged = {k: judge(fs) for k, fs in frames_by.items()}
    if unmatched:
        # nothing to hold these against: only what a frame shows on its own (clipped, cut off) is said, else stray
        # clipping against the least clipped frame of the same cluster, as judge does against an angle
        bunch_of = {p: (prod[p][0] if p in prod else ("alone", p)) for p in unmatched}
        # (a cluster that is mostly clipped is a cluster of clipped frames, and is judged on its own numbers)
        vals: dict = defaultdict(list)
        for p in unmatched:
            if meas.get(p):
                vals[bunch_of[p]].append((meas[p]["blown"], meas[p]["crushed"]))
        def base(b, i):
            xs = [x[i] for x in vals.get(b, [])]
            return min(xs) if len(xs) > 1 and float(np.median(xs)) < 0.5 else 0.0
        rest = []
        for p in unmatched:
            m = meas.get(p)
            base_b, base_c = base(bunch_of[p], 0), base(bunch_of[p], 1)
            reason = ("blown" if m and m["blown"] - base_b >= BLOWN_SHARE else "crushed" if m and m["crushed"] - base_c >= CRUSHED_SHARE
                      else "cut" if m and cut_off(m["sides"]) else "stray")
            rest.append({"id": p, "variant": 0, "angle": None, "retake": None, "score": 0.0, "reason": reason})
        judged[0] = rest
    _store_and_pick(conn, judged, target, attached | set(unmatched))
    s = summary(conn)
    s["judge_s"] = round(time.time() - t_judge, 2)
    return s


def _cands(fs: list[dict]) -> list[dict]:
    return [{"id": f["id"], "variant": f["variant"], "angle": f["angle"], "score": f["score"]} for f in fs if f["reason"] is None]


def _store_and_pick(conn, judged: dict[int, list[dict]], target: dict, attached: set[int]) -> None:
    caps = {k: len(_cands(fs)) for k, fs in judged.items()}
    if "total" in target:
        weights = {k: len({f["variant"] for f in fs}) for k, fs in judged.items()}
        n_for = split_total(target["total"], weights, caps)
    else:
        n_for = {k: target["per_product"] for k in judged}
    rows = []
    for k, fs in judged.items():
        chosen = set(pick(_cands(fs), n_for.get(k, 0)))
        for f in fs:
            num = lambda x: None if x is None else int(x)
            rows.append((int(f["id"]), int(k), int(f["variant"]), num(f.get("angle")), num(f.get("retake")), float(f.get("score", 0.0)), f["reason"],
                         int(f["id"] in chosen), int(f["id"] in attached)))
    conn.executemany("INSERT OR REPLACE INTO cull(photo_id, product, variant, angle, retake, score, reason, pick, attached) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def repick(conn, per_product: int | None = None, total: int | None = None) -> dict:
    """A new target, the same reasons: picks again from the stored judgement (no measuring), hand choices dropped."""
    _ensure(conn)
    target = set_target(conn, per_product, total)
    judged: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT photo_id, product, variant, angle, retake, score, reason, attached FROM cull"):
        judged[r[1]].append({"id": r[0], "variant": r[2], "angle": r[3], "retake": r[4], "score": r[5], "reason": r[6]})
    attached = {r[0] for r in conn.execute("SELECT photo_id FROM cull WHERE attached=1")}
    if judged:
        conn.execute("DELETE FROM cull")
        _store_and_pick(conn, judged, target, attached)
    return summary(conn)


def swap(conn, photo_id: int) -> dict:
    """Swap a pick for the next frame of the same colour and angle: the next best keepable one, then the retakes and
    the rejects of that angle, round and round. Returns {out, in}; in is None when the angle has nothing else."""
    _ensure(conn)
    r = conn.execute("SELECT product, variant, angle, retake, pick FROM cull WHERE photo_id=?", (photo_id,)).fetchone()
    if r is None or not r[4]:
        raise KeyError(photo_id)
    product, variant, angle, retake = r[0], r[1], r[2], r[3]
    if angle is not None:
        same = conn.execute("SELECT photo_id, reason, score, pick FROM cull WHERE product=? AND variant=? AND "
                            "(angle=? OR retake IN (SELECT retake FROM cull WHERE product=? AND variant=? AND angle=?))",
                            (product, variant, angle, product, variant, angle)).fetchall()
    else:
        same = conn.execute("SELECT photo_id, reason, score, pick FROM cull WHERE product=? AND variant=? AND retake=?",
                            (product, variant, retake)).fetchall()
    rank = {None: 0, "retake": 1}
    ring = sorted(same, key=lambda x: (rank.get(x[1], 2), -(x[2] or 0), x[0]))
    ids = [x[0] for x in ring]
    k = ids.index(photo_id)
    nxt = next((ids[(k + j) % len(ids)] for j in range(1, len(ids)) if not dict((x[0], x[3]) for x in ring)[ids[(k + j) % len(ids)]]), None)
    if nxt is None:
        return {"out": photo_id, "in": None}
    conn.execute("UPDATE cull SET pick=0, manual=1 WHERE photo_id=?", (photo_id,))
    conn.execute("UPDATE cull SET pick=1, manual=1 WHERE photo_id=?", (nxt,))
    conn.commit()
    return {"out": photo_id, "in": nxt}


def set_pick(conn, photo_id: int, on: bool) -> bool:
    _ensure(conn)
    n = conn.execute("UPDATE cull SET pick=?, manual=1 WHERE photo_id=?", (int(bool(on)), photo_id)).rowcount
    conn.commit()
    return bool(n)


def merge_products(conn, product: int, into: int) -> None:
    """Fold product into into (Find products split detail shots of one product off as a product of their own): every
    colour of product joins into's nearest colour within products.VARIANT_DE, or comes over as a new colour; its
    angles stay apart from into's. Rewrites tables products and product_sets."""
    from .products import _lab as lab_of_rgb, VARIANT_DE
    if product == into:
        raise ValueError("pick another product to merge into")
    sets = {(r[0], r[1]): r for r in conn.execute("SELECT product, variant, name, named, colour, hex, n, views FROM product_sets "
                                                   "WHERE product IN (?, ?)", (product, into))}
    if not any(k[0] == product for k in sets):
        raise KeyError("no such product")
    if not any(k[0] == into for k in sets):
        raise KeyError("no product to merge into")
    def lab(h):
        return lab_of_rgb(np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], np.float64)) if h else None
    theirs = {k[1]: lab(r[5]) for k, r in sets.items() if k[0] == into}
    name_row = next(r for k, r in sets.items() if k[0] == into)
    next_v = max(theirs) + 1
    vmax = conn.execute("SELECT COALESCE(MAX(view), 0) FROM products WHERE product=?", (into,)).fetchone()[0] + 1
    for (p, v), r in sorted(sets.items()):
        if p != product:
            continue
        mine = lab(r[5])
        cands = [(float(np.linalg.norm(mine - L)), tv) for tv, L in theirs.items() if mine is not None and L is not None]
        best = min(cands) if cands else None
        if best and best[0] <= VARIANT_DE:
            tv = best[1]
            conn.execute("UPDATE product_sets SET n = n + ?, views = views + ? WHERE product=? AND variant=?", (r[6], r[7], into, tv))
        else:
            tv = next_v; next_v += 1
            conn.execute("INSERT INTO product_sets(product, variant, name, named, colour, hex, n, views) VALUES(?,?,?,?,?,?,?,?)",
                         (into, tv, name_row[2], name_row[3], r[4], r[5], r[6], r[7]))
            theirs[tv] = mine
        conn.execute("UPDATE products SET product=?, variant=?, view=view+? WHERE product=? AND variant=?", (into, tv, vmax, product, v))
    conn.execute("DELETE FROM product_sets WHERE product=?", (product,))
    conn.commit()


def summary(conn) -> dict:
    """{photos, picked, products, rejected: {reason: n}, target}: the running count."""
    _ensure(conn)
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(pick), 0), COUNT(DISTINCT product) FROM cull").fetchone()
    rej = {k: n for k, n in conn.execute("SELECT reason, COUNT(*) FROM cull WHERE reason IS NOT NULL AND pick=0 GROUP BY reason")}
    return {"photos": int(r[0]), "picked": int(r[1]), "products": int(r[2]), "rejected": rej, "target": get_target(conn)}


def listing(conn) -> dict:
    """Everything the Client pick view draws: summary plus [{product, name, n, picked, target, variants: [{variant,
    colour, hex}], picks: [...], rejects: [...]}], each frame {id, qhash, rel, variant, angle, reason, words, score}.
    Picks in colour then angle order; rejects best first inside each reason."""
    from .products import list_products
    _ensure(conn)
    prods = {p["product"]: p for p in list_products(conn)}
    frames: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT c.photo_id, c.product, c.variant, c.angle, c.reason, c.score, c.pick, c.manual, p.qhash, p.rel "
                          "FROM cull c JOIN photos p ON p.id=c.photo_id ORDER BY c.product, c.variant, c.angle, c.photo_id"):
        frames[r[1]].append({"id": r[0], "variant": r[2], "angle": r[3], "reason": r[4], "words": REASONS.get(r[4]) if r[4] else None, "short": SHORT.get(r[4]) if r[4] else None,
                             "score": round(r[5] or 0.0, 3), "pick": bool(r[6]), "manual": bool(r[7]), "qhash": r[8], "rel": r[9]})
    target = get_target(conn)
    out = []
    for k in sorted(frames, key=lambda k: (k == 0, -len(frames[k]), k)):          # "not matched" last
        p = prods.get(k, {"name": "not matched to a product" if k == 0 else f"product {k}", "variants": []})
        fs = frames[k]
        picks = [f for f in fs if f["pick"]]
        rejects = sorted([f for f in fs if not f["pick"] and f["reason"]],
                         key=lambda f: (REASON_ORDER.index(f["reason"]) if f["reason"] in REASON_ORDER else 99, f["variant"], f["angle"] or 0, -f["score"], f["id"]))
        spare = [f for f in fs if not f["pick"] and not f["reason"]]
        out.append({"product": k, "name": p["name"], "n": len(fs), "picked": len(picks),
                    "variants": [{"variant": v["variant"], "colour": v["colour"], "hex": v["hex"]} for v in p["variants"]],
                    "picks": picks, "spare": spare, "rejects": rejects})
    return dict(summary(conn), products=out)


def export_plan(conn) -> tuple[dict[str, list[int]], list[list]]:
    """({"<product>/<colour>": [picked ids]}, csv rows [product, colour, angle, file, status, reason]) for the export.
    Every frame of every product is a csv row, so the client can see what was left out and why."""
    from .export import safe_segment
    from .products import list_products
    _ensure(conn)
    names = {p["product"]: p for p in list_products(conn)}
    def colour(k, v):
        for x in names.get(k, {}).get("variants", []):
            if x["variant"] == v:
                return x["colour"]
        return f"colour {v}"
    folders: dict[str, list[int]] = {}
    rows = []
    for r in conn.execute("SELECT c.photo_id, c.product, c.variant, c.angle, c.reason, c.pick, p.rel FROM cull c "
                          "JOIN photos p ON p.id=c.photo_id ORDER BY c.product, c.variant, c.angle, p.rel"):
        name = names.get(r[1], {}).get("name", "not matched to a product" if r[1] == 0 else f"product {r[1]}")
        col = colour(r[1], r[2]) if r[1] else ""
        if r[5]:
            folders.setdefault(f"{safe_segment(name)}/{safe_segment(col)}", []).append(r[0])
        rows.append([name, col, f"angle {r[3]}" if r[3] else "", Path(r[6]).name, "picked" if r[5] else "left out",
                     "" if r[5] else (REASONS.get(r[4]) or "not needed for the target")])
    return folders, rows
# ===== end the run =====
