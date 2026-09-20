from __future__ import annotations
import json, multiprocessing as mp, time
from pathlib import Path
from typing import Callable
import numpy as np
from PIL import Image
from . import db
from .config import PREVIEW_EDGE, GRID_EDGE, THUMB_QUALITY, JPEG_WORKERS, RAW_WORKERS, VIDEO_WORKERS, VIDEO_EXTS, EMBED_BATCH, YUNET_PATH, SFACE_PATH
from .walk import find_images, quick_hash
from .decode import load_preview
from .features import phash, exif_info, sharpness_tiles, to_gray

class SourceUnavailable(RuntimeError):
    """The shoot root is not there (disk unplugged, wrong mount) while the index already holds photos.
    Raised before any write so the saved index is left exactly as it was."""

_ENGINE = None
def _face_engine():
    global _ENGINE
    if _ENGINE is None:
        from .faces import FaceEngine
        _ENGINE = FaceEngine()
    return _ENGINE

def _process_video(root: str, rel: str, out: dict) -> None:
    """A clip: ffprobe for the facts, sampled frames for the thumb, the grid, the sharpness and the
    embedding (done later, in the embed stage), one segment row per scene. No face detection."""
    from . import video
    path = Path(root) / rel
    qh = quick_hash(path)
    info = video.probe(path)
    frames, segs = video.sample_frames(path, info["duration"], key=(info["codec"], info["pix_fmt"]))
    # A clip whose Sony sidecar says S-Log3 gets every sampled frame converted to Rec.709 before anything
    # is saved: thumb, grid and frames/ all show (and embed) a normal-contrast picture. Display only.
    if video.is_slog3(video.capture_gamma(path)):
        frames = [(t, video.slog3_to_rec709(fr)) for t, fr in frames]
    idx = db.index_dir(Path(root))
    mid = min(range(len(frames)), key=lambda k: abs(frames[k][0] - info["duration"] / 2))
    im = frames[mid][1]
    im.save(idx / "thumbs" / f"{qh}.jpg", quality=THUMB_QUALITY)
    g = im.copy(); g.thumbnail((GRID_EDGE, GRID_EDGE)); g.save(idx / "grid" / f"{qh}.jpg", quality=80)
    for k, (_, fr) in enumerate(frames):
        fr.save(idx / "frames" / f"{qh}_{k}.jpg", quality=THUMB_QUALITY)
    p90, mx = sharpness_tiles(to_gray(im))
    st = path.stat()
    out["row"] = dict(rel=rel, size=st.st_size, mtime=st.st_mtime, qhash=qh, sibling=None,
        width=info["width"] or im.width, height=info["height"] or im.height,
        taken_at=info["taken_at"] or video.mtime_iso(st.st_mtime), camera=info["camera"], phash=phash(im),
        sharp_tile=p90, sharp_max=mx, sharp_eye=None, sharp=p90, n_faces=0, status="ok",
        kind="video", duration=info["duration"], aerial=int(info["aerial"]))
    out["segments"] = []
    for i, (a, b) in enumerate(segs):
        k = min(range(len(frames)), key=lambda k: abs(frames[k][0] - (a + b) / 2))
        out["segments"].append(dict(idx=i, start=a, end=b, frame=f"{qh}_{k}.jpg"))

def process_one(args: tuple[str, str, bool]) -> dict:
    root, rel, want_faces = args
    path = Path(root) / rel
    out = {"rel": rel, "row": None, "faces": [], "segments": [], "error": None}
    try:
        if path.suffix.lower() in VIDEO_EXTS:
            _process_video(root, rel, out)
            return out
        qh = quick_hash(path)
        im = load_preview(path, PREVIEW_EDGE)
        idx = db.index_dir(Path(root))
        im.save(idx / "thumbs" / f"{qh}.jpg", quality=THUMB_QUALITY)
        g = im.copy(); g.thumbnail((GRID_EDGE, GRID_EDGE)); g.save(idx / "grid" / f"{qh}.jpg", quality=80)
        gray = to_gray(im)
        p90, mx = sharpness_tiles(gray)
        info = exif_info(path)
        faces = _face_engine().detect(im) if want_faces else []
        eye = max((f.eye_sharp for f in faces), default=None)
        st = path.stat()
        # n_faces stays NULL when faces were not looked for, so a later faces=True
        # run knows to come back for this photo.
        out["row"] = dict(rel=rel, size=st.st_size, mtime=st.st_mtime, qhash=qh, sibling=None,
            width=info["width"] or im.width, height=info["height"] or im.height, taken_at=info["taken_at"],
            camera=info["camera"], phash=phash(im), sharp_tile=p90, sharp_max=mx, sharp_eye=eye,
            sharp=eye if eye is not None else p90, n_faces=len(faces) if want_faces else None, status="ok", kind="photo",
            aerial=int(info["aerial"]))
        out["faces"] = [dict(x=f.x, y=f.y, w=f.w, h=f.h, score=f.score, landmarks=json.dumps(f.landmarks.tolist()),
                             eye_sharp=f.eye_sharp, embed=f.embed.astype(np.float32).tobytes()) for f in faces]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out

def faces_one(args: tuple[str, str, str]) -> dict:
    """The faces-only pass for a photo indexed with faces off: detect on the stored 1024 px thumb (the very
    image the index ran detection on when faces are on, so boxes and landmarks line up), nothing else.
    Falls back to a decode of the source only when the thumb is gone."""
    root, rel, qh = args
    out = {"rel": rel, "faces": [], "eye": None, "error": None}
    try:
        p = db.index_dir(Path(root)) / "thumbs" / f"{qh}.jpg"
        im = Image.open(p) if p.is_file() else load_preview(Path(root) / rel, PREVIEW_EDGE)
        faces = _face_engine().detect(im)
        out["eye"] = max((f.eye_sharp for f in faces), default=None)
        out["faces"] = [dict(x=f.x, y=f.y, w=f.w, h=f.h, score=f.score, landmarks=json.dumps(f.landmarks.tolist()),
                             eye_sharp=f.eye_sharp, embed=f.embed.astype(np.float32).tobytes()) for f in faces]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out

def index_folder(root: Path, faces: bool = True, workers: int | None = None,
                 progress: Callable[[dict], None] | None = None, embed: bool = True,
                 retry_errors: bool = False) -> dict:
    t0 = time.time(); root = Path(root)
    _raw = progress or (lambda d: None)
    stage = {"name": None, "t": t0}
    def notify(d: dict) -> None:
        if d["stage"] != stage["name"]:
            stage["name"], stage["t"] = d["stage"], time.time()
        _raw(dict(d, stage_started=stage["t"]))
    if faces and not (YUNET_PATH.exists() and SFACE_PATH.exists()):
        raise FileNotFoundError("face models missing; run scripts/fetch_models.sh")
    conn = db.connect(root)
    notify({"stage": "scan", "done": 0, "total": 0})
    n_ok = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
    if not root.is_dir():
        raise SourceUnavailable(f"{root} is not there. Plug the disk in; the saved index ({n_ok} photos) was left untouched.")
    files = find_images(root)
    if not files and n_ok > 0:
        raise SourceUnavailable(f"{root} has no photos right now. Is the disk mounted? The saved index ({n_ok} photos) was left untouched.")
    known = db.known_files(conn, retry_errors=retry_errors)
    missing = db.missing_files(conn)
    idx = db.index_dir(root)
    # A file that went missing and came back unchanged, with its thumb still on the Mac, needs no re-decode.
    restore = [f.rel for f in files if f.rel in missing and missing[f.rel][:2] == (f.size, f.mtime)
               and (idx / "thumbs" / f"{missing[f.rel][2]}.jpg").is_file()]
    if restore:
        db.restore_missing(conn, restore)
        known.update({r: missing[r][:2] for r in restore})
    # A changed (or new) file is decoded in full. An unchanged photo indexed with faces off only needs the
    # face pass, run on its stored thumb: its row, embedding, category, cluster and focus label all stay.
    need_faces = db.photos_without_faces(conn) if faces else set()
    todo = [f for f in files if known.get(f.rel) != (f.size, f.mtime)]
    changed = {f.rel for f in todo}
    face_todo = [f for f in files if f.rel in need_faces and f.rel not in changed]
    stats = dict(total=len(files), skipped=len(files) - len(todo) - len(face_todo), indexed=0, faced=0, errors=0, embedded=0)
    db.mark_missing(conn, {f.rel for f in files})
    if face_todo:
        qh = {r[0]: r[1] for r in conn.execute("SELECT rel, qhash FROM photos WHERE n_faces IS NULL AND status='ok'")}
        ctx = mp.get_context("spawn")
        done = 0
        with ctx.Pool(min(workers or JPEG_WORKERS, JPEG_WORKERS, len(face_todo))) as pool:
            for res in pool.imap_unordered(faces_one, [(str(root), f.rel, qh[f.rel]) for f in face_todo], chunksize=2):
                if res["error"]:
                    stats["errors"] += 1
                else:
                    db.set_faces_only(conn, res["rel"], res["faces"], res["eye"])
                    stats["faced"] += 1
                done += 1
                notify({"stage": "faces", "done": done, "total": len(face_todo)})
    if todo:
        sib = {f.rel: f.sibling for f in todo}
        meta = {f.rel: (f.size, f.mtime) for f in todo}
        ctx = mp.get_context("spawn")
        done = 0
        def _store(res):
            nonlocal done
            if res["error"]:
                stats["errors"] += 1
                db.mark_error(conn, res["rel"], meta[res["rel"]][0], meta[res["rel"]][1])
            else:
                res["row"]["sibling"] = sib.get(res["rel"])
                pid = db.upsert_photo(conn, res["row"])
                db.replace_faces(conn, pid, res["faces"])
                db.replace_segments(conn, pid, res.get("segments", []))
                stats["indexed"] += 1
            done += 1
            notify({"stage": "features", "done": done, "total": len(todo)})
        # RAW decodes hold ~10x the memory of a JPEG preview, so RAWs always run in a
        # smaller pool no matter what the caller asked for; videos each drive a multi-threaded
        # ffmpeg, so they get their own small pool last. Three sequential pools, one counter.
        std = [f for f in todo if not f.is_raw and not f.is_video]
        raw = [f for f in todo if f.is_raw]; vid = [f for f in todo if f.is_video]
        for group, cap in ((std, JPEG_WORKERS), (raw, RAW_WORKERS), (vid, VIDEO_WORKERS)):
            if not group:
                continue
            n = min(workers or cap, cap, len(group))
            with ctx.Pool(n) as pool:
                for res in pool.imap_unordered(process_one, [(str(root), f.rel, faces) for f in group], chunksize=2):
                    _store(res)
    if embed:
        from .embed import get_embedder
        rows = conn.execute("SELECT id, qhash, kind FROM photos WHERE embed IS NULL AND status='ok' ORDER BY id").fetchall()
        photos = [(r[0], r[1]) for r in rows if r[2] != "video"]
        videos = [(r[0], r[1]) for r in rows if r[2] == "video"]
        segs = db.segments_missing_embed(conn)
        vid_qh = {r[0]: r[1] for r in conn.execute("SELECT id, qhash FROM photos WHERE kind='video' AND status='ok'")}
        total = len(photos) + len(videos) + len(segs); done = 0
        E = get_embedder()
        for i in range(0, len(photos), EMBED_BATCH):
            batch = photos[i:i + EMBED_BATCH]
            ims = [Image.open(idx / "thumbs" / f"{qh}.jpg") for _, qh in batch]
            vecs = E.encode_images(ims)
            for (pid, _), v in zip(batch, vecs):
                db.set_embed(conn, pid, v)
            conn.commit()
            stats["embedded"] += len(batch); done += len(batch)
            notify({"stage": "embed", "done": done, "total": total})
        # A clip's embedding is the mean of its sampled frames, renormalised. An index that lost its
        # frames/ (an imported bundle) falls back to the thumb, which is the middle frame.
        for pid, qh in videos:
            frames = sorted((idx / "frames").glob(f"{qh}_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
            if not frames:
                frames = [idx / "thumbs" / f"{qh}.jpg"]
            vecs = E.encode_images([Image.open(p) for p in frames])
            v = vecs.mean(axis=0); v /= (np.linalg.norm(v) + 1e-9)
            db.set_embed(conn, pid, v); conn.commit()
            stats["embedded"] += 1; done += 1
            notify({"stage": "embed", "done": done, "total": total})
        for i in range(0, len(segs), EMBED_BATCH):
            batch = segs[i:i + EMBED_BATCH]
            ims = []
            for _, pid, frame in batch:
                p = idx / "frames" / frame
                ims.append(Image.open(p if p.is_file() else idx / "thumbs" / f"{vid_qh[pid]}.jpg"))
            vecs = E.encode_images(ims)
            for (sid, _, _), v in zip(batch, vecs):
                db.set_segment_embed(conn, sid, v)
            conn.commit()
            done += len(batch)
            notify({"stage": "embed", "done": done, "total": total})
    stats["seconds"] = round(time.time() - t0, 1)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_index', datetime('now'))"); conn.commit()
    notify({"stage": "done", "done": stats["total"], "total": stats["total"]})
    return stats
