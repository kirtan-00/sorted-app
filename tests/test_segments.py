"""Segment categorisation, pinned against stored vectors from a real documentary shoot.

Why this file exists. 900d347 added four interview prompts for SigLIP and measured them over 955
STILLS. On a still the question barely arises: _classify_all forces any photo with a face to "people",
so the interview-versus-people softmax is overruled before it matters. Segments carry no faces and the
face rule never sees them, so for a clip that softmax is the whole decision. NSG_26 Part 1, the next
disk to be scanned, is 579 clips and 609 segments. The production case that matters most was the one
with no test.

The fixtures hold the 476 segments of NSG_26 Part 2: one vector per segment, taken from the single
representative frame in segments.frame, which is how index.py embeds a segment (it averages frames for
the CLIP row, not for a segment). The prompt matrix is frozen beside them so this runs in milliseconds
with no encoder and no model download.

Measured on those 476 with the shipped prompts: SigLIP keeps 165 of MobileCLIP's 176 interview
segments (93.8%) and the two agree on 402 of 476. Before the four prompts it was 133 and 370. The
residual disagreement is dominated by two men walking a beach, which MobileCLIP files under people and
SigLIP under beach - a scene-versus-subject split, not an interview failure.
"""
import hashlib, json
from pathlib import Path

import numpy as np
import pytest

from photosort import classify as C
from photosort.config import CLIP_MODEL

TAG = {"MobileCLIP-S1": "mobileclip", "ViT-B-16-SigLIP": "siglip"}
FIXTURE = Path(__file__).parent / "fixtures" / f"segments-{TAG.get(CLIP_MODEL, '?')}.npz"

pytestmark = pytest.mark.skipif(not FIXTURE.is_file(), reason=f"no stored segment vectors for {CLIP_MODEL}")


@pytest.fixture(scope="module")
def stored():
    with np.load(FIXTURE, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def _fingerprint() -> str:
    """Must match make_segment_fixture.py. Everything the scoring depends on but the vectors."""
    blob = json.dumps({
        "prompts": {k: list(v) for k, v in C.PROMPTS.items()},
        "negative": list(C.NEGATIVE_PROMPTS),
        "family": dict(C.CATEGORY_FAMILY),
        "temperature": C.TEMPERATURE, "min_prob": C.MIN_PROB,
        "min_margin": C.MIN_PROB_MARGIN, "min_cosine": C.MIN_COSINE,
        "fallback": C.FALLBACK,
    }, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def test_prompts_and_calibration_are_unchanged(stored):
    """The frozen text matrix cannot notice a prompt edit, so the fingerprint does it instead.

    A deliberate change means rebuilding the fixture with make_segment_fixture.py and reading the new
    interview numbers before accepting them; that is the point of the gate, not an obstacle to it."""
    assert _fingerprint() == str(stored["fingerprint"]), (
        f"prompts or calibration changed for {CLIP_MODEL}; rebuild tests/fixtures/{FIXTURE.name} "
        "and check the interview counts below still hold")


def test_segment_categories_match_the_stored_run(stored):
    """The whole pin: same vectors, same prompts, same thresholds, same answers."""
    got = [s[0] for s in C._score(stored["M"].astype(np.float32), stored["T"].astype(np.float32),
                                  stored["owner"], list(stored["names"]))]
    want = list(stored["cats"])
    bad = [(int(stored["seg_ids"][i]), want[i], got[i]) for i in range(len(want)) if want[i] != got[i]]
    assert not bad, f"{len(bad)} of {len(want)} segments changed category, first 10: {bad[:10]}"


def test_interview_segments_do_not_regress(stored):
    """The number the prompt work exists to protect.

    SigLIP files 167 of the 476 as interview and MobileCLIP 176; before the four added prompts SigLIP
    managed 135. The floor is set below the measured value, not at it, so ordinary drift does not fail
    the suite but a collapse back toward the three-prompt behaviour does."""
    floor = {"ViT-B-16-SigLIP": 160, "MobileCLIP-S1": 170}[CLIP_MODEL]
    n = sum(1 for c in stored["cats"] if c == "interview")
    assert n >= floor, f"{CLIP_MODEL} files {n} of 476 segments as interview, below the floor of {floor}"


def test_secondary_categories_on_the_stored_run(stored):
    """Every category a frame fits, measured on the same 476 (classify.SECOND_MIN_PROB says why 0.15).

    Pinned exactly, because the rule is deterministic over frozen vectors: a change to the floor or to
    also_of shows up here as a number to read, not as drift. "other" never grows and never gains a tag.
    The precision check is the one with hand labels behind it (segments-faces.npz, label_people): a scene
    frame that also gets people or interview must nearly always have a person in it."""
    names = list(stored["names"])
    out = C._score(stored["M"].astype(np.float32), stored["T"].astype(np.float32), stored["owner"], names)
    also = [C.also_of(o[0], o[3], o[6]) for o in out]
    want = {"ViT-B-16-SigLIP": (68, 78, 18), "MobileCLIP-S1": (118, 125, 19)}[CLIP_MODEL]
    got = (sum(1 for a in also if a), sum(len(a) for a in also), sum(1 for o in out if o[0] == C.FALLBACK))
    assert got == want, f"{CLIP_MODEL}: (segments gaining a category, tags, other) = {got}, measured {want}"
    assert not any(a for o, a in zip(out, also) if o[0] == C.FALLBACK)
    faces = Path(__file__).parent / "fixtures" / "segments-faces.npz"
    if faces.is_file():
        with np.load(faces, allow_pickle=False) as f:
            people = f["label_people"]
        hits = [k for k, o in enumerate(out) if o[0] not in ("people", "interview", C.FALLBACK)
                and {"people", "interview"} & set(also[k])]
        assert len(hits) >= 15 and sum(people[k] > 0 for k in hits) / len(hits) >= 0.9


def test_no_face_rule_or_duration_rule_reaches_a_segment():
    """Segments are the bare softmax, which is why the prompts carry all the weight here.

    _classify_all applies the long-interview rule and the face rule inside the photos loop; the segment
    loop takes (category, score) straight from _score. Only 10 of NSG_26 Part 2's 325 clips reach
    INTERVIEW_MIN_DURATION_S, and even for those the rule rewrites the clip row, never its segments. If
    that ever changes, this test should be the thing that says so."""
    import inspect
    src = inspect.getsource(C._classify_all)
    seg_loop = src[src.index("for k, sid in enumerate(seg_ids"):]
    assert "INTERVIEW_MIN_DURATION_S" not in seg_loop
    assert "n_faces" not in seg_loop
