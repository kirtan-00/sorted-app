"""One list. The pile is what an editor has picked and photos.flag='pick' is its mirror, so a click, the P key, Keep best
and Client pick all land in the same place, and the Ratings filter, the counts, XMP and the CSV see them. A reject is the
one thing the list never wipes on its way out. A shoot from before the merge catches up once, on opening."""
import json, sqlite3, time, zipfile
import pytest
from fastapi.testclient import TestClient
from photosort import db, pile
from photosort.config import DB_NAME, app_home, shoot_slug
from photosort.export import export_ids
from photosort.index import index_folder
from photosort.search import Filters, Index
from photosort.server import create_app


def _shoot(tmp_path, n=5):
    from conftest import make_image
    for i in range(n):
        make_image(tmp_path, f"{chr(97 + i)}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    return conn, [r["id"] for r in conn.execute("SELECT id FROM photos ORDER BY rel")]


def _flags(conn, ids):
    return [m["flag"] for m in map(db.get_marks(conn, ids).get, ids)]


def _as_before_the_merge(conn, rows, flags):
    """The shoot as an older build left it: pile rows and flags written by hand, the merge not done yet."""
    conn.execute("DELETE FROM pile"); conn.execute("UPDATE photos SET flag=NULL")
    conn.executemany("INSERT INTO pile(photo_id, seq) VALUES(?, ?)", [(i, k) for k, i in enumerate(rows, 1)])
    conn.executemany("UPDATE photos SET flag=? WHERE id=?", [(f, i) for i, f in flags.items()])
    conn.execute("DELETE FROM meta WHERE key=?", (pile.MERGED_KEY,))
    conn.commit()


# ===== the pile moves the flag =====
def test_picking_sets_the_flag_and_a_pick_overrides_a_reject(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    db.set_marks(conn, [b], flag="reject")
    assert pile.add(conn, [a, b]) == [b]                  # says which reject it took off
    assert _flags(conn, [a, b, c]) == ["pick", "pick", None]
    assert pile.summary(conn)["ids"] == [a, b]


def test_a_reject_survives_remove_and_clear(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    pile.add(conn, [a, b])
    conn.execute("UPDATE photos SET flag='reject' WHERE id=?", (b,)); conn.commit()    # however it got there
    db.set_marks(conn, [c], flag="reject")
    pile.remove(conn, [a, b, c])
    assert _flags(conn, [a, b, c]) == [None, "reject", "reject"]
    pile.add(conn, [a])
    conn.execute("UPDATE photos SET flag='reject' WHERE id=?", (a,)); conn.commit()
    pile.clear(conn)
    assert _flags(conn, [a, b, c]) == ["reject", "reject", "reject"]


def test_remove_and_clear_take_the_pick_off(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    pile.add(conn, [a, b, c])
    pile.remove(conn, [b])
    assert _flags(conn, [a, b, c]) == ["pick", None, "pick"]
    pile.clear(conn)
    assert _flags(conn, [a, b, c]) == [None, None, None] and pile.summary(conn)["n"] == 0


# ===== the flag moves the pile =====
def test_a_pick_flag_joins_the_list_and_a_reject_or_none_leaves_it(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    db.set_marks(conn, [c, a], flag="pick")
    assert pile.summary(conn)["ids"] == [c, a]
    db.set_marks(conn, [c], flag="reject")
    assert pile.summary(conn)["ids"] == [a] and _flags(conn, [c]) == ["reject"]
    db.set_marks(conn, [a], flag=None)
    assert pile.summary(conn)["n"] == 0 and _flags(conn, [a]) == [None]


def test_stars_and_labels_leave_the_list_alone(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    pile.add(conn, [a])
    db.set_marks(conn, [a, b], rating=3, label="red")
    assert pile.summary(conn)["ids"] == [a] and _flags(conn, [a, b]) == ["pick", None]


def test_an_id_the_shoot_never_had_changes_nothing(tmp_path):
    conn, (a, *_) = _shoot(tmp_path)
    db.set_marks(conn, [a, 99999], flag="pick")
    assert pile.summary(conn)["ids"] == [a]


# ===== undo =====
def test_restore_brings_back_the_same_rows_order_and_flags(tmp_path):
    conn, (a, b, c, d, e) = _shoot(tmp_path)
    pile.add(conn, [c, a, b])
    rows = pile.clear(conn)
    pile.restore(conn, rows)
    assert pile.summary(conn)["ids"] == [c, a, b]
    assert _flags(conn, [a, b, c, d]) == ["pick", "pick", "pick", None]
    # what was picked since stays, after them, and a pick made between clear and undo gets no second copy
    rows = pile.clear(conn)
    pile.add(conn, [d, a])
    pile.restore(conn, rows)
    assert pile.summary(conn)["ids"] == [c, a, b, d]
    # a reject set since wins over the undo
    rows = pile.clear(conn)
    db.set_marks(conn, [a], flag="reject")
    pile.restore(conn, rows)
    assert pile.summary(conn)["ids"] == [c, b, d] and _flags(conn, [a]) == ["reject"]


def test_clear_and_undo_keep_a_row_whose_file_is_missing(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    pile.add(conn, [a, b])
    conn.execute("UPDATE photos SET status='missing' WHERE id=?", (b,)); conn.commit()
    rows = pile.clear(conn)
    pile.restore(conn, rows)
    conn.execute("UPDATE photos SET status='ok' WHERE id=?", (b,)); conn.commit()
    assert pile.summary(conn)["ids"] == [a, b] and _flags(conn, [b]) == ["pick"]


def test_clear_and_undo_through_the_api(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    client = TestClient(create_app(tmp_path))
    client.get("/api/search")
    client.post("/api/pile", json={"add": [b, a]})
    cleared = client.delete("/api/pile").json()
    assert cleared["n"] == 0 and [r["id"] for r in cleared["cleared"]] == [b, a]
    assert client.get("/api/search", params={"flag": "pick"}).json()["results"] == []
    back = client.post("/api/pile/restore", json={"rows": cleared["cleared"]}).json()
    assert back["ids"] == [b, a]
    assert sorted(r["id"] for r in client.get("/api/search", params={"flag": "pick"}).json()["results"]) == sorted([a, b])
    assert client.post("/api/pile/restore", json={"rows": [{"nope": 1}]}).status_code == 400
    assert TestClient(create_app(None)).post("/api/pile/restore", json={"rows": []}).status_code == 400


# ===== the search index keeps its own copy of the marks =====
def test_a_change_through_the_list_reaches_the_search_results_with_no_refresh(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    client = TestClient(create_app(tmp_path))
    flag_of = lambda: {r["id"]: r["flag"] for r in client.get("/api/search").json()["results"]}
    assert flag_of()[a] is None
    client.post("/api/pile", json={"add": [a, b]})
    assert flag_of()[a] == "pick"
    assert sorted(r["id"] for r in client.get("/api/search", params={"flag": "pick"}).json()["results"]) == sorted([a, b])
    assert client.get("/api/marks/counts").json()["flag"]["pick"] == 2
    client.post("/api/pile", json={"remove": [a]})
    assert flag_of()[a] is None
    client.delete("/api/pile")
    assert flag_of()[b] is None


def test_the_marks_endpoint_answers_with_the_list(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    client = TestClient(create_app(tmp_path))
    r = client.post("/api/marks", json={"ids": [a], "flag": "pick"}).json()
    assert r["marks"][str(a)]["flag"] == "pick" and r["pile"]["ids"] == [a]
    r = client.post("/api/marks", json={"ids": [a], "flag": "reject"}).json()
    assert r["pile"]["ids"] == [] and r["marks"][str(a)]["flag"] == "reject"
    assert "pile" not in client.post("/api/marks", json={"ids": [a], "rating": 2}).json()


def test_a_bulk_pick_says_which_rejects_it_took_off(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    client = TestClient(create_app(tmp_path))
    client.post("/api/marks", json={"ids": [b], "flag": "reject"})
    assert client.post("/api/pile", json={"add": [a, b]}).json()["unrejected"] == [b]


# ===== the merge =====
def test_the_merge_is_the_union_of_both_and_a_reject_stays(tmp_path):
    conn, (a, b, c, d, e) = _shoot(tmp_path)
    _as_before_the_merge(conn, [c, a, e], {e: "reject", d: "pick", b: "pick"})
    conn = db.connect(tmp_path)
    assert pile.summary(conn)["ids"] == [c, a, b, d]      # the pile's own order first, the flagged after it in id order
    assert _flags(conn, [a, b, c, d, e]) == ["pick", "pick", "pick", "pick", "reject"]
    done = json.loads(db.get_meta(conn, pile.MERGED_KEY))
    assert done == {"joined": 2, "flagged": 2, "rejected": [e]}


def test_the_merge_clears_nothing_else(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    db.set_marks(conn, [a], rating=4, label="red")
    db.set_marks(conn, [c], rating=2)
    _as_before_the_merge(conn, [a], {b: "reject"})
    conn = db.connect(tmp_path)
    m = db.get_marks(conn, [a, b, c])
    assert m[a] == {"flag": "pick", "rating": 4, "label": "red"}
    assert m[b]["flag"] == "reject" and m[c] == {"flag": None, "rating": 2, "label": None}


def test_the_merge_runs_once(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    _as_before_the_merge(conn, [a], {b: "pick"})
    conn = db.connect(tmp_path)
    ids, key = pile.summary(conn)["ids"], db.get_meta(conn, pile.MERGED_KEY)
    assert ids == [a, b]
    conn = db.connect(tmp_path)
    assert pile.summary(conn)["ids"] == ids and db.get_meta(conn, pile.MERGED_KEY) == key
    # once it has run, a stray row is not "merged" again
    conn.execute("UPDATE photos SET flag='pick' WHERE id=?", (c,)); conn.commit()
    assert c not in pile.summary(db.connect(tmp_path))["ids"]


def test_the_merge_leaves_a_missing_file_as_it_is_until_it_comes_back(tmp_path):
    conn, (a, b, c, d, e) = _shoot(tmp_path)
    _as_before_the_merge(conn, [a, c], {b: "pick", c: "reject"})
    conn.executemany("UPDATE photos SET status='missing' WHERE id=?", [(a,), (b,), (c,)]); conn.commit()
    conn = db.connect(tmp_path)
    assert _flags(conn, [a, b, c]) == [None, "pick", "reject"]
    assert [r[0] for r in conn.execute("SELECT photo_id FROM pile ORDER BY seq")] == [a, c]
    db.restore_missing(conn, ["a.jpg", "b.jpg", "c.jpg"])
    assert pile.summary(conn)["ids"] == [a, b]
    assert _flags(conn, [a, b, c]) == ["pick", "pick", "reject"]
    assert json.loads(db.get_meta(conn, pile.MERGED_KEY))["rejected"] == [c]


def test_coming_back_changes_nothing_on_a_row_that_agrees(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    pile.add(conn, [b, a])
    conn.execute("UPDATE photos SET status='missing'"); conn.commit()
    db.restore_missing(conn, ["a.jpg", "b.jpg"])
    assert pile.summary(conn)["ids"] == [b, a]


def test_an_older_index_without_a_pile_table_opens(tmp_path):
    conn, (a, *_) = _shoot(tmp_path)
    db.set_marks(conn, [a], flag="pick")
    conn.execute("DROP TABLE pile"); conn.execute("DELETE FROM meta WHERE key=?", (pile.MERGED_KEY,)); conn.commit()
    assert pile.summary(db.connect(tmp_path))["ids"] == [a]


def test_an_older_project_file_catches_up_when_it_is_imported(tmp_path, tmp_path_factory):
    from photosort.bundle import export_bundle, import_bundle
    conn, (a, b, c, *_) = _shoot(tmp_path)
    db.set_marks(conn, [c], flag="reject")
    # export_bundle opens the shoot, which merges it, so the zip is put back to what an older build wrote
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out, thumbs=False)
    snap = tmp_path_factory.mktemp("snap") / DB_NAME
    with zipfile.ZipFile(z) as f:
        snap.write_bytes(f.read(DB_NAME))
    raw = sqlite3.connect(snap)
    raw.execute("DELETE FROM pile"); raw.execute("INSERT INTO pile(photo_id, seq) VALUES(?, 1)", (b,))
    raw.execute("UPDATE photos SET flag=NULL WHERE id=?", (b,)); raw.execute("UPDATE photos SET flag='pick' WHERE id=?", (a,))
    raw.execute("DELETE FROM meta WHERE key=?", (pile.MERGED_KEY,)); raw.commit(); raw.close()
    old = out / "old.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(old, "w") as dst:
        for i in src.infolist():
            dst.writestr(i, snap.read_bytes() if i.filename == DB_NAME else src.read(i.filename))
    other = tmp_path_factory.mktemp("elsewhere")
    target = import_bundle(old, other)
    assert target == app_home() / shoot_slug(other)
    conn = db.connect(other)
    assert pile.summary(conn)["ids"] == [b, a]
    assert _flags(conn, [a, b, c]) == ["pick", "pick", "reject"]


# ===== nothing automatic touches the list, and what is on it travels =====
def test_an_export_leaves_the_list_alone(tmp_path):
    conn, (a, b, c, *_) = _shoot(tmp_path)
    client = TestClient(create_app(tmp_path))
    client.post("/api/pile", json={"add": [b, a]})
    assert client.post("/api/export", json={"ids": [b, a], "name": "x", "mode": "copy"}).status_code == 200
    for _ in range(200):
        if not client.get("/api/export/progress").json()["running"]:
            break
        time.sleep(0.05)
    assert client.get("/api/export/progress").json()["done"] == 2
    assert client.get("/api/pile").json()["ids"] == [b, a]
    assert _flags(db.connect(tmp_path), [a, b]) == ["pick", "pick"]


def test_a_clicked_pick_keeps_the_export_folder_clean_until_it_has_more_to_say(tmp_path, tmp_path_factory):
    conn, (a, b, *_) = _shoot(tmp_path)
    TestClient(create_app(tmp_path)).post("/api/pile", json={"add": [a]})    # a click is this call, nothing else
    out = export_ids(tmp_path, [a], "sent", "copy", base=tmp_path_factory.mktemp("dest"))
    assert (out / "a.jpg").exists() and not (out / "a.xmp").exists()         # the pick alone: a client's folder stays clean
    db.set_marks(conn, [a], rating=4)
    out = export_ids(tmp_path, [a], "starred", "copy", base=tmp_path_factory.mktemp("dest"))
    side = out / "a.xmp"
    assert 'xmpDM:pick="1"' in side.read_text("utf-8") and 'xmpDM:good="True"' in side.read_text("utf-8")
    csv_out = export_ids(tmp_path, [a], "list", "csv", base=tmp_path_factory.mktemp("dest"))
    assert ",pick," in next(csv_out.glob("*.csv")).read_text("utf-8")


def test_the_ratings_filter_and_the_list_filter_show_the_same_rows(tmp_path):
    conn, (a, b, *_) = _shoot(tmp_path)
    pile.add(conn, [b])
    db.set_marks(conn, [a], flag="pick")
    ix = Index(tmp_path)
    assert sorted(r["id"] for r in ix.search(filters=Filters(flag="pick"))) == sorted(r["id"] for r in ix.search(filters=Filters(pile=True))) == sorted([a, b])
