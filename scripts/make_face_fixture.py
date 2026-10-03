"""Build tests/fixtures/segments-faces.npz, the pin for clip faces and face-informed shot size.

Run once, after make_segment_fixture.py, under ViT-B-16-SigLIP. For the same 476 segments of NSG_26 Part 2
(same order as segments-siglip.npz) it freezes:
  face_h       the largest face's height over the frame's short edge (as index.clip_face_summary measures it; the height on these landscape frames), 0.0 when no face
  n_faces      how many faces YuNet found on that frame
  shot_T       the shot size text matrix (SHOT_SIZES plus NEGATIVE_PROMPTS), so the test needs no encoder
  shot_owner   which prompt belongs to which shot size, and shot_names the names
  label_shot   the hand label of each frame: close, medium, wide, or unknown (two frames of bokeh and sand)
  label_people the hand count of people in each frame, 3 meaning three or more
  fingerprint  the shot prompts and the calibration, so a prompt edit fails the test instead of drifting

The labels file is one line per segment, in fixture order: "<k> <c|m|w|u> <0|1|2|3>". It was written by
looking at contact sheets of the 476 frames, 30 to a sheet; a face is counted when a person is clearly in
the frame, so a crowd far behind the subject is "3" and a back of a head is a person.
"""
from __future__ import annotations
import hashlib, json, sqlite3, sys
from pathlib import Path
from urllib.request import pathname2url

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
FRAMES = Path.home() / "Desktop/photosort-out/NSG_26_Part 2.photosort-index/frames"
INDEX = Path.home() / "Desktop/photosort-out/NSG_26_Part 2.photosort-index/index.db"
DEST = REPO / "tests" / "fixtures"

import numpy as np
from PIL import Image
from photosort import classify as C
from photosort.config import CLIP_MODEL
from photosort.embed import get_embedder
from photosort.faces import FaceEngine

LABEL = {"c": "close", "m": "medium", "w": "wide", "u": "unknown"}


def fingerprint() -> str:
    blob = json.dumps({"shot": {k: list(v) for k, v in C.SHOT_SIZES.items()}, "negative": list(C.NEGATIVE_PROMPTS),
                       "temperature": C.TEMPERATURE, "min_prob": C.MIN_PROB, "min_margin": C.MIN_PROB_MARGIN,
                       "min_cosine": C.MIN_COSINE}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def main(labels_path: str):
    assert CLIP_MODEL == "ViT-B-16-SigLIP", "the face fixture rides on the SigLIP segment vectors"
    seg = np.load(DEST / "segments-siglip.npz")
    ids = [int(i) for i in seg["seg_ids"]]
    conn = sqlite3.connect("file:" + pathname2url(str(INDEX)) + "?mode=ro", uri=True)
    frame = {r[0]: r[1] for r in conn.execute("SELECT id, frame FROM segments")}
    lines = [l.split() for l in open(labels_path) if l.strip() and not l.startswith("#")]
    assert [int(l[0]) for l in lines] == list(range(len(ids))), "one label per segment, in fixture order"

    eng = FaceEngine()
    face_h, n_faces = [], []
    for sid in ids:
        im = Image.open(FRAMES / frame[sid]).convert("RGB")
        faces = eng.detect(im)
        n_faces.append(len(faces))
        face_h.append(max((f.h / min(im.size) for f in faces), default=0.0))
    names, T, owner = C._shot_matrix(get_embedder())
    out = DEST / "segments-faces.npz"
    np.savez_compressed(out, face_h=np.array(face_h, np.float32), n_faces=np.array(n_faces, np.int16),
                        shot_T=np.asarray(T, np.float16), shot_owner=np.asarray(owner, np.int16),
                        shot_names=np.array(names), label_shot=np.array([LABEL[l[1]] for l in lines]),
                        label_people=np.array([int(l[2]) for l in lines], np.int8),
                        fingerprint=np.array(fingerprint()))
    print(f"{out.name} {out.stat().st_size / 1e3:.0f} KB, {sum(n > 0 for n in n_faces)} of {len(ids)} frames with a face")


if __name__ == "__main__":
    main(sys.argv[1])
