import os, shutil, subprocess, tempfile
from pathlib import Path
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

@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("PHOTOSORT_HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("PHOTOSORT_EXPORT_DIR", str(tmp_path_factory.mktemp("out")))

FFMPEG = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

def make_video(dst, scenes=2, work=None):
    """A small h264 mp4 at dst: scenes=1 is 3 s of testsrc; scenes=2 is 5 s of testsrc followed by 5 s of
    plain blue (one hard cut at 5.0 s), built with the concat demuxer. Intermediates go in `work`
    (default: a temp dir next to nowhere in the shoot) so the shoot folder only ever gains dst."""
    dst = Path(dst)
    work = Path(work or tempfile.mkdtemp(prefix="photosort-video-")); work.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run([FFMPEG, "-v", "error", "-y", *a], check=True, capture_output=True)
    if scenes == 1:
        run("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3,format=yuv420p", str(dst))
        return dst
    a = work / "a.mp4"; b = work / "b.mp4"; lst = work / "list.txt"
    run("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5,format=yuv420p", str(a))
    run("-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=10:duration=5,format=yuv420p", str(b))
    lst.write_text(f"file '{a}'\nfile '{b}'\n")
    run("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dst))
    return dst
