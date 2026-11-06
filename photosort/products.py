"""Product shoots: the same product shot in several colours and from several angles, and several products in one shoot.

find_products groups the shoot's stills into products with the embeddings every scan already stores, then splits each
product into colour variants by the dominant colour of the subject, so the angle a product was shot from never makes a
new variant (same colour, another view is an angle). No segmentation and no model beyond the stored embeddings: the
subject is the centre-weighted part of the frame that differs from the backdrop.

Calibrated on scripts/make_product_set.py (seven products, four colours, four views, 108 files); the numbers and the
reasoning behind every threshold are in docs/shoot-modes.md.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
from . import db

# Two photos are the same product from PRODUCT_SIM cosine (average linkage) between embeddings of their GREY grid
# thumbs. Colour is taken out on purpose: the stored (colour) embedding puts a red bottle nearer a red flask (0.94, two
# SKUs) than the same bottle seen from another side (0.83), so on the calibration set (180 files, 8 products, one of
# them a near-twin SKU) it merged bottle and flask at every cut that kept the other products whole (ARI 0.85 at 0.80;
# 0.99 at 0.86 with one product split). Grey embeddings: ARI 1.0, no product split, from 0.88 to 0.90. The cost is one
# extra encode per still, 47 ms each on the M1 (about 47 s per 1,000 photos), from the grid thumbs on this Mac.
PRODUCT_SIM = 0.89
PRODUCT_MIN_PHOTOS = 2        # a one-off frame is not a product shoot
# Two photos of one product are the same variant within VARIANT_DE (CIE76 delta E between their dominant subject
# colours, average linkage). Measured on the calibration set: same colour, another view 0.7 median, 2.7 at the 95th
# percentile, 16.8 worst (a mirrored crop of the real photo); another colour 31.7 at the 5th, 24.8 worst (red against
# orange). The cut sits between the worst of each.
VARIANT_DE = 21.0
CHROMA_SHARE = 0.25           # see subject_colour: a watch is its strap's colour, not its black face
# Views: inside one variant, photos closer than VIEW_SIM are the same view (a re-take), farther are another angle.
VIEW_SIM = 0.97

# A small fixed table for naming a swatch. Purple is a product colour here, not interface chrome.
COLOUR_NAMES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("black", (20, 20, 22)), ("charcoal", (60, 62, 66)), ("grey", (128, 128, 128)), ("silver", (192, 192, 196)),
    ("white", (245, 245, 242)), ("cream", (240, 228, 200)), ("beige", (210, 190, 160)), ("brown", (110, 70, 40)),
    ("tan", (180, 130, 80)), ("red", (200, 30, 35)), ("maroon", (110, 20, 30)), ("orange", (235, 120, 30)),
    ("yellow", (240, 200, 40)), ("gold", (200, 160, 60)), ("olive", (110, 115, 40)), ("green", (40, 160, 60)),
    ("mint", (160, 220, 190)), ("teal", (20, 130, 130)), ("sky blue", (110, 180, 230)), ("blue", (40, 90, 200)),
    ("navy", (25, 35, 80)), ("purple", (110, 50, 150)), ("pink", (235, 130, 170)), ("magenta", (200, 40, 140)),
)

# Words a product can be named from, scored like the discovered categories (a photo of {word}). The shoot vocabulary
# (vocab.VOCAB) is scenes and events; these are the things a product shoot is of.
PRODUCT_VOCAB: tuple[str, ...] = (
    "sneaker", "shoe", "sandal", "boot", "high heels", "handbag", "backpack", "wallet", "belt", "watch", "sunglasses",
    "headphones", "earphones", "speaker", "phone", "phone case", "laptop", "camera", "mug", "cup", "bottle",
    "water bottle", "perfume bottle", "lipstick", "cream jar", "cosmetic tube", "soap", "candle", "lamp", "chair",
    "sofa", "cushion", "vase", "plate", "bowl", "kettle", "jar", "can", "box", "toy", "t-shirt", "shirt", "dress",
    "saree", "jacket", "cap", "hat", "scarf", "ring", "necklace", "earrings", "bracelet", "jewellery", "food packet",
    "chocolate", "bicycle", "helmet", "tube",
)
PRODUCT_NAME_MIN_COS = 0.05   # SigLIP text-image cosines run low; the margin below does the real gating
PRODUCT_NAME_MARGIN = 0.01    # the best word must beat the runner-up by this much, else "product N"


# ===== colour =====
def _lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB 0..255 (..., 3) to CIE Lab (D65)."""
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)

def _rgb(lab: np.ndarray) -> tuple[int, int, int]:
    L, a, b = (float(x) for x in lab)
    fy = (L + 16) / 116; fx = fy + a / 500; fz = fy - b / 200
    xyz = np.array([fx, fy, fz])
    xyz = np.where(xyz ** 3 > 0.008856, xyz ** 3, (xyz - 16 / 116) / 7.787) * np.array([0.95047, 1.0, 1.08883])
    Mi = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]])
    c = Mi @ xyz
    c = np.where(c > 0.0031308, 1.055 * np.clip(c, 0, None) ** (1 / 2.4) - 0.055, 12.92 * c)
    return tuple(int(round(v)) for v in np.clip(c, 0, 1) * 255)

_NAMED_LAB = _lab(np.array([c for _, c in COLOUR_NAMES], np.float64))

def colour_name(lab) -> str:
    """The nearest name in COLOUR_NAMES, lightness weighted half (a dark red is red before it is maroon's neighbour)."""
    d = np.array(lab, np.float64) - _NAMED_LAB
    d[:, 0] *= 0.5
    return COLOUR_NAMES[int(np.argmin((d ** 2).sum(1)))][0]

def subject_colour(path: Path, edge: int = 96) -> np.ndarray | None:
    """The dominant colour of a photo's subject, as Lab. The backdrop is the median colour of the frame's border; every
    pixel is weighted by how far it is from that backdrop and by how central it is (a product shot keeps its subject in
    the middle); the weighted pixels are binned (twelve hues for coloured pixels, three lightness bands for neutral
    ones) and the colour is the weighted mean of the heaviest bin. None when the file will not open."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            im = im.convert("RGB"); im.thumbnail((edge, edge))
            lab = _lab(np.asarray(im))
    except Exception:
        return None
    h, w, _ = lab.shape
    ring = np.concatenate([lab[:3].reshape(-1, 3), lab[-3:].reshape(-1, 3), lab[:, :3].reshape(-1, 3), lab[:, -3:].reshape(-1, 3)])
    bg = np.median(ring, axis=0)
    yy, xx = np.mgrid[0:h, 0:w]
    centre = np.exp(-(((xx + .5) / w - .5) ** 2 + ((yy + .5) / h - .5) ** 2) / (2 * 0.3 ** 2))
    de = np.sqrt(((lab - bg) ** 2).sum(-1))
    fg = np.clip((de - 8.0) / 12.0, 0, 1)
    wgt = centre * fg
    if wgt.sum() < 0.02 * centre.sum():          # nothing stands off the backdrop: the frame is the subject
        wgt = centre
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    hue = (np.degrees(np.arctan2(b, a)) % 360) // 30
    bins = np.where(chroma >= 12, hue, 12 + np.digitize(L, [35, 70]))
    mass = np.bincount(bins.ravel().astype(int), weights=wgt.ravel(), minlength=15)
    top = int(np.argmax(mass))
    # A coloured product with black or white details (a watch face, a sneaker's sole, a bottle's label) is its colour,
    # not its detail: the heaviest coloured bin wins when it carries CHROMA_SHARE of the heaviest bin's weight.
    hue_top = int(np.argmax(mass[:12]))
    if top >= 12 and mass[hue_top] >= CHROMA_SHARE * mass[top]:
        top = hue_top
    sel = (bins == top)
    ws = (wgt * sel).ravel()
    return (lab.reshape(-1, 3) * ws[:, None]).sum(0) / (ws.sum() or 1.0)
# ===== end colour =====


def _clusters(X: np.ndarray, threshold: float, metric: str) -> np.ndarray:
    """Average-linkage agglomerative labels, cut at threshold (a distance). One row is its own cluster."""
    if len(X) == 1:
        return np.zeros(1, np.int64)
    from sklearn.cluster import AgglomerativeClustering
    return AgglomerativeClustering(n_clusters=None, distance_threshold=threshold, metric=metric,
                                   linkage="average").fit_predict(X)

def _name(cent: np.ndarray, T: np.ndarray) -> str | None:
    s = T @ cent
    o = np.argsort(-s)
    if float(s[o[0]]) >= PRODUCT_NAME_MIN_COS and float(s[o[0]] - s[o[1]]) >= PRODUCT_NAME_MARGIN:
        return PRODUCT_VOCAB[int(o[0])]
    return None

def group(M: np.ndarray, colours: list[np.ndarray | None]) -> list[dict]:
    """The pure part of find_products: rows of M (unit embeddings) and their subject colours to
    [{rows, variants: [{rows, lab, views: [view per row]}]}], products of PRODUCT_MIN_PHOTOS or more, biggest first."""
    labels = _clusters(M, 1.0 - PRODUCT_SIM, "cosine")
    out = []
    for c in np.unique(labels):
        idx = np.where(labels == c)[0]
        if len(idx) < PRODUCT_MIN_PHOTOS:
            continue
        have = [i for i in idx if colours[i] is not None]
        lab = np.array([colours[i] for i in have]) if have else np.zeros((0, 3))
        vl = _clusters(lab, VARIANT_DE, "euclidean") if len(have) else np.zeros(0, np.int64)
        variants = []
        for v in np.unique(vl):
            rows = [have[k] for k in np.where(vl == v)[0]]
            views = _clusters(M[rows], 1.0 - VIEW_SIM, "cosine")
            variants.append(dict(rows=rows, lab=lab[vl == v].mean(0), views=[int(x) for x in views]))
        missing = [i for i in idx if colours[i] is None]
        if missing:                                    # a thumb that would not open: its own unnamed variant
            variants.append(dict(rows=missing, lab=None, views=[0] * len(missing)))
        variants.sort(key=lambda v: (-len(v["rows"]), min(v["rows"])))
        out.append(dict(rows=[int(i) for i in idx], variants=variants))
    out.sort(key=lambda p: (-len(p["rows"]), min(p["rows"])))
    return out

def grey_embeddings(paths: list[Path], progress=None, batch: int = 32) -> np.ndarray:
    """Unit embeddings of the grey versions of the given thumbs (see PRODUCT_SIM). A thumb that will not open embeds
    as a flat grey frame, which matches nothing well."""
    from PIL import Image
    from .embed import get_embedder
    E = get_embedder()
    out = []
    for k in range(0, len(paths), batch):
        ims = []
        for p in paths[k:k + batch]:
            try:
                with Image.open(p) as im:
                    ims.append(im.convert("L").convert("RGB"))
            except Exception:
                ims.append(Image.new("RGB", (64, 64), (128, 128, 128)))
        out.append(np.asarray(E.encode_images(ims), np.float32))
        if progress:
            progress(min(k + batch, len(paths)), len(paths))
    X = np.concatenate(out) if out else np.zeros((0, 1), np.float32)
    return X / np.linalg.norm(X, axis=1, keepdims=True).clip(1e-9)

# ===== client pick: the grey embeddings of the last find, beside index.db (float16, about 1.5 KB a photo) =====
GREY_FILE = "product_grey.npz"

def save_grey(root: Path, ids: list[int], X: np.ndarray) -> None:
    np.savez(db.index_dir(Path(root)) / GREY_FILE, ids=np.asarray(ids, np.int64), X=np.asarray(X, np.float16))

def load_grey(root: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """(ids, unit rows as float32) from the last find, or None when products were found before this file existed."""
    p = db.index_dir(Path(root)) / GREY_FILE
    if not p.is_file():
        return None
    with np.load(p) as z:
        ids, X = z["ids"].astype(np.int64), z["X"].astype(np.float32)
    return ids, X / np.linalg.norm(X, axis=1, keepdims=True).clip(1e-9)
# ===== end client pick =====

def find_products(root: Path, ids: list[int] | None = None, progress=None) -> dict:
    """Group the shoot's stills (or just ids, the photos on screen) into products and colour variants, and store them
    (tables products and product_sets, replacing the last run). Reads the grid thumbs on this Mac only: the shoot disk
    can be unplugged. progress(done, total) is called per batch. Returns {products, variants, photos}."""
    root = Path(root); conn = db.connect(root)
    still = {r[0]: r[1] for r in conn.execute("SELECT id, qhash FROM photos WHERE status='ok' AND kind!='video' AND qhash IS NOT NULL ORDER BY id")}
    want = set(ids) if ids is not None else None
    pids = [p for p in still if want is None or p in want]
    conn.execute("DELETE FROM products"); conn.execute("DELETE FROM product_sets")
    # ===== client pick: its picks name these product numbers, so a new find starts it over =====
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cull'").fetchone():
        conn.execute("DELETE FROM cull")
    # ===== end client pick =====
    if len(pids) < PRODUCT_MIN_PHOTOS:
        conn.commit()
        return {"products": 0, "variants": 0, "photos": 0}
    grid = db.index_dir(root) / "grid"
    paths = [grid / f"{still[p]}.jpg" for p in pids]
    X = grey_embeddings(paths, progress)
    # ===== client pick: keep the grey embeddings, so Client pick can match angles across colours and put a frame that
    # fell out of every product back next to its nearest one without encoding the shoot again (cull.py) =====
    save_grey(root, pids, X)
    # ===== end client pick =====
    colours = [subject_colour(p) for p in paths]
    found = group(X, colours)
    from .embed import get_embedder
    E = get_embedder()
    all_ids, M = db.load_embeds(conn)                 # the colour embeddings name the product, as a category is named
    pos = {int(p): i for i, p in enumerate(all_ids.tolist())}
    T = getattr(E, "_product_T", None)
    if T is None:
        T = E.encode_text(list(PRODUCT_VOCAB)); E._product_T = T
    used: dict[str, int] = {}
    unnamed = nvar = nphotos = 0
    for k, prod in enumerate(found, 1):
        rows = [pos[pids[r]] for r in prod["rows"] if pids[r] in pos]
        name = None
        if rows:
            cent = M[rows].astype(np.float32).mean(0); cent /= (np.linalg.norm(cent) or 1.0)
            name = _name(cent, T)
        if name is None:
            unnamed += 1; name, named = f"product {unnamed}", 0
        else:
            used[name] = used.get(name, 0) + 1
            name, named = (name if used[name] == 1 else f"{name} {used[name]}"), 1
        for v, var in enumerate(prod["variants"], 1):
            lab = var["lab"]
            cname, hexc = (colour_name(lab), "#%02x%02x%02x" % _rgb(lab)) if lab is not None else ("unknown", None)
            conn.execute("INSERT INTO product_sets(product, variant, name, named, colour, hex, n, views) VALUES(?,?,?,?,?,?,?,?)",
                         (k, v, name, named, cname, hexc, len(var["rows"]), len(set(var["views"]))))
            conn.executemany("INSERT INTO products(photo_id, product, variant, view) VALUES(?,?,?,?)",
                             [(pids[r], k, v, view) for r, view in zip(var["rows"], var["views"])])
            nvar += 1; nphotos += len(var["rows"])
    conn.commit()
    return {"products": len(found), "variants": nvar, "photos": nphotos}

def list_products(conn) -> list[dict]:
    """[{product, name, named, n, variants: [{variant, colour, hex, n, views, cover}]}], biggest product first. cover is
    the qhash of the variant's most central photo (lowest id among its first view) for the swatch row's thumbnail."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT product, variant, name, named, colour, hex, n, views FROM product_sets ORDER BY product, variant"):
        p = out.setdefault(r[0], {"product": r[0], "name": r[2], "named": bool(r[3]), "n": 0, "variants": []})
        cover = conn.execute("SELECT p.qhash FROM products x JOIN photos p ON p.id=x.photo_id WHERE x.product=? AND x.variant=? "
                             "ORDER BY x.view, p.id LIMIT 1", (r[0], r[1])).fetchone()
        p["variants"].append({"variant": r[1], "colour": r[4], "hex": r[5], "n": r[6], "views": r[7], "cover": cover[0] if cover else None})
        p["n"] += r[6]
    return sorted(out.values(), key=lambda p: (-p["n"], p["product"]))

def rename(conn, product: int, name: str) -> int:
    n = conn.execute("UPDATE product_sets SET name=?, named=1 WHERE product=?", (name.strip(), product)).rowcount
    conn.commit()
    return n
