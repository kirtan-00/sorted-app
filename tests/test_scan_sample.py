"""The Scan tab's desktop: GET /api/scan/sample hands the Finder scene a fixed sample of the shoot's files (the cards
on the Stack) and the true per-folder counts. Rows are seeded straight into the index: nothing here needs a decode."""
import time
from types import SimpleNamespace
from fastapi.testclient import TestClient
from photosort import db
from photosort.server import create_app


def _listed(conn, names):
    """List files the way a scan does before it reads them: pending rows, photo or clip by extension."""
    db.add_pending(conn, [SimpleNamespace(rel=r, size=1, mtime=1.0, is_video=r.lower().endswith(".mp4")) for r in names])


def _read(conn, rel, category=None, w=1600, h=1067, duration=None):
    """The features pass reading a listed file: status ok and a qhash (its thumb), then categorise's label."""
    conn.execute("UPDATE photos SET status='ok', qhash=?, width=?, height=?, duration=?, category=? WHERE rel=?",
                 ("q" + rel.replace("/", "_"), w, h, duration, category, rel))
    conn.commit()


def test_no_folder_open_is_an_empty_desktop(tmp_path):
    c = TestClient(create_app(None))
    out = c.get("/api/scan/sample").json()
    assert out["total"] == 0 and out["rows"] == [] and out["folders"] == [] and out["shoot"] is None


def test_an_empty_shoot_has_no_cards(tmp_path):
    c = TestClient(create_app(tmp_path))
    out = c.get("/api/scan/sample").json()
    assert out["total"] == 0 and out["rows"] == [] and out["updates"] == [] and out["folders"] == []
    assert out["shoot"] == tmp_path.name and out["disk"] is None and out["max_id"] == 0


def test_listed_files_are_cards_before_they_are_read(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, ["day1/DSC_0001.JPG", "day1/DSC_0002.JPG", "C0001.MP4"])
    out = TestClient(create_app(tmp_path)).get("/api/scan/sample").json()
    assert out["total"] == 3 and out["photos"] == 2 and out["videos"] == 1
    assert [r["name"] for r in out["rows"]] == ["DSC_0001.JPG", "DSC_0002.JPG", "C0001.MP4"]   # the name, not the path
    assert [r["video"] for r in out["rows"]] == [False, False, True]
    assert all(r["qhash"] is None and r["category"] is None for r in out["rows"])


def test_the_sample_is_evenly_spaced_and_at_most_n(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, [f"DSC_{i:05d}.JPG" for i in range(1000)])
    out = TestClient(create_app(tmp_path)).get("/api/scan/sample?n=120").json()
    names = [r["name"] for r in out["rows"]]
    assert out["total"] == 1000 and len(names) <= 120 and len(names) >= 100
    assert names[0] == "DSC_00000.JPG" and names[1] == "DSC_00009.JPG"     # every ceil(1000 / 120) = 9th file


def test_since_pages_forward_and_never_repeats_a_card(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, [f"DSC_{i:04d}.JPG" for i in range(50)])
    c = TestClient(create_app(tmp_path))
    first = c.get("/api/scan/sample?n=10").json()
    assert len(first["rows"]) == 10 and first["max_id"] == first["rows"][-1]["id"]
    mid = first["rows"][4]["id"]
    rest = c.get(f"/api/scan/sample?n=10&since={mid}").json()
    assert [r["id"] for r in rest["rows"]] == [r["id"] for r in first["rows"][5:]]   # the same sample, from there on
    assert c.get(f"/api/scan/sample?n=10&since={first['max_id']}").json()["rows"] == []


def test_a_resumed_scan_redraws_what_was_read_before_the_pause(tmp_path):
    """Half the shoot was read before the disk went away: opening the tab again (since=0) hands back the read
    cards with their thumbs and the rest still waiting, rather than a desktop that starts over."""
    conn = db.connect(tmp_path)
    names = [f"DSC_{i:03d}.JPG" for i in range(20)]
    _listed(conn, names)
    for r in names[:10]:
        _read(conn, r)
    out = TestClient(create_app(tmp_path)).get("/api/scan/sample?n=20").json()
    assert [r["qhash"] is not None for r in out["rows"]] == [True] * 10 + [False] * 10


def test_ids_asks_again_about_cards_still_waiting(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, ["a.jpg", "b.jpg", "c.mp4"])
    c = TestClient(create_app(tmp_path))
    rows = c.get("/api/scan/sample").json()["rows"]
    _read(conn, "b.jpg")
    _read(conn, "c.mp4", w=1920, h=1080, duration=12.5)
    ids = ",".join(str(r["id"]) for r in rows)
    up = {u["name"]: u for u in c.get(f"/api/scan/sample?since={rows[-1]['id']}&ids={ids}").json()["updates"]}
    assert up["a.jpg"]["qhash"] is None and up["b.jpg"]["qhash"] == "qb.jpg"
    assert up["c.mp4"]["video"] and up["c.mp4"]["duration"] == 12.5 and up["c.mp4"]["width"] == 1920
    assert c.get("/api/scan/sample?ids=1,x").status_code == 400


def test_folders_split_photos_from_videos_with_true_counts(tmp_path):
    conn = db.connect(tmp_path)
    names = [f"p{i}.jpg" for i in range(7)] + [f"v{i}.mp4" for i in range(3)]
    _listed(conn, names)
    for i, r in enumerate(names):
        _read(conn, r, category=("people" if i % 2 else "food") if r.endswith(".jpg") else "people")
    out = TestClient(create_app(tmp_path)).get("/api/scan/sample?n=2").json()
    assert len(out["rows"]) == 2                                       # two cards stand for the shoot ...
    folders = {(f["kind"], f["name"]): f["n"] for f in out["folders"]}
    assert folders == {("photos", "food"): 4, ("photos", "people"): 3, ("videos", "people"): 3}   # ... the counts are all of it
    assert out["photos"] == 7 and out["videos"] == 3


def test_folders_wait_while_the_scan_is_still_reading(tmp_path):
    """A rescan keeps last time's labels on unchanged rows; the desktop must not show them as this pass's folders."""
    conn = db.connect(tmp_path)
    _listed(conn, ["a.jpg"])
    _read(conn, "a.jpg", category="food")
    app = create_app(tmp_path)
    c = TestClient(app)
    assert c.get("/api/scan/sample").json()["folders"] == [{"kind": "photos", "name": "food", "n": 1}]
    state = _state(app)
    state["running"] = True
    for stage in ("scan", "features", "embed", "categorise"):
        state["progress"] = {"stage": stage, "done": 0, "total": 1}
        assert c.get("/api/scan/sample").json()["folders"] == [], stage
    state["progress"] = {"stage": "save", "done": 0, "total": 0}
    assert c.get("/api/scan/sample").json()["folders"] != []
    state["running"] = False


def test_sixty_thousand_files_come_back_as_a_small_sample_quickly(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, [f"d{i // 1000}/DSC_{i:05d}.JPG" for i in range(60000)])
    # read rows carry their embedding, as real ones do, so a query that reads whole rows would show up here
    conn.execute("UPDATE photos SET status='ok', qhash='q' || id, category='people', embed=zeroblob(?) WHERE id % 3 = 0", (db.EMBED_BYTES,))
    conn.commit()
    c = TestClient(create_app(tmp_path))
    c.get("/api/scan/sample")                       # warm
    t = time.perf_counter()
    out = c.get("/api/scan/sample").json()
    took = time.perf_counter() - t
    assert out["total"] == 60000 and 100 <= len(out["rows"]) <= 120
    assert out["folders"] == [{"kind": "photos", "name": "people", "n": 20000}]
    assert took < 0.5, took
    assert sum(r["looked"] for r in out["rows"]) > 0
    t = time.perf_counter()
    paged = c.get(f"/api/scan/sample?since={out['max_id']}&folders=0&ids=" + ",".join(str(r["id"]) for r in out["rows"])).json()
    assert time.perf_counter() - t < 0.25 and paged["rows"] == [] and len(paged["updates"]) == len(out["rows"])


def test_folders_0_skips_the_count(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, ["a.jpg"])
    _read(conn, "a.jpg", category="food")
    out = TestClient(create_app(tmp_path)).get("/api/scan/sample?folders=0").json()
    assert out["folders"] == [] and out["rows"][0]["category"] == "food"


def test_a_file_that_failed_is_marked_and_never_gets_a_thumb(tmp_path):
    conn = db.connect(tmp_path)
    _listed(conn, ["bad.jpg"])
    conn.execute("UPDATE photos SET status='error', qhash='stale' WHERE rel='bad.jpg'"); conn.commit()
    r = TestClient(create_app(tmp_path)).get("/api/scan/sample").json()["rows"][0]
    assert r["error"] is True and r["qhash"] is None and r["looked"] is False


def _state(app):
    """The server's state dict, reached through a route's closure (create_app keeps it private)."""
    for r in app.routes:
        if getattr(r, "path", "") == "/api/scan/sample":
            for cell in r.endpoint.__closure__ or ():
                v = cell.cell_contents
                if isinstance(v, dict) and "progress" in v and "running" in v:
                    return v
    raise AssertionError("state not found")
