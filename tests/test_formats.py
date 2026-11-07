"""Formats a scan used to drop without a word, and the line that now names what it still leaves out.

The CR3 and RAF files here are built to the published layouts, not taken from cameras: they prove the parsers follow
the description, not that every body writes it that way. See rawmeta.py."""
import io
import shutil
import struct
import subprocess
import numpy as np
import pytest
from pathlib import Path
from PIL import Image
from conftest import FFMPEG, make_image, needs_ffmpeg
from fastapi.testclient import TestClient
from photosort import db, decode, features, rawmeta
from photosort.index import index_folder
from photosort.server import create_app
from photosort.walk import find_images, looks_like_transport_stream


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def tiff(tags: dict, exif: dict | None = None) -> bytes:
    ex = Image.Exif()
    for k, v in tags.items():
        ex[k] = v
    b = io.BytesIO()
    Image.new("RGB", (8, 8)).save(b, "TIFF", exif=ex)
    data = b.getvalue()
    return data


def make_cr3(path: Path, orientation=1, preview_size=(1620, 1080)) -> Path:
    cmt1 = Image.Exif(); cmt1[0x010F] = "Canon"; cmt1[0x0110] = "Canon EOS R6m2"; cmt1[0x0112] = orientation
    cmt1[0x0100] = 6000; cmt1[0x0101] = 4000
    cmt2 = Image.Exif(); cmt2[0x9003] = "2026:03:04 05:06:07"; cmt2[0x8827] = 3200; cmt2[0x829A] = 0.004; cmt2[0x829D] = 2.8
    cmt2[0xA434] = "RF24-70mm F2.8 L IS USM"
    jpg = io.BytesIO(); Image.new("RGB", preview_size, (200, 40, 40)).save(jpg, "JPEG")
    prvw = box(b"PRVW", struct.pack(">IHHHHI", 0, 1, *preview_size, 1, len(jpg.getvalue())) + jpg.getvalue())
    canon = box(b"uuid", rawmeta.CANON_UUID + box(b"CNCV", b"CanonCR3_001/00.09.00/00.00.00")
                + box(b"CMT1", cmt1.tobytes()) + box(b"CMT2", cmt2.tobytes()))
    data = (box(b"ftyp", b"crx \x00\x00\x00\x01crx isom") + box(b"moov", canon)
            + box(b"uuid", rawmeta.PREVIEW_UUID + b"\x00" * 8 + prvw) + box(b"mdat", b"\x00" * 64))
    path.write_bytes(data)
    return path


def make_raf(path: Path) -> Path:
    ex = Image.Exif(); ex[0x010F] = "FUJIFILM"; ex[0x0110] = "X-T5"
    sub = ex.get_ifd(0x8769); sub[0x9003] = "2026:01:02 03:04:05"; sub[0x8827] = 800
    b = io.BytesIO(); Image.new("RGB", (1600, 1067), (20, 120, 40)).save(b, "JPEG", exif=ex)
    j = b.getvalue()
    head = b"FUJIFILMCCD-RAW 0201FF129502".ljust(84, b"\0") + struct.pack(">II", 100, len(j))
    path.write_bytes(head.ljust(100, b"\0") + j + b"\0" * 512)
    return path


def test_cr3_metadata_comes_from_its_boxes(tmp_path):
    info = features.exif_info(make_cr3(tmp_path / "IMG_0001.CR3"))
    assert info["taken_at"] == "2026-03-04T05:06:07" and info["camera"] == "Canon EOS R6m2"
    assert info["iso"] == 3200 and info["fnumber"] == 2.8 and info["lens"] == "RF24-70mm F2.8 L IS USM"
    assert (info["width"], info["height"]) == (6000, 4000)


def test_cr3_preview_when_libraw_cannot_open_it(tmp_path):
    p = make_cr3(tmp_path / "IMG_0002.CR3", orientation=6)
    im = decode.load_preview(p, 1024)                    # LibRaw refuses this synthetic file, so the PRVW JPEG is used
    assert im.size == (683, 1024)                        # 1620x1080, turned upright, fitted


def test_raf_metadata_and_preview_from_its_jpeg(tmp_path):
    p = make_raf(tmp_path / "DSCF0001.RAF")
    info = features.exif_info(p)
    assert info["taken_at"] == "2026-01-02T03:04:05" and info["camera"] == "FUJIFILM X-T5" and info["iso"] == 800
    assert decode.load_preview(p, 1024).size == (1024, 683)


@pytest.mark.parametrize("data", [b"", b"FUJIFILMCCD-RAW ", b"FUJIFILMCCD-RAW ".ljust(92, b"\xff"), b"\x00\x00\x00\x01ftypcrx " + b"\xff" * 40])
def test_damaged_containers_never_raise(tmp_path, data):
    for name in ("a.raf", "a.cr3"):
        p = tmp_path / name; p.write_bytes(data)
        assert rawmeta.raf_jpeg(p) is None and rawmeta.cr3_preview(p) is None and rawmeta.cr3_ifds(p) is None
        assert features.exif_info(p)["taken_at"] is None


def test_a_cr3_and_raf_scan_with_a_date_and_a_body(tmp_path):
    make_cr3(tmp_path / "IMG_0001.CR3"); make_raf(tmp_path / "DSCF0001.RAF")
    st = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert st["indexed"] == 2 and st["errors"] == 0
    rows = {r[0]: r[1:] for r in db.connect(tmp_path).execute("SELECT rel, taken_at, camera, iso FROM photos")}
    assert rows["IMG_0001.CR3"] == ("2026-03-04T05:06:07", "Canon EOS R6m2", 3200)
    assert rows["DSCF0001.RAF"] == ("2026-01-02T03:04:05", "FUJIFILM X-T5", 800)


def test_a_ten_bit_hif_scans(tmp_path):
    import pillow_heif
    a = (np.random.default_rng(0).random((200, 300, 3)) * 65535).astype(np.uint16)
    h = pillow_heif.from_bytes(mode="RGB;16", size=(300, 200), data=a.tobytes())
    h.save(str(tmp_path / "C0001.HIF"), quality=80)
    assert [f.rel for f in find_images(tmp_path)] == ["C0001.HIF"]
    assert index_folder(tmp_path, faces=False, workers=1, embed=False)["indexed"] == 1


def _clip(path: Path, *args):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2", *args, str(path)],
                   check=True, capture_output=True)
    return path


@needs_ffmpeg
def test_a_real_avchd_clip_with_192_byte_packets_is_kept(tmp_path):
    """The case the old check turned away: sync at 4 and 196, the way a camcorder and ffmpeg's m2ts mode write it."""
    p = _clip(tmp_path / "00000.MTS", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts", "-mpegts_m2ts_mode", "1")
    assert p.read_bytes()[4] == 0x47 and p.read_bytes()[196] == 0x47
    assert looks_like_transport_stream(p)
    shutil.copy(p, tmp_path / "00001.M2TS")
    assert [f.rel for f in find_images(tmp_path)] == ["00000.MTS", "00001.M2TS"]


@needs_ffmpeg
def test_the_new_clip_formats_scan(tmp_path):
    x264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "12"]   # a GOP a camera would use: input seeking needs keyframes
    _clip(tmp_path / "a.m2ts", *x264, "-f", "mpegts", "-mpegts_m2ts_mode", "1")
    _clip(tmp_path / "b.mkv", *x264)
    _clip(tmp_path / "c.mpg", "-c:v", "mpeg2video", "-f", "mpeg")
    _clip(tmp_path / "d.3gp", *x264, "-f", "3gp")
    _clip(tmp_path / "e.mxf", "-c:v", "mpeg2video", "-f", "mxf")
    try:
        _clip(tmp_path / "f.webm", "-c:v", "libvpx", "-f", "webm")
    except subprocess.CalledProcessError:
        pass                                             # an ffmpeg built without libvpx
    st = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert st["errors"] == 0 and st["indexed"] == len(list(tmp_path.iterdir()))
    assert {r[0] for r in db.connect(tmp_path).execute("SELECT kind FROM photos")} == {"video"}


def test_an_unproven_raw_that_will_not_decode_is_listed_and_not_dropped(tmp_path):
    for ext in ("PEF", "3FR", "IIQ", "X3F", "SRW"):
        (tmp_path / f"f.{ext}").write_bytes(b"not really a raw" * 64)
    st = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert st["total"] == 5 and st["errors"] == 5          # was: total 0, not one word


def test_what_is_left_out_is_named_on_the_scan_tab(tmp_path):
    make_image(tmp_path, "a.jpg", size=(300, 200))
    for i in range(2):
        (tmp_path / f"A00{i}.R3D").write_bytes(b"x" * 100)
    (tmp_path / "clip.braw").write_bytes(b"x" * 100)
    (tmp_path / "empty.jpg").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not a camera file, not worth a line")
    (tmp_path / "x.d.mts").write_text("export declare const a: 1;\n" * 20)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    sc = TestClient(create_app(tmp_path)).get("/api/folder").json()["scan"]
    got = {x["why"]: x["n"] for x in sc["left_out"]}
    assert got == {"RED clips (R3D), which sorted cannot read": 2, "Blackmagic RAW clips (BRAW), which sorted cannot read": 1,
                   "empty files": 1}
    assert sc["left_out"][0]["examples"] == ["A000.R3D", "A001.R3D"]
    for why in got:
        assert "--" not in why and "—" not in why


def test_the_skipped_line_is_on_the_scan_tab():
    ui = Path(__file__).resolve().parent.parent / "photosort" / "ui"
    assert 'id="scan-skipped"' in (ui / "index.html").read_text()
    assert "renderSkipped" in (ui / "app.js").read_text()
