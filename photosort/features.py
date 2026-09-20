from __future__ import annotations
from pathlib import Path
import cv2, imagehash, numpy as np
from PIL import Image
from .config import SHARP_TILE_GRID

def to_gray(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("L"), dtype=np.uint8)

def phash(im: Image.Image) -> str:
    return str(imagehash.phash(im))

def _lapvar(g: np.ndarray) -> float:
    if g.size < 16:
        return 0.0
    return float(cv2.Laplacian(g, cv2.CV_64F).var())

def sharpness_tiles(gray: np.ndarray, grid: int = SHARP_TILE_GRID) -> tuple[float, float]:
    h, w = gray.shape
    th, tw = max(h // grid, 8), max(w // grid, 8)
    vals = [_lapvar(gray[y:y + th, x:x + tw]) for y in range(0, h - th + 1, th) for x in range(0, w - tw + 1, tw)]
    if not vals:
        return 0.0, 0.0
    return float(np.percentile(vals, 90)), float(max(vals))

def eye_sharpness(gray: np.ndarray, landmarks: np.ndarray) -> float:
    re, le = landmarks[0], landmarks[1]
    cx, cy = (re + le) / 2
    d = max(float(np.linalg.norm(le - re)), 8.0)
    hw, hh = 0.9 * d, 0.45 * d
    h, w = gray.shape
    x0, x1 = int(max(cx - hw, 0)), int(min(cx + hw, w))
    y0, y1 = int(max(cy - hh, 0)), int(min(cy + hh, h))
    return _lapvar(gray[y0:y1, x0:x1])

def exif_info(path: Path) -> dict:
    """taken_at, camera, width, height from EXIF (PIL first, pyexiv2 as the fallback for what PIL cannot
    read), plus aerial: a DJI Make (Exif.Image.Make) or a DJI_ filename, deterministic, no model."""
    out = {"taken_at": None, "camera": None, "width": None, "height": None,
           "aerial": Path(path).name.upper().startswith("DJI_")}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            ex = im.getexif()
            # DateTimeOriginal (shutter time) beats DateTime (last edit) when both exist
            dt = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
            if dt:
                d, t = str(dt).split(" ", 1)
                out["taken_at"] = d.replace(":", "-") + "T" + t
            make, model = ex.get(0x010F), ex.get(0x0110)
            if model:
                out["camera"] = (f"{make} {model}" if make and make not in model else model).strip()
            if make and str(make).strip().upper().startswith("DJI"):
                out["aerial"] = True
    except Exception:
        try:
            import pyexiv2
            m = pyexiv2.Image(str(path)); e = m.read_exif(); m.close()
            if out["taken_at"] is None:
                dt = e.get("Exif.Photo.DateTimeOriginal") or e.get("Exif.Image.DateTime")
                if dt:
                    d, t = dt.split(" ", 1); out["taken_at"] = d.replace(":", "-") + "T" + t
            if out["camera"] is None:
                camera = e.get("Exif.Image.Model")
                if camera:
                    out["camera"] = camera
            if str(e.get("Exif.Image.Make") or "").strip().upper().startswith("DJI"):
                out["aerial"] = True
            if out["width"] is None:
                width = int(e.get("Exif.Photo.PixelXDimension", 0)) or None
                if width:
                    out["width"] = width
            if out["height"] is None:
                height = int(e.get("Exif.Photo.PixelYDimension", 0)) or None
                if height:
                    out["height"] = height
        except Exception:
            pass
    return out
