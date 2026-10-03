#!/usr/bin/env python3
"""Build a synthetic shoot for scale runs, so 5,000 items can be proved without a client's disk.

The scale question is not "how fast is one photo" but "does anything get worse as the index grows":
throughput per stage, resident memory, index.db size, query latency, and whether Continue resumes after
an interrupt. None of those depend much on pixel count, so the default is 3000x2000 (1.1 MB a file)
rather than a 24 MP-class 4000x3000 (2.3 MB). On a Mac with 17 GB free, 5,000 of the former is 5.7 GB
and 5,000 of the latter is 11.4 GB, which is too close to the edge to risk on a boot disk. Decode cost
per file is therefore optimistic against a real shoot; every other number here is not.

The shape copies a real card rather than a flat folder: photos split across day folders, EXIF-less but
with mtime walking forward so date grouping has something to group, bursts of near-identical frames so
the near-copy detector has work, and a scattering of truncated files so the error path is exercised at
scale instead of once in a unit test.
"""
import argparse, os, random, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
EPOCH = datetime(2026, 9, 10, 8, 0, 0)


def _texture(size, seed):
    """The same high-contrast block texture the unit tests use, so the decode path sees familiar data."""
    rng = np.random.default_rng(seed)
    arr = (rng.random((size[1] // 8, size[0] // 8, 3)) * 255).astype("uint8")
    return Image.fromarray(arr).resize(size, Image.NEAREST)


def _write(im, path, when):
    im.save(path, quality=90)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def build(out: Path, photos: int, videos: int, size, days: int, burst_every: int,
          corrupt_every: int, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    per_day = max(1, photos // days)
    made = {"photos": 0, "bursts": 0, "corrupt": 0, "videos": 0, "bytes": 0}
    i = 0
    while i < photos:
        day = min(days, i // per_day + 1)
        d = out / f"day{day}"
        d.mkdir(exist_ok=True)
        when = EPOCH + timedelta(days=day - 1, seconds=7 * (i % per_day))
        p = d / f"IMG_{i:05d}.jpg"
        if corrupt_every and i and i % corrupt_every == 0:
            p.write_bytes(bytes(random.Random(seed + i).getrandbits(8) for _ in range(12)))
            made["corrupt"] += 1
        else:
            _write(_texture(size, seed + i), p, when)
        made["photos"] += 1
        made["bytes"] += p.stat().st_size
        i += 1
        # a burst: three near-copies of the frame just written, two pixels over, three seconds apart
        if burst_every and i % burst_every == 0 and i < photos:
            base = _texture(size, seed + i - 1)
            for k in range(1, 4):
                if i >= photos:
                    break
                q = d / f"IMG_{i:05d}.jpg"
                _write(base.transform(size, Image.AFFINE, (1, 0, -2 * k, 0, 1, 0)),
                       q, when + timedelta(seconds=3 * k))
                made["photos"] += 1
                made["bursts"] += 1
                made["bytes"] += q.stat().st_size
                i += 1
    if videos and FFMPEG:
        vd = out / "day1"
        vd.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="synth-video-"))
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        run = lambda *a: subprocess.run([FFMPEG, "-v", "error", "-y", *a], check=True, capture_output=True)
        a, b, lst = work / "a.mp4", work / "b.mp4", work / "list.txt"
        run("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5", *enc, str(a))
        run("-f", "lavfi", "-i", "color=c=blue:size=320x240:rate=10:duration=5", *enc, str(b))
        lst.write_text(f"file '{a}'\nfile '{b}'\n")
        for v in range(videos):
            dst = vd / f"CLIP_{v:04d}.mp4"
            run("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dst))
            when = EPOCH + timedelta(seconds=11 * v)
            os.utime(dst, (when.timestamp(), when.timestamp()))
            made["videos"] += 1
            made["bytes"] += dst.stat().st_size
        shutil.rmtree(work, ignore_errors=True)
    return made


def main():
    p = argparse.ArgumentParser(description="build a synthetic shoot for scale tests")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--photos", type=int, default=5000)
    p.add_argument("--videos", type=int, default=300)
    p.add_argument("--size", default="3000x2000")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--burst-every", type=int, default=25)
    p.add_argument("--corrupt-every", type=int, default=500)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()

    out = a.out.expanduser().resolve()
    # A real shoot lives on /Volumes. Writing a synthetic set there would put generated files inside
    # someone's card, and the whole point of the read-only rule is that nothing of ours lands on one.
    if str(out).startswith("/Volumes"):
        sys.exit("refusing to write a synthetic set under /Volumes; real shoots live there")
    w, h = (int(x) for x in a.size.lower().split("x"))

    need = a.photos * (w * h * 3 // 16) + a.videos * 200_000
    free = shutil.disk_usage(out.parent if out.parent.exists() else Path.home()).free
    if free < need * 1.4:
        sys.exit(f"refusing: needs about {need/1e9:.1f} GB plus headroom, only {free/1e9:.1f} GB free")

    made = build(out, a.photos, a.videos, (w, h), a.days, a.burst_every, a.corrupt_every, a.seed)
    print(f"out={out}")
    print(f"photos={made['photos']} (bursts={made['bursts']}, corrupt={made['corrupt']}) "
          f"videos={made['videos']} bytes={made['bytes']/1e9:.2f} GB")


if __name__ == "__main__":
    main()
