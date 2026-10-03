"""The suite must never write into the real index folder.

~/Library/Application Support/photosort is where a person's shoots live, client work among them. Two
tests start a background job and return without waiting for it, so that thread runs after monkeypatch has
restored the environment; with nothing underneath, app_home() then resolved to the real folder and left an
index dir behind. It had done that 105 times by 2026-09-24, two more on every suite run. pytest_configure
in conftest.py puts a session temp dir under the per-test isolation so there is nothing to restore TO.
"""
import os
from pathlib import Path
import photosort.config as config

REAL_HOME = Path.home() / "Library" / "Application Support" / "photosort"

def test_photosort_home_is_set_before_any_test_runs():
    """The floor is pytest_configure's, not the per-test fixture's: this asserts the variable is set at
    all, which is what a thread outliving its test falls back on."""
    assert os.environ.get("PHOTOSORT_HOME"), "PHOTOSORT_HOME unset; a late thread would write to the real folder"

def test_app_home_is_never_the_real_index_folder():
    """The assertion that actually protects the shoots. Resolved, so a symlinked temp dir cannot sneak past."""
    assert config.app_home().resolve() != REAL_HOME.resolve()

def test_app_home_survives_the_per_test_fixture_being_undone(monkeypatch):
    """What the two failure tests do: the environment is restored mid flight. Restoring has to land on the
    session temp dir, never on unset, or app_home() falls back to the real folder again."""
    monkeypatch.setenv("PHOTOSORT_HOME", "/tmp/photosort-inner")
    assert config.app_home() == Path("/tmp/photosort-inner")
    monkeypatch.undo()
    assert os.environ.get("PHOTOSORT_HOME")
    assert config.app_home().resolve() != REAL_HOME.resolve()
