"""The pile: the one list of what an editor has picked on a shoot, in the order they were picked. The UI calls it Picked.

The grid's selection is the pile. It lives in the shoot's index.db (table pile), not in the browser, so it
survives a new search, a reload and a restart, and since the project file is a sqlite backup of index.db it
travels with the shoot to another Mac. Nothing here touches the shoot's own disk.

photos.flag = 'pick' is a mirror of this table, kept in step here and in db.set_marks, so everything that reads flags
(the Ratings filter, the counts, XMP sidecars, the CSV) needs to know nothing about the pile. A reject is the one flag
the pile never wipes: only picking the row again replaces it. This file imports nothing of ours, so db can import it.

A row whose file went missing stays in the table (the disk may come back) but is left out of every count,
and the export never sees it, because the export only takes ids the UI hands it from summary()."""
from __future__ import annotations
import json

# seq is the picking order: added_at alone cannot be, since a whole "select all" lands in one millisecond.
SCHEMA = """CREATE TABLE IF NOT EXISTS pile(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
  added_at TEXT DEFAULT (datetime('now')));"""

def _ensure(conn) -> None:
    conn.execute(SCHEMA)

MERGED_KEY = "picked_merged"   # meta: set once merge_once has made pile and flags one list, holds what it did
_CHUNK = 900                   # sqlite's bound-variable limit, with room

def _chunks(ids: list[int]):
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        yield chunk, ",".join("?" * len(chunk))

def add(conn, ids: list[int]) -> list[int]:
    """Picks ids, in the order given; an id already in the pile keeps its place, one the shoot never had is ignored.
    A reject on one of them gives way to the pick. Returns the ids that were rejected until now, so a caller can
    offer to put them back."""
    _ensure(conn)
    ids = list(dict.fromkeys(int(i) for i in ids))
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM pile").fetchone()[0]
    for pid in ids:
        seq += 1
        conn.execute("INSERT OR IGNORE INTO pile(photo_id, seq) SELECT id, ? FROM photos WHERE id=?", (seq, pid))
    was_rejected = []
    for chunk, q in _chunks(ids):
        was_rejected += [r[0] for r in conn.execute(f"SELECT id FROM photos WHERE flag='reject' AND id IN ({q})", chunk)]
        conn.execute(f"UPDATE photos SET flag='pick' WHERE id IN ({q})", chunk)
    conn.commit()
    return was_rejected

def remove(conn, ids: list[int]) -> None:
    """Takes ids off the list. Their pick goes with them; a reject stays."""
    _ensure(conn)
    ids = [int(i) for i in ids]
    conn.executemany("DELETE FROM pile WHERE photo_id=?", [(i,) for i in ids])
    for chunk, q in _chunks(ids):
        conn.execute(f"UPDATE photos SET flag=NULL WHERE flag='pick' AND id IN ({q})", chunk)
    conn.commit()

def clear(conn) -> list[dict]:
    """Empties the list, a missing file's row included. Returns every row as it was, for restore()."""
    _ensure(conn)
    rows = [{"id": r[0], "seq": r[1], "added_at": r[2], "flag": r[3]} for r in conn.execute(
        "SELECT pile.photo_id, pile.seq, pile.added_at, p.flag FROM pile JOIN photos p ON p.id = pile.photo_id ORDER BY pile.seq")]
    conn.execute("UPDATE photos SET flag=NULL WHERE flag='pick' AND id IN (SELECT photo_id FROM pile)")
    conn.execute("DELETE FROM pile")
    conn.commit()
    return rows

def restore(conn, rows: list[dict]) -> None:
    """Undoes clear(): the same rows in the same order with the flags they had, ahead of anything picked since. A row
    rejected since stays rejected and off the list, and one that has left the shoot is skipped."""
    _ensure(conn)
    top = max((int(r["seq"]) for r in rows), default=0)
    conn.execute("UPDATE pile SET seq = seq + ?", (top,))
    for r in sorted(rows, key=lambda r: int(r["seq"])):
        pid = int(r["id"])
        now = conn.execute("SELECT flag FROM photos WHERE id=?", (pid,)).fetchone()
        if now is None or now[0] == "reject":
            continue
        conn.execute("INSERT OR REPLACE INTO pile(photo_id, seq, added_at) VALUES(?, ?, ?)", (pid, int(r["seq"]), r.get("added_at")))
        if r.get("flag") == "pick":
            conn.execute("UPDATE photos SET flag='pick' WHERE id=?", (pid,))
    conn.commit()

def summary(conn) -> dict:
    """{ids, n, photos, videos, bytes} over the picked rows that are still in the shoot (status ok), in the
    order they were picked. bytes is the files themselves, not a RAW beside them: the export summary adds those."""
    _ensure(conn)
    rows = conn.execute("SELECT p.id, p.kind, p.size FROM pile JOIN photos p ON p.id = pile.photo_id "
                        "WHERE p.status='ok' ORDER BY pile.seq").fetchall()
    videos = sum(1 for r in rows if r[1] == "video")
    return {"ids": [r[0] for r in rows], "n": len(rows), "photos": len(rows) - videos, "videos": videos,
            "bytes": sum(int(r[2] or 0) for r in rows)}

def ids(conn) -> set[int]:
    """Every picked id still in the shoot, for the pile filter."""
    return set(summary(conn)["ids"])

# ===== one list: before the merge the pile and the pick flag were two separate things, and a shoot can hold both =====
def _reconcile(conn, only: list[int] | None = None) -> dict:
    """The union of the two, over status='ok' rows (or just those of only). A file that is missing stays as it is until
    it comes back, see settle. Makes no commit. A rejected row in the pile keeps its reject and leaves the list: flag
    has no timestamp, so which came last cannot be told, and a reject is the safer one to keep. Its id is returned so the
    pick is not lost without trace."""
    _ensure(conn)
    scope, args = "", []
    if only is not None:
        scope, args = f" AND p.id IN ({','.join('?' * len(only))})", list(only)
    in_pile = "FROM pile JOIN photos p ON p.id = pile.photo_id WHERE p.status='ok'" + scope
    rejected = [r[0] for r in conn.execute(f"SELECT p.id {in_pile} AND p.flag='reject' ORDER BY pile.seq", args)]
    conn.executemany("DELETE FROM pile WHERE photo_id=?", [(i,) for i in rejected])
    flagged = [r[0] for r in conn.execute(f"SELECT p.id {in_pile} AND p.flag IS NOT 'pick'", args)]
    conn.executemany("UPDATE photos SET flag='pick' WHERE id=?", [(i,) for i in flagged])
    joining = [r[0] for r in conn.execute("SELECT p.id FROM photos p WHERE p.status='ok' AND p.flag='pick' AND "
                                          f"p.id NOT IN (SELECT photo_id FROM pile){scope} ORDER BY p.id", args)]
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM pile").fetchone()[0]
    conn.executemany("INSERT INTO pile(photo_id, seq) VALUES(?, ?)", [(i, seq + k) for k, i in enumerate(joining, 1)])
    return {"joined": len(joining), "flagged": len(flagged), "rejected": rejected}

def merge_once(conn) -> None:
    """Makes the pile and the flags one list, once per shoot; meta MERGED_KEY says it is done. db.connect runs it, which
    covers an older index and an older project file alike. BEGIN IMMEDIATE because several threads open a shoot at the
    same moment: the second waits, sees the key and leaves. Busy past the timeout, the next connect tries again."""
    if conn.execute("SELECT 1 FROM meta WHERE key=?", (MERGED_KEY,)).fetchone():
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT 1 FROM meta WHERE key=?", (MERGED_KEY,)).fetchone():
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (MERGED_KEY, json.dumps(_reconcile(conn))))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

def settle(conn, ids: list[int]) -> None:
    """A missing file came back: its row catches up with the merge it sat out. A row that never disagreed is untouched.
    Makes no commit."""
    done = []
    for chunk, _ in _chunks([int(i) for i in ids]):
        done += _reconcile(conn, chunk)["rejected"]
    if done:
        r = conn.execute("SELECT value FROM meta WHERE key=?", (MERGED_KEY,)).fetchone()
        merged = json.loads(r[0]) if r else {}
        merged["rejected"] = merged.get("rejected", []) + done
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (MERGED_KEY, json.dumps(merged)))
# ===== end one list =====
