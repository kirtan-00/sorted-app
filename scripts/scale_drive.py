"""E1 driver: scan 5,300 synthetic items under SigLIP, killing it twice, and report what scales badly.

Three passes. The first is killed with SIGKILL after 90s, which is the honest shape of the risk: a
power cut or a force quit gives the process no chance to write a clean state. The second is killed
with SIGTERM after 120s, which is a Ctrl-C or a logout. The third runs to completion. Between each,
scan_counts() says what survived, which is the same query the UI runs on every render and therefore
the one whose latency matters.

The point is not the total: it is whether Continue picks up where it stopped, whether throughput sags
as the index grows, and whether anything is worse than linear.
"""
from __future__ import annotations
import json, os, signal, sqlite3, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path("/Users/purohit/Desktop/claude/claude projects/sorted/repo")
PY = REPO / ".venv/bin/python"
ROOT = HERE / "scale"
HOME = HERE / "scale-home"
LOG = HERE / "logs" / "scale.jsonl"
sys.path.insert(0, str(REPO))

PHASES = [(90, signal.SIGKILL, "SIGKILL at 90s (power cut)"),
          (120, signal.SIGTERM, "SIGTERM at 120s (Ctrl-C)"),
          (None, None, "run to completion")]


def counts():
    from photosort import db
    conn = db.connect(ROOT)
    t = time.perf_counter()
    c = db.scan_counts(conn)
    ms = (time.perf_counter() - t) * 1000
    conn.close()
    return c, ms


def dbsize():
    p = Path(os.environ["PHOTOSORT_HOME"])
    tot = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    dbs = [f for f in p.rglob("index.db")]
    return (dbs[0].stat().st_size if dbs else 0), tot


def main():
    os.environ["PHOTOSORT_HOME"] = str(HOME)
    os.environ["PHOTOSORT_CLIP_MODEL"] = "ViT-B-16-SigLIP"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists():
        LOG.unlink()
    print(f"root={ROOT}\nhome={HOME}\n")
    items = sum(1 for _ in ROOT.rglob("*") if _.is_file())
    print(f"shoot: {items} files\n")

    t_all = time.time()
    for i, (kill_after, sig, label) in enumerate(PHASES, 1):
        print(f"--- pass {i}: {label} ---")
        t0 = time.time()
        pr = subprocess.Popen(
            [str(PY), str(HERE / "scale_run.py"), "--root", str(ROOT), "--log", str(LOG), "--workers", "4"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        try:
            out, err = pr.communicate(timeout=kill_after)
            print(f"  finished on its own in {time.time()-t0:.1f}s")
            if out.strip():
                print("  stats:", out.strip()[:300])
            if pr.returncode != 0:
                print("  stderr:", err.strip()[-500:])
        except subprocess.TimeoutExpired:
            os.kill(pr.pid, sig)
            try:
                pr.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.kill(pr.pid, signal.SIGKILL); pr.communicate()
            print(f"  killed with {sig.name} after {time.time()-t0:.1f}s")
        c, ms = counts()
        print(f"  scanned={c['scanned']}/{c['items']} embedded={c['embedded']} "
              f"faced={c['faced']} errors={c['errors']} pending={c['pending']} "
              f"complete={c['complete']}")
        print(f"  scan_counts() {ms:.1f} ms")
        db_b, tot_b = dbsize()
        print(f"  index.db {db_b/1e6:.1f} MB, index total {tot_b/1e6:.1f} MB\n")

    print(f"=== total wall {time.time()-t_all:.1f}s ===")
    c, ms = counts()
    print(json.dumps(c, indent=1))
    # query latency, repeated, because the UI runs this on every render
    ts = []
    for _ in range(10):
        _, m = counts(); ts.append(m)
    ts.sort()
    print(f"scan_counts() over 10 runs: median {ts[5]:.1f} ms, max {ts[-1]:.1f} ms")


if __name__ == "__main__":
    main()
