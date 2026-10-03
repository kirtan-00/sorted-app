"""The embed stage on video-heavy shoots: frames are batched across clips, a segment reuses the vector its
frame already got, and the time-left estimate keeps one rate per sub-stage."""
import math, time
import numpy as np
import pytest
from PIL import Image
from photosort import db
from photosort.config import EMBED_BATCH
from photosort.index import index_folder, embed_eta


def _clip_frames(idx, qh):
    return sorted((idx / "frames").glob(f"{qh}_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))


@pytest.fixture
def shoot(tmp_path, tmp_path_factory):
    from conftest import make_video, needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    work = tmp_path_factory.mktemp("work")
    for i, scenes in enumerate((2, 1, 2, 1, 2, 2)):
        make_video(tmp_path / f"c{i}.mp4", scenes=scenes, work=work / str(i))
    index_folder(tmp_path, faces=False, workers=2, embed=False)
    return tmp_path


@pytest.fixture
def spy(monkeypatch):
    """Every encode_images call: (time it started, number of images). The real encoder still runs."""
    from photosort.embed import Embedder
    real = Embedder.encode_images
    calls = []
    def wrapped(self, ims):
        calls.append((time.time(), len(ims)))
        return real(self, ims)
    monkeypatch.setattr(Embedder, "encode_images", wrapped)
    return calls


def test_clip_frames_are_batched_across_clips_and_match_the_one_clip_at_a_time_vectors(shoot, spy):
    """Six clips of six to eight frames used to be six encodes of six to eight; now they share EMBED_BATCH-sized
    encodes. Each clip's vector is still the renormalised mean of its own frames, and a segment's vector is
    the vector its midpoint frame got in the same pass, so no frame is encoded twice."""
    from photosort.embed import get_embedder
    seen = []
    index_folder(shoot, faces=False, workers=1, embed=True, progress=seen.append)
    conn = db.connect(shoot); idx = db.index_dir(shoot)
    clips = conn.execute("SELECT id, qhash FROM photos WHERE kind='video' ORDER BY id").fetchall()
    n_frames = sum(len(_clip_frames(idx, qh)) for _, qh in clips)
    assert n_frames > EMBED_BATCH          # the clips cannot all fit one batch, so a clip straddles two
    sizes = [n for _, n in spy]
    assert all(n <= EMBED_BATCH for n in sizes)
    assert sum(sizes) == n_frames           # every frame once, segments included
    assert len(sizes) == math.ceil(n_frames / EMBED_BATCH)
    calls_done = list(spy)

    # the reference is the old path: one encode per clip, mean, renormalise
    E = get_embedder()
    ids, M = db.load_embeds(conn); got = dict(zip(ids.tolist(), M))
    for pid, qh in clips:
        ref = E.encode_images([Image.open(p) for p in _clip_frames(idx, qh)]).mean(axis=0)
        ref /= (np.linalg.norm(ref) + 1e-9)
        assert np.allclose(got[pid], ref, atol=2e-3), pid
        assert float(got[pid] @ ref) > 0.9999
    sids, S = db.load_segment_embeds(conn); seg = dict(zip(sids.tolist(), S))
    rows = conn.execute("SELECT id, frame FROM segments").fetchall()
    assert len(rows) == 10 and set(seg) == {r[0] for r in rows}
    for sid, frame in rows:
        ref = E.encode_images([Image.open(idx / "frames" / frame)])[0]
        assert np.allclose(seg[sid], ref, atol=2e-3) and float(seg[sid] @ ref) > 0.9999

    emb = [d for d in seen if d["stage"] == "embed"]
    clip_emits = [d for d in emb if d["substage"] == "clips"]
    # one report per batch, carrying every clip that batch finished
    counts = [d["substage_done"] for d in clip_emits]
    assert len(clip_emits) == len(sizes) and counts == sorted(set(counts)) and counts[-1] == 6
    assert all(d["substage_total"] == 6 for d in clip_emits)
    # the sub-stage clock starts before its first encode, not at its first report
    assert clip_emits[0]["substage_started"] <= calls_done[0][0]
    assert [d["done"] for d in emb][-1] == emb[-1]["total"] == 6 + 10


def test_segments_whose_clip_is_already_embedded_are_encoded_from_their_frame(shoot):
    """A pass cut short between the clips and the segments leaves clips embedded and segments not. The next
    pass encodes just the segments, from their own frame files, and leaves the clip vectors alone."""
    index_folder(shoot, faces=False, workers=1, embed=True)
    conn = db.connect(shoot)
    _, before = db.load_segment_embeds(conn)
    _, clips_before = db.load_embeds(conn)
    conn.execute("UPDATE segments SET embed=NULL"); conn.commit()
    seen = []
    index_folder(shoot, faces=False, workers=1, embed=True, progress=seen.append)
    _, after = db.load_segment_embeds(conn)
    _, clips_after = db.load_embeds(conn)
    assert after.shape == before.shape and np.allclose(after, before, atol=2e-3)
    assert np.array_equal(clips_after, clips_before)
    assert [d["substage"] for d in seen if d["stage"] == "embed"] == ["segments"]


# ---- the estimate --------------------------------------------------------------------------------------

def test_eta_uses_the_rate_of_the_sub_stage_that_is_running():
    """100 photos took 10 s (10/s). 20 clips of 8 frames are next. The photo rate says 2 s for the clips;
    a frame rate (10 frames/s) says 16 s, which is what they will take."""
    subs = [("photos", 100, 100), ("clips", 20, 160), ("segments", 30, 0)]
    rate, eta = embed_eta(subs, "photos", done=50, started=0.0, now=5.0)
    assert rate == pytest.approx(10.0)
    assert eta == pytest.approx(5.0 + 16.0)
    # in the clips now, at 2 clips/s: the photo rate no longer has a say
    rate, eta = embed_eta(subs, "clips", done=4, started=100.0, now=102.0)
    assert rate == pytest.approx(2.0)
    assert eta == pytest.approx(8.0)
    # segments that reuse their frame's vector cost no encode, so the estimate adds nothing for them
    rate, eta = embed_eta(subs, "segments", done=10, started=200.0, now=201.0)
    assert rate == pytest.approx(10.0) and eta == pytest.approx(2.0)


def test_eta_is_unknown_until_the_sub_stage_has_done_something():
    subs = [("photos", 10, 10), ("clips", 5, 40)]
    assert embed_eta(subs, "clips", done=0, started=0.0, now=3.0) == (None, None)
    assert embed_eta(subs, "clips", done=2, started=3.0, now=3.0) == (None, None)
    assert embed_eta(subs, "nope", done=2, started=0.0, now=3.0) == (None, None)


def test_eta_rides_on_the_embed_progress(shoot):
    seen = []
    index_folder(shoot, faces=False, workers=1, embed=True, progress=seen.append)
    emb = [d for d in seen if d["stage"] == "embed"]
    assert all(isinstance(d["eta_s"], float) and d["eta_s"] >= 0 and d["rate"] > 0 for d in emb)
    assert emb[-1]["eta_s"] == 0.0
    assert all(d["eta_s"] is None and d["rate"] is None for d in seen if d["stage"] != "embed")
