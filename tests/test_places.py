"""Where a shoot was shot: the catch-up pass that reads locations out of file metadata, the grid that piles
points up for the map, and the box filter behind a click on it."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from photosort import db, places
from photosort.index import index_folder
from photosort.search import Index, Filters
from photosort.server import create_app

AHMEDABAD = (23.0225, 72.5714)
GOA = (15.4909, 73.8278)

def _shoot(tmp_path, located=()):
    from conftest import make_image
    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    for rel, (lat, lon) in located:
        conn.execute("UPDATE photos SET lat=?, lon=? WHERE rel=?", (lat, lon, rel))
    conn.commit()
    return conn

def test_cluster_piles_up_points_that_are_near_each_other():
    pts = [AHMEDABAD, (AHMEDABAD[0] + 0.01, AHMEDABAD[1] + 0.01), GOA]
    piles = places.cluster(pts)
    assert len(piles) == 2
    assert piles[0]["n"] == 2 and piles[1]["n"] == 1
    assert abs(piles[0]["lat"] - AHMEDABAD[0]) < 0.02
    assert piles[0]["south"] <= piles[0]["lat"] <= piles[0]["north"]

def test_cluster_of_nothing_is_nothing():
    assert places.cluster([]) == []

def test_status_counts_what_the_map_can_draw(tmp_path):
    _shoot(tmp_path, [("p0.jpg", AHMEDABAD)])
    st = places.status(tmp_path)
    assert st["located"] == 1 and st["unlocated"] == 2 and st["total"] == 3

def test_a_shoot_indexed_now_is_never_offered_the_catch_up_pass(tmp_path):
    """Indexing reads the location out of every file as it goes. A shoot scanned by this version has nothing
    to catch up on, so the Places tab must not invite a pass that would read every file for nothing."""
    _shoot(tmp_path)
    assert places.status(tmp_path)["read_at"] is not None

def test_read_locations_fills_in_a_shoot_indexed_before_locations(tmp_path):
    """The pass reads the file's own metadata. These fixtures carry none, so it finds nothing and says so,
    and it must still mark itself as run: that is what turns the "no locations read yet" panel off."""
    _shoot(tmp_path)
    conn = db.connect(tmp_path)                              # as an index made before locations existed looks
    conn.execute("DELETE FROM meta WHERE key=?", (places.READ_KEY,)); conn.commit()
    out = places.read_locations(tmp_path)
    assert out == {"found": 0, "read": 3, "missing": 0}
    assert places.status(tmp_path)["read_at"] is not None

def test_read_locations_survives_a_file_that_is_not_there(tmp_path):
    _shoot(tmp_path)
    (tmp_path / "p1.jpg").unlink()
    assert places.read_locations(tmp_path)["missing"] == 1

def test_the_box_filter_keeps_what_is_inside_it(tmp_path):
    _shoot(tmp_path, [("p0.jpg", AHMEDABAD), ("p1.jpg", GOA)])
    ix = Index(tmp_path)
    box = (22.0, 72.0, 24.0, 73.0)
    assert [r["rel"] for r in ix.search(filters=Filters(bbox=box))] == ["p0.jpg"]
    # p2 has no location at all: it is not somewhere else, it is nowhere, so no box ever holds it
    wide = (-90.0, -180.0, 90.0, 180.0)
    assert [r["rel"] for r in ix.search(filters=Filters(bbox=wide))] == ["p0.jpg", "p1.jpg"]

def test_a_box_across_the_date_line_wraps(tmp_path):
    _shoot(tmp_path, [("p0.jpg", (-17.0, 179.0)), ("p1.jpg", (-17.0, -179.0)), ("p2.jpg", (-17.0, 0.0))])
    ix = Index(tmp_path)
    got = [r["rel"] for r in ix.search(filters=Filters(bbox=(-20.0, 170.0, -10.0, -170.0)))]
    assert got == ["p0.jpg", "p1.jpg"]

def test_places_endpoint_reports_the_piles_and_the_bounds(tmp_path):
    _shoot(tmp_path, [("p0.jpg", AHMEDABAD), ("p1.jpg", GOA)])
    c = TestClient(create_app(tmp_path))
    d = c.get("/api/places").json()
    assert d["located"] == 2 and d["unlocated"] == 1 and d["total"] == 3
    assert len(d["clusters"]) == 2 and d["clusters"][0]["n"] == 1
    assert abs(d["bounds"]["south"] - GOA[0]) < 0.001 and abs(d["bounds"]["north"] - AHMEDABAD[0]) < 0.001

def test_search_takes_a_box(tmp_path):
    _shoot(tmp_path, [("p0.jpg", AHMEDABAD), ("p1.jpg", GOA)])
    c = TestClient(create_app(tmp_path))
    rows = c.get("/api/search", params={"bbox": "22,72,24,73"}).json()["results"]
    assert [r["rel"] for r in rows] == ["p0.jpg"]
    bad = c.get("/api/search", params={"bbox": "22,72,24"})
    assert bad.status_code == 400 and "south,west,north,east" in bad.json()["detail"]
    off = c.get("/api/search", params={"bbox": "22,72,200,73"})
    assert off.status_code == 400

def test_the_world_outline_and_the_city_list_ship_with_the_app():
    """The map draws with no tile server and no network call, so both files have to be in the package and
    have to be servable at the path the UI asks for."""
    ui = Path(__file__).resolve().parents[1] / "photosort" / "ui"
    w = json.loads((ui / "world.json").read_text())
    assert w["rings"] and w["scale"] and "Natural Earth" in w["source"]
    p = json.loads((ui / "places.json").read_text())
    assert len(p["places"]) > 5000 and any(row[0] == "Ahmedabad" for row in p["places"])

def test_the_ui_serves_the_map_assets(tmp_path):
    c = TestClient(create_app(tmp_path))
    assert c.get("/ui/world.json").status_code == 200
    assert c.get("/ui/places.json").status_code == 200
