from __future__ import annotations
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import db
from . import usage
from . import awake
from . import classify as classify_mod
from . import focus as focus_mod
from . import index
from . import places as places_mod
from . import pile
from . import settings
from . import version as version_mod
from .config import DB_NAME, ANGLE_WINDOW_S, STD_EXTS, app_home, export_root, shoot_slug
from .classify import SHOT_SIZE_NAMES
from .search import Index, Filters, SORTS, angles, LOOKS, DURATION_KEYS, GROUP_KINDS, CAMERA_AXES
from .config import CAMERA_BUCKETS
from . import products as products_mod
from . import cull as cull_mod                      # ===== client pick =====
from .export import export_ids, export_bytes, ids_jobs, jobs_bytes

UI = Path(__file__).parent / "ui"
RECENT_FILE = "recent.json"
RECENT_MAX = 8
# The biggest reference image /api/search/upload will hold in memory. A 61 MP A7R V frame is about 25 MB
# as a full quality JPEG and a phone screenshot is under 5, so this takes anything a person would drag in
# and refuses anything they would not have meant to.
UPLOAD_MAX_BYTES = 40 * 1024 * 1024


class ExportReq(BaseModel):
    ids: list[int]
    name: str
    mode: str = "copy"
    web_size: int | None = None             # copies only: photos re-encoded to this long edge; RAW and clips as they are
    include_raw: bool = False               # the RAW sibling next to each JPEG


class ExportPrefsReq(BaseModel):
    preset: str = "custom"                  # web | links | full | custom
    mode: str = "copy"
    web_size: int | None = None
    include_raw: bool = False


class NameReq(BaseModel):
    name: str

# ===== shoot modes =====
class ProductsReq(BaseModel):
    ids: list[int] | None = None           # the photos on screen; None for every still in the shoot

class ProductsExportReq(BaseModel):
    mode: str = "copy"
    include_raw: bool = False
    only: list[list[int]] | None = None    # [[product, variant], ...]; None for every variant
# ===== end shoot modes =====

# ===== client pick =====
class CullReq(BaseModel):
    per_product: int | None = None         # picks a product; or
    total: int | None = None               # picks across the shoot, split by how many colours each product has

class CullIdReq(BaseModel):
    id: int
    on: bool = True

class CullMergeReq(BaseModel):
    product: int
    into: int

class CullExportReq(BaseModel):
    mode: str = "copy"
    include_raw: bool = False
# ===== end client pick =====


class SavedReq(BaseModel):
    saved: bool = True


class PileReq(BaseModel):
    add: list[int] = []
    remove: list[int] = []


class PileRestoreReq(BaseModel):
    rows: list[dict[str, Any]]                  # what DELETE /api/pile answered with as cleared


class UpdateCheckReq(BaseModel):
    enabled: bool


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
    products: bool = False        # Find products once the scan is done; left off, the Products tab does it later
    retry_errors: bool = False
    resume: bool = False          # Continue scan: faces as the interrupted scan had it (meta scan_faces), not the box


class IndexAtReq(BaseModel):
    path: str                     # the folder the project file goes in, when the shoot folder cannot hold it
    faces: bool = True
    products: bool = False
    retry_errors: bool = False
    resume: bool = False


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
    dest: str | None = None                 # folder the project file goes in; None = the export destination


class RevealReq(BaseModel):
    path: str


class RevealIdsReq(BaseModel):
    ids: list[int]


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


# ===== marks: typed loosely on purpose, so a bad value is db.set_marks' 400 with words, not a 422 =====
class MarksReq(BaseModel):
    ids: list[int]
    flag: Any = None                        # "pick" | "reject" | null; a field left out is left alone
    rating: Any = None                      # 0 to 5
    label: Any = None                       # red | yellow | green | blue | purple | null


class XmpReq(BaseModel):
    ids: list[int] | None = None            # None: every marked item in the shoot
# ===== end marks =====


def _load_recent() -> list[str]:
    p = app_home() / RECENT_FILE
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def _recent_facts(path_str: str) -> dict:
    """What the shoots menu can say about a recent shoot without opening it: how many photos and clips
    its index holds, and the project file it was last saved to or opened from. Read straight out of that
    shoot's own index.db, never through db.connect or db.index_dir: those build the schema and the thumb
    folders, and a listing must not create an index for a folder that has none. Anything unreadable (an
    older db, a locked one, none at all) comes back as zeros, so one odd shoot cannot break the list."""
    out = {"photos": 0, "videos": 0, "items": 0, "project": None, "indexed": False}
    dbf = app_home() / shoot_slug(Path(path_str)) / DB_NAME
    if not dbf.is_file():
        return out
    conn = None
    try:
        conn = sqlite3.connect(dbf, timeout=1)
        r = conn.execute("""SELECT SUM(status='ok' AND COALESCE(kind,'photo') != 'video'),
                                   SUM(status='ok' AND kind='video') FROM photos""").fetchone()
        out["photos"] = int(r[0] or 0)
        out["videos"] = int(r[1] or 0)
        out["items"] = out["photos"] + out["videos"]
        out["indexed"] = out["items"] > 0
        row = conn.execute("SELECT value FROM meta WHERE key='project_path'").fetchone()
        if row and row[0] and Path(row[0]).is_file():
            out["project"] = str(row[0])
    except (sqlite3.Error, OSError, ValueError):
        pass
    finally:
        if conn is not None:
            try: conn.close()
            except sqlite3.Error: pass
    return out


def _save_recent(path_str: str) -> None:
    """Written through a temp file and renamed into place: two windows opening at the same moment used to
    be able to catch each other mid-write, read nothing, and leave the list with one shoot in it."""
    p = app_home() / RECENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    recent = [r for r in _load_recent() if r != path_str]
    recent.insert(0, path_str)
    tmp = p.with_name(p.name + f".{os.getpid()}.part")
    try:
        tmp.write_text(json.dumps(recent[:RECENT_MAX]))
        os.replace(tmp, p)
    except OSError:
        tmp.unlink(missing_ok=True)


def create_app(root: Path | None = None, open_file: Path | None = None) -> FastAPI:
    """open_file: a project file a double-click (or "serve --project") handed us; the UI collects it once
    from GET /api/launch and runs the same load as the Open project button."""
    root = Path(root) if root is not None else None
    app = FastAPI(title="photosort")
    state = {
        "root": root,
        "open_file": str(open_file) if open_file else None,
        "index": Index(root) if root is not None else None,
        "progress": {"stage": "idle", "done": 0, "total": 0},
        "running": False,
        "stale": False,
        "classify": {"running": False, "counts": {}, "discovered": {}, "error": None},
        "focus": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "places": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "fps": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "clipfaces": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "flicker": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "camera": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},     # ===== shoot modes =====
        "products": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},
        "cull": {"running": False, "done": 0, "total": 0, "counts": {}, "error": None},       # ===== client pick =====
        "export": {"running": False, "done": 0, "total": 0, "failed": 0, "skipped": 0, "path": None, "error": None},
        "export_req": None,        # {route, body, root} of the running export, for the export history row at the end
        # Where exports land instead of export_root() (another disk), or None for the default.
        "export_base": settings.get_export_base(),
        # Held from the "already running" check through setting running=True, and around a folder
        # switch, so two rapid export POSTs (or a switch during the preflight) cannot both pass.
        "export_lock": threading.Lock(),
        "focus_lock": threading.Lock(),
        "places_lock": threading.Lock(),
        "fps_lock": threading.Lock(),
        "clipfaces_lock": threading.Lock(),
        "camera_lock": threading.Lock(),
        "products_lock": threading.Lock(),
        "cull_lock": threading.Lock(),                                                          # ===== client pick =====
    }
    app.state.photosort = state

    # ===== usage log (photosort/usage.py): one session per server start; every 4xx/5xx and every uncaught
    # exception is an event, logged against the route template so a person's name in a URL never lands in it.
    usage.start_session()
    usage.log("server_start", **usage.system_info())
    version_mod.check_in_background()          # ===== version: the daily check, only when the tester turned it on =====

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
                      stale_embeds=0, embed_model=None, faces=True, interrupted=None)

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
        _save_recent(str(root))      # the folder the app was started with belongs in the shoots menu too,
        _log_folder_open()           # or switching away from it loses the only way back

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

    from . import live
    live.install(app, _switch_root)

    def _auto_classify(root_at_start: Path, stats: dict) -> None:
        """Categorise at the end of an index run that changed something. A classify failure is
        recorded on state["classify"] and never marks the index run itself as failed."""
        if stats.get("indexed", 0) == 0 and stats.get("embedded", 0) == 0 and stats.get("faced", 0) == 0 \
                and stats.get("clip_faced", 0) == 0:
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

    def _auto_products(root_at_start: Path, stats: dict) -> None:
        """Find products at the end of a scan that asked for it. Like _auto_classify, a failure here never fails the
        scan: it is recorded on state["products"], and the Products tab can run it again."""
        if not state.get("scan_products"):
            return
        total = stats.get("total", 0)
        state["progress"] = {"stage": "products", "done": 0, "total": total, "stage_started": time.time()}
        def prog(done, n):
            state["progress"] = dict(state["progress"], done=done, total=n)
        try:
            state["products"]["counts"] = products_mod.find_products(root_at_start, progress=prog)
        except Exception as e:
            state["products"]["error"] = f"{type(e).__name__}: {e}"
        finally:
            state["progress"] = {"stage": "done", "done": total, "total": total, "stage_started": time.time()}

    def _run(root_at_start: Path, faces: bool, retry_errors: bool):
        from .index import index_folder
        def prog(d):
            state["progress"] = d
        from .index import SourceUnavailable
        t0 = time.time(); stats = {}; err = None; saved = None
        try:
            with awake.hold():
                stats = index_folder(root_at_start, faces=faces, progress=prog, retry_errors=retry_errors)
                _auto_classify(root_at_start, stats)
                _auto_products(root_at_start, stats)
                saved = _autosave_project(root_at_start, stats.get("total", 0))
        except SourceUnavailable as e:
            # The disk went away: not a failure, a pause. What was scanned is kept; Continue does the rest
            # once the disk is back (the UI reads mounted from /api/stats).
            err = str(e)
            last = state["progress"] if isinstance(state["progress"], dict) else {}
            # Keep what the last emit carried (which step, which folder) and change only what the pause
            # changes: where the pass stopped is the useful thing to show, and a fresh stage_started so
            # the panel times the pause rather than the stage that was interrupted.
            state["progress"] = dict(last, stage="paused", error=err, stage_started=time.time(),
                                     done=last.get("done", 0), total=last.get("total", 0))
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
                      faced=stats.get("faced", 0), failures=stats.get("errors", 0), seconds=round(time.time() - t0, 1), error=err,
                      project_saved=bool(saved and not saved["error"]), project_error=saved["error"] if saved else None)

    # ===== the project file follows the scan: written at the end of every scan that finished, where the
    # preflight said it would go (the shoot folder, or the place picked when that could not hold it), with
    # no click. A save that fails (the disk filled during a six-hour scan) is reported on the progress, never
    # raised: the index itself lives in app_home and is untouched, so nothing is lost, and the UI offers the
    # picker. The scan is still "running" while this writes, which keeps a manual Save and a folder switch
    # out of the way until the file is in place. =====
    def _autosave_project(root_at_start: Path, total: int) -> dict:
        from . import bundle
        dest = _scan_project_dir(root_at_start)
        target = bundle.bundle_path(root_at_start, dest)
        state["progress"] = {"stage": "save", "done": 0, "total": 0, "stage_started": time.time()}
        out = {"path": str(target), "dir": str(dest), "error": None}
        try:
            bundle.export_bundle(root_at_start, dest, progress=lambda d: state["progress"].update(d))
            _remember_project(root_at_start, target)
        except Exception as e:
            out["error"] = (e.strerror or str(e)) if isinstance(e, OSError) else f"{type(e).__name__}: {e}"
        state["progress"] = {"stage": "done", "done": total, "total": total, "stage_started": time.time(), "project": out}
        return out

    def _scan_project_dir(root: Path) -> Path:
        """Where a scan puts the project file: the folder it was last saved to or opened from, while that
        folder is there, else the shoot folder itself. Never ~/Documents quietly: when the shoot folder
        cannot hold the file the preflight stops and asks, so the person knows where their scan went."""
        try:
            conn = db.connect(root)
            try:
                remembered = db.get_meta(conn, "project_path")
            finally:
                conn.close()
        except sqlite3.Error:
            remembered = None
        if remembered and Path(remembered).parent.is_dir():
            return Path(remembered).parent
        return Path(root)

    def _size(b: int) -> str:
        """1.1 GB, or 111 MB under a GB: a card with 18 MB left should not read as "0.0 GB", nor a 111 MB
        project file as "0.1 GB"."""
        return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"

    def _project_preflight(root: Path, dest: Path, need_file: int | None = None) -> dict | None:
        """None when dest can take this shoot's project file, else {reason, message, dir, need_bytes,
        free_bytes, detail}. reason "readonly": a real file could not be created there (bundle.probe_writable,
        not os.access, which says yes on a read-only exFAT or a locked card). reason "space": the estimate for
        every photo and clip in the folder (listed by name and size, nothing decoded) plus the headroom kept
        beside it (bundle.project_headroom: a tenth of the file, 100 MB to 1 GiB) is more than the disk has."""
        from . import bundle
        from .walk import find_images
        where = _disk_name(dest)
        if not Path(dest).is_dir():
            # the shoot's disk is not plugged in (or the remembered folder went away): say that, not ENOENT
            return {"reason": "missing", "dir": str(dest), "need_bytes": None, "free_bytes": None, "detail": None,
                    "message": f"{where} is not connected, so the project file cannot be saved there."}
        why = bundle.probe_writable(dest)
        if why:
            return {"reason": "readonly", "dir": str(dest), "need_bytes": None, "free_bytes": None, "detail": why,
                    "message": f"{where} cannot be written to ({why}), so the project file cannot be saved there."}
        if need_file is None:
            try:
                files = find_images(root)
            except OSError:
                files = []
            videos = sum(1 for f in files if f.is_video)
            est = bundle.estimate_bytes(len(files) - videos, videos)
            what = f"The project file for {len(files):,} items"
        else:
            est = int(need_file)                   # a Save knows the real size: the thumbs on this Mac
            what = "The project file"
        keep = bundle.project_headroom(est)
        need = est + keep
        free = shutil.disk_usage(dest).free
        if need > free:
            return {"reason": "space", "dir": str(dest), "need_bytes": need, "free_bytes": free, "detail": None,
                    "message": f"{what} needs about {_size(need)} on {where} "
                               f"(with {_size(keep)} kept free) and it has {_size(free)}."}
        return None
    # ===== end the project file follows the scan =====

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

    @app.get("/favicon.ico")
    def favicon():
        # browsers request this at the root whether or not a page declares an icon; without a
        # route it 404s on every tab and usage.log records it as an api_error on no route at all
        return FileResponse(UI / "favicon-32.png", media_type="image/png")

    @app.get("/ui/{name}")
    def ui(name: str):
        p = UI / name
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

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
        """Every shoot the menu can offer, newest first, with what is cheap to know without opening it:
        photos and videos from its own index, the project file saved for it (or None), missing True when
        the folder is not on this Mac right now (an unplugged disk still lists, it just cannot open), and
        disk, what to plug back in. open marks the shoot that is open now; it heads the list even when it
        was never switched to (a folder the app was started with is not in recent.json)."""
        paths = _load_recent()
        here = str(state["root"]) if state["root"] is not None else None
        if here is not None and here not in paths:
            paths = [here] + paths
        out = []
        for path_str in paths:
            p = Path(path_str)
            row = {"path": path_str, "name": p.name or path_str, "disk": _disk_name(p),
                   "missing": not p.is_dir(), "open": path_str == here}
            row.update(_recent_facts(path_str))
            out.append(row)
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
        elif cut and cut["kind"] == "places":
            fix = "places"                    # and for a locations pass it is POST /api/places/read
        elif cut and cut["kind"] == "fps":
            fix = "fps"                       # and for a frame rate pass it is POST /api/fps/read
        elif cut and cut["kind"] == "clipfaces":
            fix = "clipfaces"                 # and for faces in clips it is POST /api/clipfaces/find
        elif new_on_disk:
            fix = "rescan"
        elif faces_pending:
            fix = "faces"
        else:
            fix = None
        return dict(scan, on_disk=on_disk, new_on_disk=new_on_disk, faces_pending=faces_pending, mounted=mounted,
                    running=state["running"], fix=fix)

    # Enough folders to say where the failures are without turning the summary into the list it replaces.
    FOLDER_SUMMARY_MAX = 20

    @app.get("/api/errors")
    def errors(only: str | None = None):
        """The files that could not be read, split into the ones this pass wrote down and the ones carried
        over from an earlier one. That is the distinction the panel needs: 1,932 rows from a disk that
        dropped out yesterday are not news and cannot be acted on until the pass ends, while one file
        failing right now is live evidence the disk is playing up again, and is the earliest warning there
        is. New is indexed_at at or after the scan job's started, both written by datetime('now').

        {errors: [{rel, indexed_at, new}], since, n_new, n_old, total, folders}. only=new or only=old
        filters the list; the counts and since always describe the whole set, so a panel polling for new
        rows alone can still say how many are carried over. folders is the returned set counted by
        directory, biggest first: "1,904 in .../FINAL JPG" says more than 1,904 filenames."""
        if only not in (None, "", "new", "old"):
            raise HTTPException(400, "only must be new or old")
        if state["root"] is None:
            return {"errors": [], "since": None, "n_new": 0, "n_old": 0, "total": 0, "folders": []}
        conn = db.connect(state["root"])
        since = (db.latest_jobs(conn).get("scan") or {}).get("started")
        rows = [{"rel": r[0], "indexed_at": r[1], "new": bool(since and r[1] and r[1] >= since)}
                for r in conn.execute("SELECT rel, indexed_at FROM photos WHERE status='error' ORDER BY rel")]
        n_new = sum(1 for r in rows if r["new"])
        out = [r for r in rows if only not in ("new", "old") or r["new"] == (only == "new")]
        counts: dict[str, int] = {}
        for r in out:
            folder = r["rel"].rsplit("/", 1)[0] if "/" in r["rel"] else ""      # "" is the root of the shoot
            counts[folder] = counts.get(folder, 0) + 1
        folders = sorted(({"folder": k, "n": v} for k, v in counts.items()), key=lambda f: (-f["n"], f["folder"]))
        return {"errors": out, "since": since, "n_new": n_new, "n_old": len(rows) - n_new,
                "total": len(rows), "folders": folders[:FOLDER_SUMMARY_MAX]}

    def _index_gates(req) -> bool:
        """Every reason a scan cannot start now, raised; else the faces choice this scan runs with."""
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
        if state["fps"]["running"]:
            raise HTTPException(409, "cannot scan while frame rates are being read")
        if state["clipfaces"]["running"]:
            raise HTTPException(409, "cannot scan while faces in clips are being found")
        faces = req.faces
        state["scan_products"] = req.products
        if req.resume:
            # Continue scan: the interrupted scan's own faces choice, so a scan started with faces off does
            # not grow a faces pass (and one started with faces on does not lose it) on the way back.
            faces = db.get_meta(db.connect(state["root"]), "scan_faces") != "0"
        if not state["root"].is_dir():
            raise HTTPException(409, f"{_disk_name(state['root'])} is not connected; plug it in first")
        return faces

    def _launch_index(req, faces: bool, project: str | None = None) -> dict:
        state["running"] = True
        state["progress"] = {"stage": "scan", "done": 0, "total": 0, "stage_started": time.time()}
        usage.log("index_start", shoot=state["root"].name, faces=faces, retry_errors=req.retry_errors, resume=req.resume)
        threading.Thread(target=_run, args=(state["root"], faces, req.retry_errors), daemon=True).start()
        return {"started": True, "faces": faces} if project is None else {"started": True, "faces": faces, "project": project}

    @app.post("/api/index")
    def start_index(req: IndexReq):
        """Start a scan, after the project file's preflight: the place it will be written at the end (the shoot
        folder, or the one picked for this shoot) must take a real write and have room for it. When it cannot,
        nothing starts and the reply is {started: false, blocked: {reason, message, ...}}, a 200, because a full
        or locked disk is a normal answer and not an error: the UI stops and asks where to keep the file, then
        calls /api/index/choose (the picker) or /api/index/at (the path the picker hands over)."""
        faces = _index_gates(req)
        dest = _scan_project_dir(state["root"])
        blocked = _project_preflight(state["root"], dest)
        if blocked:
            usage.log("index_blocked", reason=blocked["reason"])
            return {"started": False, "faces": faces, "blocked": blocked}
        return _launch_index(req, faces)

    def _start_index_at(dest: Path, req) -> dict:
        """The picked place for the project file: checked the same way as the preflight (a real write, room for
        the estimate), then a skeleton project file is written there at once, before a single photo is read,
        so a place that only looked writable fails now and not six hours from now. Only then is it remembered
        for this shoot and the scan started. A folder inside the shoot is refused (the shoot folder itself is
        fine). Every refusal is a 400 whose detail says why, and then nothing is remembered and no scan runs."""
        from . import bundle
        faces = _index_gates(req)
        root = state["root"]
        p = dest.expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {dest}")
        p = p.resolve()
        try:
            bundle.bundle_path(root, p)
        except ValueError as e:
            raise HTTPException(400, str(e))
        blocked = _project_preflight(root, p)
        if blocked:
            raise HTTPException(400, blocked["message"])
        try:
            out = bundle.export_bundle(root, p, thumbs=False)
        except OSError as e:
            raise HTTPException(400, f"could not write the project file in {p}: {e.strerror or e}")
        _remember_project(root, out, skeleton=True)
        usage.log("project_location", same_disk=_disk_name(p) == _disk_name(root))
        return _launch_index(req, faces, project=str(out))

    @app.post("/api/index/at")
    def start_index_at(req: IndexAtReq):
        return _start_index_at(Path(req.path), req)

    @app.post("/api/index/choose")
    def start_index_choose(req: IndexReq):
        """The blocking ask: the native folder picker (opening on ~/Documents, which is always writable), then
        _start_index_at with the pick. 204 when the picker is cancelled, and then no scan runs."""
        _index_gates(req)
        start = Path.home() / "Documents"
        if not start.is_dir():
            start = Path.home()
        quoted = str(start).replace("\\", "\\\\").replace('"', '\\"')
        path_str = _run_picker(f'POSIX path of (choose folder with prompt "Where should the project file for this shoot go?" default location (POSIX file "{quoted}"))', "folder")
        if path_str is None:
            return Response(status_code=204)
        return _start_index_at(Path(path_str), req)

    # Every key index.notify() emits, defaulted here: a categorise, a pause and a crash set the progress
    # too and none of them is a numbered step, so the panel reads one shape whoever wrote it.
    PROGRESS_DEFAULTS = {"step": None, "steps": None, "current": None, "project": None,
                         "substage": None, "substage_done": None, "substage_total": None,
                         "substage_started": None, "rate": None, "eta_s": None}

    @app.get("/api/progress")
    def progress():
        """The scan's progress plus running and awake (caffeinate is holding the Mac awake for a job).
        step of steps numbers the stages this pass will actually run (index.notify decides which); both
        are None outside a scan, and for the terminal done. current is the file being read right now,
        relative to the shoot root, or None for a stage that is not reading the disk. substage names the
        sub-stage within embed (photos, clips, segments) with its own counter and start time, because an
        item is one frame in the first, six to thirty in the second and usually none in the third. rate
        (items per second of the running sub-stage) and eta_s (seconds left in the stage, priced per
        sub-stage, see index.embed_eta) come with the embed stage only, and are None everywhere else."""
        return dict(PROGRESS_DEFAULTS, **state["progress"], running=state["running"], awake=awake.held())

    # ===== the scan desktop: the Scan tab's Finder scene polls this beside /api/progress =====
    @app.get("/api/scan/sample")
    def scan_sample(since: int = 0, n: int = 120, ids: str = "", folders: int = 1):
        """A fixed sample of the shoot's files for the cards on the Scan tab, and the true per-folder counts
        (db.scan_sample has the shape), plus shoot (the folder's name, the Stack's label) and disk (the volume under /Volumes, else None).
        since pages forward through the sample; ids asks again about cards still waiting for a thumb or a folder;
        folders=0 skips counting the folders (the scene asks only until it has them, and never while reading).
        folders stays empty while a scan is still reading or categorising, so a rescan never shows last time's
        folders before this pass has decided them. Opens its own connection, like the other status reads."""
        r = state["root"]
        if r is None:
            return {"total": 0, "photos": 0, "videos": 0, "since": max(0, since), "max_id": max(0, since),
                    "rows": [], "updates": [], "folders": [], "shoot": None, "disk": None}
        try:
            want = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids must be numbers")
        conn = db.connect(r)
        try:
            stage = (state["progress"] or {}).get("stage")
            hold = (state["running"] and stage not in ("done", "save")) or state["classify"]["running"]
            out = db.scan_sample(conn, n=n, since=since, ids=want, folders=bool(folders) and not hold)
        finally:
            conn.close()
        out["shoot"] = r.name or str(r)
        out["disk"] = r.parts[2] if len(r.parts) >= 3 and r.parts[:2] == ("/", "Volumes") else None
        return out
    # ===== end the scan desktop =====

    # live.html's demo plays a scripted shoot from the landing page's photos when run from the repo
    @app.get("/site/img/{name}")
    def site_img(name: str):
        p = UI.parent.parent / "site" / "img" / Path(name).name
        if not p.is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(p)

    def _filters(sharp, faces, person, taken_from, taken_to, category, kind=None, cluster=None, sure_only=0, aerial=0,
                 hide_bad=0, hide_soft=0, fold=0, day=None, bbox=None, shot_size=None, slowmo=0,
                 camera=None, look=None, duration=None, pile=0) -> Filters:
        if kind not in (None, "", "photos", "videos"):
            raise HTTPException(400, "kind must be photos or videos")
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(400, "day must be YYYY-MM-DD")
        # "unknown" is a value the pass writes, so it is accepted here; a typo is refused rather than
        # quietly matching nothing, which would look like a shoot with no wide shots in it.
        if shot_size not in (None, "", *SHOT_SIZE_NAMES):
            raise HTTPException(400, f"shot_size must be one of {', '.join(SHOT_SIZE_NAMES)}")
        if look not in (None, "", *LOOKS):
            raise HTTPException(400, f"look must be one of {', '.join(LOOKS)}")
        if duration not in (None, "", *DURATION_KEYS):
            raise HTTPException(400, f"duration must be one of {', '.join(DURATION_KEYS)}")
        box = _bbox(bbox)
        return Filters(sharp_min_pct=sharp, faces=faces or None, person_id=person, taken_from=taken_from,
                       taken_to=taken_to, day=day or None, bbox=box, category=category or None, kind=kind or None,
                       cluster=cluster or None, sure_only=bool(sure_only), aerial=True if aerial else None,
                       shot_size=shot_size or None, slowmo=True if slowmo else None,
                       camera=camera or None, look=look or None, duration=duration or None,
                       hide_bad=bool(hide_bad), hide_soft=bool(hide_soft), fold=bool(fold), pile=bool(pile))

    # ===== marks: the flag / stars / label filters, laid over whatever _filters built =====
    def _with_marks(f: Filters, flag: str | None = None, stars: int | None = None, label: str | None = None) -> Filters:
        if flag not in (None, "", "pick", "reject", "none"):
            raise HTTPException(400, "flag must be pick, reject or none")
        if stars is not None and not 1 <= stars <= 5:
            raise HTTPException(400, "stars must be 1 to 5")
        if label not in (None, "", "none", *db.LABELS):
            raise HTTPException(400, f"label must be one of {', '.join(db.LABELS)} or none")
        f.flag = flag or None; f.stars_min = stars; f.label = label or None
        return f
    # ===== end marks =====

    # ===== shoot modes: the group, camera and product filters, read off the query string and laid over _filters =====
    def _with_shoot(f: Filters, request: Request) -> Filters:
        q = request.query_params
        g = q.get("group") or None
        if g not in (None, *GROUP_KINDS):
            raise HTTPException(400, f"group must be one of {', '.join(GROUP_KINDS)}")
        f.group = g
        f.lens = q.get("lens") or None
        f.flicker = q.get("flicker") or None
        if f.flicker not in (None, "pulsing", "banding", "none"):
            raise HTTPException(400, "flicker must be pulsing, banding or none")
        for axis in CAMERA_AXES:
            v = q.get(axis) or None
            if v not in (None, "unknown", *(k for k, _, _ in CAMERA_BUCKETS[axis])):
                raise HTTPException(400, f"{axis} is not one of its buckets")
            setattr(f, axis, v)
        for key in ("product", "variant"):
            v = q.get(key)
            try:
                setattr(f, key, int(v) if v not in (None, "") else None)
            except ValueError:
                raise HTTPException(400, f"{key} must be a number")
        return f
    # ===== end shoot modes =====

    def _bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
        """south,west,north,east as the map hands it over. A box may cross the date line (west > east), which
        is why it is never just a pair of ranges."""
        if not bbox:
            return None
        try:
            south, west, north, east = (float(x) for x in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be south,west,north,east")
        if not (-90 <= south <= north <= 90) or not (-180 <= west <= 180 and -180 <= east <= 180):
            raise HTTPException(400, "bbox is outside the world")
        return (south, west, north, east)

    def _sort(sort: str | None) -> str | None:
        if sort in (None, ""):
            return None
        if sort not in SORTS:
            raise HTTPException(400, f"sort must be one of {', '.join(SORTS)}")
        return sort

    @app.get("/api/search")
    def search(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
               person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
               category: str | None = None, kind: str | None = None, cluster: str | None = None, sure_only: int = 0,
               aerial: int = 0, hide_bad: int = 0, hide_soft: int = 0, fold: int = 0, day: str | None = None,
               bbox: str | None = None, shot_size: str | None = None, slowmo: int = 0,
               camera: str | None = None, look: str | None = None, duration: str | None = None, pile: int = 0,
               sort: str | None = None, limit: int = 200, offset: int = 0,
               flag: str | None = None, stars: int | None = None, label: str | None = None, request: Request = None):
        """Each result carries kind, duration and aerial, sure and confidence, and focus (None until the focus
        pass ran, else ok / soft / bad); with a category or cluster filter the sure ones come first, then the
        "less sure" band by confidence. sure_only=1 drops the band (the per-tile export uses it).
        kind=photos|videos keeps one kind; aerial=1 keeps drone shots only; hide_bad=1 drops rows labelled
        bad, hide_soft=1 drops soft and bad (unchecked rows are never hidden). A row in a duplicate set or a
        burst carries group {kind: copies|burst, n, members, sharpest}; fold=1 keeps one row per set.
        sort is one of search.SORTS (default oldest first by capture time) and is ignored while q or
        image_id is set: a query scores every photo, so any other order would throw the query away."""
        if state["root"] is None:
            return {"results": [], "total": 0, "offset": 0, "limit": limit}
        sort = _sort(sort)
        limit = max(1, min(limit, 1000)); offset = max(0, offset)
        t0 = time.time()
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_with_shoot(_with_marks(_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only, aerial, hide_bad, hide_soft, fold, day, bbox, shot_size, slowmo, camera, look, duration, pile), flag=flag, stars=stars, label=label), request), sort=sort)
        except LookupError as e:
            raise HTTPException(404, str(e))
        # The usage log keeps the query's length and first characters plus which filters were on; an empty
        # search (the grid's own reload) and a "Show more" page are not logged.
        filt = {k: v for k, v in dict(sharp=sharp, faces=faces, person=bool(person), taken_from=taken_from, taken_to=taken_to,
                                      day=day, bbox=bbox, category=category, kind=kind, cluster=cluster, sure_only=sure_only, aerial=aerial,
                                      hide_bad=hide_bad, hide_soft=hide_soft, shot_size=shot_size, slowmo=slowmo,
                                      camera=bool(camera), look=look, duration=duration,
                                      image_id=bool(image_id)).items() if v}
        filt.update({k: v for k, v in dict(flag=flag, stars=stars, label=label).items() if v})   # ===== marks =====
        if offset == 0 and (q or filt):
            usage.log("search", q=q or "", filters=filt, total=len(rows), ms=round((time.time() - t0) * 1000))
        # ===== search history: a search with text goes into the shoot's searches table with the filters it ran with =====
        if offset == 0 and q and q.strip():
            hist = {k: v for k, v in dict(sharp=sharp, faces=faces, person=person, category=category, kind=kind, cluster=cluster,
                                          aerial=aerial, hide_bad=hide_bad, hide_soft=hide_soft, shot_size=shot_size,
                                          slowmo=slowmo, camera=camera, look=look, duration=duration).items() if v}
            try:
                db.record_search(db.connect(state["root"]), q, hist)
            except sqlite3.Error as e:
                usage.log("api_error", route="/api/search", what="record_search", error=str(e))
        # ===== end search history =====
        # folded: rows hidden inside the tiles of this result set (duplicates and burst frames), so the UI can
        # say "1,965 copies folded" instead of quietly showing fewer tiles than the shoot has.
        folded = sum((r.get("group") or {}).get("n", 1) - 1 for r in rows) if fold else 0
        return {"results": [dict(p) for p in rows[offset:offset + limit]], "total": len(rows), "offset": offset,
                "limit": limit, "folded": folded}

    # ===== search history and saved searches: per shoot, in the index (table searches) =====
    @app.get("/api/searches")
    def searches():
        """{recent: the last 20 queries with text, newest first; saved: the starred ones}. Each row carries the
        filters the query last ran with, so a saved search restores its chips too."""
        if state["root"] is None:
            return {"recent": [], "saved": []}
        return db.list_searches(db.connect(state["root"]))

    @app.post("/api/searches/{sid}/save")
    def save_search(sid: int, req: SavedReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        row = db.save_search(db.connect(state["root"]), sid, req.saved)
        if row is None:
            raise HTTPException(404, "no such search")
        usage.log("search_saved", saved=req.saved)
        return row

    @app.delete("/api/searches/{sid}")
    def delete_search(sid: int):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if not db.delete_search(db.connect(state["root"]), sid):
            raise HTTPException(404, "no such search")
        return {"deleted": sid}
    # ===== end search history =====

    @app.get("/api/search/ids")
    def search_ids(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
                   person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
                   category: str | None = None, kind: str | None = None, cluster: str | None = None, sure_only: int = 0,
                   aerial: int = 0, hide_bad: int = 0, hide_soft: int = 0, fold: int = 0, day: str | None = None,
                   bbox: str | None = None, shot_size: str | None = None, slowmo: int = 0,
                   camera: str | None = None, look: str | None = None, duration: str | None = None, pile: int = 0, sort: str | None = None,
                   flag: str | None = None, stars: int | None = None, label: str | None = None, request: Request = None):
        if state["root"] is None:
            return {"ids": [], "total": 0}
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_with_shoot(_with_marks(_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only, aerial, hide_bad, hide_soft, fold, day, bbox, shot_size, slowmo, camera, look, duration, pile), flag=flag, stars=stars, label=label), request), sort=_sort(sort))
        except LookupError as e:
            raise HTTPException(404, str(e))
        return {"ids": [p["id"] for p in rows], "total": len(rows)}

    @app.get("/api/search/facets")
    def search_facets(sharp: float | None = None, faces: str | None = None, person: int | None = None,
                      taken_from: str | None = None, taken_to: str | None = None, category: str | None = None,
                      kind: str | None = None, cluster: str | None = None, sure_only: int = 0, aerial: int = 0,
                      hide_bad: int = 0, hide_soft: int = 0, fold: int = 0, day: str | None = None,
                      bbox: str | None = None, shot_size: str | None = None, slowmo: int = 0,
                      camera: str | None = None, look: str | None = None, duration: str | None = None, pile: int = 0,
                      flag: str | None = None, stars: int | None = None, label: str | None = None, request: Request = None):
        """The facet rail's live numbers for the filters in force (search.Index.facet_counts): total is what
        /api/search would count, and each axis is counted with every filter on but its own. A query's text
        does not change these: a query ranks the rows that pass, it never drops one."""
        if state["root"] is None:
            return {"total": 0}
        return ix().facet_counts(_with_shoot(_with_marks(_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only, aerial,
                                                      hide_bad, hide_soft, fold, day, bbox, shot_size, slowmo, camera, look, duration, pile),
                                             flag=flag, stars=stars, label=label), request))

    # ===== the pile: what the editor has picked on this shoot, in the index (photosort.pile) =====
    _EMPTY_PILE = {"ids": [], "n": 0, "photos": 0, "videos": 0, "bytes": 0}

    def _pile_marks(conn, ids) -> None:
        """A pile change moves photos.flag, and the search Index keeps its own copy of the marks."""
        if state["index"] is not None:
            state["index"].set_marks(db.get_marks(conn, ids))

    @app.get("/api/pile")
    def get_pile():
        """{ids, n, photos, videos, bytes}: the picked rows still in the shoot, in picking order."""
        if state["root"] is None:
            return dict(_EMPTY_PILE)
        return pile.summary(db.connect(state["root"]))

    @app.post("/api/pile")
    def change_pile(req: PileReq):
        """Takes out req.remove, then puts in req.add (so a row in both ends up picked), and answers with the
        pile as it now is, plus unrejected: the ids a pick took a reject off. One call per click or per select
        all, never per tile."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(state["root"])
        unrejected = []
        if req.remove:
            pile.remove(conn, req.remove)
        if req.add:
            unrejected = pile.add(conn, req.add)
        _pile_marks(conn, req.remove + req.add)
        return dict(pile.summary(conn), unrejected=unrejected)

    @app.delete("/api/pile")
    def clear_pile():
        """Empties the list. cleared is every row as it was: POST it to /api/pile/restore to undo this."""
        if state["root"] is None:
            return dict(_EMPTY_PILE, cleared=[])
        conn = db.connect(state["root"])
        cleared = pile.clear(conn)
        _pile_marks(conn, [r["id"] for r in cleared])
        return dict(pile.summary(conn), cleared=cleared)

    @app.post("/api/pile/restore")
    def restore_pile(req: PileRestoreReq):
        """Undo for a Clear. Answers with the pile as it now is."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(state["root"])
        try:
            pile.restore(conn, req.rows)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "rows must be what clearing the list answered with")
        _pile_marks(conn, [r["id"] for r in req.rows])
        return pile.summary(conn)
    # ===== end the pile =====

    # ===== search by an image that is not in the shoot: the file is dropped on the window, not indexed =====

    async def _upload_bytes(request: Request) -> bytes:
        """The request body, refused the moment it goes past UPLOAD_MAX_BYTES.

        Content-Length is checked first so an obviously huge file is turned away before a byte of it is
        read, and the running total is checked again while streaming because a chunked upload sends no
        length at all and a lying one would otherwise be believed. The cap exists because this body is
        held in memory: the app is a local server, but a local server is still something a stray script
        can point a 4 GB file at."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > UPLOAD_MAX_BYTES:
            raise HTTPException(413, f"that image is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > UPLOAD_MAX_BYTES:
                raise HTTPException(413, f"that image is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
        if not buf:
            raise HTTPException(400, "no image in the request body")
        return bytes(buf)

    @app.post("/api/search/upload")
    async def search_upload(request: Request, sharp: float | None = None, faces: str | None = None,
                            person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
                            category: str | None = None, kind: str | None = None, cluster: str | None = None,
                            sure_only: int = 0, aerial: int = 0, hide_bad: int = 0, hide_soft: int = 0,
                            fold: int = 0, day: str | None = None, bbox: str | None = None,
                            shot_size: str | None = None, slowmo: int = 0,
                            camera: str | None = None, look: str | None = None, duration: str | None = None,
                            limit: int = 200, offset: int = 0,
                            flag: str | None = None, stars: int | None = None, label: str | None = None):
        """Rank the shoot against an image that is not in it. The body is the image file itself (POST the
        File straight from a drop zone; the Content-Type is ignored, the bytes decide), the filters are the
        same query parameters /api/search takes, and results come back in the same shape, each row with
        score = cosine similarity, so the grid renders them unchanged. embed_ms is what encoding the
        dropped image cost, which is the whole cost of this route: scoring is one matrix product.

        On top of results, the answer everyone actually came for (search.angles): cameras, the best match
        from each camera ranked by score, so the page is a set of angles rather than a burst off whichever
        body shot most; moment, the top match plus everything a different camera shot within window_s of
        it; and unknown, the rows whose camera nobody recorded, kept apart because unknown is not a camera.

        Nothing is written. The bytes are decoded from memory and dropped when the request ends."""
        data = await _upload_bytes(request)
        if state["root"] is None:
            return {"results": [], "total": 0, "offset": 0, "limit": limit, "folded": 0, "embed_ms": 0,
                    "cameras": [], "unknown": None, "moment": None, "window_s": ANGLE_WINDOW_S}
        limit = max(1, min(limit, 1000)); offset = max(0, offset)
        return await run_in_threadpool(
            _upload_search, data,
            _with_marks(_filters(sharp, faces, person, taken_from, taken_to, category, kind, cluster, sure_only,
                                 aerial, hide_bad, hide_soft, fold, day, bbox, shot_size, slowmo, camera, look, duration),
                        flag=flag, stars=stars, label=label),
            limit, offset)

    def _upload_search(data: bytes, filters: Filters, limit: int, offset: int) -> dict:
        """Decode, encode, score. Runs off the event loop because the loop is also serving the thumbnails
        the grid is asking for while this runs.

        Measured on the M1, one 12 MP JPEG: decode 55 ms, encode 70 ms on CPU and 47 ms on MPS, so a
        warm answer is about an eighth of a second. Two costs are not in that number and are paid once
        per run of the app: loading the weights, 3 to 5 s, and the first tensor op on MPS, which spends
        another half second compiling Metal kernels. Scoring is M @ q, 1,627 x 768 floats, under a
        millisecond, which is why this route reuses the index rather than building anything."""
        from .decode import load_upload, DecodeError
        from .embed import get_embedder
        try:
            im = load_upload(data)
        except DecodeError as e:
            # What PIL said goes to the log, not to the person: "cannot identify image file
            # <_io.BytesIO object at 0x136f82840>" is true and useless on a drop zone. What they
            # need is which files this takes.
            usage.log("search_upload_rejected", bytes=len(data), why=str(e)[:200])
            raise HTTPException(400, "not an image this app can read; drop a JPEG, PNG, HEIC, WEBP or TIFF still")
        t0 = time.time()
        vec = get_embedder().encode_images([im])[0]
        embed_ms = round((time.time() - t0) * 1000)
        try:
            rows = ix().query(vector=vec, filters=filters)
        except LookupError as e:
            raise HTTPException(409, str(e))
        folded = sum((r.get("group") or {}).get("n", 1) - 1 for r in rows) if filters.fold else 0
        # Over every row, not the page: see search.angles on why the first page is the wrong input.
        grouped = angles(rows)
        usage.log("search_upload", bytes=len(data), total=len(rows), embed_ms=embed_ms,
                  cameras=len(grouped["cameras"]),
                  moment=len((grouped["moment"] or {}).get("others", [])))
        return {"results": [dict(p) for p in rows[offset:offset + limit]], "total": len(rows),
                "offset": offset, "limit": limit, "folded": folded, "embed_ms": embed_ms, **grouped}

    # ===== end search by an uploaded image =====

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
        rows are flagged aerial (a flag across categories, the last tile of the fixed row); also: how many of
        each fixed tile's rows are there as a secondary (filed elsewhere, fits here too; classify.SECOND_MIN_PROB)."""
        if state["root"] is None:
            return {"fixed": {}, "discovered": {}, "drone": 0, "also": {}}
        conn = db.connect(state["root"])
        return {"fixed": _fixed_order(db.category_counts(conn)), "discovered": db.cluster_counts(conn), "drone": db.aerial_count(conn),
                "also": db.category_also_counts(conn)}

    @app.get("/api/facets")
    def facets():
        """The live numbers behind the facet rail, for the axes that are not categories.

        shot_size: close / medium / wide / unknown, plus "unscored" for rows the pass has never seen
        (an index from before the column, or a row with no embedding). unknown and unscored are two
        different facts and the rail must be able to say which it has.
        slowmo: {slow, normal, unknown} over the CLIPS of the shoot only. A still has no frame rate.
        fps_read: index.fps_status, so the UI knows whether to offer the catch-up pass at all.
        cameras: camera -> rows, stills and clips together, most used first.
        look: {log, standard, unknown} over the clips (the picture profile off Sony's sidecar).
        duration: config.DURATION_BUCKETS key -> clips.
        clip_read: index.clip_info_status, the header and sidecar pass (fps_read is the same numbers
        under the name v0.4.0 shipped). clip_faces: index.clip_faces_status, the faces-in-clips pass.
        Camera motion (locked off versus moving): not here. classify.py records the measurement that says
        a frame to frame difference over frames seconds apart cannot answer it, so there is no number to
        show and inventing one would be worse than the greyed out tile."""
        if state["root"] is None:
            return {"shot_size": {}, "slowmo": {}, "fps_read": None, "cameras": {}, "look": {}, "duration": {},
                    "clip_read": None, "clip_faces": None}
        conn = db.connect(state["root"])
        read = index.clip_info_status(state["root"])
        return {"shot_size": db.shot_size_counts(conn), "slowmo": db.slowmo_counts(conn), "fps_read": read,
                "cameras": db.camera_counts(conn), "look": db.look_counts(conn), "duration": db.duration_counts(conn),
                "clip_read": read, "clip_faces": index.clip_faces_status(state["root"]),
                # ===== shoot modes: the whole shoot's sequence and camera axes, and whether settings are still unread =====
                "shoot": {k: v for k, v in ix().facet_counts(Filters()).items()
                          if k in ("groups", "group_sets", "lens", "focal", "iso", "shutter", "aperture", "flash")},
                "flicker": ix().facet_counts(Filters()).get("flicker", {}), "flicker_read": index.flicker_status(state["root"]),
                "camera_read": index.camera_info_status(state["root"])}

    # ===== marks: flags, stars and colour labels, set from the grid's keys and the inspector =====
    @app.post("/api/marks")
    def set_marks(req: MarksReq):
        """Set any of flag, rating and label on ids in one write (a keystroke on a 200-tile selection is one call).
        A field left out of the body is left alone; one sent as null is cleared. Returns the marks as stored."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if not req.ids:
            raise HTTPException(400, "no items given")
        given = {k: getattr(req, k) for k in ("flag", "rating", "label") if k in req.model_fields_set}
        if not given:
            raise HTTPException(400, "nothing to set: send flag, rating or label")
        if "rating" in given and given["rating"] is None:
            given["rating"] = 0
        conn = db.connect(state["root"])
        try:
            db.set_marks(conn, req.ids, **given)
        except ValueError as e:
            raise HTTPException(400, str(e))
        marks = db.get_marks(conn, req.ids)
        if state["index"] is not None:
            state["index"].set_marks(marks)
        usage.log("marks", n=len(req.ids), **given)
        out = {"marks": {str(k): v for k, v in marks.items()}}
        if "flag" in given:                            # a flag moves the Picked list too
            out["pile"] = pile.summary(conn)
        return out

    @app.get("/api/marks/counts")
    def mark_counts():
        """The sidebar's numbers: flag {pick, reject, none}, stars {"1".."5": at least that many}, label, marked."""
        if state["root"] is None:
            return {"flag": {}, "stars": {}, "label": {}, "marked": 0}
        return db.mark_counts(db.connect(state["root"]))

    @app.post("/api/xmp")
    def write_xmp_api(req: XmpReq):
        """XMP sidecars on their own, under <destination>/<shoot>/xmp/ in the shoot's own folder layout, for the
        originals (merge that folder over a copy of the cards). ids None: every marked item. Never in the shoot."""
        from .export import write_xmp
        root = state["root"]
        if root is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(root)
        ids = req.ids if req.ids is not None else [r[0] for r in conn.execute(
            "SELECT id FROM photos WHERE status='ok' AND (flag IS NOT NULL OR COALESCE(rating, 0) > 0 OR label IS NOT NULL) ORDER BY id")]
        if not ids:
            raise HTTPException(400, "nothing is flagged, rated or labelled yet")
        try:
            out, n = write_xmp(root, ids, base=_resolve_base())
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f"could not write the sidecars: {e.strerror or e}")
        usage.log("xmp_written", **n)
        return dict(n, path=str(out))
    # ===== end marks =====

    @app.post("/api/fps/read")
    def start_fps():
        """Read the frame rate of every ok clip that has none, from the container header. 409 while
        indexing, categorising, checking focus, reading locations or exporting. {"started": true,
        "total": n}. This is the one facet pass that needs the shoot disk plugged in; index.read_fps
        says what it costs and why it is not a rescan."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot read frame rates while scanning")
        if state["classify"]["running"]:
            raise HTTPException(409, "cannot read frame rates while categorising")
        if state["focus"]["running"]:
            raise HTTPException(409, "cannot read frame rates while the focus pass is running")
        if state["places"]["running"]:
            raise HTTPException(409, "cannot read frame rates while locations are being read")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot read frame rates while an export is running")
        if state["clipfaces"]["running"]:
            raise HTTPException(409, "cannot read frame rates while faces in clips are being found")
        with state["fps_lock"]:
            if state["fps"]["running"]:
                raise HTTPException(409, "already reading frame rates")
            root_at_start = state["root"]
            total = index.fps_status(root_at_start)["unknown"]
            state["fps"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def prog(d):
            state["fps"].update(done=d.get("done", 0), total=d.get("total", total))

        t0 = time.time()

        def _run_fps():
            try:
                with awake.hold():
                    state["fps"]["counts"] = index.read_fps(root_at_start, progress=prog)
            except Exception as e:
                state["fps"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True      # the search Index caches the fps column
                state["fps"]["running"] = False
                c = state["fps"]["counts"] or {}
                usage.log("fps_read", found=c.get("found", 0), read=c.get("read", 0),
                          missing=c.get("missing", 0), seconds=round(time.time() - t0, 1),
                          error=state["fps"]["error"])

        threading.Thread(target=_run_fps, daemon=True).start()
        return {"started": True, "total": total}

    @app.get("/api/fps/progress")
    def fps_progress():
        """{running, done, total, counts, error, awake}; counts is {found, read, missing} once it ended."""
        return dict(state["fps"], awake=awake.held())

    # ===== shoot modes: camera settings catch-up, best of each burst, products =====
    def _busy(what: str) -> None:
        """409 while anything else holds the index or the disk, the way every pass here refuses to overlap."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        for key, label in (("classify", "categorising"), ("focus", "the focus pass is running"), ("places", "locations are being read"),
                           ("export", "an export is running"), ("clipfaces", "faces in clips are being found"),
                           ("fps", "clip details are being read"), ("camera", "camera settings are being read"),
                           ("products", "products are being found"), ("flicker", "clips are being checked for flicker"),
                           ("cull", "Client pick is measuring")):                       # ===== client pick =====
            if key != what and state[key]["running"]:
                raise HTTPException(409, f"cannot start while {label}")
        if state["running"]:
            raise HTTPException(409, "cannot start while scanning")

    @app.get("/api/camera/status")
    def camera_status():
        """{photos, known, unknown, read_at} (index.camera_info_status): how many stills have shutter, aperture and the rest."""
        if state["root"] is None:
            return {"photos": 0, "known": 0, "unknown": 0, "read_at": None}
        return index.camera_info_status(state["root"])

    @app.post("/api/camera/read")
    def start_camera_read():
        """Read the exposure settings off every still that has none (index.read_camera_info): needs the shoot disk,
        reads the EXIF header only, writes nothing there. {"started": true, "total": n}."""
        _busy("camera")
        with state["camera_lock"]:
            if state["camera"]["running"]:
                raise HTTPException(409, "already reading camera settings")
            root_at_start = state["root"]
            total = index.camera_info_status(root_at_start)["unknown"]
            state["camera"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def prog(d):
            state["camera"].update(done=d.get("done", 0), total=d.get("total", total))

        t0 = time.time()

        def _run():
            try:
                with awake.hold():
                    state["camera"]["counts"] = index.read_camera_info(root_at_start, progress=prog)
            except Exception as e:
                state["camera"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["camera"]["running"] = False
                c = state["camera"]["counts"] or {}
                usage.log("camera_read", found=c.get("found", 0), read=c.get("read", 0), missing=c.get("missing", 0),
                          seconds=round(time.time() - t0, 1), error=state["camera"]["error"])

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True, "total": total}

    @app.post("/api/flicker/read")
    def start_flicker_read():
        """Check every clip scanned before flicker detection (index.read_flicker): needs the disk, writes nothing there."""
        _busy("flicker")
        if state["flicker"]["running"]:
            raise HTTPException(409, "already checking for flicker")
        root_at_start = state["root"]
        total = index.flicker_status(root_at_start)["unchecked"]
        state["flicker"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def _run():
            try:
                with awake.hold():
                    state["flicker"]["counts"] = index.read_flicker(root_at_start, progress=lambda d: state["flicker"].update(done=d["done"], total=d["total"]))
            except Exception as e:
                state["flicker"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["flicker"]["running"] = False

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True, "total": total}

    @app.get("/api/flicker/progress")
    def flicker_progress():
        return dict(state["flicker"], awake=awake.held())

    @app.get("/api/camera/progress")
    def camera_progress():
        return dict(state["camera"], awake=awake.held())

    @app.post("/api/groups/best")
    def keep_best(request: Request, kind: str = "burst", sharp: float | None = None, faces: str | None = None,
                  person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
                  category: str | None = None, cluster: str | None = None, sure_only: int = 0, aerial: int = 0,
                  hide_bad: int = 0, hide_soft: int = 0, day: str | None = None, bbox: str | None = None,
                  shot_size: str | None = None, slowmo: int = 0, camera: str | None = None, look: str | None = None,
                  duration: str | None = None, pile: int = 0,
                  flag: str | None = None, stars: int | None = None, label: str | None = None):
        """Put the best frame of every burst (the sharpest; the eyes decide when every frame has a face) or of every
        bracket (its middle exposure) among the rows the filters leave into the pile. Answers with the pile and
        {sets, added}. The search's own kind (photos/videos) never matters here: only stills burst."""
        if kind not in ("burst", "bracket"):
            raise HTTPException(400, "kind must be burst or bracket")
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        f = _with_shoot(_with_marks(_filters(sharp, faces, person, taken_from, taken_to, category, None, cluster, sure_only, aerial,
                                             hide_bad, hide_soft, 0, day, bbox, shot_size, slowmo, camera, look, duration, pile),
                                    flag=flag, stars=stars, label=label), request)
        f.group = kind
        rows = ix().query(filters=f)
        best: dict[int, int] = {}
        for r in rows:
            g = r.get("group")
            if g and g["kind"] == kind:
                best[id(g)] = g["base"] if kind == "bracket" else g["sharpest"]
        from . import pile as pile_mod                 # the pile query parameter shadows the module in here
        conn = db.connect(state["root"])
        before = set(pile_mod.ids(conn))
        ids = list(dict.fromkeys(best.values()))
        unrejected = pile_mod.add(conn, ids)
        _pile_marks(conn, ids)
        usage.log("keep_best", kind=kind, sets=len(ids))
        return dict(pile_mod.summary(conn), sets=len(ids), added=len([i for i in ids if i not in before]), unrejected=unrejected)

    @app.get("/api/products")
    def get_products():
        """[{product, name, named, n, variants: [{variant, colour, hex, n, views, cover}]}] from the last find."""
        if state["root"] is None:
            return {"products": []}
        return {"products": products_mod.list_products(db.connect(state["root"]))}

    @app.post("/api/products/find")
    def start_products(req: ProductsReq):
        """Group the stills (or req.ids) into products and colour variants (products.find_products). Reads the grid
        thumbs on this Mac; the disk can be unplugged. One encode per still, about 47 ms each on an M1."""
        _busy("products")
        with state["products_lock"]:
            if state["products"]["running"]:
                raise HTTPException(409, "already finding products")
            root_at_start = state["root"]
            state["products"] = {"running": True, "done": 0, "total": len(req.ids) if req.ids is not None else 0, "counts": {}, "error": None}

        def prog(done, total):
            state["products"].update(done=done, total=total)

        t0 = time.time()

        def _run():
            try:
                with awake.hold():
                    state["products"]["counts"] = products_mod.find_products(root_at_start, req.ids, progress=prog)
            except Exception as e:
                state["products"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["products"]["running"] = False
                c = state["products"]["counts"] or {}
                usage.log("products_found", products=c.get("products", 0), variants=c.get("variants", 0),
                          photos=c.get("photos", 0), seconds=round(time.time() - t0, 1), error=state["products"]["error"])

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True}

    @app.get("/api/products/progress")
    def products_progress():
        return dict(state["products"], awake=awake.held())

    @app.post("/api/products/{product}/name")
    def name_product(product: int, req: NameReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        name = (req.name or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "a name, up to 80 characters")
        if not products_mod.rename(db.connect(state["root"]), product, name):
            raise HTTPException(404, "no such product")
        return {"product": product, "name": name}

    @app.post("/api/export/products")
    def export_products_api(req: ProductsExportReq):
        """One folder per product and colour, "<product> <colour>", under <destination>/<shoot>/products/. Same job
        machinery as the category export: one export at a time, a free space check for copies."""
        from .export import export_folders, folders_bytes, export_dir
        if req.mode not in ("copy", "symlink"):
            raise HTTPException(400, "mode must be copy or symlink")
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            conn = db.connect(root_at_start)
            want = {tuple(x) for x in req.only} if req.only else None
            folders: dict[str, list[int]] = {}
            for pr in products_mod.list_products(conn):
                for v in pr["variants"]:
                    if want is not None and (pr["product"], v["variant"]) not in want:
                        continue
                    key = stem = f"{pr['name']} {v['colour']}"
                    nth = 2
                    while key in folders:            # two variants one colour name apart: "mug blue", "mug blue 2"
                        key = f"{stem} {nth}"; nth += 1
                    folders[key] = [r[0] for r in conn.execute("SELECT photo_id FROM products WHERE product=? AND variant=? ORDER BY photo_id",
                                                              (pr["product"], v["variant"]))]
            if not folders:
                raise HTTPException(400, "no products found yet")
            base = _resolve_base()
            if req.mode == "copy":
                _check_free(folders_bytes(root_at_start, folders, req.include_raw), base)
            try:
                export_dir(root_at_start, "products", base)
            except ValueError as e:
                raise HTTPException(400, str(e))
            n = sum(len(v) for v in folders.values())
            state["export"] = {"running": True, "done": 0, "total": n, "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("products", "local", n, route="/api/export/products", body=req.model_dump(), mode=req.mode,
                             folders=len(folders), include_raw=req.include_raw)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(export_folders(root_at_start, "products", folders, req.mode, req.include_raw,
                                                                 base=base, progress=prog))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("products", "local", t0)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n, "folders": sorted(folders)}
    # ===== end shoot modes =====

    # ===== client pick (photosort/cull.py): the product shoot cut down to what the client sees =====
    def _cull_conn():
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(state["root"])
        cull_mod._ensure(conn)
        return conn

    def _cull_target(req) -> dict:
        if req.total is not None:
            if not 1 <= req.total <= 100000:
                raise HTTPException(400, "a total from 1 to 100,000")
            return {"total": req.total}
        n = req.per_product if req.per_product is not None else cull_mod.DEFAULT_PER_PRODUCT
        if not 1 <= n <= 500:
            raise HTTPException(400, "a target from 1 to 500 a product")
        return {"per_product": n}

    @app.get("/api/cull")
    def get_cull():
        """Everything the Client pick view draws (cull.listing): the running count, then per product its picks,
        the frames that were fine but not needed, and the rejects with their reason in words."""
        if state["root"] is None:
            return {"photos": 0, "picked": 0, "products": [], "rejected": {}, "target": {"per_product": cull_mod.DEFAULT_PER_PRODUCT}}
        return cull_mod.listing(_cull_conn())

    @app.post("/api/cull/run")
    def start_cull(req: CullReq):
        """Measure every product frame on its thumb (what is not measured yet), give each reject its reason and pick
        to the target. Needs Find products first. Reads thumbs on this Mac only. {"started": true}."""
        _busy("cull")
        target = _cull_target(req)
        conn = _cull_conn()
        if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            raise HTTPException(400, "find products first")
        with state["cull_lock"]:
            if state["cull"]["running"]:
                raise HTTPException(409, "already picking")
            root_at_start = state["root"]
            state["cull"] = {"running": True, "done": 0, "total": 0, "counts": {}, "error": None}
        cull_mod.set_target(conn, **target)

        def prog(done, total):
            state["cull"].update(done=done, total=total)

        t0 = time.time()

        def _run():
            try:
                with awake.hold():
                    state["cull"]["counts"] = cull_mod.run(root_at_start, progress=prog)
            except Exception as e:
                state["cull"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["cull"]["running"] = False
                c = state["cull"]["counts"] or {}
                usage.log("client_pick", photos=c.get("photos", 0), picked=c.get("picked", 0), products=c.get("products", 0),
                          seconds=round(time.time() - t0, 1), error=state["cull"]["error"])

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True}

    @app.get("/api/cull/progress")
    def cull_progress():
        return dict(state["cull"], awake=awake.held())

    @app.post("/api/cull/target")
    def cull_target(req: CullReq):
        """A new target, picked again from the stored reasons in a moment (nothing measured again)."""
        if state["cull"]["running"]:
            raise HTTPException(409, "still picking")
        target = _cull_target(req)
        conn = _cull_conn()
        if not conn.execute("SELECT 1 FROM cull LIMIT 1").fetchone():
            raise HTTPException(400, "run Client pick first")
        cull_mod.repick(conn, **target)
        return cull_mod.listing(conn)

    @app.post("/api/cull/swap")
    def cull_swap(req: CullIdReq):
        """Swap a pick for the next frame of the same colour and angle. {out, in}; in is null when there is none."""
        try:
            return cull_mod.swap(_cull_conn(), req.id)
        except KeyError:
            raise HTTPException(404, "that photo is not a pick")

    @app.post("/api/cull/pick")
    def cull_pick(req: CullIdReq):
        """Pick a frame by hand (on) or drop a pick (off)."""
        if not cull_mod.set_pick(_cull_conn(), req.id, req.on):
            raise HTTPException(404, "that photo is not in Client pick")
        return {"id": req.id, "pick": req.on}

    @app.post("/api/cull/merge")
    def cull_merge(req: CullMergeReq):
        """Two products that are one (detail shots Find products split off): fold product into into, colour by colour,
        and judge again from the stored measurements."""
        _busy("cull")
        conn = _cull_conn()
        with state["cull_lock"]:
            if state["cull"]["running"]:
                raise HTTPException(409, "still picking")
            state["cull"]["running"] = True
        try:
            try:
                cull_mod.merge_products(conn, req.product, req.into)
            except KeyError as e:
                raise HTTPException(404, str(e))
            except ValueError as e:
                raise HTTPException(400, str(e))
            with awake.hold():
                cull_mod.run(state["root"])
        finally:
            state["cull"]["running"] = False
        state["stale"] = True
        return cull_mod.listing(conn)

    @app.post("/api/cull/pile")
    def cull_to_pile():
        """Every pick into the pile, product by product. Answers with the pile and {added}."""
        from . import pile as pile_mod
        conn = _cull_conn()
        ids = [f["id"] for p in cull_mod.listing(conn)["products"] for f in p["picks"]]
        before = set(pile_mod.ids(conn))
        unrejected = pile_mod.add(conn, ids)
        _pile_marks(conn, ids)
        usage.log("client_pick_pile", n=len(ids))
        return dict(pile_mod.summary(conn), added=len([i for i in ids if i not in before]), unrejected=unrejected)

    @app.post("/api/export/cull")
    def export_cull(req: CullExportReq):
        """The picks, one folder per product with one folder per colour inside, under
        <destination>/<shoot>/client pick/, and client pick.csv beside them: product, colour, angle, file, status and
        the reason for every frame left out."""
        from .export import export_dir, rows_for_ids, _place, transfer_files
        import csv as csv_mod
        if req.mode not in ("copy", "symlink"):
            raise HTTPException(400, "mode must be copy or symlink")
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            conn = db.connect(root_at_start); cull_mod._ensure(conn)
            folders, rows = cull_mod.export_plan(conn)
            if not folders:
                raise HTTPException(400, "nothing is picked yet")
            base = _resolve_base()
            try:
                out = export_dir(root_at_start, "client pick", base)
            except ValueError as e:
                raise HTTPException(400, str(e))
            jobs: list = []
            for sub, ids in folders.items():
                for r in rows_for_ids(root_at_start, ids):
                    _place(jobs, r, sub, req.include_raw)
            if req.mode == "copy":
                need = 0
                for _, rel, _ in jobs:
                    try:
                        need += (Path(root_at_start) / rel).stat().st_size
                    except OSError:
                        pass
                _check_free(need, base)
            n = len(jobs)
            state["export"] = {"running": True, "done": 0, "total": n, "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("client_pick", "local", n, route="/api/export/cull", body=req.model_dump(), mode=req.mode,
                             folders=len(folders), include_raw=req.include_raw)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    out.mkdir(parents=True, exist_ok=True)
                    for sub in folders:
                        (out / sub).mkdir(parents=True, exist_ok=True)
                    transfer_files(root_at_start, [(pid, rel, out / sub) for pid, rel, sub in jobs], req.mode,
                                   out / "failed.txt", prog)
                    def cell(x):                    # a spreadsheet must never read a file name as a formula
                        x = str(x)
                        return "'" + x if x[:1] in ("=", "+", "-", "@") else x
                    with open(out / "client pick.csv", "w", newline="") as fh:
                        w = csv_mod.writer(fh)
                        w.writerow(["product", "colour", "angle", "file", "status", "reason"])
                        w.writerows([[cell(x) for x in r] for r in rows])
                    state["export"]["path"] = str(out)
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished("client_pick", "local", t0)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n, "folders": sorted(folders)}
    # ===== end client pick =====

    @app.post("/api/clipfaces/find")
    def start_clip_faces():
        """Face detection on the frames already under the index for every clip that never had it
        (index.find_clip_faces): no disk needed. When it ends the shot sizes are refreshed, because a clip's
        face size is what tells its medium from its wide (classify.refine_shot_size). 409 while scanning,
        categorising, checking focus, reading locations or frame rates, or exporting. {"started", "total"}."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        for key, what in (("running", "scanning"),):
            if state[key]:
                raise HTTPException(409, f"cannot find faces in clips while {what}")
        for key, what in (("classify", "categorising"), ("focus", "the focus pass is running"),
                          ("places", "locations are being read"), ("fps", "frame rates are being read"),
                          ("export", "an export is running")):
            if state[key]["running"]:
                raise HTTPException(409, f"cannot find faces in clips while {what}")
        with state["clipfaces_lock"]:
            if state["clipfaces"]["running"]:
                raise HTTPException(409, "already finding faces in clips")
            root_at_start = state["root"]
            total = index.clip_faces_status(root_at_start)["pending"]
            state["clipfaces"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def prog(d):
            state["clipfaces"].update(done=d.get("done", 0), total=d.get("total", total))

        t0 = time.time()

        def _run():
            try:
                with awake.hold():
                    counts = index.find_clip_faces(root_at_start, progress=prog)
                    if counts["read"] - counts["missing"] > 0:
                        classify_mod.classify_shot_size(root_at_start)
                    state["clipfaces"]["counts"] = counts
            except Exception as e:
                state["clipfaces"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True      # the search Index caches people, face_h and shot_size
                state["clipfaces"]["running"] = False
                c = state["clipfaces"]["counts"] or {}
                usage.log("clip_faces", found=c.get("found", 0), read=c.get("read", 0),
                          missing=c.get("missing", 0), seconds=round(time.time() - t0, 1),
                          error=state["clipfaces"]["error"])

        threading.Thread(target=_run, daemon=True).start()
        return {"started": True, "total": total}

    @app.get("/api/clipfaces/progress")
    def clip_faces_progress():
        """{running, done, total, counts, error, awake}; counts is {found, read, missing} once it ended."""
        return dict(state["clipfaces"], awake=awake.held())

    @app.get("/api/days")
    def days():
        """The shoot day by day, oldest first: {day, n, videos, bytes}. A file with no capture time counts on
        the day it was written, so nothing falls out of the timeline. No filters: this is the whole shoot, the
        way the category counts are."""
        if state["root"] is None:
            return {"days": []}
        return {"days": ix().days()}

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

    # ===== places: where the shoot was shot. Latitude and longitude are read from each file's own metadata
    # while indexing; this pass is the catch up for a shoot indexed before that, and the map reads the piles.
    # Nothing here reaches the network: a place gets a name only from the city list shipped with the app. =====

    @app.get("/api/places")
    def places():
        """{clusters, located, unlocated, total, read_at, bounds}. clusters are 25 km piles, biggest first,
        each with the box it covers so a click can filter to exactly it, and with the name and country of
        the nearest shipped city (both null when there is none within places.NAME_KM). One place is one
        row: piles that resolve to the same city are merged, so a city on a grid edge is not two rows.
        bounds is the box around every located file (null when nothing is located), where the map opens."""
        if state["root"] is None:
            return {"clusters": [], "located": 0, "unlocated": 0, "total": 0, "read_at": None, "bounds": None}
        st = places_mod.status(state["root"])
        pts = ix().points()
        bounds = None
        if pts:
            lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
            bounds = {"south": min(lats), "west": min(lons), "north": max(lats), "east": max(lons)}
        return dict(st, clusters=places_mod.cluster(pts), bounds=bounds)

    @app.post("/api/places/read")
    def start_places():
        """Read the location out of every ok row that has none, from the file's own metadata. 409 while
        indexing, categorising, checking focus or exporting. {"started": true, "total": n}."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot read locations while scanning")
        if state["classify"]["running"]:
            raise HTTPException(409, "cannot read locations while categorising")
        if state["focus"]["running"]:
            raise HTTPException(409, "cannot read locations while the focus pass is running")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot read locations while an export is running")
        with state["places_lock"]:
            if state["places"]["running"]:
                raise HTTPException(409, "already reading locations")
            root_at_start = state["root"]
            total = places_mod.status(root_at_start)["unlocated"]
            state["places"] = {"running": True, "done": 0, "total": total, "counts": {}, "error": None}

        def prog(d):
            state["places"].update(done=d.get("done", 0), total=d.get("total", total))

        t0 = time.time()

        def _run_places():
            try:
                with awake.hold():
                    state["places"]["counts"] = places_mod.read_locations(root_at_start, progress=prog)
            except Exception as e:
                state["places"]["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["stale"] = True
                state["places"]["running"] = False
                c = state["places"]["counts"] or {}
                usage.log("places_read", found=c.get("found", 0), read=c.get("read", 0),
                          missing=c.get("missing", 0), seconds=round(time.time() - t0, 1),
                          error=state["places"]["error"])

        threading.Thread(target=_run_places, daemon=True).start()
        return {"started": True, "total": total}

    @app.get("/api/places/progress")
    def places_progress():
        """{running, done, total, counts, error, awake}; counts is {found, read, missing} once it ended."""
        return dict(state["places"], awake=awake.held())

    # ===== end places =====

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

    def _check_free(need: int, base: Path, hint: str = "Use links, or export fewer photos.",
                    headroom: int | None = None) -> None:
        """400 when a copy of `need` bytes would leave less than `headroom` (EXPORT_HEADROOM unless given)
        on the disk holding base. The project file passes bundle.project_headroom, so a Save is held to the
        same floor as the scan's preflight and a card the scan accepted does not refuse the Save after it."""
        free = shutil.disk_usage(base).free
        where = "on this Mac" if _is_default_base(base) else "on that disk"
        if need + (EXPORT_HEADROOM if headroom is None else headroom) > free:
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

    def _export_started(what: str, dest: str, files: int, route: str | None = None, body: dict | None = None, **extra) -> float:
        """Log export_start and hand back the clock; _export_finished logs export_done from state["export"] and,
        when route and body are given, writes the shoot's export history row (Run again sends body to route)."""
        usage.log("export_start", what=what, dest=dest, files=files, **extra)
        state["export_req"] = {"route": route, "body": body, "root": state["root"]} if route else None
        return time.time()

    def _export_finished(what: str, dest: str, t0: float, nbytes: int | None = None) -> None:
        ex = state["export"]
        usage.log("export_done", what=what, dest=dest, files=ex.get("done", 0), failed=ex.get("failed", 0),
                  skipped=ex.get("skipped", 0), bytes=ex.get("bytes") if ex.get("bytes") is not None else nbytes,
                  seconds=round(time.time() - t0, 1), error=ex.get("error"))
        # ===== export history: what went where, in the index, so the Scan tab can show it and run it again =====
        req = state.get("export_req")
        state["export_req"] = None
        if req and req.get("root") is not None:
            try:
                db.record_export(db.connect(req["root"]), what, dest, req["route"], req["body"], ex.get("path"),
                                 ex.get("done", 0), ex.get("failed", 0), ex.get("skipped", 0), ex.get("error"))
            except sqlite3.Error as e:
                usage.log("api_error", route=req["route"], what="record_export", error=str(e))
        # ===== end export history =====

    # ===== export summary: what is about to happen, before the button (Q4) =====
    def _record_export_rate(root: Path, req: ExportReq, nbytes: int | None, t0: float) -> None:
        """Time a finished selection copy for the next summary (export.record_rate). Only a clean run says
        anything: a skipped file was never read and a failed one may have stopped half way. A web-size run is
        timed per photo only when every file in it was a photo re-encoded (no clips, no RAW riding along)."""
        from . import export as export_mod
        ex = state["export"]
        if req.mode != "copy" or ex.get("failed") or ex.get("skipped"):
            return
        conn = db.connect(root)
        seconds = time.time() - t0
        if req.web_size:
            kinds = export_mod.rows_for_ids(root, req.ids)
            if req.include_raw or any(Path(r["rel"]).suffix.lower() not in STD_EXTS for r in kinds):
                return
            export_mod.record_rate(conn, 0, seconds, int(ex.get("done") or 0), web=True)
        elif nbytes:
            export_mod.record_rate(conn, nbytes, seconds, int(ex.get("done") or 0), web=False)

    @app.post("/api/export/plan")
    def export_plan(req: ExportReq):
        """The line above the Export button: {n, photos, videos, files, web, bytes, dest, dest_error, free, fits,
        seconds, basis}. files counts RAW siblings when include_raw; web is how many photos a web size would
        re-encode; bytes is what is read off the shoot disk; basis is "measured" when the time comes from this
        shoot's own last export, "estimate" when it is the conservative constant (export.COPY_BPS). Writes
        nothing and creates no folder (the default destination excepted, as every export creates it)."""
        from . import export as export_mod
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        root = state["root"]
        rows = export_mod.rows_for_ids(root, req.ids)
        raws = [r["sibling"] for r in rows if req.include_raw and req.mode == "copy" and r["sibling"]]
        main = sum(int(r["size"] or 0) for r in rows)
        sibling = export_mod.jobs_bytes(root, [(0, rel, "") for rel in raws])      # RAW sizes are not in the index
        resized = [r for r in rows if req.mode == "copy" and req.web_size and Path(r["rel"]).suffix.lower() in STD_EXTS]
        videos = sum(1 for r in rows if r["kind"] == "video")
        out = {"n": len(rows), "photos": len(rows) - videos, "videos": videos, "files": len(rows) + len(raws), "web": len(resized),
               "bytes": main + sibling, "dest": None, "dest_error": None, "free": None, "fits": None}
        try:
            base = _resolve_base()
            out["dest"] = str(export_mod.export_dir(root, req.name or "export", base))
            out["free"] = shutil.disk_usage(base).free
            out["fits"] = req.mode != "copy" or out["bytes"] + EXPORT_HEADROOM <= out["free"]
        except HTTPException as e:
            out["dest_error"] = e.detail
        except ValueError as e:
            out["dest_error"] = str(e)
        copied = main + sibling - sum(int(r["size"] or 0) for r in resized)
        out["seconds"], out["basis"] = export_mod.estimate_seconds(db.connect(root), req.mode, copied, len(resized))
        return out
    # ===== end export summary =====

    @app.post("/api/export")
    def export(req: ExportReq):
        """The selection under <destination>/<shoot>/<name>. mode copy, symlink or csv; web_size (copies only)
        re-encodes photos to that long edge, include_raw puts each RAW sibling next to its JPEG. total in the
        reply counts photos; progress counts RAW siblings too."""
        if req.mode not in ("copy", "symlink", "csv"):
            raise HTTPException(400, "mode must be copy, symlink or csv")
        if req.web_size is not None and req.web_size < 100:
            raise HTTPException(400, "web size must be at least 100 px")
        if req.web_size and req.mode != "copy":
            raise HTTPException(400, "only a copy can be resized; links and the CSV point at the originals")
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["export"]["running"]:
                raise HTTPException(409, "an export is already running")
            base = _resolve_base()
            nbytes = None
            if req.mode == "copy":
                nbytes = jobs_bytes(root_at_start, ids_jobs(root_at_start, req.ids, req.include_raw)) if req.include_raw else export_bytes(root_at_start, req.ids)
                _check_free(nbytes, base)
            try:
                from .export import export_dir
                export_dir(root_at_start, req.name, base)   # validate now so a bad name or base is a 400, not a background error
            except ValueError as e:
                raise HTTPException(400, str(e))
            state["export"] = {"running": True, "done": 0, "total": len(req.ids), "failed": 0, "skipped": 0, "path": None, "error": None}
        t0 = _export_started("selection", "local", len(req.ids), route="/api/export", body=req.model_dump(), mode=req.mode,
                             web_size=req.web_size, include_raw=req.include_raw)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    state["export"]["path"] = str(export_ids(root_at_start, req.ids, req.name, req.mode, progress=prog, base=base,
                                                             include_raw=req.include_raw, web_size=req.web_size))
                try:
                    _record_export_rate(root_at_start, req, nbytes, t0)
                except Exception as e:                 # a timing that cannot be kept never fails the export
                    usage.log("api_error", route="/api/export", what="record_rate", error=str(e))
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
        t0 = _export_started("categories", "local", n_photos, route="/api/export/categories", body=req.model_dump(), mode=req.mode,
                             categories=req.categories, discovered=len(req.discovered or []), include_raw=req.include_raw,
                             include_unsure=req.include_unsure, videos=req.videos, drone=req.drone, hide_bad=req.hide_bad)

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
        t0 = _export_started("people", "local", n_photos, route="/api/export/references", body=req.model_dump(), mode=req.mode,
                             people=len(folders), include_raw=req.include_raw, min_sim=min_sim)

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

    # ===== export presets and history: the preset the shoot last used (meta), the last 10 exports (table exports) =====
    PRESETS = {"web": {"mode": "copy", "web_size": 2048, "include_raw": False},
               "links": {"mode": "symlink", "web_size": None, "include_raw": False},
               "full": {"mode": "copy", "web_size": None, "include_raw": True}}

    @app.get("/api/export/prefs")
    def export_prefs():
        """{preset, mode, web_size, include_raw} as last set for this shoot; the "web" preset until set."""
        out = dict(preset="web", **PRESETS["web"])
        if state["root"] is None:
            return out
        raw = db.get_meta(db.connect(state["root"]), "export_prefs")
        try:
            saved = json.loads(raw) if raw else {}
        except ValueError:
            saved = {}
        if isinstance(saved, dict) and saved.get("preset") in ("web", "links", "full", "custom"):
            out["preset"] = saved["preset"]
            if saved["preset"] in PRESETS:
                out.update(PRESETS[saved["preset"]])
            else:
                out["mode"] = saved.get("mode") if saved.get("mode") in ("copy", "symlink", "csv") else "copy"
                ws = saved.get("web_size")
                out["web_size"] = int(ws) if isinstance(ws, int) and not isinstance(ws, bool) and ws >= 100 else None
                out["include_raw"] = bool(saved.get("include_raw"))
        return out

    @app.post("/api/export/prefs")
    def set_export_prefs(req: ExportPrefsReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if req.preset not in ("web", "links", "full", "custom"):
            raise HTTPException(400, "preset must be web, links, full or custom")
        if req.mode not in ("copy", "symlink", "csv"):
            raise HTTPException(400, "mode must be copy, symlink or csv")
        if req.web_size is not None and req.web_size < 100:
            raise HTTPException(400, "web size must be at least 100 px")
        body = req.model_dump() if req.preset == "custom" else dict(preset=req.preset, **PRESETS[req.preset])
        db.set_meta(db.connect(state["root"]), "export_prefs", json.dumps(body))
        usage.log("export_preset", preset=req.preset)
        return body

    @app.get("/api/exports")
    def exports_history():
        """The last 10 exports of this shoot, newest first: what, where (path, or the Drive folder link), when,
        the counts, and exists (the folder is still there, so Show in Finder can open it)."""
        if state["root"] is None:
            return {"exports": []}
        rows = db.list_exports(db.connect(state["root"]))
        for r in rows:
            r["exists"] = bool(r["dest"] == "local" and r["path"] and Path(r["path"]).exists())
            r["options"] = _export_options(r)
            r["at"] = r["at"].replace(" ", "T") + "Z" if r["at"] and "T" not in r["at"] else r["at"]   # sqlite's UTC clock, as ISO
            r.pop("req", None)
        return {"exports": rows}

    def _export_options(row: dict) -> str:
        """The request in a few words: "copy, 2048 px, with RAW", "links", "CSV", "Google Drive, 2048 px", "links, segments"."""
        b = row.get("req") or {}
        parts = []
        if row.get("dest") == "drive":
            parts.append("Google Drive")
        else:
            parts.append({"copy": "copy", "symlink": "links", "csv": "CSV"}.get(b.get("mode"), ""))
        if b.get("web_size"):
            parts.append(f"{b['web_size']} px")
        if b.get("include_raw"):
            parts.append("with RAW")
        if b.get("videos") == "segments":
            parts.append("segments")
        if b.get("include_unsure"):
            parts.append("less sure too")
        return ", ".join(p for p in parts if p)

    @app.post("/api/exports/{eid}/run")
    def export_again(eid: int):
        """Send the export's request to its route again: the same ids, categories or people, the same options,
        the destination as it is now. Same 400 and 409 answers as the route itself."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        row = db.get_export(db.connect(state["root"]), eid)
        if row is None:
            raise HTTPException(404, "no such export")
        runner = EXPORT_RUNNERS.get(row["route"])
        if runner is None:
            raise HTTPException(400, "that export cannot be run again")
        try:
            out = runner(row["req"])
        except ValidationError as e:
            raise HTTPException(400, f"that export's request no longer fits: {e.errors()[0].get('msg', 'bad field')}")
        usage.log("export_again", what=row["what"], dest=row["dest"])
        return dict(out, what=row["what"], dest=row["dest"])
    # ===== end export presets and history =====

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
        t0 = _export_started(req.what, "drive", len(jobs), route="/api/drive/export", body=req.model_dump(), web_size=req.web_size,
                             skip_videos=req.skip_videos, include_raw=req.include_raw)

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

    # ===== export history: Run again sends the stored body to the route it came from =====
    EXPORT_RUNNERS = {
        "/api/export": lambda body: export(ExportReq(**body)),
        "/api/export/categories": lambda body: export_categories_api(CategoriesExportReq(**body)),
        "/api/export/references": lambda body: export_references_api(ReferencesExportReq(**body)),
        "/api/drive/export": lambda body: drive_export(DriveExportReq(**body)),
    }
    # ===== end export history =====

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

    def _start_bundle_export(dest: Path | None, remember: bool = True, what: str = "bundle") -> dict:
        """Pack the open shoot's index into <dest>/sorted_<shoot>.sorted in the background.
        dest None means the export destination. A dest inside the shoot root is refused (bundle_path
        raises; the root itself is allowed, it is where the file lives by default), as is one that is not a folder. remember False writes the file and nothing
        else: the shoot's remembered folder and its last-saved stamp stay where they were (Save a copy)."""
        from . import bundle
        with state["export_lock"]:
            root_at_start = state["root"]
            if root_at_start is None:
                raise HTTPException(400, "no folder open")
            if state["running"]:
                raise HTTPException(409, "cannot save the project while scanning")
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
            _check_free(nbytes, base, hint="Free some space there first.", headroom=bundle.project_headroom(nbytes))
            state["export"] = {"running": True, "done": 0, "total": n_files, "failed": 0, "skipped": 0, "path": None, "error": None, "what": what}
        t0 = _export_started(what, "local", n_files)

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                with awake.hold():
                    out = bundle.export_bundle(root_at_start, base, progress=prog)
                    state["export"]["path"] = str(out)
                    if remember:
                        _remember_project(root_at_start, out)
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False
                _export_finished(what, "local", t0, nbytes)

        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": n_files, "dest": str(base)}

    @app.post("/api/bundle/export")
    def export_bundle_api(req: BundleExportReq | None = None):
        return _start_bundle_export(Path(req.dest) if req and req.dest else None)

    # ===== the project file: sorted_<shoot>.sorted, saved to one remembered place (Save), or wherever
    # the picker says (Save to). The place is kept in the shoot's own index (project_path), so the next
    # Save, and the next Mac that loads the file, know where it lives. =====
    def _remember_project(root: Path, path: Path, saved: bool = True, skeleton: bool = False) -> None:
        """saved False: the file was opened, not written; the save time inside it (the other Mac's) stands,
        and project_opened marks it, so the card says "opened from this file" instead of claiming this Mac
        saved it. The next real save clears the mark. skeleton True: only the placeholder written before a
        scan is there (bundle.export_bundle thumbs=False); the place is remembered but no save time is
        claimed, so a scan whose save then fails does not leave the card saying "Saved"."""
        conn = db.connect(root)
        try:
            db.set_meta(conn, "project_path", str(path))
            db.set_meta(conn, "project_opened", "0" if saved else "1")
            db.set_meta(conn, "project_skeleton", "1" if skeleton else "0")
            if saved and not skeleton:
                db.set_meta(conn, "project_saved_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            conn.close()

    def _project_info(root: Path) -> dict:
        """name: the file name for this shoot. path: where it was last saved or loaded from (None until then),
        exists: that file is there right now. dir: where Save writes next (the remembered folder, else the
        shoot folder itself, see bundle.default_project_dir). saved_at: the last save on this Mac. opened: the remembered file was
        opened here, not written here, so saved_at (if any) came from the Mac that made it."""
        from . import bundle
        conn = db.connect(root)
        try:
            remembered = db.get_meta(conn, "project_path")
            saved_at = db.get_meta(conn, "project_saved_at")
            opened = db.get_meta(conn, "project_opened") == "1"
            skeleton = db.get_meta(conn, "project_skeleton") == "1"
        finally:
            conn.close()
        rp = Path(remembered) if remembered else None
        # The same place Save and the scan write to, so the card never names a folder the button would not
        # use: the remembered folder while it is there, else the shoot folder (Save asks when that fails).
        d = _scan_project_dir(root)
        try:
            d = d.resolve()
        except OSError:
            pass
        return {"name": bundle.project_name(root), "path": str(rp) if rp else None,
                "exists": bool(rp and rp.is_file()), "dir": str(d), "saved_at": saved_at,
                "opened": bool(rp and opened), "skeleton": bool(rp and skeleton),
                "default_dir": str(bundle.default_project_dir(root))}

    @app.get("/api/project")
    def get_project():
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        return _project_info(state["root"])

    @app.post("/api/project/save")
    def save_project():
        """Save: the project file into its remembered folder (or the shoot folder itself), no picker, after
        the same checks a scan makes: a real write there and room for the file. When the place cannot take it
        (a read-only or full disk, the disk unplugged) nothing is written and nothing is remembered; the reply
        is {started: false, blocked} at 200, as /api/index does, and the UI asks for a place (Save to). Save
        never falls back to ~/Documents on its own: a file the person did not choose a place for is a file
        they cannot find."""
        from . import bundle
        root = state["root"]
        if root is None:
            raise HTTPException(400, "no folder open")
        d = _scan_project_dir(root)
        blocked = _project_preflight(root, d, need_file=bundle.bundle_bytes(root))
        if blocked:
            usage.log("save_blocked", reason=blocked["reason"])
            return {"started": False, "blocked": blocked}
        return _start_bundle_export(d)

    # ===== Save a copy: the same file written somewhere else, and nothing about the shoot moves. Save to
    # picks a new home and remembers it; a copy is a hand-off (one for the editor, one for the archive) and
    # leaves both the remembered folder and the last-saved stamp alone. The folder Save project writes to is
    # refused, because a copy landing there would overwrite the real save under the old stamp. =====
    def _copy_gate(dest: Path) -> None:
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        home = Path(_project_info(state["root"])["dir"]).resolve()
        try:
            picked = dest.expanduser().resolve()
        except OSError:
            return
        if picked == home:
            raise HTTPException(400, "that is the folder Save project already writes to; use Save project instead")
        # The shoot folder is where the real project file lives by default; a copy there would take its name.
        if picked == state["root"].resolve():
            raise HTTPException(400, "a copy cannot go in the shoot folder; that is where the project file itself lives")

    @app.post("/api/bundle/copy")
    def copy_bundle_api(req: BundleExportReq):
        if not req.dest:
            raise HTTPException(400, "a copy needs a folder to go in")
        _copy_gate(Path(req.dest))
        return _start_bundle_export(Path(req.dest), remember=False, what="copy")

    @app.post("/api/bundle/copy/choose")
    def copy_bundle_choose():
        """Save a copy: the folder picker, opening on the export destination, then one copy written there.
        The shoot's remembered folder and last-saved stamp are untouched."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot save the project while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")
        start = _resolve_base()
        while not start.is_dir() and start.parent != start:
            start = start.parent
        quoted = str(start).replace("\\", "\\\\").replace('"', '\\"')
        path_str = _run_picker(f'POSIX path of (choose folder with prompt "Save a copy of the project file where?" default location (POSIX file "{quoted}"))', "folder")
        if path_str is None:
            return Response(status_code=204)
        _copy_gate(Path(path_str))
        return _start_bundle_export(Path(path_str), remember=False, what="copy")
    # ===== end Save a copy =====

    @app.post("/api/bundle/export/choose")
    def export_bundle_choose():
        """Save to: the native folder picker, opening on the project folder (or the nearest folder that
        exists), then the export into the pick."""
        # Same gates as the export, checked up front so nobody sits through the picker for an error.
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["running"]:
            raise HTTPException(409, "cannot save the project while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")
        start = Path(_project_info(state["root"])["dir"])
        while not start.is_dir() and start.parent != start:
            start = start.parent
        quoted = str(start).replace("\\", "\\\\").replace('"', '\\"')
        path_str = _run_picker(f'POSIX path of (choose folder with prompt "Save the project file where?" default location (POSIX file "{quoted}"))', "folder")
        if path_str is None:
            return Response(status_code=204)
        return _start_bundle_export(Path(path_str))

    @app.get("/api/launch")
    def launch_request():
        """The project file the app was started with (a double-click in the Finder), once: the UI loads it
        the way Open project does, and a reload of the page does not load it again."""
        f = state["open_file"]; state["open_file"] = None
        return {"open": f}
    # ===== end project file =====

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

    # ===== show in the Finder: the shoot's own files, picked in the grid. The UI sends ids, never paths;
    # the path comes from the index, so nothing outside the open shoot can be revealed. =====
    REVEAL_MAX = 20

    @app.post("/api/reveal/ids")
    def reveal_ids(req: RevealIdsReq):
        """open -R on the selected photos and clips: the Finder opens their folder with them highlighted.
        Up to 20 at a time (the Finder opens a window per folder). Files that are not there right now
        (disk unplugged, moved) are reported, not raised."""
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        ids = list(dict.fromkeys(req.ids))[:REVEAL_MAX]
        if not ids:
            raise HTTPException(400, "nothing selected")
        conn = db.connect(state["root"])
        rows = conn.execute(f"SELECT id, rel FROM photos WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
        paths, missing = [], 0
        for r in rows:
            p = state["root"] / r["rel"]
            if p.is_file():
                paths.append(str(p))
            else:
                missing += 1
        missing += len(ids) - len(rows)
        if not paths:
            raise HTTPException(400, "not on the disk right now" if missing else "nothing to show")
        try:
            subprocess.run(["open", "-R", *paths], capture_output=True, text=True, timeout=20, check=False)
        except FileNotFoundError:
            raise HTTPException(501, "the Finder is not available here")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "the Finder did not answer")
        usage.log("reveal", n=len(paths))
        return {"revealed": len(paths), "missing": missing, "max": REVEAL_MAX}
    # ===== end show in the Finder =====

    def _import_gates() -> None:
        # Same gates as _switch_root, checked up front so nobody sits through a picker for a 409.
        if state["running"]:
            raise HTTPException(409, "cannot open a project while scanning")
        if state["export"]["running"]:
            raise HTTPException(409, "cannot open a project while an export is running")

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
            raise HTTPException(400, "that project file is not readable")
        out = _switch_root(root)
        _remember_project(root, zip_path.resolve(), saved=False)   # Save writes back beside the file that was opened
        # scan: from the installed index itself (an older bundle has no "scan" in its bundle.json), so the
        # UI can say "2,080 of 4,315 scanned, continue?" instead of quietly showing fewer items.
        scan = _scan_block(db.connect(root))
        usage.log("bundle_import", photos=info.get("photos"), complete=scan["complete"])
        return dict(out, imported=True, root=str(root), photos=info.get("photos"), scan=scan)

    @app.post("/api/bundle/import/choose")
    def import_bundle_choose():
        _import_gates()
        path_str = _run_picker('POSIX path of (choose file with prompt "Open a sorted project file (sorted_<shoot>.sorted)")', "project file")
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

    # ===== version and the opt-in update check (photosort/version.py) =====
    @app.get("/api/version")
    def version_info():
        """The version and build, whether the daily check is on, and what it last found. When the check is on
        and a day has passed, one runs in the background now; checking says so and the next call has the answer."""
        started = version_mod.check_in_background()
        st = version_mod.status()
        st["checking"] = st["checking"] or started
        return st

    @app.post("/api/version/check")
    def version_check(req: UpdateCheckReq):
        """Turn the daily check on (one check runs now, the answer comes back) or off (the answer is forgotten)."""
        settings.set_update_enabled(req.enabled)
        usage.log("update_check", enabled=req.enabled)
        if not req.enabled:
            return version_mod.status()
        return version_mod.check_now()
    # ===== end version =====

    return app
