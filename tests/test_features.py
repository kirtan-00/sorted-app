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
