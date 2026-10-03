"""Build the stored-vector fixture that tests/test_segments.py pins segment categorisation with.

Run once per encoder. Freezes, for the 476 segments of NSG_26 Part 2:
  M      the segment image vectors (one representative frame each, as index.py embeds them)
  T      the prompt text matrix, so the test scores without loading an encoder
  owner  which prompt belongs to which category
  names  the category names
  cats   the category each segment is expected to come out as

The text matrix is frozen alongside the image vectors so the test is hermetic and fast. That alone
would not notice a prompt edit, so the fixture also carries a fingerprint of the prompt lists and the
calibration; the test asserts those still match, which is what turns a silent drift into a failure.
"""
from __future__ import annotations
import hashlib, json, sqlite3, sys
from pathlib import Path
from urllib.request import pathname2url

REPO = Path("/Users/purohit/Desktop/claude/claude projects/sorted/repo")
sys.path.insert(0, str(REPO))
FRAMES = Path("/Users/purohit/Desktop/photosort-out/NSG_26_Part 2.photosort-index/frames")
# the self-contained export bundle, not the app's index folder: the Application Support copy was
# deleted on 2026-09-24 and this bundle carries the same 955 photos and 476 segments, verified identical
INDEX = Path.home() / "Desktop/photosort-out/NSG_26_Part 2.photosort-index/index.db"
DEST = REPO / "tests" / "fixtures"

import numpy as np
from PIL import Image
from photosort import classify as C
from photosort.config import CLIP_MODEL
from photosort.embed import get_embedder

TAG = {"MobileCLIP-S1": "mobileclip", "ViT-B-16-SigLIP": "siglip"}[CLIP_MODEL]


def fingerprint() -> str:
    """Everything the scoring depends on besides the vectors themselves."""
    blob = json.dumps({
        "prompts": {k: list(v) for k, v in C.PROMPTS.items()},
        "negative": list(C.NEGATIVE_PROMPTS),
        "family": dict(C.CATEGORY_FAMILY),
        "temperature": C.TEMPERATURE, "min_prob": C.MIN_PROB,
        "min_margin": C.MIN_PROB_MARGIN, "min_cosine": C.MIN_COSINE,
        "fallback": C.FALLBACK,
    }, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def main():
    conn = sqlite3.connect("file:" + pathname2url(str(INDEX)) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [r for r in conn.execute(
        "SELECT id, frame FROM segments ORDER BY id") if r["frame"] and (FRAMES / r["frame"]).is_file()]

    E = get_embedder()
    vecs = []
    for i in range(0, len(rows), 64):
        vecs.append(E.encode_images([Image.open(FRAMES / r["frame"]) for r in rows[i:i + 64]]))
    M = np.vstack(vecs).astype(np.float32)

    names, T, owner = C._prompt_matrix(E)
    owner = np.array(owner)
    cats = [s[0] for s in C._score(M, T, owner, names)]

    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"segments-{TAG}.npz"
    np.savez_compressed(
        out,
        M=M.astype(np.float16), T=np.asarray(T, dtype=np.float16), owner=owner.astype(np.int16),
        names=np.array(names), cats=np.array(cats),
        seg_ids=np.array([r["id"] for r in rows], dtype=np.int32),
        fingerprint=np.array(fingerprint()),
    )
    from collections import Counter
    print(f"{TAG}: {out.name} {out.stat().st_size/1e3:.0f} KB  n={len(rows)}  fp={fingerprint()}")
    print("  ", Counter(cats).most_common(6))


if __name__ == "__main__":
    main()
