"""Flags, stars and colour labels (selling checklist B2): stored per item in the index, kept through a rescan,
carried in the project file, filterable."""
import os, sqlite3
import pytest
from photosort import db
from photosort.config import DB_NAME
from photosort.search import Index, Filters


def _row(rel, **kw):
    return dict(dict(rel=rel, size=1, mtime=1.0, qhash="h" + rel, sibling=None, width=10, height=10, taken_at=None,
                     camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0,
                     status="ok"), **kw)


def _three(tmp_path):
    conn = db.connect(tmp_path)
    return conn, [db.upsert_photo(conn, _row(n)) for n in ("a.jpg", "b.jpg", "c.mov")]


def test_a_new_index_has_no_marks(tmp_path):
    conn, ids = _three(tmp_path)
    assert db.get_marks(conn, ids) == {i: {"flag": None, "rating": 0, "label": None} for i in ids}


def test_set_marks_changes_only_what_is_given(tmp_path):
    conn, (a, b, c) = _three(tmp_path)
    assert db.set_marks(conn, [a, b], flag="pick") == 2
    db.set_marks(conn, [b], rating=4)
    db.set_marks(conn, [b, c], label="red")
    m = db.get_marks(conn, [a, b, c])
    assert m[a] == {"flag": "pick", "rating": 0, "label": None}
    assert m[b] == {"flag": "pick", "rating": 4, "label": "red"}
    assert m[c] == {"flag": None, "rating": 0, "label": "red"}
    db.set_marks(conn, [b], flag=None, rating=0, label=None)          # clearing is an explicit None / 0
    assert db.get_marks(conn, [b])[b] == {"flag": None, "rating": 0, "label": None}


@pytest.mark.parametrize("kw", [dict(flag="keep"), dict(rating=6), dict(rating=-1), dict(label="orange"), dict(rating="3")])
def test_set_marks_refuses_a_bad_value(tmp_path, kw):
    conn, ids = _three(tmp_path)
    with pytest.raises(ValueError):
        db.set_marks(conn, ids, **kw)
    assert db.get_marks(conn, ids)[ids[0]] == {"flag": None, "rating": 0, "label": None}


def test_marks_survive_a_rescan_of_a_changed_file(tmp_path):
    """A rating is a person's decision, not something derived from the pixels: a re-decode of the file
    (new size and mtime) clears the embedding and the category, never the marks."""
    conn, (a, b, c) = _three(tmp_path)
    db.set_marks(conn, [a], flag="reject", rating=2, label="blue")
    assert db.upsert_photo(conn, _row("a.jpg", size=9, mtime=9.0)) == a
    db.mark_missing(conn, {"b.jpg", "c.mov"}); db.restore_missing(conn, ["a.jpg"])
    assert db.get_marks(conn, [a])[a] == {"flag": "reject", "rating": 2, "label": "blue"}


def test_an_index_from_before_marks_is_migrated(tmp_path):
    d = db.index_dir(tmp_path)
    raw = sqlite3.connect(d / DB_NAME)
    raw.executescript("""CREATE TABLE photos(id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL,
        qhash TEXT, sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT, sharp_tile REAL,
        sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0, embed BLOB, status TEXT DEFAULT 'ok',
        indexed_at TEXT DEFAULT (datetime('now')));""")
    raw.execute("INSERT INTO photos(rel, status) VALUES ('old.jpg', 'ok')"); raw.commit(); raw.close()
    conn = db.connect(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
    assert all(cols.count(c) == 1 for c in ("flag", "rating", "label"))
    (pid,) = conn.execute("SELECT id FROM photos").fetchone()
    assert db.get_marks(conn, [pid])[pid] == {"flag": None, "rating": 0, "label": None}
    db.connect(tmp_path)                                                   # a second connect is a no-op


def test_mark_counts(tmp_path):
    conn, (a, b, c) = _three(tmp_path)
    db.set_marks(conn, [a, b], flag="pick"); db.set_marks(conn, [c], flag="reject")
    db.set_marks(conn, [a], rating=5); db.set_marks(conn, [b], rating=3)
    db.set_marks(conn, [a, c], label="green")
    n = db.mark_counts(conn)
    assert n["flag"] == {"pick": 2, "reject": 1, "none": 0}
    assert n["stars"] == {"1": 2, "2": 2, "3": 2, "4": 1, "5": 1}          # at least that many stars
    assert n["label"] == {"red": 0, "yellow": 0, "green": 2, "blue": 0, "purple": 0, "none": 1}
    assert n["marked"] == 3


def test_search_rows_carry_marks_and_filter_on_them(tmp_path):
    conn, (a, b, c) = _three(tmp_path)
    db.set_marks(conn, [a], flag="pick", rating=4, label="red")
    db.set_marks(conn, [b], flag="reject", rating=1)
    ix = Index(tmp_path)
    rows = {r["id"]: r for r in ix.search()}
    assert (rows[a]["flag"], rows[a]["rating"], rows[a]["label"]) == ("pick", 4, "red")
    assert (rows[c]["flag"], rows[c]["rating"], rows[c]["label"]) == (None, 0, None)
    ids = lambda **kw: sorted(r["id"] for r in ix.search(filters=Filters(**kw)))
    assert ids(flag="pick") == [a]
    assert ids(flag="reject") == [b]
    assert ids(flag="none") == [c]
    assert ids(stars_min=1) == [a, b]
    assert ids(stars_min=4) == [a]
    assert ids(label="red") == [a]
    assert ids(label="none") == [b, c]
    assert ids(flag="pick", label="blue") == []


def test_index_set_marks_updates_the_live_rows_without_a_refresh(tmp_path):
    conn, (a, b, c) = _three(tmp_path)
    ix = Index(tmp_path)
    ix.set_marks({a: {"flag": "pick", "rating": 3, "label": None}})
    assert [r["id"] for r in ix.search(filters=Filters(flag="pick"))] == [a]
    assert ix.photos[a]["rating"] == 3


def test_marks_travel_in_the_project_file(tmp_path, tmp_path_factory, monkeypatch):
    from conftest import make_image
    from photosort.index import index_folder
    from photosort.bundle import export_bundle, import_bundle, inspect_bundle
    make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=1)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM photos ORDER BY rel")]
    db.set_marks(conn, ids[:1], flag="pick", rating=5, label="purple")
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    assert inspect_bundle(z)["marked"] == 1
    monkeypatch.setenv("PHOTOSORT_HOME", str(tmp_path_factory.mktemp("other-mac")))
    import_bundle(z)
    conn2 = db.connect(tmp_path)
    assert db.get_marks(conn2, ids)[ids[0]] == {"flag": "pick", "rating": 5, "label": "purple"}
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "b.jpg"]
