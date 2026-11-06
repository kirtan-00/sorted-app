"""The pile: what an editor has picked so far, kept per shoot in the index so it survives a new search, a
reload and a restart, and travels inside the project file with the rest of index.db (Q4, backlog item 13).

An editor does not find and export once: they find, add to the pile, search again, add more, take a few
out, then send the pile. The grid's selection IS the pile; these tests hold the part that lives on disk."""
import os
from fastapi.testclient import TestClient
from photosort import db, pile
from photosort.index import index_folder
from photosort.search import Index, Filters
from photosort.server import create_app


def _shoot(tmp_path, n=3):
    from conftest import make_image
    for i in range(n):
        make_image(tmp_path, f"{chr(97 + i)}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    return conn, {r["rel"]: r["id"] for r in conn.execute("SELECT id, rel FROM photos")}


def test_add_remove_clear_and_the_running_size(tmp_path):
    conn, ids = _shoot(tmp_path)
    assert pile.summary(conn) == {"ids": [], "n": 0, "photos": 0, "videos": 0, "bytes": 0}
    pile.add(conn, [ids["b.jpg"], ids["a.jpg"], ids["b.jpg"], 99999])        # a repeat and an id the shoot never had
    s = pile.summary(conn)
    size = {rel: (tmp_path / rel).stat().st_size for rel in ids}
    assert s["ids"] == [ids["b.jpg"], ids["a.jpg"]] and s["n"] == 2 and s["photos"] == 2
    assert s["bytes"] == size["a.jpg"] + size["b.jpg"]
    pile.remove(conn, [ids["b.jpg"]])
    assert pile.summary(conn)["ids"] == [ids["a.jpg"]]
    pile.clear(conn)
    assert pile.summary(conn)["n"] == 0


def test_a_file_that_left_the_shoot_leaves_the_pile_count(tmp_path):
    """A row the scan marked missing stays picked (the disk may come back) but is not counted or exported."""
    conn, ids = _shoot(tmp_path)
    pile.add(conn, list(ids.values()))
    conn.execute("UPDATE photos SET status='missing' WHERE rel='c.jpg'"); conn.commit()
    assert pile.summary(conn)["n"] == 2
    conn.execute("UPDATE photos SET status='ok' WHERE rel='c.jpg'"); conn.commit()
    assert pile.summary(conn)["n"] == 3


def test_the_pile_filter_shows_the_pile_on_its_own(tmp_path):
    conn, ids = _shoot(tmp_path)
    pile.add(conn, [ids["c.jpg"], ids["a.jpg"]])
    ix = Index(tmp_path)
    assert sorted(r["rel"] for r in ix.search(filters=Filters(pile=True))) == ["a.jpg", "c.jpg"]
    assert ix.facet_counts(Filters(pile=True))["total"] == 2
    assert len(ix.search()) == 3


def test_the_pile_endpoints_survive_a_restart_and_a_new_search(tmp_path):
    _, ids = _shoot(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/pile").json()["n"] == 0
    r = c.post("/api/pile", json={"add": [ids["a.jpg"], ids["b.jpg"]]}).json()
    assert r["n"] == 2 and r["bytes"] > 0
    c.get("/api/search", params={"q": "", "kind": "videos"})                   # a new search changes nothing
    r = c.post("/api/pile", json={"remove": [ids["a.jpg"]], "add": [ids["c.jpg"]]}).json()
    assert r["ids"] == [ids["b.jpg"], ids["c.jpg"]]
    again = TestClient(create_app(tmp_path))                                    # the app restarted
    assert again.get("/api/pile").json()["ids"] == [ids["b.jpg"], ids["c.jpg"]]
    assert [x["rel"] for x in again.get("/api/search", params={"pile": 1}).json()["results"]] == ["b.jpg", "c.jpg"]
    assert again.delete("/api/pile").json()["n"] == 0
    assert TestClient(create_app(None)).get("/api/pile").json()["n"] == 0
    assert TestClient(create_app(None)).post("/api/pile", json={"add": [1]}).status_code == 400
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "b.jpg", "c.jpg"]          # nothing written to the shoot


def test_the_pile_is_inside_the_project_file(tmp_path, tmp_path_factory):
    """The project file is a sqlite backup of index.db, so the pile goes wherever the shoot goes."""
    import sqlite3, zipfile
    from photosort import bundle
    conn, ids = _shoot(tmp_path)
    pile.add(conn, [ids["b.jpg"]])
    out = bundle.export_bundle(tmp_path, tmp_path_factory.mktemp("out"), thumbs=False)
    with zipfile.ZipFile(out) as z:
        snap = tmp_path_factory.mktemp("snap") / "index.db"
        snap.write_bytes(z.read("index.db"))
    assert [r[0] for r in sqlite3.connect(snap).execute("SELECT photo_id FROM pile")] == [ids["b.jpg"]]
