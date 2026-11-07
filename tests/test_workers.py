"""A worker that dies or hangs on one file costs that file, never the scan."""
import os
import time
import pytest
import worker_faults as wf
from fastapi.testclient import TestClient
from conftest import make_image
from photosort import db, index, workers
from photosort.index import index_folder
from photosort.server import create_app
from photosort.workers import supervised_map, why_it_died


def run(tasks, n=2, limit=30):
    return list(supervised_map(wf.echo, tasks, n, lambda t: limit, wf.lost))


def tasks(*names):
    return [("root", n) for n in names]


def test_every_task_comes_back_exactly_once():
    names = [f"f{i}" for i in range(40)]
    assert sorted(r["rel"] for r in run(tasks(*names), 3)) == sorted(names)


def test_no_tasks_starts_nothing():
    assert run([]) == []


def test_a_dead_worker_names_its_file_and_the_rest_go_on():
    got = {r["rel"]: r for r in run(tasks("a", "exit-1", "b", "segv-1", "c", "d"), 2)}
    assert sorted(got) == ["a", "b", "c", "d", "exit-1", "segv-1"]
    assert got["exit-1"]["quarantine"] == "the reader quit on this file (exit code 9)"
    assert got["segv-1"]["quarantine"] == "this file crashed the reader (SIGSEGV)"
    assert not got["a"]["error"] and not got["d"]["error"]


def test_every_file_crashing_still_ends():
    got = run(tasks(*[f"exit-{i}" for i in range(5)]), 2)
    assert len(got) == 5 and all(r["quarantine"] for r in got)


def test_the_watchdog_stops_a_hung_file_and_nothing_else():
    t0 = time.time()
    got = {r["rel"]: r for r in run(tasks("a", "hang-1", "b"), 2, limit=6)}
    assert time.time() - t0 < 30
    assert got["hang-1"]["quarantine"] == "still not read after 6 seconds, so it was set aside"
    assert not got["a"]["error"] and not got["b"]["error"]


def test_the_watchdog_takes_the_child_process_down_too(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAULT_PID_FILE", str(pid_file))
    run(tasks("child-1"), 1, limit=6)
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the child outlived the watchdog")


def test_stopping_early_stops_the_workers():
    it = supervised_map(wf.echo, tasks("a", "hang-1", "hang-2"), 2, lambda t: 300, wf.lost)
    t0 = time.time()
    next(it)
    it.close()
    assert time.time() - t0 < 30


def test_workers_that_cannot_start_are_an_error_and_not_a_hang(monkeypatch):
    monkeypatch.setattr(workers, "_worker", wf.die_at_start)
    with pytest.raises(RuntimeError, match="would not start"):
        list(supervised_map(wf.echo, tasks("a", "b"), 1, lambda t: 5, wf.lost))


@pytest.mark.parametrize("code,want", [(-11, "SIGSEGV"), (-9, "memory"), (-6, "SIGABRT"), (1, "exit code 1"), (None, "exit code None")])
def test_the_reasons_are_words(code, want):
    assert want in why_it_died(code)


def _shoot(root, names, size=(300, 200)):
    for i, n in enumerate(names):
        make_image(root, n, size=size, seed=i)


def test_a_scan_survives_a_crash_and_shows_why(tmp_path, monkeypatch):
    names = [f"p{i}.jpg" for i in range(8)]
    _shoot(tmp_path, names + ["exit.jpg", "segv.jpg"])
    monkeypatch.setattr(index, "process_one", wf.process_one)
    st = index_folder(tmp_path, faces=False, workers=2, embed=False)
    assert st["indexed"] == 8 and st["errors"] == 2 and st["quarantined"] == 2
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='pending'").fetchone()[0] == 0
    assert dict(conn.execute("SELECT rel, problem FROM photos WHERE status='error'").fetchall()) == {
        "exit.jpg": "the reader quit on this file (exit code 9)", "segv.jpg": "this file crashed the reader (SIGSEGV)"}
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/stats").json()["errors"] == 2
    listed = {e["rel"]: e["problem"] for e in c.get("/api/errors").json()["errors"]}
    assert listed["segv.jpg"] == "this file crashed the reader (SIGSEGV)"


def test_a_set_aside_file_is_left_alone_until_a_retry_and_a_good_read_clears_the_reason(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg", "exit.jpg"])
    real = index.process_one
    monkeypatch.setattr(index, "process_one", wf.process_one)
    index_folder(tmp_path, faces=False, workers=2, embed=False)
    assert index_folder(tmp_path, faces=False, workers=2, embed=False)["errors"] == 0       # Continue does not walk into it again
    monkeypatch.setattr(index, "process_one", real)
    st = index_folder(tmp_path, faces=False, workers=1, embed=False, retry_errors=True)
    assert st["indexed"] == 1 and st["errors"] == 0
    assert db.connect(tmp_path).execute("SELECT status, problem FROM photos WHERE rel='exit.jpg'").fetchone()[:] == ("ok", None)


def test_a_hung_file_times_out_inside_a_scan(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg", "hang.jpg", "b.jpg"])
    monkeypatch.setattr(index, "process_one", wf.process_one)
    monkeypatch.setattr(index, "PHOTO_WATCHDOG_S", 6)
    st = index_folder(tmp_path, faces=False, workers=2, embed=False)
    assert st["indexed"] == 2 and st["quarantined"] == 1
    assert "still not read after 6 seconds" in db.connect(tmp_path).execute(
        "SELECT problem FROM photos WHERE rel='hang.jpg'").fetchone()[0]


def test_a_crash_in_the_face_pass_keeps_the_photo_and_is_not_repeated(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    _shoot(tmp_path, ["a.jpg", "exit.jpg"])
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    monkeypatch.setattr(index, "faces_one", wf.faces_one)
    st = index_folder(tmp_path, faces=True, workers=2, embed=False)
    assert st["quarantined"] == 1 and st["faced"] == 1
    conn = db.connect(tmp_path)
    r = conn.execute("SELECT status, n_faces, problem FROM photos WHERE rel='exit.jpg'").fetchone()
    assert r[0] == "ok" and r[1] is None and "exit code 9" in r[2]
    assert db.photos_without_faces(conn) == set() and db.photos_without_faces(conn, retry=True) == {"exit.jpg"}


def test_a_disk_that_went_away_is_a_pause_and_not_a_quarantine(tmp_path, monkeypatch):
    """A worker that dies while the disk is gone says nothing about the file: it stays pending for Continue."""
    root = tmp_path / "shoot"; root.mkdir()
    _shoot(root, ["a.jpg", "exit.jpg"])
    real = supervised_map
    def away(fn, tasks_, n, limit, lost):
        for res in real(fn, tasks_, n, limit, lost):
            if res.get("quarantine"):
                root.rename(tmp_path / "gone")
            yield res
    monkeypatch.setattr(index, "supervised_map", away)
    monkeypatch.setattr(index, "process_one", wf.process_one)
    with pytest.raises(index.SourceUnavailable):
        index_folder(root, faces=False, workers=1, embed=False)
    (tmp_path / "gone").rename(root)
    assert db.connect(root).execute("SELECT status FROM photos WHERE rel='exit.jpg'").fetchone()[0] == "pending"


# ===== a full disk =====

def _fake_free(monkeypatch, free):
    class U: pass
    U.free = free
    monkeypatch.setattr(index.shutil, "disk_usage", lambda p: U)


def test_the_preflight_prices_only_what_is_left_to_read(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg", "b.jpg"])
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    from photosort.walk import find_images
    files = find_images(tmp_path)
    conn = db.connect(tmp_path)
    _fake_free(monkeypatch, 1000)
    assert index.index_preflight(tmp_path, files, db.known_files(conn)) is None       # Continue on a full disk with nothing to do
    short = index.index_preflight(tmp_path, files, {})
    assert short["free_bytes"] == 1000 and short["need_bytes"] > 2 * 465_000
    assert "scanning 2 items" in short["message"] and "--" not in short["message"] and "—" not in short["message"]


def test_20k_items_price_at_what_the_council_measured():
    assert index.index_need(20000, 0) == 9_300_000_000


def test_a_scan_that_cannot_fit_never_starts(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg"])
    c = TestClient(create_app(tmp_path))
    _fake_free(monkeypatch, 1000)
    import photosort.server as srv
    class Roomy: free = 10 ** 13
    r = c.post("/api/index", json={"faces": False}).json()
    assert r["started"] is False and r["blocked"]["reason"] in ("space", "index_space")
    # the shoot disk has room and the Mac does not: this is the case the old preflight missed
    real = srv.shutil.disk_usage
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Roomy if "home" not in str(p) else type("U", (), {"free": 1000}))
    r = c.post("/api/index", json={"faces": False}).json()
    assert r["started"] is False and r["blocked"]["reason"] == "index_space"
    assert "on this Mac" in r["blocked"]["message"]
    assert db.connect(tmp_path).execute("SELECT count(*) FROM photos").fetchone()[0] == 0


def test_cli_and_other_callers_get_the_same_refusal(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg"])
    _fake_free(monkeypatch, 1000)
    with pytest.raises(index.DiskFull, match="on this Mac"):
        index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert db.connect(tmp_path).execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_enospc_in_a_worker_stops_cleanly_with_no_error_rows(tmp_path, monkeypatch):
    names = [f"p{i}.jpg" for i in range(3)] + ["z-full.jpg"] + [f"q{i}.jpg" for i in range(3)]
    _shoot(tmp_path, names)
    real = index.process_one
    monkeypatch.setattr(index, "process_one", wf.full_disk_one)
    with pytest.raises(index.DiskFull, match="Continue scan"):
        index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    got = dict(conn.execute("SELECT status, count(*) FROM photos GROUP BY status").fetchall())
    assert "error" not in got and got["pending"] >= 1
    assert conn.execute("SELECT status FROM photos WHERE rel='z-full.jpg'").fetchone()[0] == "pending"
    job = conn.execute("SELECT state, error FROM jobs").fetchone()
    assert job[0] == "interrupted" and "Free up some space" in job[1]
    monkeypatch.setattr(index, "process_one", real)
    st = index_folder(tmp_path, faces=False, workers=1, embed=False)                      # Continue
    assert st["errors"] == 0 and st["indexed"] == got["pending"]
    assert dict(conn.execute("SELECT status, count(*) FROM photos GROUP BY status").fetchall()) == {"ok": 7}


def test_sqlite_full_stops_cleanly_too(tmp_path, monkeypatch):
    import sqlite3
    _shoot(tmp_path, ["a.jpg", "b.jpg"])
    calls = {"n": 0}
    real = db.upsert_photo
    def full(conn, row):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real(conn, row)
    monkeypatch.setattr(db, "upsert_photo", full)
    with pytest.raises(index.DiskFull):
        index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    assert dict(conn.execute("SELECT status, count(*) FROM photos GROUP BY status").fetchall()) == {"ok": 1, "pending": 1}


def test_the_server_reports_a_full_disk_as_a_pause(tmp_path, monkeypatch):
    _shoot(tmp_path, ["a.jpg", "full.jpg"])
    monkeypatch.setattr(index, "process_one", wf.full_disk_one)
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/index", json={"faces": False}).json()["started"]
    for _ in range(600):
        p = c.get("/api/progress").json()
        if not p.get("running") and p.get("stage") in ("paused", "error", "done"):
            break
        time.sleep(0.05)
    assert p["stage"] == "paused" and p["disk_full"] is True and "Continue scan" in p["error"]
    scan = c.get("/api/folder").json()["scan"]
    assert scan["errors"] == 0 and scan["pending"] >= 1


@pytest.mark.parametrize("exc,want", [(OSError(28, "No space left on device"), True), (OSError(69, "quota"), True),
                                      (OSError(5, "I/O"), False), (ValueError("x"), False)])
def test_is_disk_full(exc, want):
    assert db.is_disk_full(exc) is want
