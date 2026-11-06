"""Flash off EXIF: the facet, the filter and the catch-up pass for shoots read before the column existed."""
from pathlib import Path
import numpy as np
from PIL import Image
from photosort import db
from photosort.features import exif_info
from photosort.index import index_folder, read_camera_info, camera_info_status
from photosort.search import Index, Filters, bucket


def save(path: Path, flash=None, seed=0):
    a = np.random.default_rng(seed).integers(0, 255, (240, 360, 3), dtype=np.uint8)
    ex = Image.Exif()
    sub = ex.get_ifd(0x8769)
    if flash is not None:
        sub[0x9209] = flash
    Image.fromarray(a).save(path, exif=ex, quality=90)
    return path


def test_exif_keeps_the_raw_value_and_tells_absent_from_unread(tmp_path):
    assert exif_info(save(tmp_path / "a.jpg", 0x19))["flash"] == 0x19
    assert exif_info(save(tmp_path / "b.jpg", 0x10))["flash"] == 0x10
    assert exif_info(save(tmp_path / "c.jpg"))["flash"] == -1


def test_bucket_reads_bit_zero_only():
    assert bucket("flash", {"flash": 1}) == "fired"
    assert bucket("flash", {"flash": 0x19}) == "fired"
    assert bucket("flash", {"flash": 0x10}) == "none"
    assert bucket("flash", {"flash": 0}) == "none"
    assert bucket("flash", {"flash": -1}) == "unknown"
    assert bucket("flash", {"flash": None}) == "unknown"


def test_filter_and_counts(tmp_path):
    save(tmp_path / "DSC1.JPG", 1, 1); save(tmp_path / "DSC2.JPG", 0x10, 2); save(tmp_path / "DSC3.JPG", None, 3)
    index_folder(tmp_path, faces=False)
    ix = Index(tmp_path)
    assert ix.facet_counts(Filters())["flash"] == {"fired": 1, "none": 1, "unknown": 1}


def test_catch_up_reads_a_shoot_scanned_before_flash(tmp_path):
    save(tmp_path / "DSC1.JPG", 1, 1); save(tmp_path / "DSC2.JPG", None, 2)
    index_folder(tmp_path, faces=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET flash=NULL"); conn.commit()
    assert camera_info_status(tmp_path)["unknown"] == 2
    read_camera_info(tmp_path)
    got = sorted(r[0] for r in conn.execute("SELECT flash FROM photos"))
    assert got == [-1, 1]
    assert camera_info_status(tmp_path)["unknown"] == 0


def test_flicker_facet_and_filter(tmp_path):
    save(tmp_path / "DSC1.JPG", 1, 1)
    index_folder(tmp_path, faces=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET kind='video', flicker='pulsing'"); conn.commit()
    ix = Index(tmp_path)
    assert ix.facet_counts(Filters())["flicker"] == {"pulsing": 1}
    assert ix.query(filters=Filters(flicker="pulsing")) and not ix.query(filters=Filters(flicker="none"))
