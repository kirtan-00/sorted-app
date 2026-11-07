"""The full-size view's picture, and the questions an export asks before it starts.

The 1024 px thumbs in the index are right for a grid and useless for judging focus, so the loupe reads the shoot's own
files. A JPEG, PNG or WebP goes to the browser as it is: no decode here, no copy anywhere, and the browser applies the
EXIF orientation itself. HEIC, TIFF and RAW cannot be shown by a browser, so each is decoded once (a RAW through
decode._load_raw, which takes the largest preview embedded in the file) into a JPEG in this Mac's index folder, the same
place the thumbs live. Nothing here ever writes under the shoot root; tests/test_fullsize.py holds that by manifest.

attach() is the one call server.py makes: these routes live here so the server file stays as it was."""
from __future__ import annotations
import os, threading
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import bundle, db, export as export_mod
from .config import export_root
from .decode import DecodeError, load_preview

BROWSER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
CACHE_DIRNAME = "full"
CACHE_QUALITY = 92
CACHE_BYTES = 4 << 30           # about 400 RAW previews; the oldest go first, the index and the thumbs are not touched
NO_LIMIT = 1 << 16              # decode._fit never shrinks at this edge

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

def _lock(key: str) -> threading.Lock:
    """One decode per file: the open and the next-shot preload can ask for the same RAW at the same moment."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())

def _trim(cache: Path, keep: Path, limit: int = CACHE_BYTES) -> None:
    """Oldest first until the folder fits. keep, the file just written, stays whatever its age."""
    files = []
    for p in cache.glob("*.jpg"):
        try:
            st = p.stat(); files.append((st.st_mtime, st.st_size, p))
        except OSError:
            pass
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files, key=lambda f: f[0]):
        if total <= limit:
            break
        if p != keep:
            try:
                p.unlink(); total -= size
            except OSError:
                pass

def decoded(root: Path, rel: str, qhash: str, limit: int = CACHE_BYTES) -> Path:
    """The cached JPEG of a file a browser cannot show, written once through a temp name and a rename."""
    cache = db.index_dir(root) / CACHE_DIRNAME
    out = cache / f"{qhash}.jpg"
    if out.is_file():
        return out
    with _lock(qhash):
        if out.is_file():
            return out
        cache.mkdir(parents=True, exist_ok=True)
        im = load_preview(Path(root) / rel, NO_LIMIT)
        tmp = cache / f".{qhash}.{os.getpid()}.part"
        try:
            im.save(tmp, "JPEG", quality=CACHE_QUALITY)
            os.replace(tmp, out)
        finally:
            try: tmp.unlink()
            except OSError: pass
    _trim(cache, out, limit)
    return out

def source(root: Path, rel: str, qhash: str) -> tuple[Path, str]:
    """(file to send, media type) for one photo. FileNotFoundError when the original is not there right now, which
    a cached decode does not need: a RAW already seen stays viewable with the disk unplugged."""
    root = Path(root); src = root / rel
    kind = BROWSER_TYPES.get(src.suffix.lower())
    if kind:
        if not src.is_file():
            raise FileNotFoundError(str(src))
        return src, kind
    cached = db.index_dir(root) / CACHE_DIRNAME / f"{qhash}.jpg"
    if not cached.is_file() and not src.is_file():
        raise FileNotFoundError(str(src))
    return decoded(root, rel, qhash), "image/jpeg"

class ExportCheckReq(BaseModel):
    ids: list[int]
    name: str
    mode: str = "copy"
    web_size: int | None = None
    include_raw: bool = False

def _gone_words(p: Path) -> str:
    parts = Path(p).parts
    return f"The disk {parts[2]} is not connected" if len(parts) > 2 and parts[1] == "Volumes" else f"{p} is not there"

def attach(app, state) -> None:
    @app.get("/api/full/{photo_id}")
    def full(photo_id: int):
        """The biggest picture of one photo. 404 for a clip, an unknown id or a file that is not there right now, and
        the view stays on the small preview. Cached by the browser against ?v=<qhash>, which a changed file changes."""
        root = state["root"]
        if root is None:
            raise HTTPException(404)
        r = db.connect(root).execute("SELECT rel, qhash, kind FROM photos WHERE id=?", (photo_id,)).fetchone()
        if r is None or r["kind"] == "video":
            raise HTTPException(404)
        try:
            path, kind = source(root, r["rel"], r["qhash"])
        except FileNotFoundError:
            raise HTTPException(404, "that file is not there right now")
        except DecodeError as e:
            raise HTTPException(415, str(e))
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})

    def _base() -> Path:
        return state["export_base"] if state["export_base"] is not None else export_root()

    @app.post("/api/export/check")
    def export_check(req: ExportCheckReq):
        """Asked once, as Export is pressed, and it writes nothing. {problem}: a sentence when the export cannot start.
        Else {dest, missing, already, clash, free_name}: files gone from the source, files this export has already put
        in the folder, files of the same name that are something else (another size, say), and the next folder name
        nothing is using, for Keep both."""
        root = state["root"]
        if root is None:
            raise HTTPException(400, "no folder open")
        if req.mode != "csv" and not Path(root).is_dir():
            return {"problem": "The shoot disk is not connected, so nothing was exported. Plug it in and export again."}
        base = _base()
        if state["export_base"] is not None and not base.is_dir():
            return {"problem": f"{_gone_words(base)}, so nothing was exported. Plug it in, or use Change at the top to pick another place."}
        if base.is_dir():
            why = bundle.probe_writable(base)
            if why:
                return {"problem": f"Nothing was exported: {base} cannot be written to ({why})."}
        try:
            out = export_mod.export_dir(root, req.name, base)
        except ValueError as e:
            return {"problem": str(e)}
        found = export_mod.existing(root, req.ids, out, req.mode, req.web_size, req.include_raw)
        return dict(found, problem=None, dest=str(out), free_name=export_mod.free_folder(root, req.name, base))

    @app.post("/api/export/replace")
    def export_replace(req: ExportCheckReq):
        """Replace, on the question above: takes this selection's earlier copies out of the folder so the export that
        follows writes them fresh. Only when the original is there to copy again, and only in the export folder."""
        root = state["root"]
        if root is None:
            raise HTTPException(400, "no folder open")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")
        try:
            out = export_mod.export_dir(root, req.name, _base())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"removed": export_mod.remove_earlier(root, req.ids, out, req.include_raw)}
