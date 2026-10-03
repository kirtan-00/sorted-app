import os
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from photosort.decode import load_preview
from photosort.features import phash, exif_info, sharpness_tiles, eye_sharpness, to_gray

def test_sharp_beats_blurry(make_img):
    s = to_gray(load_preview(make_img(name="s.jpg", kind="sharp")))
    b = to_gray(load_preview(make_img(name="b.jpg", kind="blurry")))
    assert sharpness_tiles(s)[0] > 5 * sharpness_tiles(b)[0]

def test_phash_stable_under_resize(make_img):
    p = make_img(name="p.jpg")
    im = Image.open(p)
    import imagehash
    h1 = phash(im); h2 = phash(im.resize((800, 600)))
    assert len(h1) == 16 and (imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)) <= 4

def test_exif_missing_is_none(make_img):
    info = exif_info(make_img(name="e.jpg"))
    assert info["taken_at"] is None and info["width"] == 1600

def test_eye_sharpness_uses_eye_region():
    gray = np.zeros((400, 400), np.uint8)
    gray[180:220, 120:280] = (np.random.default_rng(0).random((40, 160)) * 255).astype(np.uint8)
    lm = np.array([[150, 200], [250, 200], [200, 260], [170, 320], [230, 320]], float)
    assert eye_sharpness(gray, lm) > 100
    assert eye_sharpness(np.zeros((400, 400), np.uint8), lm) == 0.0

def test_exif_present(tmp_path):
    im = Image.new("RGB", (640, 480), "gray")
    ex = Image.Exif()
    ex[0x010F] = "SONY"; ex[0x0110] = "ILCE-7M4"
    ex[0x0132] = "2024:03:09 09:00:00"                      # DateTime: when the file was last edited
    ex.get_ifd(0x8769)[0x9003] = "2024:03:05 14:22:10"     # DateTimeOriginal: when the shutter fired
    p = tmp_path / "x.jpg"; im.save(p, exif=ex)
    info = exif_info(p)
    assert info["taken_at"] == "2024-03-05T14:22:10"       # original wins
    assert info["camera"] == "SONY ILCE-7M4"
    assert (info["width"], info["height"]) == (640, 480)

def test_exif_falls_back_to_datetime(tmp_path):
    im = Image.new("RGB", (64, 48), "gray")
    ex = Image.Exif(); ex[0x0132] = "2024:03:09 09:00:00"
    p = tmp_path / "y.jpg"; im.save(p, exif=ex)
    assert exif_info(p)["taken_at"] == "2024-03-09T09:00:00"

def test_exif_aerial_from_make_or_filename(tmp_path):
    """A DJI photo is aerial by its EXIF Make or its DJI_ filename; a Sony still is not."""
    im = Image.new("RGB", (64, 48), "gray")
    ex = Image.Exif(); ex[0x010F] = "DJI"; ex[0x0110] = "FC8482"
    p = tmp_path / "IMG_0001.JPG"; im.save(p, exif=ex)
    info = exif_info(p)
    assert info["aerial"] is True and info["camera"] == "DJI FC8482"
    im.save(tmp_path / "dji_0002.jpg")
    assert exif_info(tmp_path / "dji_0002.jpg")["aerial"] is True
    ex2 = Image.Exif(); ex2[0x010F] = "SONY"; ex2[0x0110] = "ILCE-7SM3"
    im.save(tmp_path / "DSC00001.JPG", exif=ex2)
    assert exif_info(tmp_path / "DSC00001.JPG")["aerial"] is False
    assert exif_info(tmp_path / "DSC00001.JPG")["camera"] == "SONY ILCE-7SM3"

# ===== GPS: the DMS converter, the validity gate, ISO 6709 for clips, and the lat/lon columns =====

def test_dms_to_deg_both_hemispheres():
    """Ahmedabad (north east) stays positive; the same numbers read S and W come back negative."""
    from photosort.features import _dms_to_deg
    lat = _dms_to_deg((23, 1, 23.45), "N")
    lon = _dms_to_deg((72, 34, 17.12), "E")
    assert abs(lat - 23.02318) < 1e-5 and abs(lon - 72.57142) < 1e-5
    assert _dms_to_deg((23, 1, 23.45), "S") == -lat
    assert _dms_to_deg((72, 34, 17.12), "W") == -lon
    assert _dms_to_deg(("23/1", "1/1", "2345/100"), "N") == lat      # "a/b" strings still parse
    assert _dms_to_deg((23, 1.5), "N") == 23.025                      # degrees and decimal minutes only

def test_dms_to_deg_rejects_junk_without_raising():
    from photosort.features import _dms_to_deg
    assert _dms_to_deg((), "N") is None
    assert _dms_to_deg(("north", 1, 2), "N") is None
    assert _dms_to_deg(("23/0", 1, 2), "N") is None                   # a zero denominator
    assert _dms_to_deg((23, 1, 2), "X") is None                       # not a compass letter
    assert _dms_to_deg((23, 1, 2), None) is None

def test_latlon_gate_rejects_null_island_and_out_of_range():
    from photosort.features import latlon_ok
    assert latlon_ok(23.02, 72.57) == (23.02, 72.57)
    assert latlon_ok(0.0, 0.0) == (None, None)                        # the "no fix" write
    assert latlon_ok(91.0, 10.0) == (None, None)
    assert latlon_ok(10.0, -181.0) == (None, None)
    assert latlon_ok(float("nan"), 10.0) == (None, None)              # nan passes every range test
    assert latlon_ok(None, 10.0) == (None, None)

def test_exif_reads_gps(tmp_path):
    """A JPEG written with a GPS IFD reads back as decimal degrees; one without has lat/lon None."""
    from PIL.TiffImagePlugin import IFDRational
    im = Image.new("RGB", (64, 48), "gray")
    ex = Image.Exif(); gps = ex.get_ifd(0x8825)
    gps[1] = "N"; gps[2] = (IFDRational(23, 1), IFDRational(1, 1), IFDRational(2345, 100))
    gps[3] = "E"; gps[4] = (IFDRational(72, 1), IFDRational(34, 1), IFDRational(1712, 100))
    p = tmp_path / "gps.jpg"; im.save(p, exif=ex)
    info = exif_info(p)
    assert abs(info["lat"] - 23.02318) < 1e-5 and abs(info["lon"] - 72.57142) < 1e-5
    im.save(tmp_path / "nogps.jpg")
    assert exif_info(tmp_path / "nogps.jpg")["lat"] is None

def test_exif_gps_no_fix_is_dropped(tmp_path):
    """A camera that writes 0/0/0 with refs but never had a fix must not land the shoot in the Atlantic."""
    from PIL.TiffImagePlugin import IFDRational
    im = Image.new("RGB", (64, 48), "gray")
    ex = Image.Exif(); gps = ex.get_ifd(0x8825)
    gps[1] = "N"; gps[2] = (IFDRational(0, 1), IFDRational(0, 1), IFDRational(0, 1))
    gps[3] = "E"; gps[4] = (IFDRational(0, 1), IFDRational(0, 1), IFDRational(0, 1))
    p = tmp_path / "zero.jpg"; im.save(p, exif=ex)
    info = exif_info(p)
    assert info["lat"] is None and info["lon"] is None

def test_iso6709_parses_apple_and_plain_location_shapes():
    """The clip side: both tag shapes, with and without the altitude segment, plus the southern hemisphere."""
    from photosort.video import _iso6709
    lat, lon = _iso6709("+23.0225+072.5714+055.000/")
    assert lat == 23.0225 and lon == 72.5714
    assert _iso6709("+23.0225+072.5714/") == (23.0225, 72.5714)
    assert _iso6709("+23.0225+072.5714") == (23.0225, 72.5714)
    assert _iso6709("-33.8688+151.2093+019.000/") == (-33.8688, 151.2093)

def test_iso6709_rejects_junk_and_no_fix():
    from photosort.video import _iso6709
    assert _iso6709(None) == (None, None)
    assert _iso6709("") == (None, None)
    assert _iso6709("somewhere nice") == (None, None)
    assert _iso6709("+00.0000+000.0000/") == (None, None)
    assert _iso6709("+91.5000+072.5714/") == (None, None)

def test_latlon_columns_migrate_and_round_trip(tmp_path):
    """Connecting twice adds lat/lon once and does not error the second time, and a row keeps what it stored."""
    from photosort import db
    conn = db.connect(tmp_path); conn.close()
    conn = db.connect(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    assert "lat" in cols and "lon" in cols
    row = dict(rel="g.heic", size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10, taken_at=None,
               camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0,
               n_faces=0, status="ok", lat=23.0225, lon=72.5714)
    pid = db.upsert_photo(conn, row)
    got = conn.execute("SELECT lat, lon FROM photos WHERE id=?", (pid,)).fetchone()
    assert got[0] == 23.0225 and got[1] == 72.5714
    db.upsert_photo(conn, dict(row, lat=None, lon=None))              # metadata is refreshed, not cleared, on re-index
    assert conn.execute("SELECT lat FROM photos WHERE id=?", (pid,)).fetchone()[0] is None

# ===== The fallback reader: what exif_info reads when PIL cannot open the file at all =====
# PIL opens no Sony ARW, so every ARW lands here. The four files in tests/fixtures/exif are the first 8 to
# 32 KB of real camera files from raw.pixls.us (CC0): the TIFF header and every IFD, none of the sensor data.
# Every expected value below was read from the whole file. A DJI DNG is a TIFF PIL can open, so the DNG
# tests make PIL refuse it to reach the same path a damaged one would.

FIX = Path(__file__).parent / "fixtures" / "exif"

def _blind_pil(monkeypatch):
    from PIL import UnidentifiedImageError
    def refuse(*a, **k):
        raise UnidentifiedImageError("refused by the test")
    monkeypatch.setattr("photosort.features.Image.open", refuse)

def _exif_jpeg(path, *, make="SONY", model="ILCE-7M4", original="2024:03:05 14:22:10", edited="2024:03:09 09:00:00",
               gps=True, dims=(7008, 4672)):
    from PIL.TiffImagePlugin import IFDRational as R
    ex = Image.Exif()
    if make: ex[0x010F] = make
    if model: ex[0x0110] = model
    if edited: ex[0x0132] = edited
    sub = ex.get_ifd(0x8769)
    if original: sub[0x9003] = original
    if dims: sub[0xA002], sub[0xA003] = dims
    if gps:                                                     # Sydney: south and west, so both signs flip
        g = ex.get_ifd(0x8825)
        g[1] = "S"; g[2] = (R(33, 1), R(52, 1), R(768, 100))
        g[3] = "W"; g[4] = (R(151, 1), R(12, 1), R(3348, 100))
    Image.new("RGB", (64, 48), "gray").save(path, exif=ex)
    return path

@pytest.mark.parametrize("name,camera,taken,size", [
    ("sony-ilce7sm3-DSC01568.ARW", "SONY ILCE-7SM3", "2022-04-10T17:25:40", (4240, 2832)),
    ("sony-ilce7m4-DSC06677.ARW", "SONY ILCE-7M4", "2023-10-19T16:15:24", (7008, 4672)),
])
def test_exif_fallback_reads_a_sony_arw(name, camera, taken, size):
    """No monkeypatching: PIL really cannot open an ARW, so this is the path every Sony raw takes."""
    info = exif_info(FIX / name)
    assert info["camera"] == camera and info["taken_at"] == taken
    assert (info["width"], info["height"]) == size
    assert info["lat"] is None and info["lon"] is None and info["aerial"] is False

@pytest.mark.parametrize("name,camera,taken,lat,lon", [
    ("dji-fc6310-0220.DNG", "DJI FC6310", "2017-12-27T14:32:13", 49.501443333, 14.772894),
    ("dji-fc7303-0109.DNG", "DJI FC7303", "2021-07-17T19:56:07", 37.296885583, -122.080652583),
])
def test_exif_fallback_reads_a_dji_dng(monkeypatch, name, camera, taken, lat, lon):
    """Aerial comes from the Make here: the fixture names do not start DJI_, so the filename rule is not it."""
    _blind_pil(monkeypatch)
    info = exif_info(FIX / name)
    assert info["camera"] == camera and info["taken_at"] == taken
    assert abs(info["lat"] - lat) < 1e-6 and abs(info["lon"] - lon) < 1e-6
    assert info["aerial"] is True
    # a DNG carries no PixelXDimension; the size comes from the full-resolution SubIFD, as on the PIL path
    assert (info["width"], info["height"]) == DJI_DNG_SIZE[name]

# A DJI DNG's IFD0 is the small preview (NewSubfileType 1, 160x120 or 256x192) and the sensor-sized frame
# sits in a SubIFD. PIL opens IFD0, so it used to report the preview's size, and it hands Make and Model
# back padded with NULs to their fixed field length ("DJI\x00\x00...", "FC7303\x00..."), which kept a DNG's
# camera from matching the JPG shot beside it.
DJI_DNG_SIZE = {"dji-fc6310-0220.DNG": (5472, 3648), "dji-fc7303-0109.DNG": (4000, 3000)}

@pytest.mark.parametrize("name,camera,taken,lat,lon", [
    ("dji-fc6310-0220.DNG", "DJI FC6310", "2017-12-27T14:32:13", 49.501443333, 14.772894),
    ("dji-fc7303-0109.DNG", "DJI FC7303", "2021-07-17T19:56:07", 37.296885583, -122.080652583),
])
def test_pil_reads_a_dji_dng_without_nuls_at_its_real_size(name, camera, taken, lat, lon):
    """No monkeypatching: PIL really opens a DNG, so this is the path every DJI raw takes."""
    info = exif_info(FIX / name)
    assert info["camera"] == camera
    assert (info["width"], info["height"]) == DJI_DNG_SIZE[name]
    assert info["taken_at"] == taken and info["aerial"] is True
    assert abs(info["lat"] - lat) < 1e-6 and abs(info["lon"] - lon) < 1e-6

def test_pil_strips_nuls_and_spaces_from_make_and_model(tmp_path):
    p = _exif_jpeg(tmp_path / "IMG_0002.JPG", make="DJI\x00\x00\x00 ", model=" FC7303\x00\x00", gps=False)
    info = exif_info(p)
    assert info["camera"] == "DJI FC7303" and info["aerial"] is True

def test_a_jpeg_keeps_its_own_size(tmp_path):
    """The SubIFD walk is for a file whose first image is a preview; an ordinary JPEG is untouched."""
    info = exif_info(_exif_jpeg(tmp_path / "c.jpg"))
    assert (info["width"], info["height"]) == (64, 48)

def test_subifd_size_never_raises_on_junk(tmp_path):
    from photosort.features import _subifd_size
    p = tmp_path / "x.dng"; p.write_bytes(b"II*\x00\x08\x00\x00\x00" + b"\xff" * 40)
    assert _subifd_size(p, [8, 9999, -1]) is None
    assert _subifd_size(tmp_path / "missing.dng", [8]) is None

def test_exif_fallback_reads_every_field_from_a_jpeg(tmp_path, monkeypatch):
    p = _exif_jpeg(tmp_path / "DSC00001.JPG")
    _blind_pil(monkeypatch)
    info = exif_info(p)
    assert info["taken_at"] == "2024-03-05T14:22:10"            # the shutter time, not the edit time
    assert info["camera"] == "SONY ILCE-7M4"                    # the same string the PIL branch writes
    assert (info["width"], info["height"]) == (7008, 4672)
    assert abs(info["lat"] - -33.8688) < 1e-6 and abs(info["lon"] - -151.2093) < 1e-6
    assert info["aerial"] is False

def test_exif_fallback_falls_back_to_datetime_and_model_alone(tmp_path, monkeypatch):
    p = _exif_jpeg(tmp_path / "a.jpg", make=None, original=None, gps=False, dims=None)
    _blind_pil(monkeypatch)
    info = exif_info(p)
    assert info["taken_at"] == "2024-03-09T09:00:00" and info["camera"] == "ILCE-7M4"
    assert info["lat"] is None and info["width"] is None

def test_exif_fallback_aerial_from_a_dji_make(tmp_path, monkeypatch):
    p = _exif_jpeg(tmp_path / "IMG_0001.JPG", make="DJI", model="FC8482", gps=False)
    _blind_pil(monkeypatch)
    info = exif_info(p)
    assert info["aerial"] is True and info["camera"] == "DJI FC8482"

def test_exif_fallback_keeps_the_size_pil_already_read(tmp_path, monkeypatch):
    """PIL opened the file and read its size, then the EXIF read blew up: the pixels PIL saw win over
    whatever PixelXDimension claims, and the fallback still fills the date and camera."""
    p = _exif_jpeg(tmp_path / "b.jpg")
    def boom(self):
        raise ValueError("damaged EXIF")
    monkeypatch.setattr(Image.Image, "getexif", boom)
    info = exif_info(p)
    assert (info["width"], info["height"]) == (64, 48)
    assert info["taken_at"] == "2024-03-05T14:22:10" and info["camera"] == "SONY ILCE-7M4"

def test_exif_fallback_on_junk_is_all_none(tmp_path):
    p = tmp_path / "DSC09999.ARW"; p.write_bytes(b"not an image at all" * 10)
    info = exif_info(p)
    assert all(info[k] is None for k in ("taken_at", "camera", "width", "height", "lat", "lon"))
    assert exif_info(tmp_path / "missing.ARW")["taken_at"] is None    # a vanished file does not raise either

def test_dms_to_deg_takes_fractions():
    """exifread hands GPS back as Ratio, a Fraction subclass."""
    from fractions import Fraction
    from photosort.features import _dms_to_deg
    assert abs(_dms_to_deg([Fraction(37), Fraction(17), Fraction(487881, 10000)], "N") - 37.296885583) < 1e-8

def test_no_gpl_metadata_reader_in_the_dependencies():
    """pyexiv2 is GPLv3 (and exiv2 under it GPL-2.0+); it blocked selling sorted closed source."""
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert "pyexiv2" not in text and "exifread" in text.lower()

@pytest.mark.skipif(not os.environ.get("SORTED_EXIF_SAMPLES"), reason="set SORTED_EXIF_SAMPLES to a folder of real camera files")
def test_exif_fallback_agrees_with_pil_on_real_files(monkeypatch):
    """Opt in: on every file PIL can read, the fallback must agree on date, camera and GPS; on the ones it
    cannot (ARW), the fallback must at least find a date and a camera."""
    import photosort.features as F
    real_open = F.Image.open
    files = sorted(p for p in Path(os.environ["SORTED_EXIF_SAMPLES"]).iterdir()
                   if p.suffix.lower() in (".arw", ".dng", ".jpg", ".jpeg"))
    assert files
    for p in files:
        try:
            with real_open(p):
                pil_ok = True
        except Exception:
            pil_ok = False
        _blind_pil(monkeypatch)
        fb = exif_info(p)
        monkeypatch.setattr("photosort.features.Image.open", real_open)
        assert fb["taken_at"] and fb["camera"], p.name
        if pil_ok:
            pil = exif_info(p)
            assert fb["taken_at"] == pil["taken_at"], p.name
            assert fb["camera"] == (pil["camera"] or "").replace("\x00", ""), p.name
            for k in ("lat", "lon"):
                assert (fb[k] is None) == (pil[k] is None), p.name
                assert fb[k] is None or abs(fb[k] - pil[k]) < 1e-9, p.name
