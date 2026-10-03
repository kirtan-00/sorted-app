import sqlite3
import numpy as np
from photosort import db
from photosort.config import DB_NAME, EMBED_DIM

def test_roundtrip(tmp_path):
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, dict(rel="a.jpg", size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    pid2 = db.upsert_photo(conn, dict(rel="a.jpg", size=2, mtime=2.0, qhash="h2", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    assert pid == pid2
    assert db.known_files(conn) == {"a.jpg": (2, 2.0)}
    assert db.photos_missing_embed(conn) == [(pid, "a.jpg")]
    db.set_embed(conn, pid, np.ones(EMBED_DIM, np.float32))
    ids, M = db.load_embeds(conn)
    assert ids.tolist() == [pid] and M.shape == (1, EMBED_DIM) and M.dtype == np.float32
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
    db.set_embed(conn, pid, np.ones(EMBED_DIM, np.float32))
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
    emb = np.ones(EMBED_DIM, np.float16).tobytes()
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
        "SELECT COALESCE(shot_size, 'unscored') AS s, COUNT(*) FROM photos WHERE status='ok' GROUP BY s",
        "SELECT SUM(fps >= 100.0), SUM(fps IS NOT NULL AND fps < 100.0), SUM(fps IS NULL) FROM photos WHERE status='ok' AND kind='video'",
    ]
    for q in counts:
        plan = _plan(conn, q)
        assert "SCAN photos" not in plan and "INDEX photos_status_" in plan, (q, plan)
    # The whole-table reads, as the code really writes them. Index.refresh carries "+status" because
    # ANALYZE stopped being enough: SQLite 3.50 takes photos_status_faces plus a temp b-tree sort even
    # with fresh statistics (43 ms against 38 ms at 20k rows). load_embeds still scans on its own, so it
    # is left alone and this asserts that it stays that way.
    wide = [
        "SELECT id, rel, size, mtime, lat, lon, qhash, sharp, n_faces, taken_at, width, height, category, category_score, category_guess, category_guess_score, cluster, cluster_score, kind, duration, camera, aerial, focus, shot_size, shot_size_score, fps, capture_fps, gamma, people, face_h FROM photos WHERE +status='ok' ORDER BY id",
        "SELECT id, embed FROM photos WHERE embed IS NOT NULL AND length(embed)=1536 AND status='ok' ORDER BY id",
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

def test_shot_size_and_fps_migrate_into_an_old_index(tmp_path):
    """The client's index was built before either column existed and will not be rescanned, so the
    ALTER TABLE path is the only way it ever gets them. Existing rows survive, a second connect does
    not add the columns twice, and the covering indexes are created after the migration so they can
    name columns the CREATE TABLE never had."""
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

    conn = db.connect(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
    assert {"shot_size", "shot_size_score", "fps"} <= set(cols)
    assert conn.execute("SELECT rel FROM photos").fetchone()[0] == "old.jpg"
    assert conn.execute("SELECT shot_size, fps FROM photos").fetchone()[:] == (None, None)
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"photos_status_shot_size", "photos_status_kind_fps"} <= idx

    conn2 = db.connect(tmp_path)
    cols2 = [r[1] for r in conn2.execute("PRAGMA table_info(photos)")]
    assert cols2.count("shot_size") == 1 and cols2.count("fps") == 1

def test_shot_size_counts_keeps_unknown_and_unscored_apart(tmp_path):
    """Two different facts: unknown means the pass ran and the gates refused to pick, unscored means
    it has never run over that row. A rail that showed one number for both would be claiming coverage
    it does not have."""
    conn = db.connect(tmp_path)
    base = dict(size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10, taken_at=None,
                camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None, sharp=1.0,
                n_faces=0, status="ok")
    for rel, size in (("a.jpg", "wide"), ("b.jpg", "wide"), ("c.jpg", "medium"),
                      ("d.jpg", "unknown"), ("e.jpg", None)):
        pid = db.upsert_photo(conn, dict(base, rel=rel, qhash=rel))
        conn.execute("UPDATE photos SET shot_size=? WHERE id=?", (size, pid))
    conn.execute("UPDATE photos SET status='error' WHERE rel='a.jpg'")     # never counted
    conn.commit()
    assert db.shot_size_counts(conn) == {"medium": 1, "wide": 1, "unknown": 1, "unscored": 1}

def test_fps_survives_a_reindex_of_an_unchanged_file(tmp_path):
    """fps is a fact about the file rather than something derived from its embedding, so it rides in
    PHOTO_COLS and is rewritten from the probe on every upsert. shot_size, which IS derived from the
    embedding, is cleared in the same statement. The two must not be confused."""
    conn = db.connect(tmp_path)
    row = dict(rel="c.mp4", size=1, mtime=1.0, qhash="h", sibling=None, width=1920, height=1080,
               taken_at=None, camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None,
               sharp=1.0, n_faces=0, status="ok", kind="video", duration=3.0, fps=120.0)
    pid = db.upsert_photo(conn, row)
    conn.execute("UPDATE photos SET shot_size='wide' WHERE id=?", (pid,)); conn.commit()
    db.upsert_photo(conn, dict(row, size=2, mtime=2.0))
    r = conn.execute("SELECT fps, shot_size FROM photos WHERE id=?", (pid,)).fetchone()
    assert r["fps"] == 120.0 and r["shot_size"] is None

def test_connect_scrubs_nul_padded_camera_names_once(tmp_path):
    """An index built before features stripped NULs off the PIL path holds 'DJI\\x00...\\x00 FC7303\\x00...'
    for every DJI DNG, which never groups with the 'DJI FC7303' on the JPG beside it. connect cleans those
    rows in place (no file is re-read), stamps meta so it runs once, and leaves every other camera alone."""
    conn = db.connect(tmp_path)
    base = dict(size=1, mtime=1.0, qhash="h", sibling=None, width=256, height=192, taken_at=None,
                phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok")
    db.upsert_photo(conn, dict(base, rel="a.dng", camera="DJI" + "\x00" * 29 + " FC7303" + "\x00" * 26))
    db.upsert_photo(conn, dict(base, rel="b.jpg", camera="SONY ILCE-7M4"))
    db.upsert_photo(conn, dict(base, rel="c.jpg", camera=None))
    conn.execute("DELETE FROM meta WHERE key=?", (db.CAMERA_SCRUB_KEY,)); conn.commit()   # an index from before
    assert "\x00" in conn.execute("SELECT camera FROM photos WHERE rel='a.dng'").fetchone()[0]
    conn.close()
    conn = db.connect(tmp_path)
    cams = {r["rel"]: r["camera"] for r in conn.execute("SELECT rel, camera FROM photos")}
    assert cams == {"a.dng": "DJI FC7303", "b.jpg": "SONY ILCE-7M4", "c.jpg": None}
    assert db.get_meta(conn, db.CAMERA_SCRUB_KEY)

def test_unpad_camera_matches_what_features_writes_now():
    assert db._unpad_camera("DJI\x00\x00\x00 FC6310\x00\x00") == "DJI FC6310"
    assert db._unpad_camera("Canon\x00\x00 Canon EOS R5\x00") == "Canon EOS R5"   # make in model: model alone
    assert db._unpad_camera("FC6310\x00\x00") == "FC6310"                         # no make at all
    assert db._unpad_camera("\x00\x00") is None

# ===== clip facts for the video filters: camera body, picture profile, length, people in the clip =====
_CLIP = dict(size=1, mtime=1.0, sibling=None, width=1920, height=1080, taken_at=None, camera=None,
             phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None, sharp=1.0, n_faces=0,
             status="ok", kind="video", duration=5.0)


def test_clip_fact_columns_migrate_into_an_old_index(tmp_path):
    """An index from v0.4.0 has fps and shot_size but none of the clip facts; connect adds them, NULL,
    which every count below reads as "not read yet" rather than as an answer."""
    d = db.index_dir(tmp_path)
    raw = sqlite3.connect(d / DB_NAME)
    raw.executescript("""
        CREATE TABLE photos(
          id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL, qhash TEXT,
          sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT,
          sharp_tile REAL, sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0,
          embed BLOB, status TEXT DEFAULT 'ok', indexed_at TEXT DEFAULT (datetime('now')),
          kind TEXT DEFAULT 'photo', duration REAL, fps REAL);
    """)
    raw.execute("INSERT INTO photos(rel, status, kind, duration) VALUES ('old.mp4', 'ok', 'video', 4.0)")
    raw.commit(); raw.close()
    conn = db.connect(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(photos)")]
    assert {"gamma", "capture_fps", "people", "face_h"} <= set(cols)
    assert tuple(conn.execute("SELECT gamma, capture_fps, people, face_h FROM photos").fetchone()) == (None, None, None, None)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='clip_faces'").fetchone()
    cols2 = [r[1] for r in db.connect(tmp_path).execute("PRAGMA table_info(photos)")]
    assert cols2.count("gamma") == 1 and cols2.count("people") == 1


def test_clip_fact_counts(tmp_path):
    """Camera counts cover every row a body shot (stills and clips alike, the filter is the same); the
    picture profile, the length and the slow motion counts cover clips only. gamma NULL is "never read",
    gamma '' is "read, the clip carries no profile": both are unknown to the rail, neither is Rec.709."""
    conn = db.connect(tmp_path)
    clips = [("a.mp4", dict(camera="SONY ILCE-7SM3", gamma="s-log3-cine", duration=4.0, fps=25.0)),
             ("b.mp4", dict(camera="SONY ILCE-7SM3", gamma="rec709", duration=12.0, fps=100.0)),
             ("c.mp4", dict(camera="Apple iPhone 15 Pro", gamma="", duration=45.0, fps=30.0)),
             ("d.mp4", dict(camera=None, gamma=None, duration=600.0, fps=None)),
             ("e.mp4", dict(camera=None, gamma="s-log3", duration=9.99, fps=25.0, capture_fps=100.0))]
    for rel, extra in clips:
        db.upsert_photo(conn, dict(_CLIP, rel=rel, qhash=rel, **extra))
    db.upsert_photo(conn, dict(_CLIP, rel="still.jpg", qhash="s", kind="photo", duration=None, camera="SONY ILCE-7SM3"))
    db.upsert_photo(conn, dict(_CLIP, rel="bad.mp4", qhash="x", status="error", camera="SONY ILCE-7SM3", gamma="s-log3"))
    conn.commit()
    assert db.camera_counts(conn) == {"SONY ILCE-7SM3": 3, "Apple iPhone 15 Pro": 1}
    assert db.look_counts(conn) == {"log": 2, "standard": 1, "unknown": 2}
    assert db.duration_counts(conn) == {"lt10": 2, "10to30": 1, "30to120": 1, "gt120": 1}
    # S&Q: captured at 100, written at 25. The larger rate is the one that says slow motion.
    assert db.slowmo_counts(conn) == {"slow": 2, "normal": 2, "unknown": 1}


def test_clip_counts_walk_covering_indexes(tmp_path):
    conn = db.connect(tmp_path); _fill(conn, 3000)
    for q in (db.CAMERA_COUNTS_SQL, db.LOOK_COUNTS_SQL, db.DURATION_COUNTS_SQL, db.SLOWMO_COUNTS_SQL,
              db.CLIP_FACES_PENDING_SQL):
        plan = _plan(conn, q.replace("?", "100"))
        assert "SCAN photos" not in plan and "INDEX photos_status_" in plan, (q, plan)


def test_clip_facts_survive_a_reindex_and_clip_faces_follow_their_clip(tmp_path):
    """gamma, capture_fps, people and face_h ride in PHOTO_COLS: the scan rewrites them from the file
    (and its frames) on every upsert, so a re-decoded clip never keeps a stale profile or head count. The
    faces found on a clip's frames are a table of their own, replaced per clip and gone with the clip."""
    conn = db.connect(tmp_path)
    row = dict(_CLIP, rel="c.mp4", qhash="h", gamma="s-log3", capture_fps=100.0, people=2, face_h=0.21)
    pid = db.upsert_photo(conn, row)
    r = conn.execute("SELECT gamma, capture_fps, people, face_h FROM photos WHERE id=?", (pid,)).fetchone()
    assert tuple(r) == ("s-log3", 100.0, 2, 0.21)
    db.upsert_photo(conn, dict(row, size=2, people=None, face_h=None))       # rescanned with faces off
    assert tuple(conn.execute("SELECT people, face_h FROM photos WHERE id=?", (pid,)).fetchone()) == (None, None)
    emb = np.ones(128, np.float32).tobytes()
    db.replace_clip_faces(conn, pid, [dict(frame=0, x=1, y=2, w=30, h=40, score=0.9, embed=emb),
                                      dict(frame=3, x=5, y=6, w=31, h=41, score=0.8, embed=emb)])
    db.replace_clip_faces(conn, pid, [dict(frame=1, x=1, y=2, w=30, h=40, score=0.95, embed=emb)])
    assert [tuple(r) for r in conn.execute("SELECT photo_id, frame, score FROM clip_faces")] == [(pid, 1, 0.95)]
    conn.execute("DELETE FROM photos WHERE id=?", (pid,)); conn.commit()
    assert conn.execute("SELECT count(*) FROM clip_faces").fetchone()[0] == 0
