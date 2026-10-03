"""The project file: everything the app knows about a shoot (index.db with its saved people, searches
and exports, thumbs, grid thumbs) in one sorted_<shoot>.sorted, a zip inside, so a ready index can be
handed to another Mac and opened there without re-indexing, the way an editor hands over a Premiere
project. Older files are named <shoot>.photosort-index.zip and load the same; the format inside has not
changed. Format "photosort-index/1": bundle.json, index.db, thumbs/*.jpg,
grid/*.jpg, frames/*.jpg (sampled video frames). bundle.json also carries "scan", how far the scan had got
(db.scan_counts plus faces), so a bundle of a half-scanned shoot says so before it is loaded; the key is
optional, an older bundle without it loads the same.

The shoot root is only ever read, with one exception: the project file itself may sit directly in the
scanned folder, as <root>/sorted_<shoot>.sorted, so the scan travels on the SSD with the footage and whoever
plugs the disk in next can open it. That one file (and its .part while it is being written) is the only
thing the app ever creates under a root; a subfolder of the root, a photo export or anything else is still
refused. The scanner never lists it (walk.find_images only takes photo and video extensions)."""
from __future__ import annotations
import json, os, shutil, sqlite3, tempfile, time, zipfile
from pathlib import Path
from . import db
from .config import DB_NAME, app_home, shoot_slug

FORMAT = "photosort-index/1"
SUFFIX = ".sorted"
LEGACY_SUFFIX = ".photosort-index.zip"
PREFIX = "sorted_"
NOTE = "A sorted project file (a zip inside). Open it with sorted, or unzip into ~/Library/Application Support/photosort/."


def project_name(root: Path) -> str:
    """sorted_<shoot folder name>.sorted; a volume root, which has no name, becomes sorted_root.sorted."""
    return f"{PREFIX}{Path(root).resolve().name or 'root'}{SUFFIX}"


def bundle_path(root: Path, out_dir: Path) -> Path:
    """<out_dir>/sorted_<shoot name>.sorted. out_dir may be the shoot root itself (the file sits beside the
    footage, the one write the root ever gets); a folder inside the root is refused, because nothing else
    is ever created under a shoot, not even a folder to hold the project file. Does not create anything."""
    root_res = Path(root).resolve(); out_res = Path(out_dir).resolve()
    if out_res != root_res and out_res.is_relative_to(root_res):
        raise ValueError("the project file cannot go inside the source folder (the shoot); it goes in the shoot folder itself or somewhere else")
    return Path(out_dir) / project_name(root_res)


def default_project_dir(root: Path, home: Path | None = None) -> Path:
    """Where the project file goes when nobody has chosen: the scanned folder itself, so
    /Volumes/Prod_02/NSG_26 gets /Volumes/Prod_02/NSG_26/sorted_NSG_26.sorted and the file travels with the
    disk. ~/Documents/sorted only when the folder is not there or says it cannot be written. os.access is a
    hint here, not the gate (it says yes on a read-only exFAT or a locked SD card); the scan's preflight does
    a real write before it starts and asks for a place when that fails. Nothing is created."""
    root_res = Path(root).resolve()
    if root_res.is_dir() and os.access(root_res, os.W_OK):
        return root_res
    return (home or Path.home()) / "Documents" / "sorted"


# ===== how big the project file will get, before a single photo is read. Measured 2026-09 on a mixed shoot:
# 955 items (photos and clips) packed to 211 MB, about 220 KB an item; a clip carries six sampled frames and
# its scene frames on top of the thumb and grid, so it is priced separately and higher. Both are rounded up
# and the sum takes a 1.5 margin, because the preflight's job is to never let a 6-hour scan end on a full
# disk: asking for a bit more than it needs is the cheap mistake. =====
PROJECT_BYTES_PER_PHOTO = 300 * 1024
PROJECT_BYTES_PER_VIDEO = 1024 * 1024
PROJECT_MARGIN = 1.5


def estimate_bytes(n_photos: int, n_videos: int) -> int:
    """The project file for a shoot of this many photos and clips, rounded up with the margin. 955 photos
    alone come to 440 MB, about twice the 211 MB measured, which is the point."""
    return int((n_photos * PROJECT_BYTES_PER_PHOTO + n_videos * PROJECT_BYTES_PER_VIDEO) * PROJECT_MARGIN)


# What the disk must still have free after the project file is written. A flat 1 GiB (the photo export's
# floor) made a 24-photo card "need 1.1 GB" for an 11 MB file, which is a refusal nobody can act on. So it
# scales: a tenth of the file, never under 100 MB (a filesystem wants some slack whatever the size), never
# over 1 GiB. A big shoot stays conservative: 20k photos estimate 9.2 GB (already about twice the measured
# size) and keep 920 MB on top; from about 23k photos up it is the old 1 GiB exactly.
PROJECT_HEADROOM_MIN = 100 * 1000 * 1000
PROJECT_HEADROOM_MAX = 1 << 30


def project_headroom(estimate: int) -> int:
    """Bytes to keep free beside a project file of `estimate` bytes: max(100 MB, 10%), capped at 1 GiB."""
    return min(max(PROJECT_HEADROOM_MIN, int(estimate) // 10), PROJECT_HEADROOM_MAX)


def probe_writable(d: Path) -> str | None:
    """None when a file can really be created (and removed) in d, else why not, in words. A real write,
    because os.access asks the permission bits and a read-only exFAT or NTFS volume, or an SD card with its
    lock switch on, has permissive bits and refuses the write itself. The probe is a hidden name (the walker
    skips dot files) created with O_EXCL, so it can never overwrite anything, and is removed at once."""
    probe = Path(d) / f".sorted-probe-{os.getpid()}-{time.monotonic_ns()}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as e:
        return e.strerror or str(e)
    try:
        os.write(fd, b"x")
    except OSError as e:
        return e.strerror or str(e)
    finally:
        os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass
    return None
# ===== end size =====


def bundle_files(root: Path) -> list[tuple[str, Path]]:
    """(archive name, path) for every thumb, grid and video-frame JPEG of the shoot, sorted."""
    idx = db.index_dir(Path(root)); out = []
    for sub in ("thumbs", "grid", "frames"):
        for p in sorted((idx / sub).glob("*.jpg")):
            out.append((f"{sub}/{p.name}", p))
    return out


def bundle_bytes(root: Path) -> int:
    """Size estimate for the free-space preflight: thumbs + grid + the database file."""
    total = 0
    for _, p in bundle_files(root):
        try: total += p.stat().st_size
        except OSError: pass
    dbf = db.index_dir(Path(root)) / DB_NAME
    if dbf.is_file():
        total += dbf.stat().st_size
    return int(total)


def export_bundle(root: Path, out_dir: Path, progress=None, thumbs: bool = True) -> Path:
    """Write the bundle and return its path. index.db goes in as a consistent snapshot taken with
    the sqlite backup API (never a raw copy of a WAL-mode file); thumbs and grid are stored, not
    deflated, since they are JPEGs already. progress (if given) sees {done, total, failed} after
    every thumb; a thumb that cannot be read is counted as failed, not raised. The zip is built
    under a temp name and renamed into place, so a half-written bundle never carries the real name,
    and a rescan overwrites the last one in a single step.

    thumbs False writes the skeleton: bundle.json (with "skeleton": true) and the database, no JPEGs. It is
    what a picked location gets before a scan starts, a few KB that prove the place can really be written
    and hold the name the finished scan will replace. It opens like any project file."""
    root = Path(root); out_dir = Path(out_dir)
    out = bundle_path(root, out_dir)
    notify = progress or (lambda d: None)
    files = bundle_files(root) if thumbs else []; total = len(files); failed = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory(prefix=".photosort-bundle-") as td:
        snap = Path(td) / DB_NAME
        src = db.connect(root); dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        dst.close(); src.close()
        s = sqlite3.connect(snap)
        n = lambda q: s.execute(q).fetchone()[0]
        info = {
            "format": FORMAT,
            "name": root.resolve().name or "root",
            "root": str(root.resolve()),
            "slug": shoot_slug(root),
            "photos": n("SELECT count(*) FROM photos WHERE status='ok'"),
            "faces": n("SELECT count(*) FROM faces"),
            "references": n("SELECT count(*) FROM ref_faces"),
            "marked": db.mark_counts(s)["marked"],          # flags, stars and labels ride along in index.db
            "scan": dict(db.scan_counts(s), faces=db.get_meta(s, "scan_faces") != "0"),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": NOTE,
        }
        if not thumbs:
            info["skeleton"] = True
        s.close()
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("bundle.json", json.dumps(info, indent=2))
                z.write(snap, DB_NAME)
                for k, (arc, p) in enumerate(files, 1):
                    try:
                        z.write(p, arc, compress_type=zipfile.ZIP_STORED)
                    except OSError:
                        failed += 1
                    notify({"done": k, "total": total, "failed": failed})
            os.replace(part, out)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    if total == 0:
        notify({"done": 0, "total": 0, "failed": 0})
    return out


def inspect_bundle(zip_path: Path) -> dict:
    """bundle.json of a bundle, after checking the format string and that index.db is there.
    Anything else (not a zip, no bundle.json, wrong format) is ValueError("not a sorted project file")."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            if "bundle.json" not in names or DB_NAME not in names:
                raise ValueError("not a sorted project file")
            info = json.loads(z.read("bundle.json"))
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError):
        raise ValueError("not a sorted project file")
    if not isinstance(info, dict) or info.get("format") != FORMAT or not isinstance(info.get("root"), str):
        raise ValueError("not a sorted project file")
    return info


def _bak_name(target: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(f"{target.name}.bak-{stamp}")
    k = 1
    while bak.exists():
        k += 1; bak = target.with_name(f"{target.name}.bak-{stamp}-{k}")
    return bak


def import_bundle(zip_path: Path, root: Path | None = None, progress=None) -> Path:
    """Install a bundle under app_home()/<slug of root>, root defaulting to the bundle's own. The zip
    is extracted into a temp dir next to the target first and only then moved into place; an index
    already there is renamed to <slug>.bak-<timestamp>, never deleted. meta.root in the installed
    db is set to str(root). root need not exist yet (an unmounted disk can be opened later).
    Every member path must resolve inside the temp dir or nothing is installed. Returns the target."""
    zip_path = Path(zip_path)
    info = inspect_bundle(zip_path)
    root = Path(root) if root is not None else Path(info["root"])
    slug = shoot_slug(root)
    home = app_home(); home.mkdir(parents=True, exist_ok=True)
    target = home / slug
    notify = progress or (lambda d: None)
    tmp = Path(tempfile.mkdtemp(prefix=f".{slug}.import-", dir=home))
    try:
        tmp_res = tmp.resolve()
        with zipfile.ZipFile(zip_path) as z:
            members = [i for i in z.infolist() if not i.is_dir()]
            for i in members:
                name = i.filename
                if name.startswith(("/", "\\")) or Path(name).is_absolute() or not (tmp / name).resolve().is_relative_to(tmp_res):
                    raise ValueError(f"bundle member escapes the bundle: {name!r}")
            total = len(members)
            for k, i in enumerate(members, 1):
                z.extract(i, tmp)
                notify({"done": k, "total": total, "failed": 0})
        for sub in ("thumbs", "grid", "frames"):
            (tmp / sub).mkdir(exist_ok=True)
        conn = sqlite3.connect(tmp / DB_NAME)
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        db.set_meta(conn, "root", str(root))
        conn.close()
        bak = None
        if target.exists() or target.is_symlink():
            bak = _bak_name(target)
            os.rename(target, bak)
        try:
            os.rename(tmp, target)
        except BaseException:
            # The old index was already moved aside: put it back, or the shoot has no index at all.
            if bak is not None:
                os.rename(bak, target)
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target
