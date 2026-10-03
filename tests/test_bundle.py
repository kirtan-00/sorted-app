import json
import os
import sqlite3
import zipfile
from pathlib import Path
import pytest
from photosort import db
from photosort.config import app_home, shoot_slug
from photosort.index import index_folder


def _three_photo_shoot(tmp_path):
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    return sorted(os.listdir(tmp_path))


def test_export_bundle_writes_one_zip(tmp_path, tmp_path_factory):
    from photosort.bundle import export_bundle, inspect_bundle
    before = _three_photo_shoot(tmp_path)
    out_dir = tmp_path_factory.mktemp("out")
    seen = []
    z = export_bundle(tmp_path, out_dir, progress=seen.append)
    assert z == out_dir / f"sorted_{tmp_path.name}.sorted" and z.is_file()
    assert seen and seen[-1] == {"done": 6, "total": 6, "failed": 0}
    with zipfile.ZipFile(z) as zf:
        names = sorted(zf.namelist())
        assert "bundle.json" in names and "index.db" in names
        assert len([n for n in names if n.startswith("thumbs/") and n.endswith(".jpg")]) == 3
        assert len([n for n in names if n.startswith("grid/") and n.endswith(".jpg")]) == 3
        assert zf.getinfo(names[-1]).compress_type == zipfile.ZIP_STORED     # a thumb, already a JPEG
        info = json.loads(zf.read("bundle.json"))
    assert info["format"] == "photosort-index/1"
    assert info["name"] == tmp_path.name and info["root"] == str(tmp_path.resolve())
    assert info["slug"] == shoot_slug(tmp_path) and info["photos"] == 3 and info["faces"] == 0
    assert info["references"] == 0 and info["exported_at"]
    assert inspect_bundle(z)["photos"] == 3
    assert not list(out_dir.glob("*.part"))                                # nothing half-written left behind
    assert sorted(os.listdir(tmp_path)) == before


def test_export_bundle_refuses_out_dir_inside_the_shoot(tmp_path):
    """A folder under the shoot is still refused, and refusing it creates nothing, not even the folder."""
    from photosort.bundle import export_bundle
    before = _three_photo_shoot(tmp_path)
    for bad in [tmp_path / "sub", tmp_path / "sub" / "deeper"]:
        with pytest.raises(ValueError, match="inside the source folder"):
            export_bundle(tmp_path, bad)
    assert sorted(os.listdir(tmp_path)) == before


def test_export_bundle_may_write_the_project_file_into_the_shoot_folder_itself(tmp_path):
    """The one exception to the read-only shoot: sorted_<shoot>.sorted directly in the scanned folder, so it
    travels on the disk. Exactly that file is added, no .part is left, and a second export replaces it in
    place (a rescan overwrites cleanly) instead of stacking copies."""
    from photosort.bundle import export_bundle, inspect_bundle
    before = _three_photo_shoot(tmp_path)
    z = export_bundle(tmp_path, tmp_path)
    assert z == tmp_path / f"sorted_{tmp_path.name}.sorted" and inspect_bundle(z)["photos"] == 3
    assert sorted(os.listdir(tmp_path)) == sorted(before + [z.name])
    export_bundle(tmp_path, tmp_path)
    assert sorted(os.listdir(tmp_path)) == sorted(before + [z.name])


def test_inspect_bundle_rejects_a_zip_that_is_not_a_bundle(tmp_path, tmp_path_factory):
    from photosort.bundle import inspect_bundle
    out = tmp_path_factory.mktemp("out")
    plain = out / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(ValueError, match="not a sorted project file"):
        inspect_bundle(plain)
    wrong = out / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as zf:                                # right file, wrong format string
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/99", "root": "/x"}))
        zf.writestr("index.db", b"")
    with pytest.raises(ValueError, match="not a sorted project file"):
        inspect_bundle(wrong)
    nodb = out / "nodb.zip"
    with zipfile.ZipFile(nodb, "w") as zf:                                 # right format, no database
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/1", "root": "/x"}))
    with pytest.raises(ValueError, match="not a sorted project file"):
        inspect_bundle(nodb)
    (out / "notzip.zip").write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="not a sorted project file"):
        inspect_bundle(out / "notzip.zip")


def test_import_bundle_installs_under_the_bundle_root(tmp_path, tmp_path_factory, monkeypatch):
    from photosort.bundle import export_bundle, import_bundle
    before = _three_photo_shoot(tmp_path)
    conn = db.connect(tmp_path)
    db.add_reference(conn, "Arya", [0.1] * 128, "/ref.jpg")               # rides along in index.db
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    fresh = tmp_path_factory.mktemp("fresh") / "home"                     # another Mac: no index here yet
    monkeypatch.setenv("PHOTOSORT_HOME", str(fresh))
    seen = []
    target = import_bundle(z, progress=seen.append)
    assert target == fresh / shoot_slug(tmp_path) and target.is_dir()
    assert seen and seen[-1]["done"] == seen[-1]["total"] and seen[-1]["failed"] == 0
    conn2 = db.connect(tmp_path)
    assert conn2.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 3
    assert db.get_meta(conn2, "root") == str(tmp_path.resolve())
    assert [r["name"] for r in db.list_references(conn2)] == ["Arya"]
    for (qh,) in conn2.execute("SELECT qhash FROM photos"):
        assert (target / "thumbs" / f"{qh}.jpg").is_file() and (target / "grid" / f"{qh}.jpg").is_file()
    assert not list(fresh.glob(".*import*"))                              # the temp extract dir is gone
    assert sorted(os.listdir(tmp_path)) == before


def test_import_bundle_rekeys_to_another_root(tmp_path, tmp_path_factory, monkeypatch):
    from photosort.bundle import export_bundle, import_bundle
    before = _three_photo_shoot(tmp_path)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    other = tmp_path_factory.mktemp("disk") / "01 Photos"                 # mounted under another path here
    other.mkdir()
    fresh = tmp_path_factory.mktemp("fresh") / "home"
    monkeypatch.setenv("PHOTOSORT_HOME", str(fresh))
    target = import_bundle(z, root=other)
    assert target == fresh / shoot_slug(other)
    assert not (fresh / shoot_slug(tmp_path)).exists()
    conn = db.connect(other)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 3
    assert db.get_meta(conn, "root") == str(other)
    assert sorted(os.listdir(other)) == []                                # the photo folder itself is untouched
    assert sorted(os.listdir(tmp_path)) == before


def test_import_bundle_installs_an_unmounted_root(tmp_path, tmp_path_factory, monkeypatch):
    from photosort.bundle import export_bundle, import_bundle
    _three_photo_shoot(tmp_path)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    gone = Path("/Volumes/not-plugged-in-photosort-test/shoot")           # never created, never touched
    assert not gone.exists()
    fresh = tmp_path_factory.mktemp("fresh") / "home"
    monkeypatch.setenv("PHOTOSORT_HOME", str(fresh))
    target = import_bundle(z, root=gone)
    assert target == fresh / shoot_slug(gone) and (target / "index.db").is_file()
    assert not gone.exists()


def test_import_bundle_keeps_the_old_index_as_bak(tmp_path, tmp_path_factory):
    from photosort.bundle import export_bundle, import_bundle
    _three_photo_shoot(tmp_path)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    old = db.index_dir(tmp_path)
    (old / "marker.txt").write_text("the old index")
    target = import_bundle(z)
    assert target == old and not (target / "marker.txt").exists()
    baks = [p for p in app_home().iterdir() if p.name.startswith(shoot_slug(tmp_path) + ".bak-")]
    assert len(baks) == 1 and (baks[0] / "marker.txt").read_text() == "the old index"
    assert (baks[0] / "index.db").is_file()
    target2 = import_bundle(z)                                            # twice in one second: no collision
    assert target2 == old
    baks2 = [p for p in app_home().iterdir() if p.name.startswith(shoot_slug(tmp_path) + ".bak-")]
    assert len(baks2) == 2


def test_import_bundle_rejects_zip_slip(tmp_path, tmp_path_factory):
    from photosort.bundle import import_bundle
    _three_photo_shoot(tmp_path)
    out = tmp_path_factory.mktemp("out")
    evil = out / "evil.photosort-index.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/1", "root": str(tmp_path), "name": "x"}))
        zf.writestr("index.db", b"")
        zf.writestr("../evil", "escaped")
    with pytest.raises(ValueError, match="escapes"):
        import_bundle(evil)
    assert not (app_home() / "evil").exists() and not (app_home().parent / "evil").exists()
    assert not list(app_home().glob(".*import*"))                         # the temp extract dir was cleaned up
    conn = db.connect(tmp_path)                                           # the existing index was not replaced
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 3
    absolute = out / "abs.photosort-index.zip"
    with zipfile.ZipFile(absolute, "w") as zf:
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/1", "root": str(tmp_path), "name": "x"}))
        zf.writestr("index.db", b"")
        zf.writestr("/tmp/photosort-evil", "escaped")
    with pytest.raises(ValueError, match="escapes"):
        import_bundle(absolute)
    assert not Path("/tmp/photosort-evil").exists()


def test_db_meta_round_trip(tmp_path):
    conn = db.connect(tmp_path)
    assert db.get_meta(conn, "root") is None
    db.set_meta(conn, "root", "/a")
    assert db.get_meta(conn, "root") == "/a"
    db.set_meta(conn, "root", "/b")
    assert db.get_meta(conn, "root") == "/b"
    assert sqlite3.connect(db.index_dir(tmp_path) / "index.db").execute("SELECT value FROM meta WHERE key='root'").fetchone()[0] == "/b"


def test_import_bundle_restores_the_old_index_when_the_install_rename_fails(tmp_path, tmp_path_factory, monkeypatch):
    """target was renamed to .bak, then the rename of tmp onto target blew up: the old index must
    come back to target and the .bak must be gone, or the shoot has no index at all."""
    from photosort.bundle import export_bundle, import_bundle
    _three_photo_shoot(tmp_path)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    old = db.index_dir(tmp_path)
    (old / "marker.txt").write_text("the old index")
    real_rename = os.rename
    calls = {"n": 0}
    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:                                                # the tmp -> target install
            raise OSError("disk went away")
        return real_rename(src, dst)
    monkeypatch.setattr(os, "rename", flaky_rename)
    with pytest.raises(OSError, match="disk went away"):
        import_bundle(z)
    monkeypatch.setattr(os, "rename", real_rename)
    assert calls["n"] == 3                                                 # bak, failed install, restore
    assert (old / "marker.txt").read_text() == "the old index"
    assert not [p for p in app_home().iterdir() if p.name.startswith(shoot_slug(tmp_path) + ".bak-")]
    assert not list(app_home().glob(".*import*"))
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 3


def test_export_bundle_carries_video_frames(tmp_path, tmp_path_factory, monkeypatch):
    from conftest import make_video, needs_ffmpeg
    from photosort.bundle import export_bundle, import_bundle
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    with zipfile.ZipFile(z) as zf:
        frames = [n for n in zf.namelist() if n.startswith("frames/") and n.endswith(".jpg")]
    assert len(frames) == 6
    fresh = tmp_path_factory.mktemp("fresh") / "home"
    monkeypatch.setenv("PHOTOSORT_HOME", str(fresh))
    target = import_bundle(z)
    assert len(list((target / "frames").glob("*.jpg"))) == 6


# ===== a bundle says how far its scan got =====

def test_bundle_json_carries_the_scan_counts(tmp_path, tmp_path_factory):
    from photosort.bundle import export_bundle, inspect_bundle
    _three_photo_shoot(tmp_path)                                           # read, not embedded, faces off
    z = export_bundle(tmp_path, tmp_path_factory.mktemp("out"))
    scan = inspect_bundle(z)["scan"]
    assert (scan["items"], scan["scanned"], scan["embedded"], scan["unembedded"], scan["pending"]) == (3, 3, 0, 3, 0)
    assert scan["complete"] is False and scan["faces"] is False
    index_folder(tmp_path, faces=False, workers=1, embed=True)
    z2 = export_bundle(tmp_path, tmp_path_factory.mktemp("out2"))
    scan = inspect_bundle(z2)["scan"]
    assert scan["complete"] is True and scan["embedded"] == 3


def test_older_bundle_without_scan_still_loads(tmp_path, tmp_path_factory, monkeypatch):
    from photosort.bundle import export_bundle, inspect_bundle, import_bundle
    _three_photo_shoot(tmp_path)
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out)
    old = out / "old.photosort-index.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(old, "w") as dst:
        for i in src.infolist():
            data = src.read(i.filename)
            if i.filename == "bundle.json":
                info = json.loads(data); info.pop("scan"); data = json.dumps(info)
            dst.writestr(i, data)
    assert "scan" not in inspect_bundle(old)
    target = import_bundle(old, tmp_path)
    assert (target / "index.db").is_file()
    assert db.scan_counts(db.connect(tmp_path))["scanned"] == 3            # the counts come from the index itself


# ===== the project file: its name, its default folder, and the old name still opening =====
def test_project_name_and_default_dir(tmp_path, monkeypatch):
    from photosort.bundle import project_name, bundle_path, default_project_dir
    shoot = tmp_path / "disk" / "NSG_26_Part 1"; shoot.mkdir(parents=True)
    assert project_name(shoot) == "sorted_NSG_26_Part 1.sorted"
    assert bundle_path(shoot, tmp_path / "x") == tmp_path / "x" / "sorted_NSG_26_Part 1.sorted"
    assert bundle_path(shoot, shoot) == shoot / "sorted_NSG_26_Part 1.sorted"          # in the shoot folder itself
    # the scanned folder itself, so the file travels with the disk
    assert default_project_dir(shoot) == shoot.resolve()
    # a folder that says it cannot be written: ~/Documents/sorted, the last resort
    monkeypatch.setattr("os.access", lambda p, m: False)
    assert default_project_dir(shoot, home=tmp_path / "home") == tmp_path / "home" / "Documents" / "sorted"
    # and a folder that is not there at all (the disk is unplugged)
    monkeypatch.undo()
    assert default_project_dir(tmp_path / "gone", home=tmp_path / "home") == tmp_path / "home" / "Documents" / "sorted"


def test_skeleton_project_file_has_the_database_and_no_jpegs(tmp_path, tmp_path_factory):
    """thumbs=False: what a picked location gets before the scan starts. It opens like any project file and
    says it is a skeleton."""
    from photosort.bundle import export_bundle, inspect_bundle
    _three_photo_shoot(tmp_path)
    out = tmp_path_factory.mktemp("picked")
    seen = []
    z = export_bundle(tmp_path, out, progress=seen.append, thumbs=False)
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["bundle.json", "index.db"]
    info = inspect_bundle(z)
    assert info["skeleton"] is True and info["photos"] == 3
    assert seen == [{"done": 0, "total": 0, "failed": 0}]
    full = export_bundle(tmp_path, out)
    assert full == z and "skeleton" not in inspect_bundle(z)          # the finished scan replaces it


def test_estimate_is_above_the_measured_size():
    """955 mixed items packed to 211 MB when measured; the estimate for 955 photos alone must clear that with
    room to spare, and a clip costs more than a photo."""
    from photosort.bundle import estimate_bytes
    assert estimate_bytes(955, 0) > 1.5 * 211e6
    assert estimate_bytes(0, 1) > estimate_bytes(1, 0) > 0
    assert estimate_bytes(0, 0) == 0


def test_probe_writable_really_writes_and_leaves_nothing(tmp_path, monkeypatch):
    from photosort.bundle import probe_writable
    assert probe_writable(tmp_path) is None and os.listdir(tmp_path) == []
    assert probe_writable(tmp_path / "missing")                          # a reason, in words
    # permissive bits, refusing volume: the shape of a read-only exFAT or a locked SD card. os.access says
    # yes; only the write can tell.
    import errno
    real_open = os.open
    def ro_open(path, flags, *a):
        if str(path).startswith(str(tmp_path)) and flags & os.O_CREAT:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, *a)
    monkeypatch.setattr(os, "open", ro_open)
    assert os.access(tmp_path, os.W_OK)
    assert probe_writable(tmp_path) == "Read-only file system"


def test_legacy_name_still_opens(tmp_path, tmp_path_factory, monkeypatch):
    """A <shoot>.photosort-index.zip from before the rename: same format inside, so it inspects and imports."""
    from photosort.bundle import export_bundle, inspect_bundle, import_bundle
    _three_photo_shoot(tmp_path)
    out = tmp_path_factory.mktemp("out")
    z = export_bundle(tmp_path, out)
    legacy = out / f"{tmp_path.name}.photosort-index.zip"
    z.rename(legacy)
    assert inspect_bundle(legacy)["photos"] == 3
    home = tmp_path_factory.mktemp("home"); monkeypatch.setenv("PHOTOSORT_HOME", str(home))
    assert import_bundle(legacy).is_dir()


def test_headroom_scales_with_the_shoot_and_caps_at_the_old_floor():
    """A 24-photo card is asked for about 111 MB, not 1.1 GB; a big shoot keeps a tenth of a generous
    estimate free, and from about 23k photos up the full 1 GiB it was always asked for."""
    from photosort.bundle import estimate_bytes, project_headroom
    small = estimate_bytes(24, 0)
    assert project_headroom(small) == 100 * 1000 * 1000
    assert 105e6 < small + project_headroom(small) < 115e6
    mid = estimate_bytes(3000, 0)                     # 1.35 GB: a tenth of it
    assert project_headroom(mid) == mid // 10
    assert project_headroom(estimate_bytes(20000, 0)) > 900e6
    assert project_headroom(estimate_bytes(25000, 0)) == 1 << 30
    assert project_headroom(0) == 100 * 1000 * 1000
