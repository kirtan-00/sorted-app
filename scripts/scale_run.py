"""E1: one scan pass over the synthetic shoot, logging enough to answer "does it get worse as it grows".

Run as a subprocess so the driver can kill it mid-flight; that is the whole point of the exercise. The
previous attempt on NSG_26 Part 1 left 2,814 of 4,873 rows unembedded with no record of why, because
that index predates the jobs table. A clean uninterrupted pass would prove less than a resumed one.

Writes JSONL to --log, one line per progress notification, each stamped with elapsed seconds and the
resident memory of this process and its workers. The driver reads it back to find per-stage wall time,
throughput over the life of the run, and peak memory.
"""
from __future__ import annotations
import argparse, json, os, resource, sys, time
from pathlib import Path

REPO = Path("/Users/purohit/Desktop/claude/claude projects/sorted/repo")
sys.path.insert(0, str(REPO))


def rss_mb() -> tuple[float, float]:
    """(this process, sum of reaped children) in MB. ru_maxrss is bytes on macOS, kB on Linux."""
    div = 1 << 20 if sys.platform == "darwin" else 1 << 10
    return (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / div,
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / div)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--no-faces", action="store_true")
    a = p.parse_args()

    from photosort.index import index_folder
    from photosort.config import CLIP_MODEL

    t0 = time.time()
    fh = a.log.open("a", buffering=1)
    fh.write(json.dumps({"event": "start", "t": 0.0, "pid": os.getpid(),
                         "encoder": CLIP_MODEL, "wall": time.time()}) + "\n")

    last = {}

    def notify(d: dict):
        # one line per notification is too many for 5,000 items; keep every 25th per stage plus the
        # first and any stage change, which is enough to see a throughput curve
        st, done = d.get("stage"), d.get("done", 0)
        prev = last.get(st)
        if prev is None or done - prev >= 25 or done == d.get("total"):
            last[st] = done
            me, kids = rss_mb()
            fh.write(json.dumps({"event": "progress", "t": round(time.time() - t0, 3),
                                 "stage": st, "done": done, "total": d.get("total"),
                                 "rss_mb": round(me, 1), "kids_mb": round(kids, 1)}) + "\n")

    try:
        stats = index_folder(a.root, faces=not a.no_faces, workers=a.workers, progress=notify)
        me, kids = rss_mb()
        fh.write(json.dumps({"event": "done", "t": round(time.time() - t0, 3),
                             "stats": stats, "rss_mb": round(me, 1), "kids_mb": round(kids, 1)}) + "\n")
        print(json.dumps(stats))
    except BaseException as e:                       # SIGTERM lands here too, and should be recorded
        fh.write(json.dumps({"event": "aborted", "t": round(time.time() - t0, 3),
                             "error": f"{type(e).__name__}: {e}"}) + "\n")
        raise


if __name__ == "__main__":
    main()
