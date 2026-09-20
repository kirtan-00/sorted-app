import os
import numpy as np
from photosort import db
from photosort.embed import get_embedder
from photosort.classify import classify, classify_and_store, write_manifest, CATEGORIES, FALLBACK

BASE_ROW = dict(size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10, taken_at=None,
                 camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0,
                 n_faces=0, status="ok")

def _row(rel, **kw):
    r = dict(BASE_ROW); r.update(rel=rel); r.update(kw)
    return r

def test_classify_categories_from_text_embeddings(tmp_path):
    """A row whose embedding matches a category's text prompts is put in that category; a row with
    faces is always 'people' regardless of what it looks like; a row with no signal at all is 'other'."""
    conn = db.connect(tmp_path)
    beach_vec = get_embedder().encode_text(["a sandy beach"])[0]

    beach_id = db.upsert_photo(conn, _row("beach.jpg"))
    db.set_embed(conn, beach_id, beach_vec)

    people_id = db.upsert_photo(conn, _row("face.jpg", n_faces=2))
    db.set_embed(conn, people_id, beach_vec)   # looks exactly like the beach photo except for faces

    rng = np.random.default_rng(0)
    junk_vec = rng.normal(size=512).astype(np.float32)
    junk_vec /= np.linalg.norm(junk_vec)
    junk_id = db.upsert_photo(conn, _row("junk.jpg"))
    db.set_embed(conn, junk_id, junk_vec)
    conn.commit()

    results = {r["id"]: r for r in classify(tmp_path)}
    assert results[beach_id]["category"] == "beach"
    assert results[people_id]["category"] == "people"   # face rule overrides the (identical) beach embedding
    assert results[junk_id]["category"] == FALLBACK

def test_classify_and_store_persists_and_counts(tmp_path):
    conn = db.connect(tmp_path)
    beach_vec = get_embedder().encode_text(["a sandy beach"])[0]
    pid = db.upsert_photo(conn, _row("beach.jpg"))
    db.set_embed(conn, pid, beach_vec)
    conn.commit()

    counts = classify_and_store(tmp_path)
    assert counts == {"beach": 1}

    conn2 = db.connect(tmp_path)
    row = conn2.execute("SELECT category, category_score FROM photos WHERE id=?", (pid,)).fetchone()
    assert row["category"] == "beach"
    assert row["category_score"] is not None and 0.0 < row["category_score"] <= 1.0
    assert db.category_counts(conn2) == {"beach": 1}

def test_classify_and_store_persists_the_best_guess_for_an_other_photo(tmp_path):
    """A photo the gates sent to "other" still records its best real category and that category's
    probability, so the search can show it under that category as "less sure". A face-forced "people"
    photo is people with score 1.0: the detector decided, not the softmax."""
    conn = db.connect(tmp_path)
    rng = np.random.default_rng(0)
    junk = rng.normal(size=512).astype(np.float32); junk /= np.linalg.norm(junk)
    junk_id = db.upsert_photo(conn, _row("junk.jpg")); db.set_embed(conn, junk_id, junk)
    face_id = db.upsert_photo(conn, _row("face.jpg", n_faces=1)); db.set_embed(conn, face_id, junk)
    conn.commit()
    classify_and_store(tmp_path)
    conn2 = db.connect(tmp_path)
    r = conn2.execute("SELECT category, category_score, category_guess, category_guess_score FROM photos WHERE id=?", (junk_id,)).fetchone()
    assert r["category"] == FALLBACK
    assert r["category_guess"] in CATEGORIES and 0.0 < r["category_guess_score"] <= 1.0
    f = conn2.execute("SELECT category, category_score, category_guess, category_guess_score FROM photos WHERE id=?", (face_id,)).fetchone()
    # people by the face rule; the guess keeps the scene it was shot in (or people when there is none)
    assert (f["category"], f["category_score"]) == ("people", 1.0)
    assert f["category_guess"] in CATEGORIES and 0.0 < f["category_guess_score"] <= 1.0

def test_categories_are_the_documentary_set_in_order():
    """The fixed list, in tile and export order (then "other"). Five of these came out of the first video
    shoot: interview, night, food and sky were what "other" was hiding, road grew a car interior. The first
    documentary shoot added boat (fishermen on deck had no home) and office (29 empty-office B-roll clips
    were "interview" because the old third prompt described the set, not the act; it is gone)."""
    assert list(CATEGORIES) == ["ocean", "boat", "beach", "people", "interview", "building", "office", "road",
                                "night", "food", "sky", "birds-animals"]
    assert all(prompts and all(isinstance(t, str) and t for t in prompts) for prompts in CATEGORIES.values())
    assert CATEGORIES["boat"] == ["fishermen on a fishing boat", "a boat deck with ropes, flags and masts", "boats moored in a harbour"]
    assert CATEGORIES["interview"] == ["two people sitting on chairs in a room having an interview",
                                       "a person sitting in a chair in a studio talking to the camera",
                                       "a person seated in a chair being interviewed, framed pictures and a lamp behind them"]
    assert CATEGORIES["office"] == ["an empty office interior", "a meeting room with a long table and chairs",
                                    "a desk with a lamp, plants and stationery", "framed pictures on an office wall",
                                    "a company logo on a wall", "a sofa in a waiting room"]
    assert CATEGORIES["building"][-1] == "a village with huts and small houses" and len(CATEGORIES["building"]) == 7
    assert not any("formal interview setup" in t for ts in CATEGORIES.values() for t in ts)
    assert CATEGORIES["ocean"][0] == "the open sea with waves" and CATEGORIES["road"][-1] == "the inside of a car with a person driving"

def test_negative_prompts_describe_content_never_image_quality():
    """"a blurry or badly lit photograph" matched cinematic shallow-focus and flat log footage and became a
    sink (35 of the 49 "other" items on DAY-4). Negatives name content that is off the list, nothing about
    the picture's quality."""
    from photosort.classify import NEGATIVE_PROMPTS
    assert NEGATIVE_PROMPTS
    for p in NEGATIVE_PROMPTS:
        low = p.lower()
        assert not any(w in low for w in ("blur", "focus", "lit", "lighting", "dark", "noisy", "grainy")), p

def test_food_is_a_category_not_the_other_bin(tmp_path):
    """The calibration food image used to be expected as "other" because food was a negative prompt; food
    is a real category now, so a food-shaped embedding is filed under it, sure."""
    conn = db.connect(tmp_path)
    food_vec = get_embedder().encode_text(["a plate of food on a table"])[0]
    pid = db.upsert_photo(conn, _row("thali.jpg")); db.set_embed(conn, pid, food_vec)
    conn.commit()
    res = {r["id"]: r for r in classify(tmp_path)}
    assert res[pid]["category"] == "food" and res[pid]["score"] >= 0.5

def test_category_counts_reports_unclassified(tmp_path):
    conn = db.connect(tmp_path)
    db.upsert_photo(conn, _row("never_classified.jpg"))
    assert db.category_counts(conn) == {"unclassified": 1}

def test_write_manifest_symlinks_not_copies_and_nothing_under_root(tmp_path):
    """A shoot with per-day/per-location subfolders can repeat a filename across folders, so
    write_manifest names each symlink after the full relative path ('/' -> '__'), and it must
    never touch the (read-only) shoot root: only the export dir gets new files."""
    conn = db.connect(tmp_path)
    beach_vec = get_embedder().encode_text(["a sandy beach"])[0]
    pid = db.upsert_photo(conn, _row("day2/beach/IMG_0001.jpg", sibling="day2/beach/IMG_0001.raw"))
    db.set_embed(conn, pid, beach_vec)
    conn.commit()

    results = classify(tmp_path)
    base = write_manifest(tmp_path, results)

    link = base / "beach" / "day2__beach__IMG_0001.jpg"
    raw_link = base / "beach" / "day2__beach__IMG_0001.raw"
    assert link.is_symlink() and os.path.realpath(link) == str((tmp_path / "day2/beach/IMG_0001.jpg").resolve())
    assert raw_link.is_symlink()
    assert not link.is_file()   # dangling: the source file was never actually created on disk

    assert not base.resolve().is_relative_to(tmp_path.resolve())   # export dir lives outside the shoot
    assert list(tmp_path.rglob("*")) == []   # nothing was ever written under the (read-only) shoot root

def test_classify_and_store_labels_segments_in_the_same_pass(tmp_path):
    """A video row is categorised like a photo (whole-clip embedding); each of its segments gets its own
    category from its own embedding. Counts stay per photo/video row so the tab agrees with /api/categories."""
    conn = db.connect(tmp_path)
    E = get_embedder()
    beach_vec = E.encode_text(["a sandy beach"])[0]
    road_vec = E.encode_text(["a road with vehicles"])[0]
    vid = db.upsert_photo(conn, _row("clip.mp4", kind="video", duration=4.0))
    db.set_embed(conn, vid, beach_vec)
    db.replace_segments(conn, vid, [dict(idx=0, start=0.0, end=2.0, frame="h_0.jpg"), dict(idx=1, start=2.0, end=4.0, frame="h_1.jpg")])
    segs = conn.execute("SELECT id FROM segments WHERE photo_id=? ORDER BY idx", (vid,)).fetchall()
    db.set_segment_embed(conn, segs[0]["id"], beach_vec)
    db.set_segment_embed(conn, segs[1]["id"], road_vec)
    conn.commit()
    counts = classify_and_store(tmp_path)
    assert counts == {"beach": 1}
    conn2 = db.connect(tmp_path)
    assert conn2.execute("SELECT category FROM photos WHERE id=?", (vid,)).fetchone()[0] == "beach"
    rows = conn2.execute("SELECT category, category_score FROM segments WHERE photo_id=? ORDER BY idx", (vid,)).fetchall()
    assert [r["category"] for r in rows] == ["beach", "road"]
    assert all(r["category_score"] is not None and 0.0 < r["category_score"] <= 1.0 for r in rows)


def test_margin_gate_ignores_the_winners_own_family():
    """A sub-category does not compete with its parent for the margin gate: a row split evenly between
    people and interview (seated interviews on the first documentary shoot, 17 of them filed as "other")
    is measured against the best category OUTSIDE that family, so it is filed; the same even split between
    beach and building is still ambiguous and still "other". guess and its score are untouched."""
    from photosort.classify import _score, CATEGORY_FAMILY, FALLBACK, MIN_PROB_MARGIN
    assert CATEGORY_FAMILY == {"interview": "people"}
    names = ["beach", "building", "people", "interview", "__other__"]
    T = np.eye(5, 512, dtype=np.float32)                 # one prompt per category, orthogonal
    owner = np.arange(5)
    def row(*idx):
        v = np.zeros(512, np.float32); v[list(idx)] = 1.0; return v / np.linalg.norm(v)
    out = _score(np.stack([row(2, 3), row(0, 1), row(3)]), T, owner, names)
    cat, score, margin, guess, guess_score, probs = out[0]
    assert cat in ("people", "interview") and guess == cat
    assert abs(score - 0.5) < 1e-3 and abs(guess_score - 0.5) < 1e-3
    assert margin >= MIN_PROB_MARGIN and abs(margin - 0.5) < 1e-3          # against beach, not against the sibling
    cat2, score2, margin2, guess2, _, _ = out[1]
    assert cat2 == FALLBACK and guess2 in ("beach", "building") and abs(margin2) < 1e-3
    assert out[2][0] == "interview" and out[2][2] > 0.99                   # a clear winner is unchanged

def test_long_talking_clips_are_interviews(tmp_path):
    """A ten-minute take with a person talking is an interview whatever the framing (a long "people" clip,
    or one whose best real guess is people or interview); a 30 s clip of the same thing stays people, and a
    long beach walk is untouched. Score is the larger of the two probabilities, guess is interview."""
    from photosort.classify import INTERVIEW_MIN_DURATION_S, CATEGORIES
    from photosort.config import INTERVIEW_MIN_DURATION_S as cfg
    assert INTERVIEW_MIN_DURATION_S == cfg == 600.0
    conn = db.connect(tmp_path)
    E = get_embedder()
    people_vec = E.encode_text(CATEGORIES["people"]).mean(axis=0); people_vec /= np.linalg.norm(people_vec)
    beach_vec = E.encode_text(["a sandy beach"])[0]
    long_p = db.upsert_photo(conn, _row("long.mp4", kind="video", duration=700.0)); db.set_embed(conn, long_p, people_vec)
    short_p = db.upsert_photo(conn, _row("short.mp4", kind="video", duration=30.0)); db.set_embed(conn, short_p, people_vec)
    long_b = db.upsert_photo(conn, _row("walk.mp4", kind="video", duration=700.0)); db.set_embed(conn, long_b, beach_vec)
    still = db.upsert_photo(conn, _row("still.jpg")); db.set_embed(conn, still, people_vec)
    conn.commit()
    res = {r["id"]: r for r in classify(tmp_path)}
    assert res[short_p]["category"] == "people" and res[still]["category"] == "people"
    assert res[long_p]["category"] == "interview" and res[long_p]["guess"] == "interview"
    assert res[long_p]["score"] >= res[short_p]["score"] and 0.0 < res[long_p]["score"] <= 1.0
    assert res[long_b]["category"] == "beach" and res[long_b]["guess"] == "beach"
    counts = classify_and_store(tmp_path)
    assert counts == {"interview": 1, "people": 2, "beach": 1}
    row = db.connect(tmp_path).execute("SELECT category, category_guess FROM photos WHERE id=?", (long_p,)).fetchone()
    assert tuple(row) == ("interview", "interview")

# Drone shots from cameras that leave no DJI trace: a zero-shot aerial/ground pair, run after the categories

def test_aerial_gap_from_the_prompt_pair():
    """The gate is a raw cosine gap, not a softmax: at TEMPERATURE 100 a two-way softmax at 0.7 needs a gap of
    only 0.0085, which flagged 180 of 3,677 ground-only Sony photos. An embedding on the aerial prompts'
    centroid clears AERIAL_MIN_GAP, one on the ground centroid does not, and one halfway between an aerial
    prompt and a ground prompt fails on the gap as well (halfway between the two CENTROIDS would not: the
    ground prompts are spread wider than the aerial ones, so under a max-per-side rule that point sits 0.067
    on the aerial side). The floor on the best aerial cosine mirrors MIN_COSINE: a junk vector never passes."""
    from photosort.classify import (AERIAL_PROMPTS, GROUND_PROMPTS, AERIAL_MIN_GAP, MIN_COSINE, aerial_gap,
                                    CATEGORIES, NEGATIVE_PROMPTS)
    E = get_embedder()
    A = E.encode_text(AERIAL_PROMPTS); G = E.encode_text(GROUND_PROMPTS)
    a = A.mean(axis=0); a /= np.linalg.norm(a)
    g = G.mean(axis=0); g /= np.linalg.norm(g)
    mid = A[0] + G[0]; mid /= np.linalg.norm(mid)
    rng = np.random.default_rng(0)
    junk = rng.normal(size=512).astype(np.float32); junk /= np.linalg.norm(junk)
    gap, best = aerial_gap(np.stack([a, g, mid, junk]), E)
    assert gap.shape == (4,) and best.shape == (4,)
    assert AERIAL_MIN_GAP == 0.05
    assert gap[0] >= AERIAL_MIN_GAP and best[0] >= MIN_COSINE
    assert gap[1] < AERIAL_MIN_GAP
    assert gap[2] < AERIAL_MIN_GAP
    assert best[3] < MIN_COSINE
    assert not any("drone" in t for t in AERIAL_PROMPTS)
    assert not any(t in prompts for prompts in CATEGORIES.values() for t in AERIAL_PROMPTS)
    assert not any(t in NEGATIVE_PROMPTS for t in AERIAL_PROMPTS)
    assert not hasattr(__import__("photosort.classify", fromlist=["x"]), "AERIAL_MIN_PROB")
    assert not hasattr(__import__("photosort.classify", fromlist=["x"]), "aerial_probs")

def test_classify_and_store_flags_aerial_rows_without_touching_metadata_ones(tmp_path):
    """Rows with aerial=0 get the zero-shot verdict persisted; a row already 1 from the index (DJI metadata)
    is never re-decided, and a drone shot of a beach is still filed under beach."""
    from photosort.classify import AERIAL_PROMPTS
    conn = db.connect(tmp_path)
    E = get_embedder()
    aerial_vec = E.encode_text(AERIAL_PROMPTS).mean(axis=0); aerial_vec /= np.linalg.norm(aerial_vec)
    beach_vec = E.encode_text(["a sandy beach"])[0]
    top = db.upsert_photo(conn, _row("top.jpg")); db.set_embed(conn, top, aerial_vec)
    beach = db.upsert_photo(conn, _row("beach.jpg")); db.set_embed(conn, beach, beach_vec)
    dji = db.upsert_photo(conn, _row("DJI_0001.MP4", kind="video", aerial=1)); db.set_embed(conn, dji, beach_vec)
    conn.commit()
    classify_and_store(tmp_path)
    conn2 = db.connect(tmp_path)
    got = {r[0]: (r[1], r[2]) for r in conn2.execute("SELECT rel, aerial, category FROM photos")}
    assert got["top.jpg"][0] == 1
    assert got["beach.jpg"] == (0, "beach")
    assert got["DJI_0001.MP4"] == (1, "beach")
    assert db.aerial_count(conn2) == 2

def test_classify_and_store_backfills_aerial_from_a_dji_filename_without_the_disk(tmp_path):
    """Rows indexed before the aerial column existed (unchanged files are never re-probed) get aerial=1 from
    their basename alone, no embedding and no disk needed: the same DJI_ rule the index applies. A folder
    named DJI_... does not flag the files inside it (a filename rule, not a folder rule)."""
    conn = db.connect(tmp_path)
    conn.execute("INSERT INTO photos(rel, status, aerial, kind) VALUES ('x/DJI_0001.MP4', 'ok', 0, 'video')")
    conn.execute("INSERT INTO photos(rel, status, aerial, kind) VALUES ('DJI_air3s/IMG_1.JPG', 'ok', 0, 'photo')")
    conn.execute("INSERT INTO photos(rel, status, aerial, kind) VALUES ('dji_0002.jpg', 'ok', 0, 'photo')")
    conn.commit()
    assert classify_and_store(tmp_path) == {}                          # nothing embedded, nothing categorised
    got = {r[0]: r[1] for r in db.connect(tmp_path).execute("SELECT rel, aerial FROM photos")}
    assert got == {"x/DJI_0001.MP4": 1, "DJI_air3s/IMG_1.JPG": 0, "dji_0002.jpg": 1}
    assert list(tmp_path.iterdir()) == []                              # the (absent) source was never touched

# Discovered categories: k-means over the shoot, named from a fixed vocabulary

def test_vocab_is_large_lowercase_and_unique():
    from photosort.vocab import VOCAB
    assert 250 <= len(VOCAB) <= 400
    assert len(set(VOCAB)) == len(VOCAB)
    assert all(v == v.strip().lower() and v for v in VOCAB)
    for must in ("havan fire", "excavator", "priest", "garland", "scaffolding", "drone aerial view", "wedding couple",
                 "tea cup", "rangoli", "safety helmet", "sunset", "palm tree", "night street", "whiteboard"):
        assert must in VOCAB

def _unit(rng, n=1):
    v = rng.normal(size=(n, 512)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)

def _clustered_shoot(tmp_path, sizes=(20, 20, 20), noise=0.05, seed=0):
    """sizes[i] photos around centre i (well separated random unit vectors). Returns (centres, [[ids of cluster i]])."""
    rng = np.random.default_rng(seed)
    centres = _unit(rng, len(sizes))
    conn = db.connect(tmp_path); groups = []
    n = 0
    for i, size in enumerate(sizes):
        ids = []
        for _ in range(size):
            v = centres[i] + rng.normal(scale=noise, size=512).astype(np.float32); v /= np.linalg.norm(v)
            pid = db.upsert_photo(conn, _row(f"c{i}_{n:03d}.jpg")); db.set_embed(conn, pid, v); ids.append(pid); n += 1
        groups.append(ids)
    conn.commit()
    return centres, groups

def _fake_vocab(centres, labels):
    """A stand-in for the CLIP text matrix: one row per label, the first len(centres) rows are the
    cluster centres themselves so cluster i is named labels[i] without loading the model."""
    rng = np.random.default_rng(99)
    T = np.vstack([centres, _unit(rng, len(labels) - len(centres))]).astype(np.float32)
    return lambda embedder: (list(labels), T)

def test_discover_default_k_grows_with_the_shoot():
    """sqrt(n / 6), clamped to 4..DISCOVER_MAX_K. On DAY-4 (270 items) k=4 gave four coarse clusters and k=8
    split out the vendor, the scooter, the shore and the panel discussion; DISCOVER_MIN_SIZE folding keeps
    the tiny ones away at the finer k."""
    from photosort.classify import discover_k
    assert discover_k(270) == 7
    assert discover_k(3677) == 24
    assert discover_k(30) == 4
    assert discover_k(16) == 4
    assert discover_k(100000) == 24

def test_discover_finds_the_clusters_names_them_and_is_deterministic(tmp_path, monkeypatch):
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path)
    monkeypatch.setattr(cm, "_vocab_matrix", _fake_vocab(centres, ["excavator", "havan fire", "beach", "dog", "car", "sunset"]))
    out = cm.discover(tmp_path, k=3)
    assert len(out) == 3
    assert sorted(c["name"] for c in out) == ["beach", "excavator", "havan fire"]
    assert [sorted(c["photo_ids"]) for c in sorted(out, key=lambda c: c["name"])] == [sorted(groups[2]), sorted(groups[0]), sorted(groups[1])]
    assert all(c["size"] == 20 and 0.0 < c["score"] <= 1.0 and isinstance(c["id"], int) for c in out)
    assert all(len(c["photo_scores"]) == c["size"] and min(c["photo_scores"]) == 0.0 and max(c["photo_scores"]) == 1.0 for c in out)
    again = cm.discover(tmp_path, k=3)
    assert [(c["name"], c["photo_ids"], c["photo_scores"]) for c in again] == [(c["name"], c["photo_ids"], c["photo_scores"]) for c in out]

def test_discover_names_by_contrast_with_the_shoot_mean(tmp_path, monkeypatch):
    """The name is the label that makes a cluster different from the rest of the shoot, not the label that
    fits every photo of it. A label sitting on the shoot mean ("man in a kurta" on a shoot that is all one
    man) scores highest on plain cosine for every cluster; after subtracting DISCOVER_CONTRAST times its
    cosine to the shoot mean, each cluster takes its own label. The stored score stays the plain cosine.
    Names are still distinct and the run is deterministic."""
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(20, 20, 20))
    conn = db.connect(tmp_path); ids, M = db.load_embeds(conn)
    g = M.mean(axis=0); g /= np.linalg.norm(g)
    rng = np.random.default_rng(7)
    # "shoot" is the mean itself, so it beats every cluster's own label on plain cosine by construction
    own = np.stack([c + 0.6 * g for c in centres]); own /= np.linalg.norm(own, axis=1, keepdims=True)
    T = np.vstack([g, own, _unit(rng, 2)]).astype(np.float32)
    labels = ["shoot", "a", "b", "c", "x", "y"]
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (labels, T))
    out = cm.discover(tmp_path, k=3)
    assert cm.DISCOVER_CONTRAST == 0.5
    names = [c["name"] for c in out]
    assert sorted(names) == ["a", "b", "c"], names                     # not "shoot" for the biggest cluster
    for c in out:
        cent = M[[list(ids).index(p) for p in c["photo_ids"]]].mean(axis=0); cent /= np.linalg.norm(cent)
        assert abs(c["score"] - float(T[labels.index(c["name"])] @ cent)) < 1e-3   # plain cosine, not the contrast
    assert [c["name"] for c in cm.discover(tmp_path, k=3)] == names

def test_discover_same_top_label_does_not_cascade_to_the_next_one(tmp_path, monkeypatch):
    """Two clusters whose best vocabulary label is the same word: the later (smaller) one is an unnamed
    group, never the next-best label. (Changed with the naming gates: the cascade to "its next-best unused
    label" is what named three more interview clusters "doctor", "patient in a hospital" and "businessman
    in a suit" on the first documentary.) Names stay distinct."""
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(24, 16, 20), noise=0.02)
    rng = np.random.default_rng(5)
    # "crane" sits between the first two centres (both clusters score it best); "dog" is the third cluster
    between = centres[0] + centres[1]; between /= np.linalg.norm(between)
    T = np.vstack([between, centres[2], _unit(rng, 2)]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["crane", "dog", "excavator", "cat"], T))
    out = cm.discover(tmp_path, k=3)
    assert [(c["name"], c["named"]) for c in out] == [("crane", True), ("dog", True), ("group 1", False)]
    assert sorted(out[0]["photo_ids"]) == sorted(groups[0]) and sorted(out[2]["photo_ids"]) == sorted(groups[1])

def test_discover_folds_small_clusters_into_the_nearest_neighbour(tmp_path, monkeypatch):
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(30, 25, 3))
    monkeypatch.setattr(cm, "_vocab_matrix", _fake_vocab(centres, ["a", "b", "c", "d"]))
    out = cm.discover(tmp_path, k=3)
    assert len(out) == 2 and [c["size"] for c in out] in ([33, 25], [30, 28])
    assert sum(c["size"] for c in out) == 58
    assert set(groups[2]) <= set(out[0]["photo_ids"]) | set(out[1]["photo_ids"])

def test_discover_and_store_writes_cluster_and_counts(tmp_path, monkeypatch):
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(20, 12))
    monkeypatch.setattr(cm, "_vocab_matrix", _fake_vocab(centres, ["excavator", "havan fire", "dog"]))
    counts = cm.discover_and_store(tmp_path, k=2)
    assert counts == {"excavator": 20, "havan fire": 12}
    conn = db.connect(tmp_path)
    assert db.cluster_counts(conn) == {"excavator": 20, "havan fire": 12}
    rows = conn.execute("SELECT cluster, cluster_score FROM photos WHERE id IN (%s)" % ",".join(map(str, groups[1]))).fetchall()
    assert all(r["cluster"] == "havan fire" and 0.0 <= r["cluster_score"] <= 1.0 for r in rows)

def test_discover_needs_sixteen_embedded_photos(tmp_path, monkeypatch):
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(8, 7))
    def boom(embedder):
        raise AssertionError("text scoring must not run on a shoot this small")
    monkeypatch.setattr(cm, "_vocab_matrix", boom)
    assert cm.discover(tmp_path) == []
    assert cm.discover_and_store(tmp_path) == {}
    conn = db.connect(tmp_path)
    assert db.cluster_counts(conn) == {}
    assert conn.execute("SELECT count(*) FROM photos WHERE cluster IS NOT NULL").fetchone()[0] == 0

def test_discover_with_the_real_embedder_names_from_the_vocabulary(tmp_path):
    """Three clusters built from CLIP text embeddings of vocabulary words get three distinct vocabulary
    names (the exact words are not asserted: a text embedding is only a proxy for a photo)."""
    from photosort import classify as cm
    from photosort.vocab import VOCAB
    E = get_embedder()
    centres = E.encode_text(["excavator", "havan fire", "a sandy beach"])
    rng = np.random.default_rng(3); conn = db.connect(tmp_path); groups = []
    for i in range(3):
        ids = []
        for j in range(12):
            v = centres[i] + rng.normal(scale=0.02, size=512).astype(np.float32); v /= np.linalg.norm(v)
            pid = db.upsert_photo(conn, _row(f"r{i}_{j}.jpg")); db.set_embed(conn, pid, v); ids.append(pid)
        groups.append(ids)
    conn.commit()
    out = cm.discover(tmp_path, k=3)
    names = [c["name"] for c in out]
    assert len(out) == 3 and len(set(names)) == 3 and all(n in VOCAB for n in names)
    assert sorted(sorted(c["photo_ids"]) for c in out) == sorted(sorted(g) for g in groups)

# Naming gates: a discovered category must say something true or say it does not know. Measured on the
# first corporate documentary (NSG_26 Part 2: 13 clusters, 9 wrongly named) and two regression shoots.

def _project_off(v, c):
    """v with its component along unit vector c removed, renormalised."""
    v = v - (v @ c) * c
    return v / np.linalg.norm(v)

def _label_at(c, cos, rng):
    """A unit vector whose cosine to unit vector c is exactly cos."""
    r = _project_off(_unit(rng)[0], c)
    return (cos * c + (1 - cos ** 2) ** 0.5 * r).astype(np.float32)

def _shoot_of(tmp_path, groups, noise=0.03, seed=0):
    """groups: list of (centre unit vector, size). Returns the ids per group."""
    rng = np.random.default_rng(seed); conn = db.connect(tmp_path); out = []; n = 0
    for gi, (c, size) in enumerate(groups):
        ids = []
        for _ in range(size):
            v = c + rng.normal(scale=noise, size=512).astype(np.float32); v /= np.linalg.norm(v)
            pid = db.upsert_photo(conn, _row(f"g{gi}_{n:03d}.jpg")); db.set_embed(conn, pid, v); ids.append(pid); n += 1
        out.append(ids)
    conn.commit()
    return out

def test_vocab_people_and_loaded_sets_are_vocabulary_words():
    from photosort.vocab import VOCAB, VOCAB_PEOPLE, VOCAB_LOADED
    assert VOCAB_PEOPLE <= set(VOCAB) and VOCAB_LOADED <= set(VOCAB)
    for must in ("doctor", "bride", "vendor", "cricket players", "bhoomi pujan", "holi colours", "seated interview", "man in a kurta"):
        assert must in VOCAB_PEOPLE
    for must in ("slum", "screenshot", "patient in a hospital", "monsoon flood"):
        assert must in VOCAB_LOADED
    assert {"beach", "meeting room", "fishing boats", "small painted house"}.isdisjoint(VOCAB_PEOPLE)

def test_discover_is_unnamed_below_the_cosine_floor(tmp_path, monkeypatch):
    """One cluster, one label everybody votes for: named when the centroid's plain cosine to it clears
    DISCOVER_NAME_MIN_COS, "group 1" with named False just below it."""
    from photosort import classify as cm
    rng = np.random.default_rng(1)
    c = _unit(rng)[0]
    _shoot_of(tmp_path, [(c, 20)])
    for cos, named in ((cm.DISCOVER_NAME_MIN_COS - 0.03, False), (cm.DISCOVER_NAME_MIN_COS + 0.06, True)):
        T = np.stack([_label_at(c, cos, rng), _label_at(c, -0.2, rng)])
        monkeypatch.setattr(cm, "_vocab_matrix", lambda e, T=T: (["thing", "other thing"], T))
        out = cm.discover(tmp_path, k=1)
        assert len(out) == 1 and out[0]["named"] is named
        assert out[0]["name"] == ("thing" if named else "group 1")
        assert (out[0]["score"] > 0) is named

def test_discover_names_by_member_vote_not_by_centroid(tmp_path, monkeypatch):
    """A cluster of two kinds of photo whose centroid points at a third label no member picks (the
    "screenshot" cluster: 37 unrelated B-roll clips whose average matched "a photo of screenshot" best)
    is unnamed: the label must win the member vote, not just fit the average."""
    from photosort import classify as cm
    rng = np.random.default_rng(2)
    u, v = _unit(rng, 2)
    _shoot_of(tmp_path, [(u, 10), (v, 10)])
    w = u + v; w /= np.linalg.norm(w)
    T = np.stack([w, u, v]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["average", "left", "right"], T))
    out = cm.discover(tmp_path, k=1)
    assert len(out) == 1 and out[0]["named"] is False and out[0]["name"] == "group 1" and out[0]["size"] == 20

def test_discover_needs_a_vote_share(tmp_path, monkeypatch):
    """The winning label must be the top pick of at least DISCOVER_VOTE_SHARE of the members. Members
    spread over five labels, the centroid's label holding 20% of them: unnamed; holding 40%: named."""
    from photosort import classify as cm
    rng = np.random.default_rng(3)
    c = _unit(rng)[0]
    spokes = [_label_at(c, 0.6, rng) for _ in range(4)]
    for share, named in ((0.20, False), (0.40, True)):
        conn = db.connect(tmp_path); conn.execute("DELETE FROM photos"); conn.commit()
        n_c = round(40 * share); n_s = (40 - n_c) // 4
        _shoot_of(tmp_path, [(c, n_c)] + [(s, n_s) for s in spokes], noise=0.01)
        T = np.stack([c] + spokes).astype(np.float32)
        monkeypatch.setattr(cm, "_vocab_matrix", lambda e, T=T: (["hub", "a", "b", "c", "d"], T))
        out = cm.discover(tmp_path, k=1)
        assert len(out) == 1 and out[0]["named"] is named, (share, out[0])
        assert out[0]["name"] == ("hub" if named else "group 1")

def test_discover_pick_must_be_the_outright_vote_winner(tmp_path, monkeypatch):
    """The margin rule. A plain-cosine margin cannot work: on the first documentary "beach" (0.227) and
    "fishing boats" (0.203) were not even the plain top label of their own clusters ("man in a kurta"
    0.303, "labourer" 0.245) yet both were right. Instead the centroid's pick must be the outright winner
    of the member vote (ties by summed cosine): 60% of members near one label and 40% near another whose
    mix the centroid resembles more is unnamed."""
    from photosort import classify as cm
    rng = np.random.default_rng(4)
    u, v = _unit(rng, 2)
    _shoot_of(tmp_path, [(u, 12), (v, 8)], noise=0.01)
    lean = 0.55 * u + v; lean /= np.linalg.norm(lean)        # nearer the centroid than u, but only v-members pick it
    T = np.stack([lean, u]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["lean", "up"], T))
    out = cm.discover(tmp_path, k=1)
    assert out[0]["named"] is False and out[0]["name"] == "group 1"

def test_discover_people_labels_must_win_the_plain_vote_scene_labels_the_contrast_vote(tmp_path, monkeypatch):
    """Same geometry, two label words. A smaller cluster that is mostly the shoot's theme (plain vote:
    the big cluster's label) but leans toward a second label that the contrast vote picks: if that second
    label is a people or occasion word (VOCAB_PEOPLE, "vendor": the puja table read as a stall) it is a
    lie and the cluster is unnamed; if it is a scene word ("beach": men in kurtas walking on sand) the
    contrast vote is exactly what sets the cluster apart and it is named."""
    from photosort import classify as cm
    rng = np.random.default_rng(5)
    theme, side = _unit(rng, 2)
    lean = theme + 0.8 * side; lean /= np.linalg.norm(lean)
    _shoot_of(tmp_path, [(theme, 30), (lean, 16)], noise=0.01)
    T = np.stack([theme, side]).astype(np.float32)
    for word, named in (("vendor", False), ("beach", True)):
        monkeypatch.setattr(cm, "_vocab_matrix", lambda e, w=word: (["bhoomi pujan", w], T))
        out = cm.discover(tmp_path, k=2)
        assert out[0]["name"] == "bhoomi pujan" and out[0]["named"] is True and out[0]["size"] == 30
        assert out[1]["named"] is named and out[1]["name"] == (word if named else "group 1"), (word, out[1])

def test_discover_loaded_labels_need_a_decisive_vote(tmp_path, monkeypatch):
    """A label that asserts a place or condition a plain label would also cover (VOCAB_LOADED: "slum" on
    painted village huts) needs DISCOVER_LOADED_SHARE of the vote, not DISCOVER_VOTE_SHARE."""
    from photosort import classify as cm
    rng = np.random.default_rng(6)
    c = _unit(rng)[0]
    spokes = [_label_at(c, 0.6, rng) for _ in range(2)]
    for share, named in ((0.40, False), (0.80, True)):
        conn = db.connect(tmp_path); conn.execute("DELETE FROM photos"); conn.commit()
        n_c = round(40 * share); n_s = (40 - n_c) // 2
        _shoot_of(tmp_path, [(c, n_c)] + [(s, n_s) for s in spokes], noise=0.01)
        T = np.stack([c] + spokes).astype(np.float32)
        monkeypatch.setattr(cm, "_vocab_matrix", lambda e, T=T: (["slum", "a", "b"], T))
        out = cm.discover(tmp_path, k=1)
        assert out[0]["named"] is named and out[0]["name"] == ("slum" if named else "group 1"), (share, out[0])
        assert cm.DISCOVER_VOTE_SHARE < 0.40 < cm.DISCOVER_LOADED_SHARE < 0.80

def test_discover_unnamed_groups_are_numbered_by_size_and_deterministic(tmp_path, monkeypatch):
    """Two clusters whose best label is the same word: the smaller is "group 1", not the next-best label
    (that cascade produced "doctor" and "patient in a hospital" for two more interview clusters). Unnamed
    groups are numbered by size and the run is deterministic."""
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(24, 16, 12))
    # every cluster's own centre is listed under the one word "same": all three pick it, only the biggest keeps it
    T = np.stack([centres[0], centres[1], centres[2], _unit(np.random.default_rng(9))[0]]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["same", "same", "same", "x"], T))
    out = cm.discover(tmp_path, k=3)
    assert [(c["name"], c["named"], c["size"]) for c in out] == [("same", True, 24), ("group 1", False, 16), ("group 2", False, 12)]
    assert all(c["score"] == 0.0 for c in out[1:]) and out[0]["score"] > 0
    again = cm.discover(tmp_path, k=3)
    assert [(c["name"], c["photo_ids"]) for c in again] == [(c["name"], c["photo_ids"]) for c in out]
    assert cm.is_unnamed_group("group 7") and not cm.is_unnamed_group("group") and not cm.is_unnamed_group("meeting room")

def test_discover_and_store_keeps_unnamed_groups_apart(tmp_path, monkeypatch):
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(20, 12))
    T = np.stack([centres[0], centres[1]]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["excavator", "excavator"], T))
    assert cm.discover_and_store(tmp_path, k=2) == {"excavator": 20, "group 1": 12}
    assert db.cluster_counts(db.connect(tmp_path)) == {"excavator": 20, "group 1": 12}

def test_rename_cluster_moves_every_row_and_rejects_bad_names(tmp_path, monkeypatch):
    """The user's correction for what stays unnamed or wrong: every row of the old name takes the new
    one (scores untouched), the count is returned; empty, "." and ".." names, a name another discovered
    category already carries, and the reserved "group N" form are ValueErrors; an unknown old name
    moves nothing."""
    import pytest
    from photosort import classify as cm
    centres, groups = _clustered_shoot(tmp_path, sizes=(20, 12))
    T = np.stack([centres[0], centres[1]]).astype(np.float32)
    monkeypatch.setattr(cm, "_vocab_matrix", lambda e: (["excavator", "excavator"], T))
    cm.discover_and_store(tmp_path, k=2)
    conn = db.connect(tmp_path)
    before = {r["id"]: r["cluster_score"] for r in conn.execute("SELECT id, cluster_score FROM photos WHERE cluster='group 1'")}
    assert cm.rename_cluster(tmp_path, "group 1", " site huts ") == 12
    assert db.cluster_counts(conn) == {"excavator": 20, "site huts": 12}
    assert {r["id"]: r["cluster_score"] for r in conn.execute("SELECT id, cluster_score FROM photos WHERE cluster='site huts'")} == before
    assert cm.rename_cluster(tmp_path, "group 1", "anything") == 0
    for bad in ("", "   ", ".", "..", "excavator", "group 3"):
        with pytest.raises(ValueError):
            cm.rename_cluster(tmp_path, "site huts", bad)
    assert db.cluster_counts(conn) == {"excavator": 20, "site huts": 12}
    assert cm.rename_cluster(tmp_path, "site huts", "site huts") == 12      # a no-op rename is not a collision


def test_people_photo_keeps_its_scene_as_the_guess(tmp_path, monkeypatch):
    """A beach photo with a face is filed under people but the beach tile still owns it."""
    from photosort import classify as c, db, search
    root = tmp_path / "shoot"; root.mkdir()
    conn = db.connect(root)
    conn.execute("INSERT INTO photos(id, rel, status, n_faces, kind) VALUES (1, 'a.jpg', 'ok', 2, 'photo')")
    conn.execute("INSERT INTO photos(id, rel, status, n_faces, kind) VALUES (2, 'b.jpg', 'ok', 1, 'photo')")
    conn.commit()
    # what _score would say before the face rule: 1 is a confident beach, 2 a confident portrait
    scored = {1: ("beach", 0.9, 0.5, "beach", 0.9, {"beach": 0.9}), 2: ("people", 0.8, 0.4, "people", 0.8, {"people": 0.8})}
    monkeypatch.setattr(c, "_score", lambda M, T, owner, names: [scored[i] for i in owner_ids])
    owner_ids = [1, 2]
    monkeypatch.setattr(db, "load_embeds", lambda conn_: (__import__("numpy").array(owner_ids), __import__("numpy").zeros((2, 512), "float32")))
    monkeypatch.setattr(db, "load_segment_embeds", lambda conn_: (__import__("numpy").array([], int), __import__("numpy").zeros((0, 512), "float32")))
    monkeypatch.setattr(c, "_prompt_matrix", lambda emb: (["beach", "people"], __import__("numpy").zeros((2, 512), "float32"), [0, 1]))
    monkeypatch.setattr(c, "get_embedder", lambda: None, raising=False)
    import photosort.embed as embed_mod
    monkeypatch.setattr(embed_mod, "get_embedder", lambda: None)
    photos, _ = c._classify_all(root)
    by = {p["id"]: p for p in photos}
    assert by[1]["category"] == "people" and by[1]["guess"] == "beach" and by[1]["guess_score"] == 0.9
    assert by[2]["category"] == "people" and by[2]["guess"] == "people"
    row = {"category": "people", "category_score": 1.0, "category_guess": "beach", "category_guess_score": 0.9}
    assert search.category_match(row, "beach") == (True, 0.9)
    assert search.category_match(row, "people") == (True, 1.0)
    assert search.category_match(row, "road") is None
