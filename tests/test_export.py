import os
from pathlib import Path
from photosort.index import index_folder
from photosort.search import Index
from photosort.export import export_ids

def test_export_copy_default_symlink_and_csv(tmp_path):
    import os
    from conftest import make_image
    from photosort.config import export_root
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids = [r["id"] for r in Index(tmp_path).search()]
    out = export_ids(tmp_path, ids, "test")
    assert out == export_root() / tmp_path.resolve().name / "test"
    assert (out / "a.jpg").is_file() and not (out / "a.jpg").is_symlink()
    assert (out / "a.jpg").read_bytes() == (tmp_path / "a.jpg").read_bytes()
    ln = export_ids(tmp_path, ids, "links", "symlink")
    assert (ln / "a.jpg").is_symlink() and (ln / "a.jpg").resolve() == (tmp_path / "a.jpg").resolve()
    out2 = export_ids(tmp_path, ids, "csv", "csv")
    assert "a.jpg" in (out2 / "photos.csv").read_text()
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]   # source folder untouched

def _one_photo(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    return [r["id"] for r in Index(tmp_path).search()]

def test_export_name_cannot_escape_export_root(tmp_path):
    import os, pytest
    from photosort.config import export_root
    ids = _one_photo(tmp_path)
    before = sorted(os.listdir(export_root()))
    for bad in ["../../x", "/tmp/x", "..", ".", "a//b", str(tmp_path / "inside")]:
        with pytest.raises(ValueError):
            export_ids(tmp_path, ids, bad)
    assert sorted(os.listdir(export_root())) == before          # nothing created anywhere
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]            # source untouched
    assert not (tmp_path.parent / "x").exists() and not Path("/tmp/x").exists()

def test_export_nested_name_is_sanitised_per_segment(tmp_path):
    from photosort.config import export_root
    ids = _one_photo(tmp_path)
    out = export_ids(tmp_path, ids, "people/Ar/ya")
    assert out.resolve().is_relative_to(export_root().resolve())
    assert out == export_root() / tmp_path.resolve().name / "people" / "Ar" / "ya"
    assert (out / "a.jpg").is_file()
    assert export_ids(tmp_path, ids, "").name == "export" and export_ids(tmp_path, ids, "  ").name == "export"
    out2 = export_ids(tmp_path, ids, "people/Ar:ya\\bad")
    assert out2.name == "Ar_ya_bad" and out2.parent.name == "people"

def test_export_skips_missing_photos(tmp_path):
    from photosort import db
    from conftest import make_image
    ids = _one_photo(tmp_path)
    make_image(tmp_path, "b.jpg", seed=2); index_folder(tmp_path, faces=False, workers=1, embed=False)
    (tmp_path / "a.jpg").unlink()
    index_folder(tmp_path, faces=False, workers=1, embed=False)   # marks a.jpg missing, b.jpg keeps the folder non-empty
    assert db.connect(tmp_path).execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "missing"
    out = export_ids(tmp_path, ids, "culled")                    # must not raise
    assert out.is_dir() and list(out.iterdir()) == []

def test_export_reports_progress_and_survives_a_bad_file(tmp_path):
    import os
    from conftest import make_image
    from photosort.export import export_bytes
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids = [r["id"] for r in Index(tmp_path).search()]
    assert export_bytes(tmp_path, ids) == (tmp_path / "a.jpg").stat().st_size + (tmp_path / "b.jpg").stat().st_size
    # b.jpg vanishes from the disk after indexing: copy must finish a.jpg and report one failure
    (tmp_path / "b.jpg").unlink()
    seen = []
    out = export_ids(tmp_path, ids, "partial", "copy", progress=seen.append)
    assert (out / "a.jpg").is_file() and not (out / "b.jpg").exists()
    assert seen[-1] == {"done": 2, "total": 2, "failed": 1, "skipped": 0}
    assert "b.jpg" in (out / "failed.txt").read_text()
    # links: os.symlink happily points at a missing file, so the missing source must be caught explicitly
    seen2 = []
    ln = export_ids(tmp_path, ids, "partial-links", "symlink", progress=seen2.append)
    assert (ln / "a.jpg").is_symlink() and not (ln / "b.jpg").exists() and not (ln / "b.jpg").is_symlink()
    assert seen2[-1] == {"done": 2, "total": 2, "failed": 1, "skipped": 0}
    assert "b.jpg" in (ln / "failed.txt").read_text()
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


def test_export_rerun_into_same_folder_skips_without_duplicates(tmp_path):
    """The real bug: exporting the same ids into the same folder a second time must not fail or
    duplicate, in copy and in symlink mode. Two different photos that happen to share a filename
    (different subfolders) must still both land."""
    from conftest import make_image
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids = [r["id"] for r in Index(tmp_path).search()]

    seen = []
    out = export_ids(tmp_path, ids, "again", "copy", progress=seen.append)
    assert seen[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 0}
    seen2 = []
    out2 = export_ids(tmp_path, ids, "again", "copy", progress=seen2.append)
    assert out2 == out
    assert seen2[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 2}
    assert sorted(p.name for p in out.iterdir()) == ["a.jpg", "b.jpg"]

    seen_l = []
    out_l = export_ids(tmp_path, ids, "again-links", "symlink", progress=seen_l.append)
    assert seen_l[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 0}
    seen_l2 = []
    out_l2 = export_ids(tmp_path, ids, "again-links", "symlink", progress=seen_l2.append)
    assert out_l2 == out_l
    assert seen_l2[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 2}
    assert all(x.is_symlink() for x in out_l.iterdir())

    # two different photos with the same basename, different subfolders, different sizes
    (tmp_path / "d1").mkdir(); (tmp_path / "d2").mkdir()
    make_image(tmp_path / "d1", "same.jpg", size=(1600, 1200), seed=3)
    make_image(tmp_path / "d2", "same.jpg", size=(800, 600), seed=4)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids2 = [r["id"] for r in Index(tmp_path).search() if r["rel"].endswith("same.jpg")]
    assert len(ids2) == 2
    out3 = export_ids(tmp_path, ids2, "collide", "copy")
    assert len(list(out3.iterdir())) == 2 and "same.jpg" in {p.name for p in out3.iterdir()}
    assert sorted(os.listdir(tmp_path)) == ["a.jpg", "b.jpg", "d1", "d2"]


# export destination (another disk)

def test_export_dir_honours_an_explicit_base(tmp_path, tmp_path_factory):
    import pytest
    from photosort.export import export_dir
    ids = _one_photo(tmp_path)
    other = tmp_path_factory.mktemp("disk")
    assert export_dir(tmp_path, "sel", base=other) == other / tmp_path.resolve().name / "sel"
    with pytest.raises(ValueError, match="inside the source folder"):
        export_dir(tmp_path, "sel", base=tmp_path)
    with pytest.raises(ValueError, match="inside the source folder"):
        export_dir(tmp_path, "sel", base=tmp_path / "sub")
    with pytest.raises(ValueError):
        export_dir(tmp_path, "../../x", base=other)
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"] and ids


def test_export_ids_copies_into_the_other_base(tmp_path, tmp_path_factory):
    from photosort.config import export_root
    ids = _one_photo(tmp_path)
    other = tmp_path_factory.mktemp("disk")
    before = sorted(os.listdir(export_root()))
    out = export_ids(tmp_path, ids, "sel", base=other)
    assert out == other / tmp_path.resolve().name / "sel"
    assert (out / "a.jpg").is_file() and not (out / "a.jpg").is_symlink()
    assert sorted(os.listdir(export_root())) == before          # nothing under the default
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"]


# export selected categories, one folder each

def _two_category_shoot(tmp_path):
    """a.jpg (beach) with a RAW sibling a.ARW, b.jpg (ocean), c.jpg left unclassified."""
    from conftest import make_image
    from photosort import db
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2); make_image(tmp_path, "c.jpg", seed=3)
    (tmp_path / "a.ARW").write_bytes(b"raw bytes, never decoded")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT sibling FROM photos WHERE rel='a.jpg'").fetchone()[0] == "a.ARW"
    conn.execute("UPDATE photos SET category='beach' WHERE rel='a.jpg'")
    conn.execute("UPDATE photos SET category='ocean' WHERE rel='b.jpg'")
    conn.commit()
    return sorted(os.listdir(tmp_path))


def test_export_categories_one_folder_per_category(tmp_path, tmp_path_factory):
    from photosort.export import export_categories
    before = _two_category_shoot(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    seen = []
    out = export_categories(tmp_path, ["beach", "ocean"], base=disk, progress=seen.append)
    assert out == disk / tmp_path.resolve().name / "categories"
    assert sorted(p.name for p in (out / "beach").iterdir()) == ["a.jpg"]
    assert sorted(p.name for p in (out / "ocean").iterdir()) == ["b.jpg"]
    assert (out / "beach" / "a.jpg").is_file() and not (out / "beach" / "a.jpg").is_symlink()
    assert seen[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 0} and not (out / "failed.txt").exists()
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_include_raw_and_links(tmp_path, tmp_path_factory):
    from photosort.export import export_categories
    before = _two_category_shoot(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    seen = []
    out = export_categories(tmp_path, ["beach"], mode="symlink", include_raw=True, base=disk, progress=seen.append)
    assert sorted(p.name for p in (out / "beach").iterdir()) == ["a.ARW", "a.jpg"]
    assert (out / "beach" / "a.ARW").is_symlink() and (out / "beach" / "a.ARW").resolve() == (tmp_path / "a.ARW").resolve()
    assert seen[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 0}       # the RAW sibling counts
    assert not (out / "ocean").exists()
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_drone_folder_holds_every_aerial_row_and_counts_bytes(tmp_path, tmp_path_factory):
    """drone=True adds categories/drone/ with every aerial row whatever its category (a.jpg is in beach/ too,
    c.jpg is unclassified and only lands in drone/); the RAW sibling follows; bytes count the second copy."""
    from photosort import db
    from photosort.export import export_categories, categories_bytes, aerial_rows
    before = _two_category_shoot(tmp_path)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET aerial=1 WHERE rel IN ('a.jpg', 'c.jpg')"); conn.commit()
    assert [r["rel"] for r in aerial_rows(tmp_path)] == ["a.jpg", "c.jpg"]
    sizes = {r[0]: r[1] for r in conn.execute("SELECT rel, size FROM photos")}
    raw = os.stat(tmp_path / "a.ARW").st_size
    assert categories_bytes(tmp_path, ["beach"]) == sizes["a.jpg"]
    assert categories_bytes(tmp_path, ["beach"], drone=True) == 2 * sizes["a.jpg"] + sizes["c.jpg"]
    assert categories_bytes(tmp_path, ["beach"], include_raw=True, drone=True) == 2 * (sizes["a.jpg"] + raw) + sizes["c.jpg"]
    disk = tmp_path_factory.mktemp("disk")
    seen = []
    out = export_categories(tmp_path, ["beach"], mode="symlink", include_raw=True, base=disk, progress=seen.append, drone=True)
    assert sorted(p.name for p in out.iterdir()) == ["beach", "drone"]
    assert sorted(p.name for p in (out / "beach").iterdir()) == ["a.ARW", "a.jpg"]
    assert sorted(p.name for p in (out / "drone").iterdir()) == ["a.ARW", "a.jpg", "c.jpg"]
    assert seen[-1] == {"done": 5, "total": 5, "failed": 0, "skipped": 0}
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_none_means_every_classified_one(tmp_path, tmp_path_factory):
    from photosort.export import export_categories
    before = _two_category_shoot(tmp_path)
    disk = tmp_path_factory.mktemp("disk")
    out = export_categories(tmp_path, None, base=disk)
    assert sorted(p.name for p in out.iterdir()) == ["beach", "ocean"]          # unclassified skipped
    out2 = export_categories(tmp_path, ["unclassified"], base=disk)
    assert sorted(p.name for p in (out2 / "unclassified").iterdir()) == ["c.jpg"]
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_collision_and_failed_file(tmp_path, tmp_path_factory):
    from photosort import db
    from photosort.export import export_categories
    from conftest import make_image
    _two_category_shoot(tmp_path)
    # a second, differently-sized photo in a subfolder shares a.jpg's basename: a genuine name
    # collision between two different photos, not a re-export of the same one, so it must still
    # land under {id}_a.jpg rather than being mistaken for "already there"
    (tmp_path / "sub").mkdir()
    make_image(tmp_path / "sub", "a.jpg", size=(800, 600), seed=9)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET category='beach' WHERE rel='sub/a.jpg'"); conn.commit()
    before = sorted(os.listdir(tmp_path))
    sub_id = conn.execute("SELECT id FROM photos WHERE rel='sub/a.jpg'").fetchone()[0]
    disk = tmp_path_factory.mktemp("disk")
    out = export_categories(tmp_path, ["beach"], base=disk)
    assert sorted(p.name for p in (out / "beach").iterdir()) == sorted(["a.jpg", f"{sub_id}_a.jpg"])
    # re-export: both photos are already there (same size and mtime as the earlier copies), so a
    # second run must not fail or duplicate, only skip them
    seen0 = []
    out2 = export_categories(tmp_path, ["beach"], base=disk, progress=seen0.append)
    assert sorted(p.name for p in (out2 / "beach").iterdir()) == sorted(["a.jpg", f"{sub_id}_a.jpg"])
    assert seen0[-1] == {"done": 2, "total": 2, "failed": 0, "skipped": 2}
    (tmp_path / "b.jpg").unlink()                                               # ocean's only photo vanished
    seen = []
    export_categories(tmp_path, ["ocean"], base=disk, progress=seen.append)
    assert seen[-1] == {"done": 1, "total": 1, "failed": 1, "skipped": 0}
    assert "b.jpg" in (out / "failed.txt").read_text()
    (tmp_path / "b.jpg").write_bytes(b"")                                        # restore the listing for the check
    assert sorted(os.listdir(tmp_path)) == before


def test_export_categories_discovered_go_under_their_own_folder(tmp_path, tmp_path_factory):
    from photosort import db
    from photosort.export import export_categories, categories_bytes
    before = _two_category_shoot(tmp_path)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET cluster='havan fire', cluster_score=1.0 WHERE rel IN ('a.jpg', 'c.jpg')"); conn.commit()
    disk = tmp_path_factory.mktemp("disk")
    seen = []
    out = export_categories(tmp_path, ["ocean"], base=disk, progress=seen.append, discovered=["havan fire"])
    assert sorted(p.name for p in (out / "ocean").iterdir()) == ["b.jpg"]
    assert sorted(p.name for p in (out / "discovered" / "havan fire").iterdir()) == ["a.jpg", "c.jpg"]
    assert not (out / "beach").exists()
    assert seen[-1] == {"done": 3, "total": 3, "failed": 0, "skipped": 0}
    a = (tmp_path / "a.jpg").stat().st_size; b = (tmp_path / "b.jpg").stat().st_size; cc = (tmp_path / "c.jpg").stat().st_size
    assert categories_bytes(tmp_path, ["ocean"], False, discovered=["havan fire"]) == a + b + cc
    assert categories_bytes(tmp_path, [], False, discovered=["havan fire"]) == a + cc
    assert sorted(os.listdir(tmp_path)) == before


def test_categories_bytes_counts_the_sibling(tmp_path):
    from photosort.export import categories_bytes
    _two_category_shoot(tmp_path)
    a = (tmp_path / "a.jpg").stat().st_size; raw = (tmp_path / "a.ARW").stat().st_size
    b = (tmp_path / "b.jpg").stat().st_size
    assert categories_bytes(tmp_path, ["beach"], False) == a
    assert categories_bytes(tmp_path, ["beach"], True) == a + raw
    assert categories_bytes(tmp_path, None, True) == a + raw + b
    (tmp_path / "a.ARW").unlink()
    assert categories_bytes(tmp_path, ["beach"], True) == a                     # a sibling that fails to stat is skipped

def test_export_dir_refuses_a_base_above_the_shoot(tmp_path):
    import pytest
    from photosort.export import export_dir
    ids = _one_photo(tmp_path)
    with pytest.raises(ValueError, match="contains the source folder"):
        export_dir(tmp_path, "sel", base=tmp_path.parent)
    with pytest.raises(ValueError, match="contains the source folder"):
        export_dir(tmp_path, "sel", base=tmp_path.parent.parent)
    assert sorted(os.listdir(tmp_path)) == ["a.jpg"] and ids

# the shared planners: which file, into which subfolder, used by the local export and the Drive upload alike

def test_category_jobs_name_the_subfolder_and_place_a_photo_twice(tmp_path):
    from photosort import db
    from photosort.export import category_jobs, jobs_bytes
    before = _two_category_shoot(tmp_path)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET aerial=1 WHERE rel='a.jpg'"); conn.commit()
    ids = {r[0]: r[1] for r in conn.execute("SELECT rel, id FROM photos")}
    jobs, seg_jobs = category_jobs(tmp_path, ["beach", "ocean"], include_raw=True, drone=True)
    assert seg_jobs == []
    assert jobs == [(ids["a.jpg"], "a.jpg", "beach"), (ids["a.jpg"], "a.ARW", "beach"),
                    (ids["b.jpg"], "b.jpg", "ocean"),
                    (ids["a.jpg"], "a.jpg", "drone"), (ids["a.jpg"], "a.ARW", "drone")]
    conn.execute("UPDATE photos SET cluster='excavator', cluster_score=0.9 WHERE rel='c.jpg'"); conn.commit()
    jobs, _ = category_jobs(tmp_path, [], discovered=["excavator"])
    assert jobs == [(ids["c.jpg"], "c.jpg", "discovered/excavator")]
    a = (tmp_path / "a.jpg").stat().st_size; raw = (tmp_path / "a.ARW").stat().st_size; b = (tmp_path / "b.jpg").stat().st_size
    assert jobs_bytes(tmp_path, [(1, "a.jpg", "x"), (1, "a.ARW", "x"), (2, "b.jpg", "y"), (1, "a.jpg", "z"), (9, "gone.jpg", "z")]) == 2 * a + raw + b
    assert sorted(os.listdir(tmp_path)) == before


def test_folder_jobs_and_ids_jobs_share_the_shape(tmp_path):
    from photosort import db
    from photosort.export import folder_jobs, ids_jobs
    before = _two_category_shoot(tmp_path)
    ids = {r[0]: r[1] for r in db.connect(tmp_path).execute("SELECT rel, id FROM photos")}
    jobs = folder_jobs(tmp_path, {"Meera": [ids["a.jpg"], ids["b.jpg"]], "Ar/ya": [ids["a.jpg"]]}, include_raw=True)
    assert jobs == [(ids["a.jpg"], "a.jpg", "Meera"), (ids["a.jpg"], "a.ARW", "Meera"), (ids["b.jpg"], "b.jpg", "Meera"),
                    (ids["a.jpg"], "a.jpg", "Ar_ya"), (ids["a.jpg"], "a.ARW", "Ar_ya")]
    assert ids_jobs(tmp_path, [ids["c.jpg"], ids["a.jpg"], 999]) == [(ids["a.jpg"], "a.jpg", ""), (ids["c.jpg"], "c.jpg", "")]
    assert ids_jobs(tmp_path, [ids["a.jpg"]], include_raw=True) == [(ids["a.jpg"], "a.jpg", ""), (ids["a.jpg"], "a.ARW", "")]
    assert sorted(os.listdir(tmp_path)) == before
