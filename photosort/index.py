from __future__ import annotations
import json, multiprocessing as mp, os, time
from pathlib import Path
from typing import Callable
import numpy as np
from PIL import Image
from . import db
from .config import PREVIEW_EDGE, GRID_EDGE, THUMB_QUALITY, JPEG_WORKERS, RAW_WORKERS, VIDEO_WORKERS, VIDEO_EXTS, EMBED_BATCH, YUNET_PATH, SFACE_PATH
from .walk import find_images, quick_hash
from .decode import load_preview
from .features import phash, exif_info, sharpness_tiles, to_gray
from .places import READ_KEY as _PLACES_READ_KEY

CURRENT_MAX = 120        # how much of the path in flight the progress payload carries

def _short_rel(rel: str | None, limit: int = CURRENT_MAX) -> str | None:
    """The path to show under the progress bar, cut from the LEFT: the tail is the folder that identifies
    the file ("...FURNITURE DEALEARS MEET (06-08-2026)/FINAL JPG/DSC01158.jpg"), the head is the part of a
    deep client card that says nothing. Whole path components survive where they can. This is a guard
    against a pathological path, not the display rule: the UI elides to its own width."""
    if rel is None or len(rel) <= limit:
        return rel
    keep: list[str] = []
    for part in reversed(rel.split("/")):
        if keep and len("/".join([part] + keep)) + 2 > limit:
            break
        keep.insert(0, part)
    tail = "/".join(keep)
    if len(tail) + 2 > limit:            # one enormous component: there is nothing to do but cut inside it
        tail = tail[-(limit - 2):]
    return "\u2026/" + tail

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

def clip_face_summary(per_frame: list[list], edges: list[int]) -> tuple[int, float | None]:
    """(people, face_h) for one clip from the faces found on each of its sampled frames; edges is each
    frame's SHORT edge in pixels (the height of a landscape frame, the width of a vertical one).

    people is the median of the per-frame face counts, rounded down, over every frame including the ones
    with no face. face_h is the median, over the frames that had a face, of the largest face's height as a
    share of that frame's short edge; None when no frame had one.

    Why the short edge and not the height: on a vertical phone clip the height is the long edge, so a waist
    up shot puts the face at about 0.12 of it where the same framing in landscape puts it near 0.24, and
    measuring against the height filed most vertical medium shots of the GT 1 TB event shoot as wide. Of 29
    clips there that the height rule had moved from medium to wide, checked by eye, 16 were wide; against
    the short edge 25 of the 29 come out right. A landscape frame's short edge is its height, so nothing
    measured on NSG_26 Part 2 moves.

    Both medians are measured choices, on the
    325 clips of NSG_26 Part 2 against hand counts of their segments: the median count matched 276 (84.9%)
    where the maximum over frames matched 255 (78.5%), because one stray detection or a passer-by in one
    frame of seven was enough to lift a single interview to "two people". Of the 91 clips with nobody in
    them, the median called all 91 empty."""
    counts = sorted(len(f) for f in per_frame)
    people = int(np.median(counts)) if counts else 0
    hs = [max(f.h for f in faces) / max(1, e) for faces, e in zip(per_frame, edges) if faces]
    return people, (round(float(np.median(hs)), 4) if hs else None)

def _clip_faces(frames: list[Image.Image]) -> tuple[int, float | None, list[dict]]:
    """YuNet over a clip's frames (the same engine and threshold photos use). Returns (people, face_h, rows
    for clip_faces). The frames are the ones saved under frames/, already converted out of S-Log, so the
    boxes line up with what is on disk."""
    eng = _face_engine()
    per_frame = [eng.detect(fr) for fr in frames]
    people, face_h = clip_face_summary(per_frame, [min(fr.size) for fr in frames])
    rows = [dict(frame=k, x=f.x, y=f.y, w=f.w, h=f.h, score=f.score, embed=f.embed.astype(np.float32).tobytes())
            for k, faces in enumerate(per_frame) for f in faces]
    return people, face_h, rows

def _process_video(root: str, rel: str, out: dict, want_faces: bool = False) -> None:
    """A clip: ffprobe for the facts, Sony's sidecar for the body, the picture profile and the capture rate,
    sampled frames for the thumb, the grid, the sharpness and the embedding (done later, in the embed
    stage), one segment row per scene. With faces on, YuNet over those same frames gives the clip its head
    count and face size (clip_face_summary); n_faces stays 0 on a clip, see db.connect for why."""
    from . import video
    path = Path(root) / rel
    qh = quick_hash(path)
    info = video.probe(path)
    side = video.read_sidecar(path)
    frames, segs = video.sample_frames(path, info["duration"], key=(info["codec"], info["pix_fmt"]), fps=info["fps"])
    # A clip whose Sony sidecar says S-Log3 gets every sampled frame converted to Rec.709 before anything
    # is saved: thumb, grid and frames/ all show (and embed) a normal-contrast picture. Display only.
    if video.is_slog3(side["gamma"]):
        frames = [(t, video.slog3_to_rec709(fr)) for t, fr in frames]
    idx = db.index_dir(Path(root))
    mid = min(range(len(frames)), key=lambda k: abs(frames[k][0] - info["duration"] / 2))
    im = frames[mid][1]
    im.save(idx / "thumbs" / f"{qh}.jpg", quality=THUMB_QUALITY)
    g = im.copy(); g.thumbnail((GRID_EDGE, GRID_EDGE)); g.save(idx / "grid" / f"{qh}.jpg", quality=80)
    for k, (_, fr) in enumerate(frames):
        fr.save(idx / "frames" / f"{qh}_{k}.jpg", quality=THUMB_QUALITY)
    p90, mx = sharpness_tiles(to_gray(im))
    people, face_h, out["clip_faces"] = _clip_faces([fr for _, fr in frames]) if want_faces else (None, None, [])
    st = path.stat()
    out["row"] = dict(rel=rel, size=st.st_size, mtime=st.st_mtime, qhash=qh, sibling=None,
        width=info["width"] or im.width, height=info["height"] or im.height,
        # the container's make and model when it has them (phones, DJI), else the sidecar's (Sony writes
        # none into the container), else nothing
        taken_at=info["taken_at"] or video.mtime_iso(st.st_mtime), camera=info["camera"] or side["camera"], phash=phash(im),
        sharp_tile=p90, sharp_max=mx, sharp_eye=None, sharp=p90, n_faces=0, status="ok",
        kind="video", duration=info["duration"], aerial=int(info["aerial"]), lat=info["lat"], lon=info["lon"],
        # probe() has always computed this and the row has always dropped it. It costs nothing here (the
        # container header was already read) and it is the whole of the slow motion facet: see read_fps
        # for how a shoot indexed before this column catches up. 0.0 from _fps means "ffprobe would not
        # say", which is an absence, not a frame rate of zero.
        fps=info["fps"] or None,
        # '' rather than NULL: the sidecar was looked for and said nothing, so the catch-up pass has nothing
        # left to find here (see db.connect)
        gamma=side["gamma"] or "", capture_fps=side["capture_fps"], people=people, face_h=face_h)
    out["segments"] = []
    for i, (a, b) in enumerate(segs):
        k = min(range(len(frames)), key=lambda k: abs(frames[k][0] - (a + b) / 2))
        out["segments"].append(dict(idx=i, start=a, end=b, frame=f"{qh}_{k}.jpg"))

def process_one(args: tuple[str, str, bool]) -> dict:
    root, rel, want_faces = args
    path = Path(root) / rel
    out = {"rel": rel, "row": None, "faces": [], "segments": [], "clip_faces": [], "error": None}
    try:
        if path.suffix.lower() in VIDEO_EXTS:
            _process_video(root, rel, out, want_faces)
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
            aerial=int(info["aerial"]), lat=info["lat"], lon=info["lon"])
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
    # The embed stage is three sub-stages at different rates per item (a photo is one frame, a clip six to
    # thirty, a segment usually none), so each gets its own start time and _embed_stage sends a rate and an
    # estimate (eta_s) worked out per sub-stage: one made from the blended rate of the whole stage is too
    # optimistic during the clips and too pessimistic after them. _embed_stage sends the time each sub-stage
    # actually began; a sub-stage that does not is timed from its first report.
    sub = {"name": None, "t": t0}
    job = {"id": None, "written": 0.0}
    # The stages this pass will actually run, in order, filled in once the disk has been listed: faces only
    # runs when faces are on and something lacks them, features only when something changed, embed only when
    # asked for. The UI numbers its steps off this, because index_folder is the only place that knows which
    # stages are coming; a UI guessing from the stage name alone shows the same word twice and no position.
    plan: list[str] = []
    def notify(d: dict) -> None:
        if d["stage"] != stage["name"]:
            stage["name"], stage["t"] = d["stage"], time.time()
        # step is None for a stage outside the plan: the scan emit before the plan is known, and the
        # terminal "done", which is the end of the pass rather than a step of it.
        if d.get("substage") != sub["name"]:
            sub["name"], sub["t"] = d.get("substage"), d.get("substage_started") or time.time()
        step = plan.index(d["stage"]) + 1 if d["stage"] in plan else None
        # current is the file in flight, relative to the root, or None for a stage that is not reading the
        # disk. Every key is always present so the panel never has to ask whether it is there.
        _raw(dict(d, stage_started=stage["t"], step=step, steps=len(plan) or None,
                  current=_short_rel(d.get("current")), substage=d.get("substage"),
                  substage_done=d.get("substage_done"), substage_total=d.get("substage_total"),
                  substage_started=sub["t"] if d.get("substage") else None,
                  rate=d.get("rate"), eta_s=d.get("eta_s")))
        # The job row keeps the last progress, written at most every 2 s (the features pass already commits per file).
        now = time.time()
        if job["id"] is not None and (now - job["written"] >= 2.0 or d["stage"] == "done"):
            job["written"] = now
            db.job_progress(conn, job["id"], {"stage": d["stage"], "done": d["done"], "total": d["total"], "faces": faces})
    if faces and not (YUNET_PATH.exists() and SFACE_PATH.exists()):
        raise FileNotFoundError("face models missing; run scripts/fetch_models.sh")
    conn = db.connect(root)
    notify({"stage": "scan", "done": 0, "total": 0})
    # Rows the index already holds, pending ones included: a scan cut short after listing its files and
    # before reading any of them still has a shoot worth protecting from an empty mount point.
    n_ok = conn.execute("SELECT count(*) FROM photos WHERE status IN ('ok', 'pending')").fetchone()[0]
    if not root.is_dir():
        raise SourceUnavailable(f"{root} is not there. Plug the disk in; the saved index ({n_ok} photos) was left untouched.")
    files = find_images(root)
    if not files and n_ok > 0:
        raise SourceUnavailable(f"{root} has no photos right now. Is the disk mounted? The saved index ({n_ok} photos) was left untouched.")
    if not root.is_dir():
        # Unplugged during the walk: os.walk swallows the errors and hands back a partial list, which
        # must not mark the rest of the shoot missing.
        raise SourceUnavailable(f"{root} went away while its files were being listed. Plug the disk in; the saved index ({n_ok} photos) was left untouched.")
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
    stats = dict(total=len(files), skipped=len(files) - len(todo) - len(face_todo), indexed=0, faced=0, errors=0, embedded=0,
                 clip_faced=0)
    # Clips already read whose frames have never been through face detection (a shoot from before clips got
    # faces, or scanned with faces off): the frames are on the Mac, so this costs no disk and runs last.
    clip_face_todo = faces and any(r[0] not in changed for r in conn.execute(
        "SELECT rel FROM photos WHERE status='ok' AND kind='video' AND people IS NULL"))
    plan[:] = (["scan"] + (["faces"] if face_todo else []) + (["features"] if todo else []) + (["embed"] if embed else [])
               + (["clipfaces"] if clip_face_todo else []))
    # The walk is over and the plan is known, so say so: the first scan emit carried no total and no step
    # number, and this is the only chance to report how big the shoot turned out to be.
    notify({"stage": "scan", "done": len(files), "total": len(files)})
    # Every file still to read gets its pending row now, so the index knows the whole shoot before the first
    # decode: a scan cut short (disk unplugged, app quit) reports "2,080 of 4,315 scanned" and a Continue
    # does the rest. db.scan_counts reads it back.
    db.add_pending(conn, todo)
    db.mark_missing(conn, {f.rel for f in files})
    db.set_meta(conn, "scan_faces", "1" if faces else "0")
    # The job row: found again as 'interrupted' when the shoot is next opened if this run never finishes.
    job["id"] = db.start_job(conn, "scan", {"stage": "scan", "done": 0, "total": len(todo), "faces": faces})
    try:
        _run_stages(root, conn, todo, face_todo, faces, workers, embed, stats, notify, idx)
        if clip_face_todo:
            stats["clip_faced"] = find_clip_faces(root, progress=notify, job=False)["read"]
    except SourceUnavailable as e:
        db.finish_job(conn, job["id"], "interrupted", error=str(e))
        raise
    except BaseException as e:
        db.finish_job(conn, job["id"], "failed", error=f"{type(e).__name__}: {e}")
        raise
    stats["seconds"] = round(time.time() - t0, 1)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_index', datetime('now'))")
    # Locations were read out of every file in this pass, so the Places tab has nothing to catch up on: a
    # shoot indexed by this version must not be offered the catch-up pass (see places.READ_KEY).
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?, datetime('now'))", (_PLACES_READ_KEY,))
    # Same argument for the frame rate: every clip this pass read was probed, so there is no catch-up
    # for this shoot to be offered. A clip ffprobe would not answer for stays NULL and stays NULL after
    # read_fps too, so stamping here hides nothing that running the pass would find.
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?, datetime('now'))", (FPS_READ_KEY,))
    # and for the sidecar: every clip this pass read had it looked for (gamma '' when there was none)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?, datetime('now'))", (CLIP_READ_KEY,))
    # Which encoder just embedded this shoot, so a later run under a different PHOTOSORT_CLIP_MODEL knows
    # (db.embed_model) whether its blobs are trustworthy without touching a single one of them.
    db.stamp_model(conn)
    conn.commit()
    db.finish_job(conn, job["id"], "done", progress={"stage": "done", "done": stats["total"], "total": stats["total"], "faces": faces})
    db.analyze(conn)      # row counts changed: the planner's statistics follow (14 ms at 20k rows)
    job["id"] = None
    notify({"stage": "done", "done": stats["total"], "total": stats["total"]})
    return stats

FPS_READ_KEY = "fps_read_at"      # meta: when the frame rate catch-up pass last finished, local time
# meta: when the clip header pass (frame rate, camera, picture profile) last finished. A key of its own and not
# FPS_READ_KEY, because a shoot that ran v0.4.0's "Read frame rates" has FPS_READ_KEY set and still has never
# had its sidecars read: hanging the offer on the old key would hide it from exactly those shoots.
CLIP_READ_KEY = "clip_info_read_at"

def clip_info_status(root: Path) -> dict:
    """{clips, known, unknown, read_at}: how much of the shoot the clip filters (slow motion, camera,
    picture profile) can speak for. A clip is known once its frame rate is in and its sidecar has been looked
    for (gamma not NULL); unknown counts the rest, indexed before those columns existed or unreadable, which
    the row cannot tell apart. read_at is null until the header pass has run, and is what the UI hangs the
    button on, exactly as places.status does for locations."""
    conn = db.connect(Path(root))
    r = conn.execute("SELECT COUNT(*), SUM(fps IS NOT NULL AND gamma IS NOT NULL) FROM photos "
                     "WHERE status='ok' AND kind='video'").fetchone()
    clips, known = int(r[0] or 0), int(r[1] or 0)
    return {"clips": clips, "known": known, "unknown": clips - known, "read_at": db.get_meta(conn, CLIP_READ_KEY)}

def read_clip_info(root: Path, progress: Callable[[dict], None] | None = None) -> dict:
    """Fill in, for every ok clip still missing any of them, the frame rate (container header), and from
    Sony's <stem>M01.XML sidecar the picture profile, the capture rate and the camera body when the row
    has none.

    This is the one pass in this group that needs the shoot disk, and it is worth being precise about
    what it costs, because "needs the disk" and "is a rescan" are not the same thing. ffprobe reads the
    container header and stops, and the sidecar read is its first 64 KB; it decodes no picture, writes no
    frame, embeds nothing, and touches none of the three folders under the index. A full re-index of the
    client's shoot is about five hours because every clip is decoded at several instants and every frame is
    embedded. ffprobe was measured at 0.025 s per clip on a local SSD, so the client's 1,140 clips are about
    30 seconds plus whatever the shoot disk adds in seek time, which puts the whole pass in single digit
    minutes over USB. That number is a local SSD measurement; nobody has run it against the client's disk.
    It is the places.read_locations shape in every other respect, including the job row and the 2 s
    progress cadence.

    A clip that is not on the disk right now is counted missing and left NULL, so the pass is safe to run
    with the disk half mounted and safe to run again afterwards. Returns {found, read, missing}: found is
    how many clips gained a frame rate."""
    from . import video
    root = Path(root); conn = db.connect(root)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, rel, fps, gamma, camera FROM photos WHERE status='ok' AND kind='video' "
        "AND (fps IS NULL OR gamma IS NULL) ORDER BY id")]
    total = len(rows)
    emit = progress or (lambda d: None)
    job = db.start_job(conn, "fps", {"stage": "fps", "done": 0, "total": total})
    found = missing = 0
    written = [0.0]
    try:
        emit({"stage": "fps", "done": 0, "total": total})
        for i, r in enumerate(rows, 1):
            path = root / r["rel"]
            if not path.is_file():
                missing += 1
            else:
                if r["fps"] is None:
                    try:
                        fps = video.probe(path)["fps"] or None
                    except Exception:
                        fps = None    # an unreadable clip keeps no frame rate, the way it keeps no location
                    if fps:
                        conn.execute("UPDATE photos SET fps=? WHERE id=?", (float(fps), r["id"]))
                        found += 1
                if r["gamma"] is None:
                    side = video.read_sidecar(path)
                    conn.execute("UPDATE photos SET gamma=?, capture_fps=?, camera=COALESCE(camera, ?) WHERE id=?",
                                 (side["gamma"] or "", side["capture_fps"], side["camera"], r["id"]))
            if i % 200 == 0 or i == total:
                conn.commit()
            now = time.time()
            if now - written[0] >= 2.0 or i == total:
                written[0] = now
                emit({"stage": "fps", "done": i, "total": total})
                db.job_progress(conn, job, {"stage": "fps", "done": i, "total": total})
        conn.commit()
    except BaseException as e:
        db.finish_job(conn, job, "failed", error=f"{type(e).__name__}: {e}")
        raise
    # A pass that reached no clip at all (the disk was not plugged in) has not read anything, so it leaves the
    # stamp alone and the UI keeps offering it; one that reached some clips stamps, and what it could not read
    # stays unknown without nagging, as before.
    if not (total and missing == total):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        db.set_meta(conn, FPS_READ_KEY, stamp); db.set_meta(conn, CLIP_READ_KEY, stamp)
    db.finish_job(conn, job, "done")
    return {"found": found, "read": total, "missing": missing}

# The names this pass shipped under in v0.4.0, when it read the frame rate alone.
fps_status = clip_info_status
read_fps = read_clip_info

def clip_faces_status(root: Path) -> dict:
    """{clips, scanned, pending}: clips whose frames have been through face detection, and clips not yet."""
    conn = db.connect(Path(root))
    r = conn.execute(db.CLIP_FACES_PENDING_SQL).fetchone()
    clips, pending = int(r[0] or 0), int(r[1] or 0)
    return {"clips": clips, "scanned": clips - pending, "pending": pending}

def find_clip_faces(root: Path, progress: Callable[[dict], None] | None = None, job: bool = True) -> dict:
    """Face detection on the frames already under the index for every ok clip that has not had it (people
    NULL): a shoot scanned before clips got faces, or scanned with faces off. No disk: the frames are on the
    Mac (an imported bundle without frames/ falls back to the thumb, the clip's middle frame), so this runs
    with the shoot unplugged. Measured on an M1: 476 frames of NSG_26 Part 2 in 17 s, and the 1,140 clips (7,757
    frames, many with crowds) of GT 1 TB in 10 min 18 s, so budget about half a second a clip.

    Writes people, face_h and the clip_faces rows; leaves shot_size alone, which the caller refreshes through
    classify.classify_shot_size once this returns. A clip with neither frames nor a thumb is counted missing
    and stays NULL. job=False when a scan runs this inside its own job row. Returns {found, read, missing}:
    found is how many clips had at least one person in them."""
    root = Path(root); conn = db.connect(root); idx = db.index_dir(root)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, qhash FROM photos WHERE status='ok' AND kind='video' AND people IS NULL ORDER BY id")]
    total = len(rows)
    emit = progress or (lambda d: None)
    jid = db.start_job(conn, "clipfaces", {"stage": "clipfaces", "done": 0, "total": total}) if job else None
    found = missing = 0
    written = [0.0]
    try:
        emit({"stage": "clipfaces", "done": 0, "total": total})
        for i, r in enumerate(rows, 1):
            paths = sorted((idx / "frames").glob(f"{r['qhash']}_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
            if not paths and (idx / "thumbs" / f"{r['qhash']}.jpg").is_file():
                paths = [idx / "thumbs" / f"{r['qhash']}.jpg"]
            frames = []
            for p in paths:
                try:
                    im = Image.open(p); im.load(); frames.append(im.convert("RGB"))
                except Exception:
                    pass
            if not frames:
                missing += 1
            else:
                people, face_h, faces = _clip_faces(frames)
                conn.execute("UPDATE photos SET people=?, face_h=? WHERE id=?", (people, face_h, r["id"]))
                db.replace_clip_faces(conn, r["id"], faces)       # commits
                found += people > 0
            now = time.time()
            if now - written[0] >= 2.0 or i == total:
                written[0] = now
                emit({"stage": "clipfaces", "done": i, "total": total})
                if jid is not None:
                    db.job_progress(conn, jid, {"stage": "clipfaces", "done": i, "total": total})
        conn.commit()
    except BaseException as e:
        if jid is not None:
            db.finish_job(conn, jid, "failed", error=f"{type(e).__name__}: {e}")
        raise
    if jid is not None:
        db.finish_job(conn, jid, "done")
    return {"found": found, "read": total, "missing": missing}

def _run_stages(root: Path, conn, todo, face_todo, faces: bool, workers: int | None, embed: bool,
                stats: dict, notify, idx: Path) -> None:
    """The three passes of index_folder (faces on stored thumbs, features from the disk, embeddings from
    the thumbs), each resumable: a pass only ever touches rows that lack its result."""
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
                notify({"stage": "faces", "done": done, "total": len(face_todo), "current": res["rel"]})
    if todo:
        sib = {f.rel: f.sibling for f in todo}
        meta = {f.rel: (f.size, f.mtime) for f in todo}
        ctx = mp.get_context("spawn")
        done = 0
        def _store(res):
            nonlocal done
            if res["error"] and not root.is_dir():
                # The disk went away mid-scan. Stop here, before this file is written down as an error: its
                # row stays pending, the ones already read stay ok, and Continue picks up from this point.
                n_ok = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
                raise SourceUnavailable(f"{root} went away during the scan. Plug the disk in and continue; {n_ok} photos are scanned so far.")
            if res["error"]:
                stats["errors"] += 1
                db.mark_error(conn, res["rel"], meta[res["rel"]][0], meta[res["rel"]][1])
            else:
                res["row"]["sibling"] = sib.get(res["rel"])
                pid = db.upsert_photo(conn, res["row"])
                db.replace_faces(conn, pid, res["faces"])
                db.replace_segments(conn, pid, res.get("segments", []))
                if res["row"].get("kind") == "video":
                    db.replace_clip_faces(conn, pid, res.get("clip_faces", []))
                stats["indexed"] += 1
            done += 1
            notify({"stage": "features", "done": done, "total": len(todo), "current": res["rel"]})
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
        _embed_stage(conn, stats, notify, idx)

def embed_eta(subs: list[tuple[str, int, int]], current: str, done: int, started: float,
              now: float) -> tuple[float | None, float | None]:
    """(rate, seconds left) for the embed stage, from the rate of the sub-stage that is running.

    subs is every sub-stage of this pass in order, as (name, items, frames to encode). The rest of the
    running sub-stage is priced at its own rate in items per second. A sub-stage still to come is priced in
    frames, at the frame rate the running one is achieving, because an item is a different amount of work
    in each: a photo is one frame, a clip six to thirty, and a segment whose frame was encoded with its clip
    none at all. A rate taken over the whole stage is several times off on either side of the clips.
    (None, None) until the running sub-stage has done something to measure."""
    names = [s[0] for s in subs]
    elapsed = now - started
    if current not in names or done <= 0 or elapsed <= 0:
        return None, None
    i = names.index(current)
    _, items, frames = subs[i]
    rate = done / elapsed
    eta = max(items - done, 0) / rate
    frame_rate = (done * frames / items) / elapsed if items and frames else None
    for _, _, f in subs[i + 1:]:
        if f and frame_rate:
            eta += f / frame_rate
    return float(rate), float(eta)

def _frames_by_clip(idx: Path, qhashes: set[str]) -> dict[str, list[Path]]:
    """Each clip's sampled frames in frame order, from one listing of frames/. A glob per clip lists the
    whole folder once per clip, and the folder holds about eight files for every clip in the shoot."""
    found: dict[str, list[tuple[int, Path]]] = {}
    folder = idx / "frames"
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        stem, _, ext = name.rpartition(".")
        qh, _, k = stem.rpartition("_")
        if ext == "jpg" and qh in qhashes and k.isdigit():
            found.setdefault(qh, []).append((int(k), folder / name))
    return {qh: [p for _, p in sorted(v)] for qh, v in found.items()}

def _embed_stage(conn, stats: dict, notify, idx: Path) -> None:
    """Photos, then clips, then segments. Every encode goes through Embedder.encode_images, which holds the
    MPS lock for each batch, so a search during a scan still waits a batch rather than crashing.

    Clip frames are batched ACROSS clips. A clip of seven frames used to be an encode of seven, a glob and a
    commit, about seven times slower per item than a photo and the slowest part of a 1 TB scan. Now every
    encode is EMBED_BATCH frames whoever they belong to, a clip may straddle two batches, and the clip is
    written when its last frame comes back. A segment's frame is one of its clip's frames, so the segment
    takes the vector that frame got in the same batch rather than being encoded a second time; only the
    segments of a clip embedded in an earlier pass are encoded in the segments sub-stage. At most
    EMBED_BATCH images are open at a time, the same envelope as the photo batches."""
    from .embed import get_embedder
    # "embed IS NULL" is not the same question as "needs embedding". A row carrying a blob from a
    # different encoder is the wrong byte width, is invisible to every loader behind the
    # EMBED_BYTES guard, and would never be picked up again: the shoot's photos go unsearchable and
    # Continue reports nothing left to do. db.photos_missing_embed has always asked this correctly
    # and segments_missing_embed is used below; the photo path simply never called it.
    rows = conn.execute(
        "SELECT id, qhash, kind FROM photos WHERE (embed IS NULL OR length(embed)<>?) "
        "AND status='ok' ORDER BY id", (db.EMBED_BYTES,)).fetchall()
    photos = [(r[0], r[1]) for r in rows if r[2] != "video"]
    videos = [(r[0], r[1]) for r in rows if r[2] == "video"]
    segs = db.segments_missing_embed(conn)
    vid_qh = {r[0]: r[1] for r in conn.execute("SELECT id, qhash FROM photos WHERE kind='video' AND status='ok'")}
    # A clip's embedding is the mean of its sampled frames, renormalised. An index that lost its
    # frames/ (an imported bundle) falls back to the thumb, which is the middle frame.
    by_clip = _frames_by_clip(idx, {qh for _, qh in videos})
    clip_frames = [by_clip.get(qh) or [idx / "thumbs" / f"{qh}.jpg"] for _, qh in videos]
    # The segments whose frame is encoded with its clip in this pass, by (clip id, frame path). A segment
    # whose frame file is gone falls through to the segments sub-stage, which uses the thumb, as before.
    in_pass = {pid: set(fr) for (pid, _), fr in zip(videos, clip_frames)}
    seg_of: dict[tuple[int, Path], list[int]] = {}
    for sid, pid, frame in segs:
        p = idx / "frames" / frame
        if p in in_pass.get(pid, ()):
            seg_of.setdefault((pid, p), []).append(sid)
    covered = {sid for sids in seg_of.values() for sid in sids}
    left = [s for s in segs if s[0] not in covered]
    subs = [s for s in (("photos", len(photos), len(photos)),
                        ("clips", len(videos), sum(map(len, clip_frames))),
                        ("segments", len(segs), len(left))) if s[1]]
    total = len(photos) + len(videos) + len(segs)
    done = 0
    def emit(name: str, sub_done: int, sub_total: int, started: float) -> None:
        rate, eta = embed_eta(subs, name, sub_done, started, time.time())
        notify({"stage": "embed", "done": done, "total": total, "substage": name, "substage_done": sub_done,
                "substage_total": sub_total, "substage_started": started, "rate": rate, "eta_s": eta})
    E = get_embedder()

    started = time.time()
    for i in range(0, len(photos), EMBED_BATCH):
        batch = photos[i:i + EMBED_BATCH]
        vecs = E.encode_images([Image.open(idx / "thumbs" / f"{qh}.jpg") for _, qh in batch])
        for (pid, _), v in zip(batch, vecs):
            db.set_embed(conn, pid, v)
        conn.commit()
        stats["embedded"] += len(batch); done += len(batch)
        emit("photos", i + len(batch), len(photos), started)

    started = time.time()
    queue: list[tuple[int, Path]] = []          # (clip number, frame) waiting for a batch, in clip order
    got: list[list[np.ndarray]] = [[] for _ in videos]
    written = 0                                 # clips written so far; they complete in queue order
    def flush() -> None:
        nonlocal written, done
        take = queue[:EMBED_BATCH]; del queue[:EMBED_BATCH]
        vecs = E.encode_images([Image.open(p) for _, p in take])
        for (k, p), v in zip(take, vecs):
            got[k].append(v)
            for sid in seg_of.get((videos[k][0], p), ()):
                db.set_segment_embed(conn, sid, v)
        before = written
        while written < len(videos) and len(got[written]) == len(clip_frames[written]):
            v = np.stack(got[written]).mean(axis=0); v /= (np.linalg.norm(v) + 1e-9)
            db.set_embed(conn, videos[written][0], v)
            got[written] = []
            written += 1
        conn.commit()
        # One report per batch, not per clip: the four clips a batch finishes all finish at the same instant,
        # and a report for the first of them alone would read as a quarter of the real rate.
        if written > before:
            stats["embedded"] += written - before; done += written - before
            emit("clips", written, len(videos), started)
    for k, frames in enumerate(clip_frames):
        queue.extend((k, p) for p in frames)
        while len(queue) >= EMBED_BATCH:
            flush()
    while queue:
        flush()

    # The segments written with their clips are counted when this sub-stage opens, so the overall counter
    # never runs ahead of the sub-stage it is reporting.
    started = time.time()
    n_seg = len(covered)
    done += n_seg
    if n_seg and not left:
        emit("segments", n_seg, len(segs), started)
    for i in range(0, len(left), EMBED_BATCH):
        batch = left[i:i + EMBED_BATCH]
        ims = []
        for _, pid, frame in batch:
            p = idx / "frames" / frame
            ims.append(Image.open(p if p.is_file() else idx / "thumbs" / f"{vid_qh[pid]}.jpg"))
        vecs = E.encode_images(ims)
        for (sid, _, _), v in zip(batch, vecs):
            db.set_segment_embed(conn, sid, v)
        conn.commit()
        done += len(batch)
        emit("segments", n_seg + i + len(batch), len(segs), started)
