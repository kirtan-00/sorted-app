"""Flicker detection on synthetic frames. The scene is smooth texture plus sensor noise, quantised to 8 bits like
a real decode, because a peak-to-noise test on noiseless frames would prove nothing."""
import subprocess
import numpy as np
import pytest
from conftest import FFMPEG, needs_ffmpeg

H, W = 96, 160
NOISE = 1.5 / 255


def texture(seed, h=H, w=W, level=0.5, contrast=0.25):
    """Smooth random scene, wider than the frame when a pan needs room."""
    import cv2
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.normal(size=(h, w)).astype(np.float32), (0, 0), 2.5)
    return level + contrast * base / (base.std() + 1e-9) * 0.6


def finish(frames, seed):
    rng = np.random.default_rng(seed + 1000)
    f = frames + rng.normal(0, NOISE, frames.shape)
    return np.clip(np.rint(f * 255), 0, 255).astype(np.uint8)


def static(n, seed=0):
    return np.repeat(texture(seed)[None], n, axis=0)


def pulsing(n, fps, light=100.0, depth=0.05, seed=0):
    t = np.arange(n) / fps
    return finish(static(n, seed) * (1 + depth * np.sin(2 * np.pi * light * t + 0.7))[:, None, None], seed)


def banding(n, fps, light=100.0, depth=0.05, readout=0.025, seed=0):
    """Each row is exposed readout/H later than the one above it."""
    t = np.arange(n)[:, None] / fps + np.arange(H)[None, :] * (readout / H)
    return finish(static(n, seed) * (1 + depth * np.sin(2 * np.pi * light * t))[:, :, None], seed)


def analyse(frames, fps, **kw):
    from photosort.flicker import analyse as go
    return go(frames, fps, **kw)


# ---- risk

@pytest.mark.parametrize("fps,mains,hz", [(24, 50, 4), (30, 50, 10), (25, 60, 5), (60, 50, 20), (25, 50, 0), (30, 60, 0), (24, 60, 0)])
def test_risk_predicts_the_alias(fps, mains, hz):
    from photosort.flicker import risk
    assert risk(fps)["mains"][mains]["alias_hz"] == pytest.approx(hz)


def test_risk_fractional_rate_gives_a_beat_too_slow_to_test():
    from photosort.flicker import risk
    v = risk(30000 / 1001)["mains"][60]
    assert v["alias_hz"] == pytest.approx(0.12, abs=0.01) and not v["locked"] and not v["testable"]


def test_risk_shutter():
    from photosort.flicker import risk
    assert risk(24)["mains"][50]["shutter_cancels"] is None            # unknown is not False
    r = risk(24, exposure_s=1 / 50)
    assert r["mains"][50]["shutter_cancels"] is True and r["mains"][60]["shutter_cancels"] is False
    assert risk(24, exposure_s=1 / 48)["mains"][50]["shutter_cancels"] is False
    assert risk(24, exposure_s=1 / 500)["mains"][50]["shutter_cancels"] is False
    assert risk(24, exposure_s=1 / 50, mains=(50,))["at_risk"] is False
    assert risk(24, mains=(50,))["at_risk"] is True
    assert risk(25, mains=(50,))["at_risk"] is False                   # frame-locked
    assert risk(0)["at_risk"] is None


# ---- detection

@pytest.mark.parametrize("fps,light,beat", [(24, 100, 4), (30, 100, 10), (25, 120, 5), (23.976, 100, 4.1), (60, 100, 20), (29.97, 100, 10.1)])
def test_pulsing_is_found_at_its_alias(fps, light, beat):
    r = analyse(pulsing(int(fps * 3), fps, light), fps)
    assert r["flicker"] == "pulsing", r
    assert r["hz"] == pytest.approx(beat, abs=0.5)
    assert r["strength"] == pytest.approx(0.05, rel=0.3)


def test_detection_tolerates_a_wandering_grid():
    r = analyse(pulsing(72, 24, light=100.8), 24)                      # 50.4 Hz mains
    assert r["flicker"] == "pulsing" and r["hz"] == pytest.approx(4.8, abs=0.5)


def test_flicker_survives_a_subject_crossing_the_frame():
    f = pulsing(90, 30).astype(np.float32)
    for i in range(90):
        x = 10 + i
        f[i, 20:80, x:x + 30] = 40
    assert analyse(f.astype(np.uint8), 30)["flicker"] == "pulsing"


@pytest.mark.parametrize("fps,light,readout", [(24, 100, 0.025), (30, 100, 0.03), (25, 120, 0.02), (30, 100, 0.08)])
def test_rolling_banding(fps, light, readout):
    r = analyse(banding(int(fps * 3), fps, light, readout=readout), fps)
    assert r["flicker"] == "banding", r
    assert r["strength"] == pytest.approx(0.05, rel=0.4)


def test_banding_on_a_blank_wall_is_not_read_as_a_pan():
    wall = np.full((90, H, W), 0.5, np.float32)
    t = np.arange(90)[:, None] / 30 + np.arange(H)[None, :] * (0.03 / H)
    f = finish(wall * (1 + 0.05 * np.sin(2 * np.pi * 100 * t))[:, :, None], 3)
    assert analyse(f, 30)["flicker"] == "banding"


def test_sixteen_frames_is_enough_for_strong_flicker():
    assert analyse(pulsing(16, 24, depth=0.1), 24)["flicker"] == "pulsing"


def test_shutter_that_cancels_only_rules_out_that_light():
    assert analyse(pulsing(72, 24), 24, exposure_s=1 / 50, mains=(50,))["flicker"] == "none"
    assert analyse(pulsing(75, 25, light=120), 25, exposure_s=1 / 50)["flicker"] == "pulsing"


# ---- must not fire

def test_clean_static():
    r = analyse(finish(static(90), 1), 30)
    assert r["flicker"] == "none" and r["hz"] is None and r["strength"] == 0.0


@pytest.mark.parametrize("step", [0.15, 1.0, 3.0])
@pytest.mark.parametrize("stripes", [False, True])
def test_pan_is_never_flicker(step, stripes):
    """A panned periodic pattern gives every tile a truly periodic series: stripes 30 px apart at 3 px per frame
    is a 3 Hz line at 30 fps, and 7.5 px apart at 1 px is 4 Hz at 30 fps, so this is the case the gate is for."""
    import cv2
    n = 90
    wide = texture(4, H, W + 400)
    if stripes:
        wide = wide + 0.2 * np.sin(2 * np.pi * np.arange(W + 400) / (7.5 if step == 1.0 else 30))[None, :]
    f = []
    for i in range(n):
        m = np.float32([[1, 0, -i * step], [0, 1, 0]])
        f.append(cv2.warpAffine(wide.astype(np.float32), m, (W + 400, H))[:, :W])
    r = analyse(finish(np.stack(f), 4), 30)
    assert r["flicker"] in ("unknown", "none"), r


def test_handheld_shake_at_the_beat_on_stripes_is_not_flicker():
    """A 4 Hz wobble under the motion gate, against texture: tiles swing at the alias but not together."""
    import cv2
    n = 72
    wide = (texture(5, H, W + 40) + 0.15 * np.sin(2 * np.pi * np.arange(W + 40) / 23)[None, :]).astype(np.float32)
    f = []
    for i in range(n):
        dx = 20 + 0.4 * np.sin(2 * np.pi * 4 * i / 24)
        f.append(cv2.warpAffine(wide, np.float32([[1, 0, -dx], [0, 1, 0]]), (W + 40, H))[:, :W])
    assert analyse(finish(np.stack(f), 5), 24)["flicker"] in ("none", "unknown")


def test_slow_exposure_ramp():
    n = 90
    ramp = np.linspace(0.6, 1.3, n) ** 2.2
    assert analyse(finish(static(n, 6) * ramp[:, None, None] * 0.6, 6), 30)["flicker"] == "none"


def test_hard_cut():
    f = np.concatenate([static(40, 7), static(50, 8) * 0.7 + 0.1])
    r = analyse(finish(f, 7), 30)
    assert r["flicker"] == "none", r


def test_flicker_is_still_found_on_one_side_of_a_cut():
    a = pulsing(45, 30).astype(np.uint8)
    b = finish(static(45, 9) * 0.7, 9)
    assert analyse(np.concatenate([a, b]), 30)["flicker"] == "pulsing"


@pytest.mark.parametrize("gain", [0.97, 0.9, 1.4])
def test_one_sudden_light_change(gain):
    n = 90
    g = np.where(np.arange(n) < 37, 1.0, gain)
    assert analyse(finish(static(n, 10) * 0.7 * g[:, None, None], 10), 30)["flicker"] == "none"


@pytest.mark.parametrize("tick", [1.01, 1.03])
def test_keyframe_pop_comb_is_not_flicker(tick):
    """A brightness tick every 12 frames at 24 fps is 2 Hz with harmonics, one of which sits right on 4 Hz. The
    strong tick reads as a run of cuts and says unknown; the weak one has to be caught as a comb."""
    n = 72
    g = np.ones(n)
    g[::12] = tick
    assert analyse(finish(static(n, 11) * g[:, None, None], 11), 24)["flicker"] in ("none", "unknown")


def test_burst_of_a_real_light_at_the_beat_is_not_a_steady_line():
    n = 90
    t = np.arange(n) / 30
    g = 1 + np.where(t < 0.8, 0.06 * np.sin(2 * np.pi * 10 * t), 0)
    assert analyse(finish(static(n, 12) * g[:, None, None], 12), 30)["flicker"] == "none"


# ---- not enough to say

def test_a_clean_verdict_names_the_light_it_could_not_test():
    r = analyse(finish(static(75), 2), 25)                             # 25 fps: 100 Hz is frame-locked
    assert r["flicker"] == "none" and "100 Hz light untested" in r["reason"]


def test_too_few_frames_is_unknown():
    r = analyse(pulsing(15, 24), 24)
    assert r["flicker"] == "unknown" and r["hz"] is None


@pytest.mark.parametrize("bad", [np.zeros((30, 8)), np.zeros(5)])
def test_wrong_shape_is_unknown(bad):
    assert analyse(bad, 24)["flicker"] == "unknown"


def test_no_frame_rate_is_unknown():
    assert analyse(pulsing(30, 24), 0)["flicker"] == "unknown"


def test_black_clip_is_unknown():
    assert analyse(np.zeros((90, H, W), np.uint8), 30)["flicker"] == "unknown"


def test_a_rate_with_no_testable_beat_is_unknown():
    assert analyse(finish(static(75), 2), 25, mains=(50,))["flicker"] == "unknown"


def test_a_cut_every_few_frames_is_unknown():
    f = np.concatenate([static(10, s) * (0.5 + 0.1 * (s % 3)) for s in range(8)])
    assert analyse(finish(f, 13), 30)["flicker"] == "unknown"


# ---- combining runs

def test_combine():
    from photosort.flicker import _combine, _result
    p = _result("pulsing", "x", 4.0, 0.05); q = _result("pulsing", "x", 4.3, 0.03); z = _result("none", "x"); u = _result("unknown", "x")
    assert _combine([p, q, z])["flicker"] == "pulsing"
    assert _combine([p, z, z])["flicker"] == "unknown"
    assert _combine([p, z])["flicker"] == "unknown"
    assert _combine([p, u, u])["flicker"] == "pulsing"
    assert _combine([z, u, z])["flicker"] == "none"
    assert _combine([u, u])["flicker"] == "unknown"
    assert _combine([])["flicker"] == "unknown"
    assert _combine([p, _result("pulsing", "x", 10.0, 0.05), z])["flicker"] == "unknown"     # two runs, two beats


def test_start_times_stay_inside_the_clip():
    from photosort.flicker import _start_times
    assert len(_start_times(60, 3)) == 3 and len(_start_times(7, 3)) == 2 and _start_times(2, 3) == [0.0]
    assert all(0 <= t <= 57 for t in _start_times(60, 3))


# ---- through ffmpeg

def encode(path, frames, fps):
    """Grey numpy frames through libx264 at near-lossless quality, the way a clean camera file would arrive."""
    n, h, w = frames.shape
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                    "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(path)], input=frames.tobytes(), check=True)
    return path


def big(frames):
    return np.repeat(np.repeat(frames, 2, axis=1), 2, axis=2)


@pytest.fixture(scope="module")
def flickering(tmp_path_factory):
    return encode(tmp_path_factory.mktemp("clips") / "flick.mp4", big(pulsing(240, 24)), 24)


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    return encode(tmp_path_factory.mktemp("clips") / "clean.mp4", big(finish(static(240, 5), 5)), 24)


@needs_ffmpeg
def test_read_run_gives_consecutive_small_frames(flickering):
    from photosort.flicker import read_run, analyse as go, RUN_ROWS, RUN_COLS
    frames, rate = read_run(flickering, 2.0)
    assert frames.shape == (72, RUN_ROWS, RUN_COLS) and frames.dtype == np.float32 and 0 <= frames.min() and frames.max() <= 1
    assert rate == pytest.approx(24, abs=0.01)
    assert go(frames, rate)["flicker"] == "pulsing"


@needs_ffmpeg
def test_read_run_caps_the_frames(flickering):
    from photosort.flicker import read_run
    assert read_run(flickering, 0.0, seconds=5.0, max_frames=30)[0].shape[0] == 30


@needs_ffmpeg
def test_read_run_on_junk_is_none(tmp_path):
    from photosort.flicker import read_run
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    assert read_run(tmp_path / "junk.mp4", 0.0) is None


@needs_ffmpeg
def test_read_run_keeps_the_even_stretch_of_a_variable_rate_clip():
    from photosort.flicker import _even_stretch
    gaps = np.array([1, 1, 1, 2, 1, 1, 1, 1, 1, 3, 1], float) / 30
    assert _even_stretch(gaps, 1 / 30) == (4, 10)


@needs_ffmpeg
def test_check_clip_real_files(flickering, clean):
    from photosort.flicker import check_clip
    r = check_clip(flickering, 10.0, 24)
    assert r["flicker"] == "pulsing" and r["hz"] == pytest.approx(4, abs=0.5) and len(r["runs"]) == 3
    assert check_clip(clean, 10.0, 24)["flicker"] == "none"


@needs_ffmpeg
def test_static_shot_with_short_keyframe_interval_is_not_a_run_of_cuts(tmp_path):
    """x264 skips a static shot's frames and redraws the noise at each keyframe."""
    from photosort.flicker import check_clip
    p = tmp_path / "gop.mp4"
    n, h, w = 240, H * 2, W * 2
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", "24", "-i", "-", "-c:v", "libx264",
                    "-crf", "23", "-g", "12", "-pix_fmt", "yuv420p", str(p)], input=big(finish(static(n, 6), 6)).tobytes(), check=True)
    assert check_clip(p, 10.0, 24)["flicker"] == "none"


@needs_ffmpeg
def test_check_clip_banding_real_file(tmp_path):
    from photosort.flicker import check_clip
    p = encode(tmp_path / "band.mp4", big(banding(240, 30)), 30)
    assert check_clip(p, 8.0, 30)["flicker"] == "banding"


@needs_ffmpeg
def test_check_clip_believes_the_sidecar_about_slow_motion(tmp_path):
    """50p played in a 25p file: 120 Hz light beats at 20 Hz of capture time, 10 Hz of playback time. Taken at face
    value, 25 fps predicts 0 and 5 Hz and the line at 10 Hz is missed."""
    from photosort.flicker import check_clip
    t = np.arange(240) / 50.0
    f = finish(static(240, 3) * (1 + 0.05 * np.sin(2 * np.pi * 120 * t))[:, None, None], 3)
    p = encode(tmp_path / "C0001.mp4", big(f), 25)
    assert check_clip(p, 9.6, 25)["flicker"] == "none"
    (tmp_path / "C0001M01.XML").write_text('<VideoFrame captureFps="50p" formatFps="25p"/>')
    assert check_clip(p, 9.6, 25)["flicker"] == "pulsing"


def test_check_clip_with_a_cancelling_shutter_decodes_nothing(tmp_path):
    from photosort.flicker import check_clip
    r = check_clip(tmp_path / "missing.mp4", 10, 24, exposure_s=1 / 50, mains=(50,))
    assert r["flicker"] == "none" and r["runs"] == []


def test_check_clip_never_raises(tmp_path, monkeypatch):
    from photosort.flicker import check_clip
    assert check_clip(tmp_path / "missing.mp4", 0, 24)["flicker"] == "unknown"
    assert check_clip(tmp_path / "missing.mp4", 0.2, 24)["flicker"] == "unknown"
    monkeypatch.setenv("PHOTOSORT_NO_FFMPEG", "1")
    assert check_clip(tmp_path / "missing.mp4", 10, 24)["flicker"] == "unknown"


@needs_ffmpeg
def test_check_clip_on_junk_is_unknown(tmp_path):
    from photosort.flicker import check_clip
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    assert check_clip(tmp_path / "junk.mp4", 10, 24)["flicker"] == "unknown"
