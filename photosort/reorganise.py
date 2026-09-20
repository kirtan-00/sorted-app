"""Reorganise disk: the one deliberate, guarded exception to the read-only shoot root.

Every other module only reads the shoot. This one renames files into <root>/sorted/ (same filesystem,
so each move is one atomic rename, no bytes copied) after every guard below passes, writes an exact
UNDO.json first, and can put everything back. It never deletes a file, never overwrites one (a rename
onto an existing path is refused per file) and never touches a file that is not in the index, apart
from the RAW sibling and the sidecars that belong to an indexed file.

Layout:
  sorted/photos/<category>/<file>          category, or the discovered cluster, or other/
  sorted/photos/people/<Name>/<file>       by_people only: a named person beats the category
  sorted/videos/<category>/<file>          videos are never people-wise (no faces)
  sorted/drone/photos|videos/<category>/   aerial=1 beats everything above
"""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from . import db
from .classify import is_unnamed_group
from .config import CATEGORY_FALLBACK, FACE_MATCH_MIN_SIM
from .export import safe_segment

SORTED_DIR = "sorted"
UNDO_NAME = "UNDO.json"
UNDO_FORMAT = "photosort-undo/1"
PROBE_NAME = ".photosort-write-test"
# Folder names that mean "this is a camera card, not a copy on a disk". Sorting a card in place would
# break the camera's own bookkeeping (and the card is usually the only copy).
CARD_DIRS = {"DCIM", "M4ROOT", "PRIVATE", "AVCHD", "CLIP", "XDROOT", "MISC"}
# Sidecar extensions that travel with their file (matched case-insensitively, same directory, same stem):
# XMP edits, Sony and generic XML metadata, DJI .SRT telemetry, GoPro .THM posters and .LRV proxies.
SIDECAR_EXTS = (".xml", ".srt", ".xmp", ".thm", ".lrv")
SONY_XML_TAIL = "m01.xml"          # C0001.MP4 -> C0001M01.XML
COMMIT_EVERY = 200
SAMPLE = 20
MTIME_SLACK = 2.0

# The plans this process has made, keyed by plan id: {"root": str, "by_people": bool, "moves": [...]}.
# A new plan retires every older one; apply clears the one it ran.
_PLANS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _undo_path(root: Path) -> Path:
    return Path(root) / SORTED_DIR / UNDO_NAME


def _read_manifest(root: Path) -> dict | None:
    p = _undo_path(root)
    if not p.is_file():
        return None
    try:
        man = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{p} is not readable: {e}")
    if not isinstance(man, dict) or man.get("format") != UNDO_FORMAT or not isinstance(man.get("moves"), list):
        raise ValueError(f"{p} is not a photosort undo manifest")
    return man


def _write_manifest(root: Path, moves: list[dict], created: str) -> None:
    """Our own manifest is the one file this module rewrites: to a temp name first, then os.replace,
    so a crash mid-write never leaves a half manifest."""
    p = _undo_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"format": UNDO_FORMAT, "root": str(root), "created": created,
                       "moves": [{"src_rel": m["src_rel"], "dst_rel": m["dst_rel"], "photo_id": m["photo_id"], "kind": m["kind"]}
                                 for m in moves]}, indent=1)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, p)


def status(root: Path) -> dict:
    """{"reorganised": bool, "moves": n, "created": iso} from the manifest, if there is one."""
    try:
        man = _read_manifest(Path(root))
    except ValueError:
        man = None
    if man is None:
        return {"reorganised": False, "moves": 0, "created": None}
    return {"reorganised": True, "moves": len(man["moves"]), "created": man.get("created")}


# Guards. Each raises ValueError with the message the UI shows; plan and apply both run them all.

def _guard_writable(root: Path) -> None:
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    probe = root / PROBE_NAME
    if not os.access(root, os.W_OK):
        raise ValueError(f"{root} is not writable (read-only disk?)")
    try:
        with open(probe, "w") as fh:
            fh.write("probe")
    except OSError as e:
        raise ValueError(f"{root} is not writable (read-only disk?): {e}")
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def _guard_no_card(root: Path, rels: list[str]) -> None:
    msg = "this looks like a camera card, copy it to a disk first"
    for rel in rels:
        if any(part.upper() in CARD_DIRS for part in Path(rel).parts[:-1]):
            raise ValueError(msg)
    try:
        top = [d for d in os.scandir(root) if d.is_dir(follow_symlinks=False) and not d.name.startswith(".")]
    except OSError:
        return
    for d in top:
        if d.name.upper() in CARD_DIRS:
            raise ValueError(msg)
        try:
            sub = [s for s in os.scandir(d.path) if s.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        if any(s.name.upper() in CARD_DIRS for s in sub):
            raise ValueError(msg)


def _guard_fresh_and_one_disk(root: Path, rows: list, extra: list[str]) -> None:
    """Guard 3 (every ok row still matches size and mtime, every sibling is there) and guard 6 (every
    file sits on the filesystem the root is on) in one pass of stats."""
    root_dev = os.stat(root).st_dev
    stale = 0; other_disk = False
    for r in rows:
        try:
            st = os.stat(root / r["rel"])
        except OSError:
            stale += 1; continue
        if st.st_size != (r["size"] or 0) or abs(st.st_mtime - (r["mtime"] or 0.0)) > MTIME_SLACK:
            stale += 1
        elif st.st_dev != root_dev:
            other_disk = True
        if r["sibling"]:
            try:
                if os.stat(root / r["sibling"]).st_dev != root_dev:
                    other_disk = True
            except OSError:
                stale += 1
    for rel in extra:
        try:
            if os.stat(root / rel).st_dev != root_dev:
                other_disk = True
        except OSError:
            pass
    if stale:
        raise ValueError(f"{stale} file{'s' if stale != 1 else ''} changed since the last index, re-run Index first")
    if other_disk:
        raise ValueError("this folder spans more than one disk; reorganise works on one disk at a time")


def _guard_not_reorganised(root: Path, rels: list[str]) -> None:
    if _undo_path(root).exists():
        raise ValueError("undo the previous reorganise first")
    prefix = SORTED_DIR + "/"
    if any(rel.startswith(prefix) for rel in rels):
        raise ValueError("already reorganised")


# Placement

def _category(r) -> str:
    cat = r["category"]
    if cat and cat != CATEGORY_FALLBACK:
        return cat
    cl = r["cluster"]
    if cl and not is_unnamed_group(cl):
        return cl
    return CATEGORY_FALLBACK


def _names_by_photo(root: Path, conn) -> dict[int, list[str]]:
    """photo id -> sorted distinct names: people.name over faces, plus reference names via match_references."""
    from .people import match_references
    out: dict[int, set[str]] = {}
    for pid, name in conn.execute("SELECT DISTINCT f.photo_id, pe.name FROM faces f JOIN people pe ON pe.id=f.person_id "
                                  "JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' AND pe.name IS NOT NULL AND pe.name != ''"):
        out.setdefault(int(pid), set()).add(name.strip())
    for name, matches in match_references(root, FACE_MATCH_MIN_SIM).items():
        for m in matches:
            out.setdefault(int(m["photo_id"]), set()).add(name.strip())
    return {pid: sorted(n for n in names if n) for pid, names in out.items()}


def _sidecars(root: Path, rel: str, listing: dict[str, list[str]]) -> list[str]:
    """Sidecar rels for the file at rel: same directory, same stem (case-insensitive), one of SIDECAR_EXTS
    or the Sony <stem>M01.XML. listing caches each directory's entries."""
    p = Path(rel); d = str(p.parent)
    if d not in listing:
        try:
            listing[d] = sorted(e.name for e in os.scandir(root / p.parent) if e.is_file(follow_symlinks=False))
        except OSError:
            listing[d] = []
    stem = p.stem.lower(); out = []
    for name in listing[d]:
        low = name.lower()
        if not low.startswith(stem) or low == p.name.lower():
            continue
        tail = low[len(stem):]
        if tail in SIDECAR_EXTS or tail == SONY_XML_TAIL:
            out.append(str(p.parent / name) if d != "." else name)
    return out


def _dest_folder(r, by_people: bool, names: list[str]) -> tuple[str, str, str]:
    """(folder rel under sorted/, reason, bucket) for one indexed row."""
    cat = safe_segment(_category(r))
    video = (r["kind"] or "photo") == "video"
    if r["aerial"]:
        return f"{SORTED_DIR}/drone/{'videos' if video else 'photos'}/{cat}", "drone", "drone"
    if video:
        return f"{SORTED_DIR}/videos/{cat}", f"category:{_category(r)}", "videos"
    if by_people and names:
        return f"{SORTED_DIR}/photos/people/{safe_segment(names[0])}", f"person:{names[0]}", "people"
    return f"{SORTED_DIR}/photos/{cat}", f"category:{_category(r)}", "photos"


def _existing_names(root: Path, folder: str, cache: dict[str, set[str]]) -> set[str]:
    if folder not in cache:
        try:
            cache[folder] = {e.name.lower() for e in os.scandir(root / folder)}
        except OSError:
            cache[folder] = set()
    return cache[folder]


def plan(root: Path, by_people: bool = False) -> dict:
    """Run every guard, compute every move, cache the full list under a fresh plan id and return the summary."""
    root = Path(root)
    conn = db.connect(root)
    rows = conn.execute("SELECT id, rel, size, mtime, sibling, kind, category, cluster, aerial FROM photos WHERE status='ok' ORDER BY rel").fetchall()
    rels = [r["rel"] for r in rows] + [r["sibling"] for r in rows if r["sibling"]]
    guards: dict[str, bool] = {}
    _guard_writable(root); guards["writable"] = True
    _guard_no_card(root, rels); guards["no_card"] = True
    _guard_not_reorganised(root, rels); guards["not_reorganised"] = True
    names = _names_by_photo(root, conn) if by_people else {}
    listing: dict[str, list[str]] = {}
    claimed: set[str] = set(rels)          # every rel that already has a home in the plan
    groups: list[dict] = []
    counts = {"photos": 0, "videos": 0, "drone": 0}; people: dict[str, int] = {}
    for r in rows:
        folder, reason, bucket = _dest_folder(r, by_people, names.get(r["id"], []))
        files = [(r["rel"], "video" if (r["kind"] or "photo") == "video" else "photo")]
        if r["sibling"]:
            files.append((r["sibling"], "raw"))
        for rel, _ in list(files):
            for sc in _sidecars(root, rel, listing):
                if sc not in claimed:
                    claimed.add(sc); files.append((sc, "sidecar"))
        also = names.get(r["id"], [])[1:] if bucket == "people" else []
        groups.append({"id": r["id"], "folder": folder, "reason": reason, "files": files, "also": also,
                       "stem": Path(r["rel"]).stem})
        if bucket == "people":
            counts["photos"] += 1; people[names[r["id"]][0]] = people.get(names[r["id"]][0], 0) + 1
        else:
            counts[bucket] += 1
    extra = [rel for g in groups for rel, kind in g["files"] if kind == "sidecar"]
    _guard_fresh_and_one_disk(root, rows, extra); guards["fresh"] = True; guards["one_disk"] = True
    # Destination names: case-insensitive per folder (macOS), seeded with whatever is already on disk there.
    # A group whose name is taken gets <stem>_<photo id> on every one of its files.
    taken: dict[str, set[str]] = {}
    moves: list[dict] = []; collisions = 0
    for g in groups:
        have = _existing_names(root, g["folder"], taken)
        stem = g["stem"]; low_stem = stem.lower()
        def name_for(rel: str, suffix: str) -> str:
            """The file's own name, or with the suffix slipped in after the group's stem
            (a.jpg -> a_12.jpg, C0001M01.XML -> C0001_12M01.XML, case kept)."""
            name = Path(rel).name
            if not suffix:
                return name
            if name.lower().startswith(low_stem):
                return name[:len(stem)] + suffix + name[len(stem):]
            return f"{Path(rel).stem}{suffix}{Path(rel).suffix}"
        suffix = ""
        if any(name_for(rel, "").lower() in have for rel, _ in g["files"]):
            suffix = f"_{g['id']}"; collisions += 1
        for rel, kind in g["files"]:
            dst_name = name_for(rel, suffix)
            have.add(dst_name.lower())
            mv = {"photo_id": g["id"], "src_rel": rel, "dst_rel": f"{g['folder']}/{dst_name}", "kind": kind, "reason": g["reason"]}
            if g["also"] and kind in ("photo", "video"):
                mv["also"] = list(g["also"])
            moves.append(mv)
    plan_id = str(uuid.uuid4())
    _PLANS.clear()
    _PLANS[plan_id] = {"root": str(root), "by_people": by_people, "moves": moves}
    return {"plan_id": plan_id, "by_people": by_people, "moves": len(moves), "photos": counts["photos"], "videos": counts["videos"],
            "drone": counts["drone"], "people": people, "folders": len({g["folder"] for g in groups}),
            "collisions": collisions, "sample": [dict(m) for m in moves[:SAMPLE]], "guards": guards}


def _apply_db(conn, m: dict, rel: str) -> None:
    if m["kind"] in ("photo", "video"):
        conn.execute("UPDATE photos SET rel=? WHERE id=?", (rel, m["photo_id"]))
    elif m["kind"] == "raw":
        conn.execute("UPDATE photos SET sibling=? WHERE id=?", (rel, m["photo_id"]))


def apply(root: Path, plan_id: str, progress=None) -> dict:
    """Run the cached plan: UNDO.json first, then one os.rename per file in plan order, the db row updated
    right after each rename, commits every COMMIT_EVERY moves. A failed rename is recorded and skipped, and
    the manifest only ever lists moves that happened. Returns {done, total, failed, failures, path}."""
    root = Path(root); notify = progress or (lambda d: None)
    cached = _PLANS.get(plan_id)
    if cached is None or cached["root"] != str(root):
        raise ValueError("run the plan first")
    conn = db.connect(root)
    rows = conn.execute("SELECT id, rel, size, mtime, sibling FROM photos WHERE status='ok'").fetchall()
    rels = [r["rel"] for r in rows] + [r["sibling"] for r in rows if r["sibling"]]
    _guard_writable(root)
    _guard_no_card(root, rels)
    _guard_not_reorganised(root, rels)
    moves = cached["moves"]
    _guard_fresh_and_one_disk(root, rows, [m["src_rel"] for m in moves if m["kind"] == "sidecar"])
    _PLANS.pop(plan_id, None)
    created = _now()
    _write_manifest(root, moves, created)          # proves sorted/ is writable before the first rename
    done_moves: list[dict] = []; failures: list[str] = []
    total = len(moves)
    try:
        for n, m in enumerate(moves, 1):
            src = root / m["src_rel"]; dst = root / m["dst_rel"]
            try:
                if not src.exists():
                    raise FileNotFoundError(f"{m['src_rel']} is gone")
                if dst.exists() or dst.is_symlink():
                    raise FileExistsError(f"{m['dst_rel']} exists")
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
            except OSError as e:
                failures.append(f"{m['src_rel']}\t{e}")
            else:
                done_moves.append(m)          # the file has moved: undo must know even if the db update fails
                _apply_db(conn, m, m["dst_rel"])
            if n % COMMIT_EVERY == 0:
                conn.commit(); _write_manifest(root, done_moves, created)
            notify({"done": n, "total": total, "failed": len(failures)})
    finally:
        conn.commit()
        _write_manifest(root, done_moves, created)
    db.set_meta(conn, "reorganised_at", created)
    if total == 0:
        notify({"done": 0, "total": 0, "failed": 0})
    return {"done": total, "total": total, "failed": len(failures), "failures": failures, "path": str(root / SORTED_DIR)}


def _prune_empty(root: Path, keep: set[Path]) -> None:
    """Remove empty folders under sorted/, deepest first, except those in keep. Only rmdir: a folder with
    anything in it (a stray .DS_Store included) stays."""
    base = root / SORTED_DIR
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        d = Path(dirpath)
        if d == base or d in keep:
            continue
        try:
            os.rmdir(d)
        except OSError:
            pass


def undo(root: Path, progress=None) -> dict:
    """Put every file in UNDO.json back (reverse order), restore rel and sibling, remove the now-empty folders
    under sorted/ and the manifest last. A file that is not where the manifest says (moved by hand) is a
    per-file failure; the manifest is rewritten with just those, so a retry is exact. Returns {restored, failed}."""
    root = Path(root); notify = progress or (lambda d: None)
    man = _read_manifest(root)
    if man is None:
        raise ValueError("nothing to undo")
    moves = man["moves"]; total = len(moves)
    conn = db.connect(root)
    restored: list[dict] = []; pending: list[dict] = []
    try:
        for n, m in enumerate(reversed(moves), 1):
            cur = root / m["dst_rel"]; back = root / m["src_rel"]
            try:
                if not cur.exists() and back.exists():
                    pass                                  # already back where it belongs (by hand, or a move the manifest listed that never happened)
                elif not cur.exists():
                    raise FileNotFoundError(f"{m['dst_rel']} is gone")
                elif back.exists() or back.is_symlink():
                    raise FileExistsError(f"{m['src_rel']} exists")
                else:
                    back.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(cur, back)
            except OSError:
                pending.append(m)
            else:
                restored.append(m)
                _apply_db(conn, m, m["src_rel"])
            if n % COMMIT_EVERY == 0:
                conn.commit()
            notify({"done": n, "total": total, "failed": len(pending)})
    finally:
        conn.commit()
    pending.reverse()                                     # back to apply order
    keep = {(root / m["dst_rel"]).parent for m in pending}
    keep = {p for k in keep for p in [k, *k.parents] if p != root}
    _prune_empty(root, keep)
    if pending:
        _write_manifest(root, pending, man.get("created") or _now())
    else:
        os.unlink(_undo_path(root))
        conn.execute("DELETE FROM meta WHERE key='reorganised_at'"); conn.commit()
        try:
            os.rmdir(root / SORTED_DIR)
        except OSError:
            pass
    if total == 0:
        notify({"done": 0, "total": 0, "failed": 0})
    return {"restored": len(restored), "failed": len(pending)}
