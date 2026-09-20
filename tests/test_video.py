import os
import pytest
from PIL import Image
from conftest import make_video, needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture
def two_scene(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return make_video(d / "two.mp4", scenes=2, work=d / "work")


@pytest.fixture
def one_scene(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    return make_video(d / "one.mp4", scenes=1, work=d / "work")


def test_probe_reports_duration_and_size(one_scene, two_scene):
    from photosort.video import probe
    info = probe(one_scene)
    assert abs(info["duration"] - 3.0) < 0.2 and info["width"] == 320 and info["height"] == 240
    assert abs(probe(two_scene)["duration"] - 10.0) < 0.2


def test_sample_times_are_evenly_spaced_between_5_and_95_percent():
    from photosort.video import sample_times
    t = sample_times(10.0, n=6)
    assert len(t) == 6 and t[0] == pytest.approx(0.5) and t[-1] == pytest.approx(9.5)
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert all(g == pytest.approx(gaps[0]) for g in gaps)
    assert sample_times(10.0, n=1) == [pytest.approx(5.0)]
    assert sample_times(0.0) == [0.0]


def test_frame_at_decodes_one_frame(one_scene, tmp_path):
    from photosort.video import frame_at
    im = frame_at(one_scene, 1.0)
    assert isinstance(im, Image.Image) and im.size == (320, 240) and im.mode == "RGB"
    small = frame_at(one_scene, 1.0, edge=160)
    assert max(small.size) == 160
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    assert frame_at(tmp_path / "junk.mp4", 0.5) is None


def test_scene_cuts_finds_the_one_hard_cut(two_scene, one_scene):
    from photosort.video import scene_cuts
    cuts = scene_cuts(two_scene)
    assert len(cuts) == 1 and abs(cuts[0] - 5.0) < 0.2
    assert scene_cuts(one_scene) == []


def test_segments_from_cuts_merges_short_ones_and_caps_the_count():
    from photosort.video import segments_from_cuts
    assert segments_from_cuts([], 4.0) == [(0.0, 4.0)]
    assert segments_from_cuts([2.0], 4.0) == [(0.0, 2.0), (2.0, 4.0)]
    # a cut 0.3 s after another makes a segment shorter than MIN_SEGMENT_S: merged into the previous one
    assert segments_from_cuts([2.0, 2.3, 3.0], 4.0) == [(0.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
    # a cut too close to the end is dropped too
    assert segments_from_cuts([2.0, 3.8], 4.0) == [(0.0, 2.0), (2.0, 4.0)]
    # more than the cap: the shortest segments are folded into a neighbour until the cap holds
    cuts = [float(i) for i in range(1, 40)]
    segs = segments_from_cuts(cuts, 40.0, max_segments=24)
    assert len(segs) == 24 and segs[0][0] == 0.0 and segs[-1][1] == 40.0
    assert all(b[0] == a[1] for a, b in zip(segs, segs[1:]))          # contiguous, no gaps


def test_sample_frames_returns_even_frames_plus_segment_midpoints(two_scene):
    from photosort.video import sample_frames, sample_times, probe
    d = probe(two_scene)["duration"]
    frames, segs = sample_frames(two_scene, d)
    assert len(segs) == 2 and segs[0][0] == 0.0 and abs(segs[0][1] - 5.0) < 0.2 and abs(segs[1][1] - d) < 1e-6
    times = [t for t, _ in frames]
    assert times == sorted(times) and len(set(times)) == len(times)
    for t in sample_times(d):
        assert t in times
    # every segment midpoint has a frame within half a second (here the even samples already cover both)
    for s, e in segs:
        mid = (s + e) / 2
        assert min(abs(t - mid) for t in times) <= 0.5
    assert len(frames) == 6                                              # both midpoints deduplicated
    assert all(isinstance(im, Image.Image) for _, im in frames)


def test_sample_frames_adds_a_midpoint_frame_when_no_even_sample_is_near(two_scene, monkeypatch):
    import photosort.video as v
    monkeypatch.setattr(v, "sample_times", lambda d, n=6: [0.5, 9.5])   # nothing near the midpoints 2.5 and 7.5
    frames, segs = v.sample_frames(two_scene, 10.0)
    assert len(segs) == 2 and abs(segs[0][1] - 5.0) < 0.2
    times = [t for t, _ in frames]
    assert times[0] == 0.5 and times[-1] == 9.5 and len(times) == 4
    assert any(abs(t - 2.5) < 0.1 for t in times) and any(abs(t - 7.5) < 0.1 for t in times)


def test_short_clips_skip_the_scene_pass(one_scene, monkeypatch):
    import photosort.video as v
    def boom(path, threshold=0.4):
        raise AssertionError("scene pass ran on a clip shorter than SCENE_MIN_DURATION_S")
    monkeypatch.setattr(v, "scene_cuts", boom)
    frames, segs = v.sample_frames(one_scene, 3.0)
    assert segs == [(0.0, 3.0)] and len(frames) == 6


H264 = ("h264", "yuv420p")            # the testsrc fixtures; accelerated on a Mac
HEVC10 = ("hevc", "yuv420p10le")      # DJI Air 3S: accelerated, 9x faster on an M1
SONY422 = ("h264", "yuv422p10le")     # Sony A7S III XAVC S-I: videotoolbox fails, never worth trying


def test_hwaccel_is_dropped_for_the_failing_codec_pair_only(one_scene, monkeypatch):
    """A decode that fails with hwaccel and succeeds without marks that (codec, pix_fmt) pair bad for the
    process; every other pair keeps hardware decode. Before, one Sony 4:2:2 clip switched it off for every
    DJI clip the worker saw afterwards."""
    import subprocess as sp
    import photosort.video as v
    monkeypatch.setattr(v.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(v, "FFMPEG_HWACCEL", "videotoolbox")
    monkeypatch.setattr(v, "_HWACCEL_OK", {})
    real_run = sp.run
    calls = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "-hwaccel" in cmd and "fail" in str(cmd):
            return sp.CompletedProcess(cmd, 1, b"", b"hwaccel init failed")
        return real_run(cmd, **kw)
    monkeypatch.setattr(v.subprocess, "run", fake_run)
    bad = one_scene.with_name("fail.mp4"); bad.write_bytes(one_scene.read_bytes())
    im = v.frame_at(bad, 1.0, key=HEVC10)                       # pair A: hwaccel fails, retry without succeeds
    assert im is not None and im.size == (320, 240)
    assert len(calls) == 2
    assert "-hwaccel" in calls[0] and calls[0].index("-hwaccel") < calls[0].index("-i") and calls[0][calls[0].index("-hwaccel") + 1] == "videotoolbox"
    assert "-hwaccel" not in calls[1]
    assert v._HWACCEL_OK == {HEVC10: False}
    assert v.frame_at(bad, 1.5, key=HEVC10) is not None
    assert len(calls) == 3 and "-hwaccel" not in calls[2]                 # remembered: no retry dance for that pair
    assert v.frame_at(one_scene, 1.0, key=H264) is not None                # pair B keeps hardware decode
    assert len(calls) == 4 and "-hwaccel" in calls[3]
    assert v._HWACCEL_OK == {HEVC10: False, H264: True}                    # a success is remembered good
    # off the Mac, or with the setting cleared, no hwaccel flag at all
    monkeypatch.setattr(v, "FFMPEG_HWACCEL", "")
    assert v.frame_at(one_scene, 1.0, key=H264) is not None and "-hwaccel" not in calls[-1]


def test_hwaccel_is_never_tried_for_an_unsupported_pix_fmt_or_without_a_key(one_scene, monkeypatch):
    """Sony 10-bit 4:2:2 is not on the videotoolbox list: no attempt, no failed seconds per clip. A call with
    no key (nothing probed) decodes in software too."""
    import subprocess as sp
    import photosort.video as v
    monkeypatch.setattr(v.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(v, "FFMPEG_HWACCEL", "videotoolbox")
    monkeypatch.setattr(v, "_HWACCEL_OK", {})
    calls = []; real_run = sp.run
    def spy(cmd, **kw):
        calls.append(list(cmd)); return real_run(cmd, **kw)
    monkeypatch.setattr(v.subprocess, "run", spy)
    assert "yuv422p10le" not in v.HWACCEL_PIX_FMTS and {"yuv420p", "yuv420p10le", "nv12", "p010le", "yuvj420p"} <= v.HWACCEL_PIX_FMTS
    assert v.frame_at(one_scene, 1.0, key=SONY422) is not None
    assert v.frame_at(one_scene, 1.0) is not None
    assert len(calls) == 2 and all("-hwaccel" not in c for c in calls)
    assert v._HWACCEL_OK == {}                                             # nothing learned, nothing tried
    assert v.scene_cuts(one_scene, key=SONY422) == [] and "-hwaccel" not in calls[-1]
    assert v.frame_at(one_scene, 1.0, key=H264) is not None and "-hwaccel" in calls[-1]


def test_probe_reports_codec_and_pix_fmt(one_scene):
    from photosort.video import probe
    info = probe(one_scene)
    assert (info["codec"], info["pix_fmt"]) == H264


def test_sample_frames_threads_the_key_to_every_decode(two_scene, monkeypatch):
    import photosort.video as v
    seen = []
    monkeypatch.setattr(v, "scene_cuts", lambda path, threshold=0.4, key=None: seen.append(("cuts", key)) or [5.0])
    real = v.frame_at
    monkeypatch.setattr(v, "frame_at", lambda path, t, edge=1024, key=None: seen.append(("frame", key)) or real(path, t, edge))
    frames, segs = v.sample_frames(two_scene, 10.0, key=HEVC10)
    assert len(segs) == 2 and frames
    assert seen and all(k == HEVC10 for _, k in seen) and ("cuts", HEVC10) in seen


def test_scene_pass_decodes_keyframes_only(two_scene, monkeypatch):
    import subprocess as sp
    import photosort.video as v
    seen = []
    real_run = sp.run
    def spy(cmd, **kw):
        seen.append(list(cmd)); return real_run(cmd, **kw)
    monkeypatch.setattr(v.subprocess, "run", spy)
    assert len(v.scene_cuts(two_scene)) == 1
    cmd = seen[-1]
    assert "-skip_frame" in cmd and cmd[cmd.index("-skip_frame") + 1] == "nokey" and cmd.index("-skip_frame") < cmd.index("-i")
    assert "scale=320:-2,select=" in cmd[cmd.index("-vf") + 1]


def test_unreadable_video_raises(tmp_path, monkeypatch):
    import photosort.video as v
    from photosort.video import sample_frames, VideoUnreadable, probe
    monkeypatch.setattr(v.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(v, "FFMPEG_HWACCEL", "videotoolbox"); monkeypatch.setattr(v, "_HWACCEL_OK", {})
    bad = tmp_path / "bad.mp4"; bad.write_bytes(b"\x00" * 4096)
    with pytest.raises(VideoUnreadable):
        probe(bad)
    with pytest.raises(VideoUnreadable):
        sample_frames(bad, 3.0, key=("h264", "yuv420p"))
    assert v._HWACCEL_OK == {}                       # a broken file must not mark its codec pair bad for the run


def test_missing_ffmpeg_is_a_clear_error_not_a_crash(one_scene, monkeypatch):
    import photosort.video as v
    monkeypatch.setattr(v, "_FALLBACK_DIRS", ())
    monkeypatch.setattr(v.shutil, "which", lambda name: None)
    with pytest.raises(v.VideoUnreadable, match="ffprobe not found"):
        v.probe(one_scene)
    with pytest.raises(v.VideoUnreadable, match="ffmpeg not found"):
        v.sample_frames(one_scene, 3.0)


def test_export_segments_writes_one_trimmed_clip_per_segment(tmp_path, tmp_path_factory):
    from photosort import db
    from photosort.index import index_folder
    from photosort.export import export_segments
    from photosort.video import probe
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    before = sorted(os.listdir(tmp_path))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    pid = conn.execute("SELECT id FROM photos WHERE rel='clip.mp4'").fetchone()[0]
    base = tmp_path_factory.mktemp("out")
    seen = []
    out = export_segments(tmp_path, [pid], None, "copy", base, seen.append)
    assert out == base / tmp_path.resolve().name / "segments"
    files = sorted(p.name for p in out.iterdir() if p.suffix == ".mp4")
    assert files == ["clip_00_0.0-5.0.mp4", "clip_01_5.0-10.0.mp4"]
    for f in files:
        assert abs(probe(out / f)["duration"] - 5.0) < 0.5                # stream copy: cut lands on a keyframe
    assert seen[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 0}
    # only the segments whose category matches
    conn.execute("UPDATE segments SET category='beach' WHERE photo_id=? AND idx=1", (pid,)); conn.commit()
    out2 = export_segments(tmp_path, [pid], "beach", "copy", tmp_path_factory.mktemp("out2"), None)
    assert [p.name for p in out2.iterdir() if p.suffix == ".mp4"] == ["clip_01_5.0-10.0.mp4"]
    # a re-run over the same folder skips what is there
    seen2 = []
    export_segments(tmp_path, [pid], None, "copy", base, seen2.append)
    assert seen2[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 2}
    assert sorted(os.listdir(tmp_path)) == before


# Log thumbnails: Sony's S-Log3 sidecar and the fixed conversion to Rec.709, display only

SONY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20">
  <Duration value="1523"/>
  <AcquisitionRecord>
    <Group name="CameraUnitMetadataSet">
      <Item name="CaptureGammaEquation" value="{gamma}"/>
      <Item name="CaptureColorPrimaries" value="s-gamut3-cine"/>
    </Group>
  </AcquisitionRecord>
</NonRealTimeMeta>
"""


def test_capture_gamma_reads_the_sony_sidecar_and_is_none_otherwise(tmp_path):
    from photosort.video import capture_gamma
    clip = tmp_path / "C0011.MP4"; clip.write_bytes(b"v")
    assert capture_gamma(clip) is None                                          # no sidecar
    (tmp_path / "C0011M01.XML").write_text(SONY_XML.format(gamma="s-log3-cine"))
    assert capture_gamma(clip) == "s-log3-cine"
    (tmp_path / "C0011M01.XML").write_text(SONY_XML.format(gamma="s-log3"))
    assert capture_gamma(clip) == "s-log3"
    (tmp_path / "C0011M01.XML").write_bytes(b"\xff\xfe not xml at all")
    assert capture_gamma(clip) is None                                          # junk: None, never raises
    big = tmp_path / "C0012.MP4"; big.write_bytes(b"v")
    (tmp_path / "C0012M01.XML").write_text(" " * 70000 + SONY_XML.format(gamma="s-log3"))
    assert capture_gamma(big) is None                                           # only the first 64 KB are read
    assert sorted(p.name for p in tmp_path.iterdir()) == ["C0011.MP4", "C0011M01.XML", "C0012.MP4", "C0012M01.XML"]


def test_slog3_to_rec709_maps_mid_grey_up_and_keeps_black_black():
    import numpy as np
    from photosort.video import slog3_to_rec709
    # S-Log3 puts 18% grey at 10-bit code value 420, which is 105 in 8 bits; Rec.709 puts it near 0.41
    grey = Image.new("RGB", (8, 8), (105, 105, 105))
    out = slog3_to_rec709(grey)
    assert out.mode == "RGB" and out.size == (8, 8)
    v = np.asarray(out)[0, 0].astype(float) / 255
    assert all(0.40 <= c <= 0.47 for c in v), v
    black = np.asarray(slog3_to_rec709(Image.new("RGB", (4, 4), (0, 0, 0))))
    assert black.max() <= 8
    # code value 95 is S-Log3's black (linear 0); at or below it the output is black
    assert np.asarray(slog3_to_rec709(Image.new("RGB", (4, 4), (24, 24, 24)))).max() <= 8
    white = np.asarray(slog3_to_rec709(Image.new("RGB", (4, 4), (255, 255, 255))))
    assert white.min() >= 250
    # monotonic on grey, and a flat log frame gains contrast
    ramp = np.asarray(slog3_to_rec709(Image.fromarray(np.tile(np.arange(256, dtype=np.uint8), (4, 1)).repeat(3).reshape(4, 256, 3))))
    row = ramp[0, :, 0].astype(int)
    assert all(b >= a for a, b in zip(row, row[1:]))
    assert row[160] - row[80] > 160 - 80


def test_process_video_converts_slog3_frames_only_when_the_sidecar_says_so(tmp_path, tmp_path_factory, monkeypatch):
    """With a matching sidecar every saved frame, thumb and grid image went through slog3_to_rec709; without
    one nothing is touched. The clip itself is only read."""
    import numpy as np
    from photosort import db
    from photosort.index import _process_video
    make_video(tmp_path / "C0011.MP4", scenes=1, work=tmp_path_factory.mktemp("work"))
    before = sorted(os.listdir(tmp_path))
    out = {}; _process_video(str(tmp_path), "C0011.MP4", out)
    idx = db.index_dir(tmp_path); qh = out["row"]["qhash"]
    plain = np.asarray(Image.open(idx / "thumbs" / f"{qh}.jpg")).astype(int)
    (tmp_path / "C0011M01.XML").write_text(SONY_XML.format(gamma="s-log3-cine"))
    out2 = {}; _process_video(str(tmp_path), "C0011.MP4", out2)
    graded = np.asarray(Image.open(idx / "thumbs" / f"{qh}.jpg")).astype(int)
    assert plain.shape == graded.shape and np.abs(plain - graded).mean() > 5      # the frame changed
    for k in range(6):
        assert (idx / "frames" / f"{qh}_{k}.jpg").is_file()
    assert sorted(os.listdir(tmp_path)) == before + ["C0011M01.XML"]


# Drone flag from metadata: a DJI encoder tag, a DJI_ filename or an .SRT telemetry sidecar

def _testsrc(dst, *metadata):
    import subprocess
    from conftest import FFMPEG
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1,format=yuv420p",
                    *metadata, str(dst)], check=True, capture_output=True)
    return dst


def test_probe_reports_aerial_from_a_dji_encoder_tag(tmp_path):
    """The Air 3S writes encoder=DJI Air3s; the mp4 muxer only keeps it on the stream here, and probe merges
    stream tags over format tags. camera falls back to that tag so the meta line reads "DJI Air3s"."""
    from photosort.video import probe
    dji = _testsrc(tmp_path / "C0001.MP4", "-metadata:s:v:0", "encoder=DJI Air3s")
    info = probe(dji)
    assert info["aerial"] is True and info["camera"] == "DJI Air3s"
    plain = _testsrc(tmp_path / "C0002.MP4")
    info = probe(plain)
    assert info["aerial"] is False and info["camera"] is None
    cmt = _testsrc(tmp_path / "C0003.MP4", "-metadata", "comment=DJI Mini 4 Pro")
    assert probe(cmt)["aerial"] is True


def test_probe_reports_aerial_from_the_filename_or_the_srt_sidecar(tmp_path):
    from photosort.video import probe
    assert probe(_testsrc(tmp_path / "dji_0007.mp4"))["aerial"] is True
    clip = _testsrc(tmp_path / "C0009.MP4")
    assert probe(clip)["aerial"] is False
    (tmp_path / "C0009.SRT").write_text("1\n[iso : 100] [shutter : 1/1000]\n")
    assert probe(clip)["aerial"] is True
    assert probe(clip)["camera"] is None                                    # no tag to fall back on


# Long clips: no keyframe scene pass, fixed windows instead

def test_fixed_segments_are_120_s_windows_capped_at_max_segments():
    from photosort.video import fixed_segments
    from photosort.config import LONG_SEGMENT_S, SCENE_MAX_DURATION_S, MAX_SEGMENTS
    assert (LONG_SEGMENT_S, SCENE_MAX_DURATION_S) == (120.0, 300.0)
    segs = fixed_segments(700.0)
    assert segs == [(0.0, 120.0), (120.0, 240.0), (240.0, 360.0), (360.0, 480.0), (480.0, 600.0), (600.0, 700.0)]
    assert fixed_segments(720.4)[-1] == (600.0, 720.4)                      # a 0.4 s tail folds into the last window
    long = fixed_segments(24 * 60.0 * 3)                                     # a 72-minute take: 36 windows, capped
    assert len(long) == MAX_SEGMENTS and long[0][0] == 0.0 and long[-1][1] == 4320.0
    widths = [b - a for a, b in long]
    assert max(widths) - min(widths) < 1e-6 and abs(widths[0] - 4320.0 / MAX_SEGMENTS) < 1e-6   # widened evenly
    assert all(b[0] == a[1] for a, b in zip(long, long[1:]))


def test_sample_frames_skips_the_scene_pass_on_long_clips(one_scene, monkeypatch):
    """Long takes are interviews and static B-roll: a full keyframe decode of a 35 GB file to find rare cuts
    is not worth it (41 s per 87 s Sony 4K clip on an M1, 11 min for a 24-minute take). Over
    SCENE_MAX_DURATION_S the segments are fixed windows and the midpoint frames are plain seeks."""
    import photosort.video as v
    def boom(path, threshold=0.4, key=None):
        raise AssertionError("scene pass ran on a clip longer than SCENE_MAX_DURATION_S")
    monkeypatch.setattr(v, "scene_cuts", boom)
    stub = Image.new("RGB", (32, 24), (10, 20, 30))
    seeks = []
    monkeypatch.setattr(v, "frame_at", lambda path, t, edge=1024, key=None: seeks.append(t) or stub)
    frames, segs = v.sample_frames(one_scene, 700.0)
    assert segs == v.fixed_segments(700.0) and len(segs) == 6
    assert [t for t, _ in frames] == sorted(seeks) and len(frames) == len(v.sample_times(700.0)) + 6
    for a, b in segs:
        assert any(abs(t - (a + b) / 2) < 1e-6 for t in seeks)              # one seek per window midpoint
    # at or under the limit the scene pass still runs
    with pytest.raises(AssertionError, match="scene pass ran"):
        v.sample_frames(one_scene, 100.0)
    with pytest.raises(AssertionError, match="scene pass ran"):
        v.sample_frames(one_scene, 300.0)
