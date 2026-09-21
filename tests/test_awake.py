"""The sleep guard (photosort/awake.py), tested with a fake caffeinate: a python child that sleeps
and writes the pid it was told to wait on, so the real caffeinate is never started here."""
import os
import sys
import time
import pytest
from photosort import awake


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def fake_caffeinate(tmp_path, monkeypatch):
    out = tmp_path / "args.txt"
    script = f"import sys, time, pathlib; pathlib.Path({str(out)!r}).write_text(' '.join(sys.argv[1:])); time.sleep(60)"
    monkeypatch.setattr(awake, "AWAKE_CMD", [sys.executable, "-c", script, "-i", "-w", "{pid}"])
    # a clean slate whatever an earlier test left
    monkeypatch.setattr(awake, "_proc", None)
    monkeypatch.setattr(awake, "_holders", 0)
    return out


def test_hold_starts_one_child_and_kills_it_when_the_last_job_ends(fake_caffeinate):
    assert awake.held() is False and awake.pid() is None
    with awake.hold():
        pid = awake.pid()
        assert awake.held() is True and pid and _alive(pid)
        for _ in range(100):                      # the child writes its args as it starts
            if fake_caffeinate.is_file(): break
            time.sleep(0.02)
        assert fake_caffeinate.read_text() == f"-i -w {os.getpid()}"   # told to die with the server, never left behind
        with awake.hold():                        # a second job shares the one child
            assert awake.pid() == pid
        assert awake.held() is True and awake.pid() == pid   # the first job is still running
    assert awake.held() is False and awake.pid() is None
    for _ in range(100):
        if not _alive(pid): break
        time.sleep(0.02)
    assert not _alive(pid)


def test_release_after_an_exception_still_stops_the_child(fake_caffeinate):
    with pytest.raises(RuntimeError):
        with awake.hold():
            pid = awake.pid()
            assert _alive(pid)
            raise RuntimeError("job blew up")
    assert awake.held() is False
    for _ in range(100):
        if not _alive(pid): break
        time.sleep(0.02)
    assert not _alive(pid)


def test_missing_caffeinate_is_a_quiet_no_op(monkeypatch):
    monkeypatch.setattr(awake, "AWAKE_CMD", ["/nonexistent/caffeinate", "-i", "-w", "{pid}"])
    monkeypatch.setattr(awake, "_proc", None)
    monkeypatch.setattr(awake, "_holders", 0)
    with awake.hold():
        assert awake.held() is False and awake.pid() is None
    assert awake.held() is False
