"""The marks over HTTP: set them, filter the grid by them, count them for the sidebar, write the XMP sidecars."""
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from photosort.index import index_folder
from photosort.server import create_app


def _client(tmp_path, n=3):
    from conftest import make_image
    for k in range(n):
        make_image(tmp_path, f"p{k}.jpg", seed=k)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    ids = [r["id"] for r in c.get("/api/search?sort=name").json()["results"]]
    return c, ids


def _ids(c, qs):
    return sorted(r["id"] for r in c.get("/api/search?" + qs).json()["results"])


def test_set_marks_and_see_them_on_the_results(tmp_path):
    c, (a, b, d) = _client(tmp_path)
    r = c.post("/api/marks", json={"ids": [a, b], "flag": "pick"})
    assert r.status_code == 200
    assert r.json()["marks"] == {str(a): {"flag": "pick", "rating": 0, "label": None},
                                 str(b): {"flag": "pick", "rating": 0, "label": None}}
    c.post("/api/marks", json={"ids": [a], "rating": 4, "label": "yellow"})
    rows = {x["id"]: x for x in c.get("/api/search").json()["results"]}
    assert (rows[a]["flag"], rows[a]["rating"], rows[a]["label"]) == ("pick", 4, "yellow")
    assert (rows[d]["flag"], rows[d]["rating"], rows[d]["label"]) == (None, 0, None)
    # a field sent as null is cleared, a field left out is kept
    c.post("/api/marks", json={"ids": [a], "flag": None})
    rows = {x["id"]: x for x in c.get("/api/search").json()["results"]}
    assert (rows[a]["flag"], rows[a]["rating"], rows[a]["label"]) == (None, 4, "yellow")


def test_search_filters_by_flag_stars_and_label(tmp_path):
    c, (a, b, d) = _client(tmp_path)
    c.post("/api/marks", json={"ids": [a], "flag": "pick", "rating": 5, "label": "red"})
    c.post("/api/marks", json={"ids": [b], "flag": "reject", "rating": 2})
    assert _ids(c, "flag=pick") == [a]
    assert _ids(c, "flag=reject") == [b]
    assert _ids(c, "flag=none") == [d]
    assert _ids(c, "stars=2") == [a, b]
    assert _ids(c, "stars=3") == [a]
    assert _ids(c, "label=red") == [a]
    assert _ids(c, "label=none") == [b, d]
    assert _ids(c, "flag=pick&label=blue") == []
    assert sorted(c.get("/api/search/ids?stars=2").json()["ids"]) == [a, b]
    for bad in ("flag=keep", "stars=6", "stars=0", "label=orange"):
        assert c.get("/api/search?" + bad).status_code == 400, bad


@pytest.mark.parametrize("body", [{"flag": "keep"}, {"rating": 9}, {"rating": 2.5}, {"label": "teal"}])
def test_bad_marks_are_a_400_and_change_nothing(tmp_path, body):
    c, (a, b, d) = _client(tmp_path)
    assert c.post("/api/marks", json=dict(body, ids=[a])).status_code == 400
    row = [x for x in c.get("/api/search").json()["results"] if x["id"] == a][0]
    assert (row["flag"], row["rating"], row["label"]) == (None, 0, None)


def test_marks_need_ids_and_a_field(tmp_path):
    c, (a, b, d) = _client(tmp_path)
    assert c.post("/api/marks", json={"ids": [], "flag": "pick"}).status_code == 400
    assert c.post("/api/marks", json={"ids": [a]}).status_code == 400


def test_mark_counts(tmp_path):
    c, (a, b, d) = _client(tmp_path)
    c.post("/api/marks", json={"ids": [a, b], "flag": "pick", "rating": 3})
    c.post("/api/marks", json={"ids": [d], "label": "purple"})
    n = c.get("/api/marks/counts").json()
    assert n["flag"] == {"pick": 2, "reject": 0, "none": 1}
    assert n["stars"]["3"] == 2 and n["stars"]["4"] == 0
    assert n["label"]["purple"] == 1 and n["label"]["none"] == 2
    assert n["marked"] == 3


def test_write_xmp_writes_the_sidecars_outside_the_shoot(tmp_path):
    c, (a, b, d) = _client(tmp_path)
    before = sorted(os.listdir(tmp_path))
    c.post("/api/marks", json={"ids": [a], "rating": 5})
    c.post("/api/marks", json={"ids": [b], "flag": "reject"})
    r = c.post("/api/xmp", json={}).json()                       # no ids: every marked item in the shoot
    out = Path(r["path"])
    assert r["written"] == 2 and not str(out.resolve()).startswith(str(tmp_path.resolve()))
    assert sorted(os.listdir(out)) == ["p0.xmp", "p1.xmp"]
    r2 = c.post("/api/xmp", json={"ids": [a, d]}).json()
    assert (r2["written"], r2["unmarked"]) == (1, 1)
    assert sorted(os.listdir(tmp_path)) == before


def test_write_xmp_with_nothing_marked_says_so(tmp_path):
    c, ids = _client(tmp_path)
    assert c.post("/api/xmp", json={}).status_code == 400


def test_a_selection_export_carries_sidecars(tmp_path):
    import time
    c, (a, b, d) = _client(tmp_path)
    c.post("/api/marks", json={"ids": [a], "flag": "pick", "rating": 4})
    assert c.post("/api/export", json={"ids": [a, b], "name": "sel"}).json()["started"]
    for _ in range(200):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.05)
    assert sorted(os.listdir(p["path"])) == ["p0.jpg", "p0.xmp", "p1.jpg"]
