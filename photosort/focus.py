"""The on-demand "hide blurry" pass. Not part of the index: the user starts it from the filter, and it
labels every ok row ok / soft / bad from what the index already measured. Photos cost nothing (sharp_tile,
sharp_max and sharp_eye are in the row); clips are scored from the sample frames under frames/ (the median
over frames, so one whip-pan does not condemn a clip). Nothing here reads a source photo, and a clip is
only decoded when its frames are gone. Thresholds are relative to the shoot (see config.FOCUS_*)."""
from __future__ import annotations
from pathlib import Path
from typing import Callable
import numpy as np
from PIL import Image
from . import db
from .config import (PREVIEW_EDGE, SOFT_PERCENTILE, FOCUS_BAD_PERCENTILE, FOCUS_BAD_ABS, FOCUS_BAD_MAX_ABS,
                     FOCUS_FALLBACK_FRAMES)
from .features import sharpness_tiles, to_gray

def measure_image(im: Image.Image) -> tuple[float, float]:
    """(tile p90, sharpest tile): features.sharpness_tiles on the 1024 px decode, the same numbers the
    index stores as sharp_tile and sharp_max."""
    if max(im.size) > PREVIEW_EDGE:
        im = im.copy(); im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    return sharpness_tiles(to_gray(im))

def score_image(im: Image.Image) -> float:
    return measure_image(im)[0]

def _frames(root: Path, row: dict) -> list[Path]:
    idx = db.index_dir(Path(root))
    return sorted((idx / "frames").glob(f"{row['qhash']}_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))

def measure_video(root: Path, row: dict) -> tuple[float, float]:
    """(median over frames of the tile p90, max over frames of the sharpest tile) from the stored sample
    frames; a clip without frames (an imported bundle) has FOCUS_FALLBACK_FRAMES decoded from the source."""
    ims = [Image.open(p) for p in _frames(root, row)]
    if not ims:
        from . import video
        path = Path(root) / row["rel"]
        for t in video.sample_times(float(row.get("duration") or 0.0), FOCUS_FALLBACK_FRAMES):
            fr = video.frame_at(path, t)
            if fr is not None:
                ims.append(fr)
    if not ims:
        return 0.0, 0.0
    vals = [measure_image(im) for im in ims]
    return float(np.median([v[0] for v in vals])), float(max(v[1] for v in vals))

def score_video(root: Path, row: dict) -> float:
    return measure_video(root, row)[0]

def _label(score: float, mx: float, eye: float | None, p_bad: float, p_soft: float) -> str:
    """bad: bottom of the shoot AND under the absolute floor AND nothing sharp anywhere (sharpest tile
    and eye strip both under FOCUS_BAD_MAX_ABS). soft: the bottom SOFT_PERCENTILE. else ok."""
    proof = mx >= FOCUS_BAD_MAX_ABS or (eye is not None and eye >= FOCUS_BAD_MAX_ABS)
    if score < p_bad and score < FOCUS_BAD_ABS and not proof:
        return "bad"
    if score < p_soft:
        return "soft"
    return "ok"

def check_focus(root: Path, only_unchecked: bool = True, progress: Callable[[dict], None] | None = None) -> dict:
    """Score and label the shoot's ok rows; returns {ok, soft, bad, checked} over the rows labelled this
    run. With only_unchecked only rows with focus NULL are labelled, but the percentiles always come from
    every ok row of that kind (a late batch is judged against the whole shoot, not against itself).
    Photos and clips get separate distributions: frames are softer by nature."""
    root = Path(root); conn = db.connect(root)
    notify = progress or (lambda d: None)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, rel, qhash, kind, duration, sharp_tile, sharp_max, sharp_eye, focus, focus_score FROM photos WHERE status='ok' ORDER BY id")]
    todo = [r for r in rows if not only_unchecked or r["focus"] is None]
    todo_ids = {r["id"] for r in todo}
    total = len(todo); done = 0
    notify({"stage": "focus", "done": 0, "total": total})
    # One measurement per row: a photo's numbers are already in the row; a clip's come from its frames.
    # A checked row keeps its stored score for the distribution when it is not being relabelled.
    measured: dict[int, tuple[float, float]] = {}
    for r in rows:
        if r["id"] not in todo_ids and r["focus_score"] is not None:
            measured[r["id"]] = (float(r["focus_score"]), float(r["sharp_max"] or 0.0))
            continue
        if r["kind"] == "video":
            try:
                measured[r["id"]] = measure_video(root, r)
            except Exception:
                measured[r["id"]] = (0.0, 0.0)
        elif r["sharp_tile"] is not None:
            measured[r["id"]] = (float(r["sharp_tile"]), float(r["sharp_max"] or 0.0))
        else:
            p = db.index_dir(root) / "thumbs" / f"{r['qhash']}.jpg"
            measured[r["id"]] = measure_image(Image.open(p)) if p.is_file() else (0.0, 0.0)
        if r["id"] in todo_ids:
            done += 1
            notify({"stage": "focus", "done": done, "total": total})
    cuts = {}
    for kind in ("photo", "video"):
        sc = np.array([measured[r["id"]][0] for r in rows if (r["kind"] == "video") == (kind == "video")], float)
        cuts[kind] = (float(np.percentile(sc, FOCUS_BAD_PERCENTILE)), float(np.percentile(sc, SOFT_PERCENTILE))) if sc.size else (0.0, 0.0)
    counts = {"ok": 0, "soft": 0, "bad": 0, "checked": 0}
    for r in todo:
        score, mx = measured[r["id"]]
        p_bad, p_soft = cuts["video" if r["kind"] == "video" else "photo"]
        eye = None if r["kind"] == "video" else r["sharp_eye"]
        label = _label(score, mx, eye, p_bad, p_soft)
        conn.execute("UPDATE photos SET focus=?, focus_score=? WHERE id=?", (label, score, r["id"]))
        counts[label] += 1; counts["checked"] += 1
    conn.commit()
    notify({"stage": "focus", "done": total, "total": total, "counts": counts})
    return counts

def status(root: Path) -> dict:
    """{checked, unchecked, bad, soft} over ok rows: what the UI shows next to the filter."""
    conn = db.connect(Path(root))
    r = conn.execute("SELECT SUM(focus IS NOT NULL), SUM(focus IS NULL), SUM(focus='bad'), SUM(focus='soft') "
                     "FROM photos WHERE status='ok'").fetchone()
    return {"checked": int(r[0] or 0), "unchecked": int(r[1] or 0), "bad": int(r[2] or 0), "soft": int(r[3] or 0)}
