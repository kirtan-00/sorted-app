"""Index bundles: everything the app knows about a shoot (index.db with its saved people, thumbs,
grid thumbs) in one <shoot>.photosort-index.zip, so a ready index can be handed to another Mac and
opened there without re-indexing. Format "photosort-index/1": bundle.json, index.db, thumbs/*.jpg,
grid/*.jpg, frames/*.jpg (sampled video frames). The shoot root is only ever read; a bundle never lands under it."""
from __future__ import annotations
import json, os, shutil, sqlite3, tempfile, time, zipfile
from pathlib import Path
from . import db
from .config import DB_NAME, app_home, shoot_slug

FORMAT = "photosort-index/1"
SUFFIX = ".photosort-index.zip"
NOTE = "Unzip into ~/Library/Application Support/photosort/ or use Import index bundle in the app."


def bundle_path(root: Path, out_dir: Path) -> Path:
    """<out_dir>/<shoot name>.photosort-index.zip. Refuses an out_dir that is the (read-only)
    shoot root or inside it. Does not create anything."""
    root_res = Path(root).resolve(); out_res = Path(out_dir).resolve()
    if out_res == root_res or out_res.is_relative_to(root_res):
        raise ValueError("bundle destination is inside the source folder")
    return Path(out_dir) / f"{root_res.name or 'root'}{SUFFIX}"


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


def export_bundle(root: Path, out_dir: Path, progress=None) -> Path:
    """Write the bundle and return its path. index.db goes in as a consistent snapshot taken with
    the sqlite backup API (never a raw copy of a WAL-mode file); thumbs and grid are stored, not
    deflated, since they are JPEGs already. progress (if given) sees {done, total, failed} after
    every thumb; a thumb that cannot be read is counted as failed, not raised. The zip is built
    under a temp name and renamed into place, so a half-written bundle never carries the real name."""
    root = Path(root); out_dir = Path(out_dir)
    out = bundle_path(root, out_dir)
    notify = progress or (lambda d: None)
    files = bundle_files(root); total = len(files); failed = 0
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
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": NOTE,
        }
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
    Anything else (not a zip, no bundle.json, wrong format) is ValueError("not a photosort index bundle")."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            if "bundle.json" not in names or DB_NAME not in names:
                raise ValueError("not a photosort index bundle")
            info = json.loads(z.read("bundle.json"))
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError):
        raise ValueError("not a photosort index bundle")
    if not isinstance(info, dict) or info.get("format") != FORMAT or not isinstance(info.get("root"), str):
        raise ValueError("not a photosort index bundle")
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
