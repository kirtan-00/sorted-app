from __future__ import annotations
import json
import mimetypes
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import db
from . import usage
from . import awake
from . import classify as classify_mod
from . import focus as focus_mod
from . import settings
from .config import app_home, export_root
from .search import Index, Filters
from .export import export_ids, export_bytes

UI = Path(__file__).parent / "ui"
RECENT_FILE = "recent.json"
RECENT_MAX = 8


class ExportReq(BaseModel):
    ids: list[int]
    name: str
    mode: str = "copy"


class NameReq(BaseModel):
    name: str


class RenameReq(BaseModel):
    old: str
    new: str


class ClusterReq(BaseModel):
    eps: float | None = None        # None: FACE_CLUSTER_EPS


class MergeReq(BaseModel):
    keep: int
    drop: int


class RejectReq(BaseModel):
    a: int
    b: int


class IndexReq(BaseModel):
    faces: bool = True
    retry_errors: bool = False
    resume: bool = False          # Continue scan: faces as the interrupted scan had it (meta scan_faces), not the box


class ModeReq(BaseModel):
    mode: str = "copy"


class ReorganisePlanReq(BaseModel):
    by_people: bool = False


class ReorganiseApplyReq(BaseModel):
    plan_id: str
    confirm: str          # the shoot folder's name, typed back


class FolderReq(BaseModel):
    path: str


class FindReq(BaseModel):
    path: str
    min_sim: float | None = None


class DestinationReq(BaseModel):
    path: str


class CategoriesExportReq(BaseModel):
    categories: list[str] | None = None     # fixed categories; None = every one that has a photo
    discovered: list[str] | None = None     # discovered names; None or [] = none
    mode: str = "copy"
    include_raw: bool = False
    include_unsure: bool = False            # also the "less sure" band (score under SURE_MIN, or a guess)
    videos: str = "clips"                   # or "segments": only the scenes labelled the ticked category, trimmed
    drone: bool = False                     # every aerial row (any kind, any category) also under categories/drone/
    hide_bad: bool = False                  # leave out rows the focus pass labelled bad


class ReferenceReq(BaseModel):
    name: str
    path: str


class MinSimReq(BaseModel):
    min_sim: float | None = None


class ReferencesExportReq(BaseModel):
    names: list[str] | None = None
    mode: str = "copy"
    include_raw: bool = False
    min_sim: float | None = None


class BundleImportReq(BaseModel):
    zip: str
    root: str | None = None


class BundleZipReq(BaseModel):
    zip: str


class BundleExportReq(BaseModel):
    dest: str | None = None                 # folder the scan file goes in; None = the export destination


class RevealReq(BaseModel):
    path: str


class DriveLinkReq(BaseModel):
    link: str


class DriveExportReq(BaseModel):
    link: str
    what: str                               # "categories", "people" or "selection"
    categories: list[str] | None = None     # categories: as CategoriesExportReq
    discovered: list[str] | None = None
    include_unsure: bool = False
    videos: str = "clips"                   # "segments" is refused for Drive in this pass
    drone: bool = False
    hide_bad: bool = False
    names: list[str] | None = None          # people: saved names, None = every one
    min_sim: float | None = None
    ids: list[int] = []                     # selection
    name: str = "selection"                 # selection: the subfolder under the Drive folder
    include_raw: bool = False
    web_size: int | None = None             # long edge in px for photos; videos and RAW go as is
    skip_videos: bool = False               # with web_size: leave videos out altogether


def _load_recent() -> list[str]:
    p = app_home() / RECENT_FILE
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def _save_recent(path_str: str) -> None:
    p = app_home() / RECENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    recent = [r for r in _load_recent() if r != path_str]
    recent.insert(0, path_str)
    p.write_text(json.dumps(recent[:RECENT_MAX]))


def create_app(root: Path | None = None) -> FastAPI:
    root = Path(root) if root is not None else None
    app = FastAPI(title="photosort")
    state = {
        "root": root,
        "index": Index(root) if root is not None else None,
        "progress": {"stage": "idle", "done": 0, "total": 0},
        "running": False,
        "stale": False,
        "classify": {"running": False, "counts": {}, "discovered": {}, "error": None},
        "focus": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "export": {"running": False, "done": 0, "total": 0, "failed": 0, "skipped": 0, "path": None, "error": None},
        # Where exports land instead of export_root() (another disk), or None for the default.
        "export_base": settings.get_export_base(),
        # Held from the "already running" check through setting running=True, and around a folder
        # switch, so two rapid export POSTs (or a switch during the preflight) cannot both pass.
        "export_lock": threading.Lock(),
        "focus_lock": threading.Lock(),
    }
    app.state.photosort = state

    # ===== usage log (photosort/usage.py): one session per server start; every 4xx/5xx and every uncaught
    # exception is an event, logged against the route template so a person's name in a URL never lands in it.
    usage.start_session()
    usage.log("server_start", **usage.system_info())

    def _route_path(request: Request) -> str:
        """The matched route's template (/api/people/references/{name}/find), or for an unmatched URL its
        first two segments: the raw path could carry a name, the template never does."""
        route = request.scope.get("route")
        tpl = getattr(route, "path_format", None) or getattr(route, "path", None)
        if tpl:
            return tpl
        parts = [p for p in request.url.path.split("/") if p][:2]
        return "/" + "/".join(parts) + ("/..." if len(request.url.path.split("/")) > 3 else "")

    @app.exception_handler(StarletteHTTPException)
    async def _log_http_error(request: Request, exc: StarletteHTTPException):
        route = _route_path(request)
        if exc.status_code >= 400 and not (exc.status_code == 404 and (route.startswith("/ui/") or route.startswith("/api/thumb"))):
            usage.log("api_error", route=route, status=exc.status_code, message=str(exc.detail), method=request.method)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _log_validation_error(request: Request, exc: RequestValidationError):
        first = (exc.errors() or [{}])[0]
        msg = " ".join(str(x) for x in (".".join(str(p) for p in first.get("loc", ())), first.get("type", ""), first.get("msg", "")) if x)
        usage.log("api_error", route=_route_path(request), status=422, message=msg or "invalid request", method=request.method)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _log_uncaught(request: Request, exc: Exception):
        # Starlette re-raises after this returns, so the terminal still sees the traceback.
        usage.log("exception", route=_route_path(request), status=500, error=f"{type(exc).__name__}: {exc}", method=request.method)
        return PlainTextResponse("Internal Server Error", status_code=500)
    # ===== end usage log =====

    def _disk_name(r: Path) -> str:
        """What to ask the user to plug back in: the volume under /Volumes, else the folder itself."""
        parts = r.parts
        if len(parts) >= 3 and parts[:2] == ("/", "Volumes"):
            return parts[2]
        return r.name or str(r)

    def _folder_info() -> dict:
        r = state["root"]
        if r is None:
            return {"root": None, "name": None, "indexed": False, "mounted": False, "disk": None}
        conn = db.connect(r)
        n = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
        # mounted: the folder is reachable right now; false once the shoot disk is unplugged.
        # scan: how far the scan got (the welcome needs it while the disk is away, before any stats call).
        return {"root": str(r), "name": r.name or str(r), "indexed": n > 0, "items": n,
                "mounted": r.is_dir(), "disk": _disk_name(r), "scan": _scan_block(conn)}

    def _public_folder_info() -> dict:
        return {k: v for k, v in _folder_info().items() if k != "items"}

    # ===== resume: what the index says about its own scan, and the jobs cut short =====
    def _open_root(r: Path) -> None:
        """Housekeeping when a shoot is opened: nothing can be running for it yet, so a job row still
        'running' was cut short (the app quit, the Mac died) and is marked interrupted."""
        try:
            db.interrupt_running_jobs(db.connect(r))
        except Exception:
            pass

    EMPTY_SCAN = dict(items=0, scanned=0, embedded=0, faced=0, errors=0, pending=0, pending_photos=0, pending_clips=0,
                      photos=0, clips=0, scanned_photos=0, scanned_clips=0, unembedded=0, complete=True,
                      faces=True, interrupted=None)

    def _scan_block(conn) -> dict:
        """The scan: {items, scanned, embedded, faced, complete, ...counts, faces, interrupted}. faces is what
        the last scan was asked for (Continue reuses it). interrupted is the most recent scan or focus job
        that never finished, with its last progress, or None."""
        out = db.scan_counts(conn)
        out["faces"] = db.get_meta(conn, "scan_faces") != "0"
        jobs = db.latest_jobs(conn)
        cut = [j for j in jobs.values() if j["state"] == "interrupted" and j["kind"] in ("scan", "focus")]
        # An interrupted scan whose work is all done by now (Continue ran, counts say complete) is history.
        cut = [j for j in cut if not (j["kind"] == "scan" and out["complete"])]
        if cut:
            j = max(cut, key=lambda j: j["id"])
            out["interrupted"] = {"kind": j["kind"], "stage": j["progress"].get("stage"), "done": j["progress"].get("done", 0),
                                  "total": j["progress"].get("total", 0), "started": j["started"], "error": j["error"]}
        else:
            out["interrupted"] = None
        return out
    # ===== end resume =====

    def _log_folder_open(imported: bool = False) -> None:
        info = _folder_info()
        if info["root"]:
            usage.log("folder_open", shoot=info["name"], items=info["items"], indexed=info["indexed"], imported=imported)

    if root is not None:
        _open_root(root)
        _log_folder_open()

    def _switch_root(new_root: Path) -> dict:
        if state["running"]:
            raise HTTPException(409, "cannot switch folders while scanning")
        with state["export_lock"]:
            if state["export"]["running"]:
                raise HTTPException(409, "cannot switch folders while an export is running")
            state["root"] = new_root
            state["index"] = Index(new_root)
            state["progress"] = {"stage": "idle", "done": 0, "total": 0}
            state["stale"] = False
            state["classify"] = {"running": False, "counts": {}, "discovered": {}, "error": None}
            state["focus"] = {"running": False, "done": 0, "total": 0, "counts": {}, "error": None}
            state["export"] = {"running": False, "done": 0, "total": 0, "failed": 0, "skipped": 0, "path": None, "error": None}
        _open_root(new_root)
        _save_recent(str(new_root))
        _log_folder_open()
        return _public_folder_info()

    def _auto_classify(root_at_start: Path, stats: dict) -> None:
        """Categorise at the end of an index run that changed something. A classify failure is
        recorded on state["classify"] and never marks the index run itself as failed."""
        if stats.get("indexed", 0) == 0 and stats.get("embedded", 0) == 0 and stats.get("faced", 0) == 0:
            return
        if state["classify"]["running"]:          # a manual Categorise is already on it
            return
        total = stats.get("total", 0)
        state["classify"] = {"running": True, "counts": {}, "discovered": {}, "error": None}
        state["progress"] = {"stage": "categorise", "done": 0, "total": 0, "stage_started": time.time()}
        t0 = time.time()
        try:
            state["classify"]["counts"] = classify_mod.classify_and_store(root_at_start)
            state["classify"]["discovered"] = classify_mod.discover_and_store(root_at_start)
        except Exception as e:
            state["classify"]["error"] = f"{type(e).__name__}: {e}"
        finally:
            state["classify"]["running"] = False
            state["progress"] = {"stage": "done", "done": total, "total": total, "stage_started": time.time()}
            _log_classify_done(t0, auto=True)

    def _log_classify_done(t0: float, auto: bool) -> None:
        c = state["classify"]
        usage.log("classify_done", auto=auto, seconds=round(time.time() - t0, 1), counts=c["counts"],
                  discovered=len(c["discovered"] or {}), error=c["error"])

    def _run(root_at_start: Path, faces: bool, retry_errors: bool):
        from .index import index_folder
        def prog(d):
            state["progress"] = d
        from .index import SourceUnavailable
        t0 = time.time(); stats = {}; err = None
        try:
            with awake.hold():
                stats = index_folder(root_at_start, faces=faces, progress=prog, retry_errors=retry_errors)
                _auto_classify(root_at_start, stats)
        except SourceUnavailable as e:
            # The disk went away: not a failure, a pause. What was scanned is kept; Continue does the rest
            # once the disk is back (the UI reads mounted from /api/stats).
            err = str(e)
            last = state["progress"] if isinstance(state["progress"], dict) else {}
            state["progress"] = {"stage": "paused", "error": err, "done": last.get("done", 0), "total": last.get("total", 0)}
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            err = msg
            state["progress"] = {"stage": "error", "error": msg, "done": 0, "total": 0}
        finally:
            state["running"] = False
            state["stale"] = True
            kinds = {}
            try:
                kinds = db.kind_counts(db.connect(root_at_start))
            except Exception:
                pass
            usage.log("index_done", shoot=root_at_start.name, items=stats.get("total", 0), indexed=stats.get("indexed", 0),
                      skipped=stats.get("skipped", 0), photos=kinds.get("photos"), videos=kinds.get("videos"), faces=faces,
                      faced=stats.get("faced", 0), failures=stats.get("errors", 0), seconds=round(time.time() - t0, 1), error=err)

    def ix() -> Index:
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["stale"]:
            state["index"].refresh()
            state["stale"] = False
        return state["index"]

    @app.get("/")
    def home():
        return FileResponse(UI / "index.html")

    @app.get("/ui/{name}")
    def ui(name: str):
        p = UI / name
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/folder")
    def get_folder():
        return _public_folder_info()

    @app.post("/api/folder/choose")
    def choose_folder():
        # Same gates as _switch_root, checked up front so nobody sits through the picker for a 409.
        if state["running"]:
            raise HTTPException(409, "cannot switch folders while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot switch folders while an export is running")
        try:
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Pick the photo folder")'],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "folder picker timed out")
        except FileNotFoundError:
            raise HTTPException(501, "folder picker unavailable (osascript not found)")
        path_str = result.stdout.strip()
        if result.returncode != 0 or not path_str:
            return Response(status_code=204)
        p = Path(path_str)
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {path_str}")
        return _switch_root(p)

    @app.post("/api/folder")
    def set_folder(req: FolderReq):
        p = Path(req.path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {req.path}")
        return _switch_root(p.resolve())

    @app.get("/api/folder/recent")
    def recent_folders():
        out = []
        for path_str in _load_recent():
            p = Path(path_str)
            out.append({"path": path_str, "name": p.name or path_str})
        return {"recent": out}

    @app.get("/api/stats")
    def stats():
        root = state["root"]
        if root is None:
            return dict(root=None, photos=0, videos=0, faces=0, people=0, errors=0, last_index=None, indexing=state["running"],
                        faces_pending=0, focus={"checked": 0, "bad": 0, "soft": 0}, scan=dict(EMPTY_SCAN))
        conn = db.connect(root)
        n = lambda q: conn.execute(q).fetchone()[0]
        last = conn.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        kinds = db.kind_counts(conn)
        return dict(
            root=str(root),
            photos=kinds["photos"],
            videos=kinds["videos"],
            faces=n("SELECT count(*) FROM faces"),
            people=n("SELECT count(*) FROM people"),
            errors=n("SELECT count(*) FROM photos WHERE status='error'"),
            last_index=last[0] if last else None,
            indexing=state["running"],
            faces_pending=n("SELECT count(*) FROM photos WHERE status='ok' AND n_faces IS NULL"),
            focus={k: v for k, v in focus_mod.status(root).items() if k != "unchecked"},
            mounted=root.is_dir(),
            scan=_scan_block(conn),
        )

    @app.get("/api/scan/health")
    def scan_health():
        """The Scan tab's health line, recomputed when the tab opens: the scan block plus on_disk, a fresh
        count of the items in the folder right now (None while the disk is away or a scan is running, when
        the walk would only race it), new_on_disk (items the index has no row for), faces_pending, and fix:
        the one thing that closes the gap ("continue" a scan, "rescan" for files the index has not seen,
        "faces" for photos never checked, "focus" when the last focus check was cut short, None when there
        is nothing to do)."""
        root = state["root"]
        if root is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(root)
        scan = _scan_block(conn)
        mounted = root.is_dir()
        on_disk = None; new_on_disk = 0
        if mounted and not state["running"]:
            from .walk import find_images
            try:
                files = find_images(root)
            except OSError:
                files = []
            if files or scan["items"] == 0:      # an empty walk on a shoot with rows is the unmounted guard's case, not a count
                on_disk = len(files)
                have = set(db.known_files(conn)) | set(db.missing_files(conn))
                have |= {r[0] for r in conn.execute("SELECT rel FROM photos WHERE status='pending'")}
                new_on_disk = sum(1 for f in files if f.rel not in have)
        faces_pending = int(conn.execute("SELECT count(*) FROM photos WHERE status='ok' AND n_faces IS NULL").fetchone()[0])
        cut = scan["interrupted"]
        if scan["pending"] or scan["unembedded"] or (cut and cut["kind"] == "scan"):
            fix = "continue"
        elif cut and cut["kind"] == "focus":
            fix = "focus"                     # Continue for a focus pass is POST /api/focus, not a scan
        elif new_on_disk:
            fix = "rescan"
        elif faces_pending:
            fix = "faces"
        else:
            fix = None
        return dict(scan, on_disk=on_disk, new_on_disk=new_on_disk, faces_pending=faces_pending, mounted=mounted,
                    running=state["running"], fix=fix)

    @app.get("/api/errors")
    def errors():
        if state["root"] is None:
            return {"errors": []}
        conn = db.connect(state["root"])
        rows = conn.execute("SELECT rel, indexed_at FROM photos WHERE status='error' ORDER BY rel").fetchall()
        return {"errors": [{"rel": r[0], "indexed_at": r[1]} for r in rows]}

    @app.post("/api/index")
    def start_index(req: IndexReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "already scanning")
        # A reorganise or undo renames files under the root while it runs; an index pass in the
        # middle would insert sorted/ rows before their old rows are re-keyed.
        if state["export"]["running"] and state["export"].get("what") in ("reorganise", "undo"):
            raise HTTPException(409, "cannot scan while the disk is being reorganised")
        if state["focus"]["running"]:
            raise HTTPException(409, "cannot scan while the focus check is running")
        faces = req.faces
        if req.resume:
            # Continue scan: the interrupted scan's own faces choice, so a scan started with faces off does
            # not grow a faces pass (and one started with faces on does not lose it) on the way back.
            faces = db.get_meta(db.connect(state["root"]), "scan_faces") != "0"
        if not state["root"].is_dir():
            raise HTTPException(409, f"{_disk_name(state['root'])} is not connected; plug it in first")
        state["running"] = True
        state["progress"] = {"stage": "scan", "done": 0, "total": 0, "stage_started": time.time()}
        usage.log("index_start", shoot=state["root"].name, faces=faces, retry_errors=req.retry_errors, resume=req.resume)
        threading.Thread(target=_run, args=(state["root"], faces, req.retry_errors), daemon=True).start()
        return {"started": True, "faces": faces}

    @app.get("/api/progress")
    def progress():
        """The scan's progress plus running and awake (caffeinate is holding the Mac awake for a job)."""
        return dict(state["progress"], running=state["running"], awake=awake.held())

    def _filters(sharp, faces, person, taken_from, taken_to, category, kind=None, cluster=None, sure_only=0, aerial=0,
                 hide_bad=0, hide_soft=0) -> Filters:
        if kind not in (None, "", "photos", "videos"):
            raise HTTPException(400, "kind must be photos or videos")
        return Filters(sharp_min_pct=sharp, faces=faces or None, person_id=person, taken_from=taken_from,
                       taken_to=taken_to, category=category or None, kind=kind or None,
                       cluster=cluster or None, sure_only=bool(sure_only), aerial=True if aerial else None,
                       hide_bad=bool(hide_bad), hide_soft=bool(hide_soft))

    @app.get("/api/search")
    def search(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
               person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
               category: str | None = None, kind: str | None = None, cluster: str | None = None, sure_only: int = 0,
               aerial: int = 0, hide_bad: int = 0, hide_soft: int = 0, limit: int = 200, offset: int = 0):
        """Each result carries kind, duration and aerial, sure and confidence, and focus (None until the focus
        pass ran, else ok / soft / bad); with a category or cluster filter the sure ones come first, then the
        "less sure" band by confidence. sure_only=1 drops the band (the per-tile export uses it).
        kind=photos|videos keeps one kind; aerial=1 keeps drone shots only; hide_bad=1 drops rows labelled
        bad, hide_soft=1 drops soft and bad (unchecked rows are never hidden)."""
        if state["root"] is None:
            return {"results": [], "total": 0, "offset": 0, "limit": limit}
        limit = max(1, min(limit, 1000)); offset = max(0, offset)
        t0 = time.time()
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only, aerial, hide_bad, hide_soft))
        except LookupError as e:
            raise HTTPException(404, str(e))
        # The usage log keeps the query's length and first characters plus which filters were on; an empty
        # search (the grid's own reload) and a "Show more" page are not logged.
        filt = {k: v for k, v in dict(sharp=sharp, faces=faces, person=bool(person), taken_from=taken_from, taken_to=taken_to,
                                      category=category, kind=kind, cluster=cluster, sure_only=sure_only, aerial=aerial,
                                      hide_bad=hide_bad, hide_soft=hide_soft, image_id=bool(image_id)).items() if v}
        if offset == 0 and (q or filt):
            usage.log("search", q=q or "", filters=filt, total=len(rows), ms=round((time.time() - t0) * 1000))
        return {"results": [dict(p) for p in rows[offset:offset + limit]], "total": len(rows), "offset": offset, "limit": limit}

    @app.get("/api/search/ids")
    def search_ids(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
                   person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
                   category: str | None = None, kind: str | None = None, cluster: str | None = None, sure_only: int = 0,
                   aerial: int = 0, hide_bad: int = 0, hide_soft: int = 0):
        if state["root"] is None:
            return {"ids": [], "total": 0}
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only, aerial, hide_bad, hide_soft))
        except LookupError as e:
            raise HTTPException(404, str(e))
        return {"ids": [p["id"] for p in rows], "total": len(rows)}

    @app.get("/api/thumb/{qhash}")
    def thumb(qhash: str, size: str = "grid"):
        if state["root"] is None:
            raise HTTPException(404)
        p = db.index_dir(state["root"]) / ("grid" if size == "grid" else "thumbs") / f"{qhash}.jpg"
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    # Videos: the original file for the lightbox player, and the per-scene breakdown with its frames.

    MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
                   ".mts": "video/mp2t", ".avi": "video/x-msvideo"}

    @app.get("/api/media/{photo_id}")
    def media(photo_id: int):
        """The original file, streamed with range support (Starlette's FileResponse), so a <video>
        can seek. 404 for an unknown id or a file that is not there right now (disk unplugged)."""
        if state["root"] is None:
            raise HTTPException(404)
        r = db.connect(state["root"]).execute("SELECT rel FROM photos WHERE id=?", (photo_id,)).fetchone()
        if r is None:
            raise HTTPException(404)
        p = state["root"] / r[0]
        if not p.is_file():
            raise HTTPException(404, "that file is not there right now")
        mt = MEDIA_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return FileResponse(p, media_type=mt)

    @app.get("/api/segments/{photo_id}")
    def segments(photo_id: int):
        if state["root"] is None:
            raise HTTPException(404)
        conn = db.connect(state["root"])
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone() is None:
            raise HTTPException(404)
        out = [{"idx": s["idx"], "start": s["start"], "end": s["end"], "category": s["category"],
                "score": s["category_score"], "frame_url": f"/api/frame/{s['frame']}"} for s in db.list_segments(conn, photo_id)]
        return {"segments": out}

    @app.get("/api/frame/{name}")
    def frame(name: str):
        if state["root"] is None:
            raise HTTPException(404)
        frames = db.index_dir(state["root"]) / "frames"
        p = frames / name
        if "/" in name or name in (".", "..") or p.resolve().parent != frames.resolve() or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/people")
    def people():
        if state["root"] is None:
            return []
        from .people import list_people
        return list_people(state["root"])

    @app.post("/api/people/cluster")
    def cluster(req: ClusterReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        from .people import cluster_faces
        t0 = time.time()
        out = cluster_faces(state["root"], eps=req.eps)
        state["stale"] = True
        usage.log("people_group", groups=len(out), eps=req.eps, ms=round((time.time() - t0) * 1000))
        return out

    @app.post("/api/people/{pid}/name")
    def name(pid: int, req: NameReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        from .people import name_person
        name_person(state["root"], pid, req.name)
        usage.log("rename", what="group", chars=len(req.name))
        return {"ok": True}

    # "Same person?" merge suggestions. Answers are remembered by face id (db.face_links) and applied on
    # every re-cluster; a yes merges now, a no hides the pair for good. 409 while indexing: the faces
    # table is being rewritten under us.
    @app.get("/api/people/suggestions")
    def people_suggestions():
        if state["root"] is None:
            return {"suggestions": []}
        if state["running"]:
            raise HTTPException(409, "scanning")
        from .people import suggest_merges
        return {"suggestions": suggest_merges(state["root"])}

    @app.post("/api/people/merge")
    def people_merge(req: MergeReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "scanning")
        from .people import merge_people
        try:
            out = merge_people(state["root"], req.keep, req.drop)
        except ValueError as e:
            raise HTTPException(400, str(e))
        state["stale"] = True
        usage.log("people_merge")
        return out

    @app.post("/api/people/reject")
    def people_reject(req: RejectReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "scanning")
        from .people import reject_merge
        try:
            reject_merge(state["root"], req.a, req.b)
        except ValueError as e:
            raise HTTPException(400, str(e))
        usage.log("people_reject")
        return {"ok": True}

    def _find_person(p: Path, min_sim: float | None) -> dict:
        # The reference is only read; results are index rows in the same shape as /api/search
        # plus score = cosine sim, so the grid can show them unchanged.
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if not p.is_file():
            raise HTTPException(400, f"not a readable file: {p}")
        from .people import find_by_reference, ReferenceUnreadable
        from .config import FACE_MATCH_MIN_SIM
        try:
            found = find_by_reference(state["root"], p, FACE_MATCH_MIN_SIM if min_sim is None else min_sim, unsure_band=True)
        except ReferenceUnreadable:
            raise HTTPException(400, "could not read that image")
        photos = ix().photos
        results = [dict(photos[m["photo_id"]], score=m["sim"], sure=m["sure"], confidence=m["sim"])
                   for m in found["matches"] if m["photo_id"] in photos]
        out = {"faces_in_reference": found["faces_in_reference"], "person_id": found["person_id"],
               "total": len(results), "results": results}
        if found.get("reference_face_too_small"):
            out["reference_face_too_small"] = True
        usage.log("person_find_by_photo", matches=len(results), faces_in_reference=found["faces_in_reference"],
                  min_sim=min_sim, too_small=bool(found.get("reference_face_too_small")))
        return out

    @app.post("/api/people/find")
    def find_person(req: FindReq):
        return _find_person(Path(req.path).expanduser(), req.min_sim)

    @app.post("/api/people/find/choose")
    def find_person_choose():
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        try:
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose file with prompt "Pick a photo of the person" of type {"public.image"})'],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "photo picker timed out")
        except FileNotFoundError:
            raise HTTPException(501, "photo picker unavailable (osascript not found)")
        path_str = result.stdout.strip()
        if result.returncode != 0 or not path_str:
            return Response(status_code=204)
        # path rides along so the UI can re-run /api/people/find at another min_sim without the picker.
        return dict(_find_person(Path(path_str), None), path=path_str)

    # Named people: reference photos saved under a name, matched on demand at the slider's min_sim.

    def _min_sim(v: float | None) -> float:
        from .config import FACE_MATCH_MIN_SIM
        return FACE_MATCH_MIN_SIM if v is None else v

    @app.post("/api/people/references")
    def save_reference_api(req: ReferenceReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        p = Path(req.path).expanduser()
        if not p.is_file():
            raise HTTPException(400, f"not a readable file: {p}")
        from .people import save_reference, ReferenceUnreadable
        try:
            out = save_reference(state["root"], req.name, p)
        except ReferenceUnreadable:
            raise HTTPException(400, "could not read that image")
        except ValueError as e:
            raise HTTPException(400, str(e))
        usage.log("person_save", faces_in_reference=out["faces_in_reference"], chars=len(req.name))
        return {"id": out["id"], "name": out["name"], "faces_in_reference": out["faces_in_reference"]}

    @app.get("/api/people/references")
    def list_references_api(min_sim: float | None = None):
        if state["root"] is None:
            return {"people": []}
        from .people import match_references
        conn = db.connect(state["root"])
        groups: dict[str, dict] = {}
        for r in db.list_references(conn):      # every saved name, even one with no match at this min_sim
            g = groups.setdefault(r["name"], {"name": r["name"], "reference_ids": [], "sources": [], "count": 0})
            g["reference_ids"].append(r["id"]); g["sources"].append(r["source"])
        matched = match_references(state["root"], _min_sim(min_sim))
        for name, g in groups.items():
            g["count"] = len(matched.get(name, []))
        return {"people": sorted(groups.values(), key=lambda g: (-g["count"], g["name"]))}

    def _reference_names() -> set[str]:
        return {r["name"] for r in db.list_references(db.connect(state["root"]))}

    @app.post("/api/people/references/{name:path}/find")
    def find_reference_api(name: str, req: MinSimReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if name not in _reference_names():
            raise HTTPException(404, f"no saved person called {name!r}")
        from .people import match_references
        photos = ix().photos
        matches = match_references(state["root"], _min_sim(req.min_sim), unsure_band=True).get(name, [])
        results = [dict(photos[m["photo_id"]], score=m["sim"], sure=m["sure"], confidence=m["sim"])
                   for m in matches if m["photo_id"] in photos]
        usage.log("person_find_saved", matches=len(results), min_sim=_min_sim(req.min_sim))
        return {"name": name, "total": len(results), "results": results}

    @app.post("/api/people/references/{name:path}/rename")
    def rename_reference_api(name: str, req: NameReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        new = req.name.strip()
        if not new:
            raise HTTPException(400, "give the person a name")
        from .export import safe_segment
        try:
            safe_segment(new)
        except ValueError:
            raise HTTPException(400, "that name cannot be used as a folder")
        moved = db.rename_reference(db.connect(state["root"]), name, new)
        if moved == 0:
            raise HTTPException(404, f"no saved person called {name!r}")
        usage.log("rename", what="person", chars=len(new), moved=moved)
        return {"ok": True, "name": new, "moved": moved}

    @app.delete("/api/people/references/{ref_id}")
    def delete_reference_api(ref_id: int):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if not db.delete_reference(db.connect(state["root"]), ref_id):
            raise HTTPException(404, "no such reference")
        return {"ok": True}

    def _fixed_order(counts: dict[str, int]) -> dict[str, int]:
        """The tile order: CATEGORIES in their calibrated order, then "other", then "unclassified" (SQLite's
        GROUP BY would hand them back alphabetically)."""
        order = list(classify_mod.CATEGORIES) + [classify_mod.FALLBACK, "unclassified"]
        return {k: counts[k] for k in order if k in counts} | {k: v for k, v in counts.items() if k not in order}

    @app.get("/api/categories")
    def categories():
        """fixed: the CATEGORIES counts (plus "other" and "unclassified"); discovered: the k-means
        clusters named from the vocabulary, largest first, empty until Categorise has run; drone: how many
        rows are flagged aerial (a flag across categories, the last tile of the fixed row)."""
        if state["root"] is None:
            return {"fixed": {}, "discovered": {}, "drone": 0}
        conn = db.connect(state["root"])
        return {"fixed": _fixed_order(db.category_counts(conn)), "discovered": db.cluster_counts(conn), "drone": db.aerial_count(conn)}

    @app.post("/api/categories/discovered/rename")
    def rename_discovered(req: RenameReq):
        """Rename a discovered category (an unnamed "group N", or a wrong name) in place. 409 while
        indexing or categorising: both rewrite the cluster column underneath the rename."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot rename while scanning")
        if state["classify"]["running"]:
            raise HTTPException(409, "cannot rename while categorising")
        try:
            moved = classify_mod.rename_cluster(state["root"], req.old, req.new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if moved == 0:
            raise HTTPException(404, f"no discovered category called {req.old!r}")
        state["stale"] = True      # the search Index caches the cluster column
        usage.log("rename", what="category", chars=len(req.new.strip()), moved=moved)
        return {"ok": True, "name": req.new.strip(), "moved": moved}

    @app.post("/api/classify")
    def start_classify():
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["classify"]["running"]:
            raise HTTPException(409, "already categorising")
        if state["focus"]["running"]:
            raise HTTPException(409, "cannot categorise while the focus check is running")
        state["classify"] = {"running": True, "counts": {}, "discovered": {}, "error": None}
        root_at_start = state["root"]
        t0 = time.time()

        def _run_classify():
            # Both bars fill in one pass: the fixed categories first, then the discovered ones. The Index
            # is marked stale in finally, after both: a search in between would refresh it and clear the
            # flag, and the cluster columns written after that would never reach the next search.
            try:
                with awake.hold():
                    state["classify"]["counts"] = classify_mod.classify_and_store(root_at_start)
                    state["classify"]["discovered"] = classify_mod.discover_and_store(root_at_start)
            except Exception as e:
                state["classify"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["classify"]["running"] = False
                _log_classify_done(t0, auto=False)

        threading.Thread(target=_run_classify, daemon=True).start()
        return {"started": True}

    @app.get("/api/classify/progress")
    def classify_progress():
        return dict(state["classify"], awake=awake.held())

    # The on-demand focus pass: started from the "hide blurry" filter, never at the end of an index.
    # Same job shape as classify: one background thread, progress polled, the Index marked stale after.

    @app.post("/api/focus")
    def start_focus():
        """Label unchecked rows ok / soft / bad (focus.check_focus). 409 while indexing, categorising or
        exporting, or while a focus pass is already running. {"started": true, "total": n} with n the rows
        that will be checked (0 is fine: the job ends at once)."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot check focus while scanning")
        if state["classify"]["running"]:
            raise HTTPException(409, "cannot check focus while categorising")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot check focus while an export is running")
        with state["focus_lock"]:
            if state["focus"]["running"]:
                raise HTTPException(409, "already checking focus")
            root_at_start = state["root"]
            total = focus_mod.status(root_at_start)["unchecked"]
            state["focus"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def prog(d):
            state["focus"].update(done=d.get("done", 0), total=d.get("total", total))

        t0 = time.time()

        def _run_focus():
            try:
                with awake.hold():
                    state["focus"]["counts"] = focus_mod.check_focus(root_at_start, progress=prog)
            except Exception as e:
                state["focus"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["focus"]["running"] = False
                c = state["focus"]["counts"] or {}
                usage.log("focus_run", checked=c.get("checked", 0), bad=c.get("bad", 0), soft=c.get("soft", 0),
                          planned=total, seconds=round(time.time() - t0, 1), error=state["focus"]["error"])

        threading.Thread(target=_run_focus, daemon=True).start()
        return {"started": True, "total": total}

    @app.get("/api/focus/progress")
    def focus_progress():
        """{running, done, total, counts, error, awake}; counts is {ok, soft, bad, checked} once the pass ended."""
        return dict(state["focus"], awake=awake.held())

    @app.get("/api/focus/status")
    def focus_status():
        """{checked, unchecked, bad, soft} over the shoot, for "412 of 955 checked" next to the filter."""
        if state["root"] is None:
            return {"checked": 0, "unchecked": 0, "bad": 0, "soft": 0}
        return focus_mod.status(state["root"])

    EXPORT_HEADROOM = 1 << 30   # keep 1 GiB free on the destination disk after a copy

    def _is_default_base(base: Path) -> bool:
        return Path(base).resolve() == export_root().resolve()

    def _resolve_base() -> Path:
        """The folder exports go under right now. The default is created on demand; a chosen
        destination (another disk) must already be there or the export is refused."""
        base = state["export_base"]
        if base is None:
            base = export_root(); base.mkdir(parents=True, exist_ok=True)
            return base
        if not base.is_dir():
            raise HTTPException(400, "export destination is not mounted; plug that disk in or reset the destination")
        return base

    def _check_free(need: int, base: Path, hint: str = "Use links, or export fewer photos.") -> None:
        """400 when a copy of `need` bytes would leave less than EXPORT_HEADROOM on the disk holding base."""
        free = shutil.disk_usage(base).free
        where = "on this Mac" if _is_default_base(base) else "on that disk"
        if need + EXPORT_HEADROOM > free:
            raise HTTPException(400, f"copy needs {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free {where}. {hint}")

    def _destination_info() -> dict:
        base = state["export_base"]
        default = base is None
        if default:
            base = export_root(); base.mkdir(parents=True, exist_ok=True)
        mounted = base.is_dir()
        free_gb = round(shutil.disk_usage(base).free / 1e9, 1) if mounted else 0.0
        return {"path": str(base), "default": default, "mounted": mounted, "free_gb": free_gb}

    def _set_destination(p: Path) -> dict:
        p = p.expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {p}")
        p = p.resolve()
        root = state["root"]
        if root is not None:
            r = root.resolve()
            if p == r or p.is_relative_to(r):
                raise HTTPException(400, "destination is inside the source folder")
            if r.is_relative_to(p):
                raise HTTPException(400, "destination contains the source folder; pick a folder that is not above it")
        with state["export_lock"]:
            if state["export"]["running"]:
                raise HTTPException(409, "cannot change the destination while an export is running")
            state["export_base"] = p
        settings.set_export_base(p)
        return _destination_info()

    @app.get("/api/export/destination")
    def get_export_destination():
        return _destination_info()

    @app.post("/api/export/destination")
    def set_export_destination(req: DestinationReq):
        return _set_destination(Path(req.path))

    @app.post("/api/export/destination/choose")
    def choose_export_destination():
        try:
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Pick where exports go")'],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "folder picker timed out")
        except FileNotFoundError:
            raise HTTPException(501, "folder picker unavailable (osascript not found)")
        path_str = result.stdout.strip()
        if result.returncode != 0 or not path_str:
            return Response(status_code=204)
        return _set_destination(Path(path_str))

    @app.delete("/api/export/destination")
    def reset_export_destination():
        with state["export_lock"]:
            if state["export"]["running"]:
                raise HTTPException(409, "cannot change the destination while an export is running")
            state["export_base"] = None
        settings.set_export_base(None)
        return _destination_info()

    def _export_started(what: str, dest: str, files: int, **extra) -> float:
        """Log export_start and hand back the clock; _export_finished logs export_done from state["export"]."""
        usage.log("export_start", what=what, dest=dest, files=files, **extra)
        return time.time()

    def _export_finished(what: str, dest: str, t0: float, nbytes: int | None = None) -> None:
        ex = state["export"]
        usage.log("export_done", what=what, dest=dest, files=ex.get("done", 0), failed=ex.get("failed", 0),
                  skipped=ex.get("skipped", 0), bytes=ex.get("bytes") if ex.get("bytes") is not None else nbytes,
                  seconds=round(time.time() - t0, 1), error=ex.get("error"))

    @app.post("/api/export")
    def export(req: ExportReq):
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            base = _resolve_base()
            nbytes = None
            if req.mode == "copy":
                nbytes = export_bytes(root_at_start, req.ids)
                _check_free(nbytes, base)
            try:
                from .export import export_dir
                export_dir(root_at_start, req.name, base)   # validate now so a bad name or base is a 400, not a background error
            except ValueError as e:
                raise HTTPException(400, str(e))
            state["export"] = {"running": True, "done": 0, "total": len(req.ids), "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("selection", "local", len(req.ids), mode=req.mode)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(export_ids(root_at_start, req.ids, req.name, req.mode, progress=prog, base=base))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("selection", "local", t0, nbytes)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": len(req.ids)}

    @app.get("/api/export/progress")
    def export_progress():
        return dict(state["export"], awake=awake.held())

    @app.post("/api/export/categories")
    def export_categories_api(req: CategoriesExportReq):
        """One folder per ticked category under <destination>/<shoot>/categories/. Same job
        machinery as /api/export: one export at a time, preflight for copies, progress polled
        from /api/export/progress. total in the reply counts photos; progress counts RAW siblings too."""
        from .export import export_dir, export_categories, category_rows, cluster_rows, categories_bytes, aerial_rows
        if req.mode not in ("copy", "symlink"):
            raise HTTPException(400, "mode must be copy or symlink")
        if req.categories is not None and not req.categories and not req.discovered and not req.drone:
            raise HTTPException(400, "tick at least one category")
        if req.videos not in ("clips", "segments"):
            raise HTTPException(400, "videos must be clips or segments")
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            base = _resolve_base()
            n_photos = (len(category_rows(root_at_start, req.categories, req.include_unsure, req.hide_bad))
                        + len(cluster_rows(root_at_start, req.discovered, req.include_unsure, req.hide_bad))
                        + (len(aerial_rows(root_at_start, req.hide_bad)) if req.drone else 0))
            # Trimmed segments are always written, so the preflight runs for them even in link mode.
            if req.mode == "copy" or req.videos == "segments":
                _check_free(categories_bytes(root_at_start, req.categories, req.include_raw, discovered=req.discovered,
                                             include_unsure=req.include_unsure, videos=req.videos, drone=req.drone,
                                             hide_bad=req.hide_bad), base)
            try:
                export_dir(root_at_start, "categories", base)
            except ValueError as e:
                raise HTTPException(400, str(e))
            state["export"] = {"running": True, "done": 0, "total": n_photos, "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("categories", "local", n_photos, mode=req.mode, categories=req.categories, discovered=len(req.discovered or []),
                             include_raw=req.include_raw, include_unsure=req.include_unsure, videos=req.videos, drone=req.drone, hide_bad=req.hide_bad)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(export_categories(root_at_start, req.categories, req.mode, req.include_raw,
                                                                    base=base, progress=prog, discovered=req.discovered,
                                                                    include_unsure=req.include_unsure, videos=req.videos,
                                                                    drone=req.drone, hide_bad=req.hide_bad))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("categories", "local", t0)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n_photos}

    @app.post("/api/export/references")
    def export_references_api(req: ReferencesExportReq):
        """One folder per saved (or listed) person under <destination>/<shoot>/people/, matched at
        min_sim. Same job machinery as /api/export/categories. total in the reply counts photo
        placements (a frame with two people counts twice); progress counts RAW siblings too."""
        from .export import export_dir
        from .people import export_references, export_references_ids, references_bytes
        if req.mode not in ("copy", "symlink"):
            raise HTTPException(400, "mode must be copy or symlink")
        if req.names is not None and not req.names:
            raise HTTPException(400, "tick at least one person")
        min_sim = _min_sim(req.min_sim)
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            base = _resolve_base()
            folders = export_references_ids(root_at_start, req.names, min_sim)
            n_photos = sum(len(ids) for ids in folders.values())
            if n_photos == 0:
                raise HTTPException(400, "no saved person matches any photo at this match level")
            if req.mode == "copy":
                _check_free(references_bytes(root_at_start, req.names, req.include_raw, min_sim), base)
            try:
                export_dir(root_at_start, "people", base)
            except ValueError as e:
                raise HTTPException(400, str(e))
            state["export"] = {"running": True, "done": 0, "total": n_photos, "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("people", "local", n_photos, mode=req.mode, people=len(folders), include_raw=req.include_raw, min_sim=min_sim)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(export_references(root_at_start, req.names, req.mode, req.include_raw,
                                                                    base=base, progress=prog, min_sim=min_sim))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("people", "local", t0)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n_photos}

    # Google Drive as a destination (photosort/drive.py): paste a folder link, the app uploads the
    # same three exports (ticked categories, saved people, a selection) into it. The upload rides the
    # export job machinery with what == "drive": one job at a time, progress from /api/export/progress,
    # path is the folder's web link when done, failures the per-file list. Sign-in opens the browser
    # from a thread; status says when it is through.
    from . import drive as drive_mod

    state["drive"] = {"signing_in": False, "error": None}

    def _drive_status() -> dict:
        prefs = settings.get_drive_prefs()
        return {"configured": drive_mod.client_path().is_file(), "signed_in": drive_mod.is_signed_in(),
                "email": drive_mod.signed_in_email(), "client_path": str(drive_mod.client_path()),
                "signing_in": state["drive"]["signing_in"], "error": state["drive"]["error"],
                "link": prefs["link"], "web_size": prefs["web_size"]}

    def _drive_folder(link: str) -> str:
        try:
            return drive_mod.parse_folder_link(link)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _drive_call(fn, *a, **kw):
        """Run one drive call, mapping its plain errors to HTTP: 401 not signed in, 400 no client
        file or a refused folder."""
        try:
            return fn(*a, **kw)
        except drive_mod.NotSignedIn as e:
            raise HTTPException(401, str(e))
        except (drive_mod.NoClientConfig, ValueError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/drive/status")
    def drive_status():
        return _drive_status()

    @app.post("/api/drive/signin")
    def drive_signin():
        """Start the browser sign-in in a thread and return at once; poll /api/drive/status for
        signing_in to drop and signed_in (or error) to say how it went. 409 while one is open."""
        if state["drive"]["signing_in"]:
            raise HTTPException(409, "a sign-in is already open in the browser")
        state["drive"] = {"signing_in": True, "error": None}

        def _run():
            try:
                drive_mod.sign_in()
            except Exception as e:
                state["drive"]["error"] = str(e) if isinstance(e, (drive_mod.NoClientConfig, ValueError)) else f"{type(e).__name__}: {e}"
            finally:
                state["drive"]["signing_in"] = False
                usage.log("drive_signin", ok=state["drive"]["error"] is None, error=state["drive"]["error"])

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True}

    @app.post("/api/drive/signout")
    def drive_signout():
        drive_mod.sign_out()
        return {"signed_in": False}

    @app.post("/api/drive/inspect")
    def drive_inspect(req: DriveLinkReq):
        """The folder behind a pasted link: name, owner, whether our quota applies and how much is free."""
        folder_id = _drive_folder(req.link)
        info = _drive_call(drive_mod.inspect_folder, folder_id)
        return dict(info, link=drive_mod.folder_link(folder_id))

    @app.post("/api/drive/export")
    def drive_export(req: DriveExportReq):
        """Upload one of the three exports into the Drive folder. The job list comes from the same
        planners the local exports use (category_jobs, folder_jobs, ids_jobs), prefixed categories/,
        people/ or <name>/ under the folder. Reply total counts files (RAW siblings included)."""
        from .export import category_jobs, folder_jobs, ids_jobs, jobs_bytes, safe_segment
        from .people import export_references_ids
        folder_id = _drive_folder(req.link)
        if req.what not in ("categories", "people", "selection"):
            raise HTTPException(400, "what must be categories, people or selection")
        if req.what == "categories":
            if req.categories is not None and not req.categories and not req.discovered and not req.drone:
                raise HTTPException(400, "tick at least one category")
            if req.videos == "segments":
                raise HTTPException(400, "trimmed segments cannot go to Drive yet; export whole clips, or segments to a folder")
            if req.videos != "clips":
                raise HTTPException(400, "videos must be clips or segments")
        if req.what == "people" and req.names is not None and not req.names:
            raise HTTPException(400, "tick at least one person")
        if req.what == "selection" and not req.ids:
            raise HTTPException(400, "nothing selected")
        if req.web_size is not None and req.web_size < 100:
            raise HTTPException(400, "web size must be at least 100 px")
        if not drive_mod.is_signed_in():       # the token carries the client id too; the client file is only for sign-in
            raise HTTPException(401, str(drive_mod.NotSignedIn()))
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            if req.what == "categories":
                planned, _segs = category_jobs(root_at_start, req.categories, req.include_raw, req.discovered,
                                               req.include_unsure, "clips", req.drone, req.hide_bad)
                jobs = [(pid, rel, "categories/" + sub) for pid, rel, sub in planned]
            elif req.what == "people":
                folders = export_references_ids(root_at_start, req.names, _min_sim(req.min_sim))
                if not any(folders.values()):
                    raise HTTPException(400, "no saved person matches any photo at this match level")
                jobs = [(pid, rel, "people/" + sub) for pid, rel, sub in folder_jobs(root_at_start, folders, req.include_raw)]
            else:
                try:
                    top = safe_segment(req.name or "selection")
                except ValueError as e:
                    raise HTTPException(400, str(e))
                jobs = [(pid, rel, top) for pid, rel, _ in ids_jobs(root_at_start, req.ids, req.include_raw)]
            msg = _drive_call(drive_mod.preflight, folder_id, jobs_bytes(root_at_start, jobs))
            if msg:
                raise HTTPException(400, msg)
            settings.set_drive_prefs(req.link, req.web_size)
            state["export"] = {"running": True, "done": 0, "total": len(jobs), "failed": 0, "skipped": 0, "path": None,
                               "error": None, "what": "drive", "bytes": 0, "current": None, "failures": []}
        t0 = _export_started(req.what, "drive", len(jobs), web_size=req.web_size, skip_videos=req.skip_videos, include_raw=req.include_raw)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    res = drive_mod.upload_files(root_at_start, folder_id, jobs, web_size=req.web_size,
                                                 skip_videos=req.skip_videos, progress=prog)
                state["export"]["failures"] = res["failures"]
                state["export"]["path"] = drive_mod.folder_link(folder_id)
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, (ValueError, drive_mod.NotSignedIn)) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished(req.what, "drive", t0)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": len(jobs)}

    # Reorganise disk: the one guarded exception to the read-only shoot root (photosort/reorganise.py).
    # Plan first (every guard, the full move list cached under a plan id), then apply with the folder's
    # name typed back as confirmation. Apply and undo ride the export job machinery: one at a time,
    # progress from /api/export/progress, "what" says which job it is.
    from . import reorganise as reorganise_mod

    def _reorganise_gates(root_at_start) -> None:
        if root_at_start is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot reorganise while scanning")
        if state["classify"]["running"]:
            raise HTTPException(409, "cannot reorganise while categorising")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")

    def _reorganise_job(root_at_start: Path, what: str, total: int, work) -> dict:
        """Start `work(progress)` as the export job named `what`; on success the search index is refreshed
        and state["export"]["path"] is the sorted/ folder."""
        state["export"] = {"running": True, "done": 0, "total": total, "failed": 0, "skipped": 0, "path": None,
                           "error": None, "what": what}
        t0 = time.time()

        def prog(d):
            state["export"].update(d)

        def _run():
            try:
                with awake.hold():
                    work(prog)
                state["export"]["path"] = str(root_at_start / reorganise_mod.SORTED_DIR)
                if state["root"] == root_at_start:
                    state["index"].refresh(); state["stale"] = False
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                ex = state["export"]
                usage.log("reorganise_" + ("apply" if what == "reorganise" else "undo"), moves=total, done=ex.get("done", 0),
                          failed=ex.get("failed", 0), seconds=round(time.time() - t0, 1), error=ex.get("error"))

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True, "total": total, "what": what}

    @app.post("/api/reorganise/plan")
    def reorganise_plan(req: ReorganisePlanReq):
        with state["export_lock"]:
            root_at_start = state["root"]
            _reorganise_gates(root_at_start)
            try:
                plan = reorganise_mod.plan(root_at_start, by_people=req.by_people)
            except ValueError as e:
                usage.log("reorganise_plan", by_people=req.by_people, refused=str(e))
                raise HTTPException(400, str(e))
            usage.log("reorganise_plan", by_people=req.by_people, moves=plan.get("moves"), folders=plan.get("folders"),
                      collisions=plan.get("collisions"))
            return plan

    @app.post("/api/reorganise/apply")
    def reorganise_apply(req: ReorganiseApplyReq):
        with state["export_lock"]:
            root_at_start = state["root"]
            _reorganise_gates(root_at_start)
            if req.confirm != root_at_start.name:
                raise HTTPException(400, f"type the folder name ({root_at_start.name}) to confirm")
            cached = reorganise_mod._PLANS.get(req.plan_id)
            if cached is None or cached["root"] != str(root_at_start):
                raise HTTPException(400, "run the plan first")
            total = len(cached["moves"])
            return _reorganise_job(root_at_start, "reorganise", total,
                                   lambda prog: reorganise_mod.apply(root_at_start, req.plan_id, progress=prog))

    @app.post("/api/reorganise/undo")
    def reorganise_undo():
        with state["export_lock"]:
            root_at_start = state["root"]
            _reorganise_gates(root_at_start)
            st = reorganise_mod.status(root_at_start)
            if not st["reorganised"]:
                raise HTTPException(400, "nothing to undo")
            return _reorganise_job(root_at_start, "undo", st["moves"],
                                   lambda prog: reorganise_mod.undo(root_at_start, progress=prog))

    @app.get("/api/reorganise/status")
    def reorganise_status():
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        return reorganise_mod.status(state["root"])

    # Index bundles: the whole index (db with saved people, thumbs, grid) as one zip on the export
    # destination, and the reverse: install such a zip here and open the shoot without re-indexing.

    def _start_bundle_export(dest: Path | None) -> dict:
        """Pack the open shoot's index into <dest>/<shoot>.photosort-index.zip in the background.
        dest None means the export destination. A dest that is the shoot root or inside it is refused
        (bundle_path raises), as is one that is not a folder."""
        from . import bundle
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["running"]:
                raise HTTPException(409, "cannot save the scan file while scanning")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            if dest is None:
                base = _resolve_base()
            else:
                base = dest.expanduser()
                if not base.is_dir():
                    raise HTTPException(400, f"not a directory: {dest}")
                base = base.resolve()
            try:
                bundle.bundle_path(root_at_start, base)
            except ValueError as e:
                raise HTTPException(400, str(e))
            n_files = len(bundle.bundle_files(root_at_start))
            nbytes = bundle.bundle_bytes(root_at_start)
            _check_free(nbytes, base, hint="Free some space there first.")
            state["export"] = {"running": True, "done": 0, "total": n_files, "failed": 0, "skipped": 0, "path": None, "error": None, "what": "bundle"}
        t0 = _export_started("bundle", "local", n_files)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(bundle.export_bundle(root_at_start, base, progress=prog))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("bundle", "local", t0, nbytes)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n_files, "dest": str(base)}

    @app.post("/api/bundle/export")
    def export_bundle_api(req: BundleExportReq | None = None):
        return _start_bundle_export(Path(req.dest) if req and req.dest else None)

    @app.post("/api/bundle/export/choose")
    def export_bundle_choose():
        """The native folder picker, opening on ~/Desktop/photosort-out (created if need be, and never a
        chosen export disk, which may be the one that is unplugged), then the export into the pick."""
        # Same gates as the export, checked up front so nobody sits through the picker for an error.
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot save the scan file while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")
        start = export_root(); start.mkdir(parents=True, exist_ok=True)
        quoted = str(start).replace("\\", "\\\\").replace('"', '\\"')
        path_str = _run_picker(f'POSIX path of (choose folder with prompt "Where should the scan file go?" default location (POSIX file "{quoted}"))', "folder")
        if path_str is None:
            return Response(status_code=204)
        return _start_bundle_export(Path(path_str))

    @app.post("/api/reveal")
    def reveal(req: RevealReq):
        """Show a file or folder the app wrote in the Finder (open -R). Only for paths that exist."""
        p = Path(req.path).expanduser()
        if not p.exists():
            raise HTTPException(400, f"not there: {req.path}")
        try:
            subprocess.run(["open", "-R", str(p)], capture_output=True, text=True, timeout=20, check=False)
        except FileNotFoundError:
            raise HTTPException(501, "Finder is not available here")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "the Finder did not answer")
        return {"revealed": str(p)}

    def _import_gates() -> None:
        # Same gates as _switch_root, checked up front so nobody sits through a picker for a 409.
        if state["running"]:
            raise HTTPException(409, "cannot load a scan file while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot load a scan file while an export is running")

    def _run_picker(script: str, what: str) -> str | None:
        """POSIX path from a macOS picker, or None when the user cancelled."""
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, f"{what} picker timed out")
        except FileNotFoundError:
            raise HTTPException(501, f"{what} picker unavailable (osascript not found)")
        path_str = result.stdout.strip()
        if result.returncode != 0 or not path_str:
            return None
        return path_str

    def _inspect_zip(zip_path: Path) -> dict:
        from .bundle import inspect_bundle
        try:
            return inspect_bundle(zip_path)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _import_and_switch(zip_path: Path, root: Path, info: dict) -> dict:
        """Install the bundle for root, switch to it, and return the folder payload plus what was imported."""
        from .bundle import import_bundle
        _import_gates()
        try:
            import_bundle(zip_path, root)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except sqlite3.Error:
            # index.db is in the zip but is not a SQLite file; import_bundle already removed its temp dir.
            raise HTTPException(400, "that scan file is not readable")
        out = _switch_root(root)
        # scan: from the installed index itself (an older bundle has no "scan" in its bundle.json), so the
        # UI can say "2,080 of 4,315 scanned, continue?" instead of quietly showing fewer items.
        scan = _scan_block(db.connect(root))
        usage.log("bundle_import", photos=info.get("photos"), complete=scan["complete"])
        return dict(out, imported=True, root=str(root), photos=info.get("photos"), scan=scan)

    @app.post("/api/bundle/import/choose")
    def import_bundle_choose():
        _import_gates()
        path_str = _run_picker('POSIX path of (choose file with prompt "Pick a scan file (.photosort-index.zip)" of type {"public.zip-archive"})', "bundle")
        if path_str is None:
            return Response(status_code=204)
        zip_path = Path(path_str)
        info = _inspect_zip(zip_path)
        root = Path(info["root"])
        if not root.is_dir():
            # Made on a Mac where the disk sat elsewhere: the UI asks for the folder, then calls choose-root.
            return {"needs_root": True, "bundle": info, "zip": str(zip_path)}
        return _import_and_switch(zip_path, root.resolve(), info)

    @app.post("/api/bundle/import")
    def import_bundle_api(req: BundleImportReq):
        _import_gates()
        zip_path = Path(req.zip).expanduser()
        info = _inspect_zip(zip_path)
        root = Path(req.root).expanduser() if req.root else Path(info["root"])
        if not root.is_dir():
            if req.root:
                raise HTTPException(400, f"not a directory: {req.root}")
            raise HTTPException(400, f"that bundle was made for {info['root']}, which is not here; pick the photo folder")
        return _import_and_switch(zip_path, root.resolve(), info)

    @app.post("/api/bundle/import/choose-root")
    def import_bundle_choose_root(req: BundleZipReq):
        _import_gates()
        zip_path = Path(req.zip).expanduser()
        info = _inspect_zip(zip_path)
        path_str = _run_picker('POSIX path of (choose folder with prompt "Pick the photo folder this scan was made for")', "folder")
        if path_str is None:
            return Response(status_code=204)
        root = Path(path_str)
        if not root.is_dir():
            raise HTTPException(400, f"not a directory: {path_str}")
        return _import_and_switch(zip_path, root.resolve(), info)

    # People/groups/solo export stays synchronous in this pass, it is the small-shoot
    # bundle, not the main Diu-scale export path that /api/export now backgrounds.
    @app.post("/api/export/people")
    def export_people_api(req: ModeReq):
        root_at_start = state["root"]
        if root_at_start is None:
            raise HTTPException(400, "no folder open")
        from .people import export_people, export_people_ids
        with state["export_lock"]:
            base = _resolve_base()
        if req.mode == "copy":
            # One photo lands in several folders (each person, plus groups or solo) and each is a
            # separate copy, so size every folder, not the distinct set of photos.
            _check_free(sum(export_bytes(root_at_start, ids) for ids in export_people_ids(root_at_start).values()), base)
        t0 = time.time()
        try:
            out = {"path": str(export_people(root_at_start, req.mode, base=base))}
        except ValueError as e:
            usage.log("export_done", what="people_groups", dest="local", seconds=round(time.time() - t0, 1), error=str(e))
            raise HTTPException(400, str(e))
        usage.log("export_done", what="people_groups", dest="local", seconds=round(time.time() - t0, 1), error=None)
        return out

    # ===== usage log endpoints: the UI posts its own events here (batched), the Feedback section reads the
    # summary and writes the report zip. Nothing leaves the Mac; see docs/usage-log.md.
    UI_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

    def _ui_event(d) -> dict | None:
        if not isinstance(d, dict) or not UI_EVENT_RE.match(str(d.get("ev", ""))):
            return None
        fields = {}
        for k, v in list(d.items())[:20]:
            if k == "ev" or not isinstance(k, str) or len(k) > 40:
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                fields[k] = v
        return {"ev": "ui_" + d["ev"] if not d["ev"].startswith("ui_") else d["ev"], **fields}

    @app.post("/api/usage")
    async def usage_post(request: Request):
        """{ev, ...fields}, a list of them, or {events: [...]}; up to 200 per call. Reply says how many were kept."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected JSON")
        items = body.get("events") if isinstance(body, dict) and "events" in body else body
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise HTTPException(400, "expected an event or a list of events")
        kept = 0
        for d in items[:200]:
            ev = _ui_event(d)
            if ev is None:
                continue
            usage.log(ev.pop("ev"), src="ui", **ev)
            kept += 1
        return {"logged": kept}

    @app.get("/api/usage/summary")
    def usage_summary():
        s = usage.summary()
        return dict(s, text=usage.summary_text(s))

    @app.post("/api/usage/report")
    def usage_report():
        try:
            p = usage.write_report()
        except OSError as e:
            raise HTTPException(500, f"could not write the report: {e}")
        usage.log("report_saved", bytes=p.stat().st_size)
        return {"path": str(p), "bytes": p.stat().st_size}
    # ===== end usage log endpoints =====

    return app
