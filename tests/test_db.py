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
