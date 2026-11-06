"""Exports carry the marks out: a sidecar beside every exported copy of an item with stars, a label or a reject, the explicit Write XMP action,
and the CSV columns. The shoot is never written to."""
import csv, os
from pathlib import Path
import pytest
from photosort import db, xmp
from photosort.index import index_folder
from photosort.export import export_ids, export_categories, write_xmp, export_dir
from photosort.config import export_root


def _shoot(tmp_path, names=("a.jpg", "b.jpg", "c.jpg")):
    from conftest import make_image
    for k, n in enumerate(names):
        (tmp_path / n).parent.mkdir(parents=True, exist_ok=True)
        make_image(tmp_path, n, seed=k)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    return conn, {r["rel"]: r["id"] for r in conn.execute("SELECT id, rel FROM photos")}


def _tree(p: Path):
    return sorted(str(x.relative_to(p)) for x in p.rglob("*"))


def _rating(p: Path):
    import re
    return int(re.search(r'xmp:Rating="(-?\d)"', p.read_text("utf-8")).group(1))


def test_a_copy_export_writes_a_sidecar_beside_each_marked_copy(tmp_path):
    conn, ids = _shoot(tmp_path)
    before = _tree(tmp_path)
    db.set_marks(conn, [ids["a.jpg"]], flag="pick", rating=4, label="red")
    db.set_marks(conn, [ids["b.jpg"]], flag="reject")
    out = export_ids(tmp_path, list(ids.values()), "sel")
    assert sorted(os.listdir(out)) == ["a.jpg", "a.xmp", "b.jpg", "b.xmp", "c.jpg"]     # unmarked c gets none
    assert _rating(out / "a.xmp") == 4 and 'xmp:Label="Red"' in (out / "a.xmp").read_text("utf-8")
    assert _rating(out / "b.xmp") == -1
    assert _tree(tmp_path) == before                                                   # the shoot is untouched


def test_a_link_export_puts_the_sidecar_in_the_export_folder_not_beside_the_original(tmp_path):
    conn, ids = _shoot(tmp_path)
    before = _tree(tmp_path)
    db.set_marks(conn, [ids["a.jpg"]], rating=3)
    out = export_ids(tmp_path, [ids["a.jpg"]], "links", "symlink")
    assert (out / "a.jpg").is_symlink() and (out / "a.xmp").is_file() and not (out / "a.xmp").is_symlink()
    assert _tree(tmp_path) == before


def test_a_re_export_refreshes_the_sidecar_and_clears_one_whose_marks_came_off(tmp_path):
    conn, ids = _shoot(tmp_path)
    a = ids["a.jpg"]
    db.set_marks(conn, [a], rating=2)
    out = export_ids(tmp_path, [a], "sel")
    db.set_marks(conn, [a], rating=5)
    export_ids(tmp_path, [a], "sel")                                  # the copy is skipped, the sidecar is not
    assert sorted(os.listdir(out)) == ["a.jpg", "a.xmp"] and _rating(out / "a.xmp") == 5
    db.set_marks(conn, [a], rating=0)
    export_ids(tmp_path, [a], "sel")
    assert _rating(out / "a.xmp") == 0


def test_an_export_never_overwrites_a_sidecar_it_did_not_write(tmp_path):
    conn, ids = _shoot(tmp_path)
    db.set_marks(conn, [ids["a.jpg"]], rating=2)
    out = export_dir(tmp_path, "sel"); out.mkdir(parents=True)
    (out / "a.xmp").write_text("lightroom develop settings")
    export_ids(tmp_path, [ids["a.jpg"]], "sel")
    assert (out / "a.xmp").read_text() == "lightroom develop settings"
    assert "a.xmp" in (out / "xmp-skipped.txt").read_text()


def test_a_name_collision_gets_its_sidecar_under_its_own_new_name(tmp_path):
    """Two files called a.jpg in two card folders: the second lands as <id>_a.jpg and its sidecar follows it."""
    conn, ids = _shoot(tmp_path, names=("A/a.jpg", "B/a.jpg"))
    db.set_marks(conn, [ids["A/a.jpg"]], rating=1); db.set_marks(conn, [ids["B/a.jpg"]], rating=5)
    out = export_ids(tmp_path, sorted(ids.values()), "sel")
    second = f"{ids['B/a.jpg']}_a"
    assert sorted(os.listdir(out)) == sorted(["a.jpg", "a.xmp", f"{second}.jpg", f"{second}.xmp"])
    assert _rating(out / "a.xmp") == 1 and _rating(out / f"{second}.xmp") == 5


def test_xmp_false_writes_no_sidecars(tmp_path):
    conn, ids = _shoot(tmp_path)
    db.set_marks(conn, [ids["a.jpg"]], rating=4)
    out = export_ids(tmp_path, [ids["a.jpg"]], "sel", xmp=False)
    assert os.listdir(out) == ["a.jpg"]


def test_a_category_export_writes_sidecars_in_each_category_folder(tmp_path):
    conn, ids = _shoot(tmp_path)
    conn.execute("UPDATE photos SET category='beach', category_score=0.9"); conn.commit()
    db.set_marks(conn, [ids["a.jpg"]], flag="pick", rating=5)
    out = export_categories(tmp_path, ["beach"])
    assert (out / "beach" / "a.xmp").is_file() and not (out / "beach" / "b.xmp").exists()


def test_csv_export_carries_the_marks(tmp_path):
    conn, ids = _shoot(tmp_path)
    db.set_marks(conn, [ids["a.jpg"]], flag="pick", rating=4, label="green")
    out = export_ids(tmp_path, list(ids.values()), "csv", "csv")
    rows = {Path(r["path"]).name: r for r in csv.DictReader(open(out / "photos.csv"))}
    assert (rows["a.jpg"]["flag"], rows["a.jpg"]["rating"], rows["a.jpg"]["label"]) == ("pick", "4", "green")
    assert (rows["b.jpg"]["flag"], rows["b.jpg"]["rating"], rows["b.jpg"]["label"]) == ("", "0", "")


def test_write_xmp_mirrors_the_shoot_tree_in_the_export_folder(tmp_path):
    conn, ids = _shoot(tmp_path, names=("card1/DSC_1.jpg", "card2/DSC_2.jpg", "card2/DSC_3.jpg"))
    before = _tree(tmp_path)
    db.set_marks(conn, [ids["card1/DSC_1.jpg"]], rating=3)
    db.set_marks(conn, [ids["card2/DSC_2.jpg"]], flag="reject")
    out, n = write_xmp(tmp_path, list(ids.values()))
    assert out == export_root() / tmp_path.resolve().name / "xmp"
    assert _tree(out) == ["card1", "card1/DSC_1.xmp", "card2", "card2/DSC_2.xmp"]
    assert n == {"written": 2, "foreign": 0, "clash": 0, "unmarked": 1}
    assert _rating(out / "card2" / "DSC_2.xmp") == -1
    assert _tree(tmp_path) == before


def test_write_xmp_refuses_a_destination_inside_the_shoot(tmp_path):
    conn, ids = _shoot(tmp_path)
    db.set_marks(conn, list(ids.values()), rating=3)
    before = _tree(tmp_path)
    for base in (tmp_path, tmp_path / "sub"):
        with pytest.raises(ValueError):
            write_xmp(tmp_path, list(ids.values()), base=base)
    assert _tree(tmp_path) == before


def test_trimmed_segments_of_a_marked_clip_carry_its_sidecar(tmp_path, tmp_path_factory):
    from conftest import make_video, FFMPEG
    from photosort.export import export_segments
    if FFMPEG is None:
        pytest.skip("ffmpeg not installed")
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    before = _tree(tmp_path)
    conn = db.connect(tmp_path)
    pid = conn.execute("SELECT id FROM photos WHERE rel='clip.mp4'").fetchone()[0]
    db.set_marks(conn, [pid], flag="pick", label="blue")
    out = export_segments(tmp_path, [pid], None, "copy", tmp_path_factory.mktemp("out"))
    assert sorted(p.name for p in out.iterdir()) == ["clip_00_0.0-5.0.mp4", "clip_00_0.0-5.0.xmp",
                                                      "clip_01_5.0-10.0.mp4", "clip_01_5.0-10.0.xmp"]
    assert 'xmp:Label="Blue"' in (out / "clip_00_0.0-5.0.xmp").read_text("utf-8")
    assert _tree(tmp_path) == before
