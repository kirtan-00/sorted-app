from __future__ import annotations
import json, sqlite3
from pathlib import Path
import numpy as np
from .config import DB_NAME, EMBED_DIM, app_home, shoot_slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL, qhash TEXT,
  sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT,
  sharp_tile REAL, sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0,
  embed BLOB, status TEXT DEFAULT 'ok', indexed_at TEXT DEFAULT (datetime('now')),
  category TEXT, category_score REAL, kind TEXT DEFAULT 'photo', duration REAL,
  category_guess TEXT, category_guess_score REAL, cluster TEXT, cluster_score REAL, aerial INTEGER DEFAULT 0,
  focus TEXT, focus_score REAL);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  idx INTEGER, start REAL, end REAL, frame TEXT, embed BLOB, category TEXT, category_score REAL);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  x INTEGER, y INTEGER, w INTEGER, h INTEGER, score REAL, landmarks TEXT, eye_sharp REAL,
  embed BLOB, person_id INTEGER);
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, cover_face_id INTEGER, n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ref_faces(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, embed BLOB NOT NULL, source TEXT,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS face_links(
  a INTEGER NOT NULL, b INTEGER NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('same','different')),
  created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running',
  started TEXT DEFAULT (datetime('now')), finished TEXT, progress TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY, q TEXT NOT NULL UNIQUE, filters TEXT, saved INTEGER DEFAULT 0, n INTEGER DEFAULT 1,
  used_at TEXT DEFAULT (datetime('now')), saved_at TEXT);
CREATE TABLE IF NOT EXISTS exports(
  id INTEGER PRIMARY KEY, at TEXT DEFAULT (datetime('now')), what TEXT, dest TEXT, route TEXT, req TEXT,
  path TEXT, count INTEGER, done INTEGER, failed INTEGER, skipped INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS segments_photo ON segments(photo_id);
"""
# Covering indexes for every per-render count: a photos row carries a 1 KB embedding, so a "SCAN photos" for
# a count reads the whole table (24 MB at 20k rows, 6 to 10 ms per query, 34 ms for /api/categories); with
# these each count walks a narrow index instead (under 1 ms). Kept out of SCHEMA so they are created after
# the column migrations below (an old index.db predates category, cluster, aerial and focus).
INDEXES = """
CREATE INDEX IF NOT EXISTS photos_status_category ON photos(status, category, category_guess);
CREATE INDEX IF NOT EXISTS photos_status_cluster ON photos(status, cluster);
CREATE INDEX IF NOT EXISTS photos_status_aerial ON photos(status, aerial);
CREATE INDEX IF NOT EXISTS photos_status_kind ON photos(status, kind);
CREATE INDEX IF NOT EXISTS photos_status_faces ON photos(status, n_faces);
CREATE INDEX IF NOT EXISTS photos_status_focus ON photos(status, focus);
CREATE INDEX IF NOT EXISTS photos_status_rel ON photos(status, rel);
"""

PHOTO_COLS = ["rel","size","mtime","qhash","sibling","width","height","taken_at","camera","phash",
              "sharp_tile","sharp_max","sharp_eye","sharp","n_faces","status","kind","duration","aerial"]

def index_dir(root: Path) -> Path:
    d = app_home() / shoot_slug(root)
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    (d / "grid").mkdir(parents=True, exist_ok=True)
    (d / "frames").mkdir(parents=True, exist_ok=True)     # sampled video frames, <qhash>_<k>.jpg
    return d

def connect(root: Path) -> sqlite3.Connection:
    d = index_dir(root)
    conn = sqlite3.connect(d / DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Idempotent migration: CREATE TABLE IF NOT EXISTS above only takes effect on a brand-new DB, so a
    # photos table created before category/category_score existed needs them added by hand.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "category" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN category TEXT")
    if "category_score" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN category_score REAL")
    if "kind" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN kind TEXT DEFAULT 'photo'")
    if "duration" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN duration REAL")
    # category_guess: the best real category and its probability even when the photo was filed under
    # "other", so it can still be shown there as "less sure". cluster: the discovered category (k-means
    # over the shoot, named from the vocabulary) and the photo's 0..1 closeness to its cluster centroid.
    if "category_guess" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN category_guess TEXT")
    if "category_guess_score" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN category_guess_score REAL")
    if "cluster" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN cluster TEXT")
    if "cluster_score" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN cluster_score REAL")
    # aerial: a drone shot. 1 from the index (DJI metadata, a DJI_ filename, an .SRT telemetry sidecar) or
    # from the zero-shot aerial/ground pass in classify_and_store; a metadata 1 is never re-decided.
    if "aerial" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN aerial INTEGER DEFAULT 0")
    # focus: the on-demand blur pass (focus.check_focus): NULL never checked, else ok / soft / bad, with the
    # score it was judged on. Cleared with embed when a changed file is re-decoded.
    if "focus" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN focus TEXT")
    if "focus_score" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN focus_score REAL")
    conn.executescript(INDEXES)
    conn.commit()
    # Without statistics the planner takes a (status, ...) index for EVERY status='ok' query, including the
    # wide reads (Index.refresh, load_embeds, the face join) that want the whole table in id order: those
    # went 158 -> 247 ms at 20k rows through the index plus a sort. With ANALYZE run once (14 ms at 20k
    # rows) it knows status='ok' is nearly every row and scans for those while the counts keep their
    # covering indexes. Run here when the DB has never been analysed; index_folder runs it after every scan.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'").fetchone() \
            or not conn.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone():
        analyze(conn)
    return conn

def analyze(conn) -> None:
    """Refresh the query planner's statistics (sqlite_stat1) after a bulk change in row counts."""
    conn.execute("ANALYZE"); conn.commit()

def upsert_photo(conn, row: dict) -> int:
    # Every column is bound explicitly, so a row without a kind (or aerial) would store NULL, not the
    # column default; the zero-shot drone pass looks for aerial=0, so a NULL there would never be decided.
    row = dict(row, kind=row.get("kind") or "photo", aerial=int(bool(row.get("aerial"))))
    cols = ",".join(PHOTO_COLS); ph = ",".join("?" * len(PHOTO_COLS))
    upd = ",".join(f"{c}=excluded.{c}" for c in PHOTO_COLS if c != "rel")
    # embed is cleared so a changed file gets re-embedded; category, guess and cluster are cleared with it
    # since they were derived from that embedding and would otherwise show a stale label.
    conn.execute(f"INSERT INTO photos({cols}) VALUES({ph}) ON CONFLICT(rel) DO UPDATE SET {upd}, embed=NULL, category=NULL, category_score=NULL, "
                 "category_guess=NULL, category_guess_score=NULL, cluster=NULL, cluster_score=NULL, focus=NULL, focus_score=NULL, indexed_at=datetime('now')",
                 [row.get(c) for c in PHOTO_COLS])
    conn.commit()
    return conn.execute("SELECT id FROM photos WHERE rel=?", (row["rel"],)).fetchone()[0]

def mark_error(conn, rel: str, size: int, mtime: float) -> None:
    """Flag a file that could not be read this pass. Only status/size/mtime move: qhash, embed,
    category and faces from an earlier good pass stay, so a retry after a disk hiccup does not
    have to re-decode and re-embed. A file never seen before gets a minimal error row."""
    cur = conn.execute("UPDATE photos SET status='error', size=?, mtime=?, indexed_at=datetime('now') WHERE rel=?",
                       (size, mtime, rel))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO photos(rel, size, mtime, status, n_faces) VALUES(?, ?, ?, 'error', 0)", (rel, size, mtime))
    conn.commit()

def replace_faces(conn, photo_id: int, faces: list[dict]) -> None:
    conn.execute("DELETE FROM faces WHERE photo_id=?", (photo_id,))
    conn.executemany("INSERT INTO faces(photo_id,x,y,w,h,score,landmarks,eye_sharp,embed) VALUES(?,?,?,?,?,?,?,?,?)",
        [(photo_id, f["x"], f["y"], f["w"], f["h"], f["score"], f["landmarks"], f["eye_sharp"], f["embed"]) for f in faces])
    conn.commit()

def set_faces_only(conn, rel: str, faces: list[dict], eye: float | None) -> None:
    """The faces-only pass on an already indexed photo: the faces table and the three face-derived
    columns move, nothing else (embed, category, cluster, focus, thumbs all stay)."""
    r = conn.execute("SELECT id, sharp_tile FROM photos WHERE rel=?", (rel,)).fetchone()
    if r is None:
        return
    conn.execute("UPDATE photos SET n_faces=?, sharp_eye=?, sharp=? WHERE id=?",
                 (len(faces), eye, eye if eye is not None else r[1], r[0]))
    replace_faces(conn, r[0], faces)

def set_embed(conn, photo_id: int, vec: np.ndarray) -> None:
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (np.asarray(vec, np.float16).tobytes(), photo_id))

# Video segments: one row per scene between two cuts, with the midpoint frame's filename under frames/.

def replace_segments(conn, photo_id: int, segments: list[dict]) -> None:
    conn.execute("DELETE FROM segments WHERE photo_id=?", (photo_id,))
    conn.executemany("INSERT INTO segments(photo_id, idx, start, end, frame) VALUES(?,?,?,?,?)",
        [(photo_id, s["idx"], s["start"], s["end"], s["frame"]) for s in segments])
    conn.commit()

def list_segments(conn, photo_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, idx, start, end, frame, category, category_score FROM segments WHERE photo_id=? ORDER BY idx", (photo_id,)).fetchall()
    return [dict(r) for r in rows]

def set_segment_embed(conn, seg_id: int, vec: np.ndarray) -> None:
    conn.execute("UPDATE segments SET embed=? WHERE id=?", (np.asarray(vec, np.float16).tobytes(), seg_id))

def segments_missing_embed(conn) -> list[tuple[int, int, str]]:
    """(segment id, photo id, frame filename) for every segment of an ok video that has no embedding yet."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT s.id, s.photo_id, s.frame FROM segments s JOIN photos p ON p.id=s.photo_id WHERE s.embed IS NULL AND p.status='ok' ORDER BY s.photo_id, s.idx")]

def load_segment_embeds(conn):
    """(ids, M) for every embedded segment of an ok video, float32 (n, EMBED_DIM), rows in id order."""
    rows = conn.execute("SELECT s.id, s.embed FROM segments s JOIN photos p ON p.id=s.photo_id WHERE s.embed IS NOT NULL AND p.status='ok' ORDER BY s.id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros((0, EMBED_DIM), np.float32)
    ids = np.array([r[0] for r in rows], np.int64)
    M = np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows])
    return ids, M

def photos_missing_embed(conn) -> list[tuple[int, str]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT id, rel FROM photos WHERE embed IS NULL AND status='ok' ORDER BY id")]

def load_embeds(conn):
    rows = conn.execute("SELECT id, embed FROM photos WHERE embed IS NOT NULL AND status='ok' ORDER BY id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros((0, EMBED_DIM), np.float32)
    ids = np.array([r[0] for r in rows], np.int64)
    M = np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows])
    return ids, M

def load_face_embeds(conn):
    rows = conn.execute("SELECT f.id, f.photo_id, f.embed FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' ORDER BY f.id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 128), np.float32)
    return (np.array([r[0] for r in rows], np.int64), np.array([r[1] for r in rows], np.int64),
            np.stack([np.frombuffer(r[2], np.float32) for r in rows]))

def load_face_people(conn):
    """(face ids, person ids) for the same rows and order as load_face_embeds; person id -1 when unassigned."""
    rows = conn.execute("SELECT f.id, f.person_id FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' ORDER BY f.id").fetchall()
    return (np.array([r[0] for r in rows], np.int64), np.array([-1 if r[1] is None else r[1] for r in rows], np.int64))

def load_face_quality(conn):
    """(face ids, short edge in preview px, eye_sharp) for the same rows and order as load_face_embeds."""
    rows = conn.execute("SELECT f.id, MIN(f.w, f.h), f.eye_sharp FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' ORDER BY f.id").fetchall()
    return (np.array([r[0] for r in rows], np.int64), np.array([r[1] or 0 for r in rows], np.float32),
            np.array([0.0 if r[2] is None else r[2] for r in rows], np.float32))

# Remembered "same person?" answers. Keyed by FACE ids (a < b), not person ids: person rows are wiped
# on every re-cluster, face rows survive until their photo is re-indexed. A pair holds one decision;
# a later answer replaces an earlier one (the user changed their mind).

def add_face_link(conn, a: int, b: int, decision: str) -> None:
    if a == b:
        raise ValueError("a face cannot be linked to itself")
    a, b = (a, b) if a < b else (b, a)
    conn.execute("INSERT OR REPLACE INTO face_links(a, b, decision) VALUES(?, ?, ?)", (int(a), int(b), decision))
    conn.commit()

def face_links(conn) -> list[tuple[int, int, str]]:
    """Every remembered decision as (face a, face b, 'same' | 'different'), a < b, oldest first."""
    return [(r[0], r[1], r[2]) for r in conn.execute("SELECT a, b, decision FROM face_links ORDER BY rowid")]

def add_reference(conn, name: str, embed: np.ndarray, source: str) -> int:
    """One saved reference face (a named person). Several rows may share a name; matching
    takes the best of them. The embed is stored float32 like the faces table."""
    cur = conn.execute("INSERT INTO ref_faces(name, embed, source) VALUES(?, ?, ?)",
                       (name, np.asarray(embed, np.float32).tobytes(), source))
    conn.commit()
    return int(cur.lastrowid)

def list_references(conn) -> list[dict]:
    rows = conn.execute("SELECT id, name, source, created_at FROM ref_faces ORDER BY id").fetchall()
    return [dict(id=r[0], name=r[1], source=r[2], created_at=r[3]) for r in rows]

def load_reference_embeds(conn):
    """(ids, names, R) for every saved reference, R float32 (n, 128), rows in id order."""
    rows = conn.execute("SELECT id, name, embed FROM ref_faces ORDER BY id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), [], np.zeros((0, 128), np.float32)
    return (np.array([r[0] for r in rows], np.int64), [r[1] for r in rows],
            np.stack([np.frombuffer(r[2], np.float32) for r in rows]))

def delete_reference(conn, ref_id: int) -> bool:
    cur = conn.execute("DELETE FROM ref_faces WHERE id=?", (ref_id,)); conn.commit()
    return cur.rowcount > 0

def rename_reference(conn, name_old: str, name_new: str) -> int:
    """Every reference saved under name_old now answers to name_new. Returns the rows moved."""
    cur = conn.execute("UPDATE ref_faces SET name=? WHERE name=?", (name_new, name_old)); conn.commit()
    return cur.rowcount

def get_meta(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None

def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value)); conn.commit()

def known_files(conn, retry_errors: bool = False) -> dict[str, tuple[int, float]]:
    """rel -> (size, mtime) for rows that count as already indexed. Missing rows are excluded here
    and handled by missing_files() so a returning file can be restored without a re-decode. Pending rows
    (listed by a scan that never got to them) are excluded too: they are the work a resumed scan does."""
    q = "SELECT rel, size, mtime FROM photos WHERE status NOT IN ('missing', 'pending')"
    if retry_errors:
        q += " AND status != 'error'"
    return {r[0]: (r[1], r[2]) for r in conn.execute(q)}

def missing_files(conn) -> dict[str, tuple[int, float, str]]:
    """rel -> (size, mtime, qhash) for rows the last scan could not find."""
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT rel, size, mtime, qhash FROM photos WHERE status='missing'")}

def restore_missing(conn, rels: list[str]) -> None:
    conn.executemany("UPDATE photos SET status='ok' WHERE rel=? AND status='missing'", [(r,) for r in rels])
    conn.commit()

def photos_without_faces(conn) -> set[str]:
    """Photos indexed with faces off (n_faces NULL). They need a second pass when faces are wanted."""
    return {r[0] for r in conn.execute("SELECT rel FROM photos WHERE n_faces IS NULL AND status='ok'")}

def kind_counts(conn) -> dict[str, int]:
    """{"photos": n, "videos": m} over status='ok' rows (a NULL kind is a photo)."""
    rows = conn.execute("SELECT COALESCE(kind, 'photo') AS k, COUNT(*) FROM photos WHERE status='ok' GROUP BY k").fetchall()
    d = {r[0]: r[1] for r in rows}
    return {"photos": d.get("photo", 0), "videos": d.get("video", 0)}

def category_counts(conn) -> dict[str, int]:
    """category -> count for status='ok' photos; NULL (never classified) is reported as 'unclassified'."""
    rows = conn.execute("SELECT COALESCE(category, 'unclassified') AS c, COUNT(*) FROM photos WHERE status='ok' GROUP BY c").fetchall()
    counts = {r[0]: r[1] for r in rows}
    # People photos also count under the scene they keep as their guess (a beach photo with a
    # face is a beach photo too), matching search.category_match.
    for cat, n in conn.execute("""SELECT category_guess, COUNT(*) FROM photos WHERE status='ok' AND category='people'
                                  AND category_guess IS NOT NULL AND category_guess != 'people' GROUP BY category_guess"""):
        counts[cat] = counts.get(cat, 0) + n
    return counts

def aerial_count(conn) -> int:
    """Drone shots (photos and videos) among status='ok' rows: the "drone" tile."""
    return int(conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND aerial=1").fetchone()[0])

def cluster_counts(conn) -> dict[str, int]:
    """discovered category name -> count for status='ok' photos, largest first. Empty until discover_and_store ran."""
    rows = conn.execute("SELECT cluster, COUNT(*) AS n FROM photos WHERE status='ok' AND cluster IS NOT NULL GROUP BY cluster ORDER BY n DESC, cluster").fetchall()
    return {r[0]: r[1] for r in rows}

# ===== search history and saved searches: one row per query text, per shoot (the table travels in the scan file) =====
RECENT_SEARCHES = 20

def _search_row(r) -> dict:
    try: filters = json.loads(r["filters"]) if r["filters"] else {}
    except Exception: filters = {}
    return {"id": r["id"], "q": r["q"], "filters": filters if isinstance(filters, dict) else {}, "saved": bool(r["saved"]),
            "n": r["n"], "used_at": r["used_at"], "saved_at": r["saved_at"]}

def record_search(conn, q: str, filters: dict | None = None) -> None:
    """A search with text was run: the row for that exact text moves to the top of the recent list with
    the filters it ran with (a saved row keeps its saved flag). Unsaved rows past RECENT_SEARCHES go."""
    q = (q or "").strip()
    if not q:
        return
    f = json.dumps(filters or {}, sort_keys=True)
    conn.execute("""INSERT INTO searches(q, filters) VALUES(?, ?)
                    ON CONFLICT(q) DO UPDATE SET n=n+1, used_at=datetime('now'), filters=excluded.filters""", (q, f))
    conn.execute("""DELETE FROM searches WHERE saved=0 AND id NOT IN
                    (SELECT id FROM searches ORDER BY used_at DESC, id DESC LIMIT ?)""", (RECENT_SEARCHES,))
    conn.commit()

def list_searches(conn) -> dict:
    """{recent: the last RECENT_SEARCHES queries newest first (saved ones included), saved: the starred ones, oldest star first}."""
    recent = [_search_row(r) for r in conn.execute("SELECT * FROM searches ORDER BY used_at DESC, id DESC LIMIT ?", (RECENT_SEARCHES,))]
    saved = [_search_row(r) for r in conn.execute("SELECT * FROM searches WHERE saved=1 ORDER BY saved_at, id")]
    return {"recent": recent, "saved": saved}

def save_search(conn, sid: int, saved: bool) -> dict | None:
    """Star or unstar one row; None when there is no such row. A row saved from the dropdown keeps its filters."""
    cur = conn.execute("UPDATE searches SET saved=?, saved_at=CASE WHEN ? THEN datetime('now') ELSE NULL END WHERE id=?",
                       (1 if saved else 0, 1 if saved else 0, sid))
    conn.commit()
    if cur.rowcount == 0:
        return None
    return _search_row(conn.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone())

def delete_search(conn, sid: int) -> bool:
    cur = conn.execute("DELETE FROM searches WHERE id=?", (sid,)); conn.commit()
    return cur.rowcount > 0
# ===== end search history =====

# ===== export history: one row per export of this shoot (selection, categories, people; local or Drive) =====
EXPORT_HISTORY = 10

def _export_row(r) -> dict:
    try: req = json.loads(r["req"]) if r["req"] else {}
    except Exception: req = {}
    return {"id": r["id"], "at": r["at"], "what": r["what"], "dest": r["dest"], "route": r["route"], "req": req if isinstance(req, dict) else {},
            "path": r["path"], "count": r["count"], "done": r["done"], "failed": r["failed"], "skipped": r["skipped"], "error": r["error"]}

def record_export(conn, what: str, dest: str, route: str, req: dict, path: str | None, done: int, failed: int, skipped: int,
                  error: str | None) -> int:
    """What went where: the request as sent (so Run again can send it again), the folder or Drive link it landed in,
    the counts. Rows past 5 x EXPORT_HISTORY go."""
    count = max(int(done or 0) - int(failed or 0) - int(skipped or 0), 0)
    cur = conn.execute("INSERT INTO exports(what, dest, route, req, path, count, done, failed, skipped, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (what, dest, route, json.dumps(req or {}), path, count, int(done or 0), int(failed or 0), int(skipped or 0), error))
    conn.execute("DELETE FROM exports WHERE id NOT IN (SELECT id FROM exports ORDER BY id DESC LIMIT ?)", (EXPORT_HISTORY * 5,))
    conn.commit()
    return cur.lastrowid

def list_exports(conn, n: int = EXPORT_HISTORY) -> list[dict]:
    return [_export_row(r) for r in conn.execute("SELECT * FROM exports ORDER BY id DESC LIMIT ?", (n,))]

def get_export(conn, eid: int) -> dict | None:
    r = conn.execute("SELECT * FROM exports WHERE id=?", (eid,)).fetchone()
    return _export_row(r) if r else None
# ===== end export history =====

def mark_missing(conn, present: set[str]) -> None:
    for (rel,) in conn.execute("SELECT rel FROM photos WHERE status='ok'").fetchall():
        if rel not in present:
            conn.execute("UPDATE photos SET status='missing' WHERE rel=?", (rel,))
    # A pending row holds nothing but the listing; one whose file is gone is simply dropped.
    for (rel,) in conn.execute("SELECT rel FROM photos WHERE status='pending'").fetchall():
        if rel not in present:
            conn.execute("DELETE FROM photos WHERE rel=? AND status='pending'", (rel,))
    conn.commit()

# ===== scan progress per file, and the jobs table =====
# A scan lists every file it found before it reads any of them: one row per file to do, status='pending'.
# The row moves to 'ok' when its features are stored (upsert_photo), 'error' when the read failed. From
# there the existing NULL columns say how far it got: embed NULL means not searchable yet, n_faces NULL
# means never checked for faces. So "how much of this shoot is scanned" is one query over the index,
# with or without the disk, and a resumed scan does exactly the rows that are not there yet.

def add_pending(conn, files) -> int:
    """List the files a scan is about to read: a row per (rel, size, mtime, is_video) with status='pending'.
    A known row for a changed or failed file goes back to pending with its new size and mtime; its old
    features stay until the re-read replaces them. Returns how many rows are pending now."""
    conn.executemany(
        "INSERT INTO photos(rel, size, mtime, status, kind) VALUES(?, ?, ?, 'pending', ?) "
        "ON CONFLICT(rel) DO UPDATE SET status='pending', size=excluded.size, mtime=excluded.mtime, kind=excluded.kind",
        [(f.rel, f.size, f.mtime, "video" if f.is_video else "photo") for f in files])
    conn.commit()
    return int(conn.execute("SELECT count(*) FROM photos WHERE status='pending'").fetchone()[0])

def scan_counts(conn) -> dict:
    """How far the shoot's scan got, from the index alone (no disk needed):
    items: rows the last scan listed (pending, ok and error; missing rows are not on the disk any more),
    scanned: rows read (status ok), embedded: scanned rows that are searchable, faced: scanned photos
    checked for faces (n_faces set; clips never are), errors: rows that could not be read,
    pending / pending_photos / pending_clips: listed but not read yet, unembedded: read but not searchable,
    photos / clips and scanned_photos / scanned_clips split the items and the scanned rows by kind,
    complete: nothing pending and every scanned row searchable (faces are optional and reported apart)."""
    r = conn.execute("""SELECT
        SUM(status IN ('pending','ok','error')), SUM(status='ok'),
        SUM(status='ok' AND embed IS NOT NULL),
        SUM(status='ok' AND COALESCE(kind,'photo') != 'video' AND n_faces IS NOT NULL),
        SUM(status='error'),
        SUM(status='pending'), SUM(status='pending' AND COALESCE(kind,'photo') != 'video'), SUM(status='pending' AND kind='video'),
        SUM(status IN ('pending','ok','error') AND COALESCE(kind,'photo') != 'video'), SUM(status IN ('pending','ok','error') AND kind='video'),
        SUM(status='ok' AND COALESCE(kind,'photo') != 'video'), SUM(status='ok' AND kind='video')
        FROM photos""").fetchone()
    n = [int(x or 0) for x in r]
    out = dict(items=n[0], scanned=n[1], embedded=n[2], faced=n[3], errors=n[4],
               pending=n[5], pending_photos=n[6], pending_clips=n[7],
               photos=n[8], clips=n[9], scanned_photos=n[10], scanned_clips=n[11])
    out["unembedded"] = out["scanned"] - out["embedded"]
    out["complete"] = out["pending"] == 0 and out["unembedded"] == 0
    return out

# jobs: one row per background job (scan, focus) with its state and last progress, so a job that the
# server never finished (the app quit, the Mac died) is found again as 'interrupted' the next time the
# shoot opens, instead of being forgotten. States: running, done, failed, interrupted.

def start_job(conn, kind: str, progress: dict | None = None) -> int:
    cur = conn.execute("INSERT INTO jobs(kind, state, progress) VALUES(?, 'running', ?)",
                       (kind, json.dumps(progress or {})))
    conn.commit()
    return int(cur.lastrowid)

def job_progress(conn, job_id: int, progress: dict) -> None:
    conn.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(progress), job_id)); conn.commit()

def finish_job(conn, job_id: int, state: str, error: str | None = None, progress: dict | None = None) -> None:
    if state not in ("done", "failed", "interrupted"):
        raise ValueError(f"not a final job state: {state!r}")
    if progress is not None:
        conn.execute("UPDATE jobs SET state=?, error=?, finished=datetime('now'), progress=? WHERE id=?",
                     (state, error, json.dumps(progress), job_id))
    else:
        conn.execute("UPDATE jobs SET state=?, error=?, finished=datetime('now') WHERE id=?", (state, error, job_id))
    conn.commit()

def interrupt_running_jobs(conn) -> int:
    """Called when a shoot is opened: nothing can be running for it yet, so any 'running' row was cut
    short by a quit or a crash. Returns how many rows were marked interrupted."""
    cur = conn.execute("UPDATE jobs SET state='interrupted', finished=datetime('now') WHERE state='running'")
    conn.commit()
    return cur.rowcount

def latest_jobs(conn) -> dict[str, dict]:
    """kind -> the most recent job row of that kind (id, kind, state, started, finished, progress dict, error)."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT id, kind, state, started, finished, progress, error FROM jobs ORDER BY id DESC"):
        if r[1] in out:
            continue
        try:
            prog = json.loads(r[5]) if r[5] else {}
        except ValueError:
            prog = {}
        out[r[1]] = dict(id=r[0], kind=r[1], state=r[2], started=r[3], finished=r[4], progress=prog, error=r[6])
    return out
# ===== end scan progress and jobs =====
