"""Keep the Mac awake while a long job runs: one `caffeinate -i` child held for as long as any job is
running, released when the last one ends. `-w <our pid>` means the child also dies on its own if the
server is killed, so a caffeinate is never left behind. On a machine without caffeinate (or a test that
points AWAKE_CMD elsewhere) nothing breaks: held() just says False."""
from __future__ import annotations
import os
import subprocess
import threading

# Replaced in tests with a fake that sleeps; the pid placeholder is filled in at start.
AWAKE_CMD: list[str] = ["caffeinate", "-i", "-w", "{pid}"]

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_holders = 0


def _start() -> subprocess.Popen | None:
    cmd = [a.replace("{pid}", str(os.getpid())) for a in AWAKE_CMD]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _stop(p: subprocess.Popen | None) -> None:
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=5)


def acquire() -> None:
    """One more job wants the Mac awake. The first acquire starts caffeinate."""
    global _proc, _holders
    with _lock:
        _holders += 1
        if _holders == 1 or (_proc is not None and _proc.poll() is not None):
            _stop(_proc)
            _proc = _start()


def release() -> None:
    """One job ended. The last release stops caffeinate."""
    global _proc, _holders
    with _lock:
        _holders = max(0, _holders - 1)
        if _holders == 0:
            _stop(_proc)
            _proc = None


def held() -> bool:
    """True while caffeinate is running for at least one job."""
    with _lock:
        return _holders > 0 and _proc is not None and _proc.poll() is None


def pid() -> int | None:
    with _lock:
        return _proc.pid if _proc is not None and _proc.poll() is None else None


class hold:
    """`with awake.hold():` around a job's body."""
    def __enter__(self):
        acquire()
        return self

    def __exit__(self, *exc):
        release()
        return False
