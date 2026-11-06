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

# GPS. The same three numbers reach us two ways, as PIL IFDRationals and as exifread's Ratio (a Fraction),
# so both branches and video.py's ISO 6709 parser share one converter and one validity gate.

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

def _clean(v) -> str | None:
    """A Make or Model as text. A DJI DNG pads both with NULs to the field's fixed length ("DJI\\x00\\x00..."),
    which PIL hands back as is and exifread does not, so both readers go through this to agree."""
    return str(v).replace("\x00", "").strip() or None if v is not None else None

def _camera(make: str | None, model: str | None) -> str | None:
    # one formula for both readers, so a RAW and the JPG beside it name the same camera (search.angles
    # groups by this string)
    if not model:
        return None
    return f"{make} {model}" if make and make not in model else model

def _subifd_size(path: Path, offsets) -> tuple[int, int] | None:
    """(width, height) of the full-resolution image among a TIFF's SubIFDs, read straight from the header.
    A DNG (and a NEF, and most TIFF raws) puts a small preview in IFD0 and the sensor-sized frame in a
    SubIFD; neither PIL (get_ifd(0x14A) comes back empty) nor exifread with details=False walks those.
    The frame is the SubIFD whose NewSubfileType is 0, else the largest. ImageWidth/ImageLength, not
    DefaultCropSize, which trims a few pixels (FC6310: 5472x3648 against 5464x3640): it is the size Finder
    and the sibling JPG report. DefaultCropSize only when a SubIFD has no ImageWidth. Never raises."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            e = {b"II": "<", b"MM": ">"}[head[:2]]
            f.seek(0, 2); end = f.tell()
            found = []
            for off in offsets:
                off = int(off)
                if not 8 <= off < end - 2:
                    continue
                f.seek(off)
                n, = struct.unpack(e + "H", f.read(2))
                raw = f.read(12 * n)
                tags = {}
                for i in range(len(raw) // 12):
                    tag, typ, cnt, val = struct.unpack(e + "HHI4s", raw[12 * i:12 * i + 12])
                    if typ == 3:                                          # SHORT, inline
                        tags[tag] = struct.unpack(e + "HH", val)[:min(cnt, 2)]
                    elif typ == 4 and cnt == 1:                          # LONG, inline
                        tags[tag] = struct.unpack(e + "I", val)
                    elif typ == 5 and tag == 0xC620 and cnt == 2:        # DefaultCropSize as RATIONALs
                        here = f.tell(); f.seek(struct.unpack(e + "I", val)[0])
                        a, b, c, d = struct.unpack(e + "4I", f.read(16)); f.seek(here)
                        tags[tag] = (a // b if b else 0, c // d if d else 0)
                w, h = (tags.get(0x100) or (0,))[0], (tags.get(0x101) or (0,))[0]
                if not (w and h) and len(tags.get(0xC620) or ()) == 2:
                    w, h = tags[0xC620]
                if w and h:
                    found.append(((tags.get(0xFE) or (0,))[0] == 0, w * h, (int(w), int(h))))
        return max(found)[2] if found else None
    except Exception:
        return None

# ===== camera settings: what the camera recorded about the exposure, from the standard EXIF sub-IFD only =====
# Both readers fill the same keys. MakerNote (drive mode, Sony's bracket step, focus distance) is NOT read: exifread
# with details=True costs 220 to 415 ms on a 61 MP Sony JPG (measured on nine ILCE-7RM5 frames), against about
# 83 ms for everything else a photo costs at index time, and Sony keeps drive mode in enciphered tags anyway.
# ExposureMode is the standard tag and its value 2 is the camera's own "auto bracket" flag; that is the one drive
# hint there is. lens is '' when the file was read and names no lens, so "never read" (NULL) stays tellable apart.
# flash is the raw EXIF Flash value, bit 0 meaning it fired; -1 when the file was read and has no such tag, NULL when
# never read. A radio trigger can leave bit 0 clear on a frame the strobe lit, so clear means "none recorded", no more.
SETTING_KEYS = ("exposure_s", "fnumber", "iso", "ev_bias", "focal_mm", "lens", "exp_mode", "subsec", "flash")

def _pos(x) -> float | None:
    v = _rational(x)
    return v if v is not None and math.isfinite(v) and v > 0 else None

def _subsec(x) -> float | None:
    """SubSecTimeOriginal ("400", "21") as a fraction of a second: the digits are decimal places."""
    d = "".join(ch for ch in str(x or "") if ch.isdigit())
    return float("0." + d) if d else None

def _settings_from_pil(exif_ifd, out: dict) -> None:
    try:
        g = exif_ifd.get
        out["exposure_s"] = _pos(g(0x829A)); out["fnumber"] = _pos(g(0x829D))
        iso = g(0x8827)
        iso = iso[0] if isinstance(iso, (tuple, list)) and iso else iso
        out["iso"] = int(iso) if isinstance(iso, (int, float)) and iso > 0 else None
        b = _rational(g(0x9204)) if g(0x9204) is not None else None
        out["ev_bias"] = round(b, 2) if b is not None and math.isfinite(b) else None
        out["focal_mm"] = _pos(g(0xA405)) or _pos(g(0x920A))      # the 35 mm equivalent when the camera wrote one
        out["lens"] = _clean(g(0xA434)) or ""
        m = g(0xA402)
        out["exp_mode"] = int(m) if isinstance(m, int) else None
        out["subsec"] = _subsec(g(0x9291))
        fl = g(0x9209)
        out["flash"] = int(fl) if isinstance(fl, int) else None
    except Exception:
        pass

def _settings_from_exifread(tags, out: dict) -> None:
    def first(key):
        v = tags.get(key)
        return v.values[0] if v is not None and getattr(v, "values", None) else None
    try:
        if out["exposure_s"] is None: out["exposure_s"] = _pos(first("EXIF ExposureTime"))
        if out["fnumber"] is None: out["fnumber"] = _pos(first("EXIF FNumber"))
        if out["iso"] is None:
            iso = first("EXIF ISOSpeedRatings")
            out["iso"] = int(iso) if isinstance(iso, int) and iso > 0 else None
        if out["ev_bias"] is None:
            b = _rational(first("EXIF ExposureBiasValue"))
            out["ev_bias"] = round(b, 2) if b is not None and math.isfinite(b) else None
        if out["focal_mm"] is None:
            out["focal_mm"] = _pos(first("EXIF FocalLengthIn35mmFilm")) or _pos(first("EXIF FocalLength"))
        if not out["lens"]:
            out["lens"] = _clean(tags.get("EXIF LensModel")) or ""
        if out["exp_mode"] is None:
            m = first("EXIF ExposureMode")
            out["exp_mode"] = int(m) if isinstance(m, int) else None
        if out["subsec"] is None:
            out["subsec"] = _subsec(tags.get("EXIF SubSecTimeOriginal"))
        if out["flash"] is None:
            fl = first("EXIF Flash")
            out["flash"] = int(fl) if isinstance(fl, int) else None
    except Exception:
        pass
# ===== end camera settings =====

def exif_info(path: Path) -> dict:
    """taken_at, camera, width, height, lat, lon from EXIF (PIL first, exifread as the fallback for what PIL
    cannot read), plus aerial: a DJI Make (Exif.Image.Make) or a DJI_ filename, deterministic, no model."""
    out = {"taken_at": None, "camera": None, "width": None, "height": None, "lat": None, "lon": None,
           "aerial": Path(path).name.upper().startswith("DJI_"), **{k: None for k in SETTING_KEYS}}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            ex = im.getexif()
            # NewSubfileType bit 0 says IFD0 is a reduced-resolution copy (a DNG's preview), so im.size is
            # the preview's; the real frame's size is in a SubIFD (0x14A)
            if (ex.get(0x00FE) or 0) & 1 and ex.get(0x014A):
                subs = ex.get(0x014A)
                real = _subifd_size(path, subs if isinstance(subs, (tuple, list)) else (subs,))
                if real:
                    out["width"], out["height"] = real
            # DateTimeOriginal (shutter time) beats DateTime (last edit) when both exist
            dt = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
            if dt:
                d, t = str(dt).split(" ", 1)
                out["taken_at"] = d.replace(":", "-") + "T" + t
            make, model = _clean(ex.get(0x010F)), _clean(ex.get(0x0110))
            out["camera"] = _camera(make, model)
            _settings_from_pil(ex.get_ifd(0x8769), out)
            if make and make.upper().startswith("DJI"):
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
        _exif_fallback(path, out)
    if out["flash"] is None:
        out["flash"] = -1
    return out

def _exif_fallback(path: Path, out: dict) -> None:
    """Fill what PIL could not, with exifread (BSD), which walks the TIFF IFDs itself and so reads a file
    PIL will not open: every Sony ARW, and a TIFF raw PIL chokes on. Only fields still None are filled, so
    a size PIL read before it gave up stands. details=False skips the maker notes, which keeps a 40 MB ARW
    to a few ms because only the header is read. Never raises."""
    try:
        import exifread
        import logging
        logging.getLogger("exifread").setLevel(logging.ERROR)   # "Possibly corrupted field" on every DNG
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)

        def text(key):
            return _clean(tags.get(key))

        if out["taken_at"] is None:
            dt = text("EXIF DateTimeOriginal") or text("Image DateTime")
            if dt:
                d, t = dt.split(" ", 1)
                out["taken_at"] = d.replace(":", "-") + "T" + t
        make, model = text("Image Make"), text("Image Model")
        if out["camera"] is None:
            out["camera"] = _camera(make, model)
        _settings_from_exifread(tags, out)
        if make and make.upper().startswith("DJI"):
            out["aerial"] = True
        if out["lat"] is None:
            def dms(key):
                v = tags.get(key)
                return v.values if v is not None else ()           # a list of Ratio, a Fraction subclass
            out["lat"], out["lon"] = latlon_ok(_dms_to_deg(dms("GPS GPSLatitude"), text("GPS GPSLatitudeRef")),
                                               _dms_to_deg(dms("GPS GPSLongitude"), text("GPS GPSLongitudeRef")))
        for key, tag in (("width", "EXIF ExifImageWidth"), ("height", "EXIF ExifImageLength")):
            v = tags.get(tag)                                         # PixelXDimension / PixelYDimension
            if out[key] is None and v is not None and v.values:
                out[key] = int(v.values[0]) or None
        subs = tags.get("Image SubIFDs")
        if out["width"] is None and subs is not None:                 # a DNG: no PixelXDimension, see above
            real = _subifd_size(path, subs.values)
            if real:
                out["width"], out["height"] = real
    except Exception:
        pass
