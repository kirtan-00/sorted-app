import numpy as np
from photosort.index import index_folder
from photosort import db
from photosort.config import EMBED_DIM

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
    ids, M = db.load_embeds(conn); assert M.shape == (6, EMBED_DIM)
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

def test_progress_numbers_the_steps_of_this_pass(tmp_path):
    """Step 1 of 2 rather than the word Scanning twice. The first scan emit comes before the disk has been
    listed, so it cannot know how many steps there are; every emit after the walk can."""
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=False, progress=seen.append)
    assert (seen[0]["stage"], seen[0]["step"], seen[0]["steps"]) == ("scan", None, None)
    scan = [d for d in seen if d["stage"] == "scan"]
    assert (scan[-1]["step"], scan[-1]["steps"], scan[-1]["total"]) == (1, 2, 1)
    feat = [d for d in seen if d["stage"] == "features"]
    assert feat and all((d["step"], d["steps"]) == (2, 2) for d in feat)
    assert seen[-1]["stage"] == "done" and seen[-1]["step"] is None      # the end of the pass, not a step of it

def test_the_step_count_only_counts_the_stages_this_pass_will_run(tmp_path):
    """A faces-only pass over photos already read has no features stage at all, so faces is step 2 of 2 and
    not step 3 of 4. Hardcoding the count in the UI would number it wrong on exactly this pass."""
    from conftest import make_image
    for i in range(2):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    seen = []
    index_folder(tmp_path, faces=True, workers=1, embed=False, progress=seen.append)
    faces = [d for d in seen if d["stage"] == "faces"]
    assert faces and all((d["step"], d["steps"]) == (2, 2) for d in faces)
    assert not [d for d in seen if d["stage"] == "features"]

def test_progress_says_which_file_is_being_read(tmp_path):
    """On a client card with a dozen top-level folders, "392 of 644" does not say where it is. The rel path
    of the file in flight rides along, so the panel can name the folder being read."""
    from conftest import make_image
    deep = tmp_path / "SHIVALIK FURNITURE DEALEARS MEET (06-08-2026)" / "FINAL JPG"
    deep.mkdir(parents=True)
    make_image(deep, "DSC01158.jpg")
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=False, progress=seen.append)
    feat = [d for d in seen if d["stage"] == "features"]
    assert [d["current"] for d in feat] == ["SHIVALIK FURNITURE DEALEARS MEET (06-08-2026)/FINAL JPG/DSC01158.jpg"]
    assert all(d["current"] is None for d in seen if d["stage"] in ("scan", "done"))
    seen.clear()
    index_folder(tmp_path, faces=True, workers=1, embed=False, progress=seen.append)   # the faces-only pass
    faces = [d for d in seen if d["stage"] == "faces"]
    assert faces and all(d["current"].endswith("DSC01158.jpg") for d in faces)

def test_an_enormous_path_is_cut_from_the_left(tmp_path):
    """The tail identifies the file, so a path too long to carry loses its head, not its tail. A guard
    against one pathological path, not the display rule: the UI elides to whatever width it has."""
    from photosort.index import _short_rel, CURRENT_MAX
    assert _short_rel(None) is None and _short_rel("a/b.jpg") == "a/b.jpg"
    deep = "/".join(["SHIVALIK FURNITURE DEALEARS MEET (06-08-2026)"] * 4) + "/FINAL JPG/DSC01158.jpg"
    cut = _short_rel(deep)
    assert len(cut) <= CURRENT_MAX and cut.startswith("\u2026/") and cut.endswith("/FINAL JPG/DSC01158.jpg")
    assert len(_short_rel("x" * 400 + ".jpg")) <= CURRENT_MAX          # no component boundary to cut on

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
    assert M.shape == (1, EMBED_DIM) and abs(float(np.linalg.norm(M[0])) - 1.0) < 1e-2
    seg_ids, S = db.load_segment_embeds(conn)
    assert S.shape == (2, EMBED_DIM) and all(abs(float(np.linalg.norm(S[i])) - 1.0) < 1e-2 for i in range(2))
    assert conn.execute("SELECT count(*) FROM segments WHERE embed IS NULL").fetchone()[0] == 0
    # the whole-clip embedding is the mean of its frames, so it sits between the two scenes
    assert float(S[0] @ M[0]) > 0.5 and float(S[1] @ M[0]) > 0.5
    s2 = index_folder(tmp_path, faces=False, workers=1, embed=True)
    assert s2["embedded"] == 0


def test_the_embed_stage_names_its_three_sub_stages(tmp_path, tmp_path_factory):
    """A clip's vector is the mean of every sampled frame, about eight encodes with a commit each, while
    the photos and segments on either side of it go through EMBED_BATCH. The rate drops about seven times
    in the middle of one stage and recovers at the end. Named, that reads as a different kind of work;
    unnamed, as on the GT 1 TB scan, it reads as a hang."""
    from conftest import make_image, make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    make_image(tmp_path, "a.jpg")
    make_video(tmp_path / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=True, progress=seen.append)
    emb = [d for d in seen if d["stage"] == "embed"]
    assert [d["substage"] for d in emb] == ["photos", "clips", "segments"]
    assert [(d["substage_done"], d["substage_total"]) for d in emb] == [(1, 1), (1, 1), (2, 2)]
    # each sub-stage times itself, so an estimate can be made from the rate of the work actually running
    assert emb[0]["substage_started"] <= emb[1]["substage_started"] <= emb[2]["substage_started"]
    assert [d["done"] for d in emb] == [1, 2, 4] and all(d["total"] == 4 for d in emb)
    assert all(d["substage"] is None and d["substage_started"] is None
               for d in seen if d["stage"] != "embed")

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
                                        unembedded=0, complete=True, stale_embeds=0, embed_model=None)
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

def test_raw_twins_are_recorded_never_opened(tmp_path):
    """A RAW+JPEG shoot: only the JPEG is decoded for thumbs, features and embeds; its RAW is recorded as the
    sibling. The RAWs here are junk bytes, so opening one would make an error row: none may appear, in the
    same-folder layout, the JPG/ + RAW/ sibling layout, and the sibling layout with the counter rolled over
    between days (the layout that used to decode every RAW)."""
    from conftest import make_image
    n = 0
    (tmp_path / "same").mkdir()
    for i in range(4):
        make_image(tmp_path / "same", f"DSC{i:05d}.JPG", size=(400, 300), seed=i); n += 1
        (tmp_path / "same" / f"DSC{i:05d}.ARW").write_bytes(b"\x00" * 4096)
    for day in ("day1", "day2"):
        for i in range(4):                                   # DSC00000..3 in BOTH days
            (tmp_path / day / "RAW").mkdir(parents=True, exist_ok=True); (tmp_path / day / "JPG").mkdir(exist_ok=True)
            make_image(tmp_path / day / "JPG", f"DSC{i:05d}.JPG", size=(400, 300), seed=10 + i); n += 1
            (tmp_path / day / "RAW" / f"DSC{i:05d}.ARW").write_bytes(b"\x00" * 4096)
    stats = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert stats["indexed"] == n == 12 and stats["errors"] == 0 and stats["total"] == 12
    conn = db.connect(tmp_path)
    rows = {r[0]: r[1] for r in conn.execute("SELECT rel, sibling FROM photos")}
    assert len(rows) == 12 and all(r.endswith(".JPG") for r in rows)
    assert rows["same/DSC00001.JPG"] == "same/DSC00001.ARW"
    assert rows["day1/JPG/DSC00002.JPG"] == "day1/RAW/DSC00002.ARW" and rows["day2/JPG/DSC00002.JPG"] == "day2/RAW/DSC00002.ARW"


def test_index_stores_the_clips_frame_rate(tmp_path, tmp_path_factory):
    """probe() has always computed fps and the row used to drop it. A still gets NULL: a frame rate is
    not a thing a still has, so it is absent rather than zero. The scan also stamps FPS_READ_KEY, so a
    shoot indexed by this version is never offered the catch-up pass."""
    from conftest import make_image, make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort.index import CLIP_READ_KEY, FPS_READ_KEY, fps_status
    make_image(tmp_path, "a.jpg", seed=1)
    make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    rows = {r["rel"]: r["fps"] for r in conn.execute("SELECT rel, fps FROM photos")}
    assert rows["a.jpg"] is None
    assert rows["clip.mp4"] == 10.0            # conftest.make_video writes testsrc at rate=10
    assert db.get_meta(conn, FPS_READ_KEY) is not None
    assert fps_status(tmp_path) == {"clips": 1, "known": 1, "unknown": 0,
                                    "read_at": db.get_meta(conn, CLIP_READ_KEY)}
    assert db.slowmo_counts(conn) == {"slow": 0, "normal": 1, "unknown": 0}


def test_read_fps_backfills_an_index_that_has_none(tmp_path, tmp_path_factory):
    """The client's path: 1,140 clips already indexed, fps NULL on every one of them, and nobody is
    going to rescan. read_fps reads the container header and nothing else, so no thumb, frame or
    embedding is touched and the pass can be run again safely."""
    from conftest import make_image, make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort.index import CLIP_READ_KEY, FPS_READ_KEY, fps_status, read_fps
    make_image(tmp_path, "a.jpg", seed=1)
    make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    idx = db.index_dir(tmp_path)
    # back to the state of an index written before the column existed
    conn.execute("UPDATE photos SET fps=NULL"); conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (FPS_READ_KEY, CLIP_READ_KEY))
    conn.commit()
    thumbs = sorted(p.name for p in (idx / "thumbs").iterdir())
    frames = sorted(p.name for p in (idx / "frames").iterdir())
    assert fps_status(tmp_path) == {"clips": 1, "known": 0, "unknown": 1, "read_at": None}

    seen = []
    assert read_fps(tmp_path, progress=lambda d: seen.append(dict(d))) == {"found": 1, "read": 1, "missing": 0}
    conn2 = db.connect(tmp_path)
    assert conn2.execute("SELECT fps FROM photos WHERE rel='clip.mp4'").fetchone()[0] == 10.0
    assert conn2.execute("SELECT fps FROM photos WHERE rel='a.jpg'").fetchone()[0] is None
    assert db.get_meta(conn2, FPS_READ_KEY) is not None
    assert seen and seen[-1] == {"stage": "fps", "done": 1, "total": 1}
    # nothing under the index moved, and the shoot folder still holds only what it came with
    assert sorted(p.name for p in (idx / "thumbs").iterdir()) == thumbs
    assert sorted(p.name for p in (idx / "frames").iterdir()) == frames
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "clip.mp4"]
    # idempotent: a second run has nothing to do rather than re-probing
    assert read_fps(tmp_path) == {"found": 0, "read": 0, "missing": 0}


def test_read_fps_counts_a_clip_that_is_not_on_the_disk_as_missing(tmp_path):
    """Half the shoot unplugged must not turn into a row that says "this clip has no frame rate". It
    stays NULL and is counted missing, so running the pass again with the disk mounted finishes it."""
    from photosort.index import read_fps
    conn = db.connect(tmp_path)
    db.upsert_photo(conn, dict(rel="gone.mp4", size=1, mtime=1.0, qhash="h", sibling=None, width=1920,
                               height=1080, taken_at=None, camera=None, phash="0" * 16, sharp_tile=1.0,
                               sharp_max=1.0, sharp_eye=None, sharp=1.0, n_faces=0, status="ok",
                               kind="video", duration=3.0))
    conn.commit()
    assert read_fps(tmp_path) == {"found": 0, "read": 1, "missing": 1}
    assert db.connect(tmp_path).execute("SELECT fps FROM photos").fetchone()[0] is None
    # nothing was reachable, so nothing was read: the offer stays up for when the disk is plugged in
    from photosort.index import clip_info_status
    assert clip_info_status(tmp_path)["read_at"] is None


# ===== clip facts: Sony's sidecar at scan time and in the catch-up, faces on the frames already on the Mac =====
def _sony_clip(tmp_path, work, stem="C0001", gamma="s-log3-cine", capture="100p"):
    from conftest import make_video
    from test_video import SONY_XML_FULL
    make_video(tmp_path / f"{stem}.MP4", scenes=1, work=work)
    (tmp_path / f"{stem}M01.XML").write_text(SONY_XML_FULL.format(gamma=gamma, capture=capture))


def test_index_reads_the_sony_sidecar_for_camera_profile_and_capture_rate(tmp_path, tmp_path_factory):
    """Sony puts no make or model in the container, so the sidecar is where the body comes from; the
    profile and the S&Q capture rate ride in the same 64 KB read. A clip with no sidecar gets gamma ''
    (looked for, nothing there), which is why the catch-up pass has nothing to offer a fresh scan."""
    from conftest import make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort.index import CLIP_READ_KEY, clip_info_status
    work = tmp_path_factory.mktemp("work")
    _sony_clip(tmp_path, work)
    make_video(tmp_path / "phone.mp4", scenes=1, work=work)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    rows = {r["rel"]: dict(r) for r in conn.execute("SELECT rel, camera, gamma, capture_fps, fps, people, face_h FROM photos")}
    assert rows["C0001.MP4"]["camera"] == "SONY ILCE-7SM3"
    assert rows["C0001.MP4"]["gamma"] == "s-log3-cine" and rows["C0001.MP4"]["capture_fps"] == 100.0
    assert rows["phone.mp4"]["gamma"] == "" and rows["phone.mp4"]["camera"] is None
    assert rows["phone.mp4"]["people"] is None and rows["phone.mp4"]["face_h"] is None     # faces were off
    assert db.slowmo_counts(conn)["slow"] == 1                       # 10 fps in the container, 100 captured
    assert db.look_counts(conn) == {"log": 1, "standard": 0, "unknown": 1}
    assert clip_info_status(tmp_path) == {"clips": 2, "known": 2, "unknown": 0, "read_at": db.get_meta(conn, CLIP_READ_KEY)}


def test_read_clip_info_backfills_profile_and_camera_without_touching_the_index_folders(tmp_path, tmp_path_factory):
    """The client's shoots were read before any of this; the header pass fills the profile, the capture
    rate and an empty camera from the sidecar, keeps a camera the row already has, and stamps its own key,
    so a shoot that already ran v0.4.0's frame rate read is still offered it."""
    from conftest import needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort.index import CLIP_READ_KEY, FPS_READ_KEY, clip_info_status, read_clip_info
    work = tmp_path_factory.mktemp("work")
    _sony_clip(tmp_path, work, "C0001")
    _sony_clip(tmp_path, work, "C0002", gamma="rec709", capture="25p")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET gamma=NULL, capture_fps=NULL, camera=NULL WHERE rel='C0001.MP4'")
    conn.execute("UPDATE photos SET gamma=NULL, camera='kept' WHERE rel='C0002.MP4'")
    conn.execute("DELETE FROM meta WHERE key=?", (CLIP_READ_KEY,)); conn.commit()
    assert db.get_meta(conn, FPS_READ_KEY) is not None               # the v0.4.0 read already ran
    assert clip_info_status(tmp_path) == {"clips": 2, "known": 0, "unknown": 2, "read_at": None}
    idx = db.index_dir(tmp_path)
    before = sorted(p.name for p in (idx / "frames").iterdir())
    assert read_clip_info(tmp_path) == {"found": 0, "read": 2, "missing": 0}      # fps was already in
    rows = {r["rel"]: tuple(r)[1:] for r in db.connect(tmp_path).execute("SELECT rel, camera, gamma, capture_fps FROM photos")}
    assert rows == {"C0001.MP4": ("SONY ILCE-7SM3", "s-log3-cine", 100.0), "C0002.MP4": ("kept", "rec709", 25.0)}
    assert clip_info_status(tmp_path)["unknown"] == 0 and clip_info_status(tmp_path)["read_at"]
    assert sorted(p.name for p in (idx / "frames").iterdir()) == before
    assert read_clip_info(tmp_path) == {"found": 0, "read": 0, "missing": 0}


class _StubFace:
    def __init__(self, h, x=0):
        self.x, self.y, self.w, self.h, self.score = x, 0, h, h, 0.9
        self.embed = np.ones(128, np.float32) / np.sqrt(128)


def test_clip_face_summary_takes_medians():
    """people: the median per-frame count over every frame, rounded down; face_h: the median over the frames
    with a face of the largest face's share of the frame's short edge."""
    from photosort.index import clip_face_summary
    F = _StubFace
    per = [[F(60), F(20)], [F(48)], [], [F(72), F(10), F(5)], [F(54)]]
    assert clip_face_summary(per, [240] * 5) == (1, round(float(np.median([60, 48, 72, 54])) / 240, 4))
    assert clip_face_summary([[], [], [F(30)]], [240] * 3) == (0, 0.125)
    assert clip_face_summary([[], []], [240, 240]) == (0, None)


def test_a_vertical_clip_measures_its_faces_against_the_width(monkeypatch):
    """A waist up shot on a vertical phone clip puts the face at half the share of the height it would have in
    landscape; measured against the short edge, the same framing reads the same either way round."""
    from PIL import Image
    from photosort import index as I
    class Eng:
        def detect(self, im): return [_StubFace(270)]
    monkeypatch.setattr(I, "_ENGINE", Eng())
    people, face_h, rows = I._clip_faces([Image.new("RGB", (1080, 1920)), Image.new("RGB", (1920, 1080))])
    assert (people, face_h) == (1, 0.25) and [r["frame"] for r in rows] == [0, 1]


def test_find_clip_faces_runs_on_the_frames_on_the_mac_with_the_shoot_unplugged(tmp_path, tmp_path_factory, monkeypatch):
    """No disk: the pass reads frames/ under the index. Every frame of the stub clip has one face a quarter of
    the frame tall, so the clip is one person at face_h 0.25, and its faces are kept per frame for a person
    search later. people NULL is the only thing that marks a clip as still to do, so a second run is a no-op."""
    from conftest import make_video, needs_ffmpeg
    import pytest, shutil
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort import index as I
    shoot = tmp_path / "shoot"; shoot.mkdir()
    make_video(shoot / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    index_folder(shoot, faces=False, workers=1, embed=False)
    class Eng:
        def detect(self, im): return [_StubFace(im.height // 4)]
    monkeypatch.setattr(I, "_ENGINE", Eng())
    assert I.clip_faces_status(shoot) == {"clips": 1, "scanned": 0, "pending": 1}
    shutil.move(str(shoot), str(tmp_path / "unplugged"))           # the disk is gone; the frames are not
    try:
        assert I.find_clip_faces(shoot) == {"found": 1, "read": 1, "missing": 0}
    finally:
        shutil.move(str(tmp_path / "unplugged"), str(shoot))
    conn = db.connect(shoot)
    r = conn.execute("SELECT id, people, face_h, n_faces FROM photos").fetchone()
    assert (r["people"], r["face_h"], r["n_faces"]) == (1, 0.25, 0)  # n_faces stays 0: classify's face rule never sees a clip
    n_frames = len(list((db.index_dir(shoot) / "frames").glob("*.jpg")))
    assert conn.execute("SELECT count(*), count(DISTINCT frame) FROM clip_faces WHERE photo_id=?", (r["id"],)).fetchone()[:] == (n_frames, n_frames)
    assert I.clip_faces_status(shoot) == {"clips": 1, "scanned": 1, "pending": 0}
    assert I.find_clip_faces(shoot) == {"found": 0, "read": 0, "missing": 0}


def test_a_scan_with_faces_on_catches_up_old_clips_last(tmp_path, tmp_path_factory, monkeypatch):
    """An unchanged clip with people NULL gets the clip faces stage as the last step of a faces-on scan, and
    the stage is in the step count; a scan with faces off never runs it."""
    from conftest import make_video, needs_ffmpeg
    import pytest
    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg not installed")
    from photosort import index as I
    make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path_factory.mktemp("work"))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=False, progress=seen.append)
    assert not [d for d in seen if d["stage"] == "clipfaces"]
    class Eng:
        def detect(self, im): return []
    monkeypatch.setattr(I, "_ENGINE", Eng())
    seen = []
    s = index_folder(tmp_path, faces=True, workers=1, embed=False, progress=seen.append)
    cf = [d for d in seen if d["stage"] == "clipfaces"]
    assert cf and cf[-1]["done"] == 1 and all((d["step"], d["steps"]) == (2, 2) for d in cf)
    assert s["clip_faced"] == 1
    assert db.connect(tmp_path).execute("SELECT people, face_h FROM photos").fetchone()[:] == (0, None)
