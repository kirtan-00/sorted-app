# photosort v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, offline Mac tool that indexes a folder of photos in place, then lets a designer search by content ("balcony", "dining table") with a sharpness filter, and lets a photographer split a shoot into per-person / group / solo folders.

**Architecture:** Python package `photosort`. Stage 1 (multiprocess, CPU): decode each photo ONCE to a ≤1024 px preview, write two thumbnails, compute EXIF/pHash/sharpness/faces, write to SQLite under `~/Library/Application Support/photosort/<shoot-slug>/` (the source drive is never written to). Stage 2 (main process, MPS): MobileCLIP-S1 embeds the 1024 px thumbs in batches. Search is numpy cosine over an in-memory matrix. A FastAPI server serves a single-page handmade UI; a shell-script `.app` launches it.

**Tech Stack:** Python 3.11, uv, PyTorch 2.14 (MPS), open_clip 3.3 (`MobileCLIP-S1`/`datacompdr`), OpenCV 5 (`FaceDetectorYN` YuNet + `FaceRecognizerSF` SFace), Pillow + pillow-heif, rawpy, imagehash, scikit-learn (DBSCAN), SQLite, FastAPI + uvicorn, vanilla HTML/JS.

**Spec:** `docs/superpowers/specs/2026-09-17-photosort-design.md`

## Global Constraints

- Runs on Apple M1, 8 GB RAM, macOS 26. Peak RSS of the whole pipeline ≤ 2.5 GB. RAW workers ≤ 2, JPEG workers = 4.
- Zero LLM calls anywhere in v1.
- SOURCE DRIVE IS READ-ONLY. Never write, move, rename, or delete anything under the shoot root. The index + thumbnails live under `config.app_home()` (env `PHOTOSORT_HOME`, default `~/Library/Application Support/photosort/<shoot-slug>/`). Exports COPY files into `config.export_root() / <shoot name> / <export name>/` (env `PHOTOSORT_EXPORT_DIR`, default `~/Desktop/photosort-out/`). Default export mode is `copy`; `symlink` and `csv` remain available.
- Decode each photo once at ≤1024 px long edge; every feature derives from that buffer.
- Sharpness is measured on the subject (eye crops when faces exist, else 90th-percentile tile), never whole-frame.
- Never delete or modify an original. Tests must never touch the real home or Desktop: `tests/conftest.py` has an autouse fixture that points `PHOTOSORT_HOME` and `PHOTOSORT_EXPORT_DIR` at tmp dirs.
- Models: `models/face_detection_yunet_2023mar.onnx`, `models/face_recognition_sface_2021dec.onnx` (already downloaded), MobileCLIP-S1 via open_clip cache. `models/` is gitignored; `scripts/fetch_models.sh` re-downloads.
- Repo is public: no personal photos in `tests/fixtures/`; face tests that need a real face are gated on env `PHOTOSORT_FACE_FIXTURE=<path>`.
- No em dashes in any text or UI copy. UI is handmade editorial, no orange accent.
- venv: `~/Desktop/photosort/.venv` (already created, all deps installed except scikit-learn). Run everything with `source .venv/bin/activate`.
- Commit after every task with a conventional message.

## File structure

```
photosort/
  pyproject.toml            package metadata, console script `photosort`
  photosort/__init__.py
  photosort/config.py       constants: extensions, thumb sizes, thresholds, model paths
  photosort/walk.py         find_images(root) -> list[ImageFile]; RAW+JPEG pairing; quick_hash
  photosort/decode.py       load_preview(path) -> PIL RGB <=1024 px, orientation applied
  photosort/features.py     phash, exif_info, sharpness_tiles, eye_sharpness
  photosort/faces.py        FaceEngine: detect + embed on a preview
  photosort/db.py           schema, connect, upserts, embedding matrix loader
  photosort/embed.py        Embedder: MobileCLIP image/text -> normalized float16
  photosort/index.py        index_folder(): stage 1 pool + stage 2 embed, incremental
  photosort/search.py       Filters, search()
  photosort/people.py       cluster_faces(), name_person(), assign_from_reference()
  photosort/export.py       export_ids()
  photosort/cli.py          argparse: index | find | people | serve | bench
  photosort/server.py       FastAPI app + static UI
  photosort/ui/index.html, app.js, style.css
  scripts/fetch_models.sh
  PhotoSort.app/            shell launcher
  tests/                    pytest, synthetic PIL fixtures
```

---

### Task 1: Project skeleton

**Files:**
- Create: `pyproject.toml`, `photosort/__init__.py`, `photosort/config.py`, `.gitignore`, `scripts/fetch_models.sh`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.IMAGE_EXTS: set[str]`, `config.RAW_EXTS: set[str]`, `config.PREVIEW_EDGE = 1024`, `config.GRID_EDGE = 320`, `config.INDEX_DIRNAME = ".photosort"`, `config.MODELS_DIR: Path`, `config.YUNET_PATH`, `config.SFACE_PATH`, `config.CLIP_MODEL = "MobileCLIP-S1"`, `config.CLIP_PRETRAINED = "datacompdr"`, `config.FACE_SCORE_MIN = 0.7`, `config.FACE_CLUSTER_EPS = 0.5`, `config.SOFT_PERCENTILE = 15`; `conftest.make_image(tmp_path, name, size=(1600,1200), kind="sharp"|"blurry") -> Path`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "photosort"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "torch", "torchvision", "open_clip_torch", "timm", "opencv-python",
  "pillow", "pillow-heif", "imagehash", "rawpy", "numpy", "scikit-learn",
  "fastapi", "uvicorn[standard]", "pyexiv2",
]
[project.optional-dependencies]
dev = ["pytest"]
[project.scripts]
photosort = "photosort.cli:main"
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[tool.setuptools.packages.find]
include = ["photosort*"]
[tool.setuptools.package-data]
photosort = ["ui/*"]
```

- [ ] **Step 2: Write config.py**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"

CLIP_MODEL = "MobileCLIP-S1"
CLIP_PRETRAINED = "datacompdr"
EMBED_DIM = 512

INDEX_DIRNAME = ".photosort"
DB_NAME = "index.db"
PREVIEW_EDGE = 1024
GRID_EDGE = 320
THUMB_QUALITY = 85

STD_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf", ".orf", ".rw2"}
IMAGE_EXTS = STD_EXTS | RAW_EXTS

JPEG_WORKERS = 4
RAW_WORKERS = 2
EMBED_BATCH = 32

FACE_SCORE_MIN = 0.7
FACE_CLUSTER_EPS = 0.5      # cosine distance; tune on real data
FACE_MIN_SAMPLES = 2
GROUP_MIN_FACES = 3

SOFT_PERCENTILE = 15        # bottom 15% of sharpness in a shoot = "soft"
SHARP_TILE_GRID = 8
```

- [ ] **Step 3: Write .gitignore and fetch script**

```
.venv/
models/*.onnx
__pycache__/
*.pyc
.pytest_cache/
tests/fixtures/private/
.photosort/
photosort-out/
.DS_Store
```

`scripts/fetch_models.sh`:
```bash
#!/bin/bash
set -e
cd "$(dirname "$0")/../models"
base="https://github.com/opencv/opencv_zoo/raw/main/models"
curl -sL -o face_detection_yunet_2023mar.onnx "$base/face_detection_yunet/face_detection_yunet_2023mar.onnx"
curl -sL -o face_recognition_sface_2021dec.onnx "$base/face_recognition_sface/face_recognition_sface_2021dec.onnx"
ls -la *.onnx
```

- [ ] **Step 4: Write tests/conftest.py with the synthetic image factory**

```python
import numpy as np
import pytest
from PIL import Image, ImageFilter

def make_image(tmp_path, name="a.jpg", size=(1600, 1200), kind="sharp", seed=0):
    """Random high-contrast texture (sharp) or the same blurred (blurry)."""
    rng = np.random.default_rng(seed)
    arr = (rng.random((size[1] // 8, size[0] // 8, 3)) * 255).astype("uint8")
    im = Image.fromarray(arr).resize(size, Image.NEAREST)
    if kind == "blurry":
        im = im.filter(ImageFilter.GaussianBlur(12))
    p = tmp_path / name
    im.save(p, quality=90) if p.suffix.lower() in (".jpg", ".jpeg") else im.save(p)
    return p

@pytest.fixture
def make_img(tmp_path):
    return lambda **kw: make_image(tmp_path, **kw)
```

`tests/test_config.py`:
```python
from photosort import config
def test_exts():
    assert ".jpg" in config.IMAGE_EXTS and ".arw" in config.RAW_EXTS
    assert config.PREVIEW_EDGE == 1024
```

- [ ] **Step 5: Install editable, run tests, commit**

Run: `source .venv/bin/activate && uv pip install -q scikit-learn -e ".[dev]" && pytest -q`
Expected: 1 passed
```bash
git add -A && git commit -m "chore: photosort skeleton, config, synthetic test fixtures"
```

---

### Task 2: walk.py

**Files:**
- Create: `photosort/walk.py`, `tests/test_walk.py`

**Interfaces:**
- Produces: `@dataclass ImageFile(path: Path, rel: str, size: int, mtime: float, is_raw: bool, sibling: str | None)`; `find_images(root: Path) -> list[ImageFile]` (RAW+JPEG pairs: keeps the JPEG with `sibling=<raw rel path>`, drops the RAW); `quick_hash(path: Path) -> str` (sha1 of size + first 64 KB + last 64 KB, hex).

- [ ] **Step 1: Failing tests**

```python
from pathlib import Path
from photosort.walk import find_images, quick_hash

def test_finds_and_pairs(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "a.ARW").write_bytes(b"y" * 10)
    (tmp_path / "b.nef").write_bytes(b"z" * 10)
    (tmp_path / ".photosort").mkdir(); (tmp_path / ".photosort" / "t.jpg").write_bytes(b"q")
    (tmp_path / "notes.txt").write_text("no")
    files = find_images(tmp_path)
    rels = sorted(f.rel for f in files)
    assert rels == ["a.jpg", "b.nef"]
    a = next(f for f in files if f.rel == "a.jpg")
    assert a.sibling == "a.ARW" and a.is_raw is False
    assert next(f for f in files if f.rel == "b.nef").is_raw is True

def test_quick_hash_changes_with_content(tmp_path):
    p = tmp_path / "x.jpg"; p.write_bytes(b"a" * 200_000)
    h1 = quick_hash(p); p.write_bytes(b"a" * 199_999 + b"b"); h2 = quick_hash(p)
    assert h1 != h2 and len(h1) == 40
```

- [ ] **Step 2: Run, expect ImportError.** `pytest tests/test_walk.py -q`

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
import hashlib, os
from dataclasses import dataclass
from pathlib import Path
from .config import IMAGE_EXTS, RAW_EXTS, INDEX_DIRNAME

@dataclass
class ImageFile:
    path: Path
    rel: str
    size: int
    mtime: float
    is_raw: bool
    sibling: str | None = None

def find_images(root: Path) -> list[ImageFile]:
    root = Path(root)
    found: dict[str, ImageFile] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "photosort-out"]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            ext = p.suffix.lower()
            if ext not in IMAGE_EXTS:
                continue
            st = p.stat()
            rel = str(p.relative_to(root))
            found[rel] = ImageFile(p, rel, st.st_size, st.st_mtime, ext in RAW_EXTS)
    # pair RAW+JPEG by stem within the same directory: keep the JPEG
    by_stem: dict[tuple[str, str], list[ImageFile]] = {}
    for f in found.values():
        by_stem.setdefault((str(f.path.parent), f.path.stem.lower()), []).append(f)
    out: list[ImageFile] = []
    for group in by_stem.values():
        raws = [g for g in group if g.is_raw]
        std = [g for g in group if not g.is_raw]
        if raws and std:
            keep = sorted(std, key=lambda g: g.rel)[0]
            keep.sibling = raws[0].rel
            out.append(keep)
        else:
            out.extend(group)
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
```

- [ ] **Step 4: Run tests, expect 3 passed. Commit** `git commit -am "feat: walk folder, pair RAW+JPEG, quick hash"` (use `git add -A` first).

---

### Task 3: decode.py

**Files:**
- Create: `photosort/decode.py`, `tests/test_decode.py`

**Interfaces:**
- Produces: `load_preview(path: Path, max_edge: int = PREVIEW_EDGE) -> PIL.Image.Image` (mode RGB, long edge ≤ max_edge, EXIF orientation applied; JPEG uses `draft` DCT scaling; HEIC via pillow-heif; RAW via `rawpy.extract_thumb`, falls back to `postprocess(half_size=True)` if the embedded preview's long edge < 800 or no JPEG thumb); `DecodeError(Exception)`.

- [ ] **Step 1: Failing tests**

```python
from PIL import Image
from photosort.decode import load_preview, DecodeError
import pytest

def test_jpeg_downscaled(make_img):
    p = make_img(name="big.jpg", size=(4000, 3000))
    im = load_preview(p)
    assert im.mode == "RGB" and max(im.size) <= 1024 and min(im.size) >= 700

def test_small_not_upscaled(make_img):
    p = make_img(name="s.png", size=(300, 200))
    assert load_preview(p).size == (300, 200)

def test_exif_orientation_applied(tmp_path):
    im = Image.new("RGB", (400, 200), "red")
    exif = Image.Exif(); exif[0x0112] = 6  # rotate 90 CW
    p = tmp_path / "o.jpg"; im.save(p, exif=exif)
    assert load_preview(p).size == (200, 400)

def test_bad_file_raises(tmp_path):
    p = tmp_path / "bad.jpg"; p.write_bytes(b"not an image")
    with pytest.raises(DecodeError):
        load_preview(p)
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run tests, expect 4 passed. Commit** `feat: decode-once preview loader (JPEG draft, HEIC, RAW preview)`.

---

### Task 4: features.py

**Files:**
- Create: `photosort/features.py`, `tests/test_features.py`

**Interfaces:**
- Produces: `phash(im: PIL) -> str` (16 hex chars); `exif_info(path: Path) -> dict(taken_at: str|None ISO, camera: str|None, width: int|None, height: int|None)`; `sharpness_tiles(gray: np.ndarray, grid: int = 8) -> tuple[float, float]` returns (p90, max) of per-tile Laplacian variance; `eye_sharpness(gray: np.ndarray, landmarks: np.ndarray) -> float` where landmarks is (5,2) YuNet order [right_eye, left_eye, nose, mouth_r, mouth_l]; `to_gray(im: PIL) -> np.ndarray uint8`.

- [ ] **Step 1: Failing tests**

```python
import numpy as np
from PIL import Image
from photosort.decode import load_preview
from photosort.features import phash, exif_info, sharpness_tiles, eye_sharpness, to_gray

def test_sharp_beats_blurry(make_img):
    s = to_gray(load_preview(make_img(name="s.jpg", kind="sharp")))
    b = to_gray(load_preview(make_img(name="b.jpg", kind="blurry")))
    assert sharpness_tiles(s)[0] > 5 * sharpness_tiles(b)[0]

def test_phash_stable_under_resize(make_img):
    p = make_img(name="p.jpg")
    im = Image.open(p)
    import imagehash
    h1 = phash(im); h2 = phash(im.resize((800, 600)))
    assert len(h1) == 16 and (imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)) <= 4

def test_exif_missing_is_none(make_img):
    info = exif_info(make_img(name="e.jpg"))
    assert info["taken_at"] is None and info["width"] == 1600

def test_eye_sharpness_uses_eye_region():
    gray = np.zeros((400, 400), np.uint8)
    gray[180:220, 120:280] = (np.random.default_rng(0).random((40, 160)) * 255).astype(np.uint8)
    lm = np.array([[150, 200], [250, 200], [200, 260], [170, 320], [230, 320]], float)
    assert eye_sharpness(gray, lm) > 100
    assert eye_sharpness(np.zeros((400, 400), np.uint8), lm) == 0.0
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
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
    out = {"taken_at": None, "camera": None, "width": None, "height": None}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            ex = im.getexif()
            dt = ex.get(0x0132) or ex.get_ifd(0x8769).get(0x9003)
            if dt:
                d, t = str(dt).split(" ", 1)
                out["taken_at"] = d.replace(":", "-") + "T" + t
            make, model = ex.get(0x010F), ex.get(0x0110)
            if model:
                out["camera"] = (f"{make} {model}" if make and make not in model else model).strip()
    except Exception:
        try:
            import pyexiv2
            m = pyexiv2.Image(str(path)); e = m.read_exif(); m.close()
            dt = e.get("Exif.Photo.DateTimeOriginal") or e.get("Exif.Image.DateTime")
            if dt:
                d, t = dt.split(" ", 1); out["taken_at"] = d.replace(":", "-") + "T" + t
            out["camera"] = e.get("Exif.Image.Model")
            out["width"] = int(e.get("Exif.Photo.PixelXDimension", 0)) or None
            out["height"] = int(e.get("Exif.Photo.PixelYDimension", 0)) or None
        except Exception:
            pass
    return out
```

- [ ] **Step 4: Run tests, expect 4 passed. Commit** `feat: phash, exif, tile and eye sharpness`.

---

### Task 5: faces.py

**Files:**
- Create: `photosort/faces.py`, `tests/test_faces.py`

**Interfaces:**
- Produces: `@dataclass Face(x: int, y: int, w: int, h: int, score: float, landmarks: np.ndarray (5,2), embed: np.ndarray (128,) float32 L2-normalised, eye_sharp: float)`; `class FaceEngine: __init__(yunet=YUNET_PATH, sface=SFACE_PATH, score_min=FACE_SCORE_MIN)`, `detect(self, im: PIL) -> list[Face]`. Coordinates are in preview pixels.

- [ ] **Step 1: Failing tests**

```python
import os, numpy as np, pytest
from PIL import Image
from photosort.faces import FaceEngine, Face

def test_no_faces_on_noise(make_img):
    eng = FaceEngine()
    assert eng.detect(Image.open(make_img(name="n.jpg"))) == []

@pytest.mark.skipif(not os.environ.get("PHOTOSORT_FACE_FIXTURE"), reason="needs a real face photo")
def test_real_face():
    from photosort.decode import load_preview
    eng = FaceEngine()
    faces = eng.detect(load_preview(os.environ["PHOTOSORT_FACE_FIXTURE"]))
    assert faces and isinstance(faces[0], Face)
    assert faces[0].embed.shape == (128,) and abs(np.linalg.norm(faces[0].embed) - 1) < 1e-3
    assert faces[0].eye_sharp >= 0
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
from dataclasses import dataclass
import cv2, numpy as np
from PIL import Image
from .config import YUNET_PATH, SFACE_PATH, FACE_SCORE_MIN
from .features import to_gray, eye_sharpness

@dataclass
class Face:
    x: int; y: int; w: int; h: int
    score: float
    landmarks: np.ndarray
    embed: np.ndarray
    eye_sharp: float

class FaceEngine:
    def __init__(self, yunet=YUNET_PATH, sface=SFACE_PATH, score_min=FACE_SCORE_MIN):
        if not yunet.exists() or not sface.exists():
            raise FileNotFoundError("face models missing; run scripts/fetch_models.sh")
        self.det = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320), score_min, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(sface), "")

    def detect(self, im: Image.Image) -> list[Face]:
        bgr = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, dets = self.det.detect(bgr)
        if dets is None or len(dets) == 0:
            return []
        gray = to_gray(im)
        out: list[Face] = []
        for d in dets:
            x, y, bw, bh = [int(v) for v in d[:4]]
            lm = d[4:14].reshape(5, 2).astype(float)
            crop = self.rec.alignCrop(bgr, d)
            feat = self.rec.feature(crop).flatten().astype(np.float32)
            feat /= (np.linalg.norm(feat) + 1e-9)
            out.append(Face(x, y, bw, bh, float(d[14]), lm, feat, eye_sharpness(gray, lm)))
        return out
```

- [ ] **Step 4: Run tests (1 passed, 1 skipped). Commit** `feat: YuNet + SFace face engine`.

---

### Task 6: db.py

**Files:**
- Create: `photosort/db.py`, `tests/test_db.py`
- Modify: `photosort/config.py` (append helpers), `tests/conftest.py` (append autouse env fixture)

**Interfaces:**
- Produces: `config.app_home() -> Path`, `config.export_root() -> Path`, `config.shoot_slug(root) -> str`; `index_dir(root) -> Path` (= `app_home()/shoot_slug(root)`, creates it plus `thumbs/`, `grid/`; NEVER under root); `connect(root) -> sqlite3.Connection` (WAL, schema applied, `row_factory = sqlite3.Row`); `upsert_photo(conn, row: dict) -> int` (keys: rel, size, mtime, qhash, sibling, width, height, taken_at, camera, phash, sharp_tile, sharp_max, sharp_eye, sharp, n_faces, status); `replace_faces(conn, photo_id, faces: list[dict])` (keys: x,y,w,h,score,landmarks(json),eye_sharp,embed(bytes float32)); `set_embed(conn, photo_id, vec: np.ndarray)`; `photos_missing_embed(conn) -> list[(id, rel)]`; `load_embeds(conn) -> tuple[np.ndarray ids int64, np.ndarray (N,512) float32]`; `load_face_embeds(conn) -> (face_ids, photo_ids, (M,128) float32)`; `known_files(conn) -> dict[rel, (size, mtime)]`; `mark_missing(conn, present_rels: set[str])`.

Schema:
```sql
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime REAL, qhash TEXT,
  sibling TEXT, width INTEGER, height INTEGER, taken_at TEXT, camera TEXT, phash TEXT,
  sharp_tile REAL, sharp_max REAL, sharp_eye REAL, sharp REAL, n_faces INTEGER DEFAULT 0,
  embed BLOB, status TEXT DEFAULT 'ok', indexed_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  x INTEGER, y INTEGER, w INTEGER, h INTEGER, score REAL, landmarks TEXT, eye_sharp REAL,
  embed BLOB, person_id INTEGER);
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, cover_face_id INTEGER, n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
```

- [ ] **Step 1: Failing tests**

```python
import numpy as np
from photosort import db

def test_roundtrip(tmp_path):
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, dict(rel="a.jpg", size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    pid2 = db.upsert_photo(conn, dict(rel="a.jpg", size=2, mtime=2.0, qhash="h2", sibling=None, width=10, height=10,
        taken_at=None, camera=None, phash="0"*16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok"))
    assert pid == pid2
    assert db.known_files(conn) == {"a.jpg": (2, 2.0)}
    assert db.photos_missing_embed(conn) == [(pid, "a.jpg")]
    db.set_embed(conn, pid, np.ones(512, np.float32))
    ids, M = db.load_embeds(conn)
    assert ids.tolist() == [pid] and M.shape == (1, 512) and M.dtype == np.float32
    db.replace_faces(conn, pid, [dict(x=1,y=2,w=3,h=4,score=0.9,landmarks="[]",eye_sharp=5.0,embed=np.ones(128,np.float32).tobytes())])
    fids, pids, F = db.load_face_embeds(conn)
    assert F.shape == (1, 128) and pids.tolist() == [pid]
    db.mark_missing(conn, set())
    assert conn.execute("select status from photos").fetchone()[0] == "missing"
    d = db.index_dir(tmp_path)
    assert (d / "thumbs").is_dir() and (d / "grid").is_dir()
    assert not str(d.resolve()).startswith(str(tmp_path.resolve()))   # never inside the shoot
    assert not (tmp_path / ".photosort").exists()
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Append to config.py**

```python
import os, hashlib

def app_home() -> Path:
    return Path(os.environ.get("PHOTOSORT_HOME") or (Path.home() / "Library" / "Application Support" / "photosort"))

def export_root() -> Path:
    return Path(os.environ.get("PHOTOSORT_EXPORT_DIR") or (Path.home() / "Desktop" / "photosort-out"))

def shoot_slug(root: Path) -> str:
    r = Path(root).resolve()
    return f"{r.name or 'root'}-{hashlib.sha1(str(r).encode()).hexdigest()[:8]}"
```

Append to `tests/conftest.py`:
```python
@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("PHOTOSORT_HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("PHOTOSORT_EXPORT_DIR", str(tmp_path_factory.mktemp("out")))
```

- [ ] **Step 4: Implement db.py**

```python
from __future__ import annotations
import sqlite3
from pathlib import Path
import numpy as np
from .config import DB_NAME, EMBED_DIM, app_home, shoot_slug

SCHEMA = """<paste the schema block above verbatim>"""

PHOTO_COLS = ["rel","size","mtime","qhash","sibling","width","height","taken_at","camera","phash",
              "sharp_tile","sharp_max","sharp_eye","sharp","n_faces","status"]

def index_dir(root: Path) -> Path:
    d = app_home() / shoot_slug(root)
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    (d / "grid").mkdir(parents=True, exist_ok=True)
    return d

def connect(root: Path) -> sqlite3.Connection:
    d = index_dir(root)
    conn = sqlite3.connect(d / DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn

def upsert_photo(conn, row: dict) -> int:
    cols = ",".join(PHOTO_COLS); ph = ",".join("?" * len(PHOTO_COLS))
    upd = ",".join(f"{c}=excluded.{c}" for c in PHOTO_COLS if c != "rel")
    conn.execute(f"INSERT INTO photos({cols}) VALUES({ph}) ON CONFLICT(rel) DO UPDATE SET {upd}, embed=NULL, indexed_at=datetime('now')",
                 [row.get(c) for c in PHOTO_COLS])
    conn.commit()
    return conn.execute("SELECT id FROM photos WHERE rel=?", (row["rel"],)).fetchone()[0]

def replace_faces(conn, photo_id: int, faces: list[dict]) -> None:
    conn.execute("DELETE FROM faces WHERE photo_id=?", (photo_id,))
    conn.executemany("INSERT INTO faces(photo_id,x,y,w,h,score,landmarks,eye_sharp,embed) VALUES(?,?,?,?,?,?,?,?,?)",
        [(photo_id, f["x"], f["y"], f["w"], f["h"], f["score"], f["landmarks"], f["eye_sharp"], f["embed"]) for f in faces])
    conn.commit()

def set_embed(conn, photo_id: int, vec: np.ndarray) -> None:
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (np.asarray(vec, np.float16).tobytes(), photo_id))

def photos_missing_embed(conn) -> list[tuple[int, str]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT id, rel FROM photos WHERE embed IS NULL AND status='ok' ORDER BY id")]

def load_embeds(conn):
    rows = conn.execute("SELECT id, embed FROM photos WHERE embed IS NOT NULL AND status='ok' ORDER BY id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros((0, EMBED_DIM), np.float32)
    ids = np.array([r[0] for r in rows], np.int64)
    M = np.stack([np.frombuffer(r[1], np.float16).astype(np.float32) for r in rows])
    return ids, M

def load_face_embeds(conn):
    rows = conn.execute("SELECT f.id, f.photo_id, f.embed FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' ORDER BY f.id").fetchall()
    if not rows:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 128), np.float32)
    return (np.array([r[0] for r in rows], np.int64), np.array([r[1] for r in rows], np.int64),
            np.stack([np.frombuffer(r[2], np.float32) for r in rows]))

def known_files(conn) -> dict[str, tuple[int, float]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT rel, size, mtime FROM photos")}

def mark_missing(conn, present: set[str]) -> None:
    for (rel,) in conn.execute("SELECT rel FROM photos WHERE status='ok'").fetchall():
        if rel not in present:
            conn.execute("UPDATE photos SET status='missing' WHERE rel=?", (rel,))
    conn.commit()
```

- [ ] **Step 5: Run tests, expect pass. Commit** `feat: sqlite store for photos, faces, people; index lives outside the shoot`.

---

### Task 7: embed.py

**Files:**
- Create: `photosort/embed.py`, `tests/test_embed.py`

**Interfaces:**
- Produces: `class Embedder: __init__(device: str|None=None)` lazy-loads MobileCLIP-S1 on first use, picks `mps` if available; `encode_images(ims: list[PIL]) -> np.ndarray (N,512) float32 L2-normalised`; `encode_text(texts: list[str]) -> np.ndarray (M,512) float32 L2-normalised` with prompt template `"a photo of {t}"` unless the text already starts with "a photo"; module-level `get_embedder() -> Embedder` singleton.

- [ ] **Step 1: Failing test**

```python
import numpy as np
from PIL import Image
from photosort.embed import get_embedder

def test_text_and_image_agree():
    e = get_embedder()
    red = Image.new("RGB", (256, 256), (220, 20, 20)); blue = Image.new("RGB", (256, 256), (20, 20, 220))
    I = e.encode_images([red, blue]); T = e.encode_text(["a red square", "a blue square"])
    assert I.shape == (2, 512) and abs(np.linalg.norm(I[0]) - 1) < 1e-3
    S = T @ I.T
    assert S[0, 0] > S[0, 1] and S[1, 1] > S[1, 0]
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
import numpy as np, torch, open_clip
from PIL import Image
from .config import CLIP_MODEL, CLIP_PRETRAINED, EMBED_BATCH

class Embedder:
    def __init__(self, device: str | None = None):
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self._model = self._pre = self._tok = None

    def _load(self):
        if self._model is None:
            m, _, pre = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=CLIP_PRETRAINED)
            self._model = m.eval().to(self.device); self._pre = pre
            self._tok = open_clip.get_tokenizer(CLIP_MODEL)

    @torch.no_grad()
    def encode_images(self, ims: list[Image.Image]) -> np.ndarray:
        self._load()
        out = []
        for i in range(0, len(ims), EMBED_BATCH):
            x = torch.stack([self._pre(im.convert("RGB")) for im in ims[i:i + EMBED_BATCH]]).to(self.device)
            f = self._model.encode_image(x)
            out.append((f / f.norm(dim=-1, keepdim=True)).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, 512), np.float32)

    @torch.no_grad()
    def encode_text(self, texts: list[str]) -> np.ndarray:
        self._load()
        texts = [t if t.lower().startswith("a photo") else f"a photo of {t}" for t in texts]
        f = self._model.encode_text(self._tok(texts).to(self.device))
        return (f / f.norm(dim=-1, keepdim=True)).float().cpu().numpy()

_E: Embedder | None = None
def get_embedder() -> Embedder:
    global _E
    if _E is None:
        _E = Embedder()
    return _E
```

- [ ] **Step 4: Run test (takes ~10 s after the first cached load), expect pass. Commit** `feat: MobileCLIP embedder`.

---

### Task 8: index.py (the pipeline)

**Files:**
- Create: `photosort/index.py`, `tests/test_index.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `process_one(args: tuple[str root, str rel, bool faces]) -> dict` (top-level function, picklable; returns `{"rel", "row": {...photo cols...}, "faces": [...face dicts...], "error": str|None}`; writes `thumbs/<qhash>.jpg` (1024) and `grid/<qhash>.jpg` (320); row includes `qhash`); `index_folder(root: Path, faces: bool = True, workers: int|None = None, progress: Callable[[dict], None]|None = None, embed: bool = True) -> dict(stats)` with stats keys `total, skipped, indexed, errors, embedded, seconds`. Incremental: skips files whose (size, mtime) match `known_files`. Progress dicts: `{"stage": "scan"|"features"|"embed"|"done", "done": int, "total": int}`.
- Thumb path convention used by server/search: `index_dir(root)/"thumbs"/f"{qhash}.jpg"`, `.../"grid"/f"{qhash}.jpg"`. Nothing is ever written under `root`.

- [ ] **Step 1: Failing test**

```python
import numpy as np
from photosort.index import index_folder
from photosort import db

def test_index_then_incremental(tmp_path):
    from tests.conftest import make_image
    for i in range(6):
        make_image(tmp_path, f"p{i}.jpg", kind="sharp" if i < 4 else "blurry", seed=i)
    (tmp_path / "junk.jpg").write_bytes(b"nope")
    s1 = index_folder(tmp_path, faces=True, workers=2)
    assert s1["indexed"] == 6 and s1["errors"] == 1 and s1["embedded"] == 6
    conn = db.connect(tmp_path)
    rows = conn.execute("SELECT rel, sharp, qhash FROM photos WHERE status='ok' ORDER BY rel").fetchall()
    assert len(rows) == 6
    idx = db.index_dir(tmp_path)
    assert (idx / "thumbs" / f"{rows[0]['qhash']}.jpg").exists()
    assert (idx / "grid" / f"{rows[0]['qhash']}.jpg").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"p{i}.jpg" for i in range(6)] + ["junk.jpg"]   # nothing written into the shoot
    sharp = [r["sharp"] for r in rows]
    assert min(sharp[:4]) > max(sharp[4:])
    ids, M = db.load_embeds(conn); assert M.shape == (6, 512)
    s2 = index_folder(tmp_path, faces=True, workers=2)
    assert s2["skipped"] == 7 and s2["indexed"] == 0 and s2["errors"] == 0
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
import json, multiprocessing as mp, time
from pathlib import Path
from typing import Callable
import numpy as np
from PIL import Image
from . import db
from .config import PREVIEW_EDGE, GRID_EDGE, THUMB_QUALITY, JPEG_WORKERS, RAW_WORKERS, EMBED_BATCH
from .walk import find_images, quick_hash
from .decode import load_preview, DecodeError
from .features import phash, exif_info, sharpness_tiles, to_gray

_ENGINE = None
def _face_engine():
    global _ENGINE
    if _ENGINE is None:
        from .faces import FaceEngine
        _ENGINE = FaceEngine()
    return _ENGINE

def process_one(args: tuple[str, str, bool]) -> dict:
    root, rel, want_faces = args
    path = Path(root) / rel
    out = {"rel": rel, "row": None, "faces": [], "error": None}
    try:
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
        out["row"] = dict(rel=rel, size=st.st_size, mtime=st.st_mtime, qhash=qh, sibling=None,
            width=info["width"] or im.width, height=info["height"] or im.height, taken_at=info["taken_at"],
            camera=info["camera"], phash=phash(im), sharp_tile=p90, sharp_max=mx, sharp_eye=eye,
            sharp=eye if eye is not None else p90, n_faces=len(faces), status="ok")
        out["faces"] = [dict(x=f.x, y=f.y, w=f.w, h=f.h, score=f.score, landmarks=json.dumps(f.landmarks.tolist()),
                             eye_sharp=f.eye_sharp, embed=f.embed.astype(np.float32).tobytes()) for f in faces]
    except (DecodeError, Exception) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out

def index_folder(root: Path, faces: bool = True, workers: int | None = None,
                 progress: Callable[[dict], None] | None = None, embed: bool = True) -> dict:
    t0 = time.time(); root = Path(root)
    notify = progress or (lambda d: None)
    conn = db.connect(root)
    notify({"stage": "scan", "done": 0, "total": 0})
    files = find_images(root)
    known = db.known_files(conn)
    todo = [f for f in files if known.get(f.rel) != (f.size, f.mtime)]
    stats = dict(total=len(files), skipped=len(files) - len(todo), indexed=0, errors=0, embedded=0)
    db.mark_missing(conn, {f.rel for f in files})
    if todo:
        n_raw = sum(f.is_raw for f in todo)
        workers = workers or (RAW_WORKERS if n_raw > len(todo) / 2 else JPEG_WORKERS)
        sib = {f.rel: f.sibling for f in todo}
        meta = {f.rel: (f.size, f.mtime) for f in todo}
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for i, res in enumerate(pool.imap_unordered(process_one, [(str(root), f.rel, faces) for f in todo], chunksize=2), 1):
                if res["error"]:
                    stats["errors"] += 1
                    db.upsert_photo(conn, dict(rel=res["rel"], size=meta[res["rel"]][0], mtime=meta[res["rel"]][1], status="error", n_faces=0))
                else:
                    res["row"]["sibling"] = sib.get(res["rel"])
                    pid = db.upsert_photo(conn, res["row"])
                    db.replace_faces(conn, pid, res["faces"])
                    stats["indexed"] += 1
                notify({"stage": "features", "done": i, "total": len(todo)})
    if embed:
        from .embed import get_embedder
        pending = db.photos_missing_embed(conn)
        idx = db.index_dir(root)
        qh = {r[0]: r[1] for r in conn.execute("SELECT id, qhash FROM photos WHERE embed IS NULL AND status='ok'")}
        E = get_embedder()
        for i in range(0, len(pending), EMBED_BATCH):
            batch = pending[i:i + EMBED_BATCH]
            ims = [Image.open(idx / "thumbs" / f"{qh[pid]}.jpg") for pid, _ in batch]
            vecs = E.encode_images(ims)
            for (pid, _), v in zip(batch, vecs):
                db.set_embed(conn, pid, v)
            conn.commit()
            stats["embedded"] += len(batch)
            notify({"stage": "embed", "done": min(i + EMBED_BATCH, len(pending)), "total": len(pending)})
    stats["seconds"] = round(time.time() - t0, 1)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_index', datetime('now'))"); conn.commit()
    notify({"stage": "done", "done": stats["total"], "total": stats["total"]})
    return stats
```

Note for the implementer: `upsert_photo` with a partial dict relies on `row.get(c)` returning None for absent columns; that is intended for the error row.

- [ ] **Step 4: Run test, expect pass (spawn pool + model load, ~60 s). Commit** `feat: two-stage indexing pipeline, incremental`.

---

### Task 9: cli.py with `index` and `bench`

**Files:**
- Create: `photosort/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv=None)`; subcommands `index <folder> [--no-faces] [--workers N]`, `bench <folder> [--n 200]` (indexes the first N files into a temp copy of the index dir? No: bench indexes into the real index but only the first N files, prints ms/photo per stage and projected time for the full folder and for 10,000 photos). Later tasks add `find`, `people`, `serve`.

- [ ] **Step 1: Failing test**

```python
from photosort.cli import main
def test_index_cmd(tmp_path, capsys):
    from tests.conftest import make_image
    make_image(tmp_path, "a.jpg")
    main(["index", str(tmp_path), "--no-faces", "--workers", "1"])
    out = capsys.readouterr().out
    assert "indexed 1" in out
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
import argparse, sys, time
from pathlib import Path

def _progress(d):
    if d["total"]:
        sys.stderr.write(f"\r{d['stage']:>8} {d['done']}/{d['total']}   "); sys.stderr.flush()
    if d["stage"] == "done":
        sys.stderr.write("\n")

def cmd_index(a):
    from .index import index_folder
    s = index_folder(Path(a.folder), faces=not a.no_faces, workers=a.workers, progress=_progress)
    print(f"indexed {s['indexed']}  skipped {s['skipped']}  errors {s['errors']}  embedded {s['embedded']}  in {s['seconds']}s")

def cmd_bench(a):
    from .walk import find_images
    from .index import process_one, index_folder
    from .embed import get_embedder
    from . import db
    from PIL import Image
    root = Path(a.folder); files = find_images(root)[: a.n]
    if not files:
        print("no images"); return
    db.connect(root)
    t = time.time(); ok = 0
    for f in files:
        r = process_one((str(root), f.rel, True)); ok += r["error"] is None
    feat_ms = (time.time() - t) / len(files) * 1000
    idx = db.index_dir(root)
    from .walk import quick_hash
    ims = [Image.open(idx / "thumbs" / f"{quick_hash(f.path)}.jpg") for f in files if (idx / "thumbs" / f"{quick_hash(f.path)}.jpg").exists()]
    E = get_embedder(); E.encode_images(ims[:4])
    t = time.time(); E.encode_images(ims); emb_ms = (time.time() - t) / max(len(ims), 1) * 1000
    total = len(find_images(root))
    per = feat_ms / 4 + emb_ms   # 4 workers on features, embed is serial on the GPU
    print(f"files {len(files)} ok {ok}  features {feat_ms:.0f} ms/photo (1 core)  embed {emb_ms:.1f} ms/photo")
    print(f"projected @4 workers: {per:.0f} ms/photo  -> this folder ({total}) {total*per/60000:.1f} min, 10k photos {10000*per/60000:.1f} min")

def main(argv=None):
    p = argparse.ArgumentParser(prog="photosort")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("index"); s.add_argument("folder"); s.add_argument("--no-faces", action="store_true"); s.add_argument("--workers", type=int); s.set_defaults(fn=cmd_index)
    s = sub.add_parser("bench"); s.add_argument("folder"); s.add_argument("--n", type=int, default=200); s.set_defaults(fn=cmd_bench)
    a = p.parse_args(argv); a.fn(a)

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test, expect pass. Commit** `feat: cli index and bench`.

---

### Task 10: search.py + export.py + cli `find`

**Files:**
- Create: `photosort/search.py`, `photosort/export.py`, `tests/test_search.py`, `tests/test_export.py`
- Modify: `photosort/cli.py` (add `find`)

**Interfaces:**
- Produces: `@dataclass Filters(sharp_min_pct: float|None=None, faces: str|None=None  # "none"|"one"|"two"|"group", person_id: int|None=None, taken_from: str|None=None, taken_to: str|None=None)`; `class Index: __init__(root)` loads embeds + a photo table into memory, `refresh()`, `search(text: str|None=None, image_id: int|None=None, filters: Filters=Filters(), limit: int=200) -> list[dict]` each dict `{id, rel, qhash, score, sharp, sharp_pct, n_faces, taken_at, width, height}`; with no text/image, returns photos ordered by taken_at then rel (browse mode). `sharp_pct` is the photo's percentile of `sharp` within the shoot (0-100).
- `export_ids(root: Path, ids: list[int], name: str, mode: str = "copy") -> Path` writes into `export_root() / <root.resolve().name> / <name>/` (on the Desktop by default, never on the source drive), modes `copy|symlink|csv`, default copy; returns the out dir. Filenames keep the original basename; on collision prefix with the photo id.

- [ ] **Step 1: Failing tests**

```python
# tests/test_search.py
from photosort.index import index_folder
from photosort.search import Index, Filters
from PIL import Image

def test_search_and_filters(tmp_path):
    from tests.conftest import make_image
    make_image(tmp_path, "sharp.jpg", kind="sharp"); make_image(tmp_path, "soft.jpg", kind="blurry")
    Image.new("RGB", (900, 600), (200, 30, 30)).save(tmp_path / "red.jpg")
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    allp = ix.search(); assert len(allp) == 3 and all("sharp_pct" in r for r in allp)
    top = ix.search(text="a red wall")[0]; assert top["rel"] == "red.jpg"
    # red.jpg is a flat colour so it scores 0 sharpness; only sharp.jpg is above the 60th percentile
    sharp_only = ix.search(filters=Filters(sharp_min_pct=60)); assert [r["rel"] for r in sharp_only] == ["sharp.jpg"]
    like = ix.search(image_id=top["id"]); assert like[0]["id"] == top["id"]
    assert ix.search(filters=Filters(faces="one")) == []
```
```python
# tests/test_export.py
from photosort.index import index_folder
from photosort.search import Index
from photosort.export import export_ids

def test_export_copy_default_symlink_and_csv(tmp_path):
    import os
    from tests.conftest import make_image
    from photosort.config import export_root
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids = [r["id"] for r in Index(tmp_path).search()]
    out = export_ids(tmp_path, ids, "test")
    assert out == export_root() / tmp_path.resolve().name / "test"
    assert (out / "a.jpg").is_file() and not (out / "a.jpg").is_symlink()
    assert (out / "a.jpg").read_bytes() == (tmp_path / "a.jpg").read_bytes()
    ln = export_ids(tmp_path, ids, "links", "symlink")
    assert (ln / "a.jpg").is_symlink() and (ln / "a.jpg").resolve() == (tmp_path / "a.jpg").resolve()
    out2 = export_ids(tmp_path, ids, "csv", "csv")
    assert "a.jpg" in (out2 / "photos.csv").read_text()
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]   # source folder untouched
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement search.py**

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from . import db
from .config import GROUP_MIN_FACES

@dataclass
class Filters:
    sharp_min_pct: float | None = None
    faces: str | None = None
    person_id: int | None = None
    taken_from: str | None = None
    taken_to: str | None = None

class Index:
    def __init__(self, root: Path):
        self.root = Path(root); self.conn = db.connect(self.root); self.refresh()

    def refresh(self):
        rows = self.conn.execute("SELECT id, rel, qhash, sharp, n_faces, taken_at, width, height FROM photos WHERE status='ok' ORDER BY id").fetchall()
        self.photos = {r["id"]: dict(r) for r in rows}
        sharp = np.array([r["sharp"] or 0.0 for r in rows], float)
        order = sharp.argsort().argsort()
        for r, rank in zip(rows, order):
            self.photos[r["id"]]["sharp_pct"] = float(rank) / max(len(rows) - 1, 1) * 100
        self.ids, self.M = db.load_embeds(self.conn)
        self.pos = {pid: i for i, pid in enumerate(self.ids.tolist())}

    def _person_photo_ids(self, person_id: int) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT photo_id FROM faces WHERE person_id=?", (person_id,))}

    def _passes(self, p: dict, f: Filters, person_ids: set[int] | None) -> bool:
        if f.sharp_min_pct is not None and p["sharp_pct"] < f.sharp_min_pct: return False
        n = p["n_faces"] or 0
        if f.faces == "none" and n != 0: return False
        if f.faces == "one" and n != 1: return False
        if f.faces == "two" and n != 2: return False
        if f.faces == "group" and n < GROUP_MIN_FACES: return False
        if person_ids is not None and p["id"] not in person_ids: return False
        t = p["taken_at"] or ""
        if f.taken_from and t < f.taken_from: return False
        if f.taken_to and t > f.taken_to: return False
        return True

    def search(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(), limit: int = 200) -> list[dict]:
        person_ids = self._person_photo_ids(filters.person_id) if filters.person_id is not None else None
        cands = [p for p in self.photos.values() if self._passes(p, filters, person_ids)]
        if text or image_id is not None:
            if image_id is not None:
                q = self.M[self.pos[image_id]]
            else:
                from .embed import get_embedder
                q = get_embedder().encode_text([text])[0]
            scores = self.M @ q
            for p in cands:
                i = self.pos.get(p["id"]); p["score"] = float(scores[i]) if i is not None else -1.0
            cands.sort(key=lambda p: -p["score"])
        else:
            for p in cands: p["score"] = 0.0
            cands.sort(key=lambda p: ((p["taken_at"] or "~"), p["rel"]))
        return [dict(p) for p in cands[:limit]]
```

- [ ] **Step 4: Implement export.py**

```python
from __future__ import annotations
import csv, os, shutil
from pathlib import Path
from . import db
from .config import export_root

def export_ids(root: Path, ids: list[int], name: str, mode: str = "copy") -> Path:
    root = Path(root); out = export_root() / root.resolve().name / name; out.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root)
    q = ",".join("?" * len(ids)) if ids else "NULL"
    rows = conn.execute(f"SELECT id, rel, sharp, n_faces, taken_at FROM photos WHERE id IN ({q}) ORDER BY id", ids).fetchall()
    if mode == "csv":
        with open(out / "photos.csv", "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["id", "path", "sharp", "n_faces", "taken_at"])
            for r in rows: w.writerow([r["id"], str(root / r["rel"]), r["sharp"], r["n_faces"], r["taken_at"]])
        return out
    for r in rows:
        src = root / r["rel"]; dst = out / Path(r["rel"]).name
        if dst.exists() or dst.is_symlink():
            dst = out / f"{r['id']}_{Path(r['rel']).name}"
        if mode == "copy": shutil.copy2(src, dst)
        else: os.symlink(src.resolve(), dst)
    return out
```

- [ ] **Step 5: Add `find` to cli.py**

```python
def cmd_find(a):
    from .search import Index, Filters
    from .export import export_ids
    ix = Index(Path(a.folder))
    f = Filters(sharp_min_pct=a.sharp, faces=a.faces)
    res = ix.search(text=a.query, filters=f, limit=a.limit)
    for r in res: print(f"{r['score']:.3f}  {r['sharp_pct']:5.1f}%  {r['n_faces']}f  {r['rel']}")
    if a.out:
        print("exported to", export_ids(Path(a.folder), [r["id"] for r in res], a.out, a.mode))
# in main():
    s = sub.add_parser("find"); s.add_argument("folder"); s.add_argument("query", nargs="?")
    s.add_argument("--sharp", type=float, help="min sharpness percentile 0-100"); s.add_argument("--faces", choices=["none","one","two","group"])
    s.add_argument("--limit", type=int, default=50); s.add_argument("--out", help="export folder name (created under ~/Desktop/photosort-out/<shoot>/)"); s.add_argument("--mode", default="copy", choices=["copy","symlink","csv"])
    s.set_defaults(fn=cmd_find)
```

- [ ] **Step 6: Run all tests, expect pass. Commit** `feat: search with filters, export, cli find`.

---

### Task 11: people.py + cli `people`

**Files:**
- Create: `photosort/people.py`, `tests/test_people.py`
- Modify: `photosort/cli.py`

**Interfaces:**
- Produces: `cluster_faces(root: Path, eps: float = FACE_CLUSTER_EPS, min_samples: int = FACE_MIN_SAMPLES) -> list[dict]` runs sklearn DBSCAN(metric="cosine") on all face embeds, writes `faces.person_id` (NULL for noise), rebuilds `people` rows (`n` = photo count, `cover_face_id` = highest score face), returns `[{id, name, n, cover_face_id, cover_qhash, cover_box}]` sorted by n desc; `list_people(root) -> same list`; `name_person(root, person_id, name)`; `assign_from_reference(root, image_path: Path) -> int|None` detects the largest face in the reference image and returns the person_id whose centroid is closest if cosine sim ≥ 0.5; `export_people(root, mode="copy") -> Path` writes `export_root()/<shoot name>/people/<name or person_NN>/`, plus `.../groups/` (n_faces ≥ 3) and `.../solo/` (n_faces == 1); returns `export_root()/<shoot name>`.

- [ ] **Step 1: Failing test (synthetic embeds, no real faces)**

```python
import numpy as np
from photosort import db
from photosort.people import cluster_faces, name_person, list_people, export_people

def _fake_shoot(tmp_path, n_people=3, per=4):
    conn = db.connect(tmp_path); rng = np.random.default_rng(1)
    centers = rng.normal(size=(n_people, 128)); centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    for k in range(n_people):
        for j in range(per):
            (tmp_path / f"p{k}_{j}.jpg").write_bytes(b"x")
            pid = db.upsert_photo(conn, dict(rel=f"p{k}_{j}.jpg", qhash=f"q{k}{j}", n_faces=1, status="ok"))
            v = centers[k] + rng.normal(scale=0.05, size=128); v /= np.linalg.norm(v)
            db.replace_faces(conn, pid, [dict(x=0,y=0,w=10,h=10,score=0.9,landmarks="[]",eye_sharp=1.0,embed=v.astype(np.float32).tobytes())])
    return conn

def test_cluster_and_name(tmp_path):
    _fake_shoot(tmp_path)
    people = cluster_faces(tmp_path, eps=0.3)
    assert len(people) == 3 and all(p["n"] == 4 for p in people)
    name_person(tmp_path, people[0]["id"], "Arya")
    assert list_people(tmp_path)[0]["name"] == "Arya"
    out = export_people(tmp_path)
    assert (out / "Arya").is_dir() and len(list((out / "Arya").iterdir())) == 4
    assert (out / "solo").is_dir() and len(list((out / "solo").iterdir())) == 12
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations
import os
from pathlib import Path
import numpy as np
from sklearn.cluster import DBSCAN
from . import db
from .config import FACE_CLUSTER_EPS, FACE_MIN_SAMPLES, GROUP_MIN_FACES

def cluster_faces(root: Path, eps: float = FACE_CLUSTER_EPS, min_samples: int = FACE_MIN_SAMPLES) -> list[dict]:
    conn = db.connect(root)
    fids, pids, F = db.load_face_embeds(conn)
    conn.execute("UPDATE faces SET person_id=NULL"); conn.execute("DELETE FROM people"); conn.commit()
    if len(fids) == 0:
        return []
    labels = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine", n_jobs=1).fit_predict(F)
    for lab in sorted(set(labels) - {-1}):
        idx = np.where(labels == lab)[0]
        n_photos = len(set(pids[idx].tolist()))
        best = conn.execute(f"SELECT id FROM faces WHERE id IN ({','.join('?'*len(idx))}) ORDER BY score DESC LIMIT 1", fids[idx].tolist()).fetchone()[0]
        cur = conn.execute("INSERT INTO people(name, cover_face_id, n) VALUES(NULL, ?, ?)", (best, n_photos))
        conn.executemany("UPDATE faces SET person_id=? WHERE id=?", [(cur.lastrowid, int(f)) for f in fids[idx]])
    conn.commit()
    return list_people(root)

def list_people(root: Path) -> list[dict]:
    conn = db.connect(root)
    rows = conn.execute("""SELECT pe.id, pe.name, pe.n, pe.cover_face_id, p.qhash, f.x, f.y, f.w, f.h
                           FROM people pe JOIN faces f ON f.id=pe.cover_face_id JOIN photos p ON p.id=f.photo_id
                           ORDER BY pe.n DESC, pe.id""").fetchall()
    return [dict(id=r[0], name=r[1], n=r[2], cover_face_id=r[3], cover_qhash=r[4], cover_box=[r[5], r[6], r[7], r[8]]) for r in rows]

def name_person(root: Path, person_id: int, name: str) -> None:
    conn = db.connect(root); conn.execute("UPDATE people SET name=? WHERE id=?", (name.strip() or None, person_id)); conn.commit()

def assign_from_reference(root: Path, image_path: Path) -> int | None:
    from .decode import load_preview
    from .faces import FaceEngine
    faces = FaceEngine().detect(load_preview(image_path))
    if not faces:
        return None
    q = max(faces, key=lambda f: f.w * f.h).embed
    conn = db.connect(root); fids, pids, F = db.load_face_embeds(conn)
    labels = np.array([r[0] or -1 for r in conn.execute("SELECT person_id FROM faces ORDER BY id")])
    best, best_sim = None, 0.5
    for lab in set(labels.tolist()) - {-1}:
        c = F[labels == lab].mean(axis=0); c /= np.linalg.norm(c)
        s = float(c @ q)
        if s > best_sim: best, best_sim = lab, s
    return best

def export_people(root: Path, mode: str = "copy") -> Path:
    from .export import export_ids
    from .config import export_root
    root = Path(root); conn = db.connect(root)
    for p in list_people(root):
        ids = [r[0] for r in conn.execute("SELECT DISTINCT photo_id FROM faces WHERE person_id=?", (p["id"],))]
        export_ids(root, ids, f"people/{p['name'] or f'person_{p['id']:02d}'}", mode)
    groups = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' AND n_faces>=?", (GROUP_MIN_FACES,))]
    solo = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' AND n_faces=1")]
    export_ids(root, groups, "groups", mode); export_ids(root, solo, "solo", mode)
    return export_root() / root.resolve().name
```

Note: `db.load_face_embeds` orders by `f.id`, and the `labels` query in `assign_from_reference` also orders by id, so they line up; keep both `ORDER BY` clauses.

- [ ] **Step 4: Add `people` to cli.py**

```python
def cmd_people(a):
    from .people import cluster_faces, export_people, name_person
    people = cluster_faces(Path(a.folder), eps=a.eps)
    for p in people: print(f"person_{p['id']:02d}  {p['n']} photos")
    if a.export: print("exported to", export_people(Path(a.folder), a.mode))
# main():
    s = sub.add_parser("people"); s.add_argument("folder"); s.add_argument("--eps", type=float, default=0.5)
    s.add_argument("--export", action="store_true"); s.add_argument("--mode", default="copy", choices=["copy","symlink"]); s.set_defaults(fn=cmd_people)
```

- [ ] **Step 5: Run tests, expect pass. Commit** `feat: face clustering, people naming, people/groups/solo export`.

---

### Task 12: server.py + UI

**Files:**
- Create: `photosort/server.py`, `photosort/ui/index.html`, `photosort/ui/app.js`, `photosort/ui/style.css`, `tests/test_server.py`
- Modify: `photosort/cli.py` (add `serve`)

**Interfaces:**
- Produces: `create_app(root: Path) -> FastAPI`. Endpoints:
  - `GET /` serves `ui/index.html`; `GET /ui/{file}` static.
  - `GET /api/stats` -> `{root, photos, faces, people, last_index, indexing: bool}`
  - `POST /api/index {"faces": true}` starts `index_folder` in a background thread (409 if already running); `GET /api/progress` -> last progress dict + `running`.
  - `GET /api/search?q=&image_id=&sharp=&faces=&person=&limit=` -> `{results: [...]}` (Index.search; `Index.refresh()` is called when `indexing` just finished).
  - `GET /api/thumb/{qhash}?size=grid|full` -> the JPEG file.
  - `GET /api/people` -> list; `POST /api/people/cluster {"eps": 0.5}`; `POST /api/people/{id}/name {"name": "..."}`.
  - `POST /api/export {"ids": [...], "name": "...", "mode": "copy"}` -> `{path}` (a folder under `~/Desktop/photosort-out/<shoot>/`); `POST /api/export/people {"mode": "copy"}` -> `{path}`. Show the returned path in the UI status line so the user knows where the copies went.
- `cli serve <folder> [--port 7777] [--open]` runs uvicorn and opens the browser.

- [ ] **Step 1: Failing test (TestClient, no model)**

```python
from fastapi.testclient import TestClient
from photosort.index import index_folder
from photosort.server import create_app

def test_api(tmp_path):
    from tests.conftest import make_image
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    assert c.get("/").status_code == 200 and "photosort" in c.get("/").text.lower()
    st = c.get("/api/stats").json(); assert st["photos"] == 1
    res = c.get("/api/search").json()["results"]; assert res[0]["rel"] == "a.jpg"
    assert c.get(f"/api/thumb/{res[0]['qhash']}?size=grid").headers["content-type"] == "image/jpeg"
    ex = c.post("/api/export", json={"ids": [res[0]["id"]], "name": "t"}).json()
    from pathlib import Path
    assert Path(ex["path"]).is_dir() and (Path(ex["path"]) / "a.jpg").is_file() and not str(Path(ex["path"])).startswith(str(tmp_path))
    assert c.get("/api/people").json() == []
```

- [ ] **Step 2: Run, expect ImportError.**

- [ ] **Step 3: Implement server.py**

```python
from __future__ import annotations
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import db
from .search import Index, Filters
from .export import export_ids

UI = Path(__file__).parent / "ui"

class ExportReq(BaseModel):
    ids: list[int]; name: str; mode: str = "copy"
class NameReq(BaseModel):
    name: str
class ClusterReq(BaseModel):
    eps: float = 0.5
class IndexReq(BaseModel):
    faces: bool = True
class ModeReq(BaseModel):
    mode: str = "copy"

def create_app(root: Path) -> FastAPI:
    root = Path(root); app = FastAPI(title="photosort")
    state = {"index": Index(root), "progress": {"stage": "idle", "done": 0, "total": 0}, "running": False, "stale": False}

    def _run(faces: bool):
        from .index import index_folder
        def prog(d): state["progress"] = d
        try: index_folder(root, faces=faces, progress=prog)
        finally: state["running"] = False; state["stale"] = True

    def ix() -> Index:
        if state["stale"]: state["index"].refresh(); state["stale"] = False
        return state["index"]

    @app.get("/")
    def home(): return FileResponse(UI / "index.html")
    @app.get("/ui/{name}")
    def ui(name: str):
        p = UI / name
        if not p.is_file(): raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/stats")
    def stats():
        conn = db.connect(root)
        n = lambda q: conn.execute(q).fetchone()[0]
        last = conn.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        return dict(root=str(root), photos=n("SELECT count(*) FROM photos WHERE status='ok'"), faces=n("SELECT count(*) FROM faces"),
                    people=n("SELECT count(*) FROM people"), last_index=last[0] if last else None, indexing=state["running"])

    @app.post("/api/index")
    def start_index(req: IndexReq):
        if state["running"]: raise HTTPException(409, "already indexing")
        state["running"] = True; threading.Thread(target=_run, args=(req.faces,), daemon=True).start()
        return {"started": True}
    @app.get("/api/progress")
    def progress(): return dict(state["progress"], running=state["running"])

    @app.get("/api/search")
    def search(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
               person: int | None = None, taken_from: str | None = None, taken_to: str | None = None, limit: int = 200):
        f = Filters(sharp_min_pct=sharp, faces=faces or None, person_id=person, taken_from=taken_from, taken_to=taken_to)
        return {"results": ix().search(text=q or None, image_id=image_id, filters=f, limit=limit)}

    @app.get("/api/thumb/{qhash}")
    def thumb(qhash: str, size: str = "grid"):
        p = db.index_dir(root) / ("grid" if size == "grid" else "thumbs") / f"{qhash}.jpg"
        if not p.is_file(): raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/people")
    def people():
        from .people import list_people
        return list_people(root)
    @app.post("/api/people/cluster")
    def cluster(req: ClusterReq):
        from .people import cluster_faces
        out = cluster_faces(root, eps=req.eps); state["stale"] = True; return out
    @app.post("/api/people/{pid}/name")
    def name(pid: int, req: NameReq):
        from .people import name_person
        name_person(root, pid, req.name); return {"ok": True}

    @app.post("/api/export")
    def export(req: ExportReq): return {"path": str(export_ids(root, req.ids, req.name, req.mode))}
    @app.post("/api/export/people")
    def export_people_api(req: ModeReq):
        from .people import export_people
        return {"path": str(export_people(root, req.mode))}
    return app
```

- [ ] **Step 4: Build the UI (handmade, editorial, no orange).** One page, three views switched by tabs: **Browse/Search**, **People**, **Index**. Visual rules: off-white paper `#f4f1ea` ground, ink `#161616`, one accent deep green `#1f4d3a`, monospace for numbers and labels, a serif display face for the title, 8 px hairline borders, no drop shadows, no rounded-pill buttons, no gradients. Everything in `index.html` + `app.js` + `style.css`, no frameworks, no CDN (offline).

`index.html` skeleton:
```html
<!doctype html><html><head><meta charset="utf-8"><title>photosort</title>
<link rel="stylesheet" href="/ui/style.css"></head><body>
<header><h1>photosort</h1><nav><button data-view="search" class="on">Search</button><button data-view="people">People</button><button data-view="index">Index</button></nav><span id="stats" class="mono"></span></header>
<main>
 <section id="view-search">
  <form id="q"><input name="q" placeholder="balcony, dining table, couple laughing..." autofocus>
   <label>Sharp <input type="range" name="sharp" min="0" max="90" step="5" value="0"><output>0</output>%</label>
   <select name="faces"><option value="">any faces</option><option value="none">no people</option><option value="one">one person</option><option value="two">two</option><option value="group">group (3+)</option></select>
   <select name="person" id="person-select"><option value="">anyone</option></select>
   <button>Find</button></form>
  <div id="grid" class="grid"></div>
  <footer id="selbar" hidden><span id="selcount" class="mono"></span><input id="exportname" placeholder="folder name"><select id="exportmode"><option value="copy">copy to Desktop</option><option value="symlink">links</option><option value="csv">csv</option></select><button id="export">Export</button><button id="clearsel">Clear</button></footer>
 </section>
 <section id="view-people" hidden><div class="row"><button id="cluster">Group faces</button><label>eps <input id="eps" type="number" step="0.05" value="0.5" class="mono"></label><button id="export-people">Export people/groups/solo</button></div><div id="people" class="grid people"></div></section>
 <section id="view-index" hidden><div class="row"><label><input type="checkbox" id="faces" checked> detect faces</label><button id="start-index">Index this folder</button></div><pre id="progress" class="mono"></pre></section>
</main>
<div id="lightbox" hidden><img id="lb-img"><div id="lb-meta" class="mono"></div><button id="lb-like">More like this</button><button id="lb-close">Close</button></div>
<script src="/ui/app.js"></script></body></html>
```

`app.js` responsibilities (write it fully): tab switching; `GET /api/stats` into `#stats`; search form → `GET /api/search` → render `.card` per result with `<img src=/api/thumb/{qhash}?size=grid loading=lazy>`, sharpness % and face count in mono; click = toggle selection (outline in accent), double-click = lightbox with the full thumb, "More like this" = search with `image_id`; selection bar with export → `POST /api/export` then `alert`-free status line showing the path; people view: `GET /api/people` → cards with the cover face cropped via CSS `object-position` from `cover_box` against the full thumb (`size=full`), an inline editable name (`POST /api/people/{id}/name` on blur), click a person = go to Search with `person=<id>`; populate `#person-select`; index view: `POST /api/index` then poll `/api/progress` every 800 ms into `#progress` until `running` is false, then refresh stats. No `alert()`/`confirm()`; use a status line.

- [ ] **Step 5: Add `serve` to cli.py**

```python
def cmd_serve(a):
    import uvicorn, webbrowser, threading
    from .server import create_app
    app = create_app(Path(a.folder))
    if a.open: threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
# main():
    s = sub.add_parser("serve"); s.add_argument("folder"); s.add_argument("--port", type=int, default=7777); s.add_argument("--open", action="store_true"); s.set_defaults(fn=cmd_serve)
```

- [ ] **Step 6: Run tests, then start `photosort serve <tmp folder with a few jpgs> --open` and click through every control once (search, slider, select, export, people, index).** Fix anything that errors. Commit `feat: local server and handmade UI`.

---

### Task 13: PhotoSort.app launcher + README

**Files:**
- Create: `PhotoSort.app/Contents/Info.plist`, `PhotoSort.app/Contents/MacOS/PhotoSort` (chmod +x), `README.md`

- [ ] **Step 1: Launcher script**

```bash
#!/bin/bash
# PhotoSort.app: pick a folder, start the local server, open the browser.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
FOLDER=$(osascript -e 'POSIX path of (choose folder with prompt "Pick the photo folder to sort")' 2>/dev/null)
[ -z "$FOLDER" ] && exit 0
cd "$REPO"
exec "$REPO/.venv/bin/python" -m photosort.cli serve "$FOLDER" --open
```

`Info.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>PhotoSort</string>
<key>CFBundleIdentifier</key><string>in.craywingz.photosort</string>
<key>CFBundleVersion</key><string>0.1.0</string>
<key>CFBundleExecutable</key><string>PhotoSort</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
```

- [ ] **Step 2: README** with: what it does (3 lines), setup (`uv venv`, `uv pip install -e .`, `scripts/fetch_models.sh`), CLI examples for `index`, `find`, `people`, `serve`, `bench`, where the index lives (`~/Library/Application Support/photosort/`, the source drive is never written to), where exports go (`~/Desktop/photosort-out/<shoot>/`, copies by default), the sharpness-on-subject note, and the "tune `--eps` on your own shoot" note. No em dashes.

- [ ] **Step 3: Double-click the app in Finder once, confirm the browser opens on the UI. Commit** `feat: PhotoSort.app launcher and README`.

---

## Self-review

- Spec coverage: index-in-place (T8), decode once (T3/T8), MobileCLIP search (T7/T10), subject sharpness (T4/T8), YuNet+SFace (T5), clustering + person/groups/solo folders (T11), symlink/copy/csv export (T10), local UI (T12), .app (T13), bench (T9), incremental re-run (T8), RAW preview + Sony fallback (T3), RAW+JPEG pairing (T2), no LLM (global). Candid/posed and brief expansion are intentionally out of v1 per the user.
- Type consistency: `Filters` fields match server query params; `qhash` thumb convention is identical in T8, T9, T10 search results, T12; `load_face_embeds` ordering is relied on in T11 and stated there.
- Error rows keep size/mtime so an unreadable file is skipped on re-runs until it changes; `search.Index` only loads `status='ok'` rows.
