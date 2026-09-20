"""Reorganise disk: the one guarded exception to the read-only shoot root. Every test runs in a tmp dir."""
import json
import os
import stat
from pathlib import Path
import numpy as np
import pytest
from photosort import db
from photosort.index import index_folder
from photosort.search import Index
from photosort import reorganise


def _listing(root: Path) -> dict[str, bytes]:
    """Every regular file under root (relative path -> bytes), hidden files included."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _dirs(root: Path) -> set[str]:
    out = set()
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            out.add(str((Path(dirpath) / d).relative_to(root)))
    return out


def _add_video(conn, root: Path, rel: str, aerial: int = 0, category: str | None = None) -> int:
    p = root / rel
    st = p.stat()
    pid = db.upsert_photo(conn, dict(rel=rel, size=st.st_size, mtime=st.st_mtime, qhash=f"q-{rel}", n_faces=0,
                                     status="ok", kind="video", aerial=aerial, duration=3.0))
    conn.execute("UPDATE photos SET category=? WHERE id=?", (category, pid)); conn.commit()
    return pid


def _shoot(tmp_path: Path):
    """day1/a.jpg (beach) with RAW day1/a.ARW and sidecar day1/a.xmp; b.jpg filed other but discovered
    'sunset'; DJI_0003.JPG aerial (category other, cluster 'group 2' which is unnamed); videos inserted by
    hand (no ffmpeg): sony/C0001.MP4 (interview) with Sony sidecar C0001M01.XML, drone/DJI_0010.MP4
    aerial with a DJI .SRT. Returns (conn, ids by rel)."""
    from conftest import make_image
    (tmp_path / "day1").mkdir(); (tmp_path / "sony").mkdir(); (tmp_path / "drone").mkdir()
    make_image(tmp_path / "day1", "a.jpg", seed=1)
    (tmp_path / "day1" / "a.ARW").write_bytes(b"raw bytes, never decoded")
    (tmp_path / "day1" / "a.xmp").write_bytes(b"<xmp/>")
    make_image(tmp_path, "b.jpg", seed=2)
    make_image(tmp_path, "DJI_0003.JPG", seed=3)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    (tmp_path / "sony" / "C0001.MP4").write_bytes(b"not really a clip")
    (tmp_path / "sony" / "C0001M01.XML").write_bytes(b"<NonRealTimeMeta/>")
    (tmp_path / "drone" / "DJI_0010.MP4").write_bytes(b"not really a drone clip")
    (tmp_path / "drone" / "DJI_0010.SRT").write_bytes(b"dji telemetry")
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT sibling FROM photos WHERE rel='day1/a.jpg'").fetchone()[0] == "day1/a.ARW"
    assert conn.execute("SELECT aerial FROM photos WHERE rel='DJI_0003.JPG'").fetchone()[0] == 1
    conn.execute("UPDATE photos SET category='beach', category_score=0.9 WHERE rel='day1/a.jpg'")
    conn.execute("UPDATE photos SET category='other', cluster='sunset', cluster_score=0.8 WHERE rel='b.jpg'")
    conn.execute("UPDATE photos SET category='other', cluster='group 2' WHERE rel='DJI_0003.JPG'")
    conn.commit()
    _add_video(conn, tmp_path, "sony/C0001.MP4", category="interview")
    _add_video(conn, tmp_path, "drone/DJI_0010.MP4", aerial=1, category="other")
    ids = {r[0]: r[1] for r in conn.execute("SELECT rel, id FROM photos")}
    return conn, ids


EXPECTED = {
    "day1/a.jpg": "sorted/photos/beach/a.jpg",
    "day1/a.ARW": "sorted/photos/beach/a.ARW",
    "day1/a.xmp": "sorted/photos/beach/a.xmp",
    "b.jpg": "sorted/photos/sunset/b.jpg",
    "DJI_0003.JPG": "sorted/drone/photos/other/DJI_0003.JPG",
    "sony/C0001.MP4": "sorted/videos/interview/C0001.MP4",
    "sony/C0001M01.XML": "sorted/videos/interview/C0001M01.XML",
    "drone/DJI_0010.MP4": "sorted/drone/videos/other/DJI_0010.MP4",
    "drone/DJI_0010.SRT": "sorted/drone/videos/other/DJI_0010.SRT",
}


def test_plan_counts_and_sample(tmp_path):
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    assert p["moves"] == 9 and p["photos"] == 2 and p["videos"] == 1 and p["drone"] == 2
    assert p["people"] == {} and p["collisions"] == 0 and p["folders"] == 5
    assert all(p["guards"].values())
    assert len(p["sample"]) == 9
    moves = {m["src_rel"]: m for m in p["sample"]}
    assert {m["src_rel"]: m["dst_rel"] for m in p["sample"]} == EXPECTED
    assert moves["day1/a.jpg"]["kind"] == "photo" and moves["day1/a.jpg"]["reason"] == "category:beach"
    assert moves["day1/a.ARW"]["kind"] == "raw" and moves["day1/a.ARW"]["photo_id"] == ids["day1/a.jpg"]
    assert moves["day1/a.xmp"]["kind"] == "sidecar" and moves["day1/a.xmp"]["reason"] == "category:beach"
    assert moves["b.jpg"]["reason"] == "category:sunset"
    assert moves["DJI_0003.JPG"]["reason"] == "drone" and moves["sony/C0001.MP4"]["kind"] == "video"
    assert moves["sony/C0001M01.XML"]["kind"] == "sidecar" and moves["drone/DJI_0010.SRT"]["kind"] == "sidecar"
    assert _listing(tmp_path) == before                 # a plan touches nothing
    assert not (tmp_path / ".photosort-write-test").exists()


def test_apply_moves_every_file_updates_rel_and_search_and_undo_restores(tmp_path):
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path); dirs_before = _dirs(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    seen = []
    out = reorganise.apply(tmp_path, p["plan_id"], progress=seen.append)
    assert out["done"] == 9 and out["failed"] == 0 and out["total"] == 9
    assert seen[-1] == {"done": 9, "total": 9, "failed": 0}
    after = _listing(tmp_path)
    for src, dst in EXPECTED.items():
        assert dst in after and src not in after and after[dst] == before[src]
    assert set(after) == {EXPECTED[k] for k in before} | {"sorted/UNDO.json"}
    # db follows the files
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT rel, sibling FROM photos WHERE id=?", (ids["day1/a.jpg"],)).fetchone()[:] == \
        ("sorted/photos/beach/a.jpg", "sorted/photos/beach/a.ARW")
    assert conn.execute("SELECT rel FROM photos WHERE id=?", (ids["sony/C0001.MP4"],)).fetchone()[0] == "sorted/videos/interview/C0001.MP4"
    assert db.get_meta(conn, "reorganised_at")
    found = {r["id"]: r["rel"] for r in Index(tmp_path).search()}
    assert found[ids["b.jpg"]] == "sorted/photos/sunset/b.jpg" and len(found) == 5
    # the manifest is exact
    man = json.loads((tmp_path / "sorted" / "UNDO.json").read_text())
    assert man["format"] == "photosort-undo/1" and man["root"] == str(tmp_path) and man["created"]
    assert {(m["src_rel"], m["dst_rel"]) for m in man["moves"]} == set(EXPECTED.items())
    assert all(isinstance(m["photo_id"], int) for m in man["moves"])
    st = reorganise.status(tmp_path)
    assert st["reorganised"] is True and st["moves"] == 9 and st["created"] == man["created"]
    # a second apply is refused (plan and apply both)
    with pytest.raises(ValueError, match="undo the previous reorganise first"):
        reorganise.plan(tmp_path, by_people=False)
    with pytest.raises(ValueError):
        reorganise.apply(tmp_path, p["plan_id"])
    # undo restores every path byte for byte and leaves no trace
    seen2 = []
    u = reorganise.undo(tmp_path, progress=seen2.append)
    assert u == {"restored": 9, "failed": 0}
    assert seen2[-1] == {"done": 9, "total": 9, "failed": 0}
    assert _listing(tmp_path) == before
    assert _dirs(tmp_path) == dirs_before
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT rel, sibling FROM photos WHERE id=?", (ids["day1/a.jpg"],)).fetchone()[:] == ("day1/a.jpg", "day1/a.ARW")
    assert db.get_meta(conn, "reorganised_at") is None
    assert reorganise.status(tmp_path) == {"reorganised": False, "moves": 0, "created": None}
    # and the shoot can be reorganised again
    p2 = reorganise.plan(tmp_path, by_people=False)
    assert p2["moves"] == 9


def _face(conn, pid: int, vec: np.ndarray, person_id: int | None = None) -> int:
    cur = conn.execute("INSERT INTO faces(photo_id,x,y,w,h,score,landmarks,eye_sharp,embed,person_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (pid, 0, 0, 40, 40, 0.9, "[]", 500.0, vec.astype(np.float32).tobytes(), person_id))
    conn.commit()
    return int(cur.lastrowid)


def _unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


def test_by_people_named_people_win_over_category_and_drone_wins_over_people(tmp_path):
    conn, ids = _shoot(tmp_path)
    meera = conn.execute("INSERT INTO people(name, n) VALUES('Meera', 1)").lastrowid
    nobody = conn.execute("INSERT INTO people(name, n) VALUES(NULL, 1)").lastrowid
    conn.commit()
    _face(conn, ids["b.jpg"], _unit(1), meera)            # named: goes under people/Meera
    _face(conn, ids["day1/a.jpg"], _unit(2), nobody)      # unnamed group: stays in its category
    _face(conn, ids["DJI_0003.JPG"], _unit(1), meera)     # drone wins over people
    # a saved reference face for Zara that is exactly a.jpg's face, so match_references finds it
    conn.execute("UPDATE photos SET n_faces=1 WHERE id IN (?,?,?)", (ids["b.jpg"], ids["day1/a.jpg"], ids["DJI_0003.JPG"])); conn.commit()
    p0 = reorganise.plan(tmp_path, by_people=False)
    assert p0["people"] == {} and {m["src_rel"]: m["dst_rel"] for m in p0["sample"]} == EXPECTED
    p = reorganise.plan(tmp_path, by_people=True)
    moves = {m["src_rel"]: m for m in p["sample"]}
    assert moves["b.jpg"]["dst_rel"] == "sorted/photos/people/Meera/b.jpg" and moves["b.jpg"]["reason"] == "person:Meera"
    assert moves["day1/a.jpg"]["dst_rel"] == "sorted/photos/beach/a.jpg"
    assert moves["DJI_0003.JPG"]["dst_rel"] == "sorted/drone/photos/other/DJI_0003.JPG"
    assert p["people"] == {"Meera": 1} and p["moves"] == 9
    # references count as names too, and a photo with two named people goes under the first alphabetically
    db.add_reference(conn, "Arjun", _unit(1), "ref.jpg")
    p2 = reorganise.plan(tmp_path, by_people=True)
    moves = {m["src_rel"]: m for m in p2["sample"]}
    assert moves["b.jpg"]["dst_rel"] == "sorted/photos/people/Arjun/b.jpg" and moves["b.jpg"]["reason"] == "person:Arjun"
    assert moves["b.jpg"]["also"] == ["Meera"]
    assert p2["people"] == {"Arjun": 1}
    out = reorganise.apply(tmp_path, p2["plan_id"])
    assert out["failed"] == 0 and (tmp_path / "sorted/photos/people/Arjun/b.jpg").is_file()
    man = json.loads((tmp_path / "sorted" / "UNDO.json").read_text())
    assert [m for m in man["moves"] if m["src_rel"] == "b.jpg"][0]["dst_rel"] == "sorted/photos/people/Arjun/b.jpg"
    assert reorganise.undo(tmp_path) == {"restored": 9, "failed": 0}
    assert (tmp_path / "b.jpg").is_file() and not (tmp_path / "sorted").exists()


def test_collisions_get_the_photo_id_suffix_on_the_whole_group(tmp_path):
    from conftest import make_image
    conn, ids = _shoot(tmp_path)
    (tmp_path / "day2").mkdir()
    make_image(tmp_path / "day2", "a.jpg", seed=9)
    (tmp_path / "day2" / "a.xmp").write_bytes(b"<xmp two/>")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET category='beach', category_score=0.9 WHERE rel='day2/a.jpg'"); conn.commit()
    pid2 = conn.execute("SELECT id FROM photos WHERE rel='day2/a.jpg'").fetchone()[0]
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    assert p["collisions"] == 1 and p["moves"] == 11
    dst = {m["src_rel"]: m["dst_rel"] for m in p["sample"]}
    assert dst["day1/a.jpg"] == "sorted/photos/beach/a.jpg" and dst["day1/a.xmp"] == "sorted/photos/beach/a.xmp"
    assert dst["day2/a.jpg"] == f"sorted/photos/beach/a_{pid2}.jpg" and dst["day2/a.xmp"] == f"sorted/photos/beach/a_{pid2}.xmp"
    out = reorganise.apply(tmp_path, p["plan_id"])
    assert out["failed"] == 0
    after = _listing(tmp_path)
    assert after[f"sorted/photos/beach/a_{pid2}.jpg"] == before["day2/a.jpg"] and after["sorted/photos/beach/a.jpg"] == before["day1/a.jpg"]
    assert reorganise.undo(tmp_path) == {"restored": 11, "failed": 0}
    assert _listing(tmp_path) == before


def test_guard_read_only_root(tmp_path):
    conn, ids = _shoot(tmp_path)
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, 0o555)
    try:
        with pytest.raises(ValueError, match="not writable"):
            reorganise.plan(tmp_path, by_people=False)
    finally:
        os.chmod(tmp_path, stat.S_IMODE(mode))
    assert not (tmp_path / ".photosort-write-test").exists()


def test_guard_camera_card_structure(tmp_path):
    conn, ids = _shoot(tmp_path)
    (tmp_path / "card" / "DCIM").mkdir(parents=True)
    with pytest.raises(ValueError, match="this looks like a camera card, copy it to a disk first"):
        reorganise.plan(tmp_path, by_people=False)
    (tmp_path / "card" / "DCIM").rmdir(); (tmp_path / "card").rmdir()
    assert reorganise.plan(tmp_path, by_people=False)["moves"] == 9
    conn.execute("UPDATE photos SET rel='PRIVATE/M4ROOT/CLIP/C0001.MP4' WHERE rel='sony/C0001.MP4'"); conn.commit()
    with pytest.raises(ValueError, match="camera card"):
        reorganise.plan(tmp_path, by_people=False)


def test_guard_stale_index(tmp_path):
    conn, ids = _shoot(tmp_path)
    p = tmp_path / "b.jpg"; t = p.stat().st_mtime
    os.utime(p, (t + 10, t + 10))
    with pytest.raises(ValueError, match="1 file.*re-run Index first"):
        reorganise.plan(tmp_path, by_people=False)
    os.utime(p, (t, t))
    (tmp_path / "day1" / "a.ARW").unlink()          # a missing RAW sibling is stale too
    with pytest.raises(ValueError, match="re-run Index first"):
        reorganise.plan(tmp_path, by_people=False)


def test_guard_already_reorganised_and_undo_pending(tmp_path):
    conn, ids = _shoot(tmp_path)
    (tmp_path / "sorted" / "photos").mkdir(parents=True)
    os.rename(tmp_path / "b.jpg", tmp_path / "sorted" / "photos" / "b.jpg")
    conn.execute("UPDATE photos SET rel='sorted/photos/b.jpg' WHERE rel='b.jpg'"); conn.commit()
    with pytest.raises(ValueError, match="already reorganised"):
        reorganise.plan(tmp_path, by_people=False)
    (tmp_path / "sorted" / "UNDO.json").write_text(json.dumps({"format": "photosort-undo/1", "root": str(tmp_path), "created": "x", "moves": []}))
    with pytest.raises(ValueError, match="undo the previous reorganise first"):
        reorganise.plan(tmp_path, by_people=False)


def test_guard_spanning_filesystems(tmp_path, monkeypatch):
    conn, ids = _shoot(tmp_path)
    real = os.stat
    class Dev:
        def __init__(self, st, dev): self._st, self.st_dev = st, dev
        def __getattr__(self, k): return getattr(self._st, k)
    def fake_stat(p, *a, **k):
        st = real(p, *a, **k)
        return Dev(st, st.st_dev + 1) if str(p).endswith("b.jpg") else st
    monkeypatch.setattr(reorganise.os, "stat", fake_stat)
    with pytest.raises(ValueError, match="more than one disk"):
        reorganise.plan(tmp_path, by_people=False)


def test_apply_needs_a_plan_for_this_root(tmp_path, tmp_path_factory):
    conn, ids = _shoot(tmp_path)
    with pytest.raises(ValueError, match="run the plan first"):
        reorganise.apply(tmp_path, "no-such-plan")
    p = reorganise.plan(tmp_path, by_people=False)
    other = tmp_path_factory.mktemp("other")
    with pytest.raises(ValueError, match="run the plan first"):
        reorganise.apply(other, p["plan_id"])
    p2 = reorganise.plan(tmp_path, by_people=False)          # a new plan retires the old one
    with pytest.raises(ValueError, match="run the plan first"):
        reorganise.apply(tmp_path, p["plan_id"])
    assert reorganise.apply(tmp_path, p2["plan_id"])["failed"] == 0
    assert reorganise.undo(tmp_path)["failed"] == 0


def test_rename_failure_midway_leaves_an_exact_manifest(tmp_path, monkeypatch):
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    real = os.rename; calls = {"n": 0}
    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk hiccup")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(reorganise.os, "rename", flaky)
    out = reorganise.apply(tmp_path, p["plan_id"])
    assert out["done"] == 9 and out["failed"] == 1 and len(out["failures"]) == 1 and "disk hiccup" in out["failures"][0]
    man = json.loads((tmp_path / "sorted" / "UNDO.json").read_text())
    assert len(man["moves"]) == 8
    after = _listing(tmp_path)
    moved = {m["src_rel"]: m["dst_rel"] for m in man["moves"]}
    for src, dst in moved.items():
        assert after[dst] == before[src] and src not in after
    stuck = [m for m in p["sample"] if m["src_rel"] not in moved][0]
    assert after[stuck["src_rel"]] == before[stuck["src_rel"]] and stuck["dst_rel"] not in after
    monkeypatch.setattr(reorganise.os, "rename", real)
    assert reorganise.undo(tmp_path) == {"restored": 8, "failed": 0}
    assert _listing(tmp_path) == before


def test_undo_survives_a_hand_moved_file_and_keeps_the_rest_of_the_manifest(tmp_path):
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    assert reorganise.apply(tmp_path, p["plan_id"])["failed"] == 0
    (tmp_path / "elsewhere").mkdir()
    os.rename(tmp_path / "sorted/photos/sunset/b.jpg", tmp_path / "elsewhere" / "b.jpg")
    u = reorganise.undo(tmp_path)
    assert u == {"restored": 8, "failed": 1}
    man = json.loads((tmp_path / "sorted" / "UNDO.json").read_text())
    assert [m["src_rel"] for m in man["moves"]] == ["b.jpg"]           # only what is still to undo
    assert reorganise.status(tmp_path)["moves"] == 1
    assert not (tmp_path / "sorted" / "photos" / "beach").exists() and (tmp_path / "sorted" / "photos" / "sunset").is_dir()
    os.rename(tmp_path / "elsewhere" / "b.jpg", tmp_path / "sorted/photos/sunset/b.jpg")
    assert reorganise.undo(tmp_path) == {"restored": 1, "failed": 0}
    (tmp_path / "elsewhere").rmdir()
    assert _listing(tmp_path) == before and not (tmp_path / "sorted").exists()


def test_nothing_to_undo_and_never_overwrites(tmp_path):
    conn, ids = _shoot(tmp_path)
    with pytest.raises(ValueError, match="nothing to undo"):
        reorganise.undo(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    # something appears at a destination between plan and apply: that file is refused, nothing is overwritten
    (tmp_path / "sorted" / "photos" / "sunset").mkdir(parents=True)
    (tmp_path / "sorted" / "photos" / "sunset" / "b.jpg").write_bytes(b"someone else's file")
    out = reorganise.apply(tmp_path, p["plan_id"])
    assert out["failed"] == 1 and "exists" in out["failures"][0]
    assert (tmp_path / "sorted/photos/sunset/b.jpg").read_bytes() == b"someone else's file" and (tmp_path / "b.jpg").is_file()
    assert db.connect(tmp_path).execute("SELECT rel FROM photos WHERE id=?", (ids["b.jpg"],)).fetchone()[0] == "b.jpg"
    assert reorganise.undo(tmp_path) == {"restored": 8, "failed": 0}
    assert (tmp_path / "sorted/photos/sunset/b.jpg").read_bytes() == b"someone else's file"   # not ours, left alone


def test_undo_treats_a_file_already_back_home_as_restored(tmp_path):
    """A manifest entry whose file is already at its original path (put back by hand, or a planned move a
    crash prevented) is not a failure: the manifest must not wedge on it."""
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    assert reorganise.apply(tmp_path, p["plan_id"])["failed"] == 0
    os.rename(tmp_path / "sorted/photos/sunset/b.jpg", tmp_path / "b.jpg")
    assert reorganise.undo(tmp_path) == {"restored": 9, "failed": 0}
    assert _listing(tmp_path) == before and not (tmp_path / "sorted").exists()
    assert db.connect(tmp_path).execute("SELECT rel FROM photos WHERE id=?", (ids["b.jpg"],)).fetchone()[0] == "b.jpg"


def test_db_failure_midway_still_leaves_an_exact_manifest(tmp_path, monkeypatch):
    conn, ids = _shoot(tmp_path)
    before = _listing(tmp_path)
    p = reorganise.plan(tmp_path, by_people=False)
    real = reorganise._apply_db; calls = {"n": 0}
    def flaky(conn_, m, rel):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("db went away")
        return real(conn_, m, rel)
    monkeypatch.setattr(reorganise, "_apply_db", flaky)
    with pytest.raises(RuntimeError):
        reorganise.apply(tmp_path, p["plan_id"])
    man = json.loads((tmp_path / "sorted" / "UNDO.json").read_text())
    assert len(man["moves"]) == 3                        # the third file moved before the db failed
    after = _listing(tmp_path)
    for m in man["moves"]:
        assert after[m["dst_rel"]] == before[m["src_rel"]] and m["src_rel"] not in after
    monkeypatch.setattr(reorganise, "_apply_db", real)
    assert reorganise.undo(tmp_path) == {"restored": 3, "failed": 0}
    assert _listing(tmp_path) == before and not (tmp_path / "sorted").exists()
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT rel, sibling FROM photos WHERE id=?", (ids["day1/a.jpg"],)).fetchone()[:] == ("day1/a.jpg", "day1/a.ARW")
