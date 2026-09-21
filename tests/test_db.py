import sqlite3
import numpy as np
from photosort import db
from photosort.config import DB_NAME

def test_roundtrip(tmp_path):
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, dict(rel="a.jpg", size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    pid2 = db.upsert_photo(conn, dict(rel="a.jpg", size=2, mtime=2.0, qhash="h2", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    assert pid == pid2
    assert db.known_files(conn) == {"a.jpg": (2, 2.0)}
    assert db.photos_missing_embed(conn) == [(pid, "a.jpg")]
    db.set_embed(conn, pid, np.ones(512, np.float32))
    ids, M = db.load_embeds(conn)
    assert ids.tolist() == [pid] and M.shape == (1, 512) and M.dtype == np.float32
    db.replace_faces(conn, pid, [dict(x=1,y=2,w=3,h=4,score=0.9,landmarks="[]",eye_sharp=5.0,embed=np.ones(128,np.float32).tobytes())])
    fids, pids, F = db.load_face_embeds(conn)
    assert F.shape == (1, 128) and pids.tolist() == [pid]
    db.mark_missing(conn, set())
    assert conn.execute("select status from photos").fetchone()[0] == "missing"
    assert db.known_files(conn) == {}
    d = db.index_dir(tmp_path)
    assert (d / "thumbs").is_dir() and (d / "grid").is_dir()
    assert not str(d.resolve()).startswith(str(tmp_path.resolve()))   # never inside the shoot
    assert not (tmp_path / ".photosort").exists()

def test_category_migration_is_idempotent_on_an_existing_db(tmp_path):
    """A photos table created before category/category_score existed (CREATE TABLE IF NOT EXISTS
    is a no-op on it) must get the columns added by hand, without losing existing rows, and calling
    connect() again must not error or duplicate the columns."""
    d = db.index_dir(tmp_path)
    raw = sqlite3.connect(d / DB_NAME)
    raw.executescript("""
        CREATE TABLE photos(
          id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL, qhash TEXT,
          sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT,
          sharp_tile REAL, sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0,
          embed BLOB, status TEXT DEFAULT 'ok', indexed_at TEXT DEFAULT (datetime('now')));
    """)
    raw.execute("INSERT INTO photos(rel, status) VALUES ('old.jpg', 'ok')")
    raw.commit(); raw.close()

    conn = db.connect(tmp_path)   # first connect: must ALTER TABLE in the old-schema DB
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    assert {"category", "category_score", "cluster", "cluster_score", "category_guess", "category_guess_score", "aerial"} <= cols
    assert conn.execute("SELECT rel FROM photos").fetchone()[0] == "old.jpg"   # row survives the migration

    conn2 = db.connect(tmp_path)   # second connect: ALTER TABLE must not run again / must not error
    cols2 = [r[1] for r in conn2.execute("PRAGMA table_info(photos)")]
    assert cols2.count("category") == 1 and cols2.count("category_score") == 1 and cols2.count("cluster") == 1

def test_mark_error_updates_in_place_or_inserts_a_minimal_row(tmp_path):
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, dict(rel="a.jpg", size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=2, status="ok"))
    db.set_embed(conn, pid, np.ones(512, np.float32))
    conn.execute("UPDATE photos SET category='beach' WHERE id=?", (pid,)); conn.commit()
    db.mark_error(conn, "a.jpg", 5, 5.0)
    r = conn.execute("SELECT id, status, size, mtime, qhash, embed, category, n_faces FROM photos WHERE rel='a.jpg'").fetchone()
    assert r[0] == pid and r[1] == "error" and (r[2], r[3]) == (5, 5.0)
    assert r[4] == "h" and r[5] is not None and r[6] == "beach" and r[7] == 2
    db.mark_error(conn, "new.jpg", 7, 7.0)                    # never seen before: a minimal row
    r2 = conn.execute("SELECT status, size, mtime, qhash, n_faces FROM photos WHERE rel='new.jpg'").fetchone()
    assert r2[0] == "error" and (r2[1], r2[2]) == (7, 7.0) and r2[3] is None and r2[4] == 0
    assert db.known_files(conn) == {"a.jpg": (5, 5.0), "new.jpg": (7, 7.0)}
    assert db.known_files(conn, retry_errors=True) == {}

def test_aerial_defaults_to_zero_and_is_counted(tmp_path):
    """A row stored without an aerial key (every pre-drone caller) is 0, not NULL, so the zero-shot pass
    that looks for aerial=0 rows sees it; aerial_count only counts ok rows."""
    conn = db.connect(tmp_path)
    base = dict(size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10, taken_at=None, camera=None,
                phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok")
    a = db.upsert_photo(conn, dict(base, rel="a.jpg"))
    b = db.upsert_photo(conn, dict(base, rel="DJI_0001.MP4", kind="video", aerial=True))
    c = db.upsert_photo(conn, dict(base, rel="DJI_0002.MP4", kind="video", aerial=1, status="error"))
    assert [r[0] for r in conn.execute("SELECT aerial FROM photos ORDER BY id")] == [0, 1, 1]
    assert db.aerial_count(conn) == 1
    db.upsert_photo(conn, dict(base, rel="DJI_0001.MP4", kind="video", size=2))   # re-indexed without the key: back to 0
    assert conn.execute("SELECT aerial FROM photos WHERE id=?", (b,)).fetchone()[0] == 0


def _plan(conn, q: str) -> str:
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q))


def _fill(conn, n: int) -> None:
    """n synthetic ok rows with a 1 KB embed each (the row shape of a real index), straight into photos."""
    emb = np.ones(512, np.float16).tobytes()
    conn.executemany("INSERT INTO photos(rel, size, mtime, qhash, status, category, category_guess, cluster, kind, aerial, n_faces, focus, embed) "
                     "VALUES(?, 1, 1.0, ?, 'ok', ?, ?, ?, 'photo', ?, 0, ?, ?)",
                     [(f"d/{i:05d}.jpg", f"{i:040x}"[-40:], ["beach", "people", "other"][i % 3], "beach" if i % 3 else None,
                       f"group {i % 4}", int(i % 40 == 0), ["ok", "soft", "bad", None][i % 4], emb) for i in range(n)])
    conn.commit(); db.analyze(conn)


def test_per_render_counts_walk_covering_indexes_and_wide_reads_still_scan(tmp_path):
    """Every count the UI renders (categories, kind, drone, focus, faces pending, the folder count, the error
    list) walks a covering index, never the table with its 1 KB embeds; the whole-table reads in id order
    (Index.refresh, load_embeds) still scan, which is faster than an index plus a sort. Measured at 20k rows:
    /api/categories 34 -> 7 ms, /api/stats 33 -> 8 ms, /api/errors 24 -> 1 ms, Index.refresh unchanged."""
    conn = db.connect(tmp_path); _fill(conn, 3000)
    counts = [
        "SELECT COALESCE(category, 'unclassified') AS c, COUNT(*) FROM photos WHERE status='ok' GROUP BY c",
        "SELECT category_guess, COUNT(*) FROM photos WHERE status='ok' AND category='people' AND category_guess IS NOT NULL AND category_guess != 'people' GROUP BY category_guess",
        "SELECT cluster, COUNT(*) AS n FROM photos WHERE status='ok' AND cluster IS NOT NULL GROUP BY cluster ORDER BY n DESC, cluster",
        "SELECT COUNT(*) FROM photos WHERE status='ok' AND aerial=1",
        "SELECT COALESCE(kind, 'photo') AS k, COUNT(*) FROM photos WHERE status='ok' GROUP BY k",
        "SELECT count(*) FROM photos WHERE status='ok' AND n_faces IS NULL",
        "SELECT SUM(focus IS NOT NULL), SUM(focus IS NULL), SUM(focus='bad'), SUM(focus='soft') FROM photos WHERE status='ok'",
        "SELECT count(*) FROM photos WHERE status='ok'",
        "SELECT rel, indexed_at FROM photos WHERE status='error' ORDER BY rel",
    ]
    for q in counts:
        plan = _plan(conn, q)
        assert "SCAN photos" not in plan and "INDEX photos_status_" in plan, (q, plan)
    wide = [
        "SELECT id, rel, qhash, sharp, n_faces, taken_at, category, cluster, kind, aerial, focus FROM photos WHERE status='ok' ORDER BY id",
        "SELECT id, embed FROM photos WHERE embed IS NOT NULL AND status='ok' ORDER BY id",
    ]
    for q in wide:
        assert _plan(conn, q) == "SCAN photos", (q, _plan(conn, q))
    assert db.category_counts(conn) == {"beach": 2000, "people": 1000, "other": 1000} and db.aerial_count(conn) == 75
    # the counts are right through the indexes after rows change
    conn.execute("UPDATE photos SET status='error' WHERE rel='d/00000.jpg'"); conn.commit()
    assert db.kind_counts(conn) == {"photos": 2999, "videos": 0}


def test_connect_analyses_an_index_that_never_was(tmp_path):
    """An index.db from before the covering indexes existed gets them and one ANALYZE on open, so the planner
    stops routing whole-table reads through the new indexes; a DB with statistics is left alone."""
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'photos_status_%'").fetchall()
    _fill(conn, 200)
    conn.execute("DROP TABLE sqlite_stat1"); conn.commit(); conn.close()
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0
    conn.execute("DELETE FROM sqlite_stat1"); conn.commit()
    conn.execute("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES('photos', 'photos_status_rel', '200 1 1')"); conn.commit(); conn.close()
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] == 1          # not re-run when stats exist
