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


def test_pairs_raw_in_sibling_folder_when_the_counter_rolled_over(tmp_path):
    """Day1/RAW/DSC00001.ARW and Day2/RAW/DSC00001.ARW (two cards, or a counter that wrapped) each pair with
    the JPEG under their own day's JPG/ folder. Before, a stem seen twice was left loose on both sides and
    every one of those RAWs was decoded on top of its JPEG. A stem that is unique across the tree still pairs
    across days; a stem repeated within one day with no same-day JPEG stays a RAW entry."""
    for day in ("Day1", "Day2"):
        (tmp_path / day / "JPG").mkdir(parents=True); (tmp_path / day / "RAW").mkdir()
        for i in range(3):
            (tmp_path / day / "JPG" / f"DSC0000{i}.JPG").write_bytes(b"x")
            (tmp_path / day / "RAW" / f"DSC0000{i}.ARW").write_bytes(b"y")
    (tmp_path / "Day1" / "RAW" / "DSC00009.ARW").write_bytes(b"y")        # its JPEG sits under another day
    (tmp_path / "Day2" / "JPG" / "DSC00009.JPG").write_bytes(b"x")
    (tmp_path / "Day2" / "RAW" / "DSC00007.ARW").write_bytes(b"y")        # no JPEG anywhere
    files = {f.rel: f for f in find_images(tmp_path)}
    assert sorted(files) == ["Day1/JPG/DSC00000.JPG", "Day1/JPG/DSC00001.JPG", "Day1/JPG/DSC00002.JPG",
                             "Day2/JPG/DSC00000.JPG", "Day2/JPG/DSC00001.JPG", "Day2/JPG/DSC00002.JPG",
                             "Day2/JPG/DSC00009.JPG", "Day2/RAW/DSC00007.ARW"]
    for day in ("Day1", "Day2"):
        for i in range(3):
            assert files[f"{day}/JPG/DSC0000{i}.JPG"].sibling == f"{day}/RAW/DSC0000{i}.ARW"
    assert files["Day2/JPG/DSC00009.JPG"].sibling == "Day1/RAW/DSC00009.ARW"
    assert files["Day2/RAW/DSC00007.ARW"].is_raw and files["Day2/RAW/DSC00007.ARW"].sibling is None


# ===== the whole Mac as the shoot =====
def test_prune_mac_keeps_the_walk_out_of_system_and_library(tmp_path, monkeypatch):
    from pathlib import Path
    from photosort.walk import _prune_mac
    monkeypatch.setenv("PHOTOSORT_HOME", str(tmp_path / "apphome"))
    # the disk itself: only Users
    assert _prune_mac(Path("/"), ["Users", "System", "Library", "Volumes", "private", "Applications"]) == ["Users"]
    # a home folder: everything but Library. node_modules is dropped too, by DEV_DIRS in find_images
    # rather than here, because a package tree is never a shoot wherever it sits, not only on a Mac walk.
    assert _prune_mac(Path("/Users/k"), ["Desktop", "Library", "Pictures"]) == ["Desktop", "Pictures"]
    # a folder named Library deeper down is an ordinary folder
    assert _prune_mac(Path("/Users/k/Desktop"), ["Library", "shoot"]) == ["Library", "shoot"]
    # a Photos library package: originals only
    assert _prune_mac(Path("/Users/k/Pictures/Photos Library.photoslibrary"), ["originals", "resources", "database"]) == ["originals"]
    # the app's own home (thumbnails of every shoot) wherever it sits
    assert _prune_mac(tmp_path, ["apphome", "shoot"]) == ["shoot"]


def test_find_images_skips_node_modules_and_the_app_home(tmp_path, monkeypatch):
    from conftest import make_image
    from photosort.walk import find_images
    monkeypatch.setenv("PHOTOSORT_HOME", str(tmp_path / "apphome"))
    for sub in ("shoot", "node_modules/pkg", "apphome/thumbs"):
        (tmp_path / sub).mkdir(parents=True)
    make_image(tmp_path / "shoot", "a.jpg", seed=1)
    make_image(tmp_path / "node_modules" / "pkg", "b.jpg", seed=2)
    make_image(tmp_path / "apphome" / "thumbs", "c.jpg", seed=3)
    assert [f.rel for f in find_images(tmp_path)] == ["shoot/a.jpg"]


# ===== files that would only ever become unreadable rows: never picked up in the first place =====

def test_a_typescript_declaration_is_not_a_camcorder_clip(tmp_path):
    """.mts is two formats at once: an AVCHD clip, and the TypeScript ES module declaration that a package
    tree holds thousands of. On one real home folder those were 423 of 739 unreadable files."""
    from photosort.walk import find_images, looks_like_transport_stream
    ts = tmp_path / "gen-mapping.d.mts"
    ts.write_text("export declare function foo(): void;\n" * 20)
    clip = tmp_path / "00000.MTS"
    clip.write_bytes(bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 200)
    assert looks_like_transport_stream(clip) and not looks_like_transport_stream(ts)
    assert [f.rel for f in find_images(tmp_path)] == ["00000.MTS"]

def test_an_avchd_clip_with_the_four_byte_header_is_still_a_clip(tmp_path):
    from photosort.walk import looks_like_transport_stream
    p = tmp_path / "a.mts"
    p.write_bytes(b"\x00\x00\x00\x00" + bytes([0x47]) + b"\x00" * 187 + bytes([0x47]) + b"\x00" * 200)
    assert looks_like_transport_stream(p)

def test_an_empty_file_is_skipped(tmp_path):
    from photosort.walk import find_images
    from conftest import make_image
    make_image(tmp_path, "real.jpg")
    (tmp_path / "empty.jpg").write_bytes(b"")
    assert [f.rel for f in find_images(tmp_path)] == ["real.jpg"]

def test_package_and_build_trees_are_never_walked(tmp_path):
    """node_modules was already skipped on a whole-Mac scan only. These folders never hold a shoot and are
    full of files with photo and video extensions, so they are skipped wherever they are."""
    from photosort.walk import find_images
    from conftest import make_image
    make_image(tmp_path, "keep.jpg")
    for junk in ["node_modules", "site-packages", "__pycache__", "Caches", "DerivedData"]:
        d = tmp_path / "project" / junk
        d.mkdir(parents=True)
        make_image(d, "icon.jpg")
    assert [f.rel for f in find_images(tmp_path)] == ["keep.jpg"]
