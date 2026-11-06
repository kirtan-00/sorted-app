"""Client pick (photosort/cull.py, docs/client-pick.md): quality on the product, the pick rule, the endpoints."""
import colorsys, csv, time
from pathlib import Path
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter
from photosort import cull, db
from photosort.features import sharpness_tiles, to_gray


# ===== drawn frames =====
def backdrop(size=600, textured=True, seed=0):
    """A light sweep; textured adds a sharp linen grain, the case a whole-frame focus check gets wrong."""
    y = np.linspace(0, 1, size)[:, None]
    g = (232 - 25 * y) * np.ones((1, size))
    if textured:
        rng = np.random.default_rng(seed)
        g = g + rng.normal(0, 7, g.shape) + 6 * np.sin(np.arange(size)[None, :] * 1.9) * np.sin(np.arange(size)[:, None] * 2.3)
    return Image.fromarray(np.clip(np.stack([g, g, g * 0.985], -1), 0, 255).astype(np.uint8))

def product(kind="bottle", hue=0.0, size=600, shift=(0, 0), turn=0.0, v=0.8):
    """(colour layer, mask): a labelled bottle or a mug with a handle."""
    col = Image.new("RGB", (size, size)); m = Image.new("L", (size, size))
    d, dm = ImageDraw.Draw(col), ImageDraw.Draw(m)
    c = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(hue, .75, v))
    def both(fn, box, fill, **kw):
        getattr(d, fn)(box, fill=fill, **kw); getattr(dm, fn)(box, fill=255, **kw)
    if kind == "bottle":
        both("rounded_rectangle", (230, 200, 370, 520), c, radius=40); both("rectangle", (280, 120, 320, 210), c)
        both("rectangle", (270, 90, 330, 125), (30, 30, 30)); d.rectangle((230, 320, 370, 390), fill=(245, 242, 235))
        d.text((250, 345), "AQUA 750", fill=(30, 30, 30))
    else:
        both("ellipse", (360, 260, 470, 420), c); d.ellipse((390, 295, 445, 385), fill=(0, 0, 0)); dm.ellipse((390, 295, 445, 385), fill=0)
        both("rounded_rectangle", (160, 220, 400, 500), c, radius=30)
        for k in range(4):
            d.line((190, 260 + 50 * k, 370, 260 + 50 * k), fill=(250, 250, 250), width=4)
    if turn:
        col = col.rotate(turn, resample=Image.BICUBIC); m = m.rotate(turn, resample=Image.BICUBIC)
    if shift != (0, 0):
        col = col.rotate(0, translate=shift); m = m.rotate(0, translate=shift)
    return col, m

def frame(kind="bottle", hue=0.0, blur=0.0, motion=0, shift=(0, 0), turn=0.0, stops=0.0, textured=True, seed=0, v=0.8):
    col, m = product(kind, hue, shift=shift, turn=turn, v=v)
    if blur:
        col = col.filter(ImageFilter.GaussianBlur(blur))
    im = Image.composite(col, backdrop(textured=textured, seed=seed), m.filter(ImageFilter.GaussianBlur(max(blur, 1.0))))
    if motion:
        import cv2
        k = np.zeros((motion, motion), np.float32); k[motion // 2] = 1.0 / motion
        im = Image.fromarray(cv2.filter2D(np.asarray(im), -1, k))
    if stops:
        a = np.asarray(im).astype(np.float32) / 255
        im = Image.fromarray((np.clip(a ** 2.2 * 2 ** stops, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8))
    return im


# ===== quality on the product =====
def test_soft_product_on_a_sharp_backdrop_is_soft_on_the_product_not_on_the_frame():
    sharp, soft = frame(), frame(blur=4.0)
    ms, mb = cull.measure_array(np.asarray(sharp)), cull.measure_array(np.asarray(soft))
    assert 0.02 < ms["area"] < 0.4 and mb["area"] > 0.02
    assert mb["sharp"] / ms["sharp"] < cull.SOFT_RATIO                      # the product lost its edges
    # the whole-frame measure focus.py and the index use barely moves: the linen grain is still sharp
    fs, fb = sharpness_tiles(to_gray(sharp))[0], sharpness_tiles(to_gray(soft))[0]
    assert fb / fs > 0.8

def test_motion_blur_is_one_sided():
    ref = cull.measure_array(np.asarray(frame()))
    shake = cull.measure_array(np.asarray(frame(motion=21)))
    soft = cull.measure_array(np.asarray(frame(blur=3.0)))
    assert cull.anisotropy(shake["dirs"], ref["dirs"]) < cull.MOTION_ANISO
    assert cull.anisotropy(soft["dirs"], ref["dirs"]) > cull.MOTION_ANISO
    assert cull.anisotropy(ref["dirs"], ref["dirs"]) == pytest.approx(1.0)

def test_cut_off_blown_and_crushed():
    m = cull.measure_array(np.asarray(frame(shift=(-300, 0))))           # half the bottle off the left edge
    assert m["sides"][3] >= cull.CUT_SIDE and cull.cut_off(m["sides"])
    assert not cull.cut_off(cull.measure_array(np.asarray(frame()))["sides"])
    assert cull.cut_off([0, 0, 0, 0.3]) and cull.cut_off([0.2, 0.2, 0, 0])     # one edge, or two that meet
    assert not cull.cut_off([0.5, 0, 0.5, 0]) and not cull.cut_off([0.5, 0.5, 0.5, 0])   # a tight strap, a detail
    blown = cull.measure_array(np.asarray(frame(stops=2.5)))
    assert blown["blown"] >= cull.BLOWN_SHARE
    assert cull.measure_array(np.asarray(frame()))["blown"] < cull.BLOWN_SHARE
    dark = np.asarray(frame(hue=0.62, v=0.3)).astype(np.float32)          # a navy bottle under a heavy curve
    crushed = cull.measure_array(np.clip((dark - 70) / 185 * 255, 0, 255).astype(np.uint8))
    assert crushed["crushed"] >= cull.CRUSHED_SHARE
    # nothing stands off the backdrop: clipping is read in the middle of the frame
    assert cull.measure_array(np.zeros((300, 300, 3), np.uint8))["crushed"] == 1.0

def test_judge_reasons_and_one_per_retake(tmp_path):
    from photosort.search import framing_field
    shots = {"a0": frame(seed=1), "a1": frame(shift=(3, 2), seed=2), "a2": frame(shift=(-2, 1), seed=3),
             "soft": frame(blur=4.0, seed=4), "shake": frame(motion=25, seed=5), "cut": frame(shift=(-300, 0), seed=6),
             "blown": frame(stops=2.5, seed=7), "b0": frame(turn=35, seed=8), "b1": frame(turn=35, shift=(2, 3), seed=9)}
    frames = []
    for k, (name, im) in enumerate(shots.items()):
        p = tmp_path / f"{name}.jpg"; im.save(p, quality=92)
        g = im.copy(); g.thumbnail((320, 320)); g.save(tmp_path / f"{name}_g.jpg", quality=80)
        frames.append({"id": k + 1, "variant": 1, "m": cull.measure(p), "field": framing_field(tmp_path / f"{name}_g.jpg"), "name": name})
    out = {f["name"]: f for f in cull.judge(frames)}
    assert out["soft"]["reason"] == "soft" and out["shake"]["reason"] == "motion"
    assert out["cut"]["reason"] == "cut" and out["blown"]["reason"] == "blown"
    assert sorted(out[n]["reason"] or "keep" for n in ("a0", "a1", "a2")) == ["keep", "retake", "retake"]
    assert sorted(out[n]["reason"] or "keep" for n in ("b0", "b1")) == ["keep", "retake"]
    assert out["a0"]["retake"] == out["a1"]["retake"] != out["b0"]["retake"]
    kept = [f for f in out.values() if f["reason"] is None]
    assert len({f["angle"] for f in kept}) == 2                            # front and turned are two angles


# ===== the pick rule =====
def cands(spec):
    """spec: {variant: {angle: score}} -> keepable frames, one per (variant, angle)."""
    out, i = [], 0
    for v, angles in spec.items():
        for a, s in angles.items():
            i += 1; out.append({"id": i, "variant": v, "angle": a, "score": s})
    return out

def test_pick_covers_every_colour_then_every_angle_then_quality():
    c = cands({1: {1: .9, 2: .8, 3: .7, 4: .95}, 2: {1: .6, 2: .99}, 3: {3: .5}})
    by = {x["id"]: x for x in c}
    p3 = [by[i] for i in cull.pick(c, 3)]
    assert {x["variant"] for x in p3} == {1, 2, 3}                          # three picks, three colours
    p4 = [by[i] for i in cull.pick(c, 4)]
    assert {x["angle"] for x in p4} == {1, 2, 3, 4}                         # then the angle nobody has
    assert len(cull.pick(c, 10)) == len(c) and len(set(cull.pick(c, 10))) == len(c)   # never more than there is
    assert cull.pick(c, 0) == [] and cull.pick([], 5) == []
    assert cull.pick(c, 5) == cull.pick(list(reversed(c)), 5)              # the order frames arrive in does not matter
    # inside a colour the best frame wins when the angles are tied
    one = cands({1: {1: .2, 2: .9}})
    assert cull.pick(one, 1) == [2]

def test_split_total_by_colours_with_caps():
    assert cull.split_total(30, {1: 1, 2: 2}, {1: 99, 2: 99}) == {1: 10, 2: 20}
    s = cull.split_total(300, {k: 1 + k % 3 for k in range(30)}, {k: 50 for k in range(30)})
    assert sum(s.values()) == 300
    # a product that has only 4 frames to give gives 4; the rest goes to the others
    assert cull.split_total(20, {1: 1, 2: 1}, {1: 4, 2: 99}) == {1: 4, 2: 16}
    assert sum(cull.split_total(500, {1: 1, 2: 1}, {1: 4, 2: 6}).values()) == 10


# ===== the endpoints =====
def _shoot(tmp_path):
    """Two products (bottle, mug), two colours, two angles, three retakes each, plus one soft, one cut off and one
    blown frame of the red bottle's front."""
    k = 0
    def save(im):
        nonlocal k
        im.save(tmp_path / f"P{k:04d}.jpg", quality=90); k += 1
    for kind in ("bottle", "mug"):
        for hue in (0.0, 0.6):
            for turn in (0.0, 30.0):
                for t in range(3):
                    save(frame(kind, hue, turn=turn, shift=(t * 2, -t), seed=k))
    save(frame("bottle", 0.0, blur=6.0, seed=90)); save(frame("bottle", 0.0, shift=(-300, 0), seed=91))
    save(frame("bottle", 0.0, stops=2.5, seed=92))

def _wait(c, url):
    for _ in range(1200):
        d = c.get(url).json()
        if not d["running"]:
            return d
        time.sleep(0.05)
    raise AssertionError(url)

def test_client_pick_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.index import index_folder
    from photosort.server import create_app
    _shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/cull").json()["photos"] == 0
    assert c.post("/api/cull/run", json={}).status_code == 400              # find products first
    assert c.post("/api/products/find", json={}).json()["started"]
    _wait(c, "/api/products/progress")
    assert c.post("/api/cull/run", json={"per_product": 0}).status_code == 400
    assert c.post("/api/cull/run", json={"per_product": 3}).json()["started"]
    d = _wait(c, "/api/cull/progress")
    assert d["error"] is None and d["done"] == d["total"] == 27
    L = c.get("/api/cull").json()
    assert L["photos"] == 27 and L["target"] == {"per_product": 3}
    rows = {f["rel"]: f for p in L["products"] for f in p["picks"] + p["spare"] + p["rejects"]}
    assert len(rows) == 27
    assert rows["P0024.jpg"]["reason"] == "soft" and rows["P0025.jpg"]["reason"] == "cut" and rows["P0026.jpg"]["reason"] == "blown"
    assert rows["P0024.jpg"]["words"] == "soft on the product"
    picks = [f for p in L["products"] for f in p["picks"]]
    assert all(f["reason"] is None for f in picks)
    for p in L["products"]:
        if p["product"]:
            assert p["picked"] == 3 and len({f["variant"] for f in p["picks"]}) == 2          # both colours
            assert not any(a["variant"] == b["variant"] and a["angle"] == b["angle"]
                           for i, a in enumerate(p["picks"]) for b in p["picks"][i + 1:])     # one per colour and angle
    assert L["rejected"].get("retake", 0) == 16                             # 8 angles of 3 takes keep one each
    # a new target re-picks from the stored reasons
    T = c.post("/api/cull/target", json={"total": 6}).json()
    assert T["picked"] == 6 and T["target"] == {"total": 6}
    T = c.post("/api/cull/target", json={"per_product": 4}).json()
    assert T["picked"] == 8
    # swap: the same colour and angle, never a pick
    p0 = next(p for p in T["products"] if p["product"])
    f = p0["picks"][0]
    s = c.post("/api/cull/swap", json={"id": f["id"]}).json()
    assert s["out"] == f["id"] and s["in"] is not None
    after = {x["id"]: x for p in c.get("/api/cull").json()["products"] for x in p["picks"] + p["spare"] + p["rejects"]}
    assert after[s["in"]]["pick"] and not after[f["id"]]["pick"]
    assert after[s["in"]]["variant"] == f["variant"] and after[s["in"]]["angle"] == f["angle"]
    assert c.post("/api/cull/swap", json={"id": f["id"]}).status_code == 404   # no longer a pick
    assert c.post("/api/cull/pick", json={"id": f["id"], "on": True}).json()["pick"]
    assert c.post("/api/cull/pick", json={"id": 99999}).status_code == 404
    n = c.get("/api/cull").json()["picked"]
    pile = c.post("/api/cull/pile").json()
    assert pile["n"] == n and pile["added"] == n
    # export: a folder per product with a folder per colour, and the spreadsheet of every frame
    before = sorted(p.name for p in tmp_path.rglob("*"))
    ex = c.post("/api/export/cull", json={"mode": "copy"}).json()
    assert ex["total"] == n and all("/" in k for k in ex["folders"])
    d = _wait(c, "/api/export/progress")
    out = Path(d["path"])
    assert sum(len(list(x.glob("*/*.jpg"))) for x in out.iterdir() if x.is_dir()) == n
    with open(out / "client pick.csv") as fh:
        rows_csv = list(csv.DictReader(fh))
    assert len(rows_csv) == 27 and set(rows_csv[0]) == {"product", "colour", "angle", "file", "status", "reason"}
    assert {r["reason"] for r in rows_csv if r["file"] == "P0026.jpg"} == {"blown out on the product"}
    assert sum(r["status"] == "picked" for r in rows_csv) == n
    assert sorted(p.name for p in tmp_path.rglob("*")) == before            # nothing written into the shoot
    assert c.post("/api/export/cull", json={"mode": "zip"}).status_code == 400

def test_merge_and_new_find_clears(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.index import index_folder
    from photosort.server import create_app
    _shoot(tmp_path)
    index_folder(tmp_path, faces=False, workers=1)
    c = TestClient(create_app(tmp_path))
    c.post("/api/products/find", json={}); _wait(c, "/api/products/progress")
    c.post("/api/cull/run", json={}); _wait(c, "/api/cull/progress")
    L = c.get("/api/cull").json()
    ps = [p["product"] for p in L["products"] if p["product"]]
    assert len(ps) == 2
    assert c.post("/api/cull/merge", json={"product": ps[0], "into": ps[0]}).status_code == 400
    assert c.post("/api/cull/merge", json={"product": 99, "into": ps[0]}).status_code == 404
    M = c.post("/api/cull/merge", json={"product": ps[1], "into": ps[0]}).json()
    assert [p["product"] for p in M["products"] if p["product"]] == [ps[0]] and M["photos"] == 27
    assert len(c.get("/api/products").json()["products"]) == 1
    # a new find renumbers the products, so Client pick starts over
    c.post("/api/products/find", json={}); _wait(c, "/api/products/progress")
    assert c.get("/api/cull").json()["photos"] == 0
