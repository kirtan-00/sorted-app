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
    assert z == out_dir / f"{tmp_path.name}.photosort-index.zip" and z.is_file()
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
    from photosort.bundle import export_bundle
    before = _three_photo_shoot(tmp_path)
    for bad in [tmp_path, tmp_path / "sub"]:
        with pytest.raises(ValueError, match="inside the source folder"):
            export_bundle(tmp_path, bad)
    assert sorted(os.listdir(tmp_path)) == before


def test_inspect_bundle_rejects_a_zip_that_is_not_a_bundle(tmp_path, tmp_path_factory):
    from photosort.bundle import inspect_bundle
    out = tmp_path_factory.mktemp("out")
    plain = out / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(ValueError, match="not a photosort index bundle"):
        inspect_bundle(plain)
    wrong = out / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as zf:                                # right file, wrong format string
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/99", "root": "/x"}))
        zf.writestr("index.db", b"")
    with pytest.raises(ValueError, match="not a photosort index bundle"):
        inspect_bundle(wrong)
    nodb = out / "nodb.zip"
    with zipfile.ZipFile(nodb, "w") as zf:                                 # right format, no database
        zf.writestr("bundle.json", json.dumps({"format": "photosort-index/1", "root": "/x"}))
    with pytest.raises(ValueError, match="not a photosort index bundle"):
        inspect_bundle(nodb)
    (out / "notzip.zip").write_bytes(b"not a zip at all")
    with pytest.raises(ValueError, match="not a photosort index bundle"):
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
