from __future__ import annotations
import hashlib, os
from dataclasses import dataclass
from pathlib import Path
from .config import IMAGE_EXTS, RAW_EXTS, VIDEO_EXTS, SKIP_DIRS, SONY_CARD_DIRS, SONY_CARD_ROOT

@dataclass
class ImageFile:
    path: Path
    rel: str
    size: int
    mtime: float
    is_raw: bool
    sibling: str | None = None
    is_video: bool = False

def find_images(root: Path) -> list[ImageFile]:
    root = Path(root)
    found: dict[str, ImageFile] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Hidden dirs (.Trashes, .photosort) and SKIP_DIRS are pruned anywhere; the Sony card bookkeeping
        # only directly under M4ROOT. Pruned in place so os.walk never descends. A DJI clip's .SRT sidecar
        # needs no rule: its extension is not on the list.
        on_card = Path(dirpath).name.upper() == SONY_CARD_ROOT
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and not (on_card and d in SONY_CARD_DIRS)]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue   # vanished or unreadable mid-walk; skip it
            rel = str(p.relative_to(root))
            found[rel] = ImageFile(p, rel, st.st_size, st.st_mtime, ext in RAW_EXTS, is_video=ext in VIDEO_EXTS)
    # Videos never pair with anything: a clip.MP4 next to a clip.ARW is two files, not a JPEG and its RAW.
    out: list[ImageFile] = [f for f in found.values() if f.is_video]
    # pair RAW+JPEG by stem within the same directory: keep the JPEG
    by_stem: dict[tuple[str, str], list[ImageFile]] = {}
    for f in found.values():
        if not f.is_video:
            by_stem.setdefault((str(f.path.parent), f.path.stem.lower()), []).append(f)
    for group in by_stem.values():
        raws = [g for g in group if g.is_raw]
        std = [g for g in group if not g.is_raw]
        if raws and std:
            keep = sorted(std, key=lambda g: g.rel)[0]
            keep.sibling = raws[0].rel
            out.append(keep)
        else:
            out.extend(group)
    # second pass: a RAW whose JPEG lives in a sibling folder (Day1/RAW + Day1/JPG layouts).
    # Pair by stem across the whole tree only when the stem is unique on both sides.
    loose_raw = [f for f in out if f.is_raw]
    loose_std = [f for f in out if not f.is_raw and not f.is_video and f.sibling is None]
    if loose_raw and loose_std:
        std_by_stem: dict[str, list[ImageFile]] = {}
        for f in loose_std:
            std_by_stem.setdefault(f.path.stem.lower(), []).append(f)
        raw_by_stem: dict[str, list[ImageFile]] = {}
        for f in loose_raw:
            raw_by_stem.setdefault(f.path.stem.lower(), []).append(f)
        drop: set[str] = set()
        for stem, raws in raw_by_stem.items():
            stds = std_by_stem.get(stem)
            if stds and len(stds) == 1 and len(raws) == 1:
                stds[0].sibling = raws[0].rel
                drop.add(raws[0].rel)
        out = [f for f in out if f.rel not in drop]
    return sorted(out, key=lambda f: f.rel)

def quick_hash(path: Path, chunk: int = 65536) -> str:
    h = hashlib.sha1()
    size = os.path.getsize(path)
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(chunk))
        if size > chunk:
            fh.seek(max(size - chunk, chunk))
            h.update(fh.read(chunk))
    return h.hexdigest()
