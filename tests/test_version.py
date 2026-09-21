"""The app's version (VERSION plus the BUILD stamp or git's sha) and the opt-in update check."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from photosort import version, settings
from photosort.server import create_app


def test_version_string_reads_version_and_build(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("0.3.0\n")
    monkeypatch.setattr(version, "VERSION_FILE", tmp_path / "VERSION")
    monkeypatch.setattr(version, "BUILD_FILE", tmp_path / "BUILD")
    (tmp_path / "BUILD").write_text("abc1234\n")
    assert version.app_version() == "0.3.0" and version.app_build() == "abc1234" and version.version_string() == "0.3.0 (abc1234)"
    (tmp_path / "BUILD").unlink()
    monkeypatch.setattr(version.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no git")))
    assert version.app_build() is None and version.version_string() == "0.3.0"
    monkeypatch.setattr(version, "VERSION_FILE", tmp_path / "missing")
    assert version.app_version() == "0.0.0"


def test_repo_has_version_file_and_it_matches_pyproject_and_the_site():
    root = Path(version.__file__).resolve().parent.parent
    v = (root / "VERSION").read_text().strip()
    assert v == version.app_version()
    assert f'version = "{v}"' in (root / "pyproject.toml").read_text()
    assert json.loads((root / "site" / "version.json").read_text())["version"] == v


def test_is_newer():
    assert version.is_newer("0.3.1", "0.3.0") and version.is_newer("1.0", "0.9.9") and version.is_newer("0.3.0.1", "0.3.0")
    assert not version.is_newer("0.3.0", "0.3.0") and not version.is_newer("0.2.9", "0.3.0") and not version.is_newer("", "0.3.0")
    assert version.is_newer("0.4.0-beta", "0.3.0")


def test_update_check_is_off_by_default_and_fetches_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(version, "fetch_latest", lambda timeout=6: calls.append(1) or {"version": "9.9.9", "notes": "x"})
    c = TestClient(create_app(None))
    st = c.get("/api/version").json()
    assert st["enabled"] is False and st["newer"] is False and st["latest"] is None and st["checking"] is False and st["version"] == version.app_version()
    assert st["download_url"].startswith("https://kirtan-00.github.io/sorted/")
    assert calls == []                                            # off: no request, at boot or on the call


def test_update_check_opt_in_finds_a_newer_version_once_a_day(monkeypatch, tmp_path):
    site = tmp_path / "version.json"
    site.write_text(json.dumps({"version": "0.9.0", "notes": "the notes"}))
    monkeypatch.setenv("SORTED_UPDATE_URL", "file://" + str(site))
    c = TestClient(create_app(None))
    st = c.post("/api/version/check", json={"enabled": True}).json()
    assert st["enabled"] is True and st["latest"] == "0.9.0" and st["notes"] == "the notes" and st["newer"] is True and st["checked_at"]
    assert settings.get_update_prefs()["latest"] == "0.9.0"
    # not due again for a day: the file changes, the answer does not
    site.write_text(json.dumps({"version": "1.0.0", "notes": "later"}))
    assert version.due() is False
    st2 = c.get("/api/version").json()
    assert st2["latest"] == "0.9.0" and st2["checking"] is False
    # a day later it is due: the background check runs and the next call has the answer
    old = settings.load(); old["update_checked_at"] = "2020-01-01T00:00:00Z"; settings.save(old)
    assert version.due() is True
    st3 = c.get("/api/version").json()
    assert st3["checking"] is True or st3["latest"] == "1.0.0"
    for _ in range(100):
        st4 = c.get("/api/version").json()
        if st4["latest"] == "1.0.0": break
        import time; time.sleep(0.02)
    assert st4["latest"] == "1.0.0" and st4["newer"] is True
    # the same version is not "newer"; off forgets everything
    site.write_text(json.dumps({"version": version.app_version(), "notes": ""}))
    assert c.post("/api/version/check", json={"enabled": True}).json()["newer"] is False
    st5 = c.post("/api/version/check", json={"enabled": False}).json()
    assert st5["enabled"] is False and st5["latest"] is None and settings.load().get("update_check") is None


def test_update_check_stays_quiet_on_a_network_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SORTED_UPDATE_URL", "file://" + str(tmp_path / "nope.json"))
    c = TestClient(create_app(None))
    st = c.post("/api/version/check", json={"enabled": True}).json()
    assert st["enabled"] is True and st["latest"] is None and st["newer"] is False and "FileNotFoundError" in st["error"] and st["checked_at"]
    # a bad file is an error too, and the last good answer would have stayed
    (tmp_path / "nope.json").write_text("{\"notes\": 1}")
    settings.set_update_result("0.5.0", "old", "2020-01-01T00:00:00Z", None)
    st2 = version.check_now()
    assert st2["latest"] == "0.5.0" and "ValueError" in st2["error"]


def test_feedback_summary_carries_the_version(tmp_path):
    from photosort import usage
    s = usage.summary()
    assert s["system"]["app"] == version.version_string()
    assert ("app " + version.version_string()) in usage.summary_text(s)
