"""The on-demand focus pass: focus.check_focus labels every ok row ok / soft / bad from the sharpness the
index already measured (photos) or from the stored sample frames (videos). Nothing here decodes a source
photo; the shoot folder is never written."""
import os, shutil, subprocess
import numpy as np
import pytest
from PIL import Image, ImageFilter
from photosort import db, focus
from photosort.index import index_folder
from photosort.config import FOCUS_BAD_ABS, FOCUS_BAD_MAX_ABS


def _labels(root):
    conn = db.connect(root)
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT rel, focus, focus_score FROM photos WHERE status='ok'")}


def test_score_image_sharp_beats_blurred(make_img):
    s = focus.score_image(Image.open(make_img(name="s.jpg", kind="sharp")))
    b = focus.score_image(Image.open(make_img(name="b.jpg", kind="blurry")))
    assert s > 5 * b and b < FOCUS_BAD_ABS


def test_sharp_vs_blurred_photos_are_ok_vs_bad(tmp_path):
    from conftest import make_image
    for i in range(4):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    make_image(tmp_path, "b.jpg", kind="blurry", seed=9)
    before = sorted(os.listdir(tmp_path))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    seen = []
    out = focus.check_focus(tmp_path, progress=lambda d: seen.append(dict(d)))
    lab = _labels(tmp_path)
    assert lab["b.jpg"][0] == "bad" and all(lab[f"s{i}.jpg"][0] == "ok" for i in range(4))
    assert all(v[1] is not None for v in lab.values())
    assert out == {"ok": 4, "soft": 0, "bad": 1, "checked": 5}
    assert seen and seen[-1]["done"] == seen[-1]["total"] == 5 and seen[-1]["stage"] == "focus"
    assert sorted(os.listdir(tmp_path)) == before


def test_absolute_floor_keeps_a_uniformly_sharp_shoot_whole(tmp_path):
    """Bottom 5 % of a shoot is only bad when it is also under the absolute floor: sharp everywhere means nothing lost."""
    from conftest import make_image
    for i in range(20):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    out = focus.check_focus(tmp_path)
    assert out["bad"] == 0 and out["checked"] == 20
    assert set(v[0] for v in _labels(tmp_path).values()) <= {"ok", "soft"}
    assert out["soft"] >= 1     # the relative band still exists: bottom 15 % of the shoot


def test_something_in_focus_rescues_a_flat_photo(tmp_path):
    """A small sharp subject on a flat sky (a drone) scores low on the tile p90 but high on the sharpest tile;
    a sharp eye strip is the same evidence. Neither is bad, however low the p90."""
    from conftest import make_image
    for i in range(20):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    make_image(tmp_path, "flat.jpg", kind="blurry", seed=7)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    focus.check_focus(tmp_path)
    assert _labels(tmp_path)["flat.jpg"][0] == "bad"
    conn.execute("UPDATE photos SET sharp_max=? WHERE rel='flat.jpg'", (FOCUS_BAD_MAX_ABS * 5,)); conn.commit()
    focus.check_focus(tmp_path, only_unchecked=False)
    assert _labels(tmp_path)["flat.jpg"][0] == "soft"
    conn.execute("UPDATE photos SET sharp_max=1, sharp_eye=? WHERE rel='flat.jpg'", (FOCUS_BAD_MAX_ABS * 5,)); conn.commit()
    focus.check_focus(tmp_path, only_unchecked=False)
    assert _labels(tmp_path)["flat.jpg"][0] == "soft"
    conn.execute("UPDATE photos SET sharp_eye=1 WHERE rel='flat.jpg'"); conn.commit()
    focus.check_focus(tmp_path, only_unchecked=False)
    assert _labels(tmp_path)["flat.jpg"][0] == "bad"


def test_only_unchecked_skips_labelled_rows_but_keeps_the_whole_distribution(tmp_path):
    from conftest import make_image
    for i in range(10):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    first = focus.check_focus(tmp_path)
    assert first["checked"] == 10
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET focus='ok', focus_score=99999 WHERE rel='s0.jpg'"); conn.commit()
    make_image(tmp_path, "b.jpg", kind="blurry", seed=20)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    second = focus.check_focus(tmp_path)          # only the new row
    assert second["checked"] == 1 and second["bad"] == 1
    lab = _labels(tmp_path)
    assert lab["s0.jpg"] == ("ok", 99999) and lab["b.jpg"][0] == "bad"
    third = focus.check_focus(tmp_path, only_unchecked=False)
    assert third["checked"] == 11 and _labels(tmp_path)["s0.jpg"][1] != 99999
    assert focus.check_focus(tmp_path) == {"ok": 0, "soft": 0, "bad": 0, "checked": 0}


def test_checked_rows_shape_the_percentiles_for_a_late_batch(tmp_path):
    """Five soft photos added to a checked shoot are not the bottom 5 % of themselves: the percentile
    comes from every ok row of the shoot. Here the twenty checked rows all sit lower, so none of the
    five is bad; judged among themselves the lowest one would be."""
    from conftest import make_image
    for i in range(20):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    focus.check_focus(tmp_path)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET focus='bad', focus_score=5, sharp_tile=5, sharp_max=5"); conn.commit()
    for i in range(5):
        make_image(tmp_path, f"late{i}.jpg", kind="sharp", seed=50 + i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    for i in range(5):
        conn.execute("UPDATE photos SET sharp_tile=?, sharp_max=? WHERE rel=?", (10 + i, 10 + i, f"late{i}.jpg"))
    conn.commit()
    out = focus.check_focus(tmp_path)
    assert out["checked"] == 5 and out["bad"] == 0


def test_re_decoded_photo_loses_its_label(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg", kind="sharp", seed=1); make_image(tmp_path, "b.jpg", kind="blurry", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    focus.check_focus(tmp_path)
    assert _labels(tmp_path)["b.jpg"][0] == "bad"
    make_image(tmp_path, "b.jpg", kind="sharp", seed=3)
    os.utime(tmp_path / "b.jpg", (1, 2_000_000_000))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert _labels(tmp_path)["b.jpg"] == (None, None)
    assert focus.status(tmp_path) == {"checked": 1, "unchecked": 1, "bad": 0, "soft": 0}


def test_status_counts(tmp_path):
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    make_image(tmp_path, "b.jpg", kind="blurry", seed=9)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert focus.status(tmp_path) == {"checked": 0, "unchecked": 4, "bad": 0, "soft": 0}
    focus.check_focus(tmp_path)
    st = focus.status(tmp_path)
    assert st["checked"] == 4 and st["unchecked"] == 0 and st["bad"] == 1


# Videos: scored from the sample frames the index stored, never from the clip.

def _blurred_copy(src, dst, radius=12):
    from conftest import FFMPEG
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-vf", f"boxblur={radius}:2", "-pix_fmt", "yuv420p", str(dst)],
                   check=True, capture_output=True)


def _testsrc(dst, seconds):
    """A sharp testsrc clip of its own length: identical clips share a qhash and so their stored frames."""
    from conftest import FFMPEG
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds},format=yuv420p", str(dst)],
                   check=True, capture_output=True)


def test_sharp_vs_blurred_clips_from_stored_frames(tmp_path, tmp_path_factory):
    from conftest import make_video, needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    work = tmp_path_factory.mktemp("work")
    make_video(tmp_path / "sharp.mp4", scenes=1, work=work)
    _blurred_copy(tmp_path / "sharp.mp4", tmp_path / "blur.mp4")
    for i in range(3):
        _testsrc(tmp_path / f"s{i}.mp4", 4 + i)
    before = sorted(os.listdir(tmp_path))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    idx = db.index_dir(tmp_path)
    rows = {r["rel"]: dict(r) for r in conn.execute("SELECT id, rel, qhash, kind, duration FROM photos")}
    assert focus.score_video(tmp_path, rows["sharp.mp4"]) > 5 * focus.score_video(tmp_path, rows["blur.mp4"])
    # the clip itself is not read: with the source gone the stored frames still score it
    os.rename(tmp_path / "blur.mp4", work / "blur.mp4")
    try:
        assert focus.score_video(tmp_path, rows["blur.mp4"]) < FOCUS_BAD_ABS
    finally:
        os.rename(work / "blur.mp4", tmp_path / "blur.mp4")
    out = focus.check_focus(tmp_path)
    lab = _labels(tmp_path)
    assert lab["blur.mp4"][0] == "bad" and lab["sharp.mp4"][0] == "ok" and out["bad"] == 1
    assert sorted(os.listdir(tmp_path)) == before
    assert sorted(os.listdir(idx / "frames")) == sorted(os.listdir(idx / "frames"))   # frames untouched


def test_median_across_frames_one_blurred_frame_stays_ok(tmp_path, tmp_path_factory):
    """A whip-pan in the middle of a clip is one soft frame among five: the median says the clip is fine."""
    from conftest import make_video, needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    for i in range(4):
        _testsrc(tmp_path / f"s{i}.mp4", 3 + i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path); idx = db.index_dir(tmp_path)
    assert conn.execute("SELECT COUNT(DISTINCT qhash) FROM photos").fetchone()[0] == 4
    row = dict(conn.execute("SELECT id, rel, qhash, kind, duration FROM photos WHERE rel='s0.mp4'").fetchone())
    frames = sorted((idx / "frames").glob(f"{row['qhash']}_*.jpg"))
    assert len(frames) >= 5
    for p in frames[2:3]:       # one frame blurred in place (index dir, never the shoot)
        Image.open(p).filter(ImageFilter.GaussianBlur(12)).save(p, quality=85)
    all_frames = [focus.score_image(Image.open(p)) for p in frames]
    assert min(all_frames) < FOCUS_BAD_ABS < focus.score_video(tmp_path, row)
    focus.check_focus(tmp_path)
    assert _labels(tmp_path)["s0.mp4"][0] != "bad"      # the lowest of four is soft by definition, never bad
    for p in frames:            # every frame blurred: the clip is bad
        Image.open(p).filter(ImageFilter.GaussianBlur(12)).save(p, quality=85)
    focus.check_focus(tmp_path, only_unchecked=False)
    assert _labels(tmp_path)["s0.mp4"][0] == "bad"


def test_video_without_stored_frames_decodes_three(tmp_path, tmp_path_factory, monkeypatch):
    from conftest import make_video, needs_ffmpeg
    from photosort import video
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    work = tmp_path_factory.mktemp("work")
    make_video(tmp_path / "one.mp4", scenes=1, work=work)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path); idx = db.index_dir(tmp_path)
    row = dict(conn.execute("SELECT id, rel, qhash, kind, duration FROM photos WHERE rel='one.mp4'").fetchone())
    for p in (idx / "frames").glob(f"{row['qhash']}_*.jpg"):
        p.unlink()
    calls = []
    real = video.frame_at
    def spy(path, t, **kw):
        calls.append(t); return real(path, t, **kw)
    monkeypatch.setattr(video, "frame_at", spy)
    assert focus.score_video(tmp_path, row) > FOCUS_BAD_ABS
    assert len(calls) == 3


# ===== the job row: a focus pass the server never finished is found again as interrupted =====

def test_focus_pass_keeps_a_job_row_done_or_failed(tmp_path, monkeypatch):
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"s{i}.jpg", kind="sharp", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    assert "focus" not in db.latest_jobs(conn)
    focus.check_focus(tmp_path)
    j = db.latest_jobs(conn)["focus"]
    assert j["state"] == "done" and j["progress"] == {"stage": "focus", "done": 3, "total": 3} and j["finished"]
    # A pass that blows up leaves a failed row with the reason, and the exception still reaches the caller.
    def boom(*a, **k):
        raise RuntimeError("frames unreadable")
    monkeypatch.setattr(focus, "_check_focus", boom)
    with pytest.raises(RuntimeError):
        focus.check_focus(tmp_path, only_unchecked=False)
    j = db.latest_jobs(conn)["focus"]
    assert j["state"] == "failed" and "frames unreadable" in j["error"]
    # A row still 'running' when the shoot is next opened (the app died mid-pass) is marked interrupted.
    jid = db.start_job(conn, "focus", {"stage": "focus", "done": 1, "total": 3})
    assert db.interrupt_running_jobs(conn) == 1
    assert db.latest_jobs(conn)["focus"]["state"] == "interrupted" and db.latest_jobs(conn)["focus"]["id"] == jid
