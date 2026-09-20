from pathlib import Path
from photosort.walk import find_images, quick_hash

def test_finds_and_pairs(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "a.ARW").write_bytes(b"y" * 10)
    (tmp_path / "b.nef").write_bytes(b"z" * 10)
    (tmp_path / ".photosort").mkdir(); (tmp_path / ".photosort" / "t.jpg").write_bytes(b"q")
    (tmp_path / "notes.txt").write_text("no")
    files = find_images(tmp_path)
    rels = sorted(f.rel for f in files)
    assert rels == ["a.jpg", "b.nef"]
    a = next(f for f in files if f.rel == "a.jpg")
    assert a.sibling == "a.ARW" and a.is_raw is False
    assert next(f for f in files if f.rel == "b.nef").is_raw is True

def test_quick_hash_changes_with_content(tmp_path):
    p = tmp_path / "x.jpg"; p.write_bytes(b"a" * 200_000)
    h1 = quick_hash(p); p.write_bytes(b"a" * 199_999 + b"b"); h2 = quick_hash(p)
    assert h1 != h2 and len(h1) == 40

def test_unstatable_file_is_skipped(tmp_path):
    (tmp_path / "ok.jpg").write_bytes(b"x" * 10)
    (tmp_path / "gone.jpg").symlink_to(tmp_path / "does-not-exist.jpg")   # stat() raises OSError
    assert [f.rel for f in find_images(tmp_path)] == ["ok.jpg"]


def test_pairs_raw_in_sibling_folder(tmp_path):
    (tmp_path / "Day1" / "JPG").mkdir(parents=True); (tmp_path / "Day1" / "RAW").mkdir()
    (tmp_path / "Day1" / "JPG" / "DSC01.jpg").write_bytes(b"x")
    (tmp_path / "Day1" / "RAW" / "DSC01.ARW").write_bytes(b"y")
    (tmp_path / "Day2").mkdir()
    (tmp_path / "Day2" / "DSC02.jpg").write_bytes(b"x"); (tmp_path / "Day2" / "DSC02.jpg.bak").write_bytes(b"q")
    (tmp_path / "Day2" / "DSC03.nef").write_bytes(b"z")           # no JPEG anywhere: stays as a RAW entry
    files = find_images(tmp_path)
    rels = sorted(f.rel for f in files)
    assert rels == ["Day1/JPG/DSC01.jpg", "Day2/DSC02.jpg", "Day2/DSC03.nef"]
    assert next(f for f in files if f.rel == "Day1/JPG/DSC01.jpg").sibling == "Day1/RAW/DSC01.ARW"


def test_videos_are_found_and_never_paired_with_a_raw(tmp_path):
    from photosort.walk import find_images
    (tmp_path / "clip.MP4").write_bytes(b"v" * 10)
    (tmp_path / "clip.ARW").write_bytes(b"y" * 10)       # same stem as the video: the RAW stays its own entry
    (tmp_path / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "b.mov").write_bytes(b"w" * 10)
    (tmp_path / ".hidden.mp4").write_bytes(b"h")
    (tmp_path / "photosort-out").mkdir(); (tmp_path / "photosort-out" / "old.mp4").write_bytes(b"o")
    files = {f.rel: f for f in find_images(tmp_path)}
    assert sorted(files) == ["a.jpg", "b.mov", "clip.ARW", "clip.MP4"]
    assert files["clip.MP4"].is_video is True and files["clip.MP4"].is_raw is False and files["clip.MP4"].sibling is None
    assert files["clip.ARW"].is_raw is True and files["clip.ARW"].sibling is None
    assert files["a.jpg"].is_video is False and files["b.mov"].is_video is True


def test_sony_card_bookkeeping_folders_are_skipped(tmp_path):
    """A Sony card (PRIVATE/M4ROOT) carries one poster JPEG per clip under THMBNL/, proxy clips under SUB/
    and bookkeeping under TAKE/ and GENERAL/. None of that is a photo or a clip of its own: the 12 "other"
    photos on DAY-4 were THMBNL posters. Those four names are pruned only directly under M4ROOT (any case):
    SUB, TAKE and GENERAL are ordinary words a client folder may use. photosort-out stays skipped anywhere."""
    from photosort.config import SKIP_DIRS, SONY_CARD_DIRS
    assert SONY_CARD_DIRS == {"THMBNL", "SUB", "TAKE", "GENERAL"} and "photosort-out" in SKIP_DIRS
    (tmp_path / "client" / "SUB").mkdir(parents=True); (tmp_path / "client" / "SUB" / "x.JPG").write_bytes(b"c" * 10)
    (tmp_path / "client" / "TAKE").mkdir(); (tmp_path / "client" / "TAKE" / "t.jpg").write_bytes(b"c" * 10)
    (tmp_path / "card2" / "m4root" / "THMBNL").mkdir(parents=True); (tmp_path / "card2" / "m4root" / "THMBNL" / "C0001T01.JPG").write_bytes(b"p")
    m4 = tmp_path / "PRIVATE" / "M4ROOT"
    for d in ("CLIP", "THMBNL", "SUB", "TAKE", "GENERAL"):
        (m4 / d).mkdir(parents=True)
    (m4 / "CLIP" / "C0011.MP4").write_bytes(b"v" * 10)
    (m4 / "CLIP" / "C0011M01.XML").write_text("<x/>")
    (m4 / "THMBNL" / "C0011T01.JPG").write_bytes(b"p" * 10)
    (m4 / "SUB" / "C0011S03.MP4").write_bytes(b"s" * 10)
    (m4 / "TAKE" / "T0001.jpg").write_bytes(b"t" * 10)
    (m4 / "GENERAL" / "G0001.mp4").write_bytes(b"g" * 10)
    (tmp_path / "photosort-out" / "x").mkdir(parents=True); (tmp_path / "photosort-out" / "x" / "old.jpg").write_bytes(b"o")
    (tmp_path / "DCIM").mkdir(); (tmp_path / "DCIM" / "DSC00001.JPG").write_bytes(b"j" * 10)
    assert [f.rel for f in find_images(tmp_path)] == ["DCIM/DSC00001.JPG", "PRIVATE/M4ROOT/CLIP/C0011.MP4",
                                                       "client/SUB/x.JPG", "client/TAKE/t.jpg"]


def test_dji_clip_with_srt_sidecar_is_listed_once(tmp_path):
    (tmp_path / "DJI_0001.MP4").write_bytes(b"v" * 10)
    (tmp_path / "DJI_0001.SRT").write_text("1\n[iso : 100] [shutter : 1/1000] [fnum : 2.8]\n")
    (tmp_path / "DJI_0002.DNG").write_bytes(b"r" * 10); (tmp_path / "DJI_0002.JPG").write_bytes(b"j" * 10)
    files = find_images(tmp_path)
    assert [f.rel for f in files] == ["DJI_0001.MP4", "DJI_0002.JPG"]
    assert files[0].is_video and files[1].sibling == "DJI_0002.DNG"
