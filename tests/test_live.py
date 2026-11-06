"""Live shoot (photosort/live.py). Real JPEGs written the way a tether app writes them. The encoder is a stand-in unless
a test says otherwise (loading SigLIP costs seconds a test); test_real_encoder_path runs the whole thing with the model."""
import csv, errno, json, os, shutil, sqlite3, subprocess, threading, time
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from photosort import db, embed, live
from photosort.config import EMBED_DIM, FACE_CLUSTER_MIN_EYE_SHARP
from photosort.server import create_app


class FakeEmbedder:
    """The picture itself, small and mean-centred: the same scene comes out the same, two different scenes far apart."""
    def encode_images(self, ims):
        out = []
        for im in ims:
            a = np.asarray(im.convert("L").resize((48, 16)), np.float32).ravel()
            a = np.resize(a - a.mean(), EMBED_DIM)
            out.append(a / (np.linalg.norm(a) or 1.0))
        return np.stack(out)


@pytest.fixture
def fake_encoder(monkeypatch):
    monkeypatch.setattr(embed, "get_embedder", lambda: FakeEmbedder())


def exif(when="2026:11:06 10:00:00", sub=None, flash=None, shutter=None, fnumber=None, iso=None, focal=None):
    ex = Image.Exif()
    ifd = ex.get_ifd(0x8769)
    ifd[0x9003] = when
    if sub is not None:
        ifd[0x9291] = str(sub)
    if flash is not None:
        ifd[0x9209] = flash
    if shutter is not None:
        ifd[0x829A] = shutter
    if fnumber is not None:
        ifd[0x829D] = fnumber
    if iso is not None:
        ifd[0x8827] = iso
    if focal is not None:
        ifd[0xA405] = focal
    ex[0x010F] = "SONY"; ex[0x0110] = "ILCE-7RM5"
    return ex


def product(size=(1600, 1067), blur=0.0, seed=1, shape="rect"):
    """A small textured product on a plain, gently lit sweep, about a twentieth of the frame. blur softens the product
    only, the way a lens focused behind it does. The sweep has no detail to lend the frame."""
    w, h = size
    yy = np.linspace(232, 214, h, dtype=np.float32)[:, None, None]
    sweep = np.broadcast_to(yy, (h, w, 3)).copy()
    pw, ph = w // 5, h // 4
    rng = np.random.default_rng(seed)
    tex = np.kron(rng.integers(0, 2, (ph // 6 + 1, pw // 6 + 1, 1)) * 150 + 40, np.ones((6, 6, 1)))[:ph, :pw]
    tex = np.repeat(tex, 3, axis=2).astype(np.uint8)
    tile = Image.fromarray(tex)
    mask = Image.new("L", (pw, ph), 0)
    d = ImageDraw.Draw(mask)
    (d.ellipse if shape == "round" else d.rectangle)((0, 0, pw - 1, ph - 1), fill=255)
    im = Image.fromarray(sweep.astype(np.uint8))
    layer, m = Image.new("RGB", (w, h)), Image.new("L", (w, h), 0)
    x, y = (w - pw) // 2, (h - ph) // 2
    layer.paste(tile, (x, y)); m.paste(mask, (x, y))
    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur)); m = m.filter(ImageFilter.GaussianBlur(blur))
    return Image.composite(layer, im, m)


def write_jpeg(path, im=None, **ex):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    (im or product()).save(path, quality=90, exif=exif(**ex))
    return path


def landed(tmp, name, im=None, **ex):
    """Write as a tether app does: a temp name first, renamed in when complete."""
    tmp = Path(tmp)
    p = write_jpeg(tmp / ("~" + name), im, **ex)
    os.replace(p, tmp / name)
    return tmp / name


def session(root, **kw):
    kw.setdefault("poll_s", 0.01); kw.setdefault("pair_window_s", 0.15)
    s = live.LiveSession(root, **kw)
    s.start()
    return s


def wait_for(cond, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def finish(s, timeout=30):
    s.stop()
    assert s.wait(timeout)


def shots_of(s):
    return list(s.snapshot()["shots"])


# ===== the watcher =====
def poller(root, **kw):
    """A session that was never started: its poll() is stepped by hand, so a test owns the clock."""
    kw.setdefault("poll_s", 0.01); kw.setdefault("pair_window_s", 1.0)
    return live.LiveSession(root, **kw)


def queued(s):
    return [s.q.queue[i] for i in range(s.q.qsize())]


def test_slowly_written_file_lands_only_when_it_stops_growing(tmp_path):
    src = write_jpeg(tmp_path / "src" / "x.jpg")
    data = src.read_bytes()
    root = tmp_path / "shoot"; root.mkdir()
    s = poller(root)
    dst = root / "DSC00001.JPG"
    t = 0.0
    with open(dst, "wb") as fh:
        for k in range(0, len(data), len(data) // 6 + 1):
            fh.write(data[k:k + len(data) // 6 + 1]); fh.flush()
            s.poll(t); t += 1
            assert queued(s) == [], "landed while still being written"
    s.poll(t); assert queued(s) == []          # the last write was seen in the loop: unchanged once
    s.poll(t + 1)                              # unchanged twice
    assert [i.rel for i in queued(s)] == ["DSC00001.JPG"]
    s.poll(t + 2)
    assert len(queued(s)) == 1, "handed over twice"


def test_ignored_files(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    for n in ("sorted_shoot.sorted", "sorted_shoot.sorted.part", "DSC1.JPG.part", ".sorted-probe-1-2", ".hidden.jpg",
              "._DSC1.JPG", "notes.txt", "clip.mp4"):
        (root / n).write_bytes(b"x" * 100)
    (root / ".photosort").mkdir()
    write_jpeg(root / ".photosort" / "inner.jpg")
    write_jpeg(root / "good.jpg")
    (root / "empty.jpg").write_bytes(b"")
    s = poller(root)
    for t in range(4):
        s.poll(float(t))
    assert [i.rel for i in queued(s)] == ["good.jpg"]


def test_raw_waits_for_its_jpeg_and_the_pair_is_one_shot(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, pair_window_s=5.0)
    (root / "DSC00010.ARW").write_bytes(b"not really raw" * 100)
    time.sleep(0.15)                           # the RAW has landed and is sitting in its window
    assert shots_of(s) == []
    landed(root, "DSC00010.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    conn = db.connect(root)
    rows = conn.execute("SELECT rel, sibling, status FROM photos").fetchall()
    assert [tuple(r) for r in rows] == [("DSC00010.JPG", "DSC00010.ARW", "ok")]


def test_jpeg_first_then_raw_is_one_shot_and_a_rescan_flags_nothing_missing(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00011.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    (root / "DSC00011.ARW").write_bytes(b"late raw" * 100)
    conn = db.connect(root)
    assert wait_for(lambda: conn.execute("SELECT sibling FROM photos").fetchone()[0] == "DSC00011.ARW")
    finish(s)
    assert len(shots_of(s)) == 1
    from photosort.walk import find_images
    db.mark_missing(conn, {f.rel for f in find_images(root)})    # what a later scan does
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE status='missing'").fetchone()[0] == 0


def test_raw_alone_works_and_a_late_jpeg_takes_over_its_row(tmp_path, fake_encoder, monkeypatch):
    # rawpy needs a real raw file, so the decoder is pointed at a JPEG: what is under test is the watcher and the row
    root = tmp_path / "shoot"; root.mkdir()
    preview = write_jpeg(tmp_path / "preview.jpg")
    from photosort import index
    real = index.load_preview
    monkeypatch.setattr(index, "load_preview", lambda p, e: real(preview if Path(p).suffix == ".ARW" else p, e))
    s = session(root, pair_window_s=0.05)
    (root / "DSC00012.ARW").write_bytes(b"raw alone" * 1000)
    assert wait_for(lambda: len(shots_of(s)) == 1)
    first = shots_of(s)[0]
    assert first["file"] == "DSC00012.ARW" and first["verdict"] in ("sharp", "soft")
    landed(root, "DSC00012.JPG")
    assert wait_for(lambda: shots_of(s)[0]["file"] == "DSC00012.JPG")
    finish(s)
    assert len(shots_of(s)) == 1 and shots_of(s)[0]["id"] == first["id"] and shots_of(s)[0]["product"] == first["product"]
    conn = db.connect(root)
    assert [tuple(r) for r in conn.execute("SELECT rel, sibling FROM photos")] == [("DSC00012.JPG", "DSC00012.ARW")]


def test_burst_of_twenty_fast_files_none_dropped_queue_reported(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    log = live.EventLog()
    s = session(root, log=log, queue_max=4)    # far smaller than the burst: the bound has to hold without losing a frame
    gate = threading.Event()
    real = live.judge
    live.judge = lambda *a: (gate.wait(10), real(*a))[1]
    try:
        for k in range(20):
            landed(root, f"DSC{k:05d}.JPG", when=f"2026:11:06 10:00:{k // 10:02d}", sub=(k % 10) * 10)
        assert wait_for(lambda: s.q.full() and len(s.backlog) > 0), "the queue never filled"
        assert s.q.qsize() <= 4
        assert s.snapshot()["behind"] is True and s.snapshot()["queue"] >= 15
    finally:
        gate.set()
        live.judge = real
    assert wait_for(lambda: len(shots_of(s)) == 20)
    finish(s)
    assert sorted(x["file"] for x in shots_of(s)) == [f"DSC{k:05d}.JPG" for k in range(20)]
    seqs = [e["seq"] for e in log.items]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    st = [e for e in log.items if e["type"] == "status"]
    assert any(e["behind"] for e in st) and not st[-1]["behind"]
    assert len({x["burst"]["id"] for x in shots_of(s)}) == 1 and shots_of(s)[0]["burst"]["n"] == 20


def test_truncated_file_is_an_error_then_recovers_when_it_finishes(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    data = write_jpeg(tmp_path / "x.jpg").read_bytes()
    log = live.EventLog()
    s = session(root, log=log)
    (root / "DSC00020.JPG").write_bytes(data[:len(data) // 2])     # the write stalled long enough to look finished
    assert wait_for(lambda: s.errors == 1)
    assert shots_of(s) == []
    (root / "DSC00020.JPG").write_bytes(data)
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    assert db.connect(root).execute("SELECT status FROM photos").fetchone()[0] == "ok"


# ===== crash safety =====
def test_restart_does_not_reprocess_and_picks_up_what_landed_while_stopped(tmp_path, fake_encoder, monkeypatch):
    from photosort import index
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00030.JPG"); landed(root, "DSC00031.JPG", im=product(seed=2))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    finish(s)
    before = {x["id"]: x["product"] for x in shots_of(s)}
    landed(root, "DSC00032.JPG", im=product(seed=3))               # landed with nobody watching
    calls = []
    real = index.process_one
    monkeypatch.setattr(index, "process_one", lambda a: (calls.append(a[1]), real(a))[1])
    s2 = session(root)
    assert wait_for(lambda: len(shots_of(s2)) == 3)
    time.sleep(0.1)
    finish(s2)
    assert calls == ["DSC00032.JPG"]
    got = {x["id"]: x["product"] for x in shots_of(s2)}
    assert {k: got[k] for k in before} == before


def test_a_crash_half_way_through_a_file_redoes_that_file(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00033.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    conn = db.connect(root)        # the row was written and the process died before the embedding and tags
    conn.execute("DELETE FROM live_tags"); conn.execute("UPDATE photos SET embed=NULL"); conn.commit()
    s2 = session(root)
    assert wait_for(lambda: len(shots_of(s2)) == 1)
    finish(s2)
    assert conn.execute("SELECT COUNT(*) FROM live_tags").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE embed IS NOT NULL").fetchone()[0] == 1


def test_active_survives_a_crash_and_not_a_clean_stop(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    s.set_active(product="SKU1")
    assert db.get_meta(db.connect(root), live.ACTIVE_KEY)
    crashed = live.LiveSession(root); conn = db.connect(root); crashed._resume(conn)
    assert crashed.active["product"] == "SKU1"
    finish(s)
    clean = live.LiveSession(root); clean._resume(conn)
    assert clean.active["product"] is None


# ===== the verdict =====
def verdict_of(tmp_path, im):
    from photosort.features import sharpness_tiles, to_gray
    im = im.copy(); im.thumbnail((1024, 1024))
    p90, mx = sharpness_tiles(to_gray(im))
    return live.judge({"n_faces": 0, "sharp_tile": p90, "sharp_max": mx}, im), (p90, mx)


def test_small_sharp_product_on_a_plain_sweep_is_sharp_and_the_blurred_one_is_soft(tmp_path):
    good, frame = verdict_of(tmp_path, product())
    assert good == {"kind": "product", "verdict": "sharp", "reason": None}
    # the whole frame's upper tiles are empty sweep, so p90 alone would have condemned this frame
    bad, _ = verdict_of(tmp_path, product(blur=8))
    assert bad == {"kind": "product", "verdict": "soft", "reason": "on the product"}


def test_the_measure_is_the_product_not_the_frame():
    im = product(size=(1024, 683)); im.thumbnail((1024, 1024))
    arr = np.asarray(im)
    region = live.product_region(arr)
    assert region is not None and 0.02 < region.mean() < 0.12
    from photosort.features import to_gray
    p90, top = live.region_sharpness(to_gray(im), region)
    assert not live.is_soft(p90, top)
    assert live.product_region(np.full((683, 1024, 3), 225, np.uint8)) is None


def test_small_product_sharp_and_blurred_through_the_whole_path(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00040.JPG", im=product())
    landed(root, "DSC00041.JPG", im=product(blur=8, seed=5), when="2026:11:06 10:05:00")
    assert wait_for(lambda: len(shots_of(s)) == 2)
    finish(s)
    by = {x["file"]: x for x in shots_of(s)}
    assert (by["DSC00040.JPG"]["verdict"], by["DSC00040.JPG"]["reason"]) == ("sharp", None)
    assert (by["DSC00041.JPG"]["verdict"], by["DSC00041.JPG"]["reason"]) == ("soft", "on the product")
    assert by["DSC00040.JPG"]["kind"] == "product"


def test_a_face_is_a_model_and_soft_says_the_eyes():
    im = product()
    assert live.judge({"n_faces": 1, "sharp_eye": FACE_CLUSTER_MIN_EYE_SHARP + 1}, im) == {"kind": "model", "verdict": "sharp", "reason": None}
    assert live.judge({"n_faces": 2, "sharp_eye": FACE_CLUSTER_MIN_EYE_SHARP - 1}, im) == {"kind": "model", "verdict": "soft", "reason": "on the eyes"}


def test_verdict_does_not_drift_with_the_shoot():
    # the same numbers mean the same thing as frame 1 and as frame 5,000: nothing relative goes in
    assert live.is_soft(5, 20) and not live.is_soft(5, 200) and not live.is_soft(30, 20)


# ===== flash =====
def test_flash_bit_handling():
    f = live.flash_state
    assert f(1) == "fired" and f(0x19) == "fired" and f(0x4F) == "fired"
    assert f(0) == "unknown" and f(0x10) == "unknown" and f(None) == "unknown" and f(-1) == "unknown"
    assert f(0, strobe=True) == "fired" and f(None, strobe=True) == "fired"
    assert f(0, no_flash=True) == "none" and f(None, no_flash=True) == "none"
    assert f(1, no_flash=True) == "fired"       # the camera's own record beats the declaration


def test_flash_read_from_the_file(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00050.JPG", flash=1)
    landed(root, "DSC00051.JPG", flash=16)
    landed(root, "DSC00052.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 3)
    finish(s)
    assert {x["file"]: x["flash"] for x in shots_of(s)} == {"DSC00050.JPG": "fired", "DSC00051.JPG": "unknown", "DSC00052.JPG": "unknown"}
    root2 = tmp_path / "strobes"; root2.mkdir()
    s = session(root2, strobe=True)
    landed(root2, "DSC00053.JPG", flash=16)
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    assert shots_of(s)[0]["flash"] == "fired"


# ===== products =====
def test_centroid_assignment_opens_a_product_and_reuses_it(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    log = live.EventLog()
    s = session(root, log=log)
    landed(root, "A1.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    landed(root, "A2.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    landed(root, "B1.JPG", im=ImageOps.invert(product(seed=1)))
    assert wait_for(lambda: len(shots_of(s)) == 3)
    finish(s)
    by = {x["file"]: x["product"] for x in shots_of(s)}
    assert by == {"A1.JPG": "product 1", "A2.JPG": "product 1", "B1.JPG": "product 2"}
    made = [e["name"] for e in log.items if e["type"] == "product_created"]
    assert made == ["product 1", "product 2"]
    assert {p["name"]: p["n"] for p in s.snapshot()["products"]} == {"product 1": 2, "product 2": 1}


def test_assignment_cut_is_the_products_cut(tmp_path):
    s = live.LiveSession(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE photos(id INTEGER PRIMARY KEY);"); live.ensure_tables(conn)
    a = np.zeros(EMBED_DIM, np.float32); a[0] = 1
    near = np.zeros(EMBED_DIM, np.float32); near[0] = 0.90; near[1] = np.sqrt(1 - 0.90 ** 2)
    far = np.zeros(EMBED_DIM, np.float32); far[0] = 0.88; far[1] = np.sqrt(1 - 0.88 ** 2)
    p1, _ = s._assign(conn, None, a)
    p2, _ = s._assign(conn, None, near)
    assert p2 is p1
    conn.execute("DELETE FROM live_products")
    s2 = live.LiveSession(tmp_path); first, _ = s2._assign(conn, None, a)
    assert s2._assign(conn, None, far)[0] is not first


def test_active_product_overrides_and_teaches_the_centroid(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "C1.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    s.set_active(product="  SKU-774\n", model="Asha")         # a barcode scanner ends with a return
    assert s.snapshot()["active"] == {"product": "SKU-774", "model": "Asha"}
    time.sleep(0.05)
    landed(root, "C2.JPG", im=product(seed=1))                # looks like product 1; the tag wins
    assert wait_for(lambda: len(shots_of(s)) == 2)
    s.set_active(product="", model="")
    time.sleep(0.05)
    landed(root, "C3.JPG", im=product(seed=7, shape="round"))
    assert wait_for(lambda: len(shots_of(s)) == 3)
    finish(s)
    by = {x["file"]: x for x in shots_of(s)}
    assert (by["C2.JPG"]["product"], by["C2.JPG"]["model"], by["C2.JPG"]["tagged"]) == ("SKU-774", "Asha", True)
    assert by["C3.JPG"]["model"] is None and by["C3.JPG"]["tagged"] is False
    conn = db.connect(root)
    assert conn.execute("SELECT l.name, t.model FROM live_tags t JOIN live_products l ON l.id=t.product WHERE t.tagged=1").fetchall()[0][:] == ("SKU-774", "Asha")


def test_a_queued_frame_keeps_what_was_active_when_it_appeared(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    s = poller(root)
    s.poll(0.0)                                  # the first look at the folder is over
    s.set_active(product="SKU-A")
    write_jpeg(root / "D1.JPG")
    s.poll(1.0)
    s.set_active(product="SKU-B")                # scanned the next product while D1 was still landing
    s.poll(2.0); s.poll(3.0)
    assert [(i.rel, i.product) for i in queued(s)] == [("D1.JPG", "SKU-A")]


def test_backlog_from_before_the_session_is_never_tagged(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    write_jpeg(root / "OLD.JPG")
    s = poller(root)
    s.active["product"] = "SKU-A"
    for t in range(4):
        s.poll(float(t))
    assert [(i.rel, i.product) for i in queued(s)] == [("OLD.JPG", None)]


# ===== bursts and brackets =====
def test_bracket_found_live_and_not_called_a_burst(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    for k, (t, ) in enumerate(((1 / 100,), (1 / 400,), (1 / 25,))):
        landed(root, f"DSC{100 + k:05d}.JPG", im=product(seed=1), when="2026:11:06 11:00:00", sub=k * 20, shutter=t, fnumber=8.0, iso=100)
        assert wait_for(lambda: len(shots_of(s)) == k + 1)
    landed(root, "DSC00200.JPG", im=product(seed=1), when="2026:11:06 12:00:00", shutter=1 / 100, fnumber=8.0, iso=100)
    assert wait_for(lambda: len(shots_of(s)) == 4)
    finish(s)
    by = {x["file"]: x for x in shots_of(s)}
    trio = [by[f"DSC{n:05d}.JPG"] for n in (100, 101, 102)]
    assert all(x["bracket"] and x["bracket"]["n"] == 3 and not x["burst"] for x in trio)
    assert [x["ev"] for x in trio] == [0.0, -2.0, 2.0]
    assert by["DSC00200.JPG"]["bracket"] is None
    # and it comes back from the database the same
    s2 = live.LiveSession(root); s2._resume(db.connect(root))
    assert s2.shots[trio[0]["id"]]["bracket"]["n"] == 3 and s2.shots[trio[1]["id"]]["ev"] == -2.0


def test_a_burst_longer_than_the_window_stays_one_burst(tmp_path, fake_encoder, monkeypatch):
    monkeypatch.setattr(live, "WINDOW", 4)
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    for k in range(9):
        landed(root, f"DSC{k:05d}.JPG", im=product(seed=1), when="2026:11:06 11:30:00", sub=k * 10)
    assert wait_for(lambda: len(shots_of(s)) == 9)
    finish(s)
    assert {x["burst"]["id"] for x in shots_of(s)} == {shots_of(s)[0]["id"]}
    assert shots_of(s)[0]["burst"]["n"] == 9


# ===== the read-only guarantee =====
def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(Path(root).rglob("*"))}


def test_the_watched_folder_is_never_written(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    for k in range(3):
        write_jpeg(root / f"DSC{k:05d}.JPG", im=product(seed=k + 1), when=f"2026:11:06 10:0{k}:00")
    (root / "DSC00000.ARW").write_bytes(b"raw" * 100)
    before = tree(root)
    os.chmod(root, 0o555)                       # a write of any kind now raises, not merely fails this test later
    for p in root.iterdir():
        os.chmod(p, 0o444)
    try:
        app = create_app()
        hub = app.state.live
        c = TestClient(app)
        assert c.post("/api/live/start", json={"folder": str(root)}).status_code == 200
        assert wait_for(lambda: hub.session.snapshot()["done"] == 3)
        assert c.post("/api/live/stop").status_code == 200
        assert hub.session.wait(30)
        dest = tmp_path / "store"
        assert c.post("/api/live/export-store", json={"dest": str(dest), "include_soft": True}).status_code == 200
        assert wait_for(lambda: not hub.export["running"])
        assert hub.export["done"] == 3
    finally:
        os.chmod(root, 0o755)
    assert tree(root) == before
    project = [e for e in hub.log.items if e["type"] == "status" and e.get("project")]
    assert project and not Path(project[-1]["project"]).resolve().is_relative_to(root.resolve())
    assert Path(project[-1]["project"]).is_file()


# ===== the store export =====
def test_export_numbering_filter_and_size_cap(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    s.set_active(product="SKU-9")
    time.sleep(0.03)
    big = product(size=(5000, 3334), seed=1)
    # landing order is the reverse of capture order
    landed(root, "DSC00003.JPG", im=big, when="2026:11:06 10:00:30")
    landed(root, "DSC00002.JPG", im=product(seed=1), when="2026:11:06 10:00:20")
    landed(root, "DSC00001.JPG", im=product(seed=1), when="2026:11:06 10:00:10")
    landed(root, "DSC00004.JPG", im=product(blur=8, seed=1), when="2026:11:06 10:00:40")
    assert wait_for(lambda: len(shots_of(s)) == 4, 60)
    finish(s)
    conn = db.connect(root)
    plan = live.store_plan(conn)
    assert [f["rel"] for f in plan["SKU-9"]] == ["DSC00001.JPG", "DSC00002.JPG", "DSC00003.JPG"]      # sharp only, capture order
    dest = tmp_path / "store"
    out = live.export_store(root, dest, plan)
    assert out == {"done": 3, "failed": 0, "total": 3, "path": str(dest)}
    names = sorted(p.name for p in (dest / "SKU-9").iterdir())
    assert names == ["SKU-9_01.jpg", "SKU-9_02.jpg", "SKU-9_03.jpg"]
    with Image.open(dest / "SKU-9" / "SKU-9_03.jpg") as im:
        assert max(im.size) == live.STORE_MAX_EDGE and im.format == "JPEG"
    # a frame inside the limit is the file itself, not a second generation
    assert (dest / "SKU-9" / "SKU-9_01.jpg").read_bytes() == (root / "DSC00001.JPG").read_bytes()
    assert sum(len(v) for v in live.store_plan(conn, include_soft=True).values()) == 4
    assert live.store_plan(conn, products=["nothing"]) == {}


def test_export_refuses_the_watched_folder(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    link = tmp_path / "sneaky"; link.symlink_to(root)
    with pytest.raises(ValueError):
        live.export_store(root, root / "out", {})
    with pytest.raises(ValueError):
        live.export_store(root, link / "out", {})
    app = create_app(root); c = TestClient(app)
    r = c.post("/api/live/export-store", json={"dest": str(root / "store")})
    assert r.status_code == 400 and "shoot folder" in r.json()["detail"]


def test_a_sku_cannot_climb_out_of_the_store_folder(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    src = write_jpeg(root / "a.jpg")
    plan = {"../../evil": [{"rel": "a.jpg", "width": 1600, "height": 1067}]}
    dest = tmp_path / "store"
    assert live.export_store(root, dest, plan)["done"] == 1
    assert all(p.resolve().is_relative_to(dest.resolve()) for p in dest.rglob("*"))


# ===== the API =====
def test_api_session_end_to_end(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); hub = app.state.live; c = TestClient(app)
    assert c.get("/api/live/state").json()["running"] is False
    assert c.post("/api/live/stop").status_code == 409
    assert c.post("/api/live/active", json={"product": "x"}).status_code == 409
    assert c.post("/api/live/start", json={"folder": str(tmp_path / "nope")}).status_code == 400
    assert c.post("/api/live/start", json={"folder": str(root), "strobe": True, "no_flash": True}).status_code == 400
    r = c.post("/api/live/start", json={"folder": str(root), "strobe": True})
    assert r.status_code == 200 and r.json()["strobe"] is True
    assert c.post("/api/live/start", json={"folder": str(root)}).status_code == 409
    assert c.post("/api/index", json={}).status_code == 409                       # the scan refuses during a session
    assert app.state.photosort["running"] is True
    seq = c.get("/api/live/state").json()["seq"]
    c.post("/api/live/active", json={"product": "SKU-1"})
    landed(root, "DSC00001.JPG")
    got, after = [], seq
    end = time.monotonic() + 20
    while time.monotonic() < end and not any(e["type"] == "shot" for e in got):
        r = c.get("/api/live/events", params={"after": after, "wait": 2}).json()
        got += r["events"]; after = r["seq"]
    shot = next(e for e in got if e["type"] == "shot")
    assert {"id", "file", "thumb", "verdict", "reason", "kind", "product", "model", "burst", "bracket", "flash", "ms"} <= set(shot)
    assert shot["flash"] == "fired" and shot["product"] == "SKU-1" and shot["ms"]["total"] > 0
    assert c.get(shot["thumb"]).status_code == 200
    assert any(e["type"] == "product_created" and e["name"] == "SKU-1" for e in got)
    assert [e["seq"] for e in got] == sorted(e["seq"] for e in got)
    # a screen opened now catches up from one call
    st = c.get("/api/live/state").json()
    assert [x["file"] for x in st["shots"]] == ["DSC00001.JPG"] and st["active"]["product"] == "SKU-1"
    # rename follows the active field, and a taken name is refused
    pid = st["products"][0]["id"]
    assert c.post("/api/live/rename-product", json={"product": pid, "name": "SKU-2"}).json()["name"] == "SKU-2"
    assert c.get("/api/live/state").json()["shots"][0]["product"] == "SKU-2"
    assert c.get("/api/live/state").json()["active"]["product"] == "SKU-2"
    assert c.post("/api/live/rename-product", json={"product": 999, "name": "x"}).status_code == 404
    assert c.post("/api/live/stop").status_code == 200
    assert hub.session.wait(30)
    assert app.state.photosort["running"] is False
    last = [e for e in hub.log.items if e["type"] == "status"][-1]
    assert last["state"] == "stopped" and last["project"] and last["recluster"]["url"] == "/api/products/find"
    assert last["recluster"]["offer"] is False         # one frame is no product shoot
    assert db.embed_model(db.connect(root))


def test_refuses_to_start_during_a_scan(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(root); c = TestClient(app)
    app.state.photosort["running"] = True
    r = c.post("/api/live/start", json={"folder": str(root)})
    assert r.status_code == 409 and "scan" in r.json()["detail"]


def test_refuses_a_shoot_read_with_another_encoder(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    db.set_meta(db.connect(root), "embed_model", "some/other")
    app = create_app(root); c = TestClient(app)
    assert c.post("/api/live/start", json={"folder": str(root)}).status_code == 409
    assert app.state.photosort["running"] is False


def test_long_poll_waits_then_wakes():
    log = live.EventLog()
    t0 = time.monotonic()
    assert log.since(0, 0.05) == {"events": [], "seq": 0, "reset": False}
    assert time.monotonic() - t0 >= 0.04
    threading.Timer(0.05, lambda: log.emit("status", state="live")).start()
    t0 = time.monotonic()
    out = log.since(0, 5)
    assert time.monotonic() - t0 < 2 and [e["type"] for e in out["events"]] == ["status"] and out["seq"] == 1


def test_a_client_that_fell_behind_or_outlived_the_server_is_told_to_reload():
    log = live.EventLog(keep=3)
    for k in range(10):
        log.emit("status", k=k)
    assert log.since(2, 0)["reset"] is True
    assert log.since(50, 0)["reset"] is True
    assert [e["seq"] for e in log.since(8, 0)["events"]] == [9, 10]
    assert [e["seq"] for e in log.since(7, 0)["events"]] == [8, 9, 10]


def test_no_dashes_in_user_facing_strings():
    src = Path(live.__file__).read_text()
    assert "—" not in src and "–" not in src
    import re
    for m in re.finditer(r'(?:HTTPException\(\d+, |message=f?|ValueError\()"([^"]*)"', src):
        assert "--" not in m.group(1)


# ===== the copy to the drive =====
@pytest.fixture
def drive(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "DRIVE_MIN_FREE", 0); monkeypatch.setattr(live, "DRIVE_KEEP_FREE", 0)
    monkeypatch.setattr(live, "COPY_RETRY_S", 0.0); monkeypatch.setattr(live, "MANIFEST_EVERY_S", 0.05)
    d = tmp_path / "SSD"; d.mkdir()
    return d


def dsession(root, d, **kw):
    kw.setdefault("drive_poll_s", 0.02)
    return session(root, drive=d, shoot="Job", **kw)


def settled(s, timeout=30):
    """Stopped, and the copier has finished everything: copies, moves, read-backs and the manifest."""
    finish(s)
    s.copier.thread.join(timeout)
    assert not s.copier.thread.is_alive()


def on_drive(d):
    job = d / "Job"
    return sorted(p.relative_to(job).as_posix() for p in job.rglob("*") if p.is_file() and p.name not in (live.MARKER, live.MANIFEST))


def copies(root):
    return {r["rel"]: dict(r) for r in db.connect(root).execute("SELECT * FROM live_copies")}


def test_a_file_lands_in_order(tmp_path, monkeypatch):
    src = write_jpeg(tmp_path / "a.jpg"); final = tmp_path / "out" / "a.jpg"; final.parent.mkdir()
    part = final.with_name("a.jpg.part")
    log = []
    full, sync, rename = live._full_sync, live._sync_dir, os.rename
    monkeypatch.setattr(live, "_full_sync", lambda fd: (log.append(("flush", part.exists(), final.exists())), full(fd))[1])
    monkeypatch.setattr(live, "_sync_dir", lambda d: (log.append(("dir", part.exists(), final.exists())), sync(d))[1])
    monkeypatch.setattr(live.os, "rename", lambda a, b: (log.append(("rename", part.exists(), final.exists())), rename(a, b))[1])
    digest = live.land_file(src, final)
    assert log == [("flush", True, False), ("rename", True, False), ("dir", False, True), ("flush", False, True)]    # the file, then its folder
    assert digest == live.file_hash(final) == live.file_hash(src)


def test_a_short_copy_leaves_nothing(tmp_path, monkeypatch):
    src = write_jpeg(tmp_path / "a.jpg"); final = tmp_path / "b.jpg"
    monkeypatch.setattr(live, "_pump", lambda fi, fo, h: fo.write(b"x") and 1)
    with pytest.raises(OSError):
        live.land_file(src, final)
    assert not final.exists() and not (tmp_path / "b.jpg.part").exists()


def test_full_sync_falls_back_when_the_file_system_refuses(tmp_path, monkeypatch):
    called = []
    def refuse(fd, op, *a):
        raise OSError(errno.ENOTSUP, "no")
    monkeypatch.setattr(live.fcntl, "fcntl", refuse)
    monkeypatch.setattr(live.os, "fsync", lambda fd: called.append(fd))
    live._full_sync(7)
    assert called == [7]


def test_a_crash_before_the_rename_leaves_no_final_file_and_retries(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    real, failed = os.rename, []
    def once(a, b):
        if str(a).endswith(".part") and not failed:
            failed.append(b)
            assert not Path(b).exists()
            raise OSError(errno.EIO, "pulled")
        return real(a, b)
    monkeypatch.setattr(live.os, "rename", once)
    s = dsession(root, drive)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    settled(s)
    assert failed and on_drive(drive) == ["products/product 1/DSC00001.JPG"]
    r = copies(root)["DSC00001.JPG"]
    assert r["state"] == "copied" and r["verified"] == 1


def test_a_killed_process_leaves_a_part_that_a_restart_clears_and_redoes(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive); s.copier.closed.set()      # the queue holds the row and nothing copies it
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    folder = drive / "Job" / "products" / "product 1"; folder.mkdir(parents=True)
    (folder / "DSC00001.JPG.part").write_bytes(b"half a fr")
    s2 = dsession(root, drive)
    settled(s2)
    assert on_drive(drive) == ["products/product 1/DSC00001.JPG"]
    assert live.file_hash(folder / "DSC00001.JPG") == live.file_hash(root / "DSC00001.JPG")


def test_a_repeated_counter_gets_a_suffix_and_the_raw_stays_paired(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive)
    s.set_active(product="SKU")
    landed(root / "x" if (root / "x").mkdir() is None else root, "DSC00001.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    (root / "y").mkdir()
    (root / "y" / "DSC00001.ARW").write_bytes(b"the second raw" * 100)
    landed(root / "y", "DSC00001.JPG", im=product(seed=2))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    settled(s)
    assert on_drive(drive) == ["products/SKU/DSC00001.JPG", "products/SKU/DSC00001_2.ARW", "products/SKU/DSC00001_2.JPG"]
    assert (drive / "Job/products/SKU/DSC00001.JPG").read_bytes() == (root / "x/DSC00001.JPG").read_bytes()
    assert (drive / "Job/products/SKU/DSC00001_2.JPG").read_bytes() == (root / "y/DSC00001.JPG").read_bytes()


def test_identical_bytes_are_skipped_and_never_rewritten(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    src = landed(root, "DSC00001.JPG")
    folder = drive / "Job" / "products" / "SKU"; folder.mkdir(parents=True)
    shutil.copyfile(src, folder / "DSC00001.JPG")         # what a crash between the rename and the db leaves
    before = (folder / "DSC00001.JPG").stat().st_mtime_ns
    src.unlink()
    s = dsession(root, drive); s.set_active(product="SKU")
    time.sleep(0.05)
    shutil.copyfile(folder / "DSC00001.JPG", root / "~t"); os.replace(root / "~t", src)
    assert wait_for(lambda: len(shots_of(s)) == 1)
    settled(s)
    assert on_drive(drive) == ["products/SKU/DSC00001.JPG"]
    assert (folder / "DSC00001.JPG").stat().st_mtime_ns == before
    assert copies(root)["DSC00001.JPG"]["state"] == "copied"


def test_the_source_is_never_touched(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    time.sleep(0.05)
    before = tree(root)
    settled(s)
    assert tree(root) == before


def test_ejected_drive_pauses_the_queue_and_a_remount_resumes_it(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    log = live.EventLog()
    s = dsession(root, drive, log=log)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    away = tmp_path / "away"
    os.rename(drive, away)
    assert wait_for(lambda: s.snapshot()["drive"]["state"] == "paused_gone")
    landed(root, "DSC00002.JPG", im=product(seed=2))
    landed(root, "DSC00003.JPG", im=product(seed=3))
    assert wait_for(lambda: len(shots_of(s)) == 3)        # the shoot does not notice
    time.sleep(0.1)
    assert not drive.exists()                              # and the mount point is never rebuilt on the Mac's own disk
    d = s.snapshot()["drive"]
    assert d["pending"] == 2 and d["failed"] == 0 and d["free_bytes"] is None
    assert {x["copy"] for x in shots_of(s)} == {"copied", "queued"}
    os.rename(away, drive)
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 3)
    assert s.snapshot()["drive"]["state"] == "ok"
    settled(s)
    assert {x["copy"] for x in shots_of(s)} == {"copied"}
    words = [e.get("message") for e in log.items if e["type"] == "status" and e.get("message")]
    assert any("not there" in w for w in words) and any("back" in w for w in words)
    assert any(e["type"] == "shot_update" and e["copy"] == "copied" for e in log.items)


def test_another_disk_at_the_same_path_is_not_the_drive(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    os.rename(drive, tmp_path / "away")
    drive.mkdir()                                          # a stale mount point, or a different card with the same name
    landed(root, "DSC00002.JPG", im=product(seed=2))
    assert wait_for(lambda: s.snapshot()["drive"]["state"] == "paused_gone")
    assert wait_for(lambda: len(shots_of(s)) == 2)
    time.sleep(0.1)
    assert list(drive.iterdir()) == []
    finish(s); s.copier.close()


def test_full_drive_pauses_and_carries_on_when_there_is_space(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    free = {"n": 10}
    monkeypatch.setattr(live, "_free_bytes", lambda p: free["n"])
    s = dsession(root, drive)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["state"] == "paused_full")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    assert on_drive(drive) == [] and s.snapshot()["drive"]["pending"] == 1 and s.snapshot()["drive"]["free_bytes"] == 10
    free["n"] = 1 << 40
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    assert s.snapshot()["drive"]["state"] == "ok"
    settled(s)


def test_enospc_half_way_through_a_file_pauses_without_counting_against_it(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    free = {"n": 1 << 40}
    monkeypatch.setattr(live, "_free_bytes", lambda p: free["n"])
    real, raised = live._pump, []
    def full(fi, fo, h):
        if not raised:
            raised.append(1)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fi, fo, h)
    monkeypatch.setattr(live, "_pump", full)
    s = dsession(root, drive)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["state"] == "paused_full")
    assert copies(root)["DSC00001.JPG"]["tries"] == 0 and on_drive(drive) == []
    free["n"] += 1 << 30
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    settled(s)
    assert on_drive(drive) == ["products/product 1/DSC00001.JPG"]


def test_a_burst_of_thirty_never_waits_for_the_drive(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    gate = threading.Event()
    real = live._pump
    monkeypatch.setattr(live, "_pump", lambda fi, fo, h: (gate.wait(60), real(fi, fo, h))[1])
    s = dsession(root, drive)
    for k in range(30):
        landed(root, f"DSC{k:05d}.JPG", im=product(size=(400, 300)), when=f"2026:11:06 10:00:{k // 10:02d}", sub=(k % 10) * 10)
    assert wait_for(lambda: len(shots_of(s)) == 30)        # the copier is stuck on its first file
    d = s.snapshot()["drive"]
    assert d["copied"] == 0 and d["pending"] == 30
    gate.set()
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 30)
    settled(s)
    assert len(on_drive(drive)) == 30


def test_a_restart_resumes_the_queue_without_reading_a_frame_twice(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    away = tmp_path / "away"
    s = dsession(root, drive)
    assert wait_for(lambda: (drive / "Job" / live.MARKER).exists())
    os.rename(drive, away)
    for k in range(3):
        landed(root, f"DSC{k:05d}.JPG", im=product(seed=k))
    assert wait_for(lambda: len(shots_of(s)) == 3)
    finish(s); s.copier.close()
    assert [r["state"] for r in copies(root).values()] == ["queued"] * 3
    os.rename(away, drive)
    from photosort import index
    monkeypatch.setattr(index, "process_one", lambda a: pytest.fail("a done frame was read again"))
    s2 = dsession(root, drive)
    assert s2.snapshot()["drive"]["pending"] == 3 and {x["copy"] for x in shots_of(s2)} == {"queued"}
    settled(s2)
    assert s2.snapshot()["drive"]["copied"] == 3 and len(on_drive(drive)) == 3


def test_a_drive_turned_on_later_picks_up_the_shoot_so_far(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    assert s.snapshot()["drive"]["state"] == "off" and shots_of(s)[0]["copy"] is None
    s2 = dsession(root, drive)
    settled(s2)
    assert on_drive(drive) == ["products/product 1/DSC00001.JPG"]


def api_session(tmp_path, d, **kw):
    root = tmp_path / "shoot"; root.mkdir(exist_ok=True)
    app = create_app(); c = TestClient(app)
    r = c.post("/api/live/start", json={"folder": str(root), "drive": str(d), "shoot": "Job", **kw})
    assert r.status_code == 200, r.text
    app.state.live.session.copier.poll_s = 0.02
    return root, app.state.live, c


def test_renaming_a_product_moves_what_is_copied_and_redirects_what_is_queued(tmp_path, fake_encoder, drive, monkeypatch):
    root, hub, c = api_session(tmp_path, drive)
    s = hub.session
    c.post("/api/live/active", json={"product": "old"})
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    gate = threading.Event()
    real = live._pump
    monkeypatch.setattr(live, "_pump", lambda fi, fo, h: (gate.wait(60), real(fi, fo, h))[1])
    landed(root, "DSC00002.JPG", im=product(seed=2))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    pid = c.get("/api/live/state").json()["products"][0]["id"]
    assert c.post("/api/live/rename-product", json={"product": pid, "name": "new/1"}).status_code == 200
    gate.set()
    assert wait_for(lambda: on_drive(drive) == ["products/new_1/DSC00001.JPG", "products/new_1/DSC00002.JPG"])
    assert c.post("/api/live/stop").status_code == 200
    assert s.wait(30); s.copier.thread.join(30)
    assert not (drive / "Job/products/old").exists()
    assert {r["dest"] for r in copies(root).values()} == {"products/new_1/DSC00001.JPG", "products/new_1/DSC00002.JPG"}
    assert all(r["verified"] == 1 for r in copies(root).values())


def test_changing_a_frames_product_or_model_moves_its_files_and_its_raw(tmp_path, fake_encoder, drive):
    root, hub, c = api_session(tmp_path, drive)
    s = hub.session
    c.post("/api/live/active", json={"product": "A"})
    (root / "DSC00001.ARW").write_bytes(b"raw" * 500)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 2)
    sid = shots_of(s)[0]["id"]
    r = c.post("/api/live/assign", json={"id": sid, "product": "B"}).json()
    assert r["product"] == "B" and r["tagged"] is True
    assert wait_for(lambda: on_drive(drive) == ["products/B/DSC00001.ARW", "products/B/DSC00001.JPG"])
    assert {p["name"]: p["n"] for p in c.get("/api/live/state").json()["products"]} == {"A": 0, "B": 1}
    c.post("/api/live/assign", json={"id": sid, "model": "Asha"})
    assert wait_for(lambda: on_drive(drive) == ["models/Asha/DSC00001.ARW", "models/Asha/DSC00001.JPG"])
    st = c.get("/api/live/state").json()
    assert st["shots"][0]["category"] == "model" and st["models"] == [{"name": "Asha", "n": 1}]
    assert c.post("/api/live/assign", json={"id": 999, "product": "B"}).status_code == 404
    assert c.post("/api/live/assign", json={"id": sid, "product": " "}).status_code == 400
    c.post("/api/live/stop"); assert s.wait(30); s.copier.thread.join(30)


def test_a_rename_after_the_stop_still_moves_the_files(tmp_path, fake_encoder, drive):
    root, hub, c = api_session(tmp_path, drive)
    s = hub.session
    c.post("/api/live/active", json={"product": "A"})
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    c.post("/api/live/stop"); assert s.wait(30); s.copier.thread.join(30)
    pid = c.get("/api/live/state").json()["products"][0]["id"]
    c.post("/api/live/rename-product", json={"product": pid, "name": "B"})
    assert wait_for(lambda: on_drive(drive) == ["products/B/DSC00001.JPG"])
    s.copier.thread.join(30)


def test_soft_raw_and_model_placement(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive)
    s.set_active(product="SKU")
    (root / "DSC00001.ARW").write_bytes(b"raw one" * 100)
    landed(root, "DSC00001.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    (root / "DSC00002.ARW").write_bytes(b"raw two" * 100)
    landed(root, "DSC00002.JPG", im=product(seed=1, blur=14))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    s.set_active(model="Asha")
    landed(root, "DSC00003.JPG", im=product(seed=3))
    landed(root, "DSC00004.JPG", im=product(seed=3, blur=14))
    assert wait_for(lambda: len(shots_of(s)) == 4)
    settled(s)
    assert {x["file"]: x["verdict"] for x in shots_of(s)}["DSC00002.JPG"] == "soft"
    assert on_drive(drive) == ["models/Asha/DSC00003.JPG", "models/Asha/soft/DSC00004.JPG",
                               "products/SKU/DSC00001.ARW", "products/SKU/DSC00001.JPG",
                               "products/SKU/soft/DSC00002.ARW", "products/SKU/soft/DSC00002.JPG"]
    by = {x["file"]: x for x in shots_of(s)}
    assert by["DSC00001.JPG"]["category"] == "product" and by["DSC00003.JPG"]["category"] == "model"
    assert s.snapshot()["models"] == [{"name": "Asha", "n": 2}]


def test_a_model_frame_with_no_name_goes_under_its_product(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive); s.set_active(product="SKU")
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    settled(s)
    conn = db.connect(root)
    conn.execute("UPDATE live_tags SET kind='model'"); conn.commit()
    pid = shots_of(s)[0]["id"]
    assert s.copier._folder(conn, pid) == Path("products/SKU")
    assert live._category("model", None) == "model" and live._category("product", None) == "product"


def test_a_name_cannot_climb_out_of_the_shoot_folder(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive); s.set_active(product="../../evil", model=None)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    settled(s)
    assert on_drive(drive) == ["products/_.._evil/DSC00001.JPG"]
    assert sorted(p.name for p in drive.iterdir()) == ["Job"]


def test_an_unreadable_frame_still_reaches_the_drive(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive)
    (root / "DSC00001.JPG").write_bytes(b"this is no picture" * 100)
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    settled(s)
    assert on_drive(drive) == ["unread/DSC00001.JPG"]


def test_a_copy_that_reads_back_wrong_is_redone_and_the_bad_one_kept(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive); s.set_active(product="SKU")
    real, spoiled = live.file_hash, []
    def spoil(p, uncached=False):
        if uncached and not spoiled:
            spoiled.append(1)
            with open(p, "r+b") as f:
                f.write(b"\0\0\0\0")
        return real(p, uncached)
    monkeypatch.setattr(live, "file_hash", spoil)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    settled(s)
    good = drive / "Job/products/SKU/DSC00001.JPG"
    assert good.read_bytes() == (root / "DSC00001.JPG").read_bytes() and copies(root)["DSC00001.JPG"]["verified"] == 1
    assert on_drive(drive) == ["products/SKU/DSC00001.JPG", "products/SKU/DSC00001.JPG.bad1"]


def test_manifest_lists_every_copy_and_follows_a_move(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = dsession(root, drive); s.set_active(product="=cmd", model=None)
    (root / "DSC00001.ARW").write_bytes(b"raw" * 200)
    landed(root, "DSC00001.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    s.set_active(product="SKU", model="Asha")
    landed(root, "DSC00002.JPG", im=product(seed=1, blur=14), iso=100)
    assert wait_for(lambda: len(shots_of(s)) == 2)
    settled(s)
    rows = list(csv.DictReader(open(drive / "Job/manifest.csv", newline="")))
    assert [list(rows[0])][0] == ["file", "product", "model", "verdict", "alerts", "hash"]
    by = {r["file"]: r for r in rows}
    assert set(by) == {"products/=cmd/DSC00001.JPG", "products/=cmd/DSC00001.ARW", "models/Asha/soft/DSC00002.JPG"}
    jpg = by["products/=cmd/DSC00001.JPG"]
    assert jpg["product"] == "'=cmd" and jpg["verdict"] == "sharp" and jpg["hash"] == live.file_hash(root / "DSC00001.JPG")
    assert by["models/Asha/soft/DSC00002.JPG"]["model"] == "Asha" and by["models/Asha/soft/DSC00002.JPG"]["verdict"] == "soft"
    assert not list((drive / "Job").glob("*.part"))


def test_preflight_refuses_inside_the_watched_folder_and_a_bad_drive(tmp_path, fake_encoder, drive, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    (root / "sub").mkdir()
    c = TestClient(create_app())
    start = lambda **kw: c.post("/api/live/start", json={"folder": str(root), **kw})
    for d, shoot in ((root, "Job"), (root / "sub", "Job"), (tmp_path, "shoot")):
        r = start(drive=str(d), shoot=shoot)
        assert r.status_code == 400 and "inside the folder" in r.json()["detail"], d
    if (tmp_path / "SHOOT").exists():                      # a case-insensitive Mac disk: the same folder by another spelling
        assert start(drive=str(tmp_path / "SHOOT"), shoot="Job").status_code == 400
    assert start(drive=str(tmp_path / "nope"), shoot="Job").status_code == 400
    assert start(drive="relative/path", shoot="Job").status_code == 400
    monkeypatch.setattr(live.bundle, "probe_writable", lambda d: "Read-only file system")
    assert "cannot be written" in start(drive=str(drive), shoot="Job").json()["detail"]
    monkeypatch.undo()
    monkeypatch.setattr(live, "DRIVE_MIN_FREE", 1 << 60)
    assert "free" in start(drive=str(drive), shoot="Job").json()["detail"]
    assert list(root.rglob("*.part")) == [] and c.get("/api/live/state").json()["running"] is False


def test_exfat_warns_and_never_blocks(tmp_path, fake_encoder, drive, monkeypatch):
    monkeypatch.setattr(live, "fs_type", lambda p: "exfat")
    root, hub, c = api_session(tmp_path, drive)
    d = c.get("/api/live/state").json()["drive"]
    assert d["fs"] == "exfat" and "exFAT" in d["warning"] and d["path"] == str(drive) and d["name"] == "SSD"
    assert set(d) == {"path", "dest", "name", "fs", "state", "pending", "copied", "failed", "free_bytes", "warning"}
    c.post("/api/live/stop"); assert hub.session.wait(30)


def test_drive_off_shape_and_a_restart_on_the_remembered_drive(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); c = TestClient(app); hub = app.state.live
    assert c.get("/api/live/state").json()["drive"]["state"] == "off"
    assert c.post("/api/live/start", json={"folder": str(root), "drive": str(drive), "shoot": "Job"}).json()["drive"]["path"] == str(drive)
    c.post("/api/live/stop"); assert hub.session.wait(30)
    assert c.post("/api/live/start", json={"folder": str(root)}).json()["drive"]["path"] == str(drive)
    c.post("/api/live/stop"); assert hub.session.wait(30)
    assert c.post("/api/live/start", json={"folder": str(root), "drive": None}).json()["drive"]["state"] == "off"
    c.post("/api/live/stop"); assert hub.session.wait(30)
    assert c.post("/api/live/start", json={"folder": str(root)}).json()["drive"]["state"] == "off"
    c.post("/api/live/stop"); assert hub.session.wait(30)


def test_volumes_lists_the_disks_and_not_the_mac(tmp_path, monkeypatch):
    v = tmp_path / "Volumes"; v.mkdir()
    (v / "SSD").mkdir(); (v / ".hidden").mkdir(); (v / "Macintosh HD").symlink_to("/")
    monkeypatch.setattr(live, "VOLUMES", v)
    monkeypatch.setattr(live, "_mounts", lambda: {str(v / "SSD"): "exfat"})
    got = TestClient(create_app()).get("/api/live/volumes").json()["volumes"]
    assert [(x["name"], x["path"], x["fs"]) for x in got] == [("SSD", str(v / "SSD"), "exfat")] and got[0]["free"] > 0


def test_mount_output_is_read():
    class R: stdout = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only)\n/dev/disk4s1 on /Volumes/My SSD (exfat, local, nodev)\nmap auto_home on /System/Volumes/Data/home (autofs, automounted)\n"
    import unittest.mock as m
    with m.patch.object(live.subprocess, "run", lambda *a, **k: R):
        assert live._mounts()["/Volumes/My SSD"] == "exfat" and live._mounts()["/"] == "apfs"


def test_pick_returns_a_path_and_leaves_the_open_shoot_alone(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(live, "pick_folder", lambda p: (prompts.append(p), str(tmp_path))[1])
    app = create_app(); c = TestClient(app)
    before = app.state.photosort["root"]
    r = c.post("/api/live/pick", json={"purpose": "drive"})
    assert r.json() == {"purpose": "drive", "path": str(tmp_path)} and app.state.photosort["root"] == before
    assert prompts == [live.PICK_PROMPTS["drive"]]
    assert c.post("/api/live/pick", json={"purpose": "nonsense"}).status_code == 400
    monkeypatch.setattr(live, "pick_folder", lambda p: None)
    assert c.post("/api/live/pick", json={"purpose": "watch"}).json()["path"] is None


def test_pick_folder_reads_the_picker(monkeypatch):
    class R: returncode, stdout = 0, "/Volumes/SSD/\n"
    seen = []
    monkeypatch.setattr(live.subprocess, "run", lambda cmd, **k: (seen.append(cmd), R)[1])
    assert live.pick_folder("Pick") == "/Volumes/SSD/" and seen[0][0] == "osascript"
    R.returncode, R.stdout = 1, ""
    assert live.pick_folder("Pick") is None


def test_an_old_shoot_gets_the_new_columns(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    conn = db.connect(root)
    conn.execute("CREATE TABLE live_tags(photo_id INTEGER PRIMARY KEY, n INTEGER NOT NULL, product INTEGER, model TEXT, tagged INTEGER NOT NULL DEFAULT 0, "
                 "verdict TEXT, reason TEXT, kind TEXT, flash TEXT, burst INTEGER, bracket INTEGER, ev REAL, ms REAL, grey BLOB)")
    live.ensure_tables(conn); live.ensure_tables(conn)
    assert {"alerts", "level", "hidden"} <= {r[1] for r in conn.execute("PRAGMA table_info(live_tags)")}


# ===== light alerts =====
def frame(product_id=1, level=0.5, t=1 / 200, f=8.0, iso=100):
    return {"level": level, "exposure_s": t, "fnumber": f, "iso": iso, "product": product_id}


def kinds(cur, recent, strobe=False, clip=None):
    return [a["kind"] for a in live.light_alerts(cur, recent, strobe, clip)]


def test_flash_miss_fires_only_in_strobe_on_unchanged_settings():
    steady = [frame() for _ in range(5)]
    miss = frame(level=0.5 / 2 ** 2)
    out = live.light_alerts(miss, steady, True)
    assert out == [{"kind": "flash_miss", "text": "Flash may not have fired"}]
    assert kinds(frame(level=0.5 / 2 ** 1.2), steady, True) == ["dark"]
    assert kinds(frame(level=0.5 / 2 ** 0.5), steady, True) == []
    assert kinds(frame(level=0.1, t=1 / 100), steady, True) == []       # a settings change explains it
    assert kinds(frame(level=0.1, f=11.0), steady, True) == []
    assert kinds(miss, steady[:2], True) == []                           # too little to go on


def test_dark_fires_on_unchanged_settings_and_stays_silent_when_unsure():
    steady = [frame() for _ in range(5)]
    assert live.light_alerts(frame(level=0.2), steady) == [{"kind": "dark", "text": "Darker than the last shots"}]
    assert kinds(frame(level=0.4), steady) == []
    assert kinds(frame(level=0.2, product_id=2), steady) == []          # a black shoe after a white one
    assert kinds(frame(level=0.001), [frame(level=0.01) for _ in range(5)]) == []     # no level to read on black
    assert kinds(dict(frame(level=0.2), iso=None), [dict(frame(), iso=None) for _ in range(5)]) == []


def test_a_misfire_stops_alarming_once_the_light_really_changed():
    recent = deque_of(frame() for _ in range(9))
    fired = []
    for _ in range(9):
        cur = frame(level=0.1)
        fired.append(kinds(cur, recent, True))
        recent.append(cur)
    assert fired[0] == ["flash_miss"] and fired[-1] == []


def deque_of(frames):
    from collections import deque
    return deque(frames, maxlen=live.ALERT_WINDOW)


def test_iso_jump():
    assert live.light_alerts(frame(iso=400), [frame(iso=100)]) == [{"kind": "iso", "text": "ISO jumped to 400"}]
    assert kinds(frame(iso=200), [frame(iso=100)]) == ["iso"]
    assert kinds(frame(iso=160), [frame(iso=100)]) == []
    assert kinds(frame(iso=100), []) == []
    assert kinds(frame(iso=None), [frame(iso=100)]) == []


def test_shutter_rule_and_strobe_switches_it_off():
    slow = dict(frame(t=1 / 30), focal_mm=50.0)
    assert live.light_alerts(slow, []) == [{"kind": "shutter", "text": "Shutter may be too slow to hand hold"}]
    assert kinds(slow, [], True) == []
    assert kinds(dict(frame(t=1 / 100), focal_mm=50.0), []) == []       # exactly the rule
    assert kinds(dict(frame(t=1 / 90), focal_mm=50.0), []) == ["shutter"]
    assert kinds(dict(frame(t=1 / 30), focal_mm=None), []) == []


def backdrop(value, size=(1024, 683)):
    return np.full((size[1], size[0], 3), value, np.uint8)


def box(rgb, value, frac=0.3, texture=False):
    h, w, _ = rgb.shape
    y0, y1, x0, x1 = int(h * (0.5 - frac / 2)), int(h * (0.5 + frac / 2)), int(w * (0.5 - frac / 2)), int(w * (0.5 + frac / 2))
    rgb[y0:y1, x0:x1] = value
    return rgb


def clip_of(rgb):
    return live.clipped_share(rgb, live.product_region(rgb))


def test_clipped_fires_on_a_blown_product():
    rgb = box(backdrop(110), 255)
    assert clip_of(rgb) > live.CLIP_SHARE
    assert live.light_alerts(frame(), [], False, clip_of(rgb)) == [{"kind": "clipped", "text": "Blown out on the product"}]


def test_clipped_ignores_a_blown_backdrop_around_a_dark_product():
    assert clip_of(box(backdrop(255), 60)) == 0.0        # the mask runs onto the backdrop; the rim is taken off first


def test_clipped_is_silent_on_white_on_white():
    assert live.product_region(box(backdrop(225), 240)) is None
    assert clip_of(box(backdrop(225), 240)) is None
    assert not clip_of(box(backdrop(255), 250)) and not clip_of(backdrop(255))
    # a white product whose only edge is a soft shadow, on a blown sweep
    rgb = backdrop(255)
    h, w, _ = rgb.shape
    rgb = np.ascontiguousarray(rgb)
    import cv2
    cv2.circle(rgb, (w // 2, h // 2), 150, (205, 205, 205), 18)
    assert (clip_of(rgb) or 0.0) < live.CLIP_SHARE


def test_clipped_never_falls_back_to_the_whole_frame():
    rgb = backdrop(255)
    assert live.clipped_share(rgb, None) is None


def test_clipped_stays_silent_on_a_small_lit_product_on_black():
    assert not clip_of(box(backdrop(4), 200, frac=0.2))


def run_of(root, s, ims, **ex):
    n0 = len(shots_of(s))
    for k, im in enumerate(ims):
        landed(root, f"DSC{n0 + k:05d}.JPG", im=im, **ex)
        assert wait_for(lambda: len(shots_of(s)) == n0 + k + 1)
    return shots_of(s)


def test_a_steady_white_on_white_run_raises_nothing(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, strobe=True); s.set_active(product="SKU")
    ims = []
    for dx in range(-40, 40, 10):                         # the product moves about, the light does not
        a = backdrop(240).copy(); a[250:430, 400 + dx:620 + dx] = 252
        ims.append(Image.fromarray(a))
    shots = run_of(root, s, ims, shutter=1 / 125, fnumber=8.0, iso=100, focal=50)
    finish(s)
    assert [x["alerts"] for x in shots] == [[]] * len(ims)


def test_a_steady_black_backdrop_run_raises_nothing_and_a_misfire_still_does(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, strobe=True); s.set_active(product="SKU")
    ims = []
    for k in range(6):                                    # angles of a lit product, a dim frame around it
        ims.append(Image.fromarray(box(backdrop(6), 150 + 6 * k, frac=0.3 + 0.01 * k)))
    shots = run_of(root, s, ims, shutter=1 / 125, fnumber=8.0, iso=100)
    assert [x["alerts"] for x in shots] == [[]] * 6
    shots = run_of(root, s, [Image.fromarray(box(backdrop(6), 30))], shutter=1 / 125, fnumber=8.0, iso=100)
    finish(s)
    assert [a["kind"] for a in shots[-1]["alerts"]] == ["flash_miss"]


def test_alerts_come_from_the_file_and_survive_a_restart(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root); s.set_active(product="SKU")
    run_of(root, s, [product(seed=1)] * 3, shutter=1 / 200, fnumber=8.0, iso=100, focal=35)
    dim = Image.fromarray((np.asarray(product(seed=1), np.float32) * 0.55).astype(np.uint8))
    run_of(root, s, [dim], shutter=1 / 200, fnumber=8.0, iso=100, focal=35)
    run_of(root, s, [product(seed=1)], shutter=1 / 200, fnumber=8.0, iso=800, focal=35)
    run_of(root, s, [product(seed=1)], shutter=1 / 10, fnumber=8.0, iso=800, focal=35)
    finish(s)
    got = [[a["kind"] for a in x["alerts"]] for x in shots_of(s)]
    assert got == [[], [], [], ["dark"], ["iso"], ["shutter"]]
    s2 = live.LiveSession(root); s2._resume(db.connect(root))
    assert [[a["kind"] for a in x["alerts"]] for x in s2.shots.values()] == got
    assert s2.recent[-1]["iso"] == 800 and len(s2.recent) == 6


# ===== client control, the 100% view, the UI =====
def test_hide_is_stored_and_sent(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); c = TestClient(app); hub = app.state.live
    assert c.post("/api/live/hide", json={"id": 1, "hidden": True}).status_code == 409
    c.post("/api/live/start", json={"folder": str(root)})
    landed(root, "DSC00001.JPG")
    s = hub.session
    assert wait_for(lambda: len(shots_of(s)) == 1)
    sid = shots_of(s)[0]["id"]
    assert shots_of(s)[0]["hidden"] is False
    assert c.post("/api/live/hide", json={"id": sid, "hidden": True}).json()["hidden"] is True
    ev = [e for e in hub.log.items if e["type"] == "shot_update"][-1]
    assert ev["id"] == sid and ev["hidden"] is True
    assert c.get("/api/live/state").json()["shots"][0]["hidden"] is True
    assert c.post("/api/live/hide", json={"id": 999, "hidden": True}).status_code == 404
    c.post("/api/live/stop"); assert s.wait(30)
    s2 = live.LiveSession(root); s2._resume(db.connect(root))
    assert s2.shots[sid]["hidden"] is True


def test_original_is_the_jpeg_itself_or_the_raws_whole_preview(tmp_path, fake_encoder, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    preview = write_jpeg(tmp_path / "preview.jpg", product(size=(3000, 2000)))
    from photosort import index
    real = index.load_preview
    monkeypatch.setattr(index, "load_preview", lambda p, e: real(preview if Path(p).suffix == ".ARW" else p, e))
    monkeypatch.setattr(live, "load_preview", lambda p, e: real(preview if Path(p).suffix == ".ARW" else p, e))
    app = create_app(); c = TestClient(app); hub = app.state.live
    c.post("/api/live/start", json={"folder": str(root)})
    s = hub.session
    src = landed(root, "DSC00001.JPG")
    (root / "DSC00002.ARW").write_bytes(b"raw alone" * 1000)
    assert wait_for(lambda: len(shots_of(s)) == 2, 10)
    ids = {x["file"]: x["id"] for x in shots_of(s)}
    r = c.get(f"/api/live/original/{ids['DSC00001.JPG']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == src.read_bytes()
    r = c.get(f"/api/live/original/{ids['DSC00002.ARW']}")
    import io
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (3000, 2000)
    assert c.get("/api/live/original/999").status_code == 404
    c.post("/api/live/stop"); assert s.wait(30)


def test_ui_files_are_never_kept_stale():
    r = TestClient(create_app()).get("/ui/live.html")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


# ===== faces and products off =====
def test_faces_off_skips_detection_and_judges_the_product(tmp_path, fake_encoder, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    from photosort import index
    seen, real = [], index.process_one
    monkeypatch.setattr(index, "process_one", lambda a: (seen.append(a[2]), real(a))[1])
    monkeypatch.setattr(index, "_face_engine", lambda: pytest.fail("the face engine was loaded"))
    s = session(root, faces=False)
    landed(root, "DSC00001.JPG")
    landed(root, "DSC00002.JPG", im=product(seed=1, blur=14))
    assert wait_for(lambda: len(shots_of(s)) == 2)
    finish(s)
    assert seen == [False, False] and s.errors == 0
    by = {x["file"]: x for x in shots_of(s)}
    assert {x["kind"] for x in by.values()} == {"product"} and {x["category"] for x in by.values()} == {"product"}
    assert by["DSC00001.JPG"]["verdict"] == "sharp"
    assert by["DSC00002.JPG"]["verdict"] == "soft" and by["DSC00002.JPG"]["reason"] == "on the product"
    assert s.snapshot()["faces"] is False and s.snapshot()["products_on"] is True


def test_products_off_opens_nothing_and_fills_one_pile(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    log = live.EventLog()
    s = dsession(root, drive, products=False, log=log)
    landed(root, "DSC00001.JPG", im=product(seed=1))
    landed(root, "DSC00002.JPG", im=product(seed=2, shape="round"))
    landed(root, "DSC00003.JPG", im=product(seed=3, blur=14))
    assert wait_for(lambda: len(shots_of(s)) == 3)
    s.set_active(product="SKU")
    landed(root, "DSC00004.JPG", im=product(seed=4))
    assert wait_for(lambda: len(shots_of(s)) == 4)
    settled(s)
    st = s.snapshot()
    assert st["products_on"] is False and st["faces"] is True
    assert {p["name"]: p["n"] for p in st["products"]} == {"Unsorted": 3, "SKU": 1}
    assert not [e for e in log.items if e["type"] == "product_created" and e["name"].startswith("product ")]
    assert on_drive(drive) == ["products/SKU/DSC00004.JPG", "unsorted/DSC00001.JPG", "unsorted/DSC00002.JPG", "unsorted/soft/DSC00003.JPG"]
    assert db.connect(root).execute("SELECT COUNT(*) FROM live_tags WHERE grey IS NOT NULL").fetchone()[0] == 0


def test_faces_and_products_through_the_api(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); c = TestClient(app); hub = app.state.live
    st = c.get("/api/live/state").json()
    assert st["faces"] is True and st["products_on"] is True and st["products"] == []
    r = c.post("/api/live/start", json={"folder": str(root), "faces": False, "products": False}).json()
    assert r["faces"] is False and r["products_on"] is False
    c.post("/api/live/stop"); assert hub.session.wait(30)


def test_work_skipped_on_the_set_is_picked_up_after_the_shoot(tmp_path, fake_encoder, monkeypatch):
    from photosort import index, products
    from photosort.config import SFACE_PATH, YUNET_PATH
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, faces=False, products=False)
    for k, seed in enumerate((1, 1, 2, 2)):
        landed(root, f"DSC{k:05d}.JPG", im=product(seed=seed))
    assert wait_for(lambda: len(shots_of(s)) == 4)
    finish(s)
    conn = db.connect(root)
    assert db.photos_without_faces(conn) == {f"DSC{k:05d}.JPG" for k in range(4)}
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE embed IS NOT NULL").fetchone()[0] == 4
    # Find products, as POST /api/products/find calls it
    class Named(FakeEmbedder):
        def encode_text(self, words):
            return np.eye(len(words), EMBED_DIM, dtype=np.float32)
    monkeypatch.setattr(embed, "get_embedder", lambda: Named())
    got = products.find_products(root)
    assert got["photos"] == 4 and got["products"] >= 1
    assert conn.execute("SELECT COUNT(DISTINCT photo_id) FROM products").fetchone()[0] == 4
    # Detect faces: the scan's faces pass, on the stored thumbs, decoding nothing again
    if YUNET_PATH.exists() and SFACE_PATH.exists():
        out = index.index_folder(root, faces=True, workers=1, embed=False)
        assert out["faced"] == 4 and out["indexed"] == 0 and out["errors"] == 0
        assert db.photos_without_faces(conn) == set()


# ===== the real path =====
@pytest.mark.skipif(not (Path.home() / ".cache/huggingface/hub/models--timm--ViT-B-16-SigLIP").exists(), reason="SigLIP weights not on this Mac")
def test_real_encoder_path(tmp_path):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, poll_s=0.05)
    assert wait_for(lambda: s.state == "live", 120)
    landed(root, "R1.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 1, 60)
    landed(root, "R2.JPG", im=product(seed=1))
    assert wait_for(lambda: len(shots_of(s)) == 2, 60)
    finish(s)
    a, b = shots_of(s)
    assert a["verdict"] == "sharp" and a["product"] == b["product"] == "product 1"
    conn = db.connect(root)
    r = conn.execute("SELECT length(embed), length(t.grey) FROM photos p JOIN live_tags t ON t.photo_id=p.id").fetchall()
    assert all(x[0] == db.EMBED_BYTES and x[1] == EMBED_DIM * 2 for x in r)


@pytest.mark.skipif(not os.environ.get("PHOTOSORT_LIVE_BENCH"), reason="set PHOTOSORT_LIVE_BENCH=1 to time a 61 MP frame")
def test_time_a_61mp_jpeg(tmp_path):
    """Prints ms per shot on a 9504 x 6336 JPEG through the real worker."""
    rng = np.random.default_rng(0)
    h, w = 6336, 9504
    grad = np.linspace(60, 220, w, dtype=np.float32)[None, :, None]
    noise = np.kron(rng.integers(0, 14, (h // 3 + 1, w // 3, 3)).astype(np.float32), np.ones((3, 3, 1), np.float32))[:h]    # about the entropy of a real frame
    base = Image.fromarray((grad + noise).clip(0, 255).astype(np.uint8))
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, poll_s=0.05)
    assert wait_for(lambda: s.state == "live", 120)
    sizes = []
    for k in range(5):                 # one file on the disk at a time: a frame this size is tens of MB
        base.save(tmp_path / "t.jpg", quality=92, exif=exif(when=f"2026:11:06 10:{k:02d}:00"))
        sizes.append((tmp_path / "t.jpg").stat().st_size)
        os.replace(tmp_path / "t.jpg", root / f"DSC{k:05d}.JPG")
        assert wait_for(lambda: len(shots_of(s)) == k + 1, 60)
        (root / f"DSC{k:05d}.JPG").unlink()
    s.stop(); assert s.wait(120)
    ms = [e["ms"] for e in s.log.items if e["type"] == "shot"]
    print("\n61MP", round(sizes[0] / 1e6, 1), "MB", json.dumps(ms))


# ===== a new shoot in a folder that has photos =====
def test_packages_are_never_walked(tmp_path):
    root = tmp_path / "Pictures"; root.mkdir()
    write_jpeg(root / "Photos Library.photoslibrary" / "originals" / "a.jpg")
    write_jpeg(root / "Job.cocatalog" / "Originals" / "b.jpg")
    write_jpeg(root / "Lightroom" / "Previews.lrdata" / "c.jpg")
    write_jpeg(root / "2026.11.06" / "Capture" / "good.jpg")     # a dot in a name does not make a package
    assert list(live.photo_files(root)) == ["2026.11.06/Capture/good.jpg"]
    assert live.count_photos(root) == (1, True)


def test_a_raw_and_its_jpeg_count_as_one_photo(tmp_path):
    write_jpeg(tmp_path / "A.JPG")
    (tmp_path / "A.ARW").write_bytes(b"raw" * 100)
    write_jpeg(tmp_path / "B.JPG")
    assert live.count_photos(tmp_path) == (2, True)


def test_a_count_out_of_time_says_so(tmp_path, monkeypatch):
    write_jpeg(tmp_path / "a" / "A.JPG"); write_jpeg(tmp_path / "b" / "B.JPG")
    monkeypatch.setattr(live, "COUNT_MAX_S", -1.0)
    assert live.count_photos(tmp_path)[1] is False


def test_only_new_shots_leaves_what_is_there_alone(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    landed(root, "OLD1.JPG"); landed(root, "OLD2.JPG", im=product(seed=2))
    before = tree(root)
    s = dsession(root, drive, mode=live.NEW_ONLY)
    s.set_active(product="SKU")
    time.sleep(0.1)
    assert shots_of(s) == []
    landed(root, "NEW1.JPG", im=product(seed=3))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    settled(s)
    assert [x["file"] for x in shots_of(s)] == ["NEW1.JPG"] and shots_of(s)[0]["product"] == "SKU"     # the first shot takes the active product
    assert on_drive(drive) == ["products/SKU/NEW1.JPG"]
    assert {k: v for k, v in tree(root).items() if k != "NEW1.JPG"} == before


def test_a_file_written_over_after_the_start_is_a_new_shot(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    landed(root, "A.JPG")
    s = session(root, mode=live.NEW_ONLY)
    time.sleep(0.05)
    landed(root, "A.JPG", im=product(seed=9))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)


def test_a_resume_of_an_only_new_shoot_still_leaves_them_alone(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    landed(root, "OLD.JPG")
    s = session(root, mode=live.NEW_ONLY)
    landed(root, "NEW1.JPG", im=product(seed=2))
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    landed(root, "NEW2.JPG", im=product(seed=3))       # landed while stopped: a resume takes it
    s2 = session(root, mode=live.RESUME)
    assert wait_for(lambda: len(shots_of(s2)) == 2)
    time.sleep(0.1)
    finish(s2)
    assert sorted(x["file"] for x in shots_of(s2)) == ["NEW1.JPG", "NEW2.JPG"]


def test_a_new_shoot_in_a_used_folder_starts_clean(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, drive=drive, shoot="First", drive_poll_s=0.02, mode=live.NEW_ONLY)
    s.set_active(product="SKU-OLD")
    time.sleep(0.05)
    landed(root, "A.JPG")
    assert wait_for(lambda: s.snapshot()["drive"]["copied"] == 1)
    settled(s)
    s2 = session(root, drive=drive, shoot="Second", drive_poll_s=0.02, mode=live.NEW_ONLY)
    time.sleep(0.1)
    snap = s2.snapshot()
    assert snap["shots"] == [] and snap["products"] == [] and snap["drive"]["pending"] == 0
    landed(root, "B.JPG", im=product(seed=2))
    assert wait_for(lambda: len(shots_of(s2)) == 1)
    settled(s2)
    assert [x["file"] for x in shots_of(s2)] == ["B.JPG"] and shots_of(s2)[0]["product"] == "product 1"
    assert not (drive / "Second" / "products" / "SKU-OLD").exists()
    assert sorted(p.name for p in (drive / "Second").rglob("*.JPG")) == ["B.JPG"]      # nothing old came along under the new name
    assert (drive / "First" / "products" / "SKU-OLD" / "A.JPG").is_file()              # and the old shoot is as it was
    conn = db.connect(root)
    assert conn.execute("SELECT COUNT(*) FROM live_tags_prev").fetchone()[0] == 1
    assert list(live.store_plan(conn)) == ["product 1"]                                # Save store copies is this shoot's
    s3 = session(root, mode=live.NEW_ONLY)                                             # a third: the one kept back gives way
    landed(root, "C.JPG", im=product(seed=3))
    assert wait_for(lambda: len(shots_of(s3)) == 1)
    finish(s3)
    assert [r[0] for r in conn.execute("SELECT p.rel FROM live_tags_prev t JOIN photos p ON p.id=t.photo_id")] == ["B.JPG"]
    assert conn.execute("SELECT COUNT(*) FROM live_products_prev").fetchone()[0] == 1


def test_taking_everything_brings_in_photos_a_scan_or_an_old_shoot_already_read(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root, mode=live.NEW_ONLY)
    landed(root, "A.JPG")
    assert wait_for(lambda: len(shots_of(s)) == 1)
    finish(s)
    s2 = session(root, mode=live.EVERYTHING)
    assert wait_for(lambda: len(shots_of(s2)) == 1)
    finish(s2)
    assert shots_of(s2)[0]["file"] == "A.JPG" and db.connect(root).execute("SELECT COUNT(*) FROM live_tags").fetchone()[0] == 1


def test_resume_keeps_the_shoot_and_the_new_shoot_does_not(tmp_path, fake_encoder, drive):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); hub = app.state.live; c = TestClient(app)
    assert c.post("/api/live/start", json={"folder": str(root), "mode": "nonsense"}).status_code == 400
    r = c.post("/api/live/start", json={"folder": str(root), "drive": str(drive), "shoot": "Job", "mode": "new_only", "strobe": True, "faces": False}).json()
    assert r["shoot"] == "Job"
    hub.session.copier.poll_s = 0.02
    landed(root, "A.JPG")
    assert wait_for(lambda: hub.session.snapshot()["drive"]["copied"] == 1)
    c.post("/api/live/stop"); assert hub.session.wait(30); hub.session.copier.thread.join(30)
    r = c.post("/api/live/start", json={"folder": str(root), "mode": "resume"}).json()
    assert [x["file"] for x in r["shots"]] == ["A.JPG"] and r["shoot"] == "Job" and r["drive"]["path"] == str(drive)
    assert r["strobe"] is True and r["faces"] is False
    c.post("/api/live/stop"); assert hub.session.wait(30); hub.session.copier.thread.join(30)
    r = c.post("/api/live/start", json={"folder": str(root), "drive": str(drive), "shoot": "Job", "mode": "new_only"}).json()
    assert r["shots"] == [] and r["shoot"] == "Job 2" and r["drive"]["dest"] == str(drive / "Job 2")      # never the old shoot's folder and manifest
    c.post("/api/live/stop"); assert hub.session.wait(30)


def test_each_new_shoot_keeps_its_own_project_file(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    app = create_app(); hub = app.state.live; c = TestClient(app)
    app.state.photosort["export_base"] = str(tmp_path / "out")
    made = []
    for name, mode in (("One", "new_only"), ("Two", "new_only"), ("Two", "resume")):
        c.post("/api/live/start", json={"folder": str(root), "shoot": name, "mode": mode, "drive": None})
        c.post("/api/live/stop"); assert hub.session.wait(30)
        made.append(c.get("/api/live/state").json()["project"])
    assert [Path(p).name for p in made] == ["sorted_One.sorted", "sorted_Two.sorted", "sorted_Two.sorted"]
    assert Path(made[0]).is_file() and Path(made[1]).is_file()
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".sorted-save-")]


def test_ready_says_what_the_folder_holds_without_making_an_index(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    landed(root, "A.JPG")
    from photosort.config import app_home, shoot_slug
    app = create_app(); hub = app.state.live; c = TestClient(app)
    get = lambda p: c.get("/api/live/ready", params={"folder": str(p)}).json()
    assert get(tmp_path / "nope")["exists"] is False
    r = get(root)
    assert (r["exists"], r["photos"], r["whole"], r["last"], r["busy"]) == (True, 1, True, None, None)
    assert not (app_home() / shoot_slug(root)).exists()
    app.state.photosort["running"] = True
    assert "scanning" in c.get("/api/live/ready").json()["busy"]
    app.state.photosort["running"] = False
    c.post("/api/live/start", json={"folder": str(root), "shoot": "Job", "mode": "everything"})
    assert wait_for(lambda: hub.session.snapshot()["done"] == 1)
    assert "running" in get(root)["busy"]
    c.post("/api/live/stop"); assert hub.session.wait(30)
    assert get(root)["last"] == {"shoot": "Job", "shots": 1, "waiting": 0}


def vol(path, free=2 << 30, writable=True, card=False):
    return {"name": Path(path).name, "path": str(path), "free": free, "fs": "apfs", "writable": writable, "card": card}


def test_best_drive_prefers_the_last_then_the_roomiest_and_never_a_card(tmp_path):
    a, b, c = tmp_path / "A", tmp_path / "B", tmp_path / "Card"
    for d in (a, b, c):
        d.mkdir()
    vols = [vol(a, 5 << 30), vol(b, 9 << 30), vol(c, 90 << 30, card=True)]
    assert live.best_drive(vols) == str(b)
    assert live.best_drive(vols, str(a)) == str(a)
    assert live.best_drive(vols, str(tmp_path / "gone")) == str(b)
    assert live.best_drive(vols[:2] + [vol(c, 90 << 30, writable=False)], str(c)) == str(b)
    assert live.best_drive([vol(c, card=True)]) is None
    assert live.best_drive([vol(a, 1 << 20)]) is None                   # too small to start on
    other = tmp_path / "Backups"; other.mkdir()
    assert live.best_drive(vols, str(other)) == str(other)              # a folder chosen by hand counts as the last place


def test_volumes_marks_cards_and_read_only_disks(tmp_path, monkeypatch):
    v = tmp_path / "Volumes"; v.mkdir()
    (v / "SD" / "DCIM").mkdir(parents=True); (v / "SSD").mkdir(); (v / "Locked").mkdir()
    monkeypatch.setattr(live, "VOLUMES", v)
    monkeypatch.setattr(live, "_mounts", lambda: {})
    monkeypatch.setattr(live, "_mount_text", lambda: f"/dev/disk5s1 on {v / 'Locked'} (exfat, local, read-only)\n/dev/disk4s1 on {v / 'SSD'} (apfs, local)\n")
    by = {x["name"]: x for x in live.volumes()}
    assert by["SD"]["card"] is True and by["SSD"]["card"] is False
    assert by["Locked"]["writable"] is False and by["SSD"]["writable"] is True
    r = TestClient(create_app()).get("/api/live/volumes", params={"last": str(v / "SSD")}).json()
    assert r["best"] == str(v / "SSD")


def test_the_finder_finds_where_a_test_frame_landed(tmp_path):
    home = tmp_path / "home"
    capture = home / "Pictures" / "Job" / "Capture"
    old = home / "Pictures" / "Old"
    write_jpeg(capture / "T.JPG"); write_jpeg(old / "O.JPG")
    write_jpeg(home / "Photos.photoslibrary" / "new.jpg")
    os.utime(old / "O.JPG", (1, 1)); os.utime(old, (1, 1))
    got = live.recent_folders(places=[home])
    assert [f["path"] for f in got] == [str(capture)] and got[0]["photos"] == 1 and got[0]["name"] == "Capture"


def test_the_finder_prefers_the_last_folder_and_stays_out_of_a_copy_on_a_drive(tmp_path):
    a, b, shoot = tmp_path / "a", tmp_path / "b", tmp_path / "SSD" / "Job"
    write_jpeg(a / "1.JPG"); time.sleep(0.02); write_jpeg(b / "2.JPG")
    write_jpeg(shoot / "products" / "x" / "3.JPG"); (shoot / live.MARKER).write_text("id")
    assert [f["path"] for f in live.recent_folders(places=[tmp_path])] == [str(b), str(a)]
    assert [f["path"] for f in live.recent_folders(str(a), [tmp_path])] == [str(a), str(b)]
    later = time.time() + live.FIND_WINDOW_S + 5
    assert live.recent_folders(places=[tmp_path], now=later) == []


def test_the_recent_route_answers_with_nothing_to_find(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_places", lambda: [tmp_path])
    assert TestClient(create_app()).get("/api/live/recent").json() == {"folders": []}


def test_the_slow_shutter_alert_comes_once_per_setting(tmp_path, fake_encoder):
    root = tmp_path / "shoot"; root.mkdir()
    s = session(root)
    for k, t in enumerate((1 / 30, 1 / 30, 1 / 30, 1 / 15, 1 / 15)):
        landed(root, f"D{k}.JPG", im=product(seed=1), when=f"2026:11:06 1{k}:00:00", shutter=t, fnumber=8.0, iso=100, focal=50.0)
        assert wait_for(lambda: len(shots_of(s)) == k + 1)
    finish(s)
    said = [any(a["kind"] == "shutter" for a in x["alerts"]) for x in sorted(shots_of(s), key=lambda x: x["n"])]
    assert said == [True, False, False, True, False]
    assert live.light_alerts(dict(frame(t=1 / 30), focal_mm=50.0), [dict(frame(t=1 / 30), focal_mm=85.0)]) != []    # a new lens is a new setting
