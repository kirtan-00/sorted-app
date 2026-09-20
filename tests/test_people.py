import pytest
import numpy as np
from photosort import db
from photosort.people import cluster_faces, name_person, list_people, export_people
from photosort.config import FACE_MATCH_MIN_SIM

def _fake_shoot(tmp_path, n_people=3, per=4):
    conn = db.connect(tmp_path); rng = np.random.default_rng(1)
    centers = rng.normal(size=(n_people, 128)); centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    for k in range(n_people):
        for j in range(per):
            (tmp_path / f"p{k}_{j}.jpg").write_bytes(b"x")
            pid = db.upsert_photo(conn, dict(rel=f"p{k}_{j}.jpg", qhash=f"q{k}{j}", n_faces=1, status="ok"))
            v = centers[k] + rng.normal(scale=0.05, size=128); v /= np.linalg.norm(v)
            db.replace_faces(conn, pid, [dict(x=0,y=0,w=40,h=40,score=0.9,landmarks="[]",eye_sharp=500.0,embed=v.astype(np.float32).tobytes())])
    return conn

def test_cluster_and_name(tmp_path):
    _fake_shoot(tmp_path)
    people = cluster_faces(tmp_path, eps=0.3)
    assert len(people) == 3 and all(p["n"] == 4 for p in people)
    name_person(tmp_path, people[0]["id"], "Arya")
    assert list_people(tmp_path)[0]["name"] == "Arya"
    out = export_people(tmp_path)
    assert (out / "people" / "Arya").is_dir() and len(list((out / "people" / "Arya").iterdir())) == 4
    assert (out / "solo").is_dir() and len(list((out / "solo").iterdir())) == 12

def test_name_survives_recluster(tmp_path):
    _fake_shoot(tmp_path)
    people = cluster_faces(tmp_path, eps=0.3)
    target = people[1]["id"]
    name_person(tmp_path, target, "Arya")
    target_faces = {r[0] for r in db.connect(tmp_path).execute("SELECT id FROM faces WHERE person_id=?", (target,))}
    people2 = cluster_faces(tmp_path, eps=0.35)
    named = [p for p in people2 if p["name"] == "Arya"]
    assert len(named) == 1
    conn = db.connect(tmp_path)
    assert {r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (named[0]["id"],))} == target_faces
    assert sum(1 for p in people2 if p["name"]) == 1

def test_person_name_export_is_sanitised(tmp_path):
    _fake_shoot(tmp_path)
    people = cluster_faces(tmp_path, eps=0.3)
    name_person(tmp_path, people[0]["id"], "../../Ar/ya")
    out = export_people(tmp_path)
    names = sorted(p.name for p in (out / "people").iterdir())
    assert "_.._Ar_ya" in names and not any(n in (".", "..") for n in names)
    assert not (out.parent / "Ar").exists() and not (out / "people" / "Ar").exists()
    assert sorted(x.name for x in tmp_path.iterdir()) == sorted(f"p{k}_{j}.jpg" for k in range(3) for j in range(4))

def test_cover_falls_back_when_face_row_gone(tmp_path):
    conn = _fake_shoot(tmp_path)
    people = cluster_faces(tmp_path, eps=0.3)
    p0 = people[0]
    conn.execute("DELETE FROM faces WHERE id=?", (p0["cover_face_id"],)); conn.commit()
    again = [p for p in list_people(tmp_path) if p["id"] == p0["id"]][0]
    assert again["cover_face_id"] != p0["cover_face_id"] and again["cover_qhash"] is not None
    conn.execute("DELETE FROM faces WHERE person_id=?", (p0["id"],)); conn.commit()
    again = [p for p in list_people(tmp_path) if p["id"] == p0["id"]][0]
    assert again["cover_face_id"] is None and again["cover_qhash"] is None and again["cover_box"] == [0, 0, 0, 0]


# find by reference

def _p0_reference(conn, w=50, h=50):
    """A fake reference face: person 0's centre plus a little noise, normalised."""
    from photosort.faces import Face
    rows = conn.execute("SELECT f.embed FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.rel LIKE 'p0_%'").fetchall()
    c = np.stack([np.frombuffer(r[0], np.float32) for r in rows]).mean(axis=0)
    c = c + np.random.default_rng(7).normal(scale=0.02, size=128); c = (c / np.linalg.norm(c)).astype(np.float32)
    return Face(0, 0, w, h, 0.95, np.zeros((5, 2)), c, 1.0)

def _rel_of(conn, photo_id):
    return conn.execute("SELECT rel FROM photos WHERE id=?", (photo_id,)).fetchone()[0]

def test_find_by_reference_matches_person0(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    big = _p0_reference(conn)
    small = _p0_reference(conn, w=5, h=5)
    small.embed = -big.embed   # a smaller decoy face that must be ignored
    monkeypatch.setattr(people, "_reference_faces", lambda path: [small, big])
    out = people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg")
    assert out["faces_in_reference"] == 2
    assert len(out["matches"]) == 4
    assert all(_rel_of(conn, m["photo_id"]).startswith("p0_") for m in out["matches"])
    sims = [m["sim"] for m in out["matches"]]
    assert sims == sorted(sims, reverse=True) and all(s >= FACE_MATCH_MIN_SIM for s in sims)
    assert len({m["photo_id"] for m in out["matches"]}) == 4
    assert out["person_id"] is None   # not clustered yet
    assert people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg", min_sim=0.99)["matches"] == []

def test_find_by_reference_no_face(tmp_path, monkeypatch):
    from photosort import people
    _fake_shoot(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [])
    out = people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg")
    assert out == {"faces_in_reference": 0, "matches": [], "person_id": None}

def test_find_by_reference_reports_cluster(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path)
    cluster_faces(tmp_path, eps=0.3)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_p0_reference(conn)])
    out = people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg")
    expected = {r[0] for r in conn.execute(
        "SELECT DISTINCT f.person_id FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.rel LIKE 'p0_%'")}
    assert len(expected) == 1 and out["person_id"] == expected.pop()

def test_find_by_reference_rejects_tiny_face(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_p0_reference(conn, w=20, h=20)])
    out = people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg")
    assert out["faces_in_reference"] == 1 and out["reference_face_too_small"] is True
    assert out["matches"] == [] and out["person_id"] is None

def test_find_by_reference_unreadable_reference_raises(tmp_path):
    from photosort import people
    _fake_shoot(tmp_path)   # p0_0.jpg is a one-byte stub, so the decoder rejects it
    import pytest
    with pytest.raises(people.ReferenceUnreadable):
        people.find_by_reference(tmp_path, tmp_path / "p0_0.jpg")


# named people from reference photos

def _person_reference(conn, k, w=50, h=50, seed=7):
    """A fake reference face for person k: that person's centre plus a little noise, normalised."""
    from photosort.faces import Face
    rows = conn.execute("SELECT f.embed FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.rel LIKE ?", (f"p{k}_%",)).fetchall()
    c = np.stack([np.frombuffer(r[0], np.float32) for r in rows]).mean(axis=0)
    c = c + np.random.default_rng(seed).normal(scale=0.02, size=128); c = (c / np.linalg.norm(c)).astype(np.float32)
    return Face(0, 0, w, h, 0.95, np.zeros((5, 2)), c, 1.0)

def _listing(tmp_path):
    import os
    return sorted(os.listdir(tmp_path))

def test_save_reference_stores_a_row_per_photo(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = _listing(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0)])
    out = people.save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    assert isinstance(out["id"], int) and out["name"] == "Arya" and out["faces_in_reference"] == 1
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0, seed=8)])
    out2 = people.save_reference(tmp_path, "  Arya ", tmp_path / "p0_1.jpg")   # a second photo of the same person
    assert out2["name"] == "Arya" and out2["id"] != out["id"]
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 1)])
    out3 = people.save_reference(tmp_path, "Priest", tmp_path / "p1_0.jpg")
    rows = db.list_references(db.connect(tmp_path))
    assert [(r["id"], r["name"], r["source"]) for r in rows] == [
        (out["id"], "Arya", str(tmp_path / "p0_0.jpg")), (out2["id"], "Arya", str(tmp_path / "p0_1.jpg")),
        (out3["id"], "Priest", str(tmp_path / "p1_0.jpg"))]
    assert all(r["created_at"] for r in rows)
    ids, names, R = db.load_reference_embeds(db.connect(tmp_path))
    assert ids.tolist() == [out["id"], out2["id"], out3["id"]] and names == ["Arya", "Arya", "Priest"]
    assert R.shape == (3, 128) and R.dtype == np.float32
    assert np.allclose(np.linalg.norm(R, axis=1), 1.0, atol=1e-3)
    assert _listing(tmp_path) == before

def test_save_reference_rejects_no_face_tiny_face_and_unreadable(tmp_path, monkeypatch):
    import pytest
    from photosort import people
    conn = _fake_shoot(tmp_path)
    before = _listing(tmp_path)
    with pytest.raises(people.ReferenceUnreadable):        # p0_0.jpg is a one-byte stub, real decode fails
        people.save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [])
    with pytest.raises(ValueError, match="no usable face"):
        people.save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0, w=20, h=20)])
    with pytest.raises(ValueError, match="too small"):
        people.save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0)])
    with pytest.raises(ValueError, match="name"):
        people.save_reference(tmp_path, "   ", tmp_path / "p0_0.jpg")
    for bad in (".", ".."):
        with pytest.raises(ValueError, match="cannot be used as a folder"):
            people.save_reference(tmp_path, bad, tmp_path / "p0_0.jpg")
    assert db.list_references(db.connect(tmp_path)) == []
    assert _listing(tmp_path) == before

def _two_named_people(tmp_path, monkeypatch, conn):
    from photosort import people
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0)])
    people.save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0, seed=8)])
    people.save_reference(tmp_path, "Arya", tmp_path / "p0_1.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 1)])
    people.save_reference(tmp_path, "Priest", tmp_path / "p1_0.jpg")

def test_match_references_one_list_per_name(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = _listing(tmp_path)
    _two_named_people(tmp_path, monkeypatch, conn)
    out = people.match_references(tmp_path)
    assert set(out) == {"Arya", "Priest"}
    for name, k in [("Arya", 0), ("Priest", 1)]:
        assert len(out[name]) == 4 and len({m["photo_id"] for m in out[name]}) == 4
        assert all(_rel_of(conn, m["photo_id"]).startswith(f"p{k}_") for m in out[name])
        sims = [m["sim"] for m in out[name]]
        assert sims == sorted(sims, reverse=True) and all(s >= FACE_MATCH_MIN_SIM for s in sims)
    strict = people.match_references(tmp_path, min_sim=0.99)
    assert strict == {"Arya": [], "Priest": []}
    assert _listing(tmp_path) == before

def test_match_references_photo_with_two_people_is_under_both(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    # one extra frame holding a face of person 0 and a face of person 1
    (tmp_path / "both.jpg").write_bytes(b"x")
    pid = db.upsert_photo(conn, dict(rel="both.jpg", qhash="qboth", n_faces=2, status="ok"))
    faces = []
    for k in (0, 1):
        v = _person_reference(conn, k, seed=11 + k).embed
        faces.append(dict(x=0, y=0, w=10, h=10, score=0.9, landmarks="[]", eye_sharp=1.0, embed=v.tobytes()))
    db.replace_faces(conn, pid, faces)
    before = _listing(tmp_path)
    _two_named_people(tmp_path, monkeypatch, conn)
    out = people.match_references(tmp_path)
    assert pid in {m["photo_id"] for m in out["Arya"]} and pid in {m["photo_id"] for m in out["Priest"]}
    assert len(out["Arya"]) == 5 and len(out["Priest"]) == 5
    assert _listing(tmp_path) == before

def test_export_references_ids_maps_safe_folder_to_ids(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    before = _listing(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0)])
    people.save_reference(tmp_path, "Ar/ya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 1)])
    people.save_reference(tmp_path, "Priest", tmp_path / "p1_0.jpg")
    out = people.export_references_ids(tmp_path, None, FACE_MATCH_MIN_SIM)
    assert set(out) == {"Ar_ya", "Priest"}
    assert sorted(_rel_of(conn, i) for i in out["Ar_ya"]) == [f"p0_{j}.jpg" for j in range(4)]
    assert sorted(_rel_of(conn, i) for i in out["Priest"]) == [f"p1_{j}.jpg" for j in range(4)]
    only = people.export_references_ids(tmp_path, ["Priest"], FACE_MATCH_MIN_SIM)
    assert set(only) == {"Priest"} and len(only["Priest"]) == 4
    assert people.export_references_ids(tmp_path, ["nobody"], FACE_MATCH_MIN_SIM) == {}
    assert people.export_references_ids(tmp_path, ["Priest"], 0.99) == {"Priest": []}
    assert _listing(tmp_path) == before

def test_export_references_ids_keeps_colliding_segments_apart(tmp_path, monkeypatch):
    """Two different names ('Ar/ya' and 'Ar_ya') sanitise to the same folder segment; they must
    each get their own folder, the later one suffixed, rather than one swallowing the other."""
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=2, per=4)
    before = _listing(tmp_path)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 0)])
    people.save_reference(tmp_path, "Ar/ya", tmp_path / "p0_0.jpg")
    monkeypatch.setattr(people, "_reference_faces", lambda path: [_person_reference(conn, 1)])
    people.save_reference(tmp_path, "Ar_ya", tmp_path / "p1_0.jpg")
    out = people.export_references_ids(tmp_path, None, FACE_MATCH_MIN_SIM)
    assert set(out) == {"Ar_ya", "Ar_ya_2"}
    assert sorted(_rel_of(conn, i) for i in out["Ar_ya"]) == [f"p0_{j}.jpg" for j in range(4)]
    assert sorted(_rel_of(conn, i) for i in out["Ar_ya_2"]) == [f"p1_{j}.jpg" for j in range(4)]
    assert _listing(tmp_path) == before

def test_export_references_writes_one_folder_per_person(tmp_path, tmp_path_factory, monkeypatch):
    import os
    from photosort import people
    conn = _fake_shoot(tmp_path, n_people=3, per=4)
    (tmp_path / "p0_0.ARW").write_bytes(b"raw")
    conn.execute("UPDATE photos SET sibling='p0_0.ARW' WHERE rel='p0_0.jpg'")
    conn.execute("UPDATE photos SET size=1"); conn.commit()   # the stubs are one byte; _fake_shoot leaves size NULL
    before = _listing(tmp_path)
    _two_named_people(tmp_path, monkeypatch, conn)
    disk = tmp_path_factory.mktemp("disk")
    seen = []
    out = people.export_references(tmp_path, None, "copy", True, base=disk, progress=seen.append)
    assert out == disk / tmp_path.resolve().name / "people"
    assert sorted(x.name for x in (out / "Arya").iterdir()) == ["p0_0.ARW", "p0_0.jpg", "p0_1.jpg", "p0_2.jpg", "p0_3.jpg"]
    assert sorted(x.name for x in (out / "Priest").iterdir()) == [f"p1_{j}.jpg" for j in range(4)]
    assert seen[-1] == {"done": 9, "total": 9, "failed": 0, "skipped": 0} and not (out / "failed.txt").exists()
    assert people.references_bytes(tmp_path, None, True) == 4 + 4 + 3 and people.references_bytes(tmp_path, ["Priest"], False) == 4
    # a re-export, even under a different mode, finds the same photos already there (matching
    # size and mtime from the copy above) and skips them rather than duplicating or erroring
    seen2 = []
    out2 = people.export_references(tmp_path, ["Priest"], "symlink", False, base=disk, progress=seen2.append)
    links = [x for x in (out2 / "Priest").iterdir() if x.is_symlink()]
    assert links == []
    assert len(os.listdir(out2 / "Priest")) == 4
    assert seen2[-1] == {"done": 4, "total": 4, "failed": 0, "skipped": 4}
    assert _listing(tmp_path) == before

def test_reference_delete_and_rename(tmp_path, monkeypatch):
    from photosort import people
    conn = _fake_shoot(tmp_path)
    before = _listing(tmp_path)
    _two_named_people(tmp_path, monkeypatch, conn)
    c = db.connect(tmp_path)
    assert db.rename_reference(c, "Arya", "Arya Mehta") == 2
    assert [r["name"] for r in db.list_references(c)] == ["Arya Mehta", "Arya Mehta", "Priest"]
    assert set(people.match_references(tmp_path)) == {"Arya Mehta", "Priest"}
    first = db.list_references(c)[0]["id"]
    assert db.delete_reference(c, first) is True and db.delete_reference(c, first) is False
    assert len(db.list_references(c)) == 2 and len(people.match_references(tmp_path)["Arya Mehta"]) == 4
    assert _listing(tmp_path) == before

def test_find_and_match_references_can_return_the_less_sure_band(tmp_path, monkeypatch):
    """The band below the slider (down to max(min_sim - 0.1, 0.4), never above min_sim itself) is only
    returned on request, flagged sure: false; exports and saved-people counts stay sure-only."""
    from photosort import people
    from photosort.people import find_by_reference, match_references, save_reference, export_references_ids
    conn = _fake_shoot(tmp_path, n_people=2, per=4)
    ref = _p0_reference(conn)
    monkeypatch.setattr(people, "_reference_faces", lambda path: [ref])
    plain = find_by_reference(tmp_path, tmp_path / "p0_0.jpg", 0.55)["matches"]
    assert len(plain) == 4 and all(m["sure"] for m in plain)
    sims = sorted((m["sim"] for m in plain), reverse=True)
    cut = (sims[1] + sims[2]) / 2
    assert len(find_by_reference(tmp_path, tmp_path / "p0_0.jpg", cut)["matches"]) == 2
    banded = find_by_reference(tmp_path, tmp_path / "p0_0.jpg", cut, unsure_band=True)["matches"]
    assert [m["sure"] for m in banded] == [True, True, False, False]
    assert [m["sim"] for m in banded] == sorted((m["sim"] for m in banded), reverse=True)
    assert people.band_floor(0.55) == 0.45 and people.band_floor(0.42) == 0.4 and people.band_floor(0.3) == 0.3
    save_reference(tmp_path, "Arya", tmp_path / "p0_0.jpg")
    assert len(match_references(tmp_path, cut)["Arya"]) == 2
    assert [m["sure"] for m in match_references(tmp_path, cut, unsure_band=True)["Arya"]] == [True, True, False, False]
    assert len(export_references_ids(tmp_path, None, cut)["Arya"]) == 2


# merge suggestions and remembered decisions

def _unit(v):
    v = np.asarray(v, np.float64); return v / np.linalg.norm(v)

def _two_close_people(tmp_path, gap=0.40, per=4, noise=0.02, seed=3):
    """Two synthetic people whose centres sit `gap` apart in cosine distance (well past the tight eps,
    inside the loose one), `per` faces each. Returns (conn, listing of the shoot dir)."""
    conn = db.connect(tmp_path); rng = np.random.default_rng(seed)
    a = _unit(rng.normal(size=128)); o = rng.normal(size=128); o = _unit(o - o @ a * a)
    # cosine(a, b) = 1 - gap  ->  b = cos*a + sin*o
    cos = 1 - gap; b = _unit(cos * a + np.sqrt(1 - cos * cos) * o)
    for k, c in enumerate((a, b)):
        for j in range(per):
            (tmp_path / f"p{k}_{j}.jpg").write_bytes(b"x")
            pid = db.upsert_photo(conn, dict(rel=f"p{k}_{j}.jpg", qhash=f"q{k}{j}", n_faces=1, status="ok"))
            v = _unit(c + rng.normal(scale=noise, size=128))
            db.replace_faces(conn, pid, [dict(x=k, y=j, w=40, h=40, score=0.9 - 0.01 * j, landmarks="[]", eye_sharp=500.0,
                                              embed=v.astype(np.float32).tobytes())])
    return conn, sorted(x.name for x in tmp_path.iterdir())

def _faces_of(conn, pid):
    return {r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (pid,))}

def test_tight_eps_splits_what_loose_eps_joins(tmp_path):
    from photosort.people import suggest_merges
    conn, listing = _two_close_people(tmp_path, gap=0.40)
    assert len(cluster_faces(tmp_path, eps=0.5)) == 1
    tight = cluster_faces(tmp_path, eps=0.2)
    assert len(tight) == 2 and all(p["n"] == 4 for p in tight)
    assert sorted(x.name for x in tmp_path.iterdir()) == listing

def test_same_link_joins_two_clusters_on_recluster(tmp_path):
    from photosort.people import merge_people, cluster_faces as cf
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    a, b = cf(tmp_path, eps=0.2)
    name_person(tmp_path, a["id"], "Arya")
    merged = merge_people(tmp_path, keep=a["id"], drop=b["id"])
    assert merged["id"] == a["id"] and merged["n"] == 8 and merged["name"] == "Arya"
    assert len(list_people(tmp_path)) == 1
    assert conn.execute("SELECT count(*) FROM people WHERE id=?", (b["id"],)).fetchone()[0] == 0
    assert len(_faces_of(conn, a["id"])) == 8
    links = db.face_links(conn)
    assert len(links) == 1 and links[0][2] == "same" and links[0][0] < links[0][1]
    # the link survives a re-cluster at the tight eps: DBSCAN splits, the link joins them back
    again = cf(tmp_path, eps=0.2)
    assert len(again) == 1 and again[0]["n"] == 8 and again[0]["name"] == "Arya"

def test_same_link_pulls_a_noise_face_into_the_cluster(tmp_path):
    conn, _ = _two_close_people(tmp_path, gap=0.40, per=4)
    # one lonely face far from everyone: noise at any sane eps
    (tmp_path / "lone.jpg").write_bytes(b"x")
    pid = db.upsert_photo(conn, dict(rel="lone.jpg", qhash="qlone", n_faces=1, status="ok"))
    lone = _unit(np.random.default_rng(9).normal(size=128)).astype(np.float32)
    db.replace_faces(conn, pid, [dict(x=0, y=0, w=40, h=40, score=0.5, landmarks="[]", eye_sharp=500.0, embed=lone.tobytes())])
    lone_id = conn.execute("SELECT id FROM faces WHERE photo_id=?", (pid,)).fetchone()[0]
    people = cluster_faces(tmp_path, eps=0.2)
    assert len(people) == 2 and conn.execute("SELECT person_id FROM faces WHERE id=?", (lone_id,)).fetchone()[0] is None
    a_cover = people[0]["cover_face_id"]
    db.add_face_link(conn, a_cover, lone_id, "same")
    people = cluster_faces(tmp_path, eps=0.2)
    owner = conn.execute("SELECT person_id FROM faces WHERE id=?", (lone_id,)).fetchone()[0]
    assert owner is not None and a_cover in _faces_of(conn, owner) and [p for p in people if p["id"] == owner][0]["n"] == 5

def test_different_link_stops_an_auto_merge(tmp_path, monkeypatch):
    from photosort import people as pm
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    # centroids sit at cosine 0.60: above this auto threshold, so the two DBSCAN clusters would merge
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 0.5)
    assert len(cluster_faces(tmp_path, eps=0.2)) == 1
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 1.0)
    a, b = cluster_faces(tmp_path, eps=0.2)
    pm.reject_merge(tmp_path, a["id"], b["id"])
    assert [l[2] for l in db.face_links(conn)] == ["different"]
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 0.5)
    assert len(cluster_faces(tmp_path, eps=0.2)) == 2

def test_suggest_merges_near_pair_not_far_pair_and_not_rejected(tmp_path, monkeypatch):
    from photosort import people as pm
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    # a third person far from both
    rng = np.random.default_rng(11)
    for j in range(4):
        (tmp_path / f"p2_{j}.jpg").write_bytes(b"x")
        pid = db.upsert_photo(conn, dict(rel=f"p2_{j}.jpg", qhash=f"q2{j}", n_faces=1, status="ok"))
        v = _unit(np.array([0.0] * 127 + [1.0]) + rng.normal(scale=0.02, size=128))
        db.replace_faces(conn, pid, [dict(x=2, y=j, w=40, h=40, score=0.8, landmarks="[]", eye_sharp=500.0, embed=v.astype(np.float32).tobytes())])
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 1.0)
    monkeypatch.setattr(pm, "FACE_MERGE_SUGGEST_SIM", 0.45)
    people = cluster_faces(tmp_path, eps=0.2)
    assert len(people) == 3
    sug = pm.suggest_merges(tmp_path)
    assert len(sug) == 1
    s = sug[0]
    assert s["sim"] > 0.5 and {s["a"], s["b"]} != {people[2]["id"]}
    assert s["a_n"] == 4 and s["b_n"] == 4 and s["a_name"] is None and s["b_name"] is None
    assert set(s["a_cover"]) == {"qhash", "box"} and len(s["a_cover"]["box"]) == 4
    assert len(s["a_faces"]) == 3 and len(s["b_faces"]) == 3 and s["a_cover"]["qhash"] not in {f["qhash"] for f in s["a_faces"]}
    pm.reject_merge(tmp_path, s["a"], s["b"])
    assert pm.suggest_merges(tmp_path) == []
    assert pm.suggest_merges(tmp_path, limit=0) == []

def test_merge_people_keeps_the_name_of_either_side_and_heals_cover(tmp_path):
    from photosort.people import merge_people
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    a, b = cluster_faces(tmp_path, eps=0.2)
    name_person(tmp_path, b["id"], "Bea")
    merged = merge_people(tmp_path, keep=a["id"], drop=b["id"])
    assert merged["name"] == "Bea" and merged["n"] == 8 and merged["cover_face_id"] == a["cover_face_id"]
    lk = db.face_links(conn)
    assert lk == [(min(a["cover_face_id"], b["cover_face_id"]), max(a["cover_face_id"], b["cover_face_id"]), "same")]

def test_merge_and_reject_guard_bad_ids(tmp_path):
    from photosort.people import merge_people, reject_merge
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    a, b = cluster_faces(tmp_path, eps=0.2)
    with pytest.raises(ValueError):
        merge_people(tmp_path, keep=a["id"], drop=a["id"])
    with pytest.raises(ValueError):
        merge_people(tmp_path, keep=a["id"], drop=999)
    with pytest.raises(ValueError):
        reject_merge(tmp_path, a["id"], a["id"])
    with pytest.raises(ValueError):
        reject_merge(tmp_path, 999, b["id"])
    assert db.face_links(conn) == [] and len(list_people(tmp_path)) == 2

def test_reject_then_same_overrides(tmp_path):
    from photosort.people import merge_people, reject_merge
    conn, _ = _two_close_people(tmp_path, gap=0.40)
    a, b = cluster_faces(tmp_path, eps=0.2)
    reject_merge(tmp_path, a["id"], b["id"])
    reject_merge(tmp_path, b["id"], a["id"])          # same pair either way round: one row
    assert [l[2] for l in db.face_links(conn)] == ["different"]
    merge_people(tmp_path, keep=a["id"], drop=b["id"])   # the user changed their mind: same wins
    assert [l[2] for l in db.face_links(conn)] == ["same"]


# faces too small or too blurred to trust never enter a group

def _add_face(conn, tmp_path, rel, embed, w=40, h=40, eye_sharp=500.0):
    (tmp_path / rel).write_bytes(b"x")
    pid = db.upsert_photo(conn, dict(rel=rel, qhash="q" + rel, n_faces=1, status="ok"))
    db.replace_faces(conn, pid, [dict(x=0, y=0, w=w, h=h, score=0.95, landmarks="[]", eye_sharp=eye_sharp, embed=np.asarray(embed, np.float32).tobytes())])
    return conn.execute("SELECT id FROM faces WHERE photo_id=?", (pid,)).fetchone()[0]

def test_tiny_and_blurred_faces_stay_out_of_groups(tmp_path, monkeypatch):
    from photosort import people as pm
    monkeypatch.setattr(pm, "FACE_CLUSTER_MIN_EDGE", 12)
    monkeypatch.setattr(pm, "FACE_CLUSTER_MIN_EYE_SHARP", 40.0)
    conn, listing = _two_close_people(tmp_path, gap=0.40)
    c0 = np.frombuffer(conn.execute("SELECT embed FROM faces WHERE x=0 LIMIT 1").fetchone()[0], np.float32)
    tiny = _add_face(conn, tmp_path, "tiny.jpg", c0, w=11, h=30)          # min edge 11 < 12
    blurred = _add_face(conn, tmp_path, "blur.jpg", c0, eye_sharp=39.0)   # big enough, not in focus
    ok = _add_face(conn, tmp_path, "ok.jpg", c0, w=12, h=12, eye_sharp=40.0)   # both floors are inclusive
    people = cluster_faces(tmp_path, eps=0.2)
    owner = {r[0]: r[1] for r in conn.execute("SELECT id, person_id FROM faces")}
    assert owner[tiny] is None and owner[blurred] is None and owner[ok] is not None
    assert len(people) == 2 and sorted(p["n"] for p in people) == [4, 5]
    # still findable by reference: matching never looks at size or focus
    from photosort.faces import Face
    monkeypatch.setattr(pm, "_reference_faces", lambda path: [Face(0, 0, 60, 60, 0.95, np.zeros((5, 2)), c0, 1.0)])
    found = pm.find_by_reference(tmp_path, tmp_path / "ok.jpg", min_sim=0.9)
    assert {m["face_id"] for m in found["matches"]} >= {tiny, blurred, ok}
    assert sorted(x.name for x in tmp_path.iterdir()) == sorted(listing + ["tiny.jpg", "blur.jpg", "ok.jpg"])

def test_suggestions_rank_bigger_pair_first_at_equal_sim(tmp_path, monkeypatch):
    """Two pairs of groups at the same centroid similarity: the pair whose smaller side has more faces
    ranks first (rank = sim * (1 + log10(min(a_n, b_n)))); sim itself is untouched."""
    from photosort import people as pm
    import math
    monkeypatch.setattr(pm, "FACE_MERGE_AUTO_SIM", 1.0)
    monkeypatch.setattr(pm, "FACE_MERGE_SUGGEST_SIM", 0.45)
    conn = db.connect(tmp_path); rng = np.random.default_rng(5)
    def pair(k, per_a, per_b, gap=0.40):
        a = _unit(rng.normal(size=128)); o = rng.normal(size=128); o = _unit(o - o @ a * a)
        cos = 1 - gap; b = _unit(cos * a + np.sqrt(1 - cos * cos) * o)
        for side, (c, per) in enumerate(((a, per_a), (b, per_b))):
            for j in range(per):
                _add_face(conn, tmp_path, f"g{k}_{side}_{j}.jpg", c)   # identical faces: exact centroids
    pair(0, 3, 3); pair(1, 30, 30)
    people = cluster_faces(tmp_path, eps=0.2)
    assert len(people) == 4
    sug = pm.suggest_merges(tmp_path)
    assert len(sug) == 2 and abs(sug[0]["sim"] - sug[1]["sim"]) < 1e-3
    assert min(sug[0]["a_n"], sug[0]["b_n"]) == 30 and min(sug[1]["a_n"], sug[1]["b_n"]) == 3
    assert sug[0]["rank"] > sug[1]["rank"]
    assert abs(sug[0]["rank"] - sug[0]["sim"] * (1 + math.log10(30))) < 1e-3
