import numpy as np
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
    assert _dms_to_deg(("23/1", "1/1", "2345/100"), "N") == lat      # the pyexiv2 string shape
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
