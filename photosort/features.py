from __future__ import annotations
import math
from pathlib import Path
import cv2, imagehash, numpy as np
import pillow_heif
from PIL import Image
from .config import SHARP_TILE_GRID

# Almost every phone photo is HEIC and almost all the GPS is in those, so exif_info has to be able to open
# one on its own: decode.py registers the opener too, but a caller that only wants metadata never imports it.
pillow_heif.register_heif_opener()

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

# GPS. The same three numbers reach us two ways, as PIL IFDRationals and as pyexiv2's "23/1 1/1 2345/100"
# strings, so both branches and video.py's ISO 6709 parser share one converter and one validity gate.

def _rational(x) -> float | None:
    """One EXIF rational as a float: an IFDRational, a plain number, or an "a/b" string. None when it is
    not a number at all; a zero denominator on an IFDRational floats to nan, which the callers reject."""
    try:
        if isinstance(x, str):
            num, _, den = x.partition("/")
            return float(num) / float(den) if den else float(num)
        return float(x)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

def _dms_to_deg(parts, ref) -> float | None:
    """degrees, minutes, seconds (some cameras write only degrees and decimal minutes) plus an N/S/E/W ref,
    as signed decimal degrees. None when a part will not parse or the ref is not one of the four letters."""
    vals = [_rational(p) for p in list(parts)[:3]]
    if not vals or any(v is None or not math.isfinite(v) for v in vals):
        return None
    letter = str(ref or "").strip().upper()[:1]
    if letter not in ("N", "S", "E", "W"):
        return None
    deg = vals[0] + (vals[1] if len(vals) > 1 else 0.0) / 60.0 + (vals[2] if len(vals) > 2 else 0.0) / 3600.0
    return -deg if letter in ("S", "W") else deg

def latlon_ok(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    """(lat, lon) when both are real numbers in range, else (None, None). Exactly 0,0 is thrown away: it is
    the point a phone writes when it had no fix, and it is in the middle of the Atlantic. nan has to be
    checked by hand because every comparison against it is False, so it would pass the range test."""
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None, None
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0.0 and lon == 0.0):
        return None, None
    return float(lat), float(lon)

def exif_info(path: Path) -> dict:
    """taken_at, camera, width, height, lat, lon from EXIF (PIL first, pyexiv2 as the fallback for what PIL
    cannot read), plus aerial: a DJI Make (Exif.Image.Make) or a DJI_ filename, deterministic, no model."""
    out = {"taken_at": None, "camera": None, "width": None, "height": None, "lat": None, "lon": None,
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
            # GPS IFD (0x8825): tag 1 is the latitude ref (N/S) and 2 its degrees, minutes, seconds,
            # 3 and 4 the same for longitude. Its own try so a damaged GPS block does not cost the date.
            try:
                gps = ex.get_ifd(0x8825)
                if gps:
                    out["lat"], out["lon"] = latlon_ok(_dms_to_deg(gps.get(2) or (), gps.get(1)),
                                                       _dms_to_deg(gps.get(4) or (), gps.get(3)))
            except Exception:
                pass
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
            if out["lat"] is None:
                # pyexiv2 hands the three rationals back as one space separated string
                out["lat"], out["lon"] = latlon_ok(
                    _dms_to_deg(str(e.get("Exif.GPSInfo.GPSLatitude") or "").split(), e.get("Exif.GPSInfo.GPSLatitudeRef")),
                    _dms_to_deg(str(e.get("Exif.GPSInfo.GPSLongitude") or "").split(), e.get("Exif.GPSInfo.GPSLongitudeRef")))
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
