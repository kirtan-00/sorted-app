"""The pile: the photos and clips an editor has picked on one shoot, in the order they were picked.

The grid's selection is the pile. It lives in the shoot's index.db (table pile), not in the browser, so it
survives a new search, a reload and a restart, and since the project file is a sqlite backup of index.db it
travels with the shoot to another Mac. Nothing here touches the shoot's own disk.

A row whose file went missing stays in the table (the disk may come back) but is left out of every count,
and the export never sees it, because the export only takes ids the UI hands it from summary()."""
from __future__ import annotations

# seq is the picking order: added_at alone cannot be, since a whole "select all" lands in one millisecond.
SCHEMA = """CREATE TABLE IF NOT EXISTS pile(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
  added_at TEXT DEFAULT (datetime('now')));"""

def _ensure(conn) -> None:
    conn.execute(SCHEMA)

def add(conn, ids: list[int]) -> None:
    """Picks ids, in the order given; an id already in the pile keeps its place, one the shoot never had is ignored."""
    _ensure(conn)
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM pile").fetchone()[0]
    for pid in dict.fromkeys(int(i) for i in ids):
        seq += 1
        conn.execute("INSERT OR IGNORE INTO pile(photo_id, seq) SELECT id, ? FROM photos WHERE id=?", (seq, pid))
    conn.commit()

def remove(conn, ids: list[int]) -> None:
    _ensure(conn)
    conn.executemany("DELETE FROM pile WHERE photo_id=?", [(int(i),) for i in ids])
    conn.commit()

def clear(conn) -> None:
    _ensure(conn)
    conn.execute("DELETE FROM pile")
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
