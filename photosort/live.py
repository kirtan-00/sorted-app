"""Live shoot: a tethered camera writes into a folder, and every frame gets a verdict while the shoot is still on.

The tether app (Sony Imaging Edge Remote, EOS Utility, Lightroom) owns the folder. We only ever read it: a polling
watcher finds files that have stopped growing, one worker thread runs each through the same per-file path a scan uses
(index.process_one, the db upserts, the shared encoder), and an ordered event log feeds the screen on the set.

Nothing whole-shoot runs per frame. The verdict is absolute (the config floors, not a percentile of a shoot that is
still growing), products are running centroids of grey embeddings, and bursts and brackets are the rules in search.py
applied to the last few frames. The project file is saved once, on stop, and never into the watched folder.

Tags live in live_products and live_tags inside the shoot's index.db, so they travel in the project file and a session
restarted on the same folder carries on from them.

With a drive picked, every frame also goes into a persistent queue (live_copies) and a thread of its own copies it to
<drive>/<shoot>/. The worker only ever adds a row. The source is read, never moved, and a pulled or full drive pauses the
queue while the shoot carries on.
"""
from __future__ import annotations
import csv, errno, fcntl, hashlib, io, itertools, json, os, queue, re, shutil, sqlite3, subprocess, threading, time, uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import cv2
import numpy as np
from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel
from . import awake, bundle, db, index
from .config import (DEV_DIRS, EMBED_DIM, FACE_CLUSTER_MIN_EYE_SHARP, FOCUS_BAD_ABS, FOCUS_BAD_MAX_ABS, IMAGE_EXTS, MODEL_TAG,
                     RAW_EXTS, SHARP_TILE_GRID, SKIP_DIRS, export_root)
from .decode import DecodeError, load_preview
from .export import safe_segment
from .features import to_gray
from .products import PRODUCT_MIN_PHOTOS, PRODUCT_SIM
from .search import Index, _frame_no
from .walk import PROJECT_SUFFIXES

POLL_S = 0.2              # how often the folder is listed
STABLE_POLLS = 2          # a file has landed when two polls in a row after the first sighting find it unchanged
PAIR_WINDOW_S = 2.0       # a RAW that has landed waits this long for its JPEG; a JPEG never waits
QUEUE_MAX = 64            # frames handed to the worker at once; the rest wait as names, the files are safe on the disk
BEHIND_AT = 3             # frames waiting at which the screen says we are behind
WINDOW = 32               # frames the burst and bracket rules look back over
EVENT_KEEP = 50_000       # events held for catching up; a client further back than this is told to reload the state
LONG_POLL_S = 20.0
STORE_MAX_EDGE = 4472     # Shopify's limit on the longest side
STORE_QUALITY = 92
ORIGINAL_EDGE = 1 << 15   # no camera reaches this: the 100% view is never scaled down

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_products(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, auto INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS live_tags(
  photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE, n INTEGER NOT NULL,
  product INTEGER REFERENCES live_products(id), model TEXT, tagged INTEGER NOT NULL DEFAULT 0,
  verdict TEXT, reason TEXT, kind TEXT, flash TEXT, burst INTEGER, bracket INTEGER, ev REAL, ms REAL, grey BLOB,
  alerts TEXT, level REAL, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS live_copies(
  rel TEXT PRIMARY KEY, photo_id INTEGER, src_size INTEGER, src_mtime REAL, state TEXT NOT NULL DEFAULT 'queued',
  dest TEXT, hash TEXT, verified INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0,
  tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, error TEXT);
"""
ACTIVE_KEY = "live_active"      # meta: the active product and model, cleared by a clean stop so a crash can restore them
UNSORTED = "Unsorted"           # the one pile of a session that does not sort into products
DRIVE_KEY = "live_drive"        # meta: the drive this shoot copies to, its shoot name and the id of the marker on it

_UNSET = object()
_STOP = object()


def ensure_tables(conn) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(live_tags)")}     # a shoot from before these columns
    for col, typ in (("alerts", "TEXT"), ("level", "REAL"), ("hidden", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE live_tags ADD COLUMN {col} {typ}")


# ===== the verdict =====
# Copied from ../shoot-modes/photosort/cull.py (_lab, _plane and the mask half of measure_array), which is tested on the
# calibration set in that worktree's docs/client-pick.md: the product is the part of the frame that differs from a
# plane fitted to the border in Lab. cull's own gradient number is not copied: it is judged against the sharpest frame
# of an angle, and a live frame has to stand on its own against the config floors.
MASK_EDGE = 160          # the product mask is found on a copy this many pixels on the long side
RING = 3                 # border rows the backdrop plane is fitted to, on that copy
FG_DE = 14.0             # CIE76 delta E from the backdrop plane above which a pixel is product
MIN_BLOB = 0.002         # a speck smaller than this share of the frame is dust or grain, not product
MIN_PRODUCT = 0.005      # less product than this and the frame has no product to measure
MIN_TILE_SHARE = 0.25    # a tile counts when this share of it is product


def _lab(rgb_u8: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb_u8.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)


def _plane(lab: np.ndarray, ring: int) -> np.ndarray:
    h, w, _ = lab.shape
    yy, xx = np.mgrid[0:h, 0:w]
    edge = np.zeros((h, w), bool)
    edge[:ring] = edge[-ring:] = True; edge[:, :ring] = edge[:, -ring:] = True
    A = np.stack([np.ones(edge.sum()), xx[edge] / w, yy[edge] / h], 1)
    Y = lab[edge]
    coef = np.linalg.lstsq(A, Y, rcond=None)[0]
    for _ in range(2):
        res = np.sqrt(((A @ coef - Y) ** 2).sum(1))
        keep = res <= max(np.percentile(res, 60), 4.0)
        if keep.sum() < 8:
            break
        coef = np.linalg.lstsq(A[keep], Y[keep], rcond=None)[0]
    return coef


def product_region(rgb: np.ndarray) -> np.ndarray | None:
    """A boolean mask of the product at the size of rgb, or None when nothing stands off the backdrop. Found on a small
    copy, then held to the pixels really off the backdrop (lightly smoothed, so grain does not count)."""
    h, w, _ = rgb.shape
    s = MASK_EDGE / max(h, w)
    sw, sh = max(int(round(w * s)), 8), max(int(round(h * s)), 8)
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    lab_s = _lab(small)
    coef = _plane(lab_s, RING)
    yy, xx = np.mgrid[0:sh, 0:sw]
    bg_s = coef[0] + np.multiply.outer(xx / sw, coef[1]) + np.multiply.outer(yy / sh, coef[2])
    mask = (np.sqrt(((lab_s - bg_s) ** 2).sum(-1)) > FG_DE).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, cc, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= MIN_BLOB * sw * sh
    mask = keep[cc]
    if float(mask.mean()) < MIN_PRODUCT:
        return None
    ys, xs = np.nonzero(mask)            # only the product's box is worked at full size
    y0, y1 = max(int(ys.min() / s) - 4, 0), min(int((ys.max() + 1) / s) + 4, h)
    x0, x1 = max(int(xs.min() / s) - 4, 0), min(int((xs.max() + 1) / s) + 4, w)
    big = cv2.resize(mask.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
    Y, X = np.mgrid[y0:y1, x0:x1]
    bg = coef[0] + np.multiply.outer(X / w, coef[1]) + np.multiply.outer(Y / h, coef[2])
    off = cv2.GaussianBlur(np.sqrt(((_lab(np.ascontiguousarray(rgb[y0:y1, x0:x1])) - bg) ** 2).sum(-1)), (0, 0), 1.5)
    out = np.zeros((h, w), bool)
    out[y0:y1, x0:x1] = big[y0:y1, x0:x1] & (off > FG_DE)
    return out if out.sum() >= 50 else None


def region_sharpness(gray: np.ndarray, region: np.ndarray, grid: int = SHARP_TILE_GRID) -> tuple[float, float]:
    """(tile p90, sharpest tile) of the Laplacian variance over the product's pixels only: features.sharpness_tiles in
    the same units, on a grid laid over the product's box instead of the frame, so the config floors apply as they are."""
    ys, xs = np.nonzero(region)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    th, tw = max(int(y1 - y0) // grid, 8), max(int(x1 - x0) // grid, 8)
    vals = []
    for y in range(y0, y1, th):
        for x in range(x0, x1, tw):
            m = region[y:y + th, x:x + tw]
            if m.mean() >= MIN_TILE_SHARE and m.sum() >= 16:
                vals.append(float(lap[y:y + th, x:x + tw][m].var()))
    if not vals:                          # a thin product (a ring, a cable) fills no tile: read it whole
        vals = [float(lap[region].var())]
    return float(np.percentile(vals, 90)), float(max(vals))


def is_soft(p90: float, sharpest: float) -> bool:
    """The absolute half of focus._label: soft only with nothing sharp anywhere in what was measured."""
    return p90 < FOCUS_BAD_ABS and sharpest < FOCUS_BAD_MAX_ABS


def judge(row: dict, thumb: Image.Image, region=_UNSET) -> dict:
    """{kind, verdict, reason} for one frame from its index row and its 1024 px thumb. Never shoot-relative: a percentile
    of a growing shoot would relabel the same frame as the day goes on. region is product_region's answer when the
    caller already has it."""
    if row.get("n_faces"):
        soft = float(row.get("sharp_eye") or 0.0) < FACE_CLUSTER_MIN_EYE_SHARP
        return {"kind": "model", "verdict": "soft" if soft else "sharp", "reason": "on the eyes" if soft else None}
    if region is _UNSET:
        region = product_region(np.asarray(thumb.convert("RGB")))
    if region is None:                    # the product fills the frame, or there is no backdrop to tell it from
        p90, top = float(row.get("sharp_tile") or 0.0), float(row.get("sharp_max") or 0.0)
    else:
        p90, top = region_sharpness(to_gray(thumb), region)
    soft = is_soft(p90, top)
    return {"kind": "product", "verdict": "soft" if soft else "sharp", "reason": "on the product" if soft else None}


def flash_state(tag, strobe: bool = False, no_flash: bool = False) -> str:
    """fired, none or unknown. Bit 0 of the EXIF Flash tag set is proof it fired. Clear proves nothing: a radio trigger
    leaves it clear on a frame the strobe lit, so without the photographer's word a clear bit is unknown, never none."""
    if isinstance(tag, int) and tag >= 0 and tag & 1:
        return "fired"
    return "fired" if strobe else "none" if no_flash else "unknown"
# ===== end the verdict =====


# ===== light alerts =====
# Every number here is a starting value, picked by reasoning and drawn test frames, not measured on a real shoot. An
# alert is a hint on the rail, so anything unsure stays silent: no settings in the EXIF, too few frames to compare, a
# backdrop too dark to read, no product found.
ALERT_WINDOW = 9          # frames the rolling median looks back over
ALERT_MIN_REF = 3         # comparable frames needed before anything is called darker
FLASH_MISS_STOPS = 1.5
DARK_STOPS = 1.0
ISO_JUMP_STOPS = 1.0
LEVEL_PCT = 98            # the bright end of the frame: the sweep, or the lit product on black. A subject moving does not shift it
LEVEL_FLOOR = 0.02        # linear; a reference darker than this is sensor noise and JPEG, not a level
CLIP_AT = 250             # any channel at this or more is blown
CLIP_SHARE = 0.005        # of the product
CLIP_EDGE = 4             # px taken off the product's rim: the mask runs a little over it, onto a backdrop that may be blown itself
CLIP_MIN_PIXELS = 400     # less product than this after the rim is gone says nothing


def light_level(rgb: np.ndarray) -> float:
    """The frame's bright end in linear light, so a ratio of two levels is a difference in stops."""
    h, w, _ = rgb.shape
    s = min(256 / max(h, w), 1.0)
    small = cv2.resize(rgb, (max(int(w * s), 1), max(int(h * s), 1)), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    lin = np.where(small <= 0.04045, small / 12.92, ((small + 0.055) / 1.055) ** 2.4)
    return float(np.percentile(lin @ np.array([0.2126, 0.7152, 0.0722], np.float32), LEVEL_PCT))


def clipped_share(rgb: np.ndarray, region: np.ndarray | None) -> float | None:
    """The share of the product at CLIP_AT or more in any channel, or None when there is no product to speak for. Never
    the whole frame: a white sweep is meant to be blown."""
    if region is None:
        return None
    k = 2 * CLIP_EDGE + 1
    core = cv2.erode(region.astype(np.uint8), np.ones((k, k), np.uint8)).astype(bool)
    if core.sum() < CLIP_MIN_PIXELS:
        return None
    return float((rgb[core].max(axis=1) >= CLIP_AT).mean())


def _same_settings(a: dict, b: dict) -> bool:
    return all(a.get(k) is not None and a.get(k) == b.get(k) for k in ("exposure_s", "fnumber", "iso"))


def light_alerts(cur: dict, recent, strobe: bool = False, clip: float | None = None) -> list[dict]:
    """[{kind, text}] for one frame. cur and each frame of recent carry level, exposure_s, fnumber, iso and product. Only
    frames of the same product at the same settings are compared, so a black shoe after a white one is no alarm and a
    bracket is not read as a misfire. Below the flash line in Strobe it is only flash_miss, never dark as well."""
    out = []
    ref = [f["level"] for f in recent if f["product"] == cur["product"] and _same_settings(f, cur)]
    if len(ref) >= ALERT_MIN_REF and cur.get("level") is not None and float(np.median(ref)) >= LEVEL_FLOOR:
        drop = float(np.log2(float(np.median(ref)) / max(cur["level"], 1e-4)))
        if strobe and drop >= FLASH_MISS_STOPS:
            out.append({"kind": "flash_miss", "text": "Flash may not have fired"})
        elif drop >= DARK_STOPS:
            out.append({"kind": "dark", "text": "Darker than the last shots"})
    if clip is not None and clip >= CLIP_SHARE:
        out.append({"kind": "clipped", "text": "Blown out on the product"})
    last = recent[-1].get("iso") if recent else None
    iso = cur.get("iso")
    if iso and last and abs(float(np.log2(iso / last))) >= ISO_JUMP_STOPS:     # either way: a knocked dial is the same mistake
        out.append({"kind": "iso", "text": f"ISO jumped to {iso}"})
    t, focal = cur.get("exposure_s"), cur.get("focal_mm")
    if not strobe and t and focal and t > 1.0 / (2.0 * focal):                # a strobe freezes the frame, whatever the shutter says
        out.append({"kind": "shutter", "text": "Shutter may be too slow to hand hold"})
    return out
# ===== end light alerts =====


# ===== bursts and brackets, live =====
class _Window(Index):
    """search.Index's own burst and bracket rules over the last WINDOW frames instead of a whole shoot. No refresh, no
    database: _find_groups needs only photos, pos and M. Frames come in as index row dicts with the embedding beside."""
    def __init__(self, root: Path):
        self.root = Path(root); self.photos = {}; self.vecs = {}; self.pos = {}; self.M = np.zeros((0, EMBED_DIM), np.float32)
        self._fields = {}; self._kept = {}

    def _framing(self, a: dict, b: dict) -> float:
        from .search import framing_field, framing_similarity
        grid = db.index_dir(self.root) / "grid"
        for p in (a, b):                  # _find_brackets empties _fields on every pass; these outlive the pass
            if p["qhash"] not in self._kept:
                self._kept[p["qhash"]] = framing_field(grid / f"{p['qhash']}.jpg")
        fa, fb = self._kept[a["qhash"]], self._kept[b["qhash"]]
        return framing_similarity(fa, fb) if fa is not None and fb is not None else -1.0

    def add(self, row: dict, vec: np.ndarray) -> None:
        self.photos[row["id"]] = row; self.vecs[row["id"]] = np.asarray(vec, np.float32)
        for old in list(self.photos)[:-WINDOW]:
            self._kept.pop(self.photos[old]["qhash"], None)
            del self.photos[old], self.vecs[old]
        self.pos = {i: k for k, i in enumerate(self.vecs)}
        self.M = np.stack(list(self.vecs.values()))

    def groups(self) -> tuple[list[list[dict]], list[list[dict]]]:
        """(bursts, brackets) inside the window, each a list of frames."""
        self._find_groups()
        out: dict[str, dict[int, list[dict]]] = {"burst": {}, "bracket": {}}
        for p in self.photos.values():
            for k in out:
                if p.get(k):
                    out[k].setdefault(p[k], []).append(p)
        return list(out["burst"].values()), list(out["bracket"].values())
# ===== end bursts and brackets =====


class EventLog:
    """Ordered events with a monotonically increasing seq, one log for the life of the server so a screen left open
    across two sessions never goes backwards."""
    def __init__(self, keep: int = EVENT_KEEP):
        self.cond = threading.Condition()
        self.items: deque[dict] = deque(maxlen=keep)
        self.seq = 0

    def emit(self, type_: str, **data) -> int:
        with self.cond:
            self.seq += 1
            self.items.append({"seq": self.seq, "type": type_, "t": time.time(), **data})
            self.cond.notify_all()
            return self.seq

    def since(self, after: int, wait: float = LONG_POLL_S) -> dict:
        """Events after seq, waiting up to wait seconds for the first. reset True means this client's seq means nothing
        here (the log moved past it, or the server restarted): reload GET /api/live/state and carry on from its seq."""
        end = time.monotonic() + max(0.0, min(wait, LONG_POLL_S))
        with self.cond:
            while True:
                first = self.items[0]["seq"] if self.items else self.seq + 1
                if after > self.seq or after < first - 1:
                    return {"events": [], "seq": self.seq, "reset": True}
                if after < self.seq:
                    return {"events": [e for e in self.items if e["seq"] > after], "seq": self.seq, "reset": False}
                left = end - time.monotonic()
                if left <= 0:
                    return {"events": [], "seq": self.seq, "reset": False}
                self.cond.wait(left)


@dataclass
class _Seen:
    """A file on its way in: what the last poll saw, and who was active when it first appeared."""
    size: int
    mtime: float
    first: float
    product: str | None
    model: str | None
    same: int = 0
    stable_at: float | None = None


@dataclass
class _Item:
    rel: str
    size: int
    mtime: float
    product: str | None = None
    model: str | None = None
    sibling: str | None = None
    attach_to: str | None = None      # a RAW whose JPEG is already in: nothing to decode, only the sibling to record
    first: float = 0.0


# ===== the copy to the drive =====
# <drive>/<shoot>/products/<product>/<file>, models/<model>/<file> when the shot has a model name, a soft/ folder inside
# either, unsorted/<file> for the pile of a session without products, and unread/<file> for what we could not decode. live_copies is the queue, on the Mac, and one thread owns the
# drive: it copies, moves and reads back, so nothing on the drive ever has two writers. A row names no folder until the
# moment it is copied, which is why a rename costs a queued frame nothing.
MARKER = ".sorted-shoot"      # names this shoot on the disk. A path alone cannot tell the drive from a stale mount point or another card
MANIFEST = "manifest.csv"
CHUNK = 4 << 20
DRIVE_POLL_S = 1.0            # how often a paused queue looks for the drive, and a running one checks its free space
DRIVE_MIN_FREE = 1 << 30      # refused at start below this, about thirty frames of a high megapixel body
DRIVE_KEEP_FREE = 64 << 20    # never filled to the last byte: a volume at zero misbehaves
COPY_TRIES = 3                # a file that fails this many times with the drive healthy is failed, until the drive comes back
COPY_RETRY_S = 1.0
MANIFEST_EVERY_S = 3.0        # rewritten at most this often while frames pour in, and whenever the queue goes idle
STATUS_EVERY_S = 0.5
VOLUMES = Path("/Volumes")
EXFAT_WARNING = "This drive is exFAT. Eject it before unplugging, because a pulled cable can damage the whole disk."
PICK_PROMPTS = {"watch": "Pick the folder the camera writes to", "drive": "Pick the drive to copy onto",
                "export": "Pick where the store export goes"}


def _category(kind: str | None, model: str | None) -> str:
    return "model" if model or kind == "model" else "product"


def drive_off() -> dict:
    return {"path": None, "name": None, "fs": None, "state": "off", "pending": 0, "copied": 0, "failed": 0, "free_bytes": None, "warning": None}


def _seg(name: str | None, fallback: str) -> str:
    """One folder name on the drive from whatever was typed or scanned."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()
    if name in ("", ".", ".."):
        return fallback
    return safe_segment(name)[:120].rstrip(" .") or fallback


def _volume_name(p: Path) -> str:
    parts = p.parts
    return parts[2] if len(parts) >= 3 and parts[:2] == ("/", "Volumes") else p.name or str(p)


def _free_bytes(p: Path) -> int:
    return shutil.disk_usage(p).free


def _mounts() -> dict[str, str]:
    """{mount point: filesystem}, from what mount prints: `/dev/disk4s1 on /Volumes/SSD (exfat, local, ...)`."""
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {m[1]: m[2].lower() for m in re.finditer(r"^.+? on (.+?) \((\w+)[,)]", out, re.M)}


def fs_type(path: Path) -> str | None:
    table, p = _mounts(), Path(path).resolve()
    return next((table[str(a)] for a in (p, *p.parents) if str(a) in table), None)


def volumes() -> list[dict]:
    """The disks under /Volumes, without the Mac's own (its entry there is a link to /)."""
    out, table = [], _mounts()
    try:
        names = sorted(os.listdir(VOLUMES))
    except OSError:
        return out
    for name in names:
        p = VOLUMES / name
        try:
            if name.startswith(".") or not p.is_dir() or p.resolve() == Path("/"):
                continue
            out.append({"name": name, "path": str(p), "free": _free_bytes(p), "fs": table.get(str(p))})
        except OSError:
            continue                  # ejected between the listing and the stat
    return out


def pick_folder(prompt: str) -> str | None:
    """A path from the Mac's folder picker, or None when the person cancelled."""
    try:
        r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "the folder picker timed out")
    except FileNotFoundError:
        raise HTTPException(501, "the folder picker is not available here")
    path = r.stdout.strip()
    return path if r.returncode == 0 and path else None


def _inside(path: Path, root: Path) -> bool:
    """under(), and also true when the two only differ by case on a Mac disk, where they are the same folder."""
    if under(path, root):
        return True
    try:
        want = os.stat(root)
    except OSError:
        return False
    p = Path(path).resolve()
    return any(a.exists() and os.path.samestat(os.stat(a), want) for a in (p, *p.parents))


def drive_preflight(folder: Path, drive: str, shoot: str | None) -> str | None:
    """None when this drive can take the shoot, else why not, in words."""
    d = Path(drive).expanduser()
    if not d.is_absolute():
        return "Pick a full path for the drive"
    if not d.is_dir():
        return f"{_volume_name(d)} is not connected"
    dest = d / _seg(shoot, Path(folder).name or "shoot")
    if _inside(dest, folder) or _inside(folder, dest):
        return "The copy cannot go inside the folder the camera writes to"
    why = bundle.probe_writable(d)
    if why:
        return f"{_volume_name(d)} cannot be written to ({why})"
    free = _free_bytes(d)
    if free < DRIVE_MIN_FREE:
        return f"{_volume_name(d)} has only {free / 1e9:.1f} GB free"
    return None


def _full_sync(fd: int) -> None:
    """Down to the platters. Plain fsync on a Mac stops at the disk's own cache, and that is what a pulled cable loses.
    exFAT and network volumes can refuse F_FULLFSYNC, and a file system that refuses fsync as well has nothing more to give."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (OSError, AttributeError):
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise


def _sync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        _full_sync(fd)
    except OSError:
        pass                          # the file is written; a later step finds out if the drive went
    finally:
        os.close(fd)


def _pump(fi, fo, h) -> int:
    n = 0
    while chunk := fi.read(CHUNK):
        fo.write(chunk); h.update(chunk); n += len(chunk)
    return n


def file_hash(p: Path, uncached: bool = False) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(p, "rb") as f:
        if uncached and hasattr(fcntl, "F_NOCACHE"):
            try:
                fcntl.fcntl(f.fileno(), fcntl.F_NOCACHE, 1)   # read the disk, as far as the Mac lets us, not what we just wrote
            except OSError:
                pass
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def land_file(src: Path, final: Path) -> str:
    """src onto final, or nothing: <name>.part, flushed to the disk, size checked, renamed, the folder flushed. Returns the
    hash of what was written. A stale .part from a crash is simply written over. Never replaces final."""
    tmp = final.with_name(final.name + ".part")
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            st = os.fstat(fi.fileno())
            n = _pump(fi, fo, h)
            fo.flush()
            try:
                os.utime(fo.fileno(), (st.st_atime, st.st_mtime))     # the capture time, for whatever sorts the drive by date
            except OSError:
                pass
            _full_sync(fo.fileno())
            if not n == st.st_size == os.fstat(fo.fileno()).st_size:
                raise OSError(errno.EIO, "the copy came out a different size")
        if os.path.lexists(final):
            raise FileExistsError(errno.EEXIST, "already there", str(final))
        os.rename(tmp, final)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass                      # the drive went; the sweep on its return takes this .part
        raise
    _sync_dir(final.parent)
    return h.hexdigest()


def _cell(v: str | None) -> str:
    """A product name is typed or scanned, and the manifest opens in a spreadsheet: a leading = must stay text."""
    v = v or ""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


class Copier:
    """The queue's thread. It outlives a stop until the queue is empty, and sits paused for as long as the drive is away."""

    def __init__(self, session: "LiveSession", drive: str | Path, shoot: str | None, resume: bool = False, poll_s: float = DRIVE_POLL_S):
        self.s, self.root = session, session.root
        self.drive = Path(drive).expanduser()
        self.shoot = _seg(shoot, self.root.name or "shoot")
        self.dest = self.drive / self.shoot
        self.resume, self.poll_s = resume, poll_s
        self.id: str | None = None
        self.fs = fs_type(self.drive)
        self.state = "ok"
        self.free: int | None = None
        self.counts = {"queued": 0, "copied": 0, "failed": 0}
        self.need = DRIVE_KEEP_FREE       # free bytes below which the queue pauses as full
        self.swept = False
        self.dirty = True                 # the manifest is behind
        self.last = {"check": 0.0, "manifest": 0.0, "status": 0.0}
        self.manifest_after = 0.0         # a manifest that would not write waits this long before the next try
        self.sent: tuple | None = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.finishing = threading.Event()
        self.thread: threading.Thread | None = None

    # ===== the queue, from the session's side =====
    def _marker(self) -> str | None:
        try:
            return (self.dest / MARKER).read_text().strip() or None
        except OSError:
            return None

    def open(self, conn) -> None:
        """Before the threads start. Same drive and shoot as last time: the queue carries on where it was. Anything else
        starts the table over, because a row says where its file is on one disk only. Then every shot the table does not
        know joins it, so turning a drive on mid shoot, or a crash, leaves nothing behind."""
        saved = json.loads(db.get_meta(conn, DRIVE_KEY) or "{}")
        same = saved.get("path") == str(self.drive) and saved.get("shoot") == self.shoot
        if same:
            self.id = saved.get("id")
        if not same or (not self.resume and self.drive.is_dir() and self._marker() is None):
            conn.execute("DELETE FROM live_copies")
            self.id = None
        self._remember(conn)
        for r in conn.execute("SELECT p.id, p.rel, p.sibling FROM live_tags t JOIN photos p ON p.id=t.photo_id").fetchall():
            self.add(conn, r[0], r[1])
            if r[2]:
                self.add(conn, r[0], r[2])
        conn.execute("UPDATE live_copies SET state='queued', tries=0, due=0 WHERE state='failed'")
        conn.commit()
        self._recount(conn)
        states: dict[int, set] = {}
        for r in conn.execute("SELECT photo_id, state FROM live_copies WHERE photo_id IS NOT NULL"):
            states.setdefault(r[0], set()).add(r[1])
        with self.s.lock:
            for pid, st in states.items():
                if pid in self.s.shots:
                    self.s.shots[pid]["copy"] = self._word(st)

    def _remember(self, conn) -> None:
        db.set_meta(conn, DRIVE_KEY, json.dumps({"path": str(self.drive), "shoot": self.shoot, "id": self.id}))

    def add(self, conn, pid: int | None, rel: str) -> None:
        """One row on the caller's connection and in its commit. A file written again with new bytes goes round again."""
        try:
            st = os.stat(self.root / rel)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size = mtime = None
        r = conn.execute("SELECT src_size, src_mtime FROM live_copies WHERE rel=?", (rel,)).fetchone()
        fresh = r is None or (size is not None and (r[0], r[1]) != (size, mtime))
        if fresh:
            with self.lock:           # the count moves now: the thread may be busy on one file, or paused, for a long time
                self.counts["queued"] += 1
        if r is None:
            conn.execute("INSERT INTO live_copies(rel, photo_id, src_size, src_mtime) VALUES(?,?,?,?)", (rel, pid, size, mtime))
        elif not fresh:
            conn.execute("UPDATE live_copies SET photo_id=? WHERE rel=?", (pid, rel))
        else:
            conn.execute("UPDATE live_copies SET photo_id=?, src_size=?, src_mtime=?, state='queued', dest=NULL, hash=NULL, verified=0, "
                         "stale=0, tries=0, due=0, error=NULL WHERE rel=?", (pid, size, mtime, rel))

    def poke(self) -> None:
        """There is work. Starts the thread again if it had finished: a rename after the stop still has files to move."""
        with self.lock:
            if not self.closed.is_set() and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, name="live-copier", daemon=True)
                self.thread.start()
        self.wake.set()

    def finish(self) -> None:
        self.finishing.set(); self.wake.set()

    def close(self, timeout: float = 10.0) -> None:
        self.closed.set(); self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def info(self) -> dict:
        with self.lock:
            return {"path": str(self.drive), "name": _volume_name(self.drive), "fs": self.fs, "state": self.state, "pending": self.counts["queued"],
                    "copied": self.counts["copied"], "failed": self.counts["failed"], "free_bytes": self.free,
                    "warning": EXFAT_WARNING if self.fs == "exfat" else None}

    # ===== the thread =====
    def _run(self) -> None:
        awake.acquire()
        held = True
        conn = db.connect(self.root)
        try:
            while not self.closed.is_set():
                self._check(conn)
                self._recount(conn)
                worked = False
                if self.state == "ok":
                    try:
                        worked = self._step(conn)
                    except Exception as e:        # one bad row must not end the queue; the shoot's safety is this thread staying up
                        self._report(message=f"The copy hit a problem: {type(e).__name__}")
                self._report()
                if worked:
                    continue
                if self.finishing.is_set():
                    if self.state == "ok" and not self._left(conn):
                        break
                    if held and self.state != "ok":
                        awake.release(); held = False     # the shoot is over and the drive is away: no reason to keep the Mac up
                self.wake.wait(self.poll_s)
                self.wake.clear()
        finally:
            conn.close()
            if held:
                awake.release()
            self._report(force=True)

    def _left(self, conn) -> bool:
        return conn.execute("SELECT 1 FROM live_copies WHERE state='queued' OR (state='copied' AND (verified=0 OR stale>0)) LIMIT 1").fetchone() is not None or self.dirty

    def _present(self, conn) -> bool:
        if not self.drive.is_dir():
            return False
        found = self._marker()
        if self.id is None:
            if found is None:
                found = uuid.uuid4().hex
                self.dest.mkdir(exist_ok=True)    # one level only: never recreates a drive that has gone
                (self.dest / MARKER).write_text(found)
            self.id = found
            self._remember(conn)
        return found == self.id

    def _check(self, conn, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last["check"] < self.poll_s:
            return
        self.last["check"] = now
        free = None
        try:
            if self._present(conn):
                free = _free_bytes(self.drive)
        except OSError:
            free = None
        new = "paused_gone" if free is None else "paused_full" if free < self.need else "ok"
        if free is None:
            self.swept = False
        elif not self.swept:
            self._back(conn)
        was = self.state
        if new == "ok":
            self.need = DRIVE_KEEP_FREE
        with self.lock:
            self.state, self.free = new, free
        if new != was:
            self.wake.set()
            self._report(message={"paused_gone": "The drive is not there. The shoot carries on and copying resumes when it is back.",
                                  "paused_full": "The drive is full. The shoot carries on and copying resumes when there is space.",
                                  "ok": "The drive is back. Copying again."}[new])

    def _back(self, conn) -> None:
        """The drive is here, for the first time or again: what a crash or a pulled cable left half done starts clean."""
        self.swept = True
        for dirpath, _, names in os.walk(self.dest):
            for n in names:
                if n.endswith(".part"):
                    try:
                        os.unlink(os.path.join(dirpath, n))
                    except OSError:
                        pass
        conn.execute("UPDATE live_copies SET state='queued', tries=0, due=0 WHERE state='failed'")
        conn.commit()
        self.dirty = True
        self._recount(conn)

    def _report(self, message: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        d = self.info()
        cur = (d["state"], d["pending"], d["copied"], d["failed"])
        if message is None and not force and (cur == self.sent or now - self.last["status"] < STATUS_EVERY_S):
            return
        self.sent, self.last["status"] = cur, now
        self.s._status(**({"message": message} if message else {}))

    def _step(self, conn) -> bool:
        """One piece of work, the most urgent first: a file in the wrong folder, then a frame not yet on the drive, then
        the read-back. True when there was any."""
        if self.dirty and time.monotonic() >= max(self.manifest_after, self.last["manifest"] + MANIFEST_EVERY_S):
            self._manifest(conn)
            return True
        now = time.time()
        for where, do in (("stale>0 AND state='copied'", self._move), ("state='queued'", self._copy), ("state='copied' AND verified=0", self._verify)):
            r = conn.execute(f"SELECT * FROM live_copies WHERE {where} AND due<=? ORDER BY rowid LIMIT 1", (now,)).fetchone()
            if r is not None:
                do(conn, r)
                return True
        if self.dirty and time.monotonic() >= self.manifest_after:
            self._manifest(conn)
            return True
        return False

    def _recount(self, conn) -> None:
        counts = {"queued": 0, "copied": 0, "failed": 0}
        counts.update({r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM live_copies GROUP BY state")})
        with self.lock:
            self.counts = counts

    @staticmethod
    def _word(states: set) -> str:
        return "failed" if "failed" in states else "queued" if "queued" in states else "copied"

    def _changed(self, conn, r) -> None:
        conn.commit()
        self.dirty = True
        self._recount(conn)
        if r["photo_id"] is not None:
            states = {x[0] for x in conn.execute("SELECT state FROM live_copies WHERE photo_id=?", (r["photo_id"],))}
            self.s._set_copy(r["photo_id"], self._word(states))

    def _retry(self, conn, r, why: str) -> None:
        """Counted against the file: the drive is fine and this one will not go."""
        tries = r["tries"] + 1
        conn.execute("UPDATE live_copies SET state=?, tries=?, due=?, error=? WHERE rel=?",
                     ("failed" if tries >= COPY_TRIES else r["state"], tries, time.time() + COPY_RETRY_S * 2 ** tries, why, r["rel"]))
        self._changed(conn, r)

    def _drive_error(self, conn, r, e: OSError) -> None:
        """A write failed. A drive that went or filled pauses the queue and the file loses nothing; only a healthy drive
        counts it as this file's fault."""
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            try:
                self.need = _free_bytes(self.drive) + 1       # whatever it says, it just refused: wait until something changes
            except OSError:
                pass
        self._check(conn, force=True)
        if self.state == "ok":
            self._retry(conn, r, f"{type(e).__name__}: {e.strerror or e}")

    def _folder(self, conn, pid: int | None) -> Path:
        """Where a frame belongs right now."""
        t = conn.execute("SELECT t.model, t.verdict, l.name FROM live_tags t LEFT JOIN live_products l ON l.id=t.product WHERE t.photo_id=?", (pid,)).fetchone()
        if t is None:
            return Path("unread")
        if t["model"]:
            base = Path("models", _seg(t["model"], "model"))
        else:
            base = Path("unsorted") if t["name"] == UNSORTED else Path("products", _seg(t["name"], "product"))
        return base / "soft" if t["verdict"] == "soft" else base

    def _stem(self, conn, r, own: str) -> str:
        """A JPEG and its RAW keep one name on the drive, or an editor stops seeing them as a pair."""
        q = conn.execute("SELECT dest FROM live_copies WHERE photo_id=? AND rel!=? AND dest IS NOT NULL", (r["photo_id"], r["rel"])).fetchone()
        return Path(q[0]).stem if q else own

    def _mkdirs(self, rel: Path) -> Path:
        """Down from the shoot folder, one level at a time: with the drive gone this fails, where parents=True would quietly
        build /Volumes/<drive> on the Mac's own disk and fill it."""
        cur = self.dest
        for part in rel.parts:
            cur = cur / part
            try:
                os.mkdir(cur)
                _sync_dir(cur.parent)
            except FileExistsError:
                pass
        return cur

    def _claim(self, folder: Path, stem: str, suffix: str, src: Path) -> tuple[Path, str | None]:
        """(the name to use, its hash when those very bytes are already there). A tether counter that wrapped or restarted
        reuses a name for a new picture: that one gets _2. The same picture again gets nothing, which is also what a
        crash between the rename and the db leaves behind."""
        size, mine = os.stat(src).st_size, None
        for k in itertools.count(1):
            p = folder / (f"{stem}{suffix}" if k == 1 else f"{stem}_{k}{suffix}")
            if not os.path.lexists(p):
                return p, None
            if p.stat().st_size == size:
                mine = mine or file_hash(src)
                if file_hash(p) == mine:
                    return p, mine

    def _copy(self, conn, r) -> None:
        conn.execute("UPDATE live_copies SET stale=0 WHERE rel=?", (r["rel"],))   # a rename from here on flags it again and it moves after
        conn.commit()
        src = self.root / r["rel"]
        try:
            size = os.stat(src).st_size
        except OSError:
            if self.root.is_dir():
                self._retry(conn, r, "The source file is gone")
            else:                         # the shoot's own disk is away: not this file's fault, try again in a moment
                conn.execute("UPDATE live_copies SET due=? WHERE rel=?", (time.time() + self.poll_s, r["rel"])); conn.commit()
            return
        try:
            if _free_bytes(self.drive) < size + DRIVE_KEEP_FREE:
                self.need = size + DRIVE_KEEP_FREE
                self._check(conn, force=True)
                return
            folder = self._mkdirs(self._folder(conn, r["photo_id"]))
            final, digest = self._claim(folder, self._stem(conn, r, src.stem), src.suffix, src)
            if digest is None:
                digest = land_file(src, final)
        except OSError as e:
            self._drive_error(conn, r, e)
            return
        conn.execute("UPDATE live_copies SET state='copied', dest=?, hash=?, verified=0, tries=0, due=0, error=NULL WHERE rel=?",
                     (final.relative_to(self.dest).as_posix(), digest, r["rel"]))
        self._changed(conn, r)

    def _move(self, conn, r) -> None:
        """To where the frame belongs now, inside the same shoot folder, so always a rename and never a copy. The file moves
        first and the db second: a crash between finds the old path empty, recopies, and meets its own bytes at the new one.
        stale counts requests, and only the count this move read is cleared: a rename landing mid move gets its own move."""
        old = self.dest / r["dest"]
        want = self._folder(conn, r["photo_id"])
        done = "stale=CASE WHEN stale=? THEN 0 ELSE stale END"
        if old.parent == self.dest / want:
            conn.execute(f"UPDATE live_copies SET {done} WHERE rel=?", (r["stale"], r["rel"])); conn.commit()
            return
        try:
            if not old.exists():
                self._again(conn, r, None)
                return
            folder = self._mkdirs(want)
            stem = self._stem(conn, r, old.stem)
            for k in itertools.count(1):
                new = folder / (f"{stem}{old.suffix}" if k == 1 else f"{stem}_{k}{old.suffix}")
                if not os.path.lexists(new) or os.path.samefile(new, old):    # the second is a change of case on a Mac disk
                    break
            os.rename(old, new)
            _sync_dir(new.parent); _sync_dir(old.parent)
        except OSError as e:
            self._check(conn, force=True)
            if self.state == "ok":        # it stays where it is, whole, under the old name
                conn.execute("UPDATE live_copies SET stale=?, tries=?, due=?, error=? WHERE rel=?",
                             (r["stale"] if r["tries"] + 1 < COPY_TRIES else 0, r["tries"] + 1, time.time() + COPY_RETRY_S, f"Could not move: {e.strerror or e}", r["rel"]))
                conn.commit()
            return
        conn.execute(f"UPDATE live_copies SET dest=?, {done}, tries=0, error=NULL WHERE rel=?", (new.relative_to(self.dest).as_posix(), r["stale"], r["rel"]))
        self._changed(conn, r)
        d = old.parent
        while d != self.dest and self.dest in d.parents:      # only ever an empty folder
            try:
                os.rmdir(d)
            except OSError:
                break
            d = d.parent

    def _again(self, conn, r, why: str | None) -> None:
        """Back to the queue: the file on the drive is missing or wrong."""
        conn.execute("UPDATE live_copies SET state='queued', dest=NULL, hash=NULL, verified=0, stale=0 WHERE rel=?", (r["rel"],))
        if why:
            self._retry(conn, dict(r, state="queued"), why)
        else:
            self._changed(conn, r)

    def _verify(self, conn, r) -> None:
        p = self.dest / r["dest"]
        try:
            got = file_hash(p, uncached=True)
        except FileNotFoundError:
            self._check(conn, force=True)
            if self.state == "ok":
                self._again(conn, r, None)
            return
        except OSError as e:
            self._drive_error(conn, r, e)
            return
        if got == r["hash"]:
            conn.execute("UPDATE live_copies SET verified=1 WHERE rel=?", (r["rel"],)); conn.commit()
            return
        try:                              # bad bytes keep their place out of the way: the name is free for the next try, and nothing is deleted
            for k in itertools.count(1):
                bad = p.with_name(f"{p.name}.bad{k}")
                if not os.path.lexists(bad):
                    break
            os.rename(p, bad)
        except OSError as e:
            self._drive_error(conn, r, e)
            return
        self._again(conn, r, "The copy did not read back the same")

    def _manifest(self, conn) -> None:
        """The whole file from the db every time, through a .part: frames move and the db stays on the Mac, so this is what
        the drive knows about itself when it leaves the building."""
        self.last["manifest"] = time.monotonic()
        rows = conn.execute("SELECT c.dest, c.hash, t.model, t.verdict, t.alerts, l.name FROM live_copies c LEFT JOIN live_tags t ON t.photo_id=c.photo_id "
                            "LEFT JOIN live_products l ON l.id=t.product WHERE c.state='copied' ORDER BY c.rowid").fetchall()
        tmp = self.dest / (MANIFEST + ".part")
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["file", "product", "model", "verdict", "alerts", "hash"])
                for r in rows:
                    alerts = "; ".join(a["text"] for a in json.loads(r["alerts"] or "[]"))
                    w.writerow([r["dest"], _cell(r["name"]), _cell(r["model"]), r["verdict"] or "", alerts, r["hash"]])
                f.flush()
                _full_sync(f.fileno())
            os.replace(tmp, self.dest / MANIFEST)
            _sync_dir(self.dest)
            self.dirty = False
        except OSError:
            self.manifest_after = time.monotonic() + MANIFEST_EVERY_S     # stays dirty; the drive's return writes it
            self._check(conn, force=True)
# ===== end the copy to the drive =====


class LiveSession:
    """One session on one folder. start() returns at once; stop() drains what has landed and then calls on_finished on
    the worker thread. Files that have not landed by then are simply picked up by the next session."""

    def __init__(self, root: Path, log: EventLog | None = None, strobe: bool = False, no_flash: bool = False,
                 poll_s: float = POLL_S, pair_window_s: float = PAIR_WINDOW_S, queue_max: int = QUEUE_MAX,
                 on_shot: Callable[[dict], None] | None = None, on_finished: Callable[["LiveSession"], None] | None = None,
                 drive: str | Path | None = None, shoot: str | None = None, drive_resume: bool = False,
                 drive_poll_s: float = DRIVE_POLL_S, faces: bool = True, products: bool = True):
        self.root = Path(root); self.log = log or EventLog(); self.shoot = shoot
        # Both only put work off. Frames are ordinary index rows either way: n_faces stays NULL, which is what Detect faces
        # looks for, and Find products reads every still, so the skipped work can be done after the shoot.
        self.use_faces, self.use_products = bool(faces), bool(products)
        self.strobe, self.no_flash = bool(strobe), bool(no_flash)
        self.poll_s, self.pair_window_s = poll_s, pair_window_s
        self.on_shot, self.on_finished = on_shot, on_finished
        self.q: queue.Queue = queue.Queue(maxsize=queue_max)
        self.backlog: deque[_Item] = deque()
        self.lock = threading.RLock()
        self.state = "new"
        self.errors = 0
        self.busy = False
        self.was_behind = False
        self.folder_gone = False
        self.active: dict[str, str | None] = {"product": None, "model": None}
        self.shots: dict[int, dict] = {}
        self.products: dict[int, dict] = {}
        self.sets: dict[tuple[str, int], dict] = {}
        self.window = _Window(self.root)
        self.recent: deque[dict] = deque(maxlen=ALERT_WINDOW)    # what the light alerts compare a frame with
        self.copier = Copier(self, drive, shoot, drive_resume, drive_poll_s) if drive else None
        self.seen: dict[str, _Seen] = {}
        self.done: dict[str, tuple[int, float]] = {}
        self.attached: set[str] = set()
        self.first_poll = True
        self._stop = threading.Event()
        self.finished = threading.Event()
        self._threads: list[threading.Thread] = []

    # ===== lifecycle =====
    def start(self) -> None:
        conn = db.connect(self.root)
        try:
            ensure_tables(conn)
            db.stamp_model(conn)
            self._resume(conn)
            if self.copier:
                self.copier.open(conn)
        finally:
            conn.close()
        self.state = "starting"
        self._status()
        self._threads = [threading.Thread(target=self._work, name="live-worker", daemon=True),
                         threading.Thread(target=self._watch, name="live-watcher", daemon=True)]
        for t in self._threads:
            t.start()
        if self.copier:
            self.copier.poke()

    def stop(self) -> None:
        with self.lock:
            if self.state in ("stopping", "saving", "stopped"):
                return
            self.state = "stopping"
        self._stop.set()
        self._status()

    def wait(self, timeout: float | None = None) -> bool:
        return self.finished.wait(timeout)

    def _resume(self, conn) -> None:
        """Everything a stopped or crashed session left: what is done (nothing is decoded twice), the tags, the running
        centroids and the last few frames. A file with a row but no embedding died half way, so it is not done."""
        for r in conn.execute("SELECT rel, size, mtime, status, embed IS NOT NULL, sibling FROM photos"):
            if r[3] == "error" or (r[3] == "ok" and r[4]):
                self.done[r[0]] = (r[1], r[2])
            if r[5]:
                self.attached.add(r[5])
        for r in conn.execute("SELECT id, name, auto FROM live_products ORDER BY id"):
            self.products[r[0]] = {"id": r[0], "name": r[1], "auto": bool(r[2]), "n": 0, "sum": np.zeros(EMBED_DIM, np.float32)}
        rows = conn.execute(
            "SELECT t.*, p.id, p.rel, p.qhash, p.camera, p.taken_at, p.subsec, p.exposure_s, p.fnumber, p.iso, p.ev_bias, p.exp_mode, "
            "p.kind AS photo_kind, p.embed FROM live_tags t JOIN photos p ON p.id=t.photo_id ORDER BY t.n").fetchall()
        for r in rows:
            shot = self._shot_dict(dict(r), self.products[r["product"]]["name"] if r["product"] in self.products else None)
            shot.update(verdict=r["verdict"], reason=r["reason"], kind=r["kind"], flash=r["flash"], model=r["model"],
                        product_id=r["product"], tagged=bool(r["tagged"]), ev=r["ev"], n=r["n"], ms={"total": r["ms"]},
                        alerts=json.loads(r["alerts"] or "[]"), hidden=bool(r["hidden"]), category=_category(r["kind"], r["model"]))
            self.shots[r["id"]] = shot
            p = self.products.get(r["product"])
            if p is not None and r["grey"]:
                p["sum"] += np.frombuffer(r["grey"], np.float16).astype(np.float32); p["n"] += 1
            for kind in ("burst", "bracket"):
                if r[kind]:
                    self._join(kind, r[kind], r["id"])
        for r in rows[-WINDOW:]:
            if r["embed"] and len(r["embed"]) == db.EMBED_BYTES:
                row = {k: r[k] for k in ("id", "rel", "qhash", "camera", "taken_at", "subsec", "exposure_s", "fnumber", "iso",
                                         "ev_bias", "exp_mode")}
                self.window.add(dict(row, kind=r["photo_kind"]), np.frombuffer(r["embed"], np.float16).astype(np.float32))
        for r in rows[-ALERT_WINDOW:]:
            if r["level"] is not None:
                self.recent.append({"level": r["level"], "exposure_s": r["exposure_s"], "fnumber": r["fnumber"], "iso": r["iso"], "product": r["product"]})
        saved = db.get_meta(conn, ACTIVE_KEY)     # only a session that never stopped cleanly leaves this behind
        if saved:
            try:
                self.active.update({k: v for k, v in json.loads(saved).items() if k in self.active})
            except ValueError:
                pass

    # ===== the watcher =====
    def _listing(self) -> dict[str, tuple[int, float]]:
        out: dict[str, tuple[int, float]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and d not in DEV_DIRS]
            for fn in filenames:
                if fn.startswith(".") or fn.lower().endswith(PROJECT_SUFFIXES) or Path(fn).suffix.lower() not in IMAGE_EXTS:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size:
                    out[os.path.relpath(p, self.root)] = (st.st_size, st.st_mtime)
        return out

    @staticmethod
    def _stem(rel: str) -> tuple[str, str]:
        p = Path(rel)
        return str(p.parent), p.stem.lower()

    def poll(self, now: float | None = None) -> None:
        """One look at the folder: note what changed, hand over what has landed. Public so a test can step it."""
        now = time.monotonic() if now is None else now
        if not self.root.is_dir():
            if not self.folder_gone:
                self.folder_gone = True
                self._status(message="The shoot folder is not there. Plug the disk back in; nothing has been lost.")
            return
        if self.folder_gone:
            self.folder_gone = False
            self._status(message="The shoot folder is back.")
        files = self._listing()
        with self.lock:
            active = dict(self.active)
        if self.first_poll:          # these landed while nobody was tagging, so the active product says nothing about them
            active = {"product": None, "model": None}
        self.first_poll = False
        for rel in [r for r in self.seen if r not in files]:
            del self.seen[rel]       # gone before it landed: a tether app's temp name, or moved on by an import
        for rel, (size, mtime) in files.items():
            if self.done.get(rel) == (size, mtime) or rel in self.attached:
                continue
            s = self.seen.get(rel)
            if s is None:
                self.seen[rel] = _Seen(size, mtime, now, active["product"], active["model"])
            elif (s.size, s.mtime) != (size, mtime):
                s.size, s.mtime, s.same, s.stable_at = size, mtime, 0, None
            else:
                s.same += 1
                if s.same >= STABLE_POLLS and s.stable_at is None:
                    s.stable_at = now
        std: dict[tuple[str, str], str] = {}
        raws: dict[tuple[str, str], str] = {}
        for rel in files:
            (raws if Path(rel).suffix.lower() in RAW_EXTS else std)[self._stem(rel)] = rel
        landed = [r for r, s in self.seen.items() if s.stable_at is not None]
        landed.sort(key=lambda r: (Path(r).suffix.lower() in RAW_EXTS, self.seen[r].mtime, r))    # JPEGs first
        for rel in landed:
            s = self.seen.get(rel)
            if s is None:
                continue                                 # a RAW its JPEG already claimed, earlier in this pass
            item = _Item(rel, s.size, s.mtime, s.product, s.model, first=s.first)
            if Path(rel).suffix.lower() in RAW_EXTS:
                partner = std.get(self._stem(rel))
                if partner is None:
                    if now - s.stable_at < self.pair_window_s:
                        continue                         # its JPEG may be a moment behind
                elif partner in self.seen and self.seen[partner].stable_at is None:
                    continue                             # its JPEG is still being written
                else:
                    item.attach_to = partner
                    self.attached.add(rel)
            else:
                item.sibling = raws.get(self._stem(rel))
                if item.sibling:
                    self.attached.add(item.sibling)      # the JPEG's row names it, so it never gets a row of its own
                    self.seen.pop(item.sibling, None)
            del self.seen[rel]
            self.done[rel] = (s.size, s.mtime)
            self.backlog.append(item)
        self._feed()

    def _feed(self) -> None:
        while self.backlog:
            try:
                self.q.put_nowait(self.backlog[0])
            except queue.Full:
                break                # never dropped: it stays in the backlog until the worker has caught up
            self.backlog.popleft()
        self._check_behind()

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:        # a bad poll must not end the shoot; the next one tries again
                self._status(message=f"Could not read the folder: {type(e).__name__}")
            self._stop.wait(self.poll_s)
        while self.backlog:               # what has landed is finished before the session ends
            self.q.put(self.backlog.popleft())
        self.q.put(_STOP)

    def depth(self) -> int:
        return self.q.qsize() + len(self.backlog) + int(self.busy)

    def _check_behind(self) -> None:
        behind = self.depth() >= BEHIND_AT
        if behind != self.was_behind:
            self.was_behind = behind
            self._status()

    # ===== the worker =====
    def _warm(self) -> None:
        """The encoder and the face networks load now, not on the first frame the photographer is waiting for."""
        from .embed import get_embedder
        blank = Image.new("RGB", (64, 64), (128, 128, 128))
        get_embedder().encode_images([blank, blank])
        if self.use_faces:
            index._face_engine()

    def _work(self) -> None:
        conn = None
        try:
            conn = db.connect(self.root)
            try:
                self._warm()
            except Exception as e:
                self.errors += 1
                self._status(message=f"The models would not load: {type(e).__name__}")
            with self.lock:
                if self.state == "starting":
                    self.state = "live"
            self._status()
            while True:
                item = self.q.get()
                if item is _STOP:
                    break
                self.busy = True
                try:
                    self._handle(conn, item)
                except Exception as e:
                    self.errors += 1
                    self.log.emit("status", **self._status_body(), message=f"Could not finish {Path(item.rel).name}: {type(e).__name__}", file=item.rel)
                finally:
                    self.busy = False
                    self._check_behind()
            db.set_meta(conn, ACTIVE_KEY, "")     # a clean stop: tomorrow's session does not inherit today's product
        finally:
            if conn is not None:
                conn.close()
            if self.copier:
                self.copier.finish()      # it carries on past the stop until the queue is empty; the save does not wait for it
            with self.lock:
                self.state = "saving"
            try:
                if self.on_finished:
                    self.on_finished(self)
            finally:
                with self.lock:
                    self.state = "stopped"
                self.finished.set()

    def _handle(self, conn, item: _Item) -> None:
        if item.attach_to is not None:
            conn.execute("UPDATE photos SET sibling=? WHERE rel=?", (item.rel, item.attach_to))
            pid = conn.execute("SELECT id FROM photos WHERE rel=?", (item.attach_to,)).fetchone()
            if self.copier:               # the RAW travels with the shot it belongs to
                self.copier.add(conn, pid[0] if pid else None, item.rel)
            conn.commit()
            if self.copier:
                self.copier.poke()
                if pid:
                    self._set_copy(pid[0], "queued")
            return
        t0 = time.monotonic()
        if item.sibling:
            # The RAW went in alone and its JPEG came late. The row becomes the JPEG's, keeping its id and tags, or a
            # later scan would pair the two and flag the RAW's row missing.
            conn.execute("UPDATE photos SET rel=?, sibling=? WHERE rel=? AND NOT EXISTS (SELECT 1 FROM photos WHERE rel=?)",
                         (item.rel, item.sibling, item.sibling, item.rel))
            conn.commit()
        res = index.process_one((str(self.root), item.rel, self.use_faces))
        if res["error"]:
            if not self.root.is_dir():
                self.done.pop(item.rel, None)         # the disk went away; this file is still to do
                return
            if not (self.root / item.rel).is_file():
                return                                # moved on by the tether app's own import: not ours to report
            db.mark_error(conn, item.rel, item.size, item.mtime)
            if self.copier:               # we could not read it; a RAW converter may. It goes on the drive all the same
                pid = conn.execute("SELECT id FROM photos WHERE rel=?", (item.rel,)).fetchone()
                self.copier.add(conn, pid[0] if pid else None, item.rel)
                conn.commit()
                self.copier.poke()
            self.errors += 1
            self.log.emit("status", **self._status_body(), message=f"Could not read {Path(item.rel).name}", file=item.rel)
            return
        row = res["row"]
        had = conn.execute("SELECT sibling FROM photos WHERE rel=?", (item.rel,)).fetchone()
        row["sibling"] = item.sibling or (had[0] if had else None)
        pid = db.upsert_photo(conn, row)
        db.replace_faces(conn, pid, res["faces"])
        t1 = time.monotonic()
        idx = db.index_dir(self.root)
        with Image.open(idx / "thumbs" / f"{row['qhash']}.jpg") as im:
            thumb = im.convert("RGB")
        batch, grey = [thumb], None
        if self.use_products:
            with Image.open(idx / "grid" / f"{row['qhash']}.jpg") as im:
                batch.append(im.convert("L").convert("RGB"))     # as products.grey_embeddings: colour out, shape in
        from .embed import get_embedder
        vecs = np.asarray(get_embedder().encode_images(batch), np.float32)
        colour = vecs[0]
        if self.use_products:
            grey = vecs[1] / (np.linalg.norm(vecs[1]) or 1.0)
        t2 = time.monotonic()
        rgb = np.asarray(thumb)
        region = None if row.get("n_faces") else product_region(rgb)     # a person is not the product the clip alert speaks of
        verdict = judge(row, thumb, region)
        level, clip = light_level(rgb), clipped_share(rgb, region)
        with self.lock:
            old = self.shots.get(pid)
            if old is not None:           # the same frame written again: it keeps its tags, the centroid does not count it twice
                product, model, tagged, n = self.products[old["product_id"]], old["model"], old["tagged"], old["n"]
            else:
                product, tagged = self._assign(conn, item.product, grey, verdict["verdict"] == "soft")
                model, n = item.model, len(self.shots) + 1
            shot = self._shot_dict(dict(row, id=pid), product["name"])
            light = {"level": level, "exposure_s": row.get("exposure_s"), "fnumber": row.get("fnumber"), "iso": row.get("iso"), "product": product["id"]}
            shot.update(verdict, product_id=product["id"], model=model, tagged=tagged, n=n,
                        flash=flash_state(row.get("flash"), self.strobe, self.no_flash),
                        alerts=light_alerts(dict(light, focal_mm=row.get("focal_mm")), self.recent, self.strobe, clip),
                        category=_category(verdict["kind"], model), copy="queued" if self.copier else None)
            if old is not None:
                shot["burst"], shot["bracket"], shot["ev"], shot["hidden"] = old["burst"], old["bracket"], old["ev"], old["hidden"]
            else:
                self.recent.append(light)
            self.shots[pid] = shot
            self.window.add(dict(row, id=pid), colour)
            changed = self._regroup()
        db.set_embed(conn, pid, colour)
        total = (time.monotonic() - t0) * 1000
        conn.execute("INSERT OR REPLACE INTO live_tags(photo_id, n, product, model, tagged, verdict, reason, kind, flash, burst, bracket, ev, ms, grey, alerts, level, hidden) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, n, product["id"], model, int(tagged), shot["verdict"], shot["reason"], shot["kind"], shot["flash"],
                      (shot["burst"] or {}).get("id"), (shot["bracket"] or {}).get("id"), shot["ev"], total,
                      grey.astype(np.float16).tobytes() if grey is not None else None,
                      json.dumps(shot["alerts"]), level, int(shot["hidden"])))
        for i in changed:
            s = self.shots[i]
            conn.execute("UPDATE live_tags SET burst=?, bracket=?, ev=? WHERE photo_id=?",
                         ((s["burst"] or {}).get("id"), (s["bracket"] or {}).get("id"), s["ev"], i))
        if self.copier:                   # one row in this commit is all a copy costs the worker
            self.copier.add(conn, pid, item.rel)
            if row["sibling"]:
                self.copier.add(conn, pid, row["sibling"])
        conn.commit()                     # the embedding and the tags land together: a crash leaves the file not done
        if self.copier:
            self.copier.poke()
        shot["ms"] = {"decode": round((t1 - t0) * 1000, 1), "embed": round((t2 - t1) * 1000, 1), "total": round(total, 1),
                      "landed": round((time.monotonic() - item.first) * 1000, 1) if item.first else None}
        self.log.emit("shot", **shot, queue=self.depth() - 1)
        if self.on_shot:
            self.on_shot(shot)

    @staticmethod
    def _shot_dict(row: dict, product: str | None) -> dict:
        qh = row["qhash"]
        return {"id": row["id"], "file": row["rel"], "thumb": f"/api/thumb/{qh}?size=grid", "preview": f"/api/thumb/{qh}?size=preview",
                "verdict": None, "reason": None, "kind": None, "product": product, "product_id": None, "model": None, "tagged": False,
                "burst": None, "bracket": None, "ev": None, "flash": "unknown", "ms": {}, "n": 0, "taken_at": row.get("taken_at"), "sharp": row.get("sharp"),
                "alerts": [], "hidden": False, "category": "product", "copy": None}

    # ===== products =====
    def _new_product(self, conn, name: str | None) -> dict:
        auto = name is None
        if auto:
            taken = {p["name"] for p in self.products.values()}
            k = sum(p["auto"] for p in self.products.values()) + 1
            while f"product {k}" in taken:
                k += 1
            name = f"product {k}"
        pid = conn.execute("INSERT INTO live_products(name, auto) VALUES(?, ?)", (name, int(auto))).lastrowid
        p = {"id": pid, "name": name, "auto": auto, "n": 0, "sum": np.zeros(EMBED_DIM, np.float32)}
        self.products[pid] = p
        self.log.emit("product_created", id=pid, name=name, auto=auto)
        return p

    def _assign(self, conn, active: str | None, grey: np.ndarray | None, soft: bool = False) -> tuple[dict, bool]:
        """(product, tagged): the active product when there is one, else the nearest running centroid at PRODUCT_SIM, else
        a new product. Either way the frame joins that centroid, so a scanned barcode teaches the auto assignment. With
        products off there are no centroids: the active product, else the one Unsorted pile."""
        if not self.use_products:
            name = active or UNSORTED
            p = next((x for x in self.products.values() if x["name"] == name), None) or self._new_product(conn, name)
            p["n"] += 1
            return p, bool(active)
        if active:
            p = next((x for x in self.products.values() if x["name"] == active), None) or self._new_product(conn, active)
        else:
            best, sim = None, -1.0
            for x in self.products.values():
                norm = float(np.linalg.norm(x["sum"]))
                c = float(grey @ x["sum"]) / norm if norm else -1.0
                if c > sim:
                    best, sim = x, c
            # blur moves a frame far from its own product, so a soft frame never opens a folder when one exists, and
            # it never pulls a centroid off the sharp frames
            p = best if best is not None and (soft or sim >= PRODUCT_SIM) else self._new_product(conn, None)
        if not soft:
            p["sum"] = p["sum"] + grey
        p["n"] += 1
        return p, bool(active)

    def set_active(self, product=_UNSET, model=_UNSET) -> dict:
        """Change what new frames are tagged with. A barcode scanner types into this field and ends with a return, so
        the text is trimmed; empty clears it. Frames already on their way keep what was active when they appeared."""
        with self.lock:
            if product is not _UNSET:
                self.active["product"] = (product or "").strip() or None
            if model is not _UNSET:
                self.active["model"] = (model or "").strip() or None
            out = dict(self.active)
        conn = db.connect(self.root)
        try:
            db.set_meta(conn, ACTIVE_KEY, json.dumps(out))
        finally:
            conn.close()
        self._status()
        return out

    def renamed(self, pid: int, old: str, name: str) -> None:
        with self.lock:
            if pid in self.products:
                self.products[pid]["name"] = name
            for s in self.shots.values():
                if s["product_id"] == pid:
                    s["product"] = name
            changed = self.active["product"] == old
        if changed:
            self.set_active(product=name)

    def retag(self, pid: int, product=_UNSET, model=_UNSET) -> dict:
        """Moves one frame to another product or model by hand. Its centroid moves with it, and a file already on the
        drive follows: the row is only flagged here, the copier does the moving. No sqlite under self.lock, as in
        set_active: the worker can hold the write lock while it waits for self.lock, and this would sit out the busy timeout."""
        with self.lock:
            if pid not in self.shots:
                raise KeyError(pid)
        name = None
        if product is not _UNSET:
            name = (product or "").strip()
            if not name:
                raise ValueError("a frame needs a product")
        conn = db.connect(self.root)
        try:
            r = conn.execute("SELECT grey FROM live_tags WHERE photo_id=?", (pid,)).fetchone()
            if name is not None:
                with self.lock:
                    known = any(x["name"] == name for x in self.products.values())
                if not known:
                    conn.execute("INSERT OR IGNORE INTO live_products(name, auto) VALUES(?, 0)", (name,)); conn.commit()
                    new = conn.execute("SELECT id FROM live_products WHERE name=?", (name,)).fetchone()[0]
                    with self.lock:
                        made = new not in self.products
                        self.products.setdefault(new, {"id": new, "name": name, "auto": False, "n": 0, "sum": np.zeros(EMBED_DIM, np.float32)})
                    if made:
                        self.log.emit("product_created", id=new, name=name, auto=False)
            with self.lock:
                s = self.shots[pid]
                if name is not None:
                    was = self.products.get(s["product_id"])
                    p = next(x for x in self.products.values() if x["name"] == name)
                    if p is not was:
                        grey = np.frombuffer(r[0], np.float16).astype(np.float32) if r and r[0] and s["verdict"] != "soft" else 0.0
                        if was is not None:
                            was["sum"] = was["sum"] - grey; was["n"] -= 1
                        p["sum"] = p["sum"] + grey; p["n"] += 1
                        s.update(product=p["name"], product_id=p["id"], tagged=True)
                if model is not _UNSET:
                    s["model"] = (model or "").strip() or None
                s["category"] = _category(s["kind"], s["model"])
                out = dict(s)
            conn.execute("UPDATE live_tags SET product=?, model=?, tagged=? WHERE photo_id=?", (out["product_id"], out["model"], int(out["tagged"]), pid))
            conn.execute("UPDATE live_copies SET stale=stale+1 WHERE photo_id=?", (pid,))
            conn.commit()
        finally:
            conn.close()
        self.log.emit("shot_update", **out)
        if self.copier:
            self.copier.poke()
        return out

    def set_hidden(self, pid: int, hidden: bool) -> dict:
        """Takes a frame off the client's screen. Nothing else changes: it stays in the shoot and on the drive."""
        with self.lock:
            s = self.shots[pid]
            s["hidden"] = bool(hidden)
            out = dict(s)
        conn = db.connect(self.root)
        try:
            conn.execute("UPDATE live_tags SET hidden=? WHERE photo_id=?", (int(hidden), pid))
            conn.commit()
        finally:
            conn.close()
        self.log.emit("shot_update", **out)
        return out

    def _set_copy(self, pid: int, value: str) -> None:
        with self.lock:
            s = self.shots.get(pid)
            if s is None or s["copy"] == value:
                return
            s["copy"] = value
            out = dict(s)
        self.log.emit("shot_update", **out)

    # ===== bursts and brackets =====
    def _join(self, kind: str, gid: int, fid: int) -> None:
        g = self.sets.setdefault((kind, gid), {"id": gid, "n": 0, "ids": []})
        if fid not in g["ids"]:
            g["ids"].append(fid); g["n"] = len(g["ids"])
        if fid in self.shots:
            self.shots[fid][kind] = g

    def _leave(self, kind: str, fid: int) -> set[int]:
        """Takes a frame out of its set; a set left with one frame is no set. Returns the frames that changed."""
        g = self.shots[fid][kind]
        g["ids"].remove(fid); g["n"] = len(g["ids"])
        self.shots[fid][kind] = None
        out = {fid}
        if g["n"] == 1:
            out.add(g["ids"][0])
            self.shots[g["ids"][0]][kind] = None
            del self.sets[(kind, g["id"])]
        return out

    def _regroup(self) -> set[int]:
        """Runs the rules over the window and carries the result onto the shots. A set keeps the id of its first frame
        even after that frame slides out of the window, so a 200 frame burst stays one burst. Returns the frames whose
        group moved. Three frames make a bracket, so a frame can turn from burst to bracket here."""
        bursts, brackets = self.window.groups()
        changed: set[int] = set()
        for kind, runs in (("bracket", brackets), ("burst", bursts)):
            for run in runs:
                ids = [p["id"] for p in run if p["id"] in self.shots]
                have = [self.shots[i][kind]["id"] for i in ids if self.shots[i][kind]]
                gid = min(have) if have else min(ids)
                for i in ids:
                    cur = self.shots[i][kind]
                    if not cur or cur["id"] != gid:
                        if cur:
                            changed |= self._leave(kind, i)
                        self._join(kind, gid, i); changed.add(i)
        for run in brackets:
            for p in run:
                s = self.shots.get(p["id"])
                if s is None:
                    continue
                if s["ev"] != p["ev"]:
                    s["ev"] = p["ev"]; changed.add(s["id"])
                if s["burst"]:
                    changed |= self._leave("burst", s["id"])
        return changed

    # ===== reporting =====
    def _status_body(self) -> dict:
        with self.lock:
            n = len(self.shots)
            depth = self.depth()
            return {"state": self.state, "queue": depth, "behind": depth >= BEHIND_AT, "done": n, "errors": self.errors,
                    "active": dict(self.active), "drive": self.copier.info() if self.copier else drive_off()}

    def _status(self, **extra) -> None:
        self.log.emit("status", **self._status_body(), **extra)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self._status_body(), running=self.state not in ("stopped", "new"), folder=str(self.root),
                        shoot=self.shoot, strobe=self.strobe, no_flash=self.no_flash, faces=self.use_faces, products_on=self.use_products,
                        products=[{"id": p["id"], "name": p["name"], "n": p["n"]} for p in self.products.values()],
                        models=self._models(), shots=list(self.shots.values()), seq=self.log.seq)

    def _models(self) -> list[dict]:
        counts: dict[str, int] = {}
        for s in self.shots.values():
            if s["model"]:
                counts[s["model"]] = counts.get(s["model"], 0) + 1
        return [{"name": k, "n": v} for k, v in counts.items()]


# ===== the store export =====
def store_plan(conn, products: list[str] | None = None, models: list[str] | None = None, kind: str | None = None,
               include_soft: bool = False) -> dict[str, list[dict]]:
    """{product name: frames in capture order} for the filters. Sharp frames only unless include_soft."""
    ensure_tables(conn)
    rows = conn.execute("SELECT p.rel, p.width, p.height, p.taken_at, p.subsec, t.verdict, t.kind, t.model, t.n, l.name "
                        "FROM live_tags t JOIN photos p ON p.id=t.photo_id JOIN live_products l ON l.id=t.product "
                        "WHERE p.status='ok'").fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        if (products is not None and r["name"] not in products) or (models is not None and r["model"] not in models):
            continue
        if (kind and r["kind"] != kind) or (not include_soft and r["verdict"] != "sharp"):
            continue
        out.setdefault(r["name"], []).append(dict(r))
    for frames in out.values():
        frames.sort(key=lambda f: (f["taken_at"] or "~", f["subsec"] or 0.0, _frame_no(f["rel"]) or 0, f["n"]))
    return out


def store_file(src: Path, dst: Path, width: int | None, height: int | None) -> None:
    """One frame into the store folder. A JPEG inside the limit is copied as it is; anything else is decoded at the limit
    (a RAW from its embedded preview), so no frame is ever enlarged. Written through a .part: never half a file."""
    tmp = dst.with_name(dst.name + ".part")
    try:
        if src.suffix.lower() in (".jpg", ".jpeg") and width and height and max(width, height) <= STORE_MAX_EDGE:
            shutil.copyfile(src, tmp)
        else:
            im = load_preview(src, STORE_MAX_EDGE)
            kw = {"icc_profile": im.info["icc_profile"]} if im.info.get("icc_profile") else {}
            im.save(tmp, "JPEG", quality=STORE_QUALITY, **kw)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def export_store(root: Path, dest: Path, plan: dict[str, list[dict]], progress: Callable[[dict], None] | None = None) -> dict:
    """<dest>/<product>/<SKU>_01.jpg, numbered in capture order. The folder and the SKU are the product's name, so a
    scanned barcode comes out as the file name the store wants."""
    root, dest = Path(root), Path(dest)
    if under(dest, root):
        raise ValueError("the store export cannot go inside the shoot folder")
    total = sum(len(v) for v in plan.values())
    done = failed = 0
    for name, frames in plan.items():
        sku = safe_segment(name) if name not in (".", "..") else "product"
        folder = dest / sku
        folder.mkdir(parents=True, exist_ok=True)
        for k, f in enumerate(frames, 1):
            try:
                store_file(root / f["rel"], folder / f"{sku}_{k:02d}.jpg", f["width"], f["height"])
                done += 1
            except Exception:
                failed += 1
            if progress:
                progress({"done": done, "failed": failed, "total": total})
    return {"done": done, "failed": failed, "total": total, "path": str(dest)}


def under(path: Path, root: Path) -> bool:
    """path is root or inside it, with symlinks followed."""
    return Path(path).resolve().is_relative_to(Path(root).resolve())
# ===== end the store export =====


class StartReq(BaseModel):
    folder: str
    strobe: bool = False      # the photographer declared strobes: every frame counts as flash
    no_flash: bool = False    # the photographer declared no flash: a clear or missing tag counts as none
    faces: bool = True        # false: no face detection per shot, every frame is judged as product
    products: bool = True     # false: no running centroids, frames go in the active product or the Unsorted pile
    drive: str | None = None  # copy onto this folder. Left out: the drive this shoot used last. null: no copy
    shoot: str | None = None  # the folder on the drive; the watched folder's name when empty


class ActiveReq(BaseModel):
    product: str | None = None      # a field left out stays as it is; null or empty clears it
    model: str | None = None


class RenameProductReq(BaseModel):
    product: int
    name: str


class AssignReq(BaseModel):
    id: int
    product: str | None = None      # a field left out stays as it is
    model: str | None = None        # null or empty clears the model


class HideReq(BaseModel):
    id: int
    hidden: bool


class PickReq(BaseModel):
    purpose: str


class ExportStoreReq(BaseModel):
    dest: str
    products: list[str] | None = None
    models: list[str] | None = None
    kind: str | None = None         # model or product
    include_soft: bool = False


class Hub:
    """The server's side of live shooting: at most one session, and the event log that outlives them."""

    def __init__(self, state: dict, switch_root: Callable[[Path], dict] | None = None):
        self.state, self.switch_root = state, switch_root
        self.log = EventLog()
        self.session: LiveSession | None = None
        self.lock = threading.Lock()
        self.export = {"running": False, "done": 0, "failed": 0, "total": 0, "path": None, "error": None}

    def running(self) -> bool:
        return self.session is not None and not self.session.finished.is_set()

    def _open(self, folder: Path) -> None:
        here = self.state.get("root")
        if here is not None and Path(here).resolve() == folder.resolve():
            return
        if self.switch_root:
            self.switch_root(folder)
        else:
            self.state["root"] = folder; self.state["index"] = Index(folder)

    def start(self, req: StartReq) -> dict:
        folder = Path(req.folder).expanduser()
        if req.strobe and req.no_flash:
            raise HTTPException(400, "pick strobes or no flash, not both")
        if not folder.is_dir():
            raise HTTPException(400, "that folder is not there")
        if req.drive:
            why = drive_preflight(folder, req.drive, req.shoot)
            if why:
                raise HTTPException(400, why)
        with self.lock:
            if self.running():
                raise HTTPException(409, "a live session is already running")
            s = self.state
            if s["running"]:
                raise HTTPException(409, "cannot start a live session while a scan is running")
            if any(s[k]["running"] for k in ("export", "focus", "fps", "clipfaces") if k in s):
                raise HTTPException(409, "cannot start a live session until the running job has finished")
            self._open(folder)
            conn = db.connect(folder)
            try:
                used = db.embed_model(conn)
                drive, shoot, remembered = req.drive, req.shoot, False
                if "drive" not in req.model_fields_set:     # a restart after a crash comes back to the same drive
                    saved = json.loads(db.get_meta(conn, DRIVE_KEY) or "{}")
                    drive, shoot, remembered = saved.get("path"), saved.get("shoot"), bool(saved.get("path"))
                elif not drive:
                    db.set_meta(conn, DRIVE_KEY, "")
            finally:
                conn.close()
            if used and used != MODEL_TAG:
                raise HTTPException(409, "this shoot was read with another encoder; rescan it before shooting live into it")
            if self.session is not None and self.session.copier:
                self.session.copier.close()     # one copier at a time; what the last one left is still in its db
            s["running"] = True       # what every scan gate and folder switch already refuses on
            s["progress"] = {"stage": "live", "done": 0, "total": 0, "stage_started": time.time()}
            self.session = LiveSession(folder, self.log, req.strobe, req.no_flash, on_shot=self._on_shot, on_finished=self._on_finished,
                                       drive=drive, shoot=shoot, drive_resume=remembered, faces=req.faces, products=req.products)
            awake.acquire()
            try:
                self.session.start()
            except BaseException:
                awake.release()
                s["running"] = False
                s["progress"] = {"stage": "idle", "done": 0, "total": 0}
                self.session = None
                raise
            return self.session.snapshot()

    def _on_shot(self, shot: dict) -> None:
        n = len(self.session.shots)
        self.state["stale"] = True
        self.state["progress"] = dict(self.state["progress"], done=n, total=n + self.session.depth())

    def _project_dir(self, root: Path) -> Path:
        """The folder the project was last saved to, while that is somewhere else than the shoot, else the export
        destination. The watched folder stays read-only for the whole session, the save included."""
        conn = db.connect(root)
        try:
            remembered = db.get_meta(conn, "project_path")
        finally:
            conn.close()
        if remembered and Path(remembered).parent.is_dir() and not under(Path(remembered).parent, root):
            return Path(remembered).parent
        base = Path(self.state.get("export_base") or export_root())
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _on_finished(self, session: LiveSession) -> None:
        """Runs once, on the worker thread, after the last frame. The server is handed back whatever happens here."""
        out = {"project": None, "error": None}
        try:
            session._status()
            out["project"] = str(bundle.export_bundle(session.root, self._project_dir(session.root)))
        except Exception as e:
            out["error"] = f"The project file could not be saved: {type(e).__name__}"
        finally:
            self.state["running"] = False
            self.state["stale"] = True
            self.state["progress"] = {"stage": "idle", "done": 0, "total": 0}
            awake.release()
            n = len(session.shots)
            self.log.emit("status", **dict(session._status_body(), state="stopped"), project=out["project"], message=out["error"],
                          # products.find_products does the full re-cluster; it is the person's call, so it is only offered
                          recluster={"offer": n >= PRODUCT_MIN_PHOTOS, "url": "/api/products/find", "photos": n})

    def stop(self) -> dict:
        if not self.running():
            raise HTTPException(409, "no live session is running")
        self.session.stop()
        return self.session.snapshot()

    def snapshot(self) -> dict:
        if self.session is None:
            return {"running": False, "state": "stopped", "folder": None, "queue": 0, "behind": False, "done": 0, "errors": 0,
                    "active": {"product": None, "model": None}, "strobe": False, "no_flash": False, "faces": True, "products_on": True,
                    "products": [], "models": [], "shots": [],
                    "drive": drive_off(), "seq": self.log.seq, "export": dict(self.export)}
        return dict(self.session.snapshot(), export=dict(self.export))

    def active(self, req: ActiveReq) -> dict:
        if not self.running():
            raise HTTPException(409, "no live session is running")
        given = {k: getattr(req, k) for k in req.model_fields_set}
        return self.session.set_active(**given)

    def rename(self, req: RenameProductReq) -> dict:
        name = req.name.strip()
        root = self.session.root if self.session else self.state.get("root")
        if not name:
            raise HTTPException(400, "a product needs a name")
        if root is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(root)
        try:
            ensure_tables(conn)
            r = conn.execute("SELECT name FROM live_products WHERE id=?", (req.product,)).fetchone()
            if r is None:
                raise HTTPException(404, "no such product")
            try:
                conn.execute("UPDATE live_products SET name=? WHERE id=?", (name, req.product))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "another product already has that name")
            conn.execute("UPDATE live_copies SET stale=stale+1 WHERE photo_id IN (SELECT photo_id FROM live_tags WHERE product=?)", (req.product,))
            conn.commit()
            old = r[0]
        finally:
            conn.close()
        if self.session:
            self.session.renamed(req.product, old, name)
            if self.session.copier:
                self.session.copier.poke()
        self.log.emit("product_renamed", id=req.product, name=name, old=old)
        return {"id": req.product, "name": name}

    def _shot_session(self) -> LiveSession:
        if self.session is None:
            raise HTTPException(409, "no live session is running")
        return self.session

    def assign(self, req: AssignReq) -> dict:
        s = self._shot_session()
        try:
            return s.retag(req.id, **{k: getattr(req, k) for k in req.model_fields_set if k != "id"})
        except KeyError:
            raise HTTPException(404, "no such shot")
        except ValueError as e:
            raise HTTPException(400, str(e))

    def hide(self, req: HideReq) -> dict:
        try:
            return self._shot_session().set_hidden(req.id, req.hidden)
        except KeyError:
            raise HTTPException(404, "no such shot")

    def original(self, pid: int):
        """The biggest picture there is of a shot, for 100% zoom: the file itself when it is a JPEG, else the whole of the
        RAW's embedded preview, never scaled down."""
        root = self.session.root if self.session else self.state.get("root")
        if root is None:
            raise HTTPException(400, "no folder open")
        conn = db.connect(root)
        try:
            r = conn.execute("SELECT rel FROM photos WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        src = Path(root) / r[0] if r else None
        if src is None or not src.is_file():
            raise HTTPException(404, "no such shot")
        if src.suffix.lower() in (".jpg", ".jpeg"):
            return FileResponse(src, media_type="image/jpeg")
        try:
            im = load_preview(src, ORIGINAL_EDGE)
        except DecodeError:
            raise HTTPException(404, "that file has no picture to show")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=STORE_QUALITY)
        return Response(buf.getvalue(), media_type="image/jpeg")

    def export_store(self, req: ExportStoreReq) -> dict:
        root = self.session.root if self.session else self.state.get("root")
        if root is None:
            raise HTTPException(400, "no folder open")
        dest = Path(req.dest).expanduser()
        if not dest.is_absolute():
            raise HTTPException(400, "pick a full path for the store export")
        if under(dest, root):
            raise HTTPException(400, "the store export cannot go inside the shoot folder")
        if req.kind not in (None, "model", "product"):
            raise HTTPException(400, "kind is model or product")
        with self.lock:
            if self.export["running"]:
                raise HTTPException(409, "a store export is already running")
            conn = db.connect(root)
            try:
                plan = store_plan(conn, req.products, req.models, req.kind, req.include_soft)
            finally:
                conn.close()
            total = sum(len(v) for v in plan.values())
            self.export = {"running": True, "done": 0, "failed": 0, "total": total, "path": str(dest), "error": None}
        threading.Thread(target=self._run_export, args=(root, dest, plan), daemon=True).start()
        return {"started": True, "total": total, "products": len(plan), "path": str(dest)}

    def _run_export(self, root: Path, dest: Path, plan: dict) -> None:
        def step(d: dict) -> None:
            self.export.update(d)
            self.log.emit("status", export=dict(self.export))
        try:
            with awake.hold():
                self.export.update(export_store(root, dest, plan, step))
        except Exception as e:
            self.export["error"] = f"The store export stopped: {type(e).__name__}"
        finally:
            self.export["running"] = False
            self.log.emit("status", export=dict(self.export))


def install(app: FastAPI, switch_root: Callable[[Path], dict] | None = None) -> Hub:
    """Adds /api/live/* to the app. switch_root is server.py's _switch_root, for a session started on another folder."""
    hub = Hub(app.state.photosort, switch_root)
    app.state.live = hub
    r = APIRouter(prefix="/api/live")

    # plain def on purpose, here and below: the long poll blocks a threadpool thread, never the event loop
    @r.get("/events")
    def events(after: int = 0, wait: float = LONG_POLL_S):
        return hub.log.since(after, wait)

    @r.get("/state")
    def live_state():
        return hub.snapshot()

    @r.post("/start")
    def start(req: StartReq):
        return hub.start(req)

    @r.post("/stop")
    def stop():
        return hub.stop()

    @r.post("/active")
    def active(req: ActiveReq):
        return hub.active(req)

    @r.post("/rename-product")
    def rename_product(req: RenameProductReq):
        return hub.rename(req)

    @r.post("/assign")
    def assign(req: AssignReq):
        return hub.assign(req)

    @r.post("/hide")
    def hide(req: HideReq):
        return hub.hide(req)

    @r.get("/original/{pid}")
    def original(pid: int):
        return hub.original(pid)

    @r.get("/volumes")
    def list_volumes():
        return {"volumes": volumes()}

    @r.post("/pick")
    def pick(req: PickReq):
        # the picker only answers with a path: the open shoot stays open, unlike /api/folder/choose
        if req.purpose not in PICK_PROMPTS:
            raise HTTPException(400, "purpose is watch, drive or export")
        return {"purpose": req.purpose, "path": pick_folder(PICK_PROMPTS[req.purpose])}

    @r.post("/export-store")
    def export_store_route(req: ExportStoreReq):
        return hub.export_store(req)

    app.include_router(r)
    return hub
