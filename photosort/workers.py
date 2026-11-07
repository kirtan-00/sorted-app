"""Worker processes that survive what a bad file does to them.

multiprocessing.Pool loses the task when a worker dies: it replaces the process and waits for a result that will
never come, which hung a scan at 118 of 120 for good. A decoder fed a damaged RAW can segfault, and nothing in Python
catches that. So supervised_map hands each worker one file at a time and keeps track of which file each one holds.
A worker that dies, or holds one file past its limit, is then named with that file: the file is reported as a result
with a reason, a new worker takes the place, and the scan goes on.

One file at a time costs one pipe round trip per file, under 0.1 ms against tens of milliseconds of decoding. The next
file goes out before the last result is handed to the caller, so workers keep reading while the caller writes to the
index. Measured on 240 JPEGs of 3000x2000, full scan with faces and embeddings off, median of three, alternating
runs on the M1: Pool 4.36 s, this 3.78 s, so no cost and within the noise."""
from __future__ import annotations
import multiprocessing as mp
import os
import signal
import time
from collections import deque
from multiprocessing.connection import wait
from typing import Callable, Iterable, Iterator

STARTUP_S = 120          # a worker that has not said ready by now is not going to; importing cold takes a few seconds
START_FAILURES_MAX = 3   # workers in a row that died before reading anything: the app is broken, not a file
POLL_S = 1.0
JOIN_S = 2.0             # a process stuck on a dead disk cannot be reaped; never wait on it for longer than this


def _worker(conn, fn: Callable) -> None:
    # Its own process group, so a watchdog kill takes the ffmpeg under it as well.
    try:
        os.setpgrp()
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)      # Ctrl-C belongs to the parent, which stops the workers itself
    conn.send(("ready", None))
    while True:
        try:
            task = conn.recv()
        except (EOFError, OSError):
            return                                    # the parent is gone: nothing to read for
        if task is None:
            return
        conn.send(("done", fn(task)))


def why_it_died(exitcode: int | None) -> str:
    """In a person's words, for the list of files that could not be read."""
    if exitcode is not None and exitcode < 0:
        try:
            name = signal.Signals(-exitcode).name
        except ValueError:
            name = f"signal {-exitcode}"
        if name == "SIGKILL":
            return "the reader was stopped by the Mac on this file, which usually means it ran out of memory"
        return f"this file crashed the reader ({name})"
    return f"the reader quit on this file (exit code {exitcode})"


def _limit_words(seconds: float) -> str:
    return f"{seconds / 60:g} minutes" if seconds >= 120 else f"{seconds:g} seconds"


class _Slot:
    def __init__(self, ctx, fn: Callable):
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker, args=(child, fn), daemon=True)
        self.proc.start()
        child.close()
        self.ready = False
        self.task = None
        self.limit = 0.0
        self.deadline = time.monotonic() + STARTUP_S

    def give(self, task, limit: float) -> None:
        self.task, self.limit, self.deadline = task, limit, time.monotonic() + limit
        self.conn.send(task)

    def kill(self) -> None:
        for how in (lambda: os.killpg(self.proc.pid, signal.SIGKILL), self.proc.kill):
            try:
                how()
                break
            except (OSError, ValueError, AttributeError):
                pass
        self.reap()

    def reap(self) -> None:
        self.proc.join(JOIN_S)
        try:
            self.conn.close()
        except OSError:
            pass


def supervised_map(fn: Callable, tasks: Iterable, workers: int, limit: Callable[[object], float],
                   lost: Callable[[object, str], dict]) -> Iterator[dict]:
    """fn over tasks in `workers` spawned processes, results in the order they finish, exactly one per task.
    limit(task) is that task's watchdog in seconds. lost(task, reason) makes the result for a task whose worker
    died or ran out of time; it should carry the reason under "quarantine". Stopping the iteration early stops
    the workers. Raises RuntimeError when workers cannot start at all, which no file is to blame for."""
    ctx = mp.get_context("spawn")
    queue = deque(tasks)
    workers = max(1, min(workers, len(queue)))
    slots = [_Slot(ctx, fn) for _ in range(workers)] if queue else []
    out: deque[dict] = deque()
    failed_starts = 0

    def replace(slot: _Slot) -> None:
        nonlocal failed_starts
        slots.remove(slot)
        if not slot.ready:
            failed_starts += 1
            if failed_starts >= START_FAILURES_MAX:
                raise RuntimeError(f"the file reader would not start (exit code {slot.proc.exitcode})")
        if queue and len(slots) < workers:
            slots.append(_Slot(ctx, fn))

    try:
        while queue or any(s.task is not None for s in slots) or out:
            for s in slots:
                if s.ready and s.task is None and queue:
                    task = queue.popleft()
                    try:
                        s.give(task, limit(task))
                    except (BrokenPipeError, OSError):   # it died idle: the task never reached it, so it goes back
                        queue.appendleft(task); s.task, s.deadline = None, float("inf")
            if out:                                      # after the next files went out, so workers read while this writes
                yield out.popleft()
                continue
            if not (queue or any(s.task is not None for s in slots)):
                continue
            now = time.monotonic()
            soonest = min((s.deadline for s in slots), default=now + POLL_S)
            ready = set(wait([s.conn for s in slots] + [s.proc.sentinel for s in slots],
                             timeout=max(0.0, min(POLL_S, soonest - now))))
            for s in list(slots):
                if s.conn in ready:
                    try:
                        kind, res = s.conn.recv()
                    except (EOFError, OSError):
                        kind = None                       # dead, or died halfway through a result
                    if kind == "ready":
                        s.ready, s.deadline = True, float("inf")
                        failed_starts = 0
                        continue
                    if kind == "done":
                        s.task, s.deadline = None, float("inf")
                        out.append(res)
                        continue
                elif s.proc.sentinel not in ready:
                    if time.monotonic() < s.deadline:
                        continue
                    task, spent = s.task, s.limit         # past its limit, and still alive
                    s.kill()
                    if task is not None:
                        out.append(lost(task, f"still not read after {_limit_words(spent)}, so it was set aside"))
                    s.ready = s.ready or task is not None
                    replace(s)
                    continue
                task = s.task                             # the worker is gone
                s.reap()
                if task is not None:
                    out.append(lost(task, why_it_died(s.proc.exitcode)))
                    s.ready = True                        # it got as far as a file: this is the file's doing
                replace(s)
    finally:
        for s in slots:
            if s.task is None:
                try:
                    s.conn.send(None)
                except OSError:
                    pass
        for s in slots:
            if s.task is not None:
                s.kill()
            else:
                s.proc.join(JOIN_S)
                if s.proc.is_alive():
                    s.kill()
                else:
                    s.reap()
