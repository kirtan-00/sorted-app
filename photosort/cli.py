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
    s = index_folder(Path(a.folder), faces=not a.no_faces, workers=a.workers, progress=_progress, retry_errors=a.retry_errors)
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

def cmd_find(a):
    from .search import Index, Filters
    from .export import export_ids
    ix = Index(Path(a.folder))
    f = Filters(sharp_min_pct=a.sharp, faces=a.faces)
    res = ix.search(text=a.query, filters=f, limit=a.limit)
    for r in res: print(f"{r['score']:.3f}  {r['sharp_pct']:5.1f}%  {'?' if r['n_faces'] is None else r['n_faces']}f  {r['rel']}")
    if a.out:
        print("exported to", export_ids(Path(a.folder), [r["id"] for r in res], a.out, a.mode))

def cmd_people(a):
    from .people import cluster_faces, export_people, suggest_merges
    people = cluster_faces(Path(a.folder), eps=a.eps)
    for p in people: print(f"person_{p['id']:02d}  {p['n']} photos")
    n = len(suggest_merges(Path(a.folder), limit=10**6))
    print(f"{len(people)} groups, {n} same-person suggestion{'s' if n != 1 else ''} (answer them in the People tab)")
    if a.export: print("exported to", export_people(Path(a.folder), a.mode))

def free_port(start: int, tries: int = 10, host: str = "127.0.0.1") -> int:
    """First port in [start, start+tries) that binds; a stale server may still hold the default."""
    import socket
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"no free port in {start}-{start + tries - 1}")

def cmd_classify(a):
    from .classify import classify, classify_and_store, discover_and_store, write_manifest, apply_on_disk, undo_on_disk
    from collections import Counter
    root = Path(a.folder)
    if a.undo:
        print("restored", undo_on_disk(Path(a.undo)), "files"); return
    classify_and_store(root)          # persists category + score in the index (UI reads these)
    res = classify(root)
    for cat, n in sorted(Counter(r["category"] for r in res).items(), key=lambda x: -x[1]):
        print(f"{cat:>14}  {n}")
    for name, n in discover_and_store(root).items():   # the discovered bar, same pass as the app
        print(f"{'discovered: ' + name:>28}  {n}")
    print("manifest + symlink folders:", write_manifest(root, res))
    if a.apply_on_disk:
        print("MOVING files on the disk into _sorted/ ...")
        print("moved into", apply_on_disk(root, res, dry_run=False), "; undo with --undo <export>/undo.csv")
    elif a.plan_on_disk:
        print("dry-run move plan:", apply_on_disk(root, res, dry_run=True))

def cmd_reorganise(a):
    """Plan only unless --apply. The single command that writes under the shoot root: every guard in
    reorganise.plan must pass first, and a guard failure prints its message and exits 2."""
    from . import reorganise
    root = Path(a.folder)
    def prog(d):
        sys.stderr.write(f"\r{d['done']}/{d['total']}  failed {d['failed']}   "); sys.stderr.flush()
    try:
        if a.undo:
            out = reorganise.undo(root, progress=prog); sys.stderr.write("\n")
            print(f"restored {out['restored']}  failed {out['failed']}"); return
        plan = reorganise.plan(root, by_people=a.by_people)
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr); sys.exit(2)
    print(f"plan: {plan['moves']} files into {plan['folders']} folders under {root / reorganise.SORTED_DIR}  "
          f"(photos {plan['photos']}, videos {plan['videos']}, drone {plan['drone']}, collisions {plan['collisions']})")
    for name, n in sorted(plan["people"].items()):
        print(f"  people/{name}: {n}")
    for m in plan["sample"]:
        print(f"  {m['src_rel']}  ->  {m['dst_rel']}  [{m['reason']}]")
    if plan["moves"] > len(plan["sample"]):
        print(f"  ... and {plan['moves'] - len(plan['sample'])} more")
    if not a.apply:
        print("nothing moved (add --apply to move the files; undo with --undo)"); return
    try:
        out = reorganise.apply(root, plan["plan_id"], progress=prog); sys.stderr.write("\n")
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr); sys.exit(2)
    print(f"moved {out['done'] - out['failed']} of {out['total']}  failed {out['failed']}  into {out['path']}")
    for f in out["failures"]:
        print("  failed:", f.replace("\t", "  "))

def cmd_drive_signin(a):
    """Open the browser on Google's consent screen and keep the token under app_home()."""
    from . import drive
    try:
        print("signed in as", drive.sign_in()["email"])
    except (drive.NoClientConfig, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr); sys.exit(2)

def cmd_drive_export(a):
    """Upload ticked categories, saved people or listed ids into a Drive folder, through the same
    planners the app uses. Prints one line per failure and the folder link at the end."""
    from . import drive
    from .export import category_jobs, folder_jobs, ids_jobs, jobs_bytes
    from .people import export_references_ids
    root = Path(a.folder)
    def prog(d):
        sys.stderr.write(f"\r{d['done']}/{d['total']}  skipped {d['skipped']}  failed {d['failed']}   "); sys.stderr.flush()
    try:
        folder_id = drive.parse_folder_link(a.link)
        if not drive.is_signed_in():
            raise drive.NotSignedIn()
        if a.categories is not None:
            planned, _ = category_jobs(root, a.categories or None, a.include_raw, a.discovered, a.include_unsure, "clips", a.drone, a.hide_bad)
            jobs = [(pid, rel, "categories/" + sub) for pid, rel, sub in planned]
        elif a.people:
            folders = export_references_ids(root, a.names or None)
            jobs = [(pid, rel, "people/" + sub) for pid, rel, sub in folder_jobs(root, folders, a.include_raw)]
        else:
            jobs = [(pid, rel, a.name) for pid, rel, _ in ids_jobs(root, a.ids, a.include_raw)]
        msg = drive.preflight(folder_id, jobs_bytes(root, jobs))
        if msg:
            raise ValueError(msg)
        res = drive.upload_files(root, folder_id, jobs, web_size=a.web_size, skip_videos=a.skip_videos, progress=prog)
    except (drive.NotSignedIn, drive.NoClientConfig, ValueError) as e:
        sys.stderr.write("\n"); print(f"refused: {e}", file=sys.stderr); sys.exit(2)
    sys.stderr.write("\n")
    print(f"uploaded {res['done'] - res['failed'] - res['skipped']} of {res['total']}  skipped {res['skipped']}  failed {res['failed']}  "
          f"{res['bytes'] / 1e6:.1f} MB  into {drive.folder_link(folder_id)}")
    for f in res["failures"]:
        print("  failed:", f.replace("\t", "  "))

def cmd_serve(a):
    import uvicorn, webbrowser, threading
    from .server import create_app
    folder = Path(a.folder) if a.folder else None
    app = create_app(folder)
    port = free_port(a.port)
    if port != a.port:
        sys.stderr.write(f"port {a.port} busy, using {port}\n")
    if a.open:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    where = str(folder) if folder else "(no folder open, pick one in the app)"
    print(f"photosort serving {where} at http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

def main(argv=None):
    p = argparse.ArgumentParser(prog="photosort")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("index"); s.add_argument("folder"); s.add_argument("--no-faces", action="store_true"); s.add_argument("--workers", type=int)
    s.add_argument("--retry-errors", action="store_true", help="re-process photos that failed last time"); s.set_defaults(fn=cmd_index)
    s = sub.add_parser("bench"); s.add_argument("folder"); s.add_argument("--n", type=int, default=200); s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("find"); s.add_argument("folder"); s.add_argument("query", nargs="?")
    s.add_argument("--sharp", type=float, help="min sharpness percentile 0-100"); s.add_argument("--faces", choices=["none","one","two","group"])
    s.add_argument("--limit", type=int, default=50); s.add_argument("--out", help="export folder name (created under ~/Desktop/photosort-out/<shoot>/)"); s.add_argument("--mode", default="copy", choices=["copy","symlink","csv"])
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("people"); s.add_argument("folder"); s.add_argument("--eps", type=float, default=None, help="DBSCAN cosine distance (default: FACE_CLUSTER_EPS)")
    s.add_argument("--export", action="store_true"); s.add_argument("--mode", default="copy", choices=["copy","symlink"]); s.set_defaults(fn=cmd_people)
    s = sub.add_parser("classify"); s.add_argument("folder")
    s.add_argument("--plan-on-disk", action="store_true", help="write move-plan.csv only, touch nothing")
    s.add_argument("--apply-on-disk", action="store_true", help="MOVE files into <folder>/_sorted/<category>/ (same volume, undo.csv written first)")
    s.add_argument("--undo", help="path to undo.csv from a previous --apply-on-disk"); s.set_defaults(fn=cmd_classify)
    s = sub.add_parser("reorganise", help="sort the shoot folder itself into <folder>/sorted/ (plan only unless --apply)")
    s.add_argument("folder"); s.add_argument("--by-people", action="store_true", help="named people get photos/people/<Name>/ folders")
    s.add_argument("--apply", action="store_true", help="MOVE the files (same disk renames, UNDO.json written first)")
    s.add_argument("--undo", action="store_true", help="put every file from <folder>/sorted/UNDO.json back"); s.set_defaults(fn=cmd_reorganise)
    s = sub.add_parser("drive-signin", help="sign in to Google Drive in the browser (needs google_client.json, see docs/google-drive.md)")
    s.set_defaults(fn=cmd_drive_signin)
    s = sub.add_parser("drive-export", help="upload an export into a Google Drive folder")
    s.add_argument("folder"); s.add_argument("--link", required=True, help="the Drive folder link (or its id)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--categories", nargs="*", metavar="NAME", help="ticked categories (none listed = every classified one)")
    g.add_argument("--people", action="store_true", help="saved people, one folder each (see --names)")
    g.add_argument("--ids", nargs="+", type=int, help="photo ids, flat, into --name")
    s.add_argument("--discovered", nargs="*", metavar="NAME", default=None); s.add_argument("--include-unsure", action="store_true")
    s.add_argument("--drone", action="store_true"); s.add_argument("--hide-bad", action="store_true")
    s.add_argument("--names", nargs="*", metavar="NAME", default=None, help="with --people: only these saved names")
    s.add_argument("--name", default="selection", help="with --ids: the subfolder in the Drive folder")
    s.add_argument("--include-raw", action="store_true"); s.add_argument("--web-size", type=int, default=None, help="long edge in px for photos")
    s.add_argument("--skip-videos", action="store_true", help="with --web-size: leave videos out"); s.set_defaults(fn=cmd_drive_export)
    s = sub.add_parser("serve"); s.add_argument("folder", nargs="?", help="photo folder; omit to open the picker in the app")
    s.add_argument("--port", type=int, default=7777); s.add_argument("--open", action="store_true"); s.set_defaults(fn=cmd_serve)
    a = p.parse_args(argv); a.fn(a)

if __name__ == "__main__":
    main()
