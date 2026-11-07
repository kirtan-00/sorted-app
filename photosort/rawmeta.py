"""Metadata and previews from the two RAW containers that PIL and exifread cannot read.

Canon CR3 (EOS R, R5, R6, R7, R8, R50, R100, M50 and later) is an ISO base media file, like a .mp4. Its EXIF lives in
four boxes inside moov/uuid(85c0b687-820f-11e0-8111-f4ce462b6a48): CMT1 is IFD0 (make, model, orientation), CMT2 the
EXIF IFD (date, ISO, shutter, aperture, lens), CMT4 the GPS IFD. Each is a complete little TIFF. The 1620 px JPEG
preview is a PRVW box inside the top level uuid(eaf42b5e-1c98-4b88-b9fb-b7dc406e4d16).

Fujifilm RAF starts "FUJIFILMCCD-RAW " and holds a full JPEG, EXIF included, at the offset and length stored big
endian at bytes 84 and 88.

Both layouts are from the published descriptions (lclevy's CR3 notes; the RAF header as LibRaw and exiftool read it),
checked here only against files built to those descriptions. No real camera file has been through this code yet.
Every reader returns None rather than raising, and never reads more than it has to."""
from __future__ import annotations
import io
import struct
from pathlib import Path

CANON_UUID = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48")
PREVIEW_UUID = bytes.fromhex("eaf42b5e1c984b88b9fbb7dc406e4d16")
META_MAX = 4 << 20           # a CMT box is a few KB; anything this big is not one
JPEG_MAX = 64 << 20


def _boxes(f, start: int, end: int):
    """(type, payload start, payload end) for each box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head[:8])
        body = pos + 8
        if size == 1:
            if len(head) < 16:
                return
            size = struct.unpack(">Q", head[8:16])[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos or pos + size > end:
            return
        yield kind, body, pos + size
        pos += size


def _size(f) -> int:
    f.seek(0, 2)
    return f.tell()


def cr3_tiffs(path: Path) -> dict[str, bytes] | None:
    """{"CMT1": tiff bytes, ...} from a CR3, or None when it is not one."""
    try:
        with open(path, "rb") as f:
            end = _size(f)
            top = list(_boxes(f, 0, end))
            if not top or top[0][0] != b"ftyp":
                return None
            f.seek(top[0][1]); brand = f.read(4)
            if brand != b"crx ":
                return None
            out = {}
            for kind, a, b in top:
                if kind != b"moov":
                    continue
                for k2, a2, b2 in _boxes(f, a, b):
                    if k2 != b"uuid":
                        continue
                    f.seek(a2)
                    if f.read(16) != CANON_UUID:
                        continue
                    for k3, a3, b3 in _boxes(f, a2 + 16, b2):
                        name = k3.decode("latin-1")
                        if name in ("CMT1", "CMT2", "CMT3", "CMT4") and b3 - a3 <= META_MAX:
                            f.seek(a3); out[name] = f.read(b3 - a3)
            return out or None
    except (OSError, struct.error, ValueError):
        return None


def _jpeg_in(f, a: int, b: int) -> bytes | None:
    f.seek(a)
    head = f.read(min(64, b - a))
    i = head.find(b"\xff\xd8\xff")
    if i < 0 or b - a - i > JPEG_MAX:
        return None
    f.seek(a + i)
    return f.read(b - a - i)


def cr3_preview(path: Path) -> bytes | None:
    """The PRVW JPEG (1620 px on the long side on the bodies documented), or None."""
    try:
        with open(path, "rb") as f:
            end = _size(f)
            for kind, a, b in _boxes(f, 0, end):
                if kind != b"uuid":
                    continue
                f.seek(a)
                if f.read(16) != PREVIEW_UUID:
                    continue
                for k2, a2, b2 in _boxes(f, a + 16 + 8, b):        # 8 bytes of the uuid's own header before PRVW
                    if k2 == b"PRVW":
                        return _jpeg_in(f, a2, b2)
    except (OSError, struct.error, ValueError):
        return None
    return None


def raf_jpeg(path: Path) -> bytes | None:
    """The JPEG a RAF carries, EXIF and all, or None when it is not a RAF."""
    try:
        with open(path, "rb") as f:
            head = f.read(92)
            if not head.startswith(b"FUJIFILMCCD-RAW ") or len(head) < 92:
                return None
            off, n = struct.unpack(">II", head[84:92])
            if not (92 <= off and 0 < n <= JPEG_MAX and off + n <= _size(f)):
                return None
            f.seek(off)
            data = f.read(n)
            return data if data.startswith(b"\xff\xd8") else None
    except (OSError, struct.error):
        return None


def embedded_preview(path: Path):
    """A PIL image of the camera's own JPEG inside a CR3 or RAF, upright, or None."""
    from PIL import Image, ImageOps
    ext = Path(path).suffix.lower()
    data = cr3_preview(path) if ext == ".cr3" else raf_jpeg(path) if ext == ".raf" else None
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data)); im.load()
        if ext == ".cr3":
            t = cr3_tiffs(path) or {}
            o = _ifd(t.get("CMT1")).get(0x0112)
            if o in (3, 6, 8):
                im = im.transpose({3: Image.ROTATE_180, 6: Image.ROTATE_270, 8: Image.ROTATE_90}[o])
            return im
        return ImageOps.exif_transpose(im)
    except Exception:
        return None


def _ifd(tiff: bytes | None) -> dict:
    """The first IFD of a TIFF blob as {tag: value}; {} when there is none."""
    if not tiff:
        return {}
    try:
        from PIL import Image
        ex = Image.Exif()
        ex.load(tiff)
        return dict(ex)
    except Exception:
        return {}


def cr3_ifds(path: Path) -> tuple[dict, dict, dict] | None:
    """(IFD0, EXIF IFD, GPS IFD) of a CR3, each as {tag: value}, or None when it is not one."""
    t = cr3_tiffs(path)
    if not t:
        return None
    return _ifd(t.get("CMT1")), _ifd(t.get("CMT2")), _ifd(t.get("CMT4"))
