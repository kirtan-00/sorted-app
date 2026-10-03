"""Faces on clip frames and the shot size they correct, pinned against hand labels of real frames.

The 476 segment frames of NSG_26 Part 2 (the vectors in segments-siglip.npz) were labelled by eye from
contact sheets: close, medium or wide, and how many people are in the frame. segments-faces.npz holds those
labels beside what YuNet found on each frame and the shot size text matrix, built by
scripts/make_face_fixture.py, so this runs in milliseconds with no encoder, no face model and no frames.

The floors sit a little under the measured numbers so ordinary drift passes and a regression does not:
  shot size, embedding alone     292 of 474 labelled frames right (61.6%)
  shot size, refined by faces    365 of 474 (77.0%); the embedding's misses are mostly wide shots it calls
                                 medium, which the face size separates (classify.FACE_MEDIUM_MIN_H)
  people, YuNet count            427 of 476 exact (89.7%); every one of the 95 empty frames called empty
"""
import hashlib, json
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from photosort import classify as C
from photosort.config import CLIP_MODEL

FIX = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(CLIP_MODEL != "ViT-B-16-SigLIP" or not (FIX / "segments-faces.npz").is_file(),
                                reason="the face fixture rides on the SigLIP segment vectors")


@pytest.fixture(scope="module")
def fx():
    with np.load(FIX / "segments-faces.npz", allow_pickle=False) as f, np.load(FIX / "segments-siglip.npz") as s:
        d = {k: f[k] for k in f.files}
        d["M"] = s["M"].astype(np.float32)
    return d


def _embedding_sizes(fx):
    out = C._score(fx["M"], fx["shot_T"].astype(np.float32), fx["shot_owner"], list(fx["shot_names"]))
    return [C.SHOT_SIZE_UNKNOWN if cat == C.FALLBACK else cat for cat, *_ in out]


def _accuracy(pred, labels):
    lab = [i for i, l in enumerate(labels) if l != "unknown"]
    return sum(pred[i] == labels[i] for i in lab) / len(lab), len(lab)


def test_the_shot_prompts_are_the_ones_the_fixture_was_built_with(fx):
    blob = json.dumps({"shot": {k: list(v) for k, v in C.SHOT_SIZES.items()}, "negative": list(C.NEGATIVE_PROMPTS),
                       "temperature": C.TEMPERATURE, "min_prob": C.MIN_PROB, "min_margin": C.MIN_PROB_MARGIN,
                       "min_cosine": C.MIN_COSINE}, sort_keys=True).encode()
    assert hashlib.sha256(blob).hexdigest()[:16] == str(fx["fingerprint"]), \
        "shot prompts or calibration changed: rebuild segments-faces.npz with scripts/make_face_fixture.py"


def test_faces_lift_shot_size_accuracy_on_the_labelled_frames(fx):
    labels = list(fx["label_shot"])
    emb = _embedding_sizes(fx)
    refined = [C.refine_shot_size(s, float(h)) for s, h in zip(emb, fx["face_h"])]
    a_emb, n = _accuracy(emb, labels)
    a_ref, _ = _accuracy(refined, labels)
    print(f"\nshot size over {n} labelled frames: embedding {a_emb:.3f}, refined by faces {a_ref:.3f}")
    assert n == 474
    assert a_emb >= 0.60
    assert a_ref >= 0.75 and a_ref - a_emb >= 0.12
    # where the gain comes from: wide shots stop being filed as medium
    wide = [i for i, l in enumerate(labels) if l == "wide"]
    assert sum(refined[i] == "wide" for i in wide) > 2 * sum(emb[i] == "wide" for i in wide)
    # and it does not buy that by pushing medium shots out to wide: 220 of 264 stay medium against the
    # embedding's 226
    med = [i for i, l in enumerate(labels) if l == "medium"]
    assert sum(refined[i] == "medium" for i in med) >= 0.80 * len(med)


def test_the_medium_threshold_holds_on_the_half_it_was_not_picked_on(fx):
    """Picked on one half of the frames, scored on the other, both ways round."""
    labels = list(fx["label_shot"])
    emb = _embedding_sizes(fx)
    idx = np.arange(len(labels))
    for test in (idx[1::2], idx[::2]):
        pred = {i: C.refine_shot_size(emb[i], float(fx["face_h"][i])) for i in test}
        lab = [i for i in test if labels[i] != "unknown"]
        a_ref = sum(pred[i] == labels[i] for i in lab) / len(lab)
        a_emb = sum(emb[i] == labels[i] for i in lab) / len(lab)
        assert a_ref >= 0.74 and a_ref - a_emb >= 0.12, (a_ref, a_emb)


def test_people_counts_from_faces(fx):
    pred = np.minimum(fx["n_faces"].astype(int), 3)
    truth = fx["label_people"].astype(int)
    exact = float((pred == truth).mean())
    print(f"\npeople per frame: exact {exact:.3f}", dict(Counter(zip(truth.tolist(), pred.tolist()))))
    assert exact >= 0.88
    assert not pred[truth == 0].any()                     # nobody in the frame is never called somebody
    assert ((pred > 0) == (truth > 0)).mean() >= 0.94      # somebody or nobody


def test_refine_shot_size_rules():
    r = C.refine_shot_size
    assert r("medium", None) == "medium" and r("unknown", 0.0) == "unknown"     # no face: the embedding stands
    assert r("close", 0.05) == "close"                                           # a close up keeps its call
    assert r("medium", 0.08) == "wide" and r("unknown", 0.08) == "wide"
    assert r("wide", 0.2) == "medium" and r("unknown", C.FACE_MEDIUM_MIN_H) == "medium"
    assert r("medium", 0.5) == "close"
