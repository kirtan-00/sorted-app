from photosort import db
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
    v = np.zeros(512, np.float32); v[0] = 1.0
    w = np.zeros(512, np.float32); w[1] = 1.0
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
