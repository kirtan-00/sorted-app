import numpy as np
from photosort.index import index_folder
from photosort import db

def test_index_then_incremental(tmp_path):
    from conftest import make_image
    for i in range(6):
        make_image(tmp_path, f"p{i}.jpg", kind="sharp" if i < 4 else "blurry", seed=i)
    (tmp_path / "junk.jpg").write_bytes(b"nope")
    s1 = index_folder(tmp_path, faces=True, workers=2)
    assert s1["indexed"] == 6 and s1["errors"] == 1 and s1["embedded"] == 6
    conn = db.connect(tmp_path)
    rows = conn.execute("SELECT rel, sharp, qhash FROM photos WHERE status='ok' ORDER BY rel").fetchall()
    assert len(rows) == 6
    idx = db.index_dir(tmp_path)
    assert (idx / "thumbs" / f"{rows[0]['qhash']}.jpg").exists()
    assert (idx / "grid" / f"{rows[0]['qhash']}.jpg").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([f"p{i}.jpg" for i in range(6)] + ["junk.jpg"])   # nothing written into the shoot
    sharp = [r["sharp"] for r in rows]
    assert min(sharp[:4]) > max(sharp[4:])
    ids, M = db.load_embeds(conn); assert M.shape == (6, 512)
    s2 = index_folder(tmp_path, faces=True, workers=2)
    assert s2["skipped"] == 7 and s2["indexed"] == 0 and s2["errors"] == 0

def test_unmounted_root_refuses_and_keeps_index(tmp_path):
    """The disk got unplugged: the root vanishes. Indexing must refuse, not mark everything missing."""
    import shutil, pytest
    from conftest import make_image
    from photosort.index import SourceUnavailable
    shoot = tmp_path / "shoot"; shoot.mkdir()
    make_image(shoot, "a.jpg", seed=1); make_image(shoot, "b.jpg", seed=2)
    index_folder(shoot, faces=False, workers=1, embed=False)
    parked = tmp_path / "parked"; shutil.move(str(shoot), str(parked))
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False)
    conn = db.connect(shoot)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 2
    # mounted again but empty (wrong disk, or a bad eject left an empty mount point): still refuse
    shoot.mkdir()
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 2

def test_empty_new_folder_indexes_to_zero(tmp_path):
    """A brand-new empty folder is not an error; there is nothing to protect."""
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["total"] == 0 and s["indexed"] == 0

def test_missing_then_restored(tmp_path):
    from conftest import make_image
    import os, shutil
    p = make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=2)   # b.jpg stays, so the folder is never empty
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    st = p.stat(); backup = tmp_path.parent / "a_backup.jpg"; shutil.copy2(p, backup); p.unlink()
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "missing"
    shutil.copy2(backup, p); os.utime(p, (st.st_atime, st.st_mtime))
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["indexed"] == 0 and s["skipped"] == 2          # restored from the saved index, no re-decode
    assert conn.execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "ok"

def test_no_faces_then_faces_runs_faces_only(tmp_path):
    """faces=True on a shoot indexed with faces off runs detection on the stored thumbs: the rows are
    not re-decoded (embed, category and the focus label survive, thumbs untouched), stats say faced."""
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    s1 = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s1["indexed"] == 3 and s1["faced"] == 0
    conn = db.connect(tmp_path)
    assert [r[0] for r in conn.execute("SELECT n_faces FROM photos")] == [None, None, None]
    assert db.photos_without_faces(conn) == {"p0.jpg", "p1.jpg", "p2.jpg"}
    conn.execute("UPDATE photos SET embed=?, category='beach', focus='ok', focus_score=5", (b"\x01" * 1024,)); conn.commit()
    idx = db.index_dir(tmp_path)
    thumbs = {p.name: p.stat().st_mtime_ns for p in (idx / "thumbs").iterdir()}
    seen = []
    s2 = index_folder(tmp_path, faces=True, workers=1, embed=False, progress=lambda d: seen.append(dict(d)))
    assert s2["indexed"] == 0 and s2["faced"] == 3 and s2["skipped"] == 0 and s2["errors"] == 0
    assert [d["done"] for d in seen if d["stage"] == "faces"] == [1, 2, 3]
    assert [r[0] for r in conn.execute("SELECT n_faces FROM photos")] == [0, 0, 0]
    assert db.photos_without_faces(conn) == set()
    rows = conn.execute("SELECT embed, category, focus, focus_score, sharp, sharp_tile FROM photos").fetchall()
    assert all(r[0] == b"\x01" * 1024 and r[1] == "beach" and r[2] == "ok" and r[3] == 5 and r[4] == r[5] for r in rows)
    assert {p.name: p.stat().st_mtime_ns for p in (idx / "thumbs").iterdir()} == thumbs
    s3 = index_folder(tmp_path, faces=True, workers=1, embed=False)
    assert s3["indexed"] == 0 and s3["faced"] == 0 and s3["skipped"] == 3

def test_retry_errors_reprocesses_error_rows(tmp_path):
    from conftest import make_image
    bad = tmp_path / "bad.jpg"; bad.write_bytes(b"nope")
    s1 = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s1["errors"] == 1
    s2 = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s2["errors"] == 0 and s2["skipped"] == 1          # error rows are not retried by default
    s3 = index_folder(tmp_path, faces=False, workers=1, embed=False, retry_errors=True)
    assert s3["errors"] == 1 and s3["skipped"] == 0          # still broken, but it was tried again
    st = bad.stat(); make_image(tmp_path, "bad.jpg", seed=9); import os; os.utime(bad, (st.st_atime, st.st_mtime + 5))
    s4 = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s4["indexed"] == 1 and s4["errors"] == 0

def test_raw_and_std_run_in_separate_pools_with_one_counter(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    (tmp_path / "c.nef").write_bytes(b"not a raw file")
    seen = []
    s = index_folder(tmp_path, faces=False, workers=8, embed=False, progress=lambda d: seen.append(dict(d)))
    feats = [d for d in seen if d["stage"] == "features"]
    assert [d["done"] for d in feats] == [1, 2, 3] and all(d["total"] == 3 for d in feats)
    assert s["indexed"] == 2 and s["errors"] == 1

def test_faces_true_without_models_fails_fast(tmp_path, monkeypatch):
    import pytest
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    monkeypatch.setattr("photosort.index.YUNET_PATH", tmp_path / "missing.onnx")
    with pytest.raises(FileNotFoundError):
        index_folder(tmp_path, faces=True, workers=1, embed=False)

def test_progress_carries_stage_start(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=False, progress=seen.append)
    assert all("stage_started" in d for d in seen)
    feat = [d for d in seen if d["stage"] == "features"]
    assert feat and feat[0]["stage_started"] <= feat[-1]["stage_started"]

def test_transient_error_keeps_the_old_row_intact(tmp_path):
    """A photo that indexed fine, then fails to read on a later pass (disk hiccup, corrupt re-copy):
    the row flips to error but keeps its qhash, embed and category so a retry does not re-decode
    and re-embed from scratch."""
    import os
    from conftest import make_image
    p = make_image(tmp_path, "a.jpg", seed=3); make_image(tmp_path, "b.jpg", seed=4)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    old_qhash = conn.execute("SELECT qhash FROM photos WHERE rel='a.jpg'").fetchone()[0]
    conn.execute("UPDATE photos SET embed=?, category='beach', category_score=0.9, status='error' WHERE rel='a.jpg'", (b"\x00" * 1024,))
    conn.commit()
    st = p.stat(); p.write_bytes(b"nope" * (st.st_size // 4)); os.utime(p, (st.st_atime, st.st_mtime))   # same size+mtime, unreadable
    s = index_folder(tmp_path, faces=False, workers=1, embed=False, retry_errors=True)
    assert s["errors"] == 1 and s["indexed"] == 0
    row = conn.execute("SELECT status, qhash, embed, category, category_score FROM photos WHERE rel='a.jpg'").fetchone()
    assert row[0] == "error" and row[1] == old_qhash and row[2] == b"\x00" * 1024 and row[3] == "beach" and row[4] == 0.9
    assert sorted(x.name for x in tmp_path.iterdir()) == ["a.jpg", "b.jpg"]


def test_index_videos_alongside_photos(tmp_path, tmp_path_factory):
    from conftest import make_image, make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort.config import shoot_slug
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    before = sorted(x.name for x in tmp_path.iterdir())
    seen = []
    s = index_folder(tmp_path, faces=False, workers=1, embed=False, progress=lambda d: seen.append(dict(d)))
    assert s["indexed"] == 3 and s["errors"] == 0
    feats = [d for d in seen if d["stage"] == "features"]
    assert [d["done"] for d in feats] == [1, 2, 3] and all(d["total"] == 3 for d in feats)
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 3
    assert [r[0] for r in conn.execute("SELECT kind FROM photos WHERE rel IN ('a.jpg','b.jpg')")] == ["photo", "photo"]
    v = conn.execute("SELECT * FROM photos WHERE rel='clip.mp4'").fetchone()
    assert v["kind"] == "video" and abs(v["duration"] - 10.0) < 0.2 and v["n_faces"] == 0
    assert v["width"] == 320 and v["height"] == 240 and v["taken_at"] and v["sharp"] is not None and v["phash"]
    idx = db.index_dir(tmp_path)
    assert (idx / "thumbs" / f"{v['qhash']}.jpg").is_file() and (idx / "grid" / f"{v['qhash']}.jpg").is_file()
    frames = sorted((idx / "frames").glob(f"{v['qhash']}_*.jpg"))
    assert len(frames) == 6
    segs = conn.execute("SELECT idx, start, end, frame FROM segments WHERE photo_id=? ORDER BY idx", (v["id"],)).fetchall()
    assert [r["idx"] for r in segs] == [0, 1]
    assert segs[0]["start"] == 0.0 and abs(segs[0]["end"] - 5.0) < 0.2 and abs(segs[1]["end"] - 10.0) < 0.2
    for r in segs:
        assert r["frame"].startswith(v["qhash"] + "_") and (idx / "frames" / r["frame"]).is_file()
    assert sorted(x.name for x in tmp_path.iterdir()) == before            # ffmpeg only ever read the shoot
    s2 = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s2["skipped"] == 3 and s2["indexed"] == 0 and s2["errors"] == 0
    s3 = index_folder(tmp_path, faces=True, workers=1, embed=False)       # faces on: photos get a face pass, the video does not
    assert s3["indexed"] == 0 and s3["faced"] == 2 and s3["skipped"] == 1
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 2


def test_index_video_embeds_the_clip_and_its_segments(tmp_path, tmp_path_factory):
    from conftest import make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    s = index_folder(tmp_path, faces=False, workers=1, embed=True)
    assert s["indexed"] == 1 and s["embedded"] >= 1
    conn = db.connect(tmp_path)
    ids, M = db.load_embeds(conn)
    assert M.shape == (1, 512) and abs(float(np.linalg.norm(M[0])) - 1.0) < 1e-2
    seg_ids, S = db.load_segment_embeds(conn)
    assert S.shape == (2, 512) and all(abs(float(np.linalg.norm(S[i])) - 1.0) < 1e-2 for i in range(2))
    assert conn.execute("SELECT count(*) FROM segments WHERE embed IS NULL").fetchone()[0] == 0
    # the whole-clip embedding is the mean of its frames, so it sits between the two scenes
    assert float(S[0] @ M[0]) > 0.5 and float(S[1] @ M[0]) > 0.5
    s2 = index_folder(tmp_path, faces=False, workers=1, embed=True)
    assert s2["embedded"] == 0


def test_video_without_ffmpeg_is_an_error_row_not_a_crash(tmp_path, tmp_path_factory, monkeypatch):
    from conftest import make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    monkeypatch.setenv("PHOTOSORT_NO_FFMPEG", "1")                          # process_one runs in a spawned worker
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["errors"] == 1 and s["indexed"] == 0
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT status FROM photos WHERE rel='clip.mp4'").fetchone()[0] == "error"


def test_index_sets_aerial_from_metadata_for_dji_clips(tmp_path, tmp_path_factory):
    """A DJI_x.MP4 with its .SRT telemetry next to it is aerial=1 at index time (deterministic, no model);
    a normal clip is 0. The .SRT is never a row of its own."""
    import os, pytest
    from conftest import make_video, needs_ffmpeg
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    work = tmp_path_factory.mktemp("work")
    make_video(tmp_path / "DJI_0001.MP4", scenes=1, work=work)
    (tmp_path / "DJI_0001.SRT").write_text("1\n[iso : 100] [shutter : 1/1000]\n")
    make_video(tmp_path / "C0001.MP4", scenes=1, work=work)
    before = sorted(os.listdir(tmp_path))
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["indexed"] == 2 and s["errors"] == 0
    conn = db.connect(tmp_path)
    assert {r[0]: r[1] for r in conn.execute("SELECT rel, aerial FROM photos")} == {"DJI_0001.MP4": 1, "C0001.MP4": 0}
    assert db.aerial_count(conn) == 1
    assert sorted(os.listdir(tmp_path)) == before


# ===== resume: pending rows, scan counts, the job row =====

def test_scan_lists_pending_rows_and_counts_track_each_stage(tmp_path):
    """A scan writes a pending row per file before reading any, so scan_counts says how far it got
    from the index alone: read but not embedded is not complete; embedded is."""
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    conn = db.connect(tmp_path)
    assert db.scan_counts(conn) == dict(items=0, scanned=0, embedded=0, faced=0, errors=0, pending=0, pending_photos=0,
                                        pending_clips=0, photos=0, clips=0, scanned_photos=0, scanned_clips=0,
                                        unembedded=0, complete=True)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = db.scan_counts(conn)
    assert (c["items"], c["scanned"], c["embedded"], c["faced"], c["pending"], c["unembedded"], c["complete"]) == (3, 3, 0, 0, 0, 3, False)
    assert db.get_meta(conn, "scan_faces") == "0"
    job = db.latest_jobs(conn)["scan"]
    assert job["state"] == "done" and job["progress"]["stage"] == "done" and job["progress"]["faces"] is False and job["finished"]
    index_folder(tmp_path, faces=False, workers=1, embed=True)
    c = db.scan_counts(conn)
    assert (c["scanned"], c["embedded"], c["unembedded"], c["complete"]) == (3, 3, 0, True)


def test_unplugged_mid_scan_leaves_pending_rows_then_continue_finishes(tmp_path):
    """The disk goes away after the first file is read: the scan stops (SourceUnavailable), the rows
    already read stay ok, the rest stay pending (never error), the job row says interrupted. Plugging the
    disk back and scanning again reads only what is pending and ends complete."""
    import shutil, pytest
    from photosort.index import SourceUnavailable
    from conftest import make_image
    shoot = tmp_path / "shoot"; shoot.mkdir()
    for i in range(6):
        make_image(shoot, f"p{i}.jpg", seed=i)
    parked = tmp_path / "parked"
    def unplug_after_first(d):
        if d["stage"] == "features" and d["done"] >= 1 and shoot.is_dir():
            shutil.move(str(shoot), str(parked))
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False, progress=unplug_after_first)
    conn = db.connect(shoot)
    c = db.scan_counts(conn)
    assert c["items"] == 6 and c["errors"] == 0 and c["scanned"] >= 1 and c["pending"] >= 1 and c["scanned"] + c["pending"] == 6
    assert c["complete"] is False
    assert db.latest_jobs(conn)["scan"]["state"] == "interrupted"
    # still gone: the guard refuses without touching the rows
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False)
    assert db.scan_counts(conn) == c
    shutil.move(str(parked), str(shoot))
    s = index_folder(shoot, faces=False, workers=1, embed=False)
    assert s["indexed"] == c["pending"] and s["skipped"] == c["scanned"] and s["errors"] == 0
    c2 = db.scan_counts(conn)
    assert c2["scanned"] == 6 and c2["pending"] == 0 and db.latest_jobs(conn)["scan"]["state"] == "done"
    assert sorted(r[0] for r in conn.execute("SELECT DISTINCT status FROM photos")) == ["ok"]


def test_pending_rows_for_clips_are_counted_apart_and_dropped_when_the_file_goes(tmp_path):
    """add_pending keeps the kind so the UI can say '579 clips not scanned yet'; a pending row whose
    file is gone at the next scan is dropped, not marked missing (it never held anything)."""
    from photosort.walk import ImageFile
    from conftest import make_image
    make_image(tmp_path, "a.jpg", seed=1)
    conn = db.connect(tmp_path)
    fake = [ImageFile(tmp_path / "c.mp4", "c.mp4", 10, 1.0, False, is_video=True),
            ImageFile(tmp_path / "d.mov", "d.mov", 10, 1.0, False, is_video=True)]
    assert db.add_pending(conn, fake) == 2
    c = db.scan_counts(conn)
    assert (c["items"], c["clips"], c["pending_clips"], c["pending_photos"]) == (2, 2, 2, 0)
    assert db.known_files(conn) == {}                       # pending rows are never "already indexed"
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = db.scan_counts(conn)
    assert (c["items"], c["scanned"], c["pending"], c["clips"]) == (1, 1, 0, 0)
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_running_job_rows_are_marked_interrupted_when_the_shoot_opens(tmp_path):
    import pytest
    conn = db.connect(tmp_path)
    jid = db.start_job(conn, "scan", {"stage": "features", "done": 12, "total": 40, "faces": True})
    db.job_progress(conn, jid, {"stage": "features", "done": 20, "total": 40, "faces": True})
    assert db.latest_jobs(conn)["scan"]["state"] == "running"
    assert db.interrupt_running_jobs(conn) == 1 and db.interrupt_running_jobs(conn) == 0
    j = db.latest_jobs(conn)["scan"]
    assert j["state"] == "interrupted" and j["progress"]["done"] == 20 and j["finished"]
    with pytest.raises(ValueError):
        db.finish_job(conn, jid, "running")
