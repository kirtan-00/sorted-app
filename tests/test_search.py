from photosort import db
from photosort.config import EMBED_DIM
from photosort.index import index_folder
from photosort.search import Index, Filters
from PIL import Image

def test_search_and_filters(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "sharp.jpg", kind="sharp"); make_image(tmp_path, "soft.jpg", kind="blurry")
    Image.new("RGB", (900, 600), (200, 30, 30)).save(tmp_path / "red.jpg")
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    allp = ix.search(); assert len(allp) == 3 and all("sharp_pct" in r for r in allp)
    top = ix.search(text="a red wall")[0]; assert top["rel"] == "red.jpg"
    # red.jpg is a flat colour so it scores 0 sharpness; only sharp.jpg is above the 60th percentile
    sharp_only = ix.search(filters=Filters(sharp_min_pct=60)); assert [r["rel"] for r in sharp_only] == ["sharp.jpg"]
    like = ix.search(image_id=top["id"]); assert like[0]["id"] == top["id"]
    assert ix.search(filters=Filters(faces="one")) == []

def test_category_filter(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg", kind="sharp"); make_image(tmp_path, "b.jpg", kind="blurry")
    index_folder(tmp_path, faces=False, workers=1)
    conn = db.connect(tmp_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM photos ORDER BY rel")]
    conn.execute("UPDATE photos SET category='beach' WHERE id=?", (ids[0],))
    conn.commit()   # b.jpg's category stays NULL: never classified

    ix = Index(tmp_path)
    beach = ix.search(filters=Filters(category="beach"))
    assert [r["rel"] for r in beach] == ["a.jpg"] and beach[0]["category"] == "beach"
    assert ix.search(filters=Filters(category="road")) == []
    unclassified = ix.search(filters=Filters(category="unclassified"))
    assert [r["rel"] for r in unclassified] == ["b.jpg"]

def test_search_offset_pages_through_query(tmp_path):
    from conftest import make_image
    from photosort.index import index_folder
    from photosort.search import Index
    for i in range(5): make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ix = Index(tmp_path)
    everything = ix.query()
    assert [r["rel"] for r in everything] == [f"p{i}.jpg" for i in range(5)]
    assert [r["rel"] for r in ix.search(limit=2, offset=0)] == ["p0.jpg", "p1.jpg"]
    assert [r["rel"] for r in ix.search(limit=2, offset=4)] == ["p4.jpg"]
    assert ix.search(limit=2, offset=99) == []

def test_cluster_filter(tmp_path):
    """Filters(cluster=name) selects on the stored photos.cluster column (a discovered category)."""
    from conftest import make_image
    make_image(tmp_path, "a.jpg", kind="sharp"); make_image(tmp_path, "b.jpg", kind="blurry")
    index_folder(tmp_path, faces=False, workers=1)
    conn = db.connect(tmp_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM photos ORDER BY rel")]
    conn.execute("UPDATE photos SET cluster='excavator', cluster_score=0.9 WHERE id=?", (ids[0],))
    conn.commit()
    ix = Index(tmp_path)
    hits = ix.search(filters=Filters(cluster="excavator"))
    assert [r["rel"] for r in hits] == ["a.jpg"] and hits[0]["cluster"] == "excavator"
    assert ix.search(filters=Filters(cluster="crane")) == []

def _shoot_with_scores(tmp_path, rows):
    """rows: (rel, category, category_score, category_guess, category_guess_score, cluster, cluster_score)."""
    from conftest import make_image
    for i, r in enumerate(rows): make_image(tmp_path, r[0], seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    for r in rows:
        conn.execute("UPDATE photos SET category=?, category_score=?, category_guess=?, category_guess_score=?, cluster=?, cluster_score=? WHERE rel=?",
                     r[1:] + (r[0],))
    conn.commit()

def test_category_search_puts_sure_first_then_less_sure_by_confidence(tmp_path):
    """category=X is the union of photos filed under X and photos filed under "other" whose best guess
    was X. Sure ones (score >= 0.5) come first in capture order; the rest follow sorted by confidence.
    A NULL score (classified before scores were stored) counts as sure; "other" itself is a bin, never
    less sure; a guess is never sure, whatever its probability: the gates already said no."""
    _shoot_with_scores(tmp_path, [
        ("a.jpg", "building", 0.9, "building", 0.9, None, None),
        ("b.jpg", "building", 0.41, "building", 0.41, None, None),
        ("c.jpg", "other", 0.3, "building", 0.3, None, None),
        ("d.jpg", "other", 0.45, "building", 0.45, None, None),
        ("e.jpg", "other", 0.6, "building", 0.6, None, None),
        ("f.jpg", "building", None, None, None, None, None),
        ("g.jpg", "other", 0.9, "road", 0.9, None, None),
    ])
    ix = Index(tmp_path)
    got = ix.search(filters=Filters(category="building"))
    assert [r["rel"] for r in got] == ["a.jpg", "f.jpg", "e.jpg", "d.jpg", "b.jpg", "c.jpg"]
    assert [r["sure"] for r in got] == [True, True, False, False, False, False]
    assert [r["confidence"] for r in got] == [0.9, 1.0, 0.6, 0.45, 0.41, 0.3]
    sure = ix.search(filters=Filters(category="building", sure_only=True))
    assert [r["rel"] for r in sure] == ["a.jpg", "f.jpg"]
    other = ix.search(filters=Filters(category="other"))
    assert [r["rel"] for r in other] == ["c.jpg", "d.jpg", "e.jpg", "g.jpg"] and all(r["sure"] for r in other)
    assert all(r["sure"] and r["confidence"] == 1.0 for r in ix.search())

def test_a_row_shows_under_every_category_it_fits(tmp_path):
    """A clip filed as interview at 0.55 with people at 0.41 is under both tiles: first among the interviews,
    and in the people grid's less sure band at its own people probability (a runner-up is never sure: the
    winner already took more of the softmax). Nothing else is listed under people because of it."""
    _shoot_with_scores(tmp_path, [
        ("int.jpg", "interview", 0.55, "interview", 0.55, None, None),
        ("ppl.jpg", "people", 0.9, "people", 0.9, None, None),
        ("bch.jpg", "beach", 0.9, "beach", 0.9, None, None),
    ])
    conn = db.connect(tmp_path)
    pid = conn.execute("SELECT id FROM photos WHERE rel='int.jpg'").fetchone()[0]
    db.replace_photo_categories(conn, [(pid, "people", 0.41)])
    ix = Index(tmp_path)
    got = ix.search(filters=Filters(category="people"))
    assert [(r["rel"], r["sure"], r["confidence"]) for r in got] == [("ppl.jpg", True, 0.9), ("int.jpg", False, 0.41)]
    assert [r["rel"] for r in ix.search(filters=Filters(category="interview"))] == ["int.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(category="beach"))] == ["bch.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(category="people", sure_only=True))] == ["ppl.jpg"]
    assert next(r for r in got if r["rel"] == "int.jpg")["also"] == {"people": 0.41}

def test_cluster_search_uses_cluster_score_for_the_divider(tmp_path):
    _shoot_with_scores(tmp_path, [
        ("a.jpg", None, None, None, None, "excavator", 0.2),
        ("b.jpg", None, None, None, None, "excavator", 1.0),
        ("c.jpg", None, None, None, None, "excavator", 0.49),
        ("d.jpg", None, None, None, None, "crane", 0.8),
    ])
    ix = Index(tmp_path)
    got = ix.search(filters=Filters(cluster="excavator"))
    assert [(r["rel"], r["sure"], r["confidence"]) for r in got] == [("b.jpg", True, 1.0), ("c.jpg", False, 0.49), ("a.jpg", False, 0.2)]
    assert [r["rel"] for r in ix.search(filters=Filters(cluster="excavator", sure_only=True))] == ["b.jpg"]

def test_aerial_filter_keeps_only_drone_rows(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2); make_image(tmp_path, "c.jpg", seed=3)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET aerial=1 WHERE rel='b.jpg'"); conn.commit()
    ix = Index(tmp_path)
    hits = ix.search(filters=Filters(aerial=True))
    assert [r["rel"] for r in hits] == ["b.jpg"] and hits[0]["aerial"]
    assert [r["rel"] for r in ix.search(filters=Filters(aerial=False))] == ["a.jpg", "c.jpg"]
    everything = ix.search()
    assert [bool(r["aerial"]) for r in everything] == [False, True, False]
    assert [r["rel"] for r in ix.search(filters=Filters(aerial=True, category="beach"))] == []

def test_hide_bad_and_hide_soft_drop_flagged_rows(tmp_path):
    from conftest import make_image
    for n in ("ok.jpg", "soft.jpg", "bad.jpg", "new.jpg"):
        make_image(tmp_path, n, seed=len(n))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    for n, f in (("ok.jpg", "ok"), ("soft.jpg", "soft"), ("bad.jpg", "bad")):
        conn.execute("UPDATE photos SET focus=? WHERE rel=?", (f, n))
    conn.commit()     # new.jpg stays NULL: not checked yet, never hidden
    ix = Index(tmp_path)
    everything = ix.search()
    assert {r["rel"]: r["focus"] for r in everything} == {"ok.jpg": "ok", "soft.jpg": "soft", "bad.jpg": "bad", "new.jpg": None}
    assert sorted(r["rel"] for r in ix.search(filters=Filters(hide_bad=True))) == ["new.jpg", "ok.jpg", "soft.jpg"]
    assert sorted(r["rel"] for r in ix.search(filters=Filters(hide_soft=True))) == ["new.jpg", "ok.jpg"]
    assert sorted(r["rel"] for r in ix.search(filters=Filters(hide_bad=True, hide_soft=True))) == ["new.jpg", "ok.jpg"]
    assert len(ix.search(filters=Filters(hide_bad=True, faces="one"))) == 0


def _row(rel, taken_at, sharp, qhash=None, camera="SONY", kind="photo"):
    return dict(rel=rel, size=100, mtime=1.0, qhash=qhash or ("h" + rel), sibling=None, width=100, height=80, taken_at=taken_at,
                camera=camera, phash="p", sharp_tile=sharp, sharp_max=sharp, sharp_eye=None, sharp=sharp, n_faces=0,
                status="ok", kind=kind, duration=None, aerial=0)

def test_duplicates_fold_into_one_tile_by_qhash(tmp_path):
    """The same file twice on the disk (same qhash) is one tile with group copies; fold keeps the first in order,
    unfolded rows still carry the group so the inspector can say "one of 2"."""
    conn = db.connect(tmp_path)
    for rel, t, s in (("day1/a.jpg", "2026-09-01T10:00:00", 5.0), ("backup/a.jpg", "2026-09-01T10:00:00", 5.0), ("day1/b.jpg", "2026-09-01T10:05:00", 9.0)):
        db.upsert_photo(conn, _row(rel, t, s, qhash="same" if rel.endswith("a.jpg") else None))
    ix = Index(tmp_path)
    plain = ix.search()
    assert [r["rel"] for r in plain] == ["backup/a.jpg", "day1/a.jpg", "day1/b.jpg"]
    assert plain[0]["group"]["kind"] == "copies" and plain[0]["group"]["n"] == 2 and plain[2]["group"] is None
    assert [m["rel"] for m in plain[0]["group"]["members"]] == ["backup/a.jpg", "day1/a.jpg"] and plain[1]["group"] == plain[0]["group"]
    folded = ix.search(filters=Filters(fold=True))
    assert [r["rel"] for r in folded] == ["backup/a.jpg", "day1/b.jpg"] and folded[0]["group"]["n"] == 2
    # a filter that drops one copy leaves the other alone: no group of one
    conn.execute("UPDATE photos SET focus='bad' WHERE rel='backup/a.jpg'"); conn.commit()
    ix = Index(tmp_path)
    only = ix.search(filters=Filters(hide_bad=True, fold=True))
    assert [r["rel"] for r in only] == ["day1/a.jpg", "day1/b.jpg"] and only[0]["group"] is None

def test_bursts_fold_by_second_frame_number_and_embedding(tmp_path):
    """Consecutive frame numbers each within a second of the last, in one folder, with embeddings that agree,
    are one burst; the sharpest is named; a gap in time, in numbering, in folder or in embedding breaks it."""
    import numpy as np
    conn = db.connect(tmp_path)
    rows = [("s/DSC00010.jpg", "2026-09-01T10:00:00", 3.0), ("s/DSC00011.jpg", "2026-09-01T10:00:00", 8.0), ("s/DSC00012.jpg", "2026-09-01T10:00:01", 5.0),
            ("s/DSC00013.jpg", "2026-09-01T10:00:01", 6.0),      # the embedding disagrees: not the same burst
            ("s/DSC00020.jpg", "2026-09-01T10:00:01", 6.0),      # numbering jumps
            ("s/DSC00021.jpg", "2026-09-01T10:00:05", 6.0),      # four seconds later
            ("t/DSC00022.jpg", "2026-09-01T10:00:05", 6.0),      # another folder
            ("s/C0001.MP4", "2026-09-01T10:00:05", 6.0)]
    ids = {}
    for rel, t, s in rows:
        ids[rel] = db.upsert_photo(conn, _row(rel, t, s, kind="video" if rel.endswith(".MP4") else "photo"))
    v = np.zeros(EMBED_DIM, np.float32); v[0] = 1.0
    w = np.zeros(EMBED_DIM, np.float32); w[1] = 1.0
    for rel in ("s/DSC00010.jpg", "s/DSC00011.jpg", "s/DSC00012.jpg"): db.set_embed(conn, ids[rel], v)
    db.set_embed(conn, ids["s/DSC00013.jpg"], w)
    conn.commit()
    ix = Index(tmp_path)
    folded = ix.search(filters=Filters(fold=True))
    assert [r["rel"] for r in folded] == ["s/DSC00010.jpg", "s/DSC00013.jpg", "s/DSC00020.jpg", "s/C0001.MP4", "s/DSC00021.jpg", "t/DSC00022.jpg"]
    g = folded[0]["group"]
    assert g["kind"] == "burst" and g["n"] == 3 and g["sharpest"] == ids["s/DSC00011.jpg"]
    assert [m["rel"] for m in g["members"]] == ["s/DSC00010.jpg", "s/DSC00011.jpg", "s/DSC00012.jpg"] and all(r["group"] is None for r in folded[1:])
    assert len(ix.search()) == 8                                                    # no fold: every frame
    # an image query keeps the best-scoring frame of the burst as its tile
    like = ix.search(image_id=ids["s/DSC00012.jpg"], filters=Filters(fold=True))
    assert like[0]["group"]["n"] == 3 and like[0]["rel"] in ("s/DSC00010.jpg", "s/DSC00011.jpg", "s/DSC00012.jpg")

# ===== sort: the order of an unsearched grid =====

def _sorted_fixture(tmp_path):
    """Three photos with sizes and times set by hand: a.jpg oldest and smallest, c.jpg newest and biggest."""
    from conftest import make_image
    for i, name in enumerate(["b.jpg", "c.jpg", "a.jpg"]):
        make_image(tmp_path, name, seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    for name, taken, size in [("a.jpg", "2026-01-01T09:00:00", 100),
                              ("b.jpg", "2026-01-02T09:00:00", 5000),
                              ("c.jpg", "2026-01-03T09:00:00", 900000)]:
        conn.execute("UPDATE photos SET taken_at=?, size=? WHERE rel=?", (taken, size, name))
    conn.commit()
    return Index(tmp_path)

def test_sort_orders_by_date_and_by_size(tmp_path):
    ix = _sorted_fixture(tmp_path)
    assert [r["rel"] for r in ix.search()] == ["a.jpg", "b.jpg", "c.jpg"]
    assert [r["rel"] for r in ix.search(sort="oldest")] == ["a.jpg", "b.jpg", "c.jpg"]
    assert [r["rel"] for r in ix.search(sort="newest")] == ["c.jpg", "b.jpg", "a.jpg"]
    assert [r["rel"] for r in ix.search(sort="biggest")] == ["c.jpg", "b.jpg", "a.jpg"]
    assert [r["rel"] for r in ix.search(sort="smallest")] == ["a.jpg", "b.jpg", "c.jpg"]
    assert [r["rel"] for r in ix.search(sort="name")] == ["a.jpg", "b.jpg", "c.jpg"]

def test_sort_paging_is_stable(tmp_path):
    ix = _sorted_fixture(tmp_path)
    first = [r["rel"] for r in ix.search(sort="newest", limit=2, offset=0)]
    rest = [r["rel"] for r in ix.search(sort="newest", limit=2, offset=2)]
    assert first + rest == ["c.jpg", "b.jpg", "a.jpg"]

def test_a_file_without_a_capture_time_sorts_on_its_mtime(tmp_path):
    """A screenshot or an export carries no EXIF time. It belongs on the day it was written, not at the end
    of the shoot behind everything that does have one."""
    import os
    from conftest import make_image
    make_image(tmp_path, "shot.jpg", seed=1); make_image(tmp_path, "screenshot.png", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET taken_at='2026-05-01T10:00:00' WHERE rel='shot.jpg'")
    conn.execute("UPDATE photos SET taken_at=NULL, mtime=? WHERE rel='screenshot.png'",
                 (__import__("datetime").datetime(2026, 4, 1, 10).timestamp(),))
    conn.commit()
    ix = Index(tmp_path)
    assert [r["rel"] for r in ix.search()] == ["screenshot.png", "shot.jpg"]
    assert [r["rel"] for r in ix.search(sort="newest")] == ["shot.jpg", "screenshot.png"]

def test_a_search_keeps_its_ranking_whatever_the_sort_says(tmp_path):
    """A query scores every photo, so ordering the answer any other way throws the query away."""
    from conftest import make_image
    make_image(tmp_path, "a.jpg", kind="sharp")
    Image.new("RGB", (900, 600), (200, 30, 30)).save(tmp_path / "red.jpg")
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    assert ix.search(text="a red wall", sort="name")[0]["rel"] == "red.jpg"

def test_days_counts_the_shoot_day_by_day(tmp_path):
    ix = _sorted_fixture(tmp_path)
    days = ix.days()
    assert [d["day"] for d in days] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert [d["n"] for d in days] == [1, 1, 1]
    assert [d["bytes"] for d in days] == [100, 5000, 900000]
    assert all(d["videos"] == 0 for d in days)

def test_query_by_vector_is_the_same_scorer_as_image_id(tmp_path):
    """An image from outside the shoot scores through Index.query(vector=...). Handed the embedding of a
    file that IS in the shoot, it has to agree with image_id on that file: one scorer, two front doors."""
    from conftest import make_image
    from photosort.decode import load_upload
    from photosort.embed import get_embedder
    for i, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
        make_image(tmp_path, name, seed=i)
    index_folder(tmp_path, faces=False, workers=1)
    ix = Index(tmp_path)
    b = next(r for r in ix.search() if r["rel"] == "b.jpg")

    vec = get_embedder().encode_images([load_upload((tmp_path / "b.jpg").read_bytes())])[0]
    assert vec.shape == (EMBED_DIM,)
    by_vector = ix.search(vector=vec)
    assert by_vector[0]["rel"] == "b.jpg" and by_vector[0]["score"] > 0.9
    # The same ranking as asking for the row itself: the re-encode of a re-decoded JPEG is not bit
    # identical to the stored float16 embedding, so the order is the assertion, not the scores.
    assert [r["rel"] for r in by_vector] == [r["rel"] for r in ix.search(image_id=b["id"])]
    # Filters still apply to a vector query: three rows, so only the sharpest is above the 99th percentile.
    assert len(ix.search(vector=vec, filters=Filters(sharp_min_pct=99))) == 1


def test_query_by_vector_of_the_wrong_width_is_refused(tmp_path):
    """A vector from another encoder cannot be scored against this index, and says so rather than
    failing inside numpy."""
    import numpy as np
    import pytest
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1)
    with pytest.raises(LookupError):
        Index(tmp_path).search(vector=np.zeros(EMBED_DIM // 2, np.float32))


def _two_camera_shoot(tmp_path):
    """Six frames: three from camera A across one minute, two from camera B inside that minute, one from B
    an hour later, and one clip with no camera recorded beside the A frames (the real shape: every
    camera-less row in a shoot is a video, because the camera is read off still EXIF)."""
    from conftest import make_image
    rows = [
        ("a1.jpg", "SONY ILCE-7RM5", "2026-04-17T09:30:00", "photo"),
        ("a2.jpg", "SONY ILCE-7RM5", "2026-04-17T09:30:20", "photo"),
        ("a3.jpg", "SONY ILCE-7RM5", "2026-04-17T09:30:40", "photo"),
        ("b1.jpg", "SONY ILME-FX3", "2026-04-17T09:30:10", "photo"),
        ("b2.jpg", "SONY ILME-FX3", "2026-04-17T09:31:00", "photo"),
        ("b3.jpg", "SONY ILME-FX3", "2026-04-17T10:30:00", "photo"),
        ("c1.jpg", None, "2026-04-17T09:30:05", "video"),
    ]
    for i, r in enumerate(rows):
        make_image(tmp_path, r[0], seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    for rel, camera, taken, kind in rows:
        conn.execute("UPDATE photos SET camera=?, taken_at=?, kind=? WHERE rel=?", (camera, taken, kind, rel))
    conn.commit()
    return Index(tmp_path)


def _ranked(ix, order):
    """The rows of ix in the given order, scored as a similarity query would score them (1.0 down)."""
    by_rel = {r["rel"]: r for r in ix.query()}
    return [dict(by_rel[rel], score=1.0 - i * 0.01) for i, rel in enumerate(order)]


def test_angles_best_per_camera_and_the_same_moment_elsewhere(tmp_path):
    """The whole point of task D: three frames off the strongest camera must not fill the answer."""
    from photosort.search import angles
    ix = _two_camera_shoot(tmp_path)
    rows = _ranked(ix, ["a1.jpg", "a2.jpg", "a3.jpg", "b1.jpg", "c1.jpg", "b2.jpg", "b3.jpg"])
    got = angles(rows)
    # One row per camera, best first, and the three A frames do not crowd B out.
    assert [(c["camera"], c["n"], c["best"]["rel"]) for c in got["cameras"]] == [
        ("SONY ILCE-7RM5", 3, "a1.jpg"), ("SONY ILME-FX3", 3, "b1.jpg")]
    # The clip is its own bucket: unknown is not a camera, and it is not hidden either.
    assert got["unknown"]["n"] == 1 and got["unknown"]["best"]["rel"] == "c1.jpg"
    m = got["moment"]
    assert m["anchor_camera"] == "SONY ILCE-7RM5" and m["taken_at"] == "2026-04-17T09:30:00"
    # b1 and b2 are inside the five minute window; b3 is an hour later; a2 and a3 are the same camera.
    assert [(r["rel"], r["dt_s"]) for r in m["others"]] == [("b1.jpg", 10.0), ("b2.jpg", 60.0)]
    assert [r["rel"] for r in m["unknown"]] == ["c1.jpg"]


def test_angles_window_is_the_only_thing_that_admits_a_row(tmp_path):
    from photosort.search import angles
    ix = _two_camera_shoot(tmp_path)
    rows = _ranked(ix, ["a1.jpg", "b1.jpg", "b2.jpg", "b3.jpg", "a2.jpg", "a3.jpg", "c1.jpg"])
    assert [r["rel"] for r in angles(rows, window_s=30)["moment"]["others"]] == ["b1.jpg"]
    assert [r["rel"] for r in angles(rows, window_s=7200)["moment"]["others"]] == ["b1.jpg", "b2.jpg", "b3.jpg"]


def test_angles_anchor_without_a_camera_names_what_it_can(tmp_path):
    """The clip ranks top. Nothing can be called a different camera from an unknown one, so anchor_camera
    is null and every named camera in the window is offered as what it is: a camera we can name."""
    from photosort.search import angles
    ix = _two_camera_shoot(tmp_path)
    rows = _ranked(ix, ["c1.jpg", "a1.jpg", "b1.jpg", "a2.jpg", "a3.jpg", "b2.jpg", "b3.jpg"])
    m = angles(rows)["moment"]
    assert m["anchor_camera"] is None
    assert [r["rel"] for r in m["others"]] == ["a1.jpg", "b1.jpg", "a2.jpg", "a3.jpg", "b2.jpg"]
    assert m["unknown"] == []


def test_angles_of_nothing_and_of_a_row_with_no_capture_time(tmp_path):
    from photosort.search import angles
    ix = _two_camera_shoot(tmp_path)
    assert angles([]) == {"cameras": [], "window_s": 300.0, "unknown": None, "moment": None}
    rows = _ranked(ix, ["a1.jpg", "b1.jpg"])
    rows[0] = dict(rows[0], taken_at=None)
    assert angles(rows)["moment"] is None          # no time, no moment; the camera list still stands
    assert [c["camera"] for c in angles(rows)["cameras"]] == ["SONY ILCE-7RM5", "SONY ILME-FX3"]

def test_shot_size_filter(tmp_path):
    """close / medium / wide / unknown each keep exactly their own rows, and a row the pass never
    scored (shot_size NULL) is in none of them: unscored is an absence, not a fifth framing."""
    from conftest import make_image
    for i, name in enumerate(("close.jpg", "wide.jpg", "unsure.jpg", "unscored.jpg")):
        make_image(tmp_path, name, seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    ids = {r["rel"]: r["id"] for r in conn.execute("SELECT id, rel FROM photos")}
    for rel, size in (("close.jpg", "close"), ("wide.jpg", "wide"), ("unsure.jpg", "unknown")):
        conn.execute("UPDATE photos SET shot_size=?, shot_size_score=0.6 WHERE id=?", (size, ids[rel]))
    conn.commit()

    ix = Index(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(shot_size="close"))] == ["close.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(shot_size="wide"))] == ["wide.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(shot_size="unknown"))] == ["unsure.jpg"]
    assert ix.search(filters=Filters(shot_size="medium")) == []
    assert len(ix.search()) == 4                        # unscored.jpg is still in the shoot
    assert all(r["shot_size"] is None for r in ix.search() if r["rel"] == "unscored.jpg")

def test_slowmo_filter_is_about_clips_and_only_about_clips(tmp_path):
    """A still has no frame rate, so it is absent from both halves rather than counted as normal
    speed, and a clip whose fps was never read is absent too: "not known to be" is not "is not"."""
    from conftest import make_image
    make_image(tmp_path, "still.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    base = dict(size=1, mtime=1.0, qhash="q", sibling=None, width=1920, height=1080, taken_at=None,
                camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None, sharp=1.0,
                n_faces=0, status="ok", kind="video", duration=4.0)
    db.upsert_photo(conn, dict(base, rel="slow.mp4", qhash="a", fps=120.0))
    db.upsert_photo(conn, dict(base, rel="normal.mp4", qhash="b", fps=25.0))
    db.upsert_photo(conn, dict(base, rel="unread.mp4", qhash="c", fps=None))
    conn.commit()

    ix = Index(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(slowmo=True))] == ["slow.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(slowmo=False))] == ["normal.mp4"]
    assert len(ix.search()) == 4
    assert db.slowmo_counts(conn) == {"slow": 1, "normal": 1, "unknown": 1}


def _clip_shoot(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "still.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("UPDATE photos SET camera='SONY ILCE-7SM3', n_faces=2 WHERE rel='still.jpg'")
    base = dict(size=1, mtime=1.0, sibling=None, width=1920, height=1080, taken_at=None, camera=None,
                phash="0" * 16, sharp_tile=1.0, sharp_max=1.0, sharp_eye=None, sharp=1.0, n_faces=0,
                status="ok", kind="video")
    for rel, extra in (("log.mp4", dict(camera="SONY ILCE-7SM3", gamma="s-log3-cine", duration=4.0, fps=25.0, people=1)),
                       ("709.mp4", dict(camera="SONY ILCE-7SM3", gamma="rec709", duration=15.0, fps=25.0, capture_fps=100.0, people=2)),
                       ("phone.mp4", dict(camera="Apple iPhone 15 Pro", gamma="", duration=200.0, fps=30.0, people=0)),
                       ("old.mp4", dict(gamma=None, duration=45.0, fps=None, people=None))):
        db.upsert_photo(conn, dict(base, rel=rel, qhash=rel, **extra))
    conn.commit()
    return Index(tmp_path)


def test_camera_filter_keeps_a_bodys_stills_and_clips(tmp_path):
    ix = _clip_shoot(tmp_path)
    assert sorted(r["rel"] for r in ix.search(filters=Filters(camera="SONY ILCE-7SM3"))) == ["709.mp4", "log.mp4", "still.jpg"]
    assert [r["rel"] for r in ix.search(filters=Filters(camera="Apple iPhone 15 Pro"))] == ["phone.mp4"]
    assert ix.search(filters=Filters(camera="Canon")) == []


def test_profile_filter_is_about_clips_and_unknown_is_neither(tmp_path):
    """Log is the S-Log family; standard is any other profile the sidecar named. A clip with no sidecar
    (gamma '') or never read (NULL) is in neither half, and a still never is."""
    ix = _clip_shoot(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(look="log"))] == ["log.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(look="standard"))] == ["709.mp4"]


def test_duration_filter_buckets_clips_only(tmp_path):
    ix = _clip_shoot(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(duration="lt10"))] == ["log.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(duration="10to30"))] == ["709.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(duration="30to120"))] == ["old.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(duration="gt120"))] == ["phone.mp4"]


def test_slowmo_takes_the_sidecars_capture_rate(tmp_path):
    ix = _clip_shoot(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(slowmo=True))] == ["709.mp4"]


def test_the_faces_filter_counts_people_in_a_clip_and_skips_clips_never_looked_at(tmp_path):
    """A clip's head count is photos.people, not n_faces (which stays 0 on a clip). Before clips had faces
    every clip passed "No people"; now a clip nobody looked at passes no faces filter at all."""
    ix = _clip_shoot(tmp_path)
    assert [r["rel"] for r in ix.search(filters=Filters(faces="none"))] == ["phone.mp4"]
    assert [r["rel"] for r in ix.search(filters=Filters(faces="one"))] == ["log.mp4"]
    assert sorted(r["rel"] for r in ix.search(filters=Filters(faces="two"))) == ["709.mp4", "still.jpg"]
    row = next(r for r in ix.search() if r["rel"] == "709.mp4")
    assert (row["people"], row["gamma"], row["capture_fps"]) == (2, "rec709", 100.0)


# ===== what each filter would leave: live counts for the facet rail (Q4) =====

def test_facet_counts_with_nothing_on_are_the_shoot(tmp_path):
    ix = _clip_shoot(tmp_path)
    f = ix.facet_counts(Filters())
    assert f["total"] == 5
    assert f["kind"] == {"photos": 1, "videos": 4}
    assert f["faces"] == {"none": 1, "one": 1, "two": 2}            # old.mp4 was never looked at: in none of them
    assert f["cameras"] == {"SONY ILCE-7SM3": 3, "Apple iPhone 15 Pro": 1}
    assert f["duration"] == {"lt10": 1, "10to30": 1, "30to120": 1, "gt120": 1}
    assert f["slowmo"] == {"slow": 1, "normal": 2}
    assert f["look"] == {"log": 1, "standard": 1}
    assert sum(f["days"].values()) == 5 and f["drone"] == 0


def test_each_facet_is_counted_under_every_filter_but_its_own(tmp_path):
    """With videos and the Sony on, the camera list still offers the iPhone with what it would show (counted
    under videos alone), the kind segments say what the Sony shot of each kind, and every other axis counts
    what is on screen. That is what lets an editor learn the shoot by narrowing it."""
    ix = _clip_shoot(tmp_path)
    f = ix.facet_counts(Filters(kind="videos", camera="SONY ILCE-7SM3"))
    assert f["total"] == 2 == len(ix.query(filters=Filters(kind="videos", camera="SONY ILCE-7SM3")))
    assert f["cameras"] == {"SONY ILCE-7SM3": 2, "Apple iPhone 15 Pro": 1}
    assert f["kind"] == {"photos": 1, "videos": 2}
    assert f["duration"] == {"lt10": 1, "10to30": 1}
    assert f["faces"] == {"one": 1, "two": 1}
    g = ix.facet_counts(Filters(faces="two"))
    assert g["total"] == 2 and g["faces"] == {"none": 1, "one": 1, "two": 2} and g["kind"] == {"photos": 1, "videos": 1}


def test_facet_counts_list_a_row_under_every_category_it_fits(tmp_path):
    _shoot_with_scores(tmp_path, [
        ("int.jpg", "interview", 0.55, "interview", 0.55, None, None),
        ("bch.jpg", "people", 1.0, "beach", 0.4, None, None),
        ("oth.jpg", "other", 0.3, "office", 0.3, None, None),
    ])
    conn = db.connect(tmp_path)
    pid = conn.execute("SELECT id FROM photos WHERE rel='int.jpg'").fetchone()[0]
    db.replace_photo_categories(conn, [(pid, "people", 0.41)])
    f = Index(tmp_path).facet_counts(Filters(category="people"))
    assert f["categories"] == {"interview": 1, "people": 2, "beach": 1, "other": 1, "office": 1}
    assert f["total"] == 2
