from __future__ import annotations
import errno, json, os, shutil, sqlite3, time
from pathlib import Path
import numpy as np
from .config import DB_NAME, EMBED_DIM, MODEL_TAG, app_home, shoot_slug
from . import pile

# float16 storage, so a stored blob's byte length names its width unambiguously. A blob whose length is
# not EMBED_BYTES was written by a different encoder (or is corrupt) and must never be treated as data:
# see the loaders and photos_missing_embed/segments_missing_embed below, and stale_embeds.
EMBED_BYTES = EMBED_DIM * 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL, qhash TEXT,
  sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT,
  sharp_tile REAL, sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0,
  embed BLOB, status TEXT DEFAULT 'ok', indexed_at TEXT DEFAULT (datetime('now')),
  category TEXT, category_score REAL, kind TEXT DEFAULT 'photo', duration REAL,
  category_guess TEXT, category_guess_score REAL, cluster TEXT, cluster_score REAL, aerial INTEGER DEFAULT 0,
  focus TEXT, focus_score REAL, lat REAL, lon REAL, shot_size TEXT, shot_size_score REAL, fps REAL,
  gamma TEXT, capture_fps REAL, people INTEGER, face_h REAL,
  flag TEXT, rating INTEGER DEFAULT 0, label TEXT,
  exposure_s REAL, fnumber REAL, iso INTEGER, ev_bias REAL, focal_mm REAL, lens TEXT, exp_mode INTEGER, subsec REAL, flash INTEGER, flicker TEXT,
  problem TEXT);
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
CREATE TABLE IF NOT EXISTS clip_faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  frame INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER, score REAL, embed BLOB);
CREATE TABLE IF NOT EXISTS photo_categories(
  photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE, category TEXT NOT NULL, score REAL,
  PRIMARY KEY(photo_id, category));
CREATE TABLE IF NOT EXISTS products(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, product INTEGER NOT NULL, variant INTEGER NOT NULL,
  view INTEGER);
CREATE TABLE IF NOT EXISTS product_sets(
  product INTEGER NOT NULL, variant INTEGER NOT NULL, name TEXT, named INTEGER DEFAULT 0, colour TEXT, hex TEXT,
  n INTEGER, views INTEGER, renamed INTEGER DEFAULT 0, PRIMARY KEY(product, variant));
CREATE INDEX IF NOT EXISTS photo_categories_category ON photo_categories(category, photo_id);
CREATE INDEX IF NOT EXISTS clip_faces_photo ON clip_faces(photo_id);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS segments_photo ON segments(photo_id);
"""
# Covering indexes for every per-render count: a photos row carries a 1 KB embedding (1.5 KB under
# SigLIP's 768-dim float16), so a "SCAN photos" for a count reads the whole table (24 MB at 20k rows,
# 36 MB under SigLIP, 6 to 10 ms per query, 34 ms for /api/categories); with these each count walks a
# narrow index instead (under 1 ms). Kept out of SCHEMA so they are created after the column migrations
# below (an old index.db predates category, cluster, aerial and focus).
INDEXES = """
CREATE INDEX IF NOT EXISTS photos_status_category ON photos(status, category, category_guess);
CREATE INDEX IF NOT EXISTS photos_status_cluster ON photos(status, cluster);
CREATE INDEX IF NOT EXISTS photos_status_aerial ON photos(status, aerial);
CREATE INDEX IF NOT EXISTS photos_status_kind ON photos(status, kind);
CREATE INDEX IF NOT EXISTS photos_status_faces ON photos(status, n_faces);
CREATE INDEX IF NOT EXISTS photos_status_focus ON photos(status, focus);
CREATE INDEX IF NOT EXISTS photos_status_rel ON photos(status, rel);
CREATE INDEX IF NOT EXISTS photos_status_shot_size ON photos(status, shot_size);
CREATE INDEX IF NOT EXISTS photos_status_kind_fps ON photos(status, kind, fps);
CREATE INDEX IF NOT EXISTS photos_status_kind_rates ON photos(status, kind, fps, capture_fps);
CREATE INDEX IF NOT EXISTS photos_status_camera ON photos(status, camera);
CREATE INDEX IF NOT EXISTS photos_status_kind_gamma ON photos(status, kind, gamma);
CREATE INDEX IF NOT EXISTS photos_status_kind_duration ON photos(status, kind, duration);
CREATE INDEX IF NOT EXISTS photos_status_kind_people ON photos(status, kind, people);
CREATE INDEX IF NOT EXISTS photos_status_marks ON photos(status, flag, rating, label);
"""

PHOTO_COLS = ["rel","size","mtime","qhash","sibling","width","height","taken_at","camera","phash",
              "sharp_tile","sharp_max","sharp_eye","sharp","n_faces","status","kind","duration","aerial","lat","lon","fps",
              "gamma","capture_fps","people","face_h",
              "exposure_s","fnumber","iso","ev_bias","focal_mm","lens","exp_mode","subsec","flash","flicker"]

def is_disk_full(e: BaseException) -> bool:
    """True for the two ways a full disk reaches us: an OS write, and SQLite's SQLITE_FULL (13), which sqlite3 raises
    as a plain OperationalError with the code in its low byte."""
    if isinstance(e, OSError):
        return e.errno in (errno.ENOSPC, errno.EDQUOT)
    if isinstance(e, sqlite3.Error):
        code = getattr(e, "sqlite_errorcode", None)
        return (code is not None and code & 0xFF == sqlite3.SQLITE_FULL) or "disk is full" in str(e)
    return False

def index_dir(root: Path) -> Path:
    d = app_home() / shoot_slug(root)
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    (d / "grid").mkdir(parents=True, exist_ok=True)
    (d / "frames").mkdir(parents=True, exist_ok=True)     # sampled video frames, <qhash>_<k>.jpg
    return d

CAMERA_SCRUB_KEY = "camera_nuls_scrubbed"   # meta: set once connect has cleaned NUL-padded camera names

def _unpad_camera(c: str) -> str | None:
    """A camera stored as f"{make} {model}" with both NUL-padded, rebuilt the way features._camera builds it
    now. The padded make ends in a NUL, so the first NUL-then-space is the seam between the two."""
    seam = c.find("\x00 ")
    if seam < 0:
        return c.replace("\x00", "").strip() or None
    make, model = c[:seam].replace("\x00", "").strip(), c[seam + 2:].replace("\x00", "").strip()
    if not model:
        return make or None
    return f"{make} {model}" if make and make not in model else model

def durable(conn) -> None:
    """Call before a write a person made by hand: their marks, picks and names. macOS fsync only reaches the drive's
    cache, so a power cut can take a commit SQLite was told is safe; F_FULLFSYNC goes all the way. Measured on the M1
    with one set_marks: 0.08 ms a commit without, 6 ms with (59 ms at the 95th percentile), which nobody notices on a
    keypress and would cost minutes on a scan's per-file commits, so scans leave it off."""
    conn.execute("PRAGMA fullfsync=ON")

# ===== the index keeps itself: an integrity check when a shoot opens, and a few rolling backups. Measured at 20,000
# photos and 40,000 faces (57 MB): the check 0.18 s, the backup 0.19 s, and three backups come to about 170 MB. =====
BACKUP_DIR = "backups"
BACKUPS_KEEP = 3
BACKUP_EVERY_S = 3600        # one an hour at most, so opening a shoot a few times does not roll the older backups away

def _sound(path: Path) -> bool:
    try:
        c = sqlite3.connect(path, timeout=10)
        try:
            return c.execute("PRAGMA integrity_check(1)").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.OperationalError as e:
        return "malformed" not in str(e) and "not a database" not in str(e)   # busy or locked says nothing about the file
    except sqlite3.DatabaseError:
        return False

def list_backups(root: Path) -> list[Path]:
    """Newest first."""
    d = index_dir(root) / BACKUP_DIR
    return sorted(d.glob("index-*.db"), key=lambda p: p.name, reverse=True) if d.is_dir() else []

def _write_backup(root: Path) -> Path | None:
    d = index_dir(root); src = d / DB_NAME
    size = src.stat().st_size
    if shutil.disk_usage(d).free < 2 * size:
        return None                                    # a backup must never be what fills the disk
    (d / BACKUP_DIR).mkdir(exist_ok=True)
    out = d / BACKUP_DIR / f"index-{time.strftime('%Y%m%d-%H%M%S')}.db"
    tmp = out.with_suffix(".part")
    a, b = sqlite3.connect(src, timeout=10), sqlite3.connect(tmp)
    try:
        a.backup(b)
    finally:
        b.close(); a.close()
    os.replace(tmp, out)
    for old in list_backups(root)[BACKUPS_KEEP:]:
        old.unlink(missing_ok=True)
    return out

def open_check(root: Path) -> str | None:
    """Run when a shoot opens, before anything reads the index. A sound index gets a backup when it has changed since the
    last one and that one is over an hour old. A damaged index is set aside under backups/ (nothing is deleted) and the
    newest backup, which passed this same check when it was made, takes its place. Returns a line for the person when
    that happened, else None. Never raises: a shoot that cannot be checked still opens."""
    try:
        d = index_dir(root); f = d / DB_NAME
        if not f.is_file() or f.stat().st_size == 0:
            return None
        if _sound(f):
            have = list_backups(root)
            newest = have[0].stat().st_mtime if have else 0
            changed = max(p.stat().st_mtime for p in (f, f.with_name(DB_NAME + "-wal")) if p.exists())
            if changed > newest and time.time() - newest >= BACKUP_EVERY_S:
                _write_backup(root)
            return None
        have = list_backups(root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (d / BACKUP_DIR).mkdir(exist_ok=True)
        if not have:
            return "This shoot's index is damaged and there is no backup of it. Scan the folder again to rebuild it."
        for suffix in ("", "-wal", "-shm"):
            p = f.with_name(DB_NAME + suffix)
            if p.exists():
                os.replace(p, d / BACKUP_DIR / f"damaged-{stamp}{suffix or '.db'}")
        shutil.copyfile(have[0], f)
        when = time.strftime("%d %b at %H:%M", time.localtime(have[0].stat().st_mtime))
        return f"This shoot's index was damaged, so sorted went back to the backup from {when}. Marks made after that are gone."
    except (OSError, sqlite3.Error):
        return None
# ===== end the index keeps itself =====

def connect(root: Path) -> sqlite3.Connection:
    d = index_dir(root)
    conn = sqlite3.connect(d / DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA foreign_keys=ON")
    # A checkpoint moves committed pages out of the WAL and then reuses it, so it has to reach the platter for a durable
    # commit (see durable) to stay durable, whichever connection runs it. It happens every few MB, so it costs nothing.
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
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
    # lat, lon: where the file says it was shot, signed decimal degrees. Read from the file's own metadata
    # only (EXIF GPS for a photo, the container's ISO 6709 tag for a clip), never looked up anywhere.
    if "lat" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN lat REAL")
    if "lon" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN lon REAL")
    # shot_size: close / medium / wide from the zero-shot pass in classify.classify_shot_size, with the
    # softmax share it won on. NULL means never scored (an index from before the column, or a row with no
    # embedding); "unknown" means scored and the gates refused to pick, which is a different fact and has
    # to stay tellable apart. Cleared with embed when a changed file is re-decoded, like category.
    if "shot_size" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN shot_size TEXT")
    if "shot_size_score" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN shot_size_score REAL")
    # fps: the clip's own frame rate, straight off the container (video.probe already computes it and
    # _process_video used to throw it away). NULL on every still, and on a clip indexed before the column
    # existed; index.read_fps is the catch-up pass that fills those in without re-decoding anything.
    if "fps" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN fps REAL")
    # The clip facts behind the video filters, all NULL on a still and on a clip indexed before they existed
    # (index.read_clip_info fills the first two in from the header and the sidecar, index.clip_faces the
    # other two from the frames already under the index; neither re-decodes a clip).
    # gamma: Sony's CaptureGammaEquation off the <stem>M01.XML sidecar ("s-log3-cine", "rec709"). NULL means
    #   never read; '' means read and the clip says nothing (no sidecar: phones, DJI). Both are "unknown" to
    #   the picture profile filter, and only the second is done.
    # capture_fps: the sidecar's captureFps, the rate an S&Q clip was shot at before the camera conformed it.
    # people: the most faces YuNet found on any one sampled frame of the clip; NULL means not looked for.
    #   Kept apart from n_faces on purpose: n_faces >= 1 makes a row "people" in classify, and on a clip that
    #   would overrule the long-interview rule.
    # face_h: the median, over the frames that had one, of the largest face's height as a share of the frame's
    #   short edge; what shot size reads to tell a medium from a wide (classify.FACE_MEDIUM_MIN_H).
    for col, typ in (("gamma", "TEXT"), ("capture_fps", "REAL"), ("people", "INTEGER"), ("face_h", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {typ}")
    # ===== marks: flag (pick / reject / NULL), rating (0 to 5 stars) and a colour label, set by hand in the
    # grid. They are a person's decisions, not derived from the file, so upsert_photo never touches them
    # (they are not in PHOTO_COLS) and a re-decoded file keeps its stars. See set_marks. =====
    if "flag" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN flag TEXT")
    if "rating" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN rating INTEGER DEFAULT 0")
    if "label" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN label TEXT")
    # ===== camera settings (features.SETTING_KEYS): what the camera recorded about each still's exposure, read
    # off the standard EXIF sub-IFD at index time. lens NULL means never read (a shoot indexed before these
    # columns: index.read_camera_info is the catch-up), '' means read and the file names no lens. =====
    for col, typ in (("exposure_s", "REAL"), ("fnumber", "REAL"), ("iso", "INTEGER"), ("ev_bias", "REAL"),
                     ("focal_mm", "REAL"), ("lens", "TEXT"), ("exp_mode", "INTEGER"), ("subsec", "REAL"),
                     ("flash", "INTEGER"), ("flicker", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {typ}")
    # problem: why this file was set aside (it crashed the reader, or hung), in a person's words; shown in the list of
    # files that could not be read. NULL for every other row, plain decode errors included.
    if "problem" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN problem TEXT")
    # renamed: a person gave this product its name, so a new Find products keeps it (products._kept_names).
    if "renamed" not in {r[1] for r in conn.execute("PRAGMA table_info(product_sets)")}:
        conn.execute("ALTER TABLE product_sets ADD COLUMN renamed INTEGER DEFAULT 0")
    conn.executescript(INDEXES)
    conn.commit()
    # camera: features.exif_info used to hand back a DJI DNG's Make and Model padded with NULs to their
    # fixed field length, so an older index holds 'DJI\x00...\x00 FC7303\x00...' and the DNG never groups
    # with the 'DJI FC7303' on its JPG. The clean string is fully recoverable from the stored one, so this
    # fixes those rows in place, once, with no file re-read. Filtered in Python, not with SQL's replace()
    # and char(0), so no SQLite string function gets to stop at the first NUL.
    if get_meta(conn, CAMERA_SCRUB_KEY) is None:
        bad = [(_unpad_camera(c), i) for i, c in
               conn.execute("SELECT id, camera FROM photos WHERE camera IS NOT NULL") if "\x00" in c]
        conn.executemany("UPDATE photos SET camera=? WHERE id=?", bad)
        set_meta(conn, CAMERA_SCRUB_KEY, "done")
    # The pile and the pick flag become one list, once per shoot, so an index or project file from before that catches up here.
    try:
        pile.merge_once(conn)
    except sqlite3.OperationalError:
        pass                                           # the index is busy: the next connect does it, this one must still open
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
                 "category_guess=NULL, category_guess_score=NULL, cluster=NULL, cluster_score=NULL, focus=NULL, focus_score=NULL, "
                 "shot_size=NULL, shot_size_score=NULL, problem=NULL, indexed_at=datetime('now')",
                 [row.get(c) for c in PHOTO_COLS])
    pid = conn.execute("SELECT id FROM photos WHERE rel=?", (row["rel"],)).fetchone()[0]
    conn.execute("DELETE FROM photo_categories WHERE photo_id=?", (pid,))   # the secondaries go with the category
    conn.commit()
    return pid

def mark_error(conn, rel: str, size: int, mtime: float, problem: str | None = None) -> None:
    """Flag a file that could not be read this pass. Only status/size/mtime move: qhash, embed,
    category and faces from an earlier good pass stay, so a retry after a disk hiccup does not
    have to re-decode and re-embed. A file never seen before gets a minimal error row. problem is
    the reason when the file took a worker down with it."""
    cur = conn.execute("UPDATE photos SET status='error', size=?, mtime=?, problem=?, indexed_at=datetime('now') WHERE rel=?",
                       (size, mtime, problem, rel))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO photos(rel, size, mtime, status, n_faces, problem) VALUES(?, ?, ?, 'error', 0, ?)",
                     (rel, size, mtime, problem))
    conn.commit()

def set_problem(conn, rel: str, problem: str | None) -> None:
    """Why a pass had to step over a file it otherwise knows. The row keeps its status."""
    conn.execute("UPDATE photos SET problem=? WHERE rel=?", (problem, rel))
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

def _checked(vec: np.ndarray) -> bytes:
    """The float16 bytes of one embedding, or ValueError. A short write is what poisons an index: the
    blob would sit invisible under the EMBED_BYTES guard until someone wondered why a photo never became
    searchable. Not an assert, because python -O strips those and the shipped app may well run that way."""
    vec = np.asarray(vec, np.float16)
    if vec.shape != (EMBED_DIM,):
        raise ValueError(f"embed is {vec.shape} wide, EMBED_DIM is {EMBED_DIM}")
    return vec.tobytes()

def set_embed(conn, photo_id: int, vec: np.ndarray) -> None:
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (_checked(vec), photo_id))

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
    conn.execute("UPDATE segments SET embed=? WHERE id=?", (_checked(vec), seg_id))

def segments_missing_embed(conn) -> list[tuple[int, int, str]]:
    """(segment id, photo id, frame filename) for every segment of an ok video that has no embedding yet,
    or whose embedding is the wrong width for the current encoder (a stale blob from another model,
    treated as absent so the ordinary Continue pass re-embeds it)."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT s.id, s.photo_id, s.frame FROM segments s JOIN photos p ON p.id=s.photo_id "
        "WHERE (s.embed IS NULL OR length(s.embed)<>?) AND p.status='ok' ORDER BY s.photo_id, s.idx", (EMBED_BYTES,))]

def load_segment_embeds(conn):
    """(ids, M) for every embedded segment of an ok video whose blob matches EMBED_BYTES, float32
    (n, EMBED_DIM), rows in id order. A blob from another encoder is a different width and is filtered
    out by the length check rather than crashing the stack below on a ragged shape."""
    rows = conn.execute("SELECT s.id, s.embed FROM segments s JOIN photos p ON p.id=s.photo_id "
                        "WHERE s.embed IS NOT NULL AND length(s.embed)=? AND p.status='ok' ORDER BY s.id", (EMBED_BYTES,)).fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros((0, EMBED_DIM), np.float32)
    ids = np.array([r[0] for r in rows], np.int64)
    M = np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows])
    return ids, M

def photos_missing_embed(conn) -> list[tuple[int, str]]:
    """Rows with no embedding yet, or an embedding the wrong width for the current encoder: both are
    the Continue pass's job, since a wrong-width blob is invisible to search either way (see EMBED_BYTES)."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT id, rel FROM photos WHERE (embed IS NULL OR length(embed)<>?) AND status='ok' ORDER BY id", (EMBED_BYTES,))]

def load_embeds(conn):
    """(ids, M) for every ok photo whose embedding matches EMBED_BYTES, float32 (n, EMBED_DIM), id order.
    A blob left over from a different encoder is a different byte length and is skipped here, never
    stacked into M: see the hazard this guards against in db.EMBED_BYTES."""
    rows = conn.execute("SELECT id, embed FROM photos WHERE embed IS NOT NULL AND length(embed)=? AND status='ok' ORDER BY id", (EMBED_BYTES,)).fetchall()
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
    durable(conn)
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

# Which encoder filled this index. Stamped once per scan (index.py, beside last_index), not from
# set_embed: that is called thousands of times a scan and meta is one row, not worth a write each time.
# embed_model is None on every index written before this task, which is exactly the state that means
# "assume nothing, let the byte-length guard (EMBED_BYTES) sort it out."
def stamp_model(conn) -> None:
    set_meta(conn, "embed_model", MODEL_TAG)

def embed_model(conn) -> str | None:
    return get_meta(conn, "embed_model")

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
    pile.settle(conn, [x[0] for r in rels for x in conn.execute("SELECT id FROM photos WHERE rel=?", (r,))])
    conn.commit()

def photos_without_faces(conn, retry: bool = False) -> set[str]:
    """Photos indexed with faces off (n_faces NULL). They need a second pass when faces are wanted. One the face pass
    had to set aside is left out until a person asks for a retry, or every scan would pay for the same crash."""
    q = "SELECT rel FROM photos WHERE n_faces IS NULL AND status='ok'"
    return {r[0] for r in conn.execute(q if retry else q + " AND problem IS NULL")}

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
    # And every other category a row fits (classify.SECOND_MIN_PROB): a tile counts what clicking it shows.
    for cat, n in category_also_counts(conn).items():
        counts[cat] = counts.get(cat, 0) + n
    return counts

# The ok ids come off a covering (status, ...) index: a join to photos would read each row past its embed.
CATEGORY_ALSO_COUNTS_SQL = ("SELECT category, COUNT(*) FROM photo_categories "
                            "WHERE photo_id IN (SELECT id FROM photos WHERE status='ok') GROUP BY category")

def category_also_counts(conn) -> dict[str, int]:
    """category -> ok rows listed there as a secondary only (photo_categories), so a tile can say how much of
    its number is rows filed elsewhere."""
    return {r[0]: r[1] for r in conn.execute(CATEGORY_ALSO_COUNTS_SQL)}

def replace_photo_categories(conn, rows: list[tuple[int, str, float]]) -> None:
    """Every secondary category of the shoot as (photo_id, category, score), replacing whatever was there:
    classify_and_store scores every row in one pass, so it writes them all at once."""
    conn.execute("DELETE FROM photo_categories")
    conn.executemany("INSERT OR REPLACE INTO photo_categories(photo_id, category, score) VALUES(?,?,?)", rows)
    conn.commit()

def load_photo_categories(conn) -> dict[int, dict[str, float]]:
    """photo_id -> {category: score} for every row with a secondary."""
    out: dict[int, dict[str, float]] = {}
    for pid, cat, score in conn.execute("SELECT photo_id, category, score FROM photo_categories ORDER BY photo_id, score DESC"):
        out.setdefault(pid, {})[cat] = score
    return out

def aerial_count(conn) -> int:
    """Drone shots (photos and videos) among status='ok' rows: the "drone" tile."""
    return int(conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND aerial=1").fetchone()[0])

def shot_size_counts(conn) -> dict[str, int]:
    """shot_size -> count over status='ok' rows, in close, medium, wide, unknown order, then whatever
    else is in there. A NULL (never scored) is reported as "unscored", which is not the same row as
    "unknown": unscored means the pass has not run over it, unknown means it ran and the confidence
    gates refused to pick. The facet rail needs both numbers to say anything honest about coverage."""
    rows = conn.execute("SELECT COALESCE(shot_size, 'unscored') AS s, COUNT(*) FROM photos WHERE status='ok' GROUP BY s").fetchall()
    counts = {r[0]: r[1] for r in rows}
    order = ["close", "medium", "wide", "unknown", "unscored"]
    return {k: counts[k] for k in order if k in counts} | {k: v for k, v in counts.items() if k not in order}

# The facet counts as named SQL, so the covering index test plans exactly what runs.
SLOWMO_COUNTS_SQL = ("SELECT SUM(MAX(COALESCE(fps, 0), COALESCE(capture_fps, 0)) >= ?), "
                     "SUM((fps IS NOT NULL OR capture_fps IS NOT NULL) AND MAX(COALESCE(fps, 0), COALESCE(capture_fps, 0)) < ?), "
                     "SUM(fps IS NULL AND capture_fps IS NULL) FROM photos WHERE status='ok' AND kind='video'")
CAMERA_COUNTS_SQL = ("SELECT camera, COUNT(*) AS n FROM photos WHERE status='ok' AND camera IS NOT NULL AND camera <> '' "
                     "GROUP BY camera ORDER BY n DESC, camera")
LOOK_COUNTS_SQL = ("SELECT SUM(gamma LIKE 's-log%'), SUM(gamma <> '' AND gamma NOT LIKE 's-log%'), "
                   "SUM(gamma IS NULL OR gamma = '') FROM photos WHERE status='ok' AND kind='video'")
CLIP_FACES_PENDING_SQL = "SELECT COUNT(*), SUM(people IS NULL) FROM photos WHERE status='ok' AND kind='video'"

def _duration_sql() -> str:
    from .config import DURATION_BUCKETS
    parts = [f"SUM(duration >= {lo!r}" + (f" AND duration < {hi!r})" if hi != float("inf") else ")")
             for _, lo, hi in DURATION_BUCKETS]
    return f"SELECT {', '.join(parts)} FROM photos WHERE status='ok' AND kind='video'"
DURATION_COUNTS_SQL = _duration_sql()

def slowmo_counts(conn) -> dict[str, int]:
    """{slow, normal, unknown} over the clips of a shoot, by the larger of photos.fps (the container) and
    photos.capture_fps (Sony's sidecar, the rate an S&Q clip was shot at) against SLOWMO_MIN_FPS. Stills
    never appear: a frame rate is not a thing a still has, so it is absent rather than "normal". unknown
    is a clip with neither rate (indexed before the column, or ffprobe could not say)."""
    from .config import SLOWMO_MIN_FPS
    r = conn.execute(SLOWMO_COUNTS_SQL, (SLOWMO_MIN_FPS, SLOWMO_MIN_FPS)).fetchone()
    return {"slow": int(r[0] or 0), "normal": int(r[1] or 0), "unknown": int(r[2] or 0)}

def camera_counts(conn) -> dict[str, int]:
    """camera -> count over every ok row, stills and clips together (a body shoots both, and the filter
    keeps both), most used first. A row with no recorded camera is not a camera and is left out."""
    return {r[0]: r[1] for r in conn.execute(CAMERA_COUNTS_SQL)}

def look_counts(conn) -> dict[str, int]:
    """{log, standard, unknown} over the clips: log is the S-Log family (video.is_log), standard is any other
    profile the sidecar named, unknown is a clip whose sidecar was never read (NULL) or that has none ('')."""
    r = conn.execute(LOOK_COUNTS_SQL).fetchone()
    return {"log": int(r[0] or 0), "standard": int(r[1] or 0), "unknown": int(r[2] or 0)}

def duration_counts(conn) -> dict[str, int]:
    """config.DURATION_BUCKETS key -> clips in it. A clip with no duration falls in none of them."""
    from .config import DURATION_BUCKETS
    r = conn.execute(DURATION_COUNTS_SQL).fetchone()
    return {key: int(r[i] or 0) for i, (key, _, _) in enumerate(DURATION_BUCKETS)}

def replace_clip_faces(conn, photo_id: int, faces: list[dict]) -> None:
    """The faces found on one clip's sampled frames: frame is the k of frames/<qhash>_<k>.jpg, the box is in
    that frame's pixels, embed is the SFace vector (float32 bytes) a person search will match against."""
    conn.execute("DELETE FROM clip_faces WHERE photo_id=?", (photo_id,))
    conn.executemany("INSERT INTO clip_faces(photo_id, frame, x, y, w, h, score, embed) VALUES(?,?,?,?,?,?,?,?)",
                     [(photo_id, f["frame"], f["x"], f["y"], f["w"], f["h"], f["score"], f["embed"]) for f in faces])
    conn.commit()

def stale_embeds(conn) -> int:
    """How many ok photos plus segments of ok videos hold an embedding blob of the wrong byte length for
    the current encoder (EMBED_BYTES): a real blob, just written by a different model. This is the number
    the UI needs to say "955 photos were embedded with a different model", distinct from unembedded
    (NULL or wrong length) because a fresh shoot with nothing embedded yet has plenty of the latter
    and none of this."""
    p = conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND embed IS NOT NULL AND length(embed)<>?", (EMBED_BYTES,)).fetchone()[0]
    s = conn.execute("SELECT COUNT(*) FROM segments s JOIN photos p ON p.id=s.photo_id "
                     "WHERE p.status='ok' AND s.embed IS NOT NULL AND length(s.embed)<>?", (EMBED_BYTES,)).fetchone()[0]
    return int(p) + int(s)

def cluster_counts(conn) -> dict[str, int]:
    """discovered category name -> count for status='ok' photos, largest first. Empty until discover_and_store ran."""
    rows = conn.execute("SELECT cluster, COUNT(*) AS n FROM photos WHERE status='ok' AND cluster IS NOT NULL GROUP BY cluster ORDER BY n DESC, cluster").fetchall()
    return {r[0]: r[1] for r in rows}

# ===== marks: flags, stars and colour labels per item (photos and clips alike) =====
FLAGS = ("pick", "reject")
LABELS = ("red", "yellow", "green", "blue", "purple")     # Lightroom's default label set, in its order
_UNSET = object()

def _check_marks(flag, rating, label) -> None:
    if flag is not _UNSET and flag is not None and flag not in FLAGS:
        raise ValueError(f"flag must be pick, reject or none, not {flag!r}")
    if rating is not _UNSET and (type(rating) is not int or not 0 <= rating <= 5):
        raise ValueError(f"rating must be a whole number from 0 to 5, not {rating!r}")
    if label is not _UNSET and label is not None and label not in LABELS:
        raise ValueError(f"label must be one of {', '.join(LABELS)} or none, not {label!r}")

def set_marks(conn, ids, flag=_UNSET, rating=_UNSET, label=_UNSET) -> int:
    """Set any of flag (pick / reject / None), rating (0 to 5) and label (one of LABELS / None) on these rows;
    an argument left out is left alone, so a P keystroke on a selection does not wipe its stars. Every value is
    checked before anything is written. A flag keeps the Picked list in step: pick joins it, reject or None leaves it
    (see pile.py). Returns how many rows exist among ids."""
    _check_marks(flag, rating, label)
    durable(conn)
    sets, vals = [], []
    for col, v in (("flag", flag), ("rating", rating), ("label", label)):
        if v is not _UNSET:
            sets.append(f"{col}=?"); vals.append(v)
    ids = [int(i) for i in ids]; n = 0
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        if sets:
            n += conn.execute(f"UPDATE photos SET {','.join(sets)} WHERE id IN ({q})", vals + chunk).rowcount
        else:
            n += conn.execute(f"SELECT count(*) FROM photos WHERE id IN ({q})", chunk).fetchone()[0]
    if flag is not _UNSET:
        (pile.add if flag == "pick" else pile.remove)(conn, ids)
    conn.commit()
    return n

def get_marks(conn, ids) -> dict[int, dict]:
    """id -> {flag, rating, label} for the rows among ids that exist; rating is 0 for an unrated row."""
    ids = [int(i) for i in ids]; out = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT id, flag, COALESCE(rating, 0), label FROM photos WHERE id IN ({q})", chunk):
            out[r[0]] = {"flag": r[1], "rating": int(r[2]), "label": r[3]}
    return out

def is_marked(m: dict | None) -> bool:
    """True when a {flag, rating, label} carries anything at all."""
    return bool(m) and bool(m.get("flag") or m.get("rating") or m.get("label"))

def mark_counts(conn) -> dict:
    """The sidebar's numbers over status='ok' rows: flag {pick, reject, none}, stars {"1".."5": rows with AT
    LEAST that many}, label {each of LABELS, none}, marked: rows with any mark at all."""
    flag = {"pick": 0, "reject": 0, "none": 0}
    for f, n in conn.execute("SELECT flag, COUNT(*) FROM photos WHERE status='ok' GROUP BY flag"):
        flag[f if f in FLAGS else "none"] += n
    by = dict(conn.execute("SELECT COALESCE(rating, 0), COUNT(*) FROM photos WHERE status='ok' GROUP BY 1").fetchall())
    stars = {str(k): sum(v for r, v in by.items() if r >= k) for k in range(1, 6)}
    label = {k: 0 for k in LABELS} | {"none": 0}
    for lb, n in conn.execute("SELECT label, COUNT(*) FROM photos WHERE status='ok' GROUP BY label"):
        label[lb if lb in LABELS else "none"] += n
    marked = conn.execute("SELECT COUNT(*) FROM photos WHERE status='ok' AND (flag IS NOT NULL OR COALESCE(rating, 0) > 0 "
                          "OR label IS NOT NULL)").fetchone()[0]
    return {"flag": flag, "stars": stars, "label": label, "marked": int(marked)}
# ===== end marks =====

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

SAMPLE_MAX = 240     # the most cards a Scan tab sample may ask for

def scan_sample(conn, n: int = 120, since: int = 0, ids=(), folders: bool = True) -> dict:
    """The Scan tab's desktop: a fixed, evenly spaced sample of the files the scan listed (pending, ok and error,
    the population scan_counts calls items), and the true counts the folders show.

    rows: every k-th listed file in id order (k = ceil(items / n), so at most n rows), only those with id > since,
    each {id, name, video, qhash, category, width, height, duration, error, looked}. qhash is None until features has
    read the file (no thumb yet), looked False until embed has, category None until categorise has, error True for a
    file that could not be read (it never gets a thumb). The sample is the same on every call while the listing
    is the same, so a client pages forward with since = the last id it holds and gets nothing twice.
    updates: the same fields for the ids asked for, the cards a client holds that still lack a thumb or a folder.
    max_id: the last id in rows (or since when there are none). folders: [{kind, name, n}] per kind ("photos" or
    "videos") and category over the read files, largest first; empty until something is categorised, and not counted
    at all with folders=False (a GROUP BY over every row, which a client polling mid-scan has no use for)."""
    n = max(1, min(SAMPLE_MAX, int(n)))
    since = max(0, int(since))
    live = "status IN ('pending','ok','error')"
    c = conn.execute(f"SELECT count(*), SUM(kind='video') FROM photos WHERE {live}").fetchone()
    total, videos = int(c[0] or 0), int(c[1] or 0)
    out = {"total": total, "photos": total - videos, "videos": videos, "since": since, "max_id": since,
           "rows": [], "updates": [], "folders": []}
    if not total:
        return out
    k = -(-total // n)
    cols = ("id, rel, kind, CASE WHEN status='ok' THEN qhash END, CASE WHEN status='ok' THEN category END, width, height, duration, "
            "status='error', status='ok' AND embed IS NOT NULL AND length(embed)=%d, "
            "(SELECT x.product FROM products x WHERE x.photo_id=photos.id)" % EMBED_BYTES)

    def shape(r) -> dict:
        return {"id": r[0], "name": r[1].rsplit("/", 1)[-1], "video": r[2] == "video", "qhash": r[3], "category": r[4],
                "width": r[5], "height": r[6], "duration": r[7], "error": bool(r[8]), "looked": bool(r[9]), "product": r[10]}
    # The positions are picked over ids alone (photos_status_kind covers status and the rowid), and only the picked
    # rows are read: a shoot's rows carry their embeddings, and a window over whole rows reads every one of them.
    picked = [r[0] for r in conn.execute(
        f"SELECT id FROM (SELECT id, row_number() OVER (ORDER BY id) - 1 AS rn FROM photos WHERE {live}) "
        "WHERE rn % ? = 0 AND id > ? ORDER BY id", (k, since))]

    def fetch(idlist) -> list[dict]:
        if not idlist:
            return []
        q = ",".join("?" * len(idlist))
        return [shape(r) for r in conn.execute(f"SELECT {cols} FROM photos WHERE id IN ({q}) ORDER BY id", idlist)]
    out["rows"] = fetch(picked)
    # products: only when Find products has run, so the desktop stays as it was on a shoot without them
    found = conn.execute("SELECT product, MIN(name), SUM(n) FROM product_sets GROUP BY product ORDER BY SUM(n) DESC").fetchall()
    if found:
        out["products"] = [{"id": r[0], "name": r[1] or f"product {i}", "count": int(r[2] or 0)} for i, r in enumerate(found, 1)]
    if picked:
        out["max_id"] = picked[-1]
    out["updates"] = fetch(sorted({int(i) for i in ids})[:SAMPLE_MAX])
    if folders:
        # Every read file per category off photos_status_category, and the clips per category off photos_status_kind,
        # which reads only the clips' rows; the photos are the difference. Grouping by kind and category together
        # would read every row in the shoot, embedding and all.
        every = dict(conn.execute("SELECT category, count(*) FROM photos "
                                  "WHERE status='ok' AND category IS NOT NULL GROUP BY category"))
        clips = dict(conn.execute("SELECT category, count(*) FROM photos INDEXED BY photos_status_kind "
                                  "WHERE status='ok' AND kind='video' AND category IS NOT NULL GROUP BY category"))
        f = [{"kind": "photos", "name": c, "n": int(m) - int(clips.get(c, 0))} for c, m in every.items()]
        f += [{"kind": "videos", "name": c, "n": int(m)} for c, m in clips.items()]
        out["folders"] = sorted((x for x in f if x["n"] > 0), key=lambda x: (-x["n"], x["name"], x["kind"]))
    return out

def scan_counts(conn) -> dict:
    """How far the shoot's scan got, from the index alone (no disk needed):
    items: rows the last scan listed (pending, ok and error; missing rows are not on the disk any more),
    scanned: rows read (status ok), embedded: scanned rows that are searchable, faced: scanned photos
    checked for faces (n_faces set; clips never are), errors: rows that could not be read,
    pending / pending_photos / pending_clips: listed but not read yet, unembedded: read but not searchable
    (no embedding yet, or one of the wrong width for the current encoder), stale_embeds: of those, how
    many are a wrong-width blob rather than simply missing, embed_model: the encoder that filled this
    index (None for one written before this task, or one still empty), photos / clips and
    scanned_photos / scanned_clips split the items and the scanned rows by kind, complete: nothing
    pending and every scanned row searchable (faces are optional and reported apart)."""
    r = conn.execute("""SELECT
        SUM(status IN ('pending','ok','error')), SUM(status='ok'),
        SUM(status='ok' AND embed IS NOT NULL AND length(embed)=?),
        SUM(status='ok' AND COALESCE(kind,'photo') != 'video' AND n_faces IS NOT NULL),
        SUM(status='error'),
        SUM(status='pending'), SUM(status='pending' AND COALESCE(kind,'photo') != 'video'), SUM(status='pending' AND kind='video'),
        SUM(status IN ('pending','ok','error') AND COALESCE(kind,'photo') != 'video'), SUM(status IN ('pending','ok','error') AND kind='video'),
        SUM(status='ok' AND COALESCE(kind,'photo') != 'video'), SUM(status='ok' AND kind='video')
        FROM photos""", (EMBED_BYTES,)).fetchone()
    n = [int(x or 0) for x in r]
    out = dict(items=n[0], scanned=n[1], embedded=n[2], faced=n[3], errors=n[4],
               pending=n[5], pending_photos=n[6], pending_clips=n[7],
               photos=n[8], clips=n[9], scanned_photos=n[10], scanned_clips=n[11])
    out["unembedded"] = out["scanned"] - out["embedded"]
    out["complete"] = out["pending"] == 0 and out["unembedded"] == 0
    out["stale_embeds"] = stale_embeds(conn)
    out["embed_model"] = embed_model(conn)
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
