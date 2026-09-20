from PIL import Image
from photosort.decode import load_preview, DecodeError
import pytest

def test_jpeg_downscaled(make_img):
    p = make_img(name="big.jpg", size=(4000, 3000))
    im = load_preview(p)
    assert im.mode == "RGB" and max(im.size) <= 1024 and min(im.size) >= 700

def test_small_not_upscaled(make_img):
    p = make_img(name="s.png", size=(300, 200))
    assert load_preview(p).size == (300, 200)

def test_exif_orientation_applied(tmp_path):
    im = Image.new("RGB", (400, 200), "red")
    exif = Image.Exif(); exif[0x0112] = 6  # rotate 90 CW
    p = tmp_path / "o.jpg"; im.save(p, exif=exif)
    assert load_preview(p).size == (200, 400)

def test_bad_file_raises(tmp_path):
    p = tmp_path / "bad.jpg"; p.write_bytes(b"not an image")
    with pytest.raises(DecodeError):
        load_preview(p)
