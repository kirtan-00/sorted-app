"""The full-size view's picture and the export check: /api/full and /api/export/check. The shoot is only read, by manifest."""
import hashlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import make_image
from photosort import db, export, fullsize
from photosort.index import index_folder
from photosort.server import create_app


def _manifest(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def _big(root, name, size=(3000, 2000), fmt=None):
    a = Image.effect_noise(size, 60).convert("RGB")
    a.save(root / name, fmt) if fmt else a.save(root / name)


@pytest.fixture
def shoot(tmp_path, monkeypatch):
    root = tmp_path / "shoot"; root.mkdir()
    monkeypatch.setenv("PHOTOSORT_EXPORT_DIR", str(tmp_path / "out"))
    _big(root, "a.jpg"); _big(root, "b.tif")
    make_image(root, "c.jpg", seed=3)
    index_folder(root, faces=False, workers=1, embed=False)
    return root


def _id(root, name):
    return db.connect(root).execute("SELECT id FROM photos WHERE rel=?", (name,)).fetchone()[0]


def _ids(root):
    return [r[0] for r in db.connect(root).execute("SELECT id FROM photos WHERE status='ok' ORDER BY id")]


def test_a_jpeg_goes_out_as_the_file_itself_and_nothing_is_cached(shoot):
    c = TestClient(create_app(shoot))
    r = c.get(f"/api/full/{_id(shoot, 'a.jpg')}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == (shoot / "a.jpg").read_bytes()
    assert not (db.index_dir(shoot) / fullsize.CACHE_DIRNAME).exists()


def test_a_tiff_is_decoded_once_at_full_size_into_the_index_and_the_shoot_is_only_read(shoot, monkeypatch):
    before = _manifest(shoot)
    c = TestClient(create_app(shoot))
    i = _id(shoot, "b.tif")
    calls = []
    real = fullsize.load_preview
    monkeypatch.setattr(fullsize, "load_preview", lambda *a: calls.append(a) or real(*a))
    r = c.get(f"/api/full/{i}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    made = list((db.index_dir(shoot) / fullsize.CACHE_DIRNAME).iterdir())
    assert len(made) == 1 and Image.open(made[0]).size == (3000, 2000)
    assert c.get(f"/api/full/{i}").content == r.content and len(calls) == 1
    assert _manifest(shoot) == before


def test_a_raw_uses_the_decoder_and_stays_viewable_with_the_disk_unplugged(shoot, monkeypatch):
    (shoot / "d.arw").write_bytes(b"not really a raw")
    seen = []
    monkeypatch.setattr(fullsize, "load_preview", lambda p, edge: seen.append(edge) or Image.new("RGB", (6000, 4000), "gray"))
    out = fullsize.source(shoot, "d.arw", "abc")
    assert out[1] == "image/jpeg" and Image.open(out[0]).size == (6000, 4000)
    assert seen == [fullsize.NO_LIMIT]
    (shoot / "d.arw").unlink()
    assert fullsize.source(shoot, "d.arw", "abc")[0] == out[0]


def test_a_clip_an_unknown_id_and_a_missing_file_are_404(shoot):
    c = TestClient(create_app(shoot))
    i = _id(shoot, "a.jpg")
    assert c.get("/api/full/99999").status_code == 404
    db.connect(shoot).execute("UPDATE photos SET kind='video' WHERE id=?", (_id(shoot, "c.jpg"),)).connection.commit()
    assert c.get(f"/api/full/{_id(shoot, 'c.jpg')}").status_code == 404
    (shoot / "a.jpg").unlink()
    assert c.get(f"/api/full/{i}").status_code == 404


def test_the_cache_drops_the_oldest_and_never_the_new_file(tmp_path):
    for n, t in (("old", 1), ("mid", 2), ("new", 3)):
        p = tmp_path / f"{n}.jpg"; p.write_bytes(b"x" * 100); os.utime(p, (t, t))
    fullsize._trim(tmp_path, tmp_path / "old.jpg", limit=200)
    assert sorted(p.stem for p in tmp_path.iterdir()) == ["new", "old"]


def _check(c, ids, name, **kw):
    return c.post("/api/export/check", json=dict(ids=ids, name=name, **kw)).json()


def _export(c, ids, name, **kw):
    assert c.post("/api/export", json=dict(ids=ids, name=name, **kw)).status_code == 200
    for _ in range(500):
        p = c.get("/api/export/progress").json()
        if not p["running"]:
            return p
    raise AssertionError("export did not end")


def test_a_repeat_export_is_found_before_it_starts(shoot):
    c = TestClient(create_app(shoot))
    ids = [_id(shoot, "a.jpg")]
    first = _check(c, ids, "set")
    assert first["problem"] is None and first["already"] == 0 and first["clash"] == 0 and first["free_name"] == "set"
    _export(c, ids, "set")
    again = _check(c, ids, "set")
    assert again["already"] == 1 and again["clash"] == 0 and again["free_name"] == "set 2"


def test_web_then_full_in_one_folder_is_a_clash_either_way_round(shoot):
    c = TestClient(create_app(shoot))
    ids = [_id(shoot, "a.jpg")]
    _export(c, ids, "web", web_size=1000)
    assert _check(c, ids, "web", web_size=1000)["already"] == 1
    assert _check(c, ids, "web")["clash"] == 1                         # full size asked, a 1000 px copy is there
    _export(c, ids, "whole")
    assert _check(c, ids, "whole", web_size=1000)["clash"] == 1        # the same mtime, but not the size asked for


def test_replace_takes_out_this_selections_copies_and_nothing_else(shoot):
    c = TestClient(create_app(shoot))
    ids = [_id(shoot, "a.jpg")]
    p = Path(_export(c, ids, "set")["path"])
    (p / "other.txt").write_text("keep")
    assert c.post("/api/export/replace", json=dict(ids=ids, name="set")).json() == {"removed": 1}
    assert sorted(x.name for x in p.iterdir()) == ["other.txt"]
    assert (shoot / "a.jpg").is_file()


def test_replace_leaves_a_copy_whose_original_is_gone(shoot):
    c = TestClient(create_app(shoot))
    ids = [_id(shoot, "a.jpg")]
    p = Path(_export(c, ids, "set")["path"])
    (shoot / "a.jpg").unlink()
    assert c.post("/api/export/replace", json=dict(ids=ids, name="set")).json() == {"removed": 0}
    assert (p / "a.jpg").is_file()
    assert _check(c, ids, "set")["missing"] == 1


def test_a_shoot_disk_that_went_away_is_a_plain_sentence(shoot):
    c = TestClient(create_app(shoot))
    ids = _ids(shoot)
    shoot.rename(shoot.with_name("gone"))
    d = _check(c, ids, "set")
    assert "shoot disk is not connected" in d["problem"] and "nothing was exported" in d["problem"]


def test_a_destination_that_went_away_is_a_plain_sentence(shoot, tmp_path):
    c = TestClient(create_app(shoot))
    disk = tmp_path / "disk"; disk.mkdir()
    assert c.post("/api/export/destination", json={"path": str(disk)}).status_code == 200
    disk.rmdir()
    assert "nothing was exported" in _check(c, _ids(shoot), "set")["problem"]


def test_the_check_writes_nothing(shoot, tmp_path):
    before = _manifest(shoot)
    c = TestClient(create_app(shoot))
    assert _check(c, _ids(shoot), "set")["problem"] is None
    assert _manifest(shoot) == before
    assert not (tmp_path / "out" / "shoot" / "set").exists()
