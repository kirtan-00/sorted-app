"""The on-device usage log (photosort/usage.py): append, rotate, summary, the report zip, and that a
fixture holding a photo path and a person's name leaks neither."""
import json
import os
import sqlite3
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from photosort import usage
from photosort.config import app_home


def _lines():
    p = usage.events_path()
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_append_one_line_per_event_with_session_and_local_ts():
    sid = usage.start_session()
    assert usage.log("tab", tab="people") is not None
    usage.log("search", q="fishing boats at dusk", total=12, ms=30)
    rows = _lines()
    assert [r["ev"] for r in rows] == ["tab", "search"]
    assert all(r["session"] == sid for r in rows)
    assert "+" in rows[0]["ts"] or "-" in rows[0]["ts"][10:]      # local ISO with an offset
    assert rows[1]["q"] == "fishing boats at dusk" and rows[1]["q_len"] == 21
    assert usage.counters["search"] == 1 and usage.counters["tab"] == 1
    assert usage.events_path() == app_home() / "usage" / "events.jsonl"


def test_query_is_cut_to_60_chars_but_its_length_is_kept():
    usage.start_session()
    long_q = "a very long description of a shot " * 4
    usage.log("search", q=long_q, total=0, ms=1)
    r = _lines()[-1]
    assert len(r["q"]) == 60 and r["q_len"] == len(long_q) and r["q"] == long_q[:60]


def test_scrub_drops_paths_and_quoted_names_and_caps_length():
    assert usage.scrub("not a directory: /Volumes/SSD/NSG 26") == "not a directory: <path> 26"
    assert usage.scrub("no saved person called 'Dhrumil Shah'") == "no saved person called '?'"
    assert usage.scrub('bad "Ravi" here') == "bad '?' here"
    assert usage.scrub("~/Desktop/x.jpg is gone") == "<path> is gone"
    assert usage.scrub("categories/drone and sorted/ stay") == "categories/drone and sorted/ stay"
    assert len(usage.scrub("x" * 500)) == usage.FIELD_CHARS
    assert usage.scrub(12) == 12 and usage.scrub(None) is None


def test_log_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("PHOTOSORT_HOME", str(blocker))       # usage/ cannot be created under a file
    assert usage.log("tab", tab="search") is None


def test_rotate_keeps_two_older_files(monkeypatch):
    monkeypatch.setattr(usage, "ROTATE_BYTES", 200)
    usage.start_session()
    for i in range(30):
        usage.log("click", id="btn-" + str(i), pad="x" * 40)
    d = usage.usage_dir()
    names = sorted(p.name for p in d.iterdir())
    assert names == ["events.1.jsonl", "events.2.jsonl", "events.jsonl"]
    assert all((d / n).stat().st_size <= 400 for n in names)
    assert [p.name for p in usage.event_files()] == ["events.jsonl", "events.1.jsonl", "events.2.jsonl"]
    evs = usage.read_events()
    ids = [e["id"] for e in evs]
    assert ids == sorted(ids, key=lambda s: int(s.split("-")[1])) and ids[-1] == "btn-29"   # oldest first, newest last
    assert len(evs) < 30                                                                # the oldest were dropped


def test_summary_counts_sessions_queries_errors_and_seconds():
    usage.start_session()
    usage.log("server_start", app="abc1234")
    usage.log("search", q="beach", total=3, ms=500)
    usage.log("search", q="beach", total=3, ms=500)
    usage.log("search", q="boats", total=0, ms=250)
    usage.log("api_error", route="/api/folder", status=400, message="not a directory: <path>")
    usage.log("ui_error", message="TypeError: x is null", source="app.js", line=12)
    usage.log("index_done", items=100, indexed=90, seconds=12.5, error=None)
    usage.log("export_done", what="selection", dest="local", files=3, seconds=2.0, error="disk full")
    usage.log("tab_time", tab="people", seconds=30)
    usage.log("tab_time", tab="people", seconds=15.5)
    s1 = usage.summary()
    usage.start_session()
    usage.log("server_start", app="abc1234")
    s = usage.summary()
    assert s["sessions"] == 2 and s1["sessions"] == 1
    assert s["events"] == 11 and s["first_ts"] <= s["last_ts"]
    assert s["counters"]["search"] == 3 and s["counters"]["server_start"] == 2
    assert s["this_session"] == {"server_start": 1}
    assert s["top_queries"][:2] == [{"q": "beach", "n": 2}, {"q": "boats", "n": 1}]
    assert [e["ev"] for e in s["errors"]] == ["api_error", "ui_error", "export_done"]
    assert s["errors"][0]["status"] == 400 and s["errors"][0]["where"] == "/api/folder" and s["errors"][2]["error"] == "disk full"
    assert s["indexed_items"] == 90
    assert s["seconds_by_feature"]["tab:people"] == 45.5
    assert s["seconds_by_feature"]["indexing"] == 12.5 and s["seconds_by_feature"]["exporting"] == 2.0
    assert s["seconds_by_feature"]["searching"] == 1.2
    txt = usage.summary_text(s)
    assert "2 session(s), 11 events" in txt and "beach" in txt and "disk full" in txt and "tab:people" in txt
    assert "--" not in txt and "—" not in txt


@pytest.fixture
def leaky_app(tmp_path):
    """An indexed shoot whose photo name and whose saved person's name must never reach the log."""
    from conftest import make_image
    from photosort.index import index_folder
    from photosort.server import create_app
    from photosort import db
    make_image(tmp_path, "secretphotoname.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("INSERT INTO people(name, cover_face_id, n) VALUES(?, NULL, 1)", ("Dhrumil Shah",))
    conn.commit()
    pid = conn.execute("SELECT id FROM people").fetchone()[0]
    c = TestClient(create_app(tmp_path))
    # Things a tester does that carry names and paths through the server.
    assert c.get("/api/search", params={"q": "fishing boats on the beach " * 5, "kind": "photos"}).status_code == 200
    assert c.post("/api/folder", json={"path": "/Volumes/NoSuchDisk/secretfolder"}).status_code == 400
    assert c.post("/api/people/references/Dhrumil%20Shah/find", json={}).status_code == 404
    assert c.post(f"/api/people/{pid}/name", json={"name": "Dhrumil Shah"}).status_code == 200
    assert c.post("/api/people/find", json={"path": str(tmp_path / "secretphotoname.jpg")}).status_code in (200, 400)
    assert c.get("/api/thumb/nope").status_code == 404             # not logged: a missing thumb is grid noise
    assert c.get("/api/search").status_code == 200                  # the empty reload search is not logged either
    return c, tmp_path


def test_report_zip_contents_and_nothing_leaks(leaky_app, tmp_path):
    c, root = leaky_app
    events = usage.read_events()
    evs = [e["ev"] for e in events]
    assert evs[:2] == ["server_start", "folder_open"]
    assert evs.count("search") == 1 and "api_error" in evs and "rename" in evs
    err = [e for e in events if e["ev"] == "api_error"]
    assert {e["status"] for e in err} >= {400, 404}
    assert any(e["route"] == "/api/people/references/{name}/find" for e in err)
    assert not any(e.get("route", "").startswith("/api/thumb") for e in err)
    fo = next(e for e in events if e["ev"] == "folder_open")
    assert fo["shoot"] == root.name and fo["items"] == 1 and fo["indexed"] is True
    srch = next(e for e in events if e["ev"] == "search")
    assert srch["q_len"] == 135 and len(srch["q"]) == 60 and srch["filters"] == {"kind": "photos"} and srch["total"] == 1
    start = next(e for e in events if e["ev"] == "server_start")
    assert start["python"] and "app" in start and "macos" in start and "ffmpeg" in start

    dest = tmp_path / "reports"
    zpath = usage.write_report(dest)
    assert zpath.parent == dest and zpath.name.startswith("sorted-report-") and zpath.suffix == ".zip"
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        assert "usage/events.jsonl" in names and "summary.json" in names and "summary.txt" in names and "system.json" in names
        assert not any(n.endswith((".jpg", ".jpeg", ".db", ".png")) for n in names)
        blob = "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
        summ = json.loads(z.read("summary.json"))
    raw = usage.events_path().read_text()
    for text in (raw, blob):
        assert "Dhrumil" not in text, "a person's name leaked"
        assert "secretphotoname" not in text, "a photo path leaked"
        assert str(root) not in text and "secretfolder" not in text and "/Volumes" not in text, "a path leaked"
    assert summ["sessions"] == 1 and summ["counters"]["search"] == 1 and summ["top_queries"][0]["n"] == 1
    assert summ["errors"] and all("Dhrumil" not in json.dumps(e) for e in summ["errors"])
    # a second report the same day gets its own name
    z2 = usage.write_report(dest)
    assert z2 != zpath and z2.exists()


def test_report_includes_the_launcher_log_when_present(tmp_path, monkeypatch):
    log = tmp_path / "photosort.log"
    log.write_text("launcher started\n")
    monkeypatch.setenv("PHOTOSORT_LOG", str(log))
    usage.start_session(); usage.log("server_start")
    zpath = usage.write_report(tmp_path / "out")
    with zipfile.ZipFile(zpath) as z:
        assert "photosort.log" in z.namelist() and z.read("photosort.log") == b"launcher started\n"
