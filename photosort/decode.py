from __future__ import annotations
import io
from pathlib import Path
from PIL import Image, ImageOps
import pillow_heif
from .config import PREVIEW_EDGE, RAW_EXTS

pillow_heif.register_heif_opener()

class DecodeError(Exception):
    pass

# What load_upload will take from a browser: the still formats a camera, a phone or a screenshot
# produces. PIL's own name for the file it just parsed, so a .png renamed .jpg is judged on its bytes.
ACCEPTED_FORMATS = {"JPEG", "MPO", "PNG", "HEIF", "WEBP", "TIFF", "BMP", "GIF"}

_FLIP_TO_TRANSPOSE = {3: Image.ROTATE_180, 5: Image.ROTATE_90, 6: Image.ROTATE_270}

def _fit(im: Image.Image, max_edge: int) -> Image.Image:
    im = im.convert("RGB")
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return im

def _load_raw(path: Path, max_edge: int) -> Image.Image:
    import rawpy
    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        try:
            thumb = raw.extract_thumb()
        except Exception:
            thumb = None
        im = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            im = Image.open(io.BytesIO(thumb.data))
            im.draft("RGB", (max_edge, max_edge))
            im = im.convert("RGB")
            if max(im.size) < 800:
                im = None
        if im is None:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
            im = Image.fromarray(rgb)
            flip = 0  # postprocess already applies orientation
    if flip in _FLIP_TO_TRANSPOSE:
        im = im.transpose(_FLIP_TO_TRANSPOSE[flip])
    return _fit(im, max_edge)

def load_upload(data: bytes, max_edge: int = PREVIEW_EDGE) -> Image.Image:
    """A reference image that arrived over the wire, decoded from memory.

    It never becomes a file. The shoot disk is read-only (tests/test_readonly.py), the index folder is
    ours to keep clean, and a still someone dropped on the window to ask a question is not part of the
    shoot, so there is nowhere it belongs on disk. The bytes live in the request and are freed with it.

    Only formats PIL identifies are accepted, and the check is the decoder's own answer, not the browser's
    Content-Type header, which the caller controls. RAW is missing on purpose: rawpy reads a path, so a
    dropped .ARW is refused here rather than half handled. A screenshot or a JPEG off a phone, which is
    what anyone actually drags in, both land in ACCEPTED_FORMATS."""
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        if fmt not in ACCEPTED_FORMATS:
            raise DecodeError(f"not an image we read: {fmt or 'unknown format'}")
        if fmt == "JPEG":
            im.draft("RGB", (max_edge, max_edge))
        im = ImageOps.exif_transpose(im)        # as load_preview does, or a portrait phone shot embeds sideways
        return _fit(im, max_edge)
    except DecodeError:
        raise
    except Exception as e:      # PIL raises UnidentifiedImageError, DecompressionBombError and more
        raise DecodeError(f"upload: {e}") from e

def load_preview(path: Path, max_edge: int = PREVIEW_EDGE) -> Image.Image:
    path = Path(path)
    try:
        if path.suffix.lower() in RAW_EXTS:
            return _load_raw(path, max_edge)
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (max_edge, max_edge))
        im = ImageOps.exif_transpose(im)
        return _fit(im, max_edge)
    except DecodeError:
        raise
    except Exception as e:  # PIL raises many types; normalise
        raise DecodeError(f"{path}: {e}") from e
