from __future__ import annotations
import csv, os, re, shutil, subprocess
from pathlib import Path
from . import db
from .config import export_root, CATEGORY_FALLBACK, SURE_MIN, STD_EXTS

WEB_QUALITY = 90

def web_copy(src: Path, dst: Path, web_size: int) -> bool:
    """dst = a copy of the photo at most web_size px on its long edge, orientation applied, the rest of the
    EXIF kept, in the photo's own format (JPEG at quality 90), with the source's mtime so a re-run knows it.
    False when the photo already fits (a copy of the original is better than a re-encode) or Pillow cannot
    read it (a RAW, a clip, a broken file): the caller copies the original."""
    if Path(src).suffix.lower() not in STD_EXTS:
        return False
    from PIL import Image, ImageOps
    try:
        import pillow_heif; pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(src) as im:
            fmt = im.format or "JPEG"
            if max(im.size) <= web_size:
                return False
            im = ImageOps.exif_transpose(im)
            im.thumbnail((web_size, web_size), Image.LANCZOS)
            exif = im.info.get("exif")
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            kw = {"quality": WEB_QUALITY} if fmt in ("JPEG", "WEBP") else {}
            if exif:
                kw["exif"] = exif
            im.save(dst, format=fmt, **kw)
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
        return True
    except Exception:
        try: os.unlink(dst)
        except OSError: pass
        return False

def safe_segment(name: str) -> str:
    """One folder-name segment: no separators, no leading/trailing dots or spaces, never '.' or '..'."""
    if name in (".", ".."):
        raise ValueError(f"illegal export name segment: {name!r}")
    return re.sub(r"[\\/:]+", "_", name).strip(" .") or "export"

def safe_name(name: str) -> str:
    """Sanitise a possibly nested export name ('people/Arya') segment by segment.
    Absolute paths and empty segments are rejected outright."""
    if name == "":
        name = "export"
    if Path(name).is_absolute() or name.startswith(("/", "\\")):
        raise ValueError(f"export name must be relative: {name!r}")
    parts = [p for p in name.split("/")]
    if any(p == "" for p in parts):
        raise ValueError(f"export name has an empty segment: {name!r}")
    return "/".join(safe_segment(p) for p in parts)

def export_dir(root: Path, name: str, base: Path | None = None) -> Path:
    """Resolve the export folder for a shoot: <base>/<shoot>/<name>, base defaulting to export_root().
    Refuses a name that escapes the base, a base that is the (read-only) shoot root, inside it or
    above it, and a name that would land inside the shoot root. Does not create anything."""
    root = Path(root); root_res = root.resolve()
    base = (Path(base) if base is not None else export_root()).resolve()
    if base == root_res or base.is_relative_to(root_res):
        raise ValueError("destination is inside the source folder")
    if root_res.is_relative_to(base):
        raise ValueError("destination contains the source folder; pick a folder that is not above it")
    out = base / root_res.name / safe_name(name)
    res = out.resolve()
    if not res.is_relative_to(base):
        raise ValueError(f"export path escapes the export folder: {name!r}")
    if res.is_relative_to(root_res):
        raise ValueError(f"export path would land inside the source folder: {name!r}")
    return out

def export_bytes(root: Path, ids: list[int]) -> int:
    conn = db.connect(Path(root)); total = 0
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        total += conn.execute(f"SELECT COALESCE(SUM(size), 0) FROM photos WHERE id IN ({q}) AND status='ok'", chunk).fetchone()[0]
    return int(total)

def _is_existing_copy(dst: Path, src: Path, web: bool = False) -> bool:
    """True when dst already represents src, i.e. an earlier export already put it there: a
    symlink that resolves to src, or a regular file whose size and mtime are within 2 seconds
    of src's own (copy2 preserves mtime, so a plain re-copy matches exactly). A web-size copy
    (web) has another size by design, so its mtime alone, which web_copy sets from the source,
    is the match. A source that has since vanished is never "already there": that stays a
    failure on re-run, same as a first run."""
    if not src.exists():
        return False
    try:
        if dst.is_symlink():
            return dst.resolve() == src.resolve()
        st_d = dst.stat(); st_s = src.stat()
        if web:
            return abs(st_d.st_mtime - st_s.st_mtime) <= 2
        return st_d.st_size == st_s.st_size and abs(st_d.st_mtime - st_s.st_mtime) <= 2
    except OSError:
        return False

def _resolve_destination(dst_dir: Path, name: str, pid: int, src: Path, web: bool = False):
    """Where one file lands: ("write", path) for a free name, ("skip", None) when the taken name
    is already this same file (a prior export, so re-running must not fail or duplicate), or
    ("fail", message) when 99 numbered fallbacks are all taken by something else."""
    dst = dst_dir / name
    if not (dst.exists() or dst.is_symlink()):
        return "write", dst
    if _is_existing_copy(dst, src, web):
        return "skip", None
    for i in range(1, 100):
        cand = dst_dir / (f"{pid}_{name}" if i == 1 else f"{pid}_{i}_{name}")
        if not (cand.exists() or cand.is_symlink()):
            return "write", cand
        if _is_existing_copy(cand, src, web):
            return "skip", None
    return "fail", f"no free name for {name} in {dst_dir} after 99 tries"

def transfer_files(root: Path, jobs: list[tuple[int, str, Path]], mode: str, failed_file: Path, progress=None,
                   web_size: int | None = None) -> list[str]:
    """The per-file loop every export shares. jobs are (photo id, rel, destination folder); each file
    lands in its folder under its own name, or {id}_{name} (then {id}_2_{name}, ...) when that name
    is already taken by something else. A name already taken by this same file (symlink target or
    copy with matching size/mtime) counts as done without writing, so re-exporting into a folder that
    already has the photos does not fail or duplicate. mode is "copy" or "symlink". A per-file OSError
    is counted, not raised, and the list is written to failed_file at the end. progress (if given)
    sees {done, total, failed, skipped} after every file. Destination folders must already exist.
    web_size (copy only): photos go out re-encoded to that long edge; RAW files and clips as they are."""
    root = Path(root); notify = progress or (lambda d: None)
    failed: list[str] = []; skipped = 0; total = len(jobs)
    for n, (pid, rel, dst_dir) in enumerate(jobs, 1):
        src = root / rel; name = Path(rel).name
        web = bool(web_size) and mode == "copy" and src.suffix.lower() in STD_EXTS
        action, val = _resolve_destination(dst_dir, name, pid, src, web)
        if action == "skip":
            skipped += 1
        elif action == "fail":
            failed.append(f"{rel}\t{val}")
        else:
            dst = val
            try:
                if not src.exists():               # os.symlink would happily point at nothing
                    raise FileNotFoundError(str(src))
                if web and web_copy(src, dst, int(web_size)): pass
                elif mode == "copy": shutil.copy2(src, dst)
                else: os.symlink(src.resolve(), dst)
            except OSError as e:
                failed.append(f"{rel}\t{e}")
        notify({"done": n, "total": total, "failed": len(failed), "skipped": skipped})
    if failed:
        failed_file.write_text("\n".join(failed) + "\n")
    return failed

def export_ids(root: Path, ids: list[int], name: str, mode: str = "copy", progress=None, base: Path | None = None,
               include_raw: bool = False, web_size: int | None = None) -> Path:
    """A flat selection under <base>/<shoot>/<name>: copies, links or a CSV. include_raw puts each RAW sibling
    next to its JPEG; web_size (copies only) re-encodes photos to that long edge, RAW and clips go as they are."""
    root = Path(root); out = export_dir(root, name, base)
    notify = progress or (lambda d: None)
    out.mkdir(parents=True, exist_ok=True)
    if mode == "csv":
        conn = db.connect(root); rows = []
        for i in range(0, len(ids), 900):        # chunk: SQLite caps bound variables
            chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
            rows += conn.execute(f"SELECT id, rel, sharp, n_faces, taken_at FROM photos WHERE id IN ({q}) AND status='ok' ORDER BY id", chunk).fetchall()
        with open(out / "photos.csv", "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["id", "path", "sharp", "n_faces", "taken_at"])
            for r in rows: w.writerow([r["id"], str(root / r["rel"]), r["sharp"], r["n_faces"], r["taken_at"]])
        notify({"done": len(rows), "total": len(rows), "failed": 0, "skipped": 0})
        return out
    transfer_files(root, [(pid, rel, out) for pid, rel, _ in ids_jobs(root, ids, include_raw)], mode, out / "failed.txt", progress,
                   web_size=web_size)
    return out

def category_rows(root: Path, categories: list[str] | None, include_unsure: bool = False, hide_bad: bool = False) -> list[dict]:
    """status='ok' rows (id, rel, sibling, size, category, kind, duration) placed in the given categories,
    category being the folder the row lands in. None means every category that has a photo; "unclassified"
    (category NULL) only when named explicitly. Placement follows search.category_match: a row filed under
    X, and a row filed under "other" whose best guess was X (that one, and a row under X with a score below
    SURE_MIN, is "less sure" and only included with include_unsure). A row can land in two folders.
    hide_bad leaves out rows the focus pass labelled bad (unchecked rows stay)."""
    from .search import category_match
    conn = db.connect(Path(root))
    bad = "AND focus IS NOT 'bad' " if hide_bad else ""
    if categories is None:
        categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM photos WHERE status='ok' AND category IS NOT NULL ORDER BY category")]
    names = [c for c in categories if c != "unclassified"]
    out: list[dict] = []
    if names:
        q = ",".join("?" * len(names))
        rows = conn.execute(f"SELECT id, rel, sibling, size, category, category_score, category_guess, category_guess_score, kind, duration FROM photos "
                            f"WHERE status='ok' {bad}AND (category IN ({q}) OR (category IN (?, 'people') AND category_guess IN ({q}))) ORDER BY id",
                            names + [CATEGORY_FALLBACK] + names).fetchall()
        rows = [dict(r) for r in rows]
        for cat in names:
            for r in rows:
                m = category_match(r, cat)
                if m is not None and (m[0] or include_unsure):
                    out.append(dict(id=r["id"], rel=r["rel"], sibling=r["sibling"], size=r["size"], category=cat,
                                    kind=r["kind"], duration=r["duration"]))
    if "unclassified" in categories:
        out += [dict(r, category="unclassified") for r in conn.execute(
            f"SELECT id, rel, sibling, size, kind, duration FROM photos WHERE status='ok' {bad}AND category IS NULL ORDER BY id")]
    out.sort(key=lambda r: (r["category"], r["id"]))
    return out

def cluster_rows(root: Path, names: list[str] | None, include_unsure: bool = False, hide_bad: bool = False) -> list[dict]:
    """status='ok' rows (id, rel, sibling, size, cluster, kind, duration) in the given discovered categories.
    None or [] means none: a discovered name is only exported when asked for by name. Rows under SURE_MIN
    only with include_unsure."""
    if not names:
        return []
    conn = db.connect(Path(root))
    q = ",".join("?" * len(names))
    bad = "AND focus IS NOT 'bad' " if hide_bad else ""
    rows = conn.execute(f"SELECT id, rel, sibling, size, cluster, cluster_score, kind, duration FROM photos WHERE status='ok' {bad}AND cluster IN ({q}) ORDER BY cluster, id", list(names)).fetchall()
    return [dict(id=r["id"], rel=r["rel"], sibling=r["sibling"], size=r["size"], cluster=r["cluster"], kind=r["kind"], duration=r["duration"])
            for r in rows if include_unsure or r["cluster_score"] is None or r["cluster_score"] >= SURE_MIN]

def aerial_rows(root: Path, hide_bad: bool = False) -> list[dict]:
    """status='ok' rows (id, rel, sibling, size, kind, duration) flagged aerial, whatever their category:
    the categories/drone/ folder, on top of (not instead of) each row's own category folder."""
    conn = db.connect(Path(root))
    bad = "AND focus IS NOT 'bad' " if hide_bad else ""
    rows = conn.execute(f"SELECT id, rel, sibling, size, kind, duration FROM photos WHERE status='ok' {bad}AND aerial=1 ORDER BY id").fetchall()
    return [dict(r) for r in rows]

# Video segments: each scene of a clip, cut with ffmpeg as a stream copy (no re-encode, so the cut
# lands on the nearest keyframe before the start). Always a written file, whatever the export mode.

def segment_jobs(root: Path, photo_ids: list[int], category: str | None) -> list[tuple[int, str, dict]]:
    """(photo id, rel, segment) for every segment of these ok videos, in id then idx order; with a
    category only the segments labelled that way ("unclassified" is a NULL category)."""
    conn = db.connect(Path(root)); out = []
    for i in range(0, len(photo_ids), 900):
        chunk = photo_ids[i:i + 900]; q = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id, rel FROM photos WHERE id IN ({q}) AND status='ok' AND kind='video' ORDER BY id", chunk).fetchall()
        for r in rows:
            for s in db.list_segments(conn, r["id"]):
                if category is None or (s["category"] == category) or (category == "unclassified" and s["category"] is None):
                    out.append((r["id"], r["rel"], s))
    return out

def segment_name(rel: str, seg: dict) -> str:
    return f"{Path(rel).stem}_{seg['idx']:02d}_{seg['start']:.1f}-{seg['end']:.1f}.mp4"

def segment_bytes(root: Path, photo_ids: list[int], category: str | None) -> int:
    """Rough size of the trimmed files: each clip's size scaled by the share of its duration exported."""
    conn = db.connect(Path(root)); total = 0.0
    meta = {}
    for pid, rel, seg in segment_jobs(root, photo_ids, category):
        if pid not in meta:
            meta[pid] = conn.execute("SELECT size, duration FROM photos WHERE id=?", (pid,)).fetchone()
        size, dur = meta[pid]["size"] or 0, meta[pid]["duration"] or 0
        total += size * ((seg["end"] - seg["start"]) / dur) if dur > 0 else size
    return int(total)

def _ffmpeg() -> str:
    from .video import _bin
    return _bin("ffmpeg")

def write_segment(src: Path, dst: Path, start: float, end: float) -> None:
    """One trimmed clip by stream copy. Raises OSError with ffmpeg's last stderr line on failure."""
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
           "-c", "copy", "-movflags", "+faststart", str(dst)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        dst.unlink(missing_ok=True)
        raise OSError("ffmpeg timed out")
    if out.returncode != 0 or not dst.is_file():
        dst.unlink(missing_ok=True)
        err = (out.stderr or b"").decode(errors="replace").strip().splitlines()
        raise OSError(err[-1] if err else f"ffmpeg exit {out.returncode}")

def transfer_segments(root: Path, jobs: list[tuple[int, str, dict, Path]], failed: list[str], progress=None,
                      done0: int = 0, total: int | None = None, failed0: int = 0, skipped0: int = 0) -> int:
    """The per-segment loop: (photo id, rel, segment, destination folder) each become one trimmed
    mp4, or count as skipped when that name is already there. Appends to `failed`, continues an
    outer progress counter from done0/total, returns the number skipped."""
    root = Path(root); notify = progress or (lambda d: None)
    total = len(jobs) if total is None else total; skipped = skipped0
    for n, (pid, rel, seg, dst_dir) in enumerate(jobs, done0 + 1):
        src = root / rel; dst = dst_dir / segment_name(rel, seg)
        if dst.exists() or dst.is_symlink():
            skipped += 1
        else:
            try:
                if not src.exists():
                    raise FileNotFoundError(str(src))
                write_segment(src, dst, seg["start"], seg["end"])
            except OSError as e:
                failed.append(f"{rel} [{seg['idx']}]\t{e}")
        notify({"done": n, "total": total, "failed": failed0 + len(failed), "skipped": skipped})
    return skipped

def export_segments(root: Path, photo_ids: list[int], category: str | None = None, mode: str = "copy",
                    base: Path | None = None, progress=None) -> Path:
    """<base>/<shoot>/segments/<clipstem>_<idx>_<start>-<end>.mp4 for every segment of these videos
    (only those labelled `category` when given). mode is accepted for symmetry with the other exports
    and ignored: a trimmed segment is a new file, a link makes no sense. Returns the segments folder."""
    root = Path(root); out = export_dir(root, "segments", base)
    jobs = [(pid, rel, seg, out) for pid, rel, seg in segment_jobs(root, photo_ids, category)]
    out.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    transfer_segments(root, jobs, failed, progress)
    if failed:
        (out / "failed.txt").write_text("\n".join(failed) + "\n")
    return out

def _row_bytes(root: Path, r: dict, include_raw: bool) -> int:
    """One row's bytes: its size from the DB plus its RAW sibling stat'ed on the disk when include_raw."""
    total = r["size"] or 0
    if include_raw and r["sibling"]:
        try: total += os.stat(root / r["sibling"]).st_size
        except OSError: pass
    return total

# Planners: which file lands in which subfolder. Each job is (photo id, rel, sub) with sub a relative
# subfolder ("beach", "discovered/excavator", "drone"; "Meera"; "" for a flat selection). The local
# exports turn sub into <out>/<sub> and hand the jobs to transfer_files; the Drive upload prefixes
# "categories/", "people/" or "selection" and hands the same jobs to drive.upload_files. One
# selection rule, two destinations.

def _place(jobs: list, r, sub: str, include_raw: bool) -> None:
    jobs.append((r["id"], r["rel"], sub))
    if include_raw and r["sibling"]:
        jobs.append((r["id"], r["sibling"], sub))

def category_jobs(root: Path, categories: list[str] | None, include_raw: bool = False, discovered: list[str] | None = None,
                  include_unsure: bool = False, videos: str = "clips", drone: bool = False, hide_bad: bool = False):
    """(jobs, seg_jobs) for a categories export: jobs are (id, rel, sub) with sub the category folder,
    "discovered/<name>" or "drone", the RAW sibling right after its JPEG when include_raw; seg_jobs are
    (id, rel, segment, sub) for the videos that go out trimmed (videos="segments"). Same placement as
    categories_bytes counts."""
    if videos not in ("clips", "segments"):
        raise ValueError("videos must be clips or segments")
    root = Path(root)
    jobs: list[tuple[int, str, str]] = []
    seg_jobs: list[tuple[int, str, dict, str]] = []
    for r in category_rows(root, categories, include_unsure, hide_bad):
        sub = safe_segment(r["category"])
        if r["kind"] == "video" and videos == "segments":
            seg_jobs += [(pid, rel, seg, sub) for pid, rel, seg in segment_jobs(root, [r["id"]], r["category"])]
            continue
        _place(jobs, r, sub, include_raw)
    for r in cluster_rows(root, discovered, include_unsure, hide_bad):
        _place(jobs, r, "discovered/" + safe_segment(r["cluster"]), include_raw)
    if drone:
        for r in aerial_rows(root, hide_bad):
            _place(jobs, r, "drone", include_raw)
    return jobs, seg_jobs

def folder_jobs(root: Path, folders: dict[str, list[int]], include_raw: bool = False) -> list[tuple[int, str, str]]:
    """(id, rel, sub) for every ok photo id under each folder, sub the folder key made safe, the RAW
    sibling right after its JPEG when include_raw. A photo listed under two folders is two jobs."""
    root = Path(root); jobs: list[tuple[int, str, str]] = []
    for folder, ids in folders.items():
        sub = safe_segment(folder)
        for r in rows_for_ids(root, ids):
            _place(jobs, r, sub, include_raw)
    return jobs

def ids_jobs(root: Path, ids: list[int], include_raw: bool = False) -> list[tuple[int, str, str]]:
    """(id, rel, "") for these ok ids in id order (a flat selection), RAW siblings along when include_raw."""
    jobs: list[tuple[int, str, str]] = []
    for r in rows_for_ids(Path(root), ids):
        _place(jobs, r, "", include_raw)
    return jobs

def jobs_bytes(root: Path, jobs: list[tuple[int, str, str]]) -> int:
    """Bytes these jobs read from the disk, each placement counted (a photo in two folders is two
    uploads or copies); a file that fails to stat is skipped, the run reports it as failed."""
    root = Path(root); total = 0
    for _, rel, _ in jobs:
        try: total += os.stat(root / rel).st_size
        except OSError: pass
    return int(total)

def categories_bytes(root: Path, categories: list[str] | None, include_raw: bool = False,
                     discovered: list[str] | None = None, include_unsure: bool = False, videos: str = "clips",
                     drone: bool = False, hide_bad: bool = False) -> int:
    """Bytes a copy of these categories (fixed, plus the named discovered ones, plus the drone folder when
    asked) needs: JPEG sizes from the DB, RAW siblings stat'ed on the disk (a sibling that fails to stat is
    skipped, the export will report it as failed). A photo in a fixed and a discovered category (or in the
    drone folder too) is two copies, so it counts twice. In segments mode a video in a fixed category
    counts its matching segments' share of its size instead of the whole clip; a video in a discovered
    category or the drone folder always counts whole (segments carry no cluster and no flag)."""
    root = Path(root); total = 0
    for r in category_rows(root, categories, include_unsure, hide_bad):
        if r["kind"] == "video" and videos == "segments":
            total += segment_bytes(root, [r["id"]], r["category"])
            continue
        total += _row_bytes(root, r, include_raw)
    for r in cluster_rows(root, discovered, include_unsure, hide_bad):
        total += _row_bytes(root, r, include_raw)
    if drone:
        for r in aerial_rows(root, hide_bad):
            total += _row_bytes(root, r, include_raw)
    return int(total)

def export_categories(root: Path, categories: list[str] | None, mode: str = "copy", include_raw: bool = False,
                      base: Path | None = None, progress=None, discovered: list[str] | None = None,
                      include_unsure: bool = False, videos: str = "clips", drone: bool = False, hide_bad: bool = False) -> Path:
    """<base>/<shoot>/categories/<category>/<file> for every ok photo in the chosen fixed categories and
    <base>/<shoot>/categories/discovered/<name>/<file> for the named discovered ones, each RAW sibling next
    to its JPEG when include_raw. Only what the model is sure of unless include_unsure. Videos go along as
    whole clips, or with videos="segments" as their trimmed segments labelled that fixed category (always
    written, whatever mode); a video in a discovered category always goes whole, segments carry no cluster.
    With drone, every aerial row (any kind, any category, always whole) also lands in categories/drone/.
    hide_bad leaves out every row the focus pass labelled bad. One progress counter over files then
    segments, one failed.txt. Returns the categories folder."""
    if mode == "csv":
        raise ValueError("csv is not supported for a category export")
    if videos not in ("clips", "segments"):
        raise ValueError("videos must be clips or segments")
    root = Path(root); out = export_dir(root, "categories", base)
    planned, planned_segs = category_jobs(root, categories, include_raw, discovered, include_unsure, videos, drone, hide_bad)
    jobs: list[tuple[int, str, Path]] = [(pid, rel, out / sub) for pid, rel, sub in planned]
    seg_jobs: list[tuple[int, str, dict, Path]] = [(pid, rel, seg, out / sub) for pid, rel, seg, sub in planned_segs]
    out.mkdir(parents=True, exist_ok=True)
    for d in {j[2] for j in jobs} | {j[3] for j in seg_jobs}:
        d.mkdir(parents=True, exist_ok=True)
    total = len(jobs) + len(seg_jobs)
    notify = progress or (lambda d: None)
    last = {"failed": 0, "skipped": 0}
    def prog(d):
        last.update(failed=d["failed"], skipped=d["skipped"])
        notify(dict(d, total=total))
    failed = transfer_files(root, jobs, mode, out / "failed.txt", prog)
    if seg_jobs:
        more: list[str] = []
        transfer_segments(root, seg_jobs, more, notify, done0=len(jobs), total=total, failed0=len(failed), skipped0=last["skipped"])
        if more:
            with open(out / "failed.txt", "a") as fh:
                fh.write("\n".join(more) + "\n")
    elif total == 0:
        notify({"done": 0, "total": 0, "failed": 0, "skipped": 0})
    return out

def rows_for_ids(root: Path, ids: list[int]) -> list:
    """status='ok' rows (id, rel, sibling, size) for these ids, in id order. Chunked: SQLite caps bound variables."""
    conn = db.connect(Path(root)); rows = []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        rows += conn.execute(f"SELECT id, rel, sibling, size FROM photos WHERE id IN ({q}) AND status='ok' ORDER BY id", chunk).fetchall()
    return rows

def folders_bytes(root: Path, folders: dict[str, list[int]], include_raw: bool = False) -> int:
    """Bytes a copy of these folders needs. A photo listed under two folders is two copies, so it
    counts twice. JPEG sizes from the DB, RAW siblings stat'ed on the disk (a failing stat is skipped)."""
    root = Path(root); total = 0
    for ids in folders.values():
        for r in rows_for_ids(root, ids):
            total += r["size"] or 0
            if include_raw and r["sibling"]:
                try: total += os.stat(root / r["sibling"]).st_size
                except OSError: pass
    return int(total)

def export_folders(root: Path, group: str, folders: dict[str, list[int]], mode: str = "copy", include_raw: bool = False,
                   base: Path | None = None, progress=None) -> Path:
    """<base>/<shoot>/<group>/<folder>/<file> for every ok photo id in each folder, its RAW sibling next
    to it when include_raw. Folder keys must already be safe segments. One progress stream and one
    failed.txt at <base>/<shoot>/<group>/failed.txt. Returns the group folder."""
    if mode == "csv":
        raise ValueError(f"csv is not supported for a {group} export")
    root = Path(root); out = export_dir(root, group, base)
    jobs: list[tuple[int, str, Path]] = [(pid, rel, out / sub) for pid, rel, sub in folder_jobs(root, folders, include_raw)]
    out.mkdir(parents=True, exist_ok=True)
    for d in {j[2] for j in jobs}:
        d.mkdir(parents=True, exist_ok=True)
    transfer_files(root, jobs, mode, out / "failed.txt", progress)
    return out
