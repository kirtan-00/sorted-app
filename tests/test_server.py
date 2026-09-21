import os
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from photosort.index import index_folder
from photosort.server import create_app


def test_api(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    assert c.get("/").status_code == 200 and "photosort" in c.get("/").text.lower()
    st = c.get("/api/stats").json()
    assert st["photos"] == 1
    res = c.get("/api/search").json()["results"]
    assert res[0]["rel"] == "a.jpg"
    assert c.get(f"/api/thumb/{res[0]['qhash']}?size=grid").headers["content-type"] == "image/jpeg"
    assert c.post("/api/export", json={"ids": [res[0]["id"]], "name": "t"}).json()["started"]
    for _ in range(100):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.05)
    assert p["error"] is None and p["done"] == 1
    ex = Path(p["path"])
    assert ex.is_dir() and (ex / "a.jpg").is_file() and not str(ex).startswith(str(tmp_path))
    assert c.get("/api/people").json() == []
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


def test_export_refuses_when_disk_is_short(tmp_path, monkeypatch):
    from conftest import make_image
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    pid = c.get("/api/search").json()["results"][0]["id"]
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "copy"})
    assert r.status_code == 400 and "free" in r.json()["detail"]
    assert c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "symlink"}).json()["started"]
    for _ in range(100):
        p2 = c.get("/api/export/progress").json()
        if not p2["running"]: break
        time.sleep(0.05)
    assert p2["error"] is None
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


def test_folder_switch_refused_while_export_running(tmp_path, monkeypatch):
    from conftest import make_image
    import photosort.server as srv
    from photosort.export import export_ids as real_export_ids
    for i in range(5):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    ids = c.get("/api/search/ids").json()["ids"]

    def slow_export_ids(root, ids_, name, mode="copy", progress=None, base=None):
        def slow_progress(d):
            time.sleep(0.1)
            if progress: progress(d)
        return real_export_ids(root, ids_, name, mode, progress=slow_progress, base=base)

    monkeypatch.setattr(srv, "export_ids", slow_export_ids)
    assert c.post("/api/export", json={"ids": ids, "name": "t", "mode": "symlink"}).json()["started"]
    assert c.post("/api/folder", json={"path": str(tmp_path)}).status_code == 409
    for _ in range(200):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.02)
    assert p["error"] is None
    assert c.post("/api/folder", json={"path": str(tmp_path)}).status_code == 200


def test_search_by_missing_image_id_is_404(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/search", params={"image_id": 999})
    assert r.status_code == 404


def test_search_total_and_ids_endpoint(tmp_path):
    from conftest import make_image
    for i in range(5): make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    page = c.get("/api/search", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5 and page["offset"] == 2 and [r["rel"] for r in page["results"]] == ["p2.jpg", "p3.jpg"]
    ids = c.get("/api/search/ids").json()
    assert ids["total"] == 5 and len(ids["ids"]) == 5 and all(isinstance(i, int) for i in ids["ids"])


def test_ui_static_app_js_served(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    r = c.get("/ui/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_ui_unknown_static_file_404s(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    assert c.get("/ui/does-not-exist.js").status_code == 404


def test_search_after_reindex_and_person_filter_dont_500(tmp_path):
    """Index() opens its sqlite connection on the thread that builds the app; FastAPI
    runs sync endpoints in a worker thread. refresh() and person-filtered search must
    not reuse that connection cross-thread or sqlite raises ProgrammingError -> 500."""
    from conftest import make_image
    import time
    make_image(tmp_path, "a.jpg", seed=1)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    make_image(tmp_path, "b.jpg", seed=2)
    r = c.post("/api/index", json={"faces": False})
    assert r.status_code == 200
    for _ in range(200):
        p = c.get("/api/progress").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["running"] is False
    r2 = c.get("/api/search")
    assert r2.status_code == 200
    assert len(r2.json()["results"]) == 2
    r3 = c.get("/api/search", params={"person": 1})
    assert r3.status_code == 200


def test_index_and_progress_cycle(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    r = c.post("/api/index", json={"faces": False})
    assert r.status_code == 200 and r.json()["started"] is True
    r2 = c.post("/api/index", json={"faces": False})
    assert r2.status_code == 409
    import time
    for _ in range(200):
        p = c.get("/api/progress").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["running"] is False


def test_export_bad_name_is_400_and_writes_nothing(tmp_path):
    import os
    from conftest import make_image
    from photosort.config import export_root
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    pid = c.get("/api/search").json()["results"][0]["id"]
    for bad in ["../../x", "/tmp/x", ".."]:
        r = c.post("/api/export", json={"ids": [pid], "name": bad})
        assert r.status_code == 400, bad
    assert os.listdir(export_root()) == [] and sorted(os.listdir(tmp_path)) == ["a.jpg"]
    assert c.post("/api/export", json={"ids": [pid], "name": "fine"}).json()["started"]
    for _ in range(100):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.05)
    assert p["error"] is None
    assert Path(p["path"]).resolve().is_relative_to(export_root().resolve())


def test_index_failure_is_reported_as_error_stage(tmp_path, monkeypatch):
    import time
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("photosort.index.index_folder", boom)
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/index", json={"faces": False}).status_code == 200
    for _ in range(200):
        p = c.get("/api/progress").json()
        if not p["running"]:
            break
        time.sleep(0.05)
    assert p["running"] is False and p["stage"] == "error" and "disk on fire" in p["error"]
    assert c.post("/api/index", json={"faces": False}).status_code == 200   # not wedged


def test_free_port_skips_busy_port():
    import socket
    from photosort.cli import free_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0)); busy = s.getsockname()[1]; s.listen(1)
        got = free_port(busy)
        assert got != busy and busy < got < busy + 10


# ---------- folder switching ----------

def test_no_folder_open_by_default_and_endpoints_degrade():
    c = TestClient(create_app(None))
    f = c.get("/api/folder").json()
    assert f == {"root": None, "name": None, "indexed": False, "mounted": False, "disk": None}
    assert c.get("/api/stats").json()["photos"] == 0
    assert c.get("/api/search").json() == {"results": [], "total": 0, "offset": 0, "limit": 200}
    assert c.get("/api/people").json() == []
    assert c.post("/api/index", json={"faces": False}).status_code == 400
    assert c.post("/api/export", json={"ids": [], "name": "t"}).status_code == 400


def test_post_folder_switches_root(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(None))
    r = c.post("/api/folder", json={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == str(tmp_path) and body["indexed"] is True
    st = c.get("/api/stats").json()
    assert st["photos"] == 1
    res = c.get("/api/search").json()["results"]
    assert res[0]["rel"] == "a.jpg"


def test_post_folder_non_directory_is_400(tmp_path):
    c = TestClient(create_app(None))
    r = c.post("/api/folder", json={"path": str(tmp_path / "does-not-exist")})
    assert r.status_code == 400


def test_post_folder_409_while_indexing(tmp_path):
    c = TestClient(create_app(tmp_path))
    c.app.state.photosort["running"] = True
    try:
        r = c.post("/api/folder", json={"path": str(tmp_path)})
        assert r.status_code == 409
    finally:
        c.app.state.photosort["running"] = False


def test_folder_recent_lists_switched_path(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(None))
    c.post("/api/folder", json={"path": str(tmp_path)})
    recent = c.get("/api/folder/recent").json()["recent"]
    assert any(r["path"] == str(tmp_path) for r in recent)


def test_folder_choose_returns_204_when_picker_gives_nothing(tmp_path, monkeypatch):
    import subprocess as sp
    def fake_run(*a, **k):
        return sp.CompletedProcess(a, returncode=1, stdout="", stderr="")
    monkeypatch.setattr("photosort.server.subprocess.run", fake_run)
    c = TestClient(create_app(None))
    r = c.post("/api/folder/choose")
    assert r.status_code == 204


# ---------- categories ----------

def test_categories_endpoint_returns_fixed_and_discovered(tmp_path):
    from conftest import make_image
    from photosort import db as db_mod
    make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/categories").json() == {"fixed": {"unclassified": 2}, "discovered": {}, "drone": 0}
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET category='beach', cluster='excavator', cluster_score=0.9 WHERE rel='a.jpg'")
    conn.execute("UPDATE photos SET cluster='excavator', cluster_score=0.2, aerial=1 WHERE rel='b.jpg'")
    conn.commit()
    assert c.get("/api/categories").json() == {"fixed": {"beach": 1, "unclassified": 1}, "discovered": {"excavator": 2}, "drone": 1}
    assert TestClient(create_app(None)).get("/api/categories").json() == {"fixed": {}, "discovered": {}, "drone": 0}


def test_categories_endpoint_lists_fixed_tiles_in_calibrated_order(tmp_path):
    """Tiles follow CATEGORIES order, then "other", then "unclassified", not the alphabetical order SQLite
    hands back from GROUP BY."""
    from conftest import make_image
    from photosort import db as db_mod
    from photosort.classify import CATEGORIES
    for i, name in enumerate(["a", "b", "c", "d", "e"]): make_image(tmp_path, f"{name}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    for rel, cat in (("a.jpg", "other"), ("b.jpg", "birds-animals"), ("c.jpg", "food"), ("d.jpg", "ocean")):
        conn.execute("UPDATE photos SET category=? WHERE rel=?", (cat, rel))
    conn.commit()
    fixed = TestClient(create_app(tmp_path)).get("/api/categories").json()["fixed"]
    assert list(fixed) == ["ocean", "food", "birds-animals", "other", "unclassified"]
    assert list(CATEGORIES).index("food") < list(CATEGORIES).index("birds-animals")


def test_search_by_aerial(tmp_path):
    from conftest import make_image
    from photosort import db as db_mod
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET aerial=1 WHERE rel='b.jpg'"); conn.commit()
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/search", params={"aerial": 1})
    assert r.status_code == 200 and [x["rel"] for x in r.json()["results"]] == ["b.jpg"] and r.json()["total"] == 1
    assert r.json()["results"][0]["aerial"]
    assert c.get("/api/search/ids", params={"aerial": 1}).json()["total"] == 1
    res = c.get("/api/search").json()["results"]
    assert len(res) == 2 and [bool(x["aerial"]) for x in res] == [False, True]      # every result carries it


def test_search_by_cluster(tmp_path):
    from conftest import make_image
    from photosort import db as db_mod
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET cluster='havan fire', cluster_score=0.8 WHERE rel='b.jpg'"); conn.commit()
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/search", params={"cluster": "havan fire"})
    assert r.status_code == 200 and [x["rel"] for x in r.json()["results"]] == ["b.jpg"] and r.json()["total"] == 1
    assert c.get("/api/search/ids", params={"cluster": "havan fire"}).json()["total"] == 1
    assert c.get("/api/search", params={"cluster": "crane"}).json()["results"] == []


def test_rename_discovered_category_endpoint(tmp_path):
    """POST /api/categories/discovered/rename {old, new}: 200 with the rows moved and the search index
    refreshed, 400 on a bad or colliding name, 404 on an unknown old name, 409 while indexing or
    categorising."""
    from conftest import make_image
    from photosort import db as db_mod
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2); make_image(tmp_path, "c.jpg", seed=3)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET cluster='group 1', cluster_score=0.8 WHERE rel IN ('a.jpg', 'b.jpg')")
    conn.execute("UPDATE photos SET cluster='beach', cluster_score=0.9 WHERE rel='c.jpg'"); conn.commit()
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/search", params={"cluster": "group 1"}).json()["total"] == 2
    r = c.post("/api/categories/discovered/rename", json={"old": "group 1", "new": "office b-roll"})
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "office b-roll", "moved": 2}
    assert c.get("/api/categories").json()["discovered"] == {"office b-roll": 2, "beach": 1}
    assert c.get("/api/search", params={"cluster": "office b-roll"}).json()["total"] == 2     # the index was refreshed
    assert c.get("/api/search", params={"cluster": "group 1"}).json()["total"] == 0
    for bad in ("", "  ", ".", "..", "beach", "group 2"):
        assert c.post("/api/categories/discovered/rename", json={"old": "office b-roll", "new": bad}).status_code == 400, bad
    assert c.post("/api/categories/discovered/rename", json={"old": "group 1", "new": "x"}).status_code == 404
    st = c.app.state.photosort
    st["running"] = True
    try:
        assert c.post("/api/categories/discovered/rename", json={"old": "office b-roll", "new": "x"}).status_code == 409
    finally:
        st["running"] = False
    st["classify"]["running"] = True
    try:
        assert c.post("/api/categories/discovered/rename", json={"old": "office b-roll", "new": "x"}).status_code == 409
    finally:
        st["classify"]["running"] = False
    assert c.get("/api/categories").json()["discovered"] == {"office b-roll": 2, "beach": 1}
    assert TestClient(create_app(None)).post("/api/categories/discovered/rename", json={"old": "a", "new": "b"}).status_code == 400


def _wait_idle(c, n=100):
    import time
    for _ in range(n):
        if not c.get("/api/progress").json()["running"]: return
        time.sleep(0.1)

def test_errors_listed_and_retryable(tmp_path):
    """A transient read failure on an unchanged file: a plain re-index leaves it alone (same size+mtime),
    retry_errors re-processes it."""
    from conftest import make_image
    from photosort import db
    make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path); conn.execute("UPDATE photos SET status='error' WHERE rel='a.jpg'"); conn.commit()
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/stats").json()["errors"] == 1
    listed = c.get("/api/errors").json()["errors"]
    assert [e["rel"] for e in listed] == ["a.jpg"] and listed[0]["indexed_at"]
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    assert c.get("/api/stats").json()["errors"] == 1          # unchanged file, not retried by default
    assert c.post("/api/index", json={"faces": False, "retry_errors": True}).json()["started"]; _wait_idle(c)
    assert c.get("/api/stats").json()["errors"] == 0


def test_search_by_category(tmp_path):
    """category is another agent's concurrent work (db column + Filters field + the
    actual filtering in search.Index). We degrade to an empty list if Filters doesn't
    support category yet; otherwise we check /api/search only returns matching rows.
    Both photos are written BEFORE the app/Index is built so a fresh Index sees them
    (no stale cache)."""
    from conftest import make_image
    from photosort import db as db_mod
    make_image(tmp_path, "a.jpg", seed=1)
    make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
    if "category" in cols:
        a_id = conn.execute("SELECT id FROM photos WHERE rel='a.jpg'").fetchone()[0]
        b_id = conn.execute("SELECT id FROM photos WHERE rel='b.jpg'").fetchone()[0]
        conn.execute("UPDATE photos SET category='beach' WHERE id=?", (a_id,))
        conn.execute("UPDATE photos SET category='ocean' WHERE id=?", (b_id,))
        conn.commit()
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/search", params={"category": "beach"})
    assert r.status_code == 200
    if "category" in cols:
        res = r.json()["results"]
        assert [row["rel"] for row in res] == ["a.jpg"]
        assert res[0]["category"] == "beach"
    else:
        assert r.json()["results"] == []


def test_search_by_category_flags_less_sure_and_sure_only_drops_them(tmp_path):
    from conftest import make_image
    from photosort import db as db_mod
    for i, n in enumerate("abcd"): make_image(tmp_path, f"{n}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET category='building', category_score=0.8, category_guess='building', category_guess_score=0.8 WHERE rel='a.jpg'")
    conn.execute("UPDATE photos SET category='building', category_score=0.4, category_guess='building', category_guess_score=0.4 WHERE rel='b.jpg'")
    conn.execute("UPDATE photos SET category='other', category_score=0.45, category_guess='building', category_guess_score=0.45 WHERE rel='c.jpg'")
    conn.execute("UPDATE photos SET category='other', category_score=0.9, category_guess='road', category_guess_score=0.9 WHERE rel='d.jpg'")
    conn.commit()
    c = TestClient(create_app(tmp_path))
    body = c.get("/api/search", params={"category": "building"}).json()
    assert body["total"] == 3
    assert [(r["rel"], r["sure"], r["confidence"]) for r in body["results"]] == [("a.jpg", True, 0.8), ("c.jpg", False, 0.45), ("b.jpg", False, 0.4)]
    assert c.get("/api/search/ids", params={"category": "building"}).json()["ids"] == [r["id"] for r in body["results"]]
    sure = c.get("/api/search", params={"category": "building", "sure_only": 1}).json()
    assert sure["total"] == 1 and [r["rel"] for r in sure["results"]] == ["a.jpg"]
    assert c.get("/api/search/ids", params={"category": "building", "sure_only": 1}).json()["total"] == 1


def test_export_categories_sure_only_by_default(tmp_path, tmp_path_factory):
    """Export ticked categories leaves the less-sure band out unless include_unsure is set; then a photo
    filed under "other" whose guess was beach lands in beach/ (and still in other/ when other is ticked)."""
    from test_export import _two_category_shoot
    from photosort import db as db_mod
    before = _two_category_shoot(tmp_path)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET category='other', category_score=0.3, category_guess='beach', category_guess_score=0.3 WHERE rel='c.jpg'")
    conn.execute("UPDATE photos SET category_score=0.2 WHERE rel='b.jpg'")       # ocean, but barely
    conn.commit()
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean", "other"], "mode": "symlink"})
    assert r.json()["total"] == 2 and _wait_export(c)["error"] is None
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg"]
    assert sorted(x.name for x in (out / "other").iterdir()) == ["c.jpg"]
    assert not (out / "ocean").exists()
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean", "other"], "mode": "symlink", "include_unsure": True})
    assert r.json()["total"] == 4 and _wait_export(c)["error"] is None
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg", "c.jpg"]
    assert sorted(x.name for x in (out / "ocean").iterdir()) == ["b.jpg"]
    assert sorted(os.listdir(tmp_path)) == before


# final-review fixes

def test_export_people_refuses_copy_when_disk_is_short(tmp_path, monkeypatch):
    from conftest import make_image
    from photosort import db
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path); conn.execute("UPDATE photos SET n_faces=1"); conn.commit()   # one solo shot to export
    c = TestClient(create_app(tmp_path))
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/export/people", json={"mode": "copy"})
    assert r.status_code == 400 and "free" in r.json()["detail"]
    r2 = c.post("/api/export/people", json={"mode": "symlink"})
    assert r2.status_code == 200
    assert (Path(r2.json()["path"]) / "solo" / "a.jpg").is_symlink()
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


def test_export_start_is_serialised(tmp_path, monkeypatch):
    """Two rapid export POSTs: the check-then-set window spans export_bytes + disk_usage + export_dir,
    so without a lock both pass the 'already running' check."""
    import threading
    from conftest import make_image
    import photosort.server as srv
    from photosort.export import export_bytes as real_export_bytes, export_ids as real_export_ids
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    app = create_app(tmp_path)
    pid = TestClient(app).get("/api/search").json()["results"][0]["id"]

    def slow_export_bytes(root, ids):
        time.sleep(0.3)
        return real_export_bytes(root, ids)

    def slow_export_ids(root, ids_, name, mode="copy", progress=None, base=None):
        time.sleep(0.5)
        return real_export_ids(root, ids_, name, mode, progress=progress, base=base)

    monkeypatch.setattr(srv, "export_bytes", slow_export_bytes)
    monkeypatch.setattr(srv, "export_ids", slow_export_ids)
    codes = []
    def post():
        codes.append(TestClient(app).post("/api/export", json={"ids": [pid], "name": "race", "mode": "copy"}).status_code)
    ts = [threading.Thread(target=post) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(codes) == [200, 409]
    c = TestClient(app)
    for _ in range(200):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.02)
    assert p["error"] is None and p["done"] == 1
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


def test_folder_choose_409_while_export_running(tmp_path, monkeypatch):
    import subprocess as sp
    called = []
    def fake_run(*a, **k):
        called.append(a)
        return sp.CompletedProcess(a, returncode=0, stdout=str(tmp_path) + "\n", stderr="")
    monkeypatch.setattr("photosort.server.subprocess.run", fake_run)
    c = TestClient(create_app(tmp_path))
    c.app.state.photosort["export"]["running"] = True
    try:
        assert c.post("/api/folder/choose").status_code == 409
        assert called == []            # refused before the picker was even opened
    finally:
        c.app.state.photosort["export"]["running"] = False
    assert c.post("/api/folder/choose").status_code == 200


def _fake_index_folder(stats):
    def fake(root, faces=True, progress=None, retry_errors=False, **kw):
        if progress: progress({"stage": "done", "done": stats["total"], "total": stats["total"], "stage_started": time.time()})
        return dict(stats)
    return fake


def test_index_run_categorises_when_something_changed(tmp_path, monkeypatch):
    from conftest import make_image
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    calls = []
    def fake_classify(root, people_by_faces=True):
        calls.append(Path(root)); return {"other": 1}
    monkeypatch.setattr(srv.classify_mod, "classify_and_store", fake_classify)
    monkeypatch.setattr("photosort.index.index_folder",
                        _fake_index_folder(dict(total=1, skipped=0, indexed=1, errors=0, embedded=1, seconds=0.1)))
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    assert calls == [tmp_path]
    cp = c.get("/api/classify/progress").json()
    assert cp["running"] is False and cp["error"] is None and cp["counts"] == {"other": 1}
    p = c.get("/api/progress").json()
    assert p["stage"] == "done" and p["done"] == 1 and p["total"] == 1 and p["running"] is False
    # nothing changed on the next run: no second classify pass
    monkeypatch.setattr("photosort.index.index_folder",
                        _fake_index_folder(dict(total=1, skipped=1, indexed=0, errors=0, embedded=0, seconds=0.1)))
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    assert calls == [tmp_path]
    assert c.get("/api/progress").json()["stage"] == "done"


def test_classify_endpoint_leaves_the_index_fresh_after_discovery(tmp_path, monkeypatch):
    """Categorise writes the fixed categories, then the discovered ones. A search that lands between the two
    refreshes the Index and clears the stale flag; the cluster columns written after that must still reach
    the next search, otherwise a tile says "x 1" and clicking it finds nothing."""
    from conftest import make_image
    from photosort import db as db_mod
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    monkeypatch.setattr(srv.classify_mod, "classify_and_store", lambda root, people_by_faces=True: {"other": 1})
    def fake_discover(root, k=None):
        c.app.state.photosort["stale"] = False              # a search refreshed the Index in between
        conn = db_mod.connect(root); conn.execute("UPDATE photos SET cluster='x', cluster_score=1.0"); conn.commit()
        return {"x": 1}
    monkeypatch.setattr(srv.classify_mod, "discover_and_store", fake_discover)
    assert c.post("/api/classify").json()["started"]
    for _ in range(100):
        if not c.get("/api/classify/progress").json()["running"]: break
        time.sleep(0.05)
    cp = c.get("/api/classify/progress").json()
    assert cp["counts"] == {"other": 1} and cp["discovered"] == {"x": 1} and cp["error"] is None
    assert c.get("/api/categories").json()["discovered"] == {"x": 1}
    assert c.get("/api/search", params={"cluster": "x"}).json()["total"] == 1


def test_index_run_survives_a_classify_failure(tmp_path, monkeypatch):
    from conftest import make_image
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    def boom(root, people_by_faces=True):
        raise RuntimeError("no embeddings")
    monkeypatch.setattr(srv.classify_mod, "classify_and_store", boom)
    monkeypatch.setattr("photosort.index.index_folder",
                        _fake_index_folder(dict(total=1, skipped=0, indexed=1, errors=0, embedded=1, seconds=0.1)))
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    p = c.get("/api/progress").json()
    assert p["stage"] == "done" and p["running"] is False
    cp = c.get("/api/classify/progress").json()
    assert cp["running"] is False and "no embeddings" in cp["error"]
    assert c.post("/api/classify").status_code == 200        # the manual button still works afterwards
    for _ in range(100):
        if not c.get("/api/classify/progress").json()["running"]: break
        time.sleep(0.05)


# find a person from a reference photo

def test_people_find_returns_ranked_photos(tmp_path, monkeypatch):
    from test_people import _fake_shoot, _p0_reference
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = sorted(os.listdir(tmp_path))
    ref = _p0_reference(conn)   # built here: the endpoint runs on a worker thread, sqlite conns don't cross
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    c = TestClient(create_app(tmp_path))
    r = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4 and len(body["results"]) == 4 and body["faces_in_reference"] == 1
    assert body["results"][0]["score"] >= body["results"][-1]["score"]
    assert all("qhash" in x and "rel" in x for x in body["results"])
    assert all(x["rel"].startswith("p0_") for x in body["results"])
    assert all(x["sure"] is True and x["confidence"] == x["score"] for x in body["results"])
    assert c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg"), "min_sim": 0.99}).json()["total"] == 0
    assert c.post("/api/people/find", json={"path": "/nope.jpg"}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


def test_people_find_returns_a_less_sure_band_below_the_slider(tmp_path, monkeypatch):
    """Matches from max(min_sim - 0.1, 0.4) up to min_sim come back too, flagged sure: false, after the
    sure ones and sorted by similarity; total counts both bands."""
    from test_people import _fake_shoot, _p0_reference
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    ref = _p0_reference(conn)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    c = TestClient(create_app(tmp_path))
    at_default = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg")}).json()
    sims = sorted((x["score"] for x in at_default["results"]), reverse=True)
    assert len(sims) == 4
    cut = (sims[1] + sims[2]) / 2                    # two above the slider, two in the band below it
    body = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg"), "min_sim": cut}).json()
    assert body["total"] == 4
    assert [x["sure"] for x in body["results"]] == [True, True, False, False]
    scores = [x["score"] for x in body["results"]]
    assert scores == sorted(scores, reverse=True) and all(x["confidence"] == x["score"] for x in body["results"])

def test_people_find_no_face_is_200_empty(tmp_path, monkeypatch):
    from test_people import _fake_shoot
    from photosort import people
    _fake_shoot(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [])
    c = TestClient(create_app(tmp_path))
    body = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg")}).json()
    assert body == {"faces_in_reference": 0, "person_id": None, "total": 0, "results": []}

def test_people_find_choose_204_on_cancel_and_400_without_folder(tmp_path, monkeypatch):
    import subprocess as sp
    def fake_run(*a, **k):
        return sp.CompletedProcess(a, returncode=1, stdout="", stderr="")
    monkeypatch.setattr("photosort.server.subprocess.run", fake_run)
    assert TestClient(create_app(None)).post("/api/people/find/choose").status_code == 400
    from test_people import _fake_shoot, _p0_reference
    from photosort import people
    conn = _fake_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/people/find/choose").status_code == 204
    ref = _p0_reference(conn)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    chosen = str(tmp_path / "p2_1.jpg")
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=chosen + "\n", stderr=""))
    body = c.post("/api/people/find/choose").json()
    assert body["path"] == chosen and body["total"] == 4 and body["results"][0]["rel"].startswith("p0_")

def test_people_find_tiny_face_and_unreadable_reference(tmp_path, monkeypatch):
    from test_people import _fake_shoot, _p0_reference
    from photosort import people
    conn = _fake_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    r = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg")})   # real decode of a byte stub
    assert r.status_code == 400 and "could not read" in r.json()["detail"]
    tiny = _p0_reference(conn, w=20, h=20)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [tiny])
    body = c.post("/api/people/find", json={"path": str(tmp_path / "p1_0.jpg")}).json()
    assert body["reference_face_too_small"] is True and body["total"] == 0 and body["faces_in_reference"] == 1


# export destination (another disk)

def _shoot_client(tmp_path, n=1):
    from conftest import make_image
    for i in range(n):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    return TestClient(create_app(tmp_path))


def _wait_export(c, n=200):
    for _ in range(n):
        p = c.get("/api/export/progress").json()
        if not p["running"]: return p
        time.sleep(0.02)
    return p


def test_export_destination_default_payload(tmp_path):
    from photosort.config import export_root
    c = _shoot_client(tmp_path)
    d = c.get("/api/export/destination").json()
    assert d["path"] == str(export_root()) and d["default"] is True and d["mounted"] is True
    assert isinstance(d["free_gb"], float) and d["free_gb"] > 0


def test_export_destination_set_get_and_reset(tmp_path, tmp_path_factory):
    from photosort import settings
    from photosort.config import export_root
    c = _shoot_client(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    r = c.post("/api/export/destination", json={"path": str(disk)})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["path"] == str(disk.resolve()) and d["default"] is False and d["mounted"] is True and d["free_gb"] > 0
    assert c.get("/api/export/destination").json() == d
    assert settings.get_export_base() == disk.resolve()            # survives a restart
    assert c.post("/api/export/destination", json={"path": str(disk / "nope")}).status_code == 400
    pid = c.get("/api/search").json()["results"][0]["id"]
    assert c.post("/api/export", json={"ids": [pid], "name": "t"}).json()["started"]
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 1
    assert Path(p["path"]) == disk.resolve() / tmp_path.resolve().name / "t" and (Path(p["path"]) / "p0.jpg").is_file()
    assert os.listdir(export_root()) == []
    d2 = c.request("DELETE", "/api/export/destination").json()
    assert d2["path"] == str(export_root()) and d2["default"] is True
    assert settings.get_export_base() is None
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_export_destination_inside_source_is_400(tmp_path):
    c = _shoot_client(tmp_path)
    (tmp_path / "sub").mkdir()
    for bad in [tmp_path, tmp_path / "sub"]:
        r = c.post("/api/export/destination", json={"path": str(bad)})
        assert r.status_code == 400 and "inside the source folder" in r.json()["detail"], bad
    assert c.get("/api/export/destination").json()["default"] is True
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "sub"]


def test_export_refuses_when_destination_not_mounted(tmp_path, tmp_path_factory):
    import shutil as sh
    c = _shoot_client(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    sh.rmtree(disk)                                                  # the disk got unplugged
    d = c.get("/api/export/destination").json()
    assert d["mounted"] is False and d["free_gb"] == 0.0 and d["default"] is False
    pid = c.get("/api/search").json()["results"][0]["id"]
    for mode in ["copy", "symlink"]:
        r = c.post("/api/export", json={"ids": [pid], "name": "t", "mode": mode})
        assert r.status_code == 400 and "not mounted" in r.json()["detail"], mode
    r = c.post("/api/export/people", json={"mode": "symlink"})
    assert r.status_code == 400 and "not mounted" in r.json()["detail"]
    assert c.request("DELETE", "/api/export/destination").json()["default"] is True
    assert c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "symlink"}).json()["started"]
    assert _wait_export(c)["error"] is None
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_export_preflight_checks_the_destination_disk(tmp_path, tmp_path_factory, monkeypatch):
    import photosort.server as srv
    c = _shoot_client(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    asked = []
    class Usage: free = 10
    def fake_usage(p):
        asked.append(Path(p)); return Usage
    monkeypatch.setattr(srv.shutil, "disk_usage", fake_usage)
    pid = c.get("/api/search").json()["results"][0]["id"]
    r = c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "copy"})
    assert r.status_code == 400 and "on that disk" in r.json()["detail"]
    assert asked and asked[-1] == disk.resolve()
    assert c.request("DELETE", "/api/export/destination").json()["default"] is True
    r2 = c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "copy"})
    assert r2.status_code == 400 and "on this Mac" in r2.json()["detail"]
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_export_destination_choose_mirrors_folder_picker(tmp_path, tmp_path_factory, monkeypatch):
    import subprocess as sp
    c = _shoot_client(tmp_path)
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=1, stdout="", stderr=""))
    assert c.post("/api/export/destination/choose").status_code == 204
    disk = tmp_path_factory.mktemp("disk")
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(disk) + "\n", stderr=""))
    d = c.post("/api/export/destination/choose").json()
    assert d["path"] == str(disk.resolve()) and d["default"] is False and d["mounted"] is True
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(tmp_path) + "\n", stderr=""))
    r = c.post("/api/export/destination/choose")
    assert r.status_code == 400 and "inside the source folder" in r.json()["detail"]
    assert c.get("/api/export/destination").json()["path"] == str(disk.resolve())   # the bad pick changed nothing


# export selected categories, one folder each

def test_export_categories_endpoint_runs_to_completion(tmp_path, tmp_path_factory):
    from test_export import _two_category_shoot
    before = _two_category_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean"], "mode": "copy", "include_raw": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 2}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 3 and p["total"] == 3 and p["failed"] == 0
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert Path(p["path"]) == out
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.ARW", "a.jpg"]
    assert sorted(x.name for x in (out / "ocean").iterdir()) == ["b.jpg"]
    assert c.post("/api/export/categories", json={"categories": ["beach"], "mode": "csv"}).status_code == 400
    assert c.post("/api/export/categories", json={"categories": [], "mode": "copy"}).status_code == 400
    r2 = c.post("/api/export/categories", json={"categories": None, "mode": "symlink"})
    assert r2.json()["total"] == 2
    assert _wait_export(c)["error"] is None
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_endpoint_exports_discovered_names_too(tmp_path, tmp_path_factory):
    """Discovered names land under categories/discovered/<name>/; fixed and discovered go out in one job,
    and a request with no fixed categories ticked but a discovered one is fine."""
    from test_export import _two_category_shoot
    from photosort import db as db_mod
    before = _two_category_shoot(tmp_path)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET cluster='excavator', cluster_score=0.9 WHERE rel IN ('b.jpg', 'c.jpg')"); conn.commit()
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach"], "discovered": ["excavator"], "mode": "symlink"})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 3}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 3 and p["failed"] == 0
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg"]
    assert sorted(x.name for x in (out / "discovered" / "excavator").iterdir()) == ["b.jpg", "c.jpg"]
    assert not (out / "ocean").exists()
    r2 = c.post("/api/export/categories", json={"categories": [], "discovered": ["excavator"], "mode": "symlink"})
    assert r2.status_code == 200 and r2.json()["total"] == 2
    assert _wait_export(c)["error"] is None
    assert c.post("/api/export/categories", json={"categories": [], "discovered": [], "mode": "symlink"}).status_code == 400
    assert c.post("/api/export/categories", json={"categories": [], "discovered": None, "mode": "symlink"}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_endpoint_writes_a_drone_folder_when_asked(tmp_path, tmp_path_factory):
    """drone: true puts every aerial row (whatever its kind or category) under categories/drone/ as well as
    in its own category folder; the drone tile alone is a valid request; the source is only read."""
    from test_export import _two_category_shoot
    from photosort import db as db_mod
    before = _two_category_shoot(tmp_path)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET aerial=1 WHERE rel IN ('a.jpg', 'c.jpg')"); conn.commit()    # beach + unclassified
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach"], "mode": "symlink", "drone": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 3}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 3 and p["failed"] == 0
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg"]
    assert sorted(x.name for x in (out / "drone").iterdir()) == ["a.jpg", "c.jpg"]
    assert not (out / "ocean").exists()
    r2 = c.post("/api/export/categories", json={"categories": [], "drone": True, "mode": "symlink"})
    assert r2.status_code == 200 and r2.json()["total"] == 2
    assert _wait_export(c)["error"] is None
    r3 = c.post("/api/export/categories", json={"categories": ["beach"], "mode": "symlink"})
    assert r3.json()["total"] == 1 and _wait_export(c)["error"] is None                 # default: no drone folder
    assert c.post("/api/export/categories", json={"categories": [], "drone": False, "mode": "symlink"}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_endpoint_preflight_and_lock(tmp_path, tmp_path_factory, monkeypatch):
    import photosort.server as srv
    from test_export import _two_category_shoot
    before = _two_category_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/export/categories", json={"categories": None, "mode": "copy"})
    assert r.status_code == 400 and "on that disk" in r.json()["detail"]
    c.app.state.photosort["export"]["running"] = True
    try:
        assert c.post("/api/export/categories", json={"categories": None, "mode": "symlink"}).status_code == 409
    finally:
        c.app.state.photosort["export"]["running"] = False
    assert c.post("/api/export/categories", json={"categories": None, "mode": "symlink"}).status_code == 200
    assert _wait_export(c)["error"] is None
    assert sorted(os.listdir(tmp_path)) == before


# named people: save a reference, list, show, rename, delete, export per person

def _named_shoot(tmp_path, monkeypatch):
    """Three fake people, references saved for two of them (Arya twice, Priest once)."""
    from test_people import _fake_shoot, _two_named_people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = sorted(os.listdir(tmp_path))
    _two_named_people(tmp_path, monkeypatch, conn)
    return conn, before


def test_people_references_save_list_show_rename_delete(tmp_path, monkeypatch):
    from test_people import _fake_shoot, _person_reference
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = sorted(os.listdir(tmp_path))
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/people/references").json() == {"people": []}
    ref = _person_reference(conn, 0)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    r = c.post("/api/people/references", json={"name": "Arya", "path": str(tmp_path / "p0_0.jpg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["id"], int) and body["name"] == "Arya" and body["faces_in_reference"] == 1
    assert c.post("/api/people/references", json={"name": "  ", "path": str(tmp_path / "p0_0.jpg")}).status_code == 400
    assert c.post("/api/people/references", json={"name": "X", "path": "/nope.jpg"}).status_code == 400
    monkeypatch.setattr(people, "_reference_faces", lambda path: [])
    r = c.post("/api/people/references", json={"name": "X", "path": str(tmp_path / "p0_0.jpg")})
    assert r.status_code == 400 and "no face" in r.json()["detail"]
    tiny = _person_reference(conn, 0, w=20, h=20)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [tiny])
    r = c.post("/api/people/references", json={"name": "X", "path": str(tmp_path / "p0_0.jpg")})
    assert r.status_code == 400 and "too small" in r.json()["detail"]
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    for bad in (".", ".."):
        r = c.post("/api/people/references", json={"name": bad, "path": str(tmp_path / "p0_0.jpg")})
        assert r.status_code == 400 and "cannot be used as a folder" in r.json()["detail"], bad
    monkeypatch.delattr(people, "_reference_faces")   # restore the real one: a byte stub is unreadable
    r = c.post("/api/people/references", json={"name": "X", "path": str(tmp_path / "p0_0.jpg")})
    assert r.status_code == 400 and "could not read" in r.json()["detail"]

    lst = c.get("/api/people/references").json()["people"]
    assert lst == [{"name": "Arya", "reference_ids": [body["id"]], "sources": [str(tmp_path / "p0_0.jpg")], "count": 4}]
    assert c.get("/api/people/references", params={"min_sim": 0.99}).json()["people"][0]["count"] == 0

    r = c.post("/api/people/references/Arya/find", json={})
    assert r.status_code == 200, r.text
    found = r.json()
    assert found["total"] == 4 and len(found["results"]) == 4
    assert all(x["rel"].startswith("p0_") and "qhash" in x for x in found["results"])
    scores = [x["score"] for x in found["results"]]
    assert scores == sorted(scores, reverse=True)
    assert c.post("/api/people/references/Arya/find", json={"min_sim": 0.99}).json()["total"] == 0
    assert c.post("/api/people/references/Nobody/find", json={}).status_code == 404

    r = c.post("/api/people/references/Arya/rename", json={"name": "Arya Mehta"})
    assert r.status_code == 200, r.text
    assert [p["name"] for p in c.get("/api/people/references").json()["people"]] == ["Arya Mehta"]
    assert c.post("/api/people/references/Arya/rename", json={"name": "Z"}).status_code == 404
    assert c.post("/api/people/references/Arya%20Mehta/rename", json={"name": " "}).status_code == 400
    for bad in (".", ".."):
        r = c.post("/api/people/references/Arya%20Mehta/rename", json={"name": bad})
        assert r.status_code == 400 and "cannot be used as a folder" in r.json()["detail"], bad
    assert [p["name"] for p in c.get("/api/people/references").json()["people"]] == ["Arya Mehta"]   # rename refused, unchanged
    assert c.post("/api/people/references/Arya%20Mehta/find", json={}).json()["total"] == 4

    assert c.delete("/api/people/references/" + str(body["id"])).status_code == 200
    assert c.delete("/api/people/references/" + str(body["id"])).status_code == 404
    assert c.get("/api/people/references").json() == {"people": []}
    assert sorted(os.listdir(tmp_path)) == before


def test_people_references_list_sorted_by_count_and_no_folder(tmp_path, monkeypatch):
    assert TestClient(create_app(None)).get("/api/people/references").json() == {"people": []}
    assert TestClient(create_app(None)).post("/api/people/references", json={"name": "A", "path": "/x.jpg"}).status_code == 400
    conn, before = _named_shoot(tmp_path, monkeypatch)
    conn.execute("DELETE FROM faces WHERE photo_id IN (SELECT id FROM photos WHERE rel='p0_3.jpg')"); conn.commit()
    c = TestClient(create_app(tmp_path))
    lst = c.get("/api/people/references").json()["people"]
    assert [(p["name"], p["count"], len(p["reference_ids"])) for p in lst] == [("Priest", 4, 1), ("Arya", 3, 2)]
    assert sorted(os.listdir(tmp_path)) == before


def test_export_references_endpoint_runs_to_completion(tmp_path, tmp_path_factory, monkeypatch):
    conn, before = _named_shoot(tmp_path, monkeypatch)
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/references", json={"names": None, "mode": "copy", "include_raw": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 8}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 8 and p["total"] == 8 and p["failed"] == 0 and p["skipped"] == 0
    out = disk.resolve() / tmp_path.resolve().name / "people"
    assert Path(p["path"]) == out
    assert sorted(x.name for x in (out / "Arya").iterdir()) == [f"p0_{j}.jpg" for j in range(4)]
    assert sorted(x.name for x in (out / "Priest").iterdir()) == [f"p1_{j}.jpg" for j in range(4)]
    assert all((out / "Arya" / f).is_file() and not (out / "Arya" / f).is_symlink() for f in os.listdir(out / "Arya"))
    assert not (out / "failed.txt").exists()
    # a re-export, even under a different mode, finds Priest's photos already there and skips
    # them rather than failing or duplicating
    r2 = c.post("/api/export/references", json={"names": ["Priest"], "mode": "symlink"})
    assert r2.json()["total"] == 4
    p2 = _wait_export(c)
    assert p2["error"] is None and p2["skipped"] == 4
    assert len(os.listdir(out / "Priest")) == 4 and not any(x.is_symlink() for x in (out / "Priest").iterdir())
    for bad in [{"names": ["Nobody"]}, {"names": []}, {"names": ["Arya"], "min_sim": 0.99}]:
        r = c.post("/api/export/references", json=dict(bad, mode="symlink"))
        assert r.status_code == 400, bad
    assert c.post("/api/export/references", json={"names": None, "mode": "csv"}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


def test_export_references_endpoint_preflight_and_lock(tmp_path, tmp_path_factory, monkeypatch):
    import photosort.server as srv
    conn, before = _named_shoot(tmp_path, monkeypatch)
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/export/references", json={"names": None, "mode": "copy"})
    assert r.status_code == 400 and "on that disk" in r.json()["detail"]
    c.app.state.photosort["export"]["running"] = True
    try:
        assert c.post("/api/export/references", json={"names": None, "mode": "symlink"}).status_code == 409
    finally:
        c.app.state.photosort["export"]["running"] = False
    assert c.post("/api/export/references", json={"names": None, "mode": "symlink"}).status_code == 200
    p = _wait_export(c)
    assert p["error"] is None and p["skipped"] == 0
    assert TestClient(create_app(None)).post("/api/export/references", json={"names": None}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


# index bundles: pack the index into one zip, install one on another Mac

def test_bundle_export_endpoint_runs_to_completion(tmp_path, tmp_path_factory):
    from photosort.bundle import inspect_bundle
    c = _shoot_client(tmp_path, n=3)
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/bundle/export")
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 6, "dest": str(disk.resolve())}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 6 and p["total"] == 6 and p["failed"] == 0
    z = Path(p["path"])
    assert z == disk.resolve() / f"{tmp_path.resolve().name}.photosort-index.zip" and z.is_file()
    assert inspect_bundle(z)["photos"] == 3
    assert TestClient(create_app(None)).post("/api/bundle/export").status_code == 400
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg", "p2.jpg"]


def test_bundle_export_409_while_busy_and_preflight(tmp_path, tmp_path_factory, monkeypatch):
    import photosort.server as srv
    c = _shoot_client(tmp_path, n=2)
    st = c.app.state.photosort
    st["export"]["running"] = True
    try:
        assert c.post("/api/bundle/export").status_code == 409
    finally:
        st["export"]["running"] = False
    st["running"] = True
    try:
        assert c.post("/api/bundle/export").status_code == 409
    finally:
        st["running"] = False
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/bundle/export")
    assert r.status_code == 400 and "free" in r.json()["detail"]
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg"]


def test_bundle_export_dest_lands_there_and_refuses_the_shoot(tmp_path, tmp_path_factory):
    """Save scan file anywhere: {dest} picks the folder; the shoot root, anything under it, and a
    non-folder are refused before anything runs."""
    from photosort.bundle import inspect_bundle
    c = _shoot_client(tmp_path, n=2)
    before = sorted(os.listdir(tmp_path))
    r = c.post("/api/bundle/export", json={"dest": str(tmp_path)})
    assert r.status_code == 400 and "inside the source folder" in r.json()["detail"]
    r = c.post("/api/bundle/export", json={"dest": str(tmp_path / "sub")})
    assert r.status_code == 400
    r = c.post("/api/bundle/export", json={"dest": str(tmp_path_factory.mktemp("x") / "missing")})
    assert r.status_code == 400 and "not a directory" in r.json()["detail"]
    assert c.get("/api/export/progress").json()["running"] is False
    dest = tmp_path_factory.mktemp("anywhere")
    r = c.post("/api/bundle/export", json={"dest": str(dest)})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 4, "dest": str(dest.resolve())}
    p = _wait_export(c)
    assert p["error"] is None and p["what"] == "bundle"
    z = Path(p["path"])
    assert z == dest.resolve() / f"{tmp_path.resolve().name}.photosort-index.zip" and z.is_file()
    assert inspect_bundle(z)["photos"] == 2
    assert sorted(os.listdir(tmp_path)) == before


def test_bundle_export_choose_runs_the_picker_then_exports(tmp_path, tmp_path_factory, monkeypatch):
    import subprocess as sp
    c = _shoot_client(tmp_path, n=1)
    before = sorted(os.listdir(tmp_path))
    scripts = []
    def fake_run(cmd, *a, **k):
        scripts.append(cmd[-1])
        return sp.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
    monkeypatch.setattr("photosort.server.subprocess.run", fake_run)
    assert c.post("/api/bundle/export/choose").status_code == 204          # cancelled: nothing started
    from photosort.config import export_root
    assert "choose folder" in scripts[-1] and f'default location (POSIX file "{export_root()}")' in scripts[-1]
    assert c.get("/api/export/progress").json()["running"] is False
    # an export destination on a disk that is not there must not block the picker (it opens on the Desktop folder)
    gone = tmp_path_factory.mktemp("gone") / "disk"; gone.mkdir()
    assert c.post("/api/export/destination", json={"path": str(gone)}).status_code == 200
    gone.rmdir()
    assert c.get("/api/export/destination").json()["mounted"] is False
    assert c.post("/api/bundle/export/choose").status_code == 204
    assert c.delete("/api/export/destination").status_code == 200
    # a pick inside the shoot is refused with bundle_path's message
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(tmp_path) + "\n", stderr=""))
    r = c.post("/api/bundle/export/choose")
    assert r.status_code == 400 and "inside the source folder" in r.json()["detail"]
    dest = tmp_path_factory.mktemp("picked")
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(dest) + "\n", stderr=""))
    r = c.post("/api/bundle/export/choose")
    assert r.status_code == 200 and r.json()["dest"] == str(dest.resolve())
    p = _wait_export(c)
    assert p["error"] is None and Path(p["path"]).is_file() and Path(p["path"]).parent == dest.resolve()
    assert TestClient(create_app(None)).post("/api/bundle/export/choose").status_code == 400
    assert sorted(os.listdir(tmp_path)) == before


def test_reveal_only_for_paths_that_exist(tmp_path, monkeypatch):
    import subprocess as sp
    c = _shoot_client(tmp_path, n=1)
    calls = []
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda cmd, *a, **k: (calls.append(cmd), sp.CompletedProcess(cmd, returncode=0, stdout="", stderr=""))[1])
    assert c.post("/api/reveal", json={"path": str(tmp_path / "nope.zip")}).status_code == 400
    assert calls == []
    r = c.post("/api/reveal", json={"path": str(tmp_path / "p0.jpg")})
    assert r.status_code == 200 and calls[0][:2] == ["open", "-R"]


def test_folder_info_says_whether_the_disk_is_there(tmp_path, tmp_path_factory):
    c = _shoot_client(tmp_path, n=1)
    f = c.get("/api/folder").json()
    assert f["mounted"] is True and f["disk"] == tmp_path.resolve().name
    assert c.get("/api/stats").json()["mounted"] is True
    assert TestClient(create_app(None)).get("/api/folder").json() == {"root": None, "name": None, "indexed": False, "mounted": False, "disk": None}
    # the disk got unplugged: the index is still there, the folder is not
    disk = tmp_path_factory.mktemp("disk") / "shoot"; disk.mkdir()
    from conftest import make_image
    make_image(disk, "a.jpg")
    index_folder(disk, faces=False, workers=1, embed=False)
    c2 = TestClient(create_app(disk))
    import shutil as sh
    sh.rmtree(disk)
    f = c2.get("/api/folder").json()
    assert f["root"] == str(disk) and f["indexed"] is True and f["mounted"] is False and f["disk"] == "shoot"
    assert c2.get("/api/stats").json()["mounted"] is False


def test_bundle_import_endpoint_switches_to_the_shoot(tmp_path, tmp_path_factory):
    from photosort.bundle import export_bundle
    c = _shoot_client(tmp_path, n=3)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    other = TestClient(create_app(None))                                 # a second app with no folder open
    r = other.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path)})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["imported"] is True and info["root"] == str(tmp_path.resolve()) and info["indexed"] is True
    assert info["name"] == tmp_path.name and info["photos"] == 3
    assert other.get("/api/stats").json()["photos"] == 3
    assert other.get("/api/folder").json()["root"] == str(tmp_path.resolve())
    assert len(other.get("/api/search").json()["results"]) == 3
    # root omitted: the bundle's own root, which is a directory on this Mac
    r2 = other.post("/api/bundle/import", json={"zip": str(z)})
    assert r2.status_code == 200 and r2.json()["imported"] is True
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg", "p2.jpg"]


def test_bundle_import_endpoint_400s_and_409s(tmp_path, tmp_path_factory):
    import zipfile
    from photosort.bundle import export_bundle
    c = _shoot_client(tmp_path, n=1)
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out)
    plain = out / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("hello.txt", "hi")
    r = c.post("/api/bundle/import", json={"zip": str(plain), "root": str(tmp_path)})
    assert r.status_code == 400 and "not a photosort index bundle" in r.json()["detail"]
    r = c.post("/api/bundle/import", json={"zip": str(out / "missing.zip"), "root": str(tmp_path)})
    assert r.status_code == 400
    r = c.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path / "nope")})
    assert r.status_code == 400 and "not a directory" in r.json()["detail"]
    st = c.app.state.photosort
    st["export"]["running"] = True
    try:
        assert c.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path)}).status_code == 409
        assert c.post("/api/bundle/import/choose").status_code == 409
        assert c.post("/api/bundle/import/choose-root", json={"zip": str(z)}).status_code == 409
    finally:
        st["export"]["running"] = False
    st["running"] = True
    try:
        assert c.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path)}).status_code == 409
    finally:
        st["running"] = False
    assert c.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path)}).status_code == 200
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_bundle_import_choose_imports_or_asks_for_the_root(tmp_path, tmp_path_factory, monkeypatch):
    import json as js
    import subprocess as sp
    import zipfile
    from photosort.bundle import export_bundle
    c = _shoot_client(tmp_path, n=2)
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out)
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=1, stdout="", stderr=""))
    assert c.post("/api/bundle/import/choose").status_code == 204
    assert c.post("/api/bundle/import/choose-root", json={"zip": str(z)}).status_code == 204
    # the bundle's root is here: imported straight away
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(z) + "\n", stderr=""))
    info = c.post("/api/bundle/import/choose").json()
    assert info["imported"] is True and info["root"] == str(tmp_path.resolve()) and info["photos"] == 2
    # the bundle's root is not here: nothing installed, the UI must ask for the folder
    away = out / "away.photosort-index.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(away, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "bundle.json":
                b = js.loads(data); b["root"] = "/Volumes/not-here-photosort-test/shoot"; data = js.dumps(b).encode()
            dst.writestr(item, data)
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(away) + "\n", stderr=""))
    r = c.post("/api/bundle/import/choose").json()
    assert r["needs_root"] is True and r["zip"] == str(away) and r["bundle"]["photos"] == 2
    assert r["bundle"]["root"] == "/Volumes/not-here-photosort-test/shoot"
    from photosort.config import app_home, shoot_slug
    assert not (app_home() / shoot_slug(Path("/Volumes/not-here-photosort-test/shoot"))).exists()
    # second step: the folder picker names the folder, then it is imported under that root
    here = tmp_path_factory.mktemp("disk") / "shoot"; here.mkdir()
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(here) + "\n", stderr=""))
    r2 = c.post("/api/bundle/import/choose-root", json={"zip": str(away)}).json()
    assert r2["imported"] is True and r2["root"] == str(here.resolve()) and r2["photos"] == 2
    assert c.get("/api/folder").json()["root"] == str(here.resolve())
    assert c.get("/api/stats").json()["photos"] == 2
    # a picked zip that is not a bundle is a 400, not a crash
    plain = out / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("hello.txt", "hi")
    monkeypatch.setattr("photosort.server.subprocess.run",
                        lambda *a, **k: sp.CompletedProcess(a, returncode=0, stdout=str(plain) + "\n", stderr=""))
    assert c.post("/api/bundle/import/choose").status_code == 400
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg"]
    assert sorted(os.listdir(here)) == []


def test_export_destination_above_source_is_400(tmp_path):
    c = _shoot_client(tmp_path)
    r = c.post("/api/export/destination", json={"path": str(tmp_path.parent)})
    assert r.status_code == 400 and "contains the source folder" in r.json()["detail"]
    assert c.get("/api/export/destination").json()["default"] is True
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_bundle_import_with_an_unreadable_index_is_400_and_installs_nothing(tmp_path, tmp_path_factory):
    import json as js
    import zipfile
    from photosort.config import app_home, shoot_slug
    c = _shoot_client(tmp_path, n=1)
    other = tmp_path_factory.mktemp("disk") / "shoot"; other.mkdir()          # a root with no index yet
    out = tmp_path_factory.mktemp("out")
    bad = out / "bad.photosort-index.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("bundle.json", js.dumps({"format": "photosort-index/1", "root": str(other), "name": "shoot", "photos": 1}))
        zf.writestr("index.db", os.urandom(4096))                              # not SQLite
    r = c.post("/api/bundle/import", json={"zip": str(bad), "root": str(other)})
    assert r.status_code == 400 and "not readable" in r.json()["detail"]
    assert not (app_home() / shoot_slug(other)).exists()
    assert not list(app_home().glob(".*import*"))
    assert c.get("/api/folder").json()["root"] == str(tmp_path.resolve())      # still on the old shoot
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"] and sorted(os.listdir(other)) == []


# videos: same grid, same search, plus the original media, the segment strip and a kind filter

def _video_shoot_client(tmp_path, tmp_path_factory):
    """a.jpg (beach), b.jpg (ocean), clip.mp4 (two scenes, beach; segment 0 beach, segment 1 ocean)."""
    from conftest import make_image, make_video
    from photosort import db
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET category='beach' WHERE rel IN ('a.jpg', 'clip.mp4')")
    conn.execute("UPDATE photos SET category='ocean' WHERE rel='b.jpg'")
    vid = conn.execute("SELECT id FROM photos WHERE rel='clip.mp4'").fetchone()[0]
    conn.execute("UPDATE segments SET category='beach', category_score=0.9 WHERE photo_id=? AND idx=0", (vid,))
    conn.execute("UPDATE segments SET category='ocean', category_score=0.8 WHERE photo_id=? AND idx=1", (vid,))
    conn.commit()
    return TestClient(create_app(tmp_path)), vid, sorted(os.listdir(tmp_path))


def test_search_carries_kind_and_duration_and_filters_by_kind(tmp_path, tmp_path_factory):
    from conftest import needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    c, vid, before = _video_shoot_client(tmp_path, tmp_path_factory)
    res = c.get("/api/search").json()["results"]
    by_rel = {r["rel"]: r for r in res}
    assert sorted(by_rel) == ["a.jpg", "b.jpg", "clip.mp4"]
    assert by_rel["a.jpg"]["kind"] == "photo" and by_rel["a.jpg"]["duration"] is None
    assert by_rel["clip.mp4"]["kind"] == "video" and abs(by_rel["clip.mp4"]["duration"] - 10.0) < 0.2
    assert [r["rel"] for r in c.get("/api/search", params={"kind": "videos"}).json()["results"]] == ["clip.mp4"]
    assert sorted(r["rel"] for r in c.get("/api/search", params={"kind": "photos"}).json()["results"]) == ["a.jpg", "b.jpg"]
    assert c.get("/api/search/ids", params={"kind": "videos"}).json() == {"ids": [vid], "total": 1}
    assert sorted(r["rel"] for r in c.get("/api/search", params={"category": "beach"}).json()["results"]) == ["a.jpg", "clip.mp4"]
    st = c.get("/api/stats").json()
    assert st["photos"] == 2 and st["videos"] == 1
    assert sorted(os.listdir(tmp_path)) == before


def test_media_endpoint_serves_the_original_and_404s_when_gone(tmp_path, tmp_path_factory):
    from conftest import needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    c, vid, before = _video_shoot_client(tmp_path, tmp_path_factory)
    r = c.get(f"/api/media/{vid}")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert len(r.content) == (tmp_path / "clip.mp4").stat().st_size
    part = c.get(f"/api/media/{vid}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    a_id = next(r["id"] for r in c.get("/api/search").json()["results"] if r["rel"] == "a.jpg")
    assert c.get(f"/api/media/{a_id}").headers["content-type"] == "image/jpeg"
    (tmp_path / "a.jpg").rename(tmp_path.parent / "a_parked.jpg")
    try:
        assert c.get(f"/api/media/{a_id}").status_code == 404
    finally:
        (tmp_path.parent / "a_parked.jpg").rename(tmp_path / "a.jpg")
    assert c.get("/api/media/999999").status_code == 404
    assert TestClient(create_app(None)).get("/api/media/1").status_code == 404
    assert sorted(os.listdir(tmp_path)) == before


def test_segments_endpoint_lists_segments_with_working_frames(tmp_path, tmp_path_factory):
    from conftest import needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    c, vid, before = _video_shoot_client(tmp_path, tmp_path_factory)
    segs = c.get(f"/api/segments/{vid}").json()["segments"]
    assert [s["idx"] for s in segs] == [0, 1]
    assert segs[0]["start"] == 0.0 and abs(segs[0]["end"] - 5.0) < 0.2 and abs(segs[1]["end"] - 10.0) < 0.2
    assert [s["category"] for s in segs] == ["beach", "ocean"] and segs[0]["score"] == 0.9
    for s in segs:
        assert s["frame_url"].startswith("/api/frame/")
        f = c.get(s["frame_url"])
        assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg"
    a_id = next(r["id"] for r in c.get("/api/search").json()["results"] if r["rel"] == "a.jpg")
    assert c.get(f"/api/segments/{a_id}").json() == {"segments": []}
    assert c.get("/api/segments/999999").status_code == 404
    assert c.get("/api/frame/nope.jpg").status_code == 404
    assert c.get("/api/frame/..%2Findex.db").status_code == 404
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_with_video_segments(tmp_path, tmp_path_factory):
    from conftest import needs_ffmpeg
    from photosort.video import probe
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    c, vid, before = _video_shoot_client(tmp_path, tmp_path_factory)
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    assert c.post("/api/export/categories", json={"categories": ["beach"], "videos": "nope"}).status_code == 400
    # whole clips (the default): the video lands next to the photo under beach/
    r = c.post("/api/export/categories", json={"categories": ["beach"], "mode": "copy"})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 2}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 2 and p["failed"] == 0
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg", "clip.mp4"]
    # only the matching segments: beach gets segment 0 trimmed, ocean (segment 1 only, clip not in ocean) nothing
    disk2 = tmp_path_factory.mktemp("disk2")
    assert c.post("/api/export/destination", json={"path": str(disk2)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean"], "mode": "symlink", "videos": "segments"})
    assert r.status_code == 200, r.text
    p = _wait_export(c)
    assert p["error"] is None and p["failed"] == 0 and p["done"] == p["total"] == 3
    out2 = disk2.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out2 / "beach").iterdir()) == ["a.jpg", "clip_00_0.0-5.0.mp4"]
    assert (out2 / "beach" / "a.jpg").is_symlink() and not (out2 / "beach" / "clip_00_0.0-5.0.mp4").is_symlink()
    assert abs(probe(out2 / "beach" / "clip_00_0.0-5.0.mp4")["duration"] - 5.0) < 0.5
    assert sorted(x.name for x in (out2 / "ocean").iterdir()) == ["b.jpg"]
    assert sorted(os.listdir(tmp_path)) == before


# merge suggestions: same person? yes merges, no is remembered

def test_people_suggestions_merge_and_reject(tmp_path, monkeypatch):
    from test_people import _two_close_people
    from photosort import people as pm
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 1.0)
    monkeypatch.setattr(pm, "FACE_MERGE_SUGGEST_SIM", 0.45)
    conn, listing = _two_close_people(tmp_path, gap=0.40)
    c = TestClient(create_app(tmp_path))
    a, b = c.post("/api/people/cluster", json={"eps": 0.2}).json()
    body = c.get("/api/people/suggestions").json()
    assert set(body) == {"suggestions"} and len(body["suggestions"]) == 1
    s = body["suggestions"][0]
    assert {s["a"], s["b"]} == {a["id"], b["id"]} and 0.45 <= s["sim"] <= 1.0
    assert set(s) == {"a", "b", "sim", "rank", "a_name", "b_name", "a_n", "b_n", "a_cover", "b_cover", "a_faces", "b_faces"}
    assert s["a_cover"]["qhash"] == a["cover_qhash"] and s["a_cover"]["box"] == a["cover_box"] and len(s["a_faces"]) == 3
    # no: remembered, the pair is gone
    assert c.post("/api/people/reject", json={"a": a["id"], "b": b["id"]}).json() == {"ok": True}
    assert c.get("/api/people/suggestions").json()["suggestions"] == []
    # yes anyway: faces move, the dropped person is gone, the same link replaces the different one
    r = c.post("/api/people/merge", json={"keep": a["id"], "drop": b["id"]})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["id"] == a["id"] and merged["n"] == 8 and merged["cover_qhash"] == a["cover_qhash"]
    assert [p["id"] for p in c.get("/api/people").json()] == [a["id"]]
    assert c.get("/api/people/suggestions").json()["suggestions"] == []
    # a re-cluster at the default eps keeps the yes
    again = c.post("/api/people/cluster", json={}).json()
    assert len(again) == 1 and again[0]["n"] == 8
    assert sorted(x.name for x in tmp_path.iterdir()) == listing


def test_people_merge_and_reject_400_on_bad_ids_and_409_while_indexing(tmp_path):
    from test_people import _two_close_people
    _two_close_people(tmp_path, gap=0.40)
    c = TestClient(create_app(tmp_path))
    a, b = c.post("/api/people/cluster", json={"eps": 0.2}).json()
    assert c.post("/api/people/merge", json={"keep": a["id"], "drop": a["id"]}).status_code == 400
    assert c.post("/api/people/merge", json={"keep": a["id"], "drop": 9999}).status_code == 400
    assert c.post("/api/people/reject", json={"a": b["id"], "b": b["id"]}).status_code == 400
    assert c.post("/api/people/reject", json={"a": 9999, "b": b["id"]}).status_code == 400
    assert len(c.get("/api/people").json()) == 2
    c.app.state.photosort["running"] = True
    try:
        assert c.get("/api/people/suggestions").status_code == 409
        assert c.post("/api/people/merge", json={"keep": a["id"], "drop": b["id"]}).status_code == 409
        assert c.post("/api/people/reject", json={"a": a["id"], "b": b["id"]}).status_code == 409
    finally:
        c.app.state.photosort["running"] = False
    none = TestClient(create_app(None))
    assert none.get("/api/people/suggestions").json() == {"suggestions": []}
    assert none.post("/api/people/merge", json={"keep": 1, "drop": 2}).status_code == 400
    assert none.post("/api/people/reject", json={"a": 1, "b": 2}).status_code == 400


# Reorganise disk: the guarded exception to the read-only root, plan then apply with the folder name typed back

def test_reorganise_plan_apply_status_undo_cycle(tmp_path):
    from test_reorganise import _shoot, _listing, EXPECTED
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/reorganise/status").json() == {"reorganised": False, "moves": 0, "created": None}
    r = c.post("/api/reorganise/plan", json={"by_people": False})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["moves"] == 9 and plan["photos"] == 2 and plan["videos"] == 1 and plan["drone"] == 2 and plan["plan_id"]
    assert {m["src_rel"]: m["dst_rel"] for m in plan["sample"]} == EXPECTED
    assert _listing(tmp_path) == before
    # apply: the folder name must be typed back exactly, and the plan must be this session's
    assert c.post("/api/reorganise/apply", json={"plan_id": plan["plan_id"], "confirm": "nope"}).status_code == 400
    assert c.post("/api/reorganise/apply", json={"plan_id": plan["plan_id"], "confirm": tmp_path.name.upper()}).status_code == 400
    r = c.post("/api/reorganise/apply", json={"plan_id": "stale-id", "confirm": tmp_path.name})
    assert r.status_code == 400 and r.json()["detail"] == "run the plan first"
    assert _listing(tmp_path) == before
    r = c.post("/api/reorganise/apply", json={"plan_id": plan["plan_id"], "confirm": tmp_path.name})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 9, "what": "reorganise"}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 9 and p["total"] == 9 and p["failed"] == 0 and p["what"] == "reorganise"
    assert Path(p["path"]) == tmp_path / "sorted"
    assert (tmp_path / "sorted" / "photos" / "beach" / "a.jpg").is_file() and (tmp_path / "sorted" / "UNDO.json").is_file()
    st = c.get("/api/reorganise/status").json()
    assert st["reorganised"] is True and st["moves"] == 9 and st["created"]
    # the search index follows the files without a re-index, and the original is still served
    res = {x["id"]: x["rel"] for x in c.get("/api/search").json()["results"]}
    assert res[ids["b.jpg"]] == "sorted/photos/sunset/b.jpg"
    assert c.get(f"/api/media/{ids['b.jpg']}").status_code == 200
    # a second plan is refused while the previous run is not undone
    r = c.post("/api/reorganise/plan", json={"by_people": False})
    assert r.status_code == 400 and r.json()["detail"] == "undo the previous reorganise first"
    r = c.post("/api/reorganise/undo")
    assert r.status_code == 200 and r.json() == {"started": True, "total": 9, "what": "undo"}
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 9 and p["failed"] == 0 and p["what"] == "undo"
    assert _listing(tmp_path) == before and not (tmp_path / "sorted").exists()
    assert c.get("/api/reorganise/status").json() == {"reorganised": False, "moves": 0, "created": None}
    assert c.post("/api/reorganise/undo").status_code == 400
    res = {x["id"]: x["rel"] for x in c.get("/api/search").json()["results"]}
    assert res[ids["b.jpg"]] == "b.jpg"


def test_reorganise_plan_400_on_a_guard_and_409_while_busy(tmp_path):
    from test_reorganise import _shoot, _listing
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    c = TestClient(create_app(tmp_path))
    (tmp_path / "DCIM").mkdir()
    r = c.post("/api/reorganise/plan", json={"by_people": True})
    assert r.status_code == 400 and r.json()["detail"] == "this looks like a camera card, copy it to a disk first"
    (tmp_path / "DCIM").rmdir()
    plan = c.post("/api/reorganise/plan", json={"by_people": True}).json()
    assert plan["moves"] == 9
    c.app.state.photosort["export"]["running"] = True
    try:
        assert c.post("/api/reorganise/plan", json={"by_people": False}).status_code == 409
        assert c.post("/api/reorganise/apply", json={"plan_id": plan["plan_id"], "confirm": tmp_path.name}).status_code == 409
        assert c.post("/api/reorganise/undo").status_code == 409
    finally:
        c.app.state.photosort["export"]["running"] = False
    c.app.state.photosort["running"] = True
    try:
        assert c.post("/api/reorganise/plan", json={"by_people": False}).status_code == 409
    finally:
        c.app.state.photosort["running"] = False
    assert _listing(tmp_path) == before
    none = TestClient(create_app(None))
    assert none.post("/api/reorganise/plan", json={"by_people": False}).status_code == 400
    assert none.get("/api/reorganise/status").status_code == 400

# the on-demand focus pass

def _wait_focus(c, n=200):
    import time
    for _ in range(n):
        p = c.get("/api/focus/progress").json()
        if not p["running"]: return p
        time.sleep(0.05)
    return p


def _focus_shoot(tmp_path):
    from conftest import make_image
    for i in range(6):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    make_image(tmp_path, "b.jpg", kind="blurry", seed=9)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    return sorted(os.listdir(tmp_path))


def test_focus_job_status_progress_and_hide_bad(tmp_path):
    before = _focus_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/focus/status").json() == {"checked": 0, "unchecked": 7, "bad": 0, "soft": 0}
    assert c.get("/api/stats").json()["focus"] == {"checked": 0, "bad": 0, "soft": 0}
    assert [r["focus"] for r in c.get("/api/search").json()["results"]] == [None] * 7
    r = c.post("/api/focus")
    assert r.status_code == 200 and r.json() == {"started": True, "total": 7}
    p = _wait_focus(c)
    assert p["running"] is False and p["error"] is None and p["done"] == p["total"] == 7
    assert p["counts"] == {"ok": 6, "soft": 0, "bad": 1, "checked": 7}
    st = c.get("/api/focus/status").json()
    assert st == {"checked": 7, "unchecked": 0, "bad": 1, "soft": 0}
    assert c.get("/api/stats").json()["focus"] == {"checked": 7, "bad": 1, "soft": 0}
    res = c.get("/api/search").json()["results"]
    assert {r["rel"]: r["focus"] for r in res}["b.jpg"] == "bad" and len(res) == 7
    hidden = c.get("/api/search", params={"hide_bad": 1}).json()
    assert hidden["total"] == 6 and "b.jpg" not in {r["rel"] for r in hidden["results"]}
    assert c.get("/api/search/ids", params={"hide_bad": 1}).json()["total"] == 6
    assert c.get("/api/search/ids", params={"hide_soft": 1}).json()["total"] <= 6
    # a second run has nothing left to check and is not an error
    assert c.post("/api/focus").json() == {"started": True, "total": 0}
    assert _wait_focus(c)["counts"]["checked"] == 0
    assert sorted(os.listdir(tmp_path)) == before


def test_focus_409_while_busy_and_400_without_a_folder(tmp_path, monkeypatch):
    import photosort.server as srv
    _focus_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    s = c.app.state.photosort
    for key, val in (("running", True), ("classify", {"running": True, "counts": {}, "discovered": {}, "error": None})):
        old = s[key]; s[key] = val
        try:
            assert c.post("/api/focus").status_code == 409
        finally:
            s[key] = old
    s["export"]["running"] = True
    try:
        assert c.post("/api/focus").status_code == 409
    finally:
        s["export"]["running"] = False
    # two rapid starts: the second is a 409, the first finishes
    import threading
    gate = threading.Event()
    real = srv.focus_mod.check_focus
    def slow(root, only_unchecked=True, progress=None):
        gate.wait(5); return real(root, only_unchecked=only_unchecked, progress=progress)
    monkeypatch.setattr(srv.focus_mod, "check_focus", slow)
    assert c.post("/api/focus").status_code == 200
    assert c.post("/api/focus").status_code == 409
    gate.set()
    assert _wait_focus(c)["error"] is None
    assert TestClient(create_app(None)).post("/api/focus").status_code == 400
    assert TestClient(create_app(None)).get("/api/focus/status").json() == {"checked": 0, "unchecked": 0, "bad": 0, "soft": 0}


def test_focus_failure_is_reported_not_wedged(tmp_path, monkeypatch):
    import photosort.server as srv
    _focus_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    def boom(root, only_unchecked=True, progress=None):
        raise RuntimeError("frames gone")
    monkeypatch.setattr(srv.focus_mod, "check_focus", boom)
    assert c.post("/api/focus").status_code == 200
    p = _wait_focus(c)
    assert p["running"] is False and "frames gone" in p["error"]
    monkeypatch.undo()
    assert c.post("/api/focus").status_code == 200 and _wait_focus(c)["error"] is None


def test_index_never_runs_focus_on_its_own(tmp_path):
    _focus_shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    from conftest import make_image
    make_image(tmp_path, "later.jpg", seed=30)
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    assert c.get("/api/focus/status").json()["checked"] == 0
    assert c.get("/api/focus/progress").json()["running"] is False


def test_export_categories_hide_bad(tmp_path, tmp_path_factory):
    from test_export import _two_category_shoot
    from photosort import db as db_mod
    before = _two_category_shoot(tmp_path)
    conn = db_mod.connect(tmp_path)
    conn.execute("UPDATE photos SET focus='bad' WHERE rel='a.jpg'"); conn.execute("UPDATE photos SET aerial=1"); conn.commit()
    c = TestClient(create_app(tmp_path))
    disk = tmp_path_factory.mktemp("disk")
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean"], "mode": "symlink", "drone": True})
    assert r.json()["total"] == 5 and _wait_export(c)["error"] is None       # default: bad rows still go
    out = disk.resolve() / tmp_path.resolve().name / "categories"
    assert sorted(x.name for x in (out / "beach").iterdir()) == ["a.jpg"]
    import shutil; shutil.rmtree(out)
    r = c.post("/api/export/categories", json={"categories": ["beach", "ocean"], "mode": "symlink", "drone": True, "hide_bad": True})
    assert r.json()["total"] == 3 and _wait_export(c)["error"] is None
    assert not (out / "beach").exists() and sorted(x.name for x in (out / "ocean").iterdir()) == ["b.jpg"]
    assert sorted(x.name for x in (out / "drone").iterdir()) == ["b.jpg", "c.jpg"]
    assert sorted(os.listdir(tmp_path)) == before


# faces are optional at index time and can be added later without a re-index

def test_second_index_with_faces_only_runs_faces(tmp_path, monkeypatch):
    """faces=false first; a later faces=true run touches only n_faces / sharp_eye / sharp and the faces
    table: embeddings, thumbs and the focus label all survive, and nothing is re-decoded. Categorise does
    run again afterwards (the people category is decided from n_faces), so categories are not pinned here;
    tests/test_index.py checks them on the bare index_folder path."""
    from conftest import make_image
    from photosort import db
    import photosort.server as srv
    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET embed=?, focus='ok', focus_score=500", (b"\x00" * 1024,))
    conn.commit()
    classified = []
    monkeypatch.setattr(srv.classify_mod, "classify_and_store", lambda root, people_by_faces=True: classified.append(root) or {})
    monkeypatch.setattr(srv.classify_mod, "discover_and_store", lambda root: {})
    idx = db.index_dir(tmp_path)
    thumbs = {p.name: p.stat().st_mtime_ns for p in (idx / "thumbs").iterdir()}
    c = TestClient(create_app(tmp_path))
    st = c.get("/api/stats").json()
    assert st["faces_pending"] == 3 and st["faces"] == 0
    assert c.post("/api/index", json={"faces": True}).json()["started"]; _wait_idle(c)
    assert c.get("/api/progress").json()["stage"] != "error"
    st = c.get("/api/stats").json()
    assert st["faces_pending"] == 0
    rows = conn.execute("SELECT rel, n_faces, embed, focus, focus_score FROM photos ORDER BY rel").fetchall()
    assert [r["n_faces"] for r in rows] == [0, 0, 0]
    assert all(r["embed"] == b"\x00" * 1024 and r["focus"] == "ok" and r["focus_score"] == 500 for r in rows)
    assert {p.name: p.stat().st_mtime_ns for p in (idx / "thumbs").iterdir()} == thumbs
    assert classified == [tmp_path]          # faces changed, so the people category is decided again
    # and once more with faces on: nothing to do
    assert c.post("/api/index", json={"faces": True}).json()["started"]; _wait_idle(c)
    assert c.get("/api/stats").json()["faces_pending"] == 0
    assert c.get("/api/folder").json()["indexed"] is True
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg", "p2.jpg"]


# Google Drive as a destination: the endpoints ride the export job machinery with what == "drive".
# Everything runs against the fake service from test_drive; never a real sign-in or upload.

def _drive_fake(monkeypatch):
    from test_drive import FakeService, FakeMedia
    from photosort import drive
    svc = FakeService()
    svc.add_folder("root1", "Client delivery")
    monkeypatch.setattr(drive, "_build_service", lambda: svc)
    monkeypatch.setattr(drive, "MediaFileUpload", FakeMedia)
    monkeypatch.setattr(drive, "_sleep", lambda s: None)
    return svc


LINK = "https://drive.google.com/drive/folders/root1?usp=sharing"


def test_drive_status_not_configured(tmp_path):
    c = _shoot_client(tmp_path)
    st = c.get("/api/drive/status").json()
    assert st == {"configured": False, "signed_in": False, "email": None, "client_path": st["client_path"],
                  "signing_in": False, "error": None, "link": None, "web_size": None}
    assert st["client_path"].endswith("google_client.json")
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_drive_inspect_400_on_a_document_link_and_401_when_not_signed_in(tmp_path, monkeypatch):
    from test_drive import fake_token
    c = _shoot_client(tmp_path)
    r = c.post("/api/drive/inspect", json={"link": "https://docs.google.com/document/d/1abcdefghijklmnop/edit"})
    assert r.status_code == 400 and r.json()["detail"] == "that is a document link, paste a folder link"
    r = c.post("/api/drive/inspect", json={"link": LINK})
    assert r.status_code == 401 and r.json()["detail"] == "sign in to Google first"
    fake = _drive_fake(monkeypatch); fake_token("me@example.com")
    fake.limit = 100; fake.usage = 30
    r = c.post("/api/drive/inspect", json={"link": LINK})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "root1", "name": "Client delivery", "owner_email": "owner@example.com", "shared_drive": False,
                        "quota_applies": True, "free_bytes": 70, "link": "https://drive.google.com/drive/folders/root1"}
    r = c.post("/api/drive/inspect", json={"link": "https://drive.google.com/drive/folders/nope123456"})
    assert r.status_code == 400 and "no folder with that id" in r.json()["detail"]
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_drive_export_401_when_not_signed_in(tmp_path, monkeypatch):
    c = _shoot_client(tmp_path)
    pid = c.get("/api/search").json()["results"][0]["id"]
    r = c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": [pid]})
    assert r.status_code == 401 and r.json()["detail"] == "sign in to Google first"
    r = c.post("/api/drive/export", json={"link": "https://drive.google.com/file/d/1abcdefghijklmnop/view", "what": "selection", "ids": [pid]})
    assert r.status_code == 400 and "folder link" in r.json()["detail"]
    r = c.post("/api/drive/export", json={"link": LINK, "what": "everything"})
    assert r.status_code == 400
    assert c.get("/api/export/progress").json()["running"] is False
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_drive_export_runs_to_completion(tmp_path, monkeypatch):
    from test_export import _two_category_shoot
    from test_drive import fake_token
    from photosort import drive
    from photosort import people as people_mod
    before = _two_category_shoot(tmp_path)
    fake = _drive_fake(monkeypatch); fake_token("me@example.com")
    c = TestClient(create_app(tmp_path))
    r = c.post("/api/drive/export", json={"link": LINK, "what": "categories", "categories": ["beach", "ocean"], "include_raw": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "total": 3}
    p = _wait_export(c)
    assert p["error"] is None and p["what"] == "drive" and p["done"] == 3 and p["total"] == 3 and p["failed"] == 0
    assert p["path"] == "https://drive.google.com/drive/folders/root1" and p["failures"] == [] and p["bytes"] > 0
    ups = {u["name"]: u for u in fake.uploads}
    assert sorted(ups) == ["a.ARW", "a.jpg", "b.jpg"]
    def path_of(fid):
        out = []
        while fid:
            rec = fake.records[fid]; out.append(rec["name"]); fid = (rec["parents"] or [None])[0]
        return "/".join(reversed(out))
    assert path_of(ups["a.jpg"]["parent"]) == "Client delivery/categories/beach"
    assert path_of(ups["b.jpg"]["parent"]) == "Client delivery/categories/ocean"
    assert drive.manifest_path(tmp_path, "root1").is_file()
    st = c.get("/api/drive/status").json()
    assert st["signed_in"] and st["email"] == "me@example.com" and st["link"] == LINK and st["web_size"] is None
    # people: the saved names, through the same planner the local people export uses
    ids = {r["rel"]: r["id"] for r in c.get("/api/search").json()["results"]}
    monkeypatch.setattr(people_mod, "export_references_ids", lambda root, names, min_sim: {"Meera": [ids["a.jpg"]], "Ravi": [ids["a.jpg"], ids["c.jpg"]]})
    r = c.post("/api/drive/export", json={"link": LINK, "what": "people", "names": None, "web_size": 800})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 3 and p["failed"] == 0 and p["skipped"] == 0
    assert path_of(fake.uploads[-1]["parent"]) == "Client delivery/people/Ravi" and fake.uploads[-1]["name"] == "c.jpg"
    assert len(fake.uploads[-1]["bytes"]) < (tmp_path / "c.jpg").stat().st_size
    assert c.get("/api/drive/status").json()["web_size"] == 800
    # selection: flat, into selection/, and a second run skips what is there
    r = c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": [ids["b.jpg"], ids["c.jpg"]]})
    assert r.json()["total"] == 2
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 2 and path_of(fake.uploads[-1]["parent"]) == "Client delivery/selection"
    n = len(fake.uploads)
    c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": [ids["b.jpg"], ids["c.jpg"]]})
    p = _wait_export(c)
    assert p["skipped"] == 2 and len(fake.uploads) == n
    # refusals
    assert c.post("/api/drive/export", json={"link": LINK, "what": "categories", "categories": []}).status_code == 400
    assert c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": []}).status_code == 400
    assert c.post("/api/drive/export", json={"link": LINK, "what": "categories", "categories": ["beach"], "videos": "segments"}).status_code == 400
    fake.limit = 1000; fake.usage = 0; fake.me = "owner@example.com"
    r = c.post("/api/drive/export", json={"link": LINK, "what": "categories", "categories": ["beach"]})
    assert r.status_code == 400 and "free in your Google Drive" in r.json()["detail"]
    assert sorted(os.listdir(tmp_path)) == before


def test_drive_export_409_while_another_export_runs(tmp_path, monkeypatch):
    from test_drive import fake_token
    c = _shoot_client(tmp_path, n=4)
    fake = _drive_fake(monkeypatch); fake_token(); fake.slow = 0.1
    ids = c.get("/api/search/ids").json()["ids"]
    r = c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": ids})
    assert r.status_code == 200, r.text
    assert c.post("/api/drive/export", json={"link": LINK, "what": "selection", "ids": ids}).status_code == 409
    assert c.post("/api/export", json={"ids": ids, "name": "t", "mode": "symlink"}).status_code == 409
    assert c.post("/api/folder", json={"path": str(tmp_path)}).status_code == 409
    p = _wait_export(c)
    assert p["error"] is None and p["done"] == 4 and p["what"] == "drive"
    assert c.post("/api/export", json={"ids": ids, "name": "t", "mode": "symlink"}).status_code == 200
    assert _wait_export(c)["error"] is None
    assert sorted(os.listdir(tmp_path)) == [f"p{i}.jpg" for i in range(4)]


def test_drive_signin_runs_in_a_thread_and_signout_forgets(tmp_path, monkeypatch):
    from test_drive import fake_token
    from photosort import drive
    c = _shoot_client(tmp_path)
    r = c.post("/api/drive/signin")                # no client file: the thread reports it on status
    assert r.status_code == 200 and r.json() == {"started": True}
    for _ in range(100):
        st = c.get("/api/drive/status").json()
        if not st["signing_in"]: break
        time.sleep(0.02)
    assert not st["signed_in"] and "google_client.json" in st["error"]
    drive.client_path().parent.mkdir(parents=True, exist_ok=True)
    drive.client_path().write_text("{}")
    import threading
    gate = threading.Event()
    def pretend_sign_in():
        gate.wait(5); fake_token("me@example.com"); return {"email": "me@example.com"}
    monkeypatch.setattr(drive, "sign_in", pretend_sign_in)
    assert c.post("/api/drive/signin").json() == {"started": True}
    st = c.get("/api/drive/status").json()
    assert st["configured"] and st["signing_in"] and st["error"] is None
    assert c.post("/api/drive/signin").status_code == 409
    gate.set()
    for _ in range(100):
        st = c.get("/api/drive/status").json()
        if not st["signing_in"]: break
        time.sleep(0.02)
    assert st["signed_in"] and st["email"] == "me@example.com"
    assert c.post("/api/drive/signout").json() == {"signed_in": False}
    assert not drive.token_path().exists() and not c.get("/api/drive/status").json()["signed_in"]
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


# ===== usage log: the UI's own events land through POST /api/usage, the summary reads every file =====

def test_usage_post_takes_ui_events_batched_and_drops_junk(tmp_path):
    from photosort import usage
    c = _shoot_client(tmp_path)
    assert c.post("/api/usage", json={"ev": "tab", "tab": "people"}).json() == {"logged": 1}
    batch = {"events": [
        {"ev": "click", "id": "cluster"},
        {"ev": "key", "key": "Escape", "cmd": False},
        {"ev": "tab_time", "tab": "people", "seconds": 12.5},
        {"ev": "ui_error", "message": "TypeError: x is null", "source": "app.js", "line": 12, "nested": {"not": "kept"}},
        {"ev": "Bad Name!", "x": 1},                     # not a valid event name
        "not an event",                                   # not an object
        {"ev": "click", "path": "/Volumes/SSD/shoot/DSC01.jpg"},   # a path is scrubbed like any other string
    ]}
    assert c.post("/api/usage", json=batch).json() == {"logged": 5}
    assert c.post("/api/usage", json=[{"ev": "inspector", "open": True}]).json() == {"logged": 1}
    assert c.post("/api/usage", json={"nope": 1}).json() == {"logged": 0}
    assert c.post("/api/usage", json=12).status_code == 400
    assert c.post("/api/usage", content=b"{bad", headers={"content-type": "application/json"}).status_code == 400
    evs = [e for e in usage.read_events() if e.get("src") == "ui"]
    assert [e["ev"] for e in evs] == ["ui_tab", "ui_click", "ui_key", "ui_tab_time", "ui_error", "ui_click", "ui_inspector"]
    assert evs[0]["tab"] == "people" and evs[3]["seconds"] == 12.5 and "nested" not in evs[4]
    assert evs[5]["path"] == "<path>"
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


def test_usage_summary_and_report_endpoints(tmp_path, monkeypatch):
    import zipfile
    from photosort import usage
    monkeypatch.setenv("PHOTOSORT_REPORT_DIR", str(tmp_path.parent / "desk"))
    c = _shoot_client(tmp_path)
    c.get("/api/search", params={"q": "boats"})
    c.get("/api/search", params={"q": "boats"})
    assert c.post("/api/folder", json={"path": str(tmp_path / "missing")}).status_code == 400
    c.post("/api/usage", json=[{"ev": "tab_time", "tab": "search", "seconds": 4}, {"ev": "help_open"}])
    s = c.get("/api/usage/summary").json()
    assert s["sessions"] == 1 and s["session"] == usage.session_id()
    assert s["counters"]["server_start"] == 1 and s["counters"]["folder_open"] == 1 and s["counters"]["search"] == 2
    assert s["counters"]["ui_help_open"] == 1 and s["this_session"]["search"] == 2
    assert s["top_queries"] == [{"q": "boats", "n": 2}]
    assert s["errors"][-1]["where"] == "/api/folder" and s["errors"][-1]["status"] == 400 and "<path>" in s["errors"][-1]["error"]
    assert s["seconds_by_feature"]["tab:search"] == 4 and s["system"]["python"]
    assert "sorted beta report" in s["text"] and "boats" in s["text"] and str(tmp_path) not in s["text"]
    r = c.post("/api/usage/report").json()
    p = Path(r["path"])
    assert p.parent == tmp_path.parent / "desk" and p.name.startswith("sorted-report-") and p.stat().st_size == r["bytes"] > 0
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert "usage/events.jsonl" in names and "summary.json" in names and "summary.txt" in names and "system.json" in names
        assert not any(n.endswith((".jpg", ".db")) for n in names)
        assert str(tmp_path) not in z.read("usage/events.jsonl").decode()
    assert c.get("/api/usage/summary").json()["counters"]["report_saved"] == 1
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg"]


# ===== resume: the scan block on /api/stats, interrupted jobs, Continue, the pause, the health line, the sleep guard =====

def _wait_progress(c, n=200):
    for _ in range(n):
        p = c.get("/api/progress").json()
        if not p["running"]: return p
        time.sleep(0.05)
    return p


def test_stats_scan_block_empty_without_a_folder_and_counted_with_one(tmp_path):
    from photosort import db
    none = TestClient(create_app(None)).get("/api/stats").json()["scan"]
    assert none["items"] == 0 and none["complete"] is True and none["faces"] is True and none["interrupted"] is None
    c = _shoot_client(tmp_path, n=3)                       # features read, no embeddings, faces off
    s = c.get("/api/stats").json()["scan"]
    assert (s["items"], s["scanned"], s["embedded"], s["pending"], s["unembedded"], s["complete"]) == (3, 3, 0, 0, 3, False)
    assert s["faces"] is False                             # what the last scan was asked for, from meta scan_faces
    assert s["interrupted"] is None                        # the scan finished, it was just asked for less
    assert db.get_meta(db.connect(tmp_path), "scan_faces") == "0"
    assert c.get("/api/folder").json()["scan"]["complete"] is False   # the welcome reads it from here while the disk is away


def test_running_job_is_marked_interrupted_when_the_shoot_opens_and_stats_says_so(tmp_path):
    from photosort import db
    from photosort.walk import ImageFile
    c = _shoot_client(tmp_path, n=2)
    conn = db.connect(tmp_path)
    # A scan cut short after listing two more files: the job row is still 'running' (the app died mid-scan).
    db.add_pending(conn, [ImageFile(tmp_path / "x.jpg", "x.jpg", 10, 1.0, False), ImageFile(tmp_path / "y.jpg", "y.jpg", 10, 1.0, False)])
    jid = db.start_job(conn, "scan", {"stage": "features", "done": 2, "total": 4, "faces": True})
    db.job_progress(conn, jid, {"stage": "features", "done": 2, "total": 4, "faces": True})
    # Opening the shoot (a fresh app, or POST /api/folder) marks it interrupted and /api/stats carries it.
    fresh = TestClient(create_app(tmp_path))
    s = fresh.get("/api/stats").json()["scan"]
    assert db.latest_jobs(conn)["scan"]["state"] == "interrupted"
    assert s["interrupted"] == dict(kind="scan", stage="features", done=2, total=4, started=s["interrupted"]["started"], error=None)
    assert (s["items"], s["scanned"], s["pending"], s["complete"]) == (4, 2, 2, False)
    jid2 = db.start_job(conn, "focus", {"stage": "focus", "done": 1, "total": 2})
    assert c.post("/api/folder", json={"path": str(tmp_path)}).status_code == 200
    assert db.latest_jobs(conn)["focus"]["state"] == "interrupted"
    assert c.get("/api/stats").json()["scan"]["interrupted"]["kind"] == "focus"    # the most recent one wins
    # The pending rows belong to files that are not there: a scan drops them, and the finished scan is history.
    conn.execute("DELETE FROM photos WHERE status='pending'"); conn.commit()
    db.finish_job(conn, jid2, "done")
    index_folder(tmp_path, faces=False, workers=1, embed=True)
    s = c.get("/api/stats").json()["scan"]
    assert s["complete"] is True and s["interrupted"] is None


def test_index_resume_uses_the_interrupted_scans_own_faces_choice(tmp_path, monkeypatch):
    from photosort import db
    seen = []
    def fake_index(root, faces=True, progress=None, retry_errors=False, **kw):
        seen.append(dict(faces=faces, retry_errors=retry_errors))
        return dict(total=1, indexed=0, skipped=1, faced=0, errors=0, embedded=0, seconds=0.0)
    monkeypatch.setattr("photosort.index.index_folder", fake_index)
    c = _shoot_client(tmp_path, n=1)                       # scan_faces is "0" from the faces=False index
    r = c.post("/api/index", json={"faces": True, "resume": True})
    assert r.status_code == 200 and r.json() == {"started": True, "faces": False}
    _wait_progress(c)
    assert seen[-1] == dict(faces=False, retry_errors=False)
    db.set_meta(db.connect(tmp_path), "scan_faces", "1")
    r = c.post("/api/index", json={"faces": False, "resume": True})
    assert r.status_code == 200 and r.json()["faces"] is True
    _wait_progress(c)
    assert seen[-1]["faces"] is True
    r = c.post("/api/index", json={"faces": False})        # a plain scan takes the box
    assert r.json()["faces"] is False
    _wait_progress(c)
    assert seen[-1]["faces"] is False


def test_index_409_while_the_disk_is_away_and_a_mid_scan_unplug_is_a_pause(tmp_path, monkeypatch):
    import shutil
    from photosort.index import SourceUnavailable
    from conftest import make_image
    shoot = tmp_path / "shoot"; shoot.mkdir()
    make_image(shoot, "a.jpg")
    index_folder(shoot, faces=False, workers=1, embed=False)
    c = TestClient(create_app(shoot))
    parked = tmp_path / "parked"
    shutil.move(str(shoot), str(parked))
    r = c.post("/api/index", json={"faces": False})
    assert r.status_code == 409 and "not connected" in r.json()["detail"]
    assert c.get("/api/progress").json()["running"] is False
    shutil.move(str(parked), str(shoot))
    def unplugged(root, faces=True, progress=None, retry_errors=False, **kw):
        progress({"stage": "features", "done": 3, "total": 9, "stage_started": time.time()})
        raise SourceUnavailable(f"{root} went away during the scan. Plug the disk in and continue; 3 photos are scanned so far.")
    monkeypatch.setattr("photosort.index.index_folder", unplugged)
    assert c.post("/api/index", json={"faces": False}).status_code == 200
    p = _wait_progress(c)
    assert p["stage"] == "paused" and "went away" in p["error"] and (p["done"], p["total"]) == (3, 9)
    assert p["running"] is False and p["awake"] is False
    assert c.post("/api/index", json={"faces": False, "resume": True}).status_code == 200   # not wedged
    _wait_progress(c)


def test_scan_health_names_the_one_fix(tmp_path, monkeypatch):
    from photosort import db
    from conftest import make_image
    assert TestClient(create_app(None)).get("/api/scan/health").status_code == 400
    c = _shoot_client(tmp_path, n=2)
    h = c.get("/api/scan/health").json()
    assert (h["on_disk"], h["new_on_disk"], h["scanned"], h["unembedded"], h["faces_pending"]) == (2, 0, 2, 2, 2)
    assert h["fix"] == "continue" and h["mounted"] is True and h["running"] is False
    index_folder(tmp_path, faces=False, workers=1, embed=True)
    assert c.get("/api/scan/health").json()["fix"] == "faces"          # scanned without faces: the one gap left
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET n_faces=0"); conn.commit()
    assert c.get("/api/scan/health").json()["fix"] is None
    make_image(tmp_path, "new.jpg", seed=9)
    h = c.get("/api/scan/health").json()
    assert (h["on_disk"], h["new_on_disk"], h["fix"]) == (3, 1, "rescan")
    jid = db.start_job(conn, "focus", {"stage": "focus", "done": 1, "total": 2})
    db.finish_job(conn, jid, "interrupted")
    assert c.get("/api/scan/health").json()["fix"] == "focus"          # a cut-short focus pass is not a scan
    db.finish_job(conn, db.start_job(conn, "focus"), "done")
    assert c.get("/api/scan/health").json()["fix"] == "rescan"
    # While a scan runs the walk would only race it: on_disk is None and the counts come from the index.
    def slow(root, faces=True, progress=None, retry_errors=False, **kw):
        time.sleep(0.4)
        return dict(total=3, indexed=1, skipped=2, faced=0, errors=0, embedded=1, seconds=0.4)
    monkeypatch.setattr("photosort.index.index_folder", slow)
    assert c.post("/api/index", json={"faces": False}).status_code == 200
    h = c.get("/api/scan/health").json()
    assert h["running"] is True and h["on_disk"] is None and h["new_on_disk"] == 0
    _wait_progress(c)


def test_awake_is_held_for_a_scan_and_reported_on_every_progress_endpoint(tmp_path, monkeypatch):
    from photosort import awake
    held = []
    def fake_index(root, faces=True, progress=None, retry_errors=False, **kw):
        held.append(awake.held())
        time.sleep(0.3)
        return dict(total=1, indexed=0, skipped=1, faced=0, errors=0, embedded=0, seconds=0.3)
    monkeypatch.setattr("photosort.index.index_folder", fake_index)
    c = _shoot_client(tmp_path, n=1)
    for path in ("/api/progress", "/api/classify/progress", "/api/focus/progress", "/api/export/progress"):
        assert c.get(path).json()["awake"] is False, path
    assert c.post("/api/index", json={"faces": False}).status_code == 200
    seen_true = False
    for _ in range(100):
        p = c.get("/api/progress").json()
        if p["awake"]: seen_true = True
        if not p["running"]: break
        time.sleep(0.02)
    assert held == [True] and seen_true and p["awake"] is False and awake.held() is False


def test_bundle_import_reply_says_how_far_the_scan_got(tmp_path, tmp_path_factory):
    """A scan file of a half-scanned shoot: the import reply carries the scan block (complete False), so the UI
    says so and offers Continue instead of quietly showing fewer items. After Continue the same shoot's
    stats say complete."""
    import zipfile, json
    from photosort import db
    from photosort.walk import ImageFile
    from photosort.bundle import export_bundle
    c = _shoot_client(tmp_path, n=2)
    conn = db.connect(tmp_path)
    db.add_pending(conn, [ImageFile(tmp_path / "later.jpg", "later.jpg", 10, 1.0, False)])   # listed, never read
    jid = db.start_job(conn, "scan", {"stage": "features", "done": 2, "total": 3, "faces": False})
    db.finish_job(conn, jid, "interrupted", error="unplugged")
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out)
    other = TestClient(create_app(None))
    r = other.post("/api/bundle/import", json={"zip": str(z), "root": str(tmp_path)})
    assert r.status_code == 200, r.text
    scan = r.json()["scan"]
    assert scan["complete"] is False and (scan["items"], scan["scanned"], scan["pending"], scan["unembedded"]) == (3, 2, 1, 2)
    assert scan["interrupted"]["kind"] == "scan" and scan["interrupted"]["done"] == 2 and scan["faces"] is False
    assert other.get("/api/stats").json()["scan"]["pending"] == 1
    # An older scan file without "scan" in bundle.json gets the same reply, from the installed index.
    old = out / "old.photosort-index.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(old, "w") as dst:
        for i in src.infolist():
            data = src.read(i.filename)
            if i.filename == "bundle.json":
                info = json.loads(data); info.pop("scan"); data = json.dumps(info)
            dst.writestr(i, data)
    r = other.post("/api/bundle/import", json={"zip": str(old), "root": str(tmp_path)})
    assert r.status_code == 200 and r.json()["scan"]["pending"] == 1 and r.json()["scan"]["complete"] is False
    assert sorted(os.listdir(tmp_path)) == ["p0.jpg", "p1.jpg"]
