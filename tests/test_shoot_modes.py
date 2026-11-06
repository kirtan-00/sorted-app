"""Shoot modes (docs/shoot-modes.md): camera settings off EXIF, exposure brackets, bursts done properly, camera facets."""
from fractions import Fraction
from pathlib import Path
import numpy as np
from PIL import Image
from photosort import db
from photosort.features import exif_info
from photosort.index import index_folder, read_camera_info, camera_info_status
from photosort.search import Index, Filters, ev_label, bucket, ev100


def scene(seed=0, size=(900, 600)):
    """A photo-like picture: soft blobs and edges, so a stop up or down keeps its structure."""
    rng = np.random.default_rng(seed)
    h, w = size[1], size[0]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    a = np.full((h, w, 3), 0.06, np.float32)
    for _ in range(14):
        cx, cy, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(30, 160)
        col = rng.uniform(0.01, 0.22, 3)            # a +2 stop frame (x4) stays under clipping, as a metered scene does
        m = ((xx - cx) ** 2 + (yy - cy) ** 2) < r * r
        a[m] = col
    return a                                    # linear light, 0..1

def shot(lin, stops=0.0):
    a = np.clip(lin * (2 ** stops), 0, 1) ** (1 / 2.2)
    return Image.fromarray((a * 255).astype(np.uint8))

def save(tmp_path, name, im, when="2026:10:17 10:00:00", sub="0", t=Fraction(1, 125), n=Fraction(28, 10), iso=100,
         bias=Fraction(0), focal=50, lens="FE 24-70mm F2.8 GM II", mode=1, model="ILCE-7RM5"):
    from PIL.TiffImagePlugin import IFDRational
    R = lambda f: IFDRational(Fraction(f).numerator, Fraction(f).denominator)
    t, n, bias = (R(x) if x is not None else None for x in (t, n, bias))
    ex = Image.Exif()
    ex[0x010F] = "SONY"; ex[0x0110] = model
    e = ex.get_ifd(0x8769)
    e[0x9003] = when; e[0x9291] = sub
    if t is not None: e[0x829A] = t
    if n is not None: e[0x829D] = n
    if iso is not None: e[0x8827] = iso
    e[0x9204] = bias; e[0x920A] = R(focal); e[0xA434] = lens; e[0xA402] = mode
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    im.save(p, quality=90, exif=ex)
    return p


def test_exif_reads_exposure_settings(tmp_path):
    p = save(tmp_path, "DSC00001.JPG", shot(scene()), t=Fraction(1, 160), n=Fraction(28, 10), iso=10000,
             bias=Fraction(-2, 3), sub="214", mode=2)
    i = exif_info(p)
    assert i["exposure_s"] == 1 / 160 and i["fnumber"] == 2.8 and i["iso"] == 10000
    assert i["ev_bias"] == -0.67 and i["focal_mm"] == 50 and i["lens"] == "FE 24-70mm F2.8 GM II"
    assert i["exp_mode"] == 2 and abs(i["subsec"] - 0.214) < 1e-9
    bare = tmp_path / "plain.jpg"; shot(scene()).save(bare)
    j = exif_info(bare)
    assert j["exposure_s"] is None and j["lens"] == ""        # read, and the file names no lens


def test_ev_label_and_buckets():
    assert [ev_label(x) for x in (0, 2, -2, 0.67, -1.33, 0.01)] == ["0", "+2", "-2", "+0.7", "-1.3", "0"]
    assert bucket("shutter", {"exposure_s": 1 / 4000}) == "fast" and bucket("shutter", {"exposure_s": 1 / 1000}) == "fast"
    assert bucket("shutter", {"exposure_s": 1 / 160}) == "normal" and bucket("shutter", {"exposure_s": 2.0}) == "long"
    assert bucket("aperture", {"fnumber": 2.8}) == "mid" and bucket("iso", {"iso": 10000}) == "gt6400"
    assert bucket("focal", {"focal_mm": 35}) == "20to35" and bucket("focal", {}) == "unknown"
    # one stop more shutter time is one stop brighter
    assert abs(ev100({"exposure_s": 1 / 60, "fnumber": 4, "iso": 100}) - ev100({"exposure_s": 1 / 30, "fnumber": 4, "iso": 100}) - 1) < 1e-9


def _bracket_shoot(tmp_path):
    lin = scene(1)
    # a manual-mode bracket: the shutter moves, EV compensation stays 0 on every frame (what a Sony writes)
    for k, (stops, t) in enumerate(((0, Fraction(1, 125)), (-2, Fraction(1, 500)), (2, Fraction(1, 30)))):
        save(tmp_path, f"b/DSC0010{k}.JPG", shot(lin, stops), when="2026:10:17 10:00:00", sub=str(2 + 3 * k), t=t)
    # then a three-frame burst of another view at one exposure
    other = scene(7)
    for k in range(3):
        save(tmp_path, f"b/DSC0011{k}.JPG", shot(other), when="2026:10:17 10:00:30", sub=str(k * 2))
    # and a different view at another exposure, right after: not part of anything
    save(tmp_path, "b/DSC00113.JPG", shot(scene(9), 1), when="2026:10:17 10:00:31", t=Fraction(1, 60))


def test_bracket_found_folded_to_middle_exposure(tmp_path):
    _bracket_shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    rows = {r["rel"]: r for r in ix.search(limit=50)}
    g = rows["b/DSC00100.JPG"]["group"]
    assert g["kind"] == "bracket" and g["n"] == 3 and g["ev"] == ["0", "-2", "+2"]
    assert g["base"] == rows["b/DSC00100.JPG"]["id"]
    assert rows["b/DSC00113.JPG"]["group"] is None
    assert rows["b/DSC00110.JPG"]["group"]["kind"] == "burst"
    folded = ix.search(filters=Filters(fold=True), limit=50)
    assert [r["rel"] for r in folded] == ["b/DSC00100.JPG", "b/DSC00110.JPG", "b/DSC00113.JPG"]
    # the group filter and its facet
    assert {r["rel"] for r in ix.search(filters=Filters(group="bracket"))} == {f"b/DSC0010{k}.JPG" for k in range(3)}
    fc = ix.facet_counts(Filters())
    assert fc["groups"] == {"bracket": 3, "burst": 3} and fc["group_sets"] == {"burst": 1, "bracket": 1, "copies": 0}


def test_bracket_order_dark_first_still_folds_to_zero(tmp_path):
    lin = scene(2)
    # aperture priority: the EV compensation moves and the camera moves the shutter to match
    for k, (stops, t) in enumerate(((-1, Fraction(1, 250)), (0, Fraction(1, 125)), (1, Fraction(1, 60)))):
        save(tmp_path, f"DSC0020{k}.JPG", shot(lin, stops), when="2026:10:17 11:00:00", t=t,
             bias=Fraction(stops), sub=str(k), mode=0)
    index_folder(tmp_path, faces=False, workers=1)
    rows = Index(tmp_path).search(filters=Filters(fold=True))
    assert len(rows) == 1 and rows[0]["rel"] == "DSC00201.JPG" and rows[0]["group"]["ev"] == ["-1", "0", "+1"]


def test_not_a_bracket(tmp_path):
    """Two frames is a photographer adjusting, different views are not one bracket, and the same exposure twice
    in a row (0 -2 +2 0 -2 +2) splits into two sets."""
    a, b = scene(3), scene(4)
    save(tmp_path, "x/DSC00300.JPG", shot(a), sub="0")
    save(tmp_path, "x/DSC00301.JPG", shot(a, -2), sub="3", t=Fraction(1, 500))
    save(tmp_path, "y/DSC00400.JPG", shot(a), sub="0")
    save(tmp_path, "y/DSC00401.JPG", shot(b, -2), sub="3", t=Fraction(1, 500))
    save(tmp_path, "y/DSC00402.JPG", shot(a, 2), sub="6", t=Fraction(1, 30))
    for k, (s, t) in enumerate(((0, Fraction(1, 125)), (-2, Fraction(1, 500)), (2, Fraction(1, 30))) * 2):
        save(tmp_path, f"z/DSC0050{k}.JPG", shot(a, s), when="2026:10:17 10:00:01" if k < 3 else "2026:10:17 10:00:02",
             sub=str(1 + 2 * (k % 3)), t=t)
    index_folder(tmp_path, faces=False, workers=1)
    rows = {r["rel"]: r for r in Index(tmp_path).search(limit=50)}
    assert (rows["x/DSC00300.JPG"]["group"] or {}).get("kind") != "bracket"
    assert all((rows[f"y/DSC0040{k}.JPG"]["group"] or {}).get("kind") != "bracket" for k in range(3))
    z = [rows[f"z/DSC0050{k}.JPG"]["group"] for k in range(6)]
    assert all(g and g["kind"] == "bracket" and g["n"] == 3 for g in z) and z[0] is not z[3] and z[0] is z[2]


def test_camera_said_bracket_allows_two_frames(tmp_path):
    a = scene(5)
    save(tmp_path, "DSC00600.JPG", shot(a), mode=2, bias=Fraction(0))
    save(tmp_path, "DSC00601.JPG", shot(a, -1), mode=2, bias=Fraction(-1), t=Fraction(1, 250), sub="5")
    index_folder(tmp_path, faces=False, workers=1)
    g = Index(tmp_path).search()[0]["group"]
    assert g["kind"] == "bracket" and g["ev"] == ["0", "-1"]      # median of two is the brighter one


def test_burst_best_by_and_exposure_guard(tmp_path):
    """A burst holds its exposure: consecutive frames a stop apart are not a burst (even with nothing else to go on)."""
    a = scene(6)
    save(tmp_path, "DSC00700.JPG", shot(a), sub="0")
    save(tmp_path, "DSC00701.JPG", shot(a), sub="2")
    index_folder(tmp_path, faces=False, workers=1)
    g = Index(tmp_path).search()[0]["group"]
    assert g["kind"] == "burst" and g["best_by"] == "detail"
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET exposure_s=0.004 WHERE rel='DSC00701.JPG'"); conn.commit()   # 1/250 against 1/125: a stop
    assert Index(tmp_path).search()[0]["group"] is None


def test_camera_facets_and_filters(tmp_path):
    save(tmp_path, "DSC00800.JPG", shot(scene(1)), t=Fraction(1, 2000), n=Fraction(18, 10), iso=100, focal=24)
    save(tmp_path, "DSC00900.JPG", shot(scene(2)), when="2026:10:17 12:00:00", t=Fraction(1, 60), n=Fraction(8),
         iso=3200, focal=85, lens="FE 85mm F1.4 GM")
    shot(scene(3)).save(tmp_path / "phone.jpg")
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    fc = ix.facet_counts(Filters())
    assert fc["lens"] == {"FE 24-70mm F2.8 GM II": 1, "FE 85mm F1.4 GM": 1, "unknown": 1}
    assert fc["shutter"] == {"fast": 1, "normal": 1, "unknown": 1}
    assert fc["aperture"] == {"wide": 1, "small": 1, "unknown": 1}
    assert fc["focal"] == {"20to35": 1, "71to135": 1, "unknown": 1} and fc["iso"] == {"le400": 1, "to6400": 1, "unknown": 1}
    assert [r["rel"] for r in ix.search(filters=Filters(lens="unknown"))] == ["phone.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(iso="to6400"))] == ["DSC00900.JPG"]
    # counted with its own filter off, the others on
    fc = ix.facet_counts(Filters(lens="FE 85mm F1.4 GM"))
    assert fc["total"] == 1 and fc["lens"]["unknown"] == 1 and fc["shutter"] == {"normal": 1}


def test_read_camera_info_catches_up_old_index(tmp_path):
    save(tmp_path, "DSC01000.JPG", shot(scene(1)), iso=640)
    save(tmp_path, "gone/DSC01001.JPG", shot(scene(2)), iso=640)
    index_folder(tmp_path, faces=False, workers=1)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET exposure_s=NULL, fnumber=NULL, iso=NULL, lens=NULL"); conn.commit()   # an index from v0.6
    assert camera_info_status(tmp_path) == {"photos": 2, "known": 0, "unknown": 2, "read_at": None}
    (tmp_path / "gone/DSC01001.JPG").unlink()
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert read_camera_info(tmp_path) == {"read": 2, "found": 1, "missing": 1}
    assert sorted(p.name for p in tmp_path.rglob("*")) == before                # nothing written under the shoot
    st = camera_info_status(tmp_path)
    assert st["known"] == 1 and st["read_at"]
    assert conn.execute("SELECT iso, lens FROM photos WHERE rel='DSC01000.JPG'").fetchone()[:] == (640, "FE 24-70mm F2.8 GM II")


# ===== server =====
def _wait(c, url):
    import time
    for _ in range(600):
        d = c.get(url).json()
        if not d["running"]:
            return d
        time.sleep(0.05)
    raise AssertionError(url)

def test_server_filters_best_and_camera(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.server import create_app
    _bracket_shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/search", params={"group": "bracket"}).json()
    assert r["total"] == 3 and r["results"][0]["group"]["ev"] == ["0", "-2", "+2"]
    r = c.get("/api/search", params={"fold": 1}).json()
    assert r["total"] == 3 and r["folded"] == 4
    fc = c.get("/api/search/facets", params={"shutter": "normal"}).json()
    assert fc["groups"]["burst"] == 3 and fc["group_sets"]["bracket"] == 1 and fc["shutter"]["action"] == 1
    assert c.get("/api/search", params={"group": "nope"}).status_code == 400
    assert c.get("/api/search", params={"iso": "huge"}).status_code == 400
    assert c.get("/api/search/ids", params={"lens": "FE 24-70mm F2.8 GM II"}).json()["total"] == 7
    best = c.post("/api/groups/best", params={"kind": "burst"}).json()
    assert c.get("/api/marks/counts").json()["flag"]["pick"] == best["n"]  # the one list: the flag moved with it
    assert best["sets"] == 1 and best["added"] == 1 and best["n"] == 1
    assert c.post("/api/groups/best", params={"kind": "burst", "camera": "nobody"}).json()["sets"] == 0   # every filter counts
    best = c.post("/api/groups/best", params={"kind": "bracket"}).json()
    rows = {x["rel"]: x["id"] for x in c.get("/api/search").json()["results"]}
    assert best["n"] == 2 and rows["b/DSC00100.JPG"] in best["ids"]
    st = c.get("/api/camera/status").json()
    assert st["known"] == 7 and st["unknown"] == 0
    assert c.post("/api/camera/read").json()["started"]
    assert _wait(c, "/api/camera/progress")["counts"]["read"] == 0


def _product_shoot(tmp_path):
    """Three products, two colours each, two views each: drawn shapes, recoloured, one turned."""
    from PIL import ImageDraw
    import colorsys
    def draw(kind, hue, turn):
        im = Image.new("RGB", (600, 600), (228, 228, 225)); d = ImageDraw.Draw(im)
        col = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(hue, .75, .8))
        if kind == "bottle":
            d.rounded_rectangle((230, 200, 370, 520), radius=40, fill=col); d.rectangle((280, 120, 320, 210), fill=col)
            d.rectangle((270, 90, 330, 125), fill=(30, 30, 30)); d.rectangle((230, 320, 370, 390), fill=(245, 242, 235))
        elif kind == "mug":
            d.ellipse((360, 260, 470, 420), fill=col); d.ellipse((390, 295, 445, 385), fill=(228, 228, 225))
            d.rounded_rectangle((160, 220, 400, 500), radius=30, fill=col)
        else:
            d.polygon([(90, 420), (120, 300), (220, 270), (310, 340), (460, 360), (520, 420)], fill=col)
            d.rounded_rectangle((80, 410, 530, 460), radius=20, fill=(240, 240, 236))
        return im.rotate(10, fillcolor=(228, 228, 225)) if turn else im
    k = 0
    for kind in ("bottle", "mug", "sneaker"):
        for hue in (0.0, 0.6):
            for turn in (False, True):
                draw(kind, hue, turn).save(tmp_path / f"P{k:04d}.jpg", quality=90); k += 1

def test_products_group_into_variants_and_export(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.server import create_app
    from photosort import products
    _product_shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    found = products.find_products(tmp_path)
    assert found == {"products": 3, "variants": 6, "photos": 12}
    listed = products.list_products(db.connect(tmp_path))
    assert all(p["n"] == 4 and len(p["variants"]) == 2 for p in listed)
    assert all(sorted(v["colour"] for v in p["variants"]) == ["blue", "red"] for p in listed)
    # a product's photos are one product: P0000..P0003 are the bottle
    conn = db.connect(tmp_path)
    by = dict(conn.execute("SELECT p.rel, x.product * 10 + x.variant FROM products x JOIN photos p ON p.id = x.photo_id"))
    assert by["P0000.jpg"] == by["P0001.jpg"] != by["P0002.jpg"] == by["P0003.jpg"]   # turned: same variant; blue: another
    assert by["P0000.jpg"] // 10 == by["P0002.jpg"] // 10 != by["P0004.jpg"] // 10
    c = TestClient(create_app(tmp_path))
    lst = c.get("/api/products").json()["products"]
    p0, v0 = lst[0]["product"], lst[0]["variants"][0]["variant"]
    r = c.get("/api/search", params={"product": p0, "variant": v0}).json()
    assert r["total"] == 2
    assert c.post(f"/api/products/{p0}/name", json={"name": "hero bottle"}).json()["name"] == "hero bottle"
    before = sorted(p.name for p in tmp_path.rglob("*"))
    ex = c.post("/api/export/products", json={"mode": "copy"}).json()
    assert ex["total"] == 12 and len(ex["folders"]) == 6 and any(f.startswith("hero bottle ") for f in ex["folders"])
    for _ in range(400):
        d = c.get("/api/export/progress").json()
        if not d["running"]:
            break
        import time; time.sleep(0.05)
    out = Path(d["path"])
    assert sorted(len(list(x.glob("*.jpg"))) for x in out.iterdir() if x.is_dir()) == [2] * 6
    assert sorted(p.name for p in tmp_path.rglob("*")) == before                 # nothing written into the shoot
    # a run over fewer photos replaces the last one
    assert products.find_products(tmp_path, ids=[1])["products"] == 0 and products.list_products(conn) == []


def test_subject_colour_and_names(tmp_path):
    from photosort.products import subject_colour, colour_name
    im = Image.new("RGB", (300, 300), (230, 230, 228))
    from PIL import ImageDraw
    d = ImageDraw.Draw(im); d.rectangle((130, 20, 170, 280), fill=(40, 90, 200)); d.ellipse((90, 90, 210, 210), fill=(20, 20, 25))
    im.save(tmp_path / "w.jpg")
    assert colour_name(subject_colour(tmp_path / "w.jpg")) == "blue"       # a watch is its strap, not its black face
    Image.new("RGB", (300, 300), (20, 20, 22)).save(tmp_path / "k.jpg")
    assert colour_name(subject_colour(tmp_path / "k.jpg")) == "black"
    assert subject_colour(tmp_path / "missing.jpg") is None


def test_products_find_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.server import create_app
    _product_shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/products").json() == {"products": []}
    assert c.post("/api/products/find", json={}).json()["started"]
    d = _wait(c, "/api/products/progress")
    assert d["error"] is None and d["counts"]["products"] == 3 and d["done"] == d["total"] == 12
    assert len(c.get("/api/products").json()["products"]) == 3
    assert c.post("/api/products/99/name", json={"name": "x"}).status_code == 404


def test_auto_exposure_drift_is_a_burst_not_a_bracket(tmp_path):
    """Auto ISO moves a third of a stop between frames of one scene (800, 1000, 1250): that is neither a bracket nor a
    reason to break the burst."""
    a = scene(8)
    for k, iso in enumerate((800, 1000, 1250)):
        save(tmp_path, f"DSC0080{k}.JPG", shot(a), sub=str(k * 2), iso=iso, mode=0)
    index_folder(tmp_path, faces=False, workers=1)
    g = Index(tmp_path).search()[0]["group"]
    assert g["kind"] == "burst" and g["n"] == 3
