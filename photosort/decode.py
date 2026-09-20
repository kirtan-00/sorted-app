from __future__ import annotations
import io
from pathlib import Path
from PIL import Image, ImageOps
import pillow_heif
from .config import PREVIEW_EDGE, RAW_EXTS

pillow_heif.register_heif_opener()

class DecodeError(Exception):
    pass

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
