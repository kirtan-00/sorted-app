"""What a person decided outlives what the app recomputes, and survives the app and the power going away."""
import sqlite3
import threading
import time
import numpy as np
import pytest
from conftest import make_image
from photosort import cull, db, people, products
from photosort.index import index_folder
from photosort.config import DB_NAME


def shoot(root, n=6):
    for i in range(n):
        make_image(root, f"p{i}.jpg", size=(300, 200), seed=i)
    index_folder(root, faces=False, workers=1, embed=False)
    return db.connect(root)


def fake_find(monkeypatch, groups, gate=None):
    """find_products with the model taken out: groups is [[row, ...], ...] over the shoot's stills in id order."""
    def embeds(paths, progress=None, batch=32):
        if gate:
            gate.wait()
        return np.eye(len(paths), dtype=np.float32)
    monkeypatch.setattr(products, "grey_embeddings", embeds)
    monkeypatch.setattr(products, "subject_colour", lambda p: np.zeros(3))
    monkeypatch.setattr(products, "group", lambda X, colours: [
        {"rows": g, "variants": [{"rows": g, "lab": np.zeros(3), "views": [0] * len(g)}]} for g in groups])
    class E:
        _product_T = np.zeros((len(products.PRODUCT_VOCAB), 4), np.float32)
    import photosort.embed as em
    monkeypatch.setattr(em, "get_embedder", lambda: E)


# ===== products =====

def test_marks_can_be_set_while_products_are_found(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    gate = threading.Event(); entered = threading.Event()
    fake_find(monkeypatch, [[0, 1, 2]])
    real = products.grey_embeddings
    def slow(paths, progress=None, batch=32):
        entered.set(); gate.wait(10)
        return real(paths, progress, batch)
    monkeypatch.setattr(products, "grey_embeddings", slow)
    t = threading.Thread(target=products.find_products, args=(tmp_path,)); t.start()
    assert entered.wait(10)
    other = sqlite3.connect(db.index_dir(tmp_path) / DB_NAME, timeout=0.3)
    other.execute("UPDATE photos SET rating=5 WHERE id=1"); other.commit()             # was: database is locked
    gate.set(); t.join(10)
    assert conn.execute("SELECT rating FROM photos WHERE id=1").fetchone()[0] == 5


def test_a_find_that_dies_leaves_the_last_result_alone(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    fake_find(monkeypatch, [[0, 1, 2]])
    products.find_products(tmp_path)
    products.rename(conn, 1, "Oak chair")
    def boom(*a, **k):
        raise RuntimeError("the encoder went away")
    monkeypatch.setattr(products, "grey_embeddings", boom)
    with pytest.raises(RuntimeError):
        products.find_products(tmp_path)
    assert [p["name"] for p in products.list_products(db.connect(tmp_path))] == ["Oak chair"]


def test_a_failed_swap_rolls_back_whole(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    fake_find(monkeypatch, [[0, 1, 2]])
    products.find_products(tmp_path)
    products.rename(conn, 1, "Oak chair")
    fake_find(monkeypatch, [[0, 1, 2, 99]])                                            # a row with no photo: the insert fails
    with pytest.raises(IndexError):
        products.find_products(tmp_path)
    assert [p["name"] for p in products.list_products(db.connect(tmp_path))] == ["Oak chair"]


def test_a_rerun_keeps_a_persons_name_where_its_photos_went(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    fake_find(monkeypatch, [[0, 1, 2], [3, 4, 5]])
    products.find_products(tmp_path)
    products.rename(conn, 2, "Oak chair")
    fake_find(monkeypatch, [[0, 1], [2, 3, 4, 5]])                                      # renumbered, and a frame moved over
    products.find_products(tmp_path)
    got = {p["product"]: p for p in products.list_products(conn)}
    assert got[2]["name"] == "Oak chair" and got[1]["name"].startswith("product")


def test_a_split_product_keeps_the_name_on_both_halves(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    fake_find(monkeypatch, [[0, 1, 2, 3]])
    products.find_products(tmp_path)
    products.rename(conn, 1, "Oak chair")
    fake_find(monkeypatch, [[0, 1], [2, 3]])
    products.find_products(tmp_path)
    assert sorted(p["name"] for p in products.list_products(conn)) == ["Oak chair", "Oak chair 2"]


def test_a_made_up_name_is_not_kept(tmp_path, monkeypatch):
    assert products._person_named("product 3", 0) is False
    assert products._person_named(products.PRODUCT_VOCAB[0], 0) is False
    assert products._person_named(products.PRODUCT_VOCAB[0] + " 2", 0) is False
    assert products._person_named("Oak chair", 0) is True                               # an index from before the column
    assert products._person_named("product 3", 1) is True
    assert products._person_named(None, 0) is False


def _cull_rows(conn, rows):
    cull._ensure(conn)
    for pid, product, variant, pick, manual in rows:
        conn.execute("INSERT INTO cull(photo_id, product, variant, angle, retake, score, reason, pick, manual) VALUES(?,?,?,0,0,0.5,NULL,?,?)",
                     (pid, product, variant, pick, manual))
    conn.commit()


def test_a_rerun_keeps_hand_picks_and_drops_the_machines(tmp_path, monkeypatch):
    conn = shoot(tmp_path)
    fake_find(monkeypatch, [[0, 1, 2], [3, 4, 5]])
    products.find_products(tmp_path)
    _cull_rows(conn, [(1, 1, 1, 1, 0), (2, 1, 1, 1, 1), (3, 1, 1, 0, 1), (6, 2, 1, 1, 1)])
    fake_find(monkeypatch, [[3, 4], [0, 1, 2]])                                         # the numbers change places; row 5 leaves
    products.find_products(tmp_path)
    got = {r[0]: tuple(r)[1:] for r in conn.execute("SELECT photo_id, product, variant, pick, manual FROM cull")}
    assert got == {2: (2, 1, 1, 1), 3: (2, 1, 0, 1), 6: (0, 0, 1, 1)}                    # photo 6 has no product: still theirs


def test_client_pick_rerun_keeps_a_hand_choice_on_a_frame_it_judged_again(tmp_path):
    conn = shoot(tmp_path)
    _cull_rows(conn, [(1, 1, 1, 1, 0), (2, 1, 1, 0, 1), (3, 1, 1, 1, 1)])
    judged = {1: [{"id": i, "variant": 1, "angle": 0, "retake": 0, "score": 0.9, "reason": None} for i in (1, 2, 3)]}
    cull._store_and_pick(conn, judged, {"per_product": 3}, set())
    assert {r[0]: (r[1], r[2]) for r in conn.execute("SELECT photo_id, pick, manual FROM cull")} == {1: (1, 0), 2: (0, 1), 3: (1, 1)}
    cull._store_and_pick(conn, judged, {"per_product": 3}, set(), keep_hand=False)     # a new target asked for by hand
    assert conn.execute("SELECT count(*) FROM cull WHERE manual=1").fetchone()[0] == 0


# ===== people =====

def faces_shoot(root, monkeypatch, groups):
    """Faces straight into the index: groups is {cluster: count}, unit vectors far apart between clusters."""
    make_image(root, "a.jpg", size=(300, 200))
    index_folder(root, faces=False, workers=1, embed=False)
    conn = db.connect(root)
    pid = conn.execute("SELECT id FROM photos").fetchone()[0]
    rng = np.random.default_rng(1)
    for g, n in groups.items():
        c = rng.normal(size=128)
        for _ in range(n):
            v = c + rng.normal(size=128) * 0.01
            v = (v / np.linalg.norm(v)).astype(np.float32)
            conn.execute("INSERT INTO faces(photo_id,x,y,w,h,score,landmarks,eye_sharp,embed) VALUES(?,0,0,200,200,0.9,'[]',500,?)", (pid, v.tobytes()))
    conn.commit()
    return conn


def test_a_recluster_that_dies_loses_no_name(tmp_path, monkeypatch):
    conn = faces_shoot(tmp_path, monkeypatch, {"a": 6, "b": 6})
    people.cluster_faces(tmp_path)
    ps = people.list_people(tmp_path); assert len(ps) == 2
    people.name_person(tmp_path, ps[0]["id"], "Asha")
    class Dies:
        def __init__(self, **k): pass
        def fit_predict(self, F): raise MemoryError("DBSCAN")
    monkeypatch.setattr(people, "DBSCAN", Dies)
    with pytest.raises(MemoryError):
        people.cluster_faces(tmp_path)
    assert "Asha" in [p["name"] for p in people.list_people(tmp_path)]                  # was: wiped and committed before DBSCAN
    assert conn.execute("SELECT count(*) FROM faces WHERE person_id IS NOT NULL").fetchone()[0] == 12


def test_a_name_survives_its_group_turning_to_noise(tmp_path, monkeypatch):
    faces_shoot(tmp_path, monkeypatch, {"a": 6, "b": 6})
    people.cluster_faces(tmp_path)
    ps = people.list_people(tmp_path)
    people.name_person(tmp_path, ps[0]["id"], "Asha")
    people.cluster_faces(tmp_path, min_samples=50)                                      # nothing clusters at all
    assert [(p["name"], p["n"]) for p in people.list_people(tmp_path)] == [("Asha", 1)]


def test_a_name_outvoted_inside_a_merged_group_keeps_its_faces(tmp_path, monkeypatch):
    conn = faces_shoot(tmp_path, monkeypatch, {"a": 6, "b": 8})
    people.cluster_faces(tmp_path)
    ps = people.list_people(tmp_path)
    people.name_person(tmp_path, ps[0]["id"], "Big")
    people.name_person(tmp_path, ps[1]["id"], "Small")
    people.cluster_faces(tmp_path, eps=1.9)                                             # one group holds everybody
    got = {r[0]: r[1] for r in conn.execute("SELECT pe.name, count(*) FROM faces f JOIN people pe ON pe.id=f.person_id GROUP BY pe.id")}
    assert sorted(got) == ["Big", "Small"] and sum(got.values()) == 14


def test_a_chosen_cover_stays(tmp_path, monkeypatch):
    conn = faces_shoot(tmp_path, monkeypatch, {"a": 6})
    people.cluster_faces(tmp_path)
    p = people.list_people(tmp_path)[0]
    other = conn.execute("SELECT max(id) FROM faces").fetchone()[0]
    conn.execute("UPDATE people SET name='Asha', cover_face_id=? WHERE id=?", (other, p["id"])); conn.commit()
    people.cluster_faces(tmp_path)
    assert people.list_people(tmp_path)[0]["cover_face_id"] == other


def test_no_faces_and_no_names_is_still_an_empty_list(tmp_path):
    make_image(tmp_path, "a.jpg", size=(300, 200))
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert people.cluster_faces(tmp_path) == []


# ===== durability =====

@pytest.mark.parametrize("write", [
    lambda c: db.set_marks(c, [1], rating=4),
    lambda c: products.rename(c, 1, "x"),
    lambda c: cull.set_pick(c, 1, True),
    lambda c: db.add_face_link(c, 1, 2, "same"),
])
def test_a_persons_write_goes_all_the_way_to_the_disk(tmp_path, write):
    conn = shoot(tmp_path, 2)
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 0
    write(conn)
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1


def test_the_scan_stays_off_it_and_checkpoints_stay_on_it(tmp_path):
    conn = shoot(tmp_path, 1)
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 0
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1


# ===== backups =====

def age_backups(root, by=7200):
    import os
    for p in db.list_backups(root):
        st = p.stat(); os.utime(p, (st.st_atime - by, st.st_mtime - by))


def test_open_writes_a_backup_that_is_a_real_copy(tmp_path):
    conn = shoot(tmp_path, 2)
    db.set_marks(conn, [1], rating=3)
    assert db.open_check(tmp_path) is None
    (b,) = db.list_backups(tmp_path)
    assert sqlite3.connect(b).execute("SELECT rating FROM photos WHERE id=1").fetchone()[0] == 3


def test_backups_roll_and_stop_at_the_limit(tmp_path, monkeypatch):
    conn = shoot(tmp_path, 1)
    names = iter(f"index-2026010{i}-000000" for i in range(1, 9))
    real = db.time.strftime
    monkeypatch.setattr(db.time, "strftime", lambda f, *a: next(names).split("-", 1)[1] if f == "%Y%m%d-%H%M%S" else real(f, *a))
    for i in range(6):
        db.set_marks(conn, [1], rating=i % 5)
        assert db.open_check(tmp_path) is None
        age_backups(tmp_path)
    got = db.list_backups(tmp_path)
    assert len(got) == db.BACKUPS_KEEP == 3 and got[0].name == "index-20260106-000000.db"


def test_no_new_backup_within_the_hour_or_without_a_change(tmp_path):
    conn = shoot(tmp_path, 1)
    db.open_check(tmp_path)
    db.set_marks(conn, [1], rating=2)
    db.open_check(tmp_path)
    assert len(db.list_backups(tmp_path)) == 1


def smash(root):
    f = db.index_dir(root) / DB_NAME
    c = sqlite3.connect(f); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
    import gc; gc.collect()                            # the scan's own connection goes with its last reference
    data = bytearray(f.read_bytes())
    assert len(data) > 8192
    data[4096:len(data)] = b"\xff" * (len(data) - 4096)
    f.write_bytes(data)
    (f.with_name(DB_NAME + "-wal")).unlink(missing_ok=True); (f.with_name(DB_NAME + "-shm")).unlink(missing_ok=True)


def test_a_damaged_index_goes_back_to_the_backup_and_is_kept_aside(tmp_path):
    conn = shoot(tmp_path, 2)
    db.set_marks(conn, [1], rating=3)
    conn.close()
    db.open_check(tmp_path)
    smash(tmp_path)
    note = db.open_check(tmp_path)
    assert "went back to the backup" in note and "--" not in note and "—" not in note
    assert db.connect(tmp_path).execute("SELECT rating FROM photos WHERE id=1").fetchone()[0] == 3
    assert list((db.index_dir(tmp_path) / db.BACKUP_DIR).glob("damaged-*.db"))


def test_a_damaged_index_with_no_backup_says_so_and_is_left_where_it_is(tmp_path):
    conn = shoot(tmp_path, 2); conn.close()
    smash(tmp_path)
    assert "no backup" in db.open_check(tmp_path)
    assert (db.index_dir(tmp_path) / DB_NAME).is_file()


def test_the_page_hears_about_a_restore_once(tmp_path):
    from fastapi.testclient import TestClient
    from photosort.server import create_app
    conn = shoot(tmp_path, 2); conn.close()
    db.open_check(tmp_path)
    smash(tmp_path)
    c = TestClient(create_app(tmp_path))
    assert "went back to the backup" in c.get("/api/folder").json()["index_note"]
    assert c.get("/api/folder").json()["index_note"] is None
    assert c.get("/api/stats").json()["photos"] == 2


def test_a_backup_never_fills_the_disk(tmp_path, monkeypatch):
    conn = shoot(tmp_path, 1); conn.close()
    class U: free = 10
    monkeypatch.setattr(db.shutil, "disk_usage", lambda p: U)
    assert db.open_check(tmp_path) is None and db.list_backups(tmp_path) == []


def test_backups_stay_out_of_the_project_file(tmp_path):
    from photosort import bundle
    conn = shoot(tmp_path, 1); conn.close()
    db.open_check(tmp_path)
    assert not [n for n, _ in bundle.bundle_files(tmp_path) if "backup" in n]
