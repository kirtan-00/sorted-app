"""Picklable stand-ins for index.process_one and faces_one. A spawned worker imports these by name, so the fault lives
in the worker and nowhere else. Not a test module: pytest does not collect it, tests/test_workers.py uses it."""
import os
import signal
import subprocess
import sys
import time
from photosort import index


def fault_of(rel: str) -> None:
    if "exit" in rel:
        os._exit(9)
    if "segv" in rel:
        os.kill(os.getpid(), signal.SIGSEGV)
    if "hang" in rel:
        time.sleep(600)
    if "child" in rel:                 # a hung ffmpeg stands in here: the watchdog has to take it down too
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(os.environ["FAULT_PID_FILE"], "w") as f:
            f.write(str(p.pid))
        p.wait()


def process_one(args):
    fault_of(args[1])
    return index.process_one(args)


def faces_one(args):
    fault_of(args[1])
    return index.faces_one(args)


def echo(task):
    fault_of(task[1])
    return {"rel": task[1], "error": None}


def die_at_start(conn, fn):
    os._exit(3)


def lost(task, reason):
    return {"rel": task[1], "error": reason, "quarantine": reason}


def full_disk_one(args):
    """What process_one does when the thumb will not fit, reached the honest way: a real ENOSPC from the save."""
    if "full" in args[1]:
        real = index.Image.Image.save
        def save(self, *a, **k):
            raise OSError(28, "No space left on device")
        index.Image.Image.save = save
        try:
            return index.process_one(args)
        finally:
            index.Image.Image.save = real
    return index.process_one(args)
