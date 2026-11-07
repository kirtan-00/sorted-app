"""sorted.app/Contents/MacOS/sorted, run for real against a stand-in server: a click must never end live work.
The stand-in is a python script sitting where .venv/bin/python goes, so the launcher's ps match and curl see what
they would see in the installed app, without loading the real server."""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from photosort.server import create_app

LAUNCHER = Path(__file__).resolve().parent.parent / "sorted.app" / "Contents" / "MacOS" / "sorted"
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the launcher is a Mac script")

STANDIN = f"""#!INTERPRETER
import http.server, json, os, sys, time
repo = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        open(os.path.join(repo, "handed.txt"), "w").write(self.path)
        self.send_response(200); self.send_header("content-length", "2"); self.end_headers(); self.wfile.write(b"{{}}")
    def do_GET(self):
        mode = open(os.path.join(repo, "mode.txt")).read().strip()
        if mode == "hang":
            time.sleep(60)
        body = json.dumps({{"app": "sorted", "repo": repo, "pid": os.getpid(), "busy": "busy" in mode, "stale": "stale" in mode}},
                          separators=(",", ":")).encode()
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
s = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
print(f"photosort serving (no folder) at http://127.0.0.1:{{port}}", flush=True)
s.serve_forever()
"""


class App:
    def __init__(self, base: Path):
        self.repo = base / "repo"
        macos = self.repo / "sorted.app" / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        shutil.copy(LAUNCHER, macos / "sorted")
        self.script = macos / "sorted"
        py = self.repo / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        link = base / "interp"                         # a shebang cannot hold the space in this checkout's path
        link.symlink_to(sys.executable)
        py.write_text(STANDIN.replace("INTERPRETER", str(link)))
        py.chmod(py.stat().st_mode | stat.S_IEXEC)
        self.home = base / "home"
        (self.home / "Library" / "Logs").mkdir(parents=True)
        self.log = self.home / "Library" / "Logs" / "photosort.log"
        self.log.touch()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.mode("ok")
        self.procs = []

    def mode(self, m):
        (self.repo / "mode.txt").write_text(m)

    def click(self, *args):
        env = dict(os.environ, HOME=str(self.home), SORTED_NO_OPEN="1", SORTED_PORT=str(self.port))
        p = subprocess.Popen(["/bin/bash", str(self.script), *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def server_pid(self, timeout=15):
        import urllib.request
        end = time.time() + timeout
        while time.time() < end:
            try:
                return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1))["pid"]
            except Exception:
                time.sleep(0.1)
        pytest.fail("no server came up")

    def stop(self):
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if str(self.repo) in line and "serve" in line:
                try:
                    os.kill(int(line.split()[0]), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        for p in self.procs:
            p.wait(10)


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path)
    yield a
    a.stop()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_a_second_click_on_a_healthy_server_opens_it_and_leaves_it_running(app):
    app.click()
    pid = app.server_pid()
    assert app.click().wait(15) == 0
    assert app.server_pid() == pid
    assert f"already running at http://127.0.0.1:{app.port}" in app.log.read_text()


def test_a_click_during_a_scan_leaves_it_running_even_after_an_update(app):
    app.click()
    pid = app.server_pid()
    app.mode("busy stale")
    assert app.click().wait(15) == 0
    assert app.server_pid() == pid and alive(pid)


def test_an_idle_server_from_before_an_update_is_replaced(app):
    app.click()
    pid = app.server_pid()
    app.mode("stale")
    app.click()
    deadline = time.time() + 15
    while time.time() < deadline and "this copy was updated" not in app.log.read_text():
        time.sleep(0.1)
    app.mode("ok")
    for _ in range(100):
        if app.server_pid() != pid:
            break
        time.sleep(0.1)
    assert app.server_pid() != pid


def test_a_server_that_will_not_answer_is_replaced(app):
    app.click()
    pid = app.server_pid()
    app.mode("hang")
    app.click()
    deadline = time.time() + 90                        # five tries of a 2 s curl, slower when the Mac is busy
    while time.time() < deadline and "does not answer" not in app.log.read_text():
        time.sleep(0.2)
    assert "does not answer" in app.log.read_text()
    app.mode("ok")
    assert app.server_pid(30) != pid


def test_a_project_file_is_handed_to_the_running_server(app, tmp_path):
    app.click()
    pid = app.server_pid()
    f = tmp_path / "my shoot.sorted"; f.write_text("x")
    assert app.click(str(f)).wait(15) == 0
    assert app.server_pid() == pid
    assert "my%20shoot.sorted" in (app.repo / "handed.txt").read_text() or "my+shoot.sorted" in (app.repo / "handed.txt").read_text()


def test_another_copys_server_on_the_port_is_not_touched(app, tmp_path):
    """A server of another checkout answers health with its own repo. This copy starts on the next port instead."""
    other = App(tmp_path / "other")
    other.port = app.port
    try:
        other.click()
        pid = other.server_pid()
        app.click()
        for _ in range(100):
            if f"http://127.0.0.1:{app.port + 1}" in (app.log.read_text()):
                break
            time.sleep(0.1)
        assert other.server_pid() == pid
    finally:
        other.stop()


def test_health_says_which_copy_and_whether_it_is_busy(tmp_path, monkeypatch):
    from photosort import version
    from photosort.config import ROOT
    c = TestClient(create_app(None))
    h = c.get("/api/health").json()
    assert h["app"] == "sorted" and h["repo"] == str(ROOT) and h["busy"] is False and h["stale"] is False
    c.app.state.photosort["running"] = True
    assert c.get("/api/health").json()["busy"] is True
    c.app.state.photosort["running"] = False
    c.app.state.photosort["export"]["running"] = True
    assert c.get("/api/health").json()["busy"] is True
    c.app.state.photosort["export"]["running"] = False
    monkeypatch.setattr(version, "stamp", lambda: ("9.9.9", ""))
    assert c.get("/api/health").json()["stale"] is True


def test_health_format_is_what_the_launcher_greps_for():
    r = TestClient(create_app(None)).get("/api/health")
    assert '"stale":false' in r.text and '"busy":' in r.text and '"repo":"' in r.text


def test_health_answers_while_every_worker_thread_is_busy(tmp_path):
    """Not answering is what gets a server killed, so a threadpool full of slow requests must not look like a hang."""
    import asyncio, httpx, threading
    app = create_app(None)
    gate = threading.Event()

    @app.get("/slow")
    def slow():
        gate.wait(10)
        return {}

    async def go():
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://testserver") as c:
            jobs = [asyncio.create_task(c.get("/slow")) for _ in range(60)]
            await asyncio.sleep(0.3)
            t0 = time.time()
            r = await c.get("/api/health")
            took = time.time() - t0
            gate.set()
            await asyncio.gather(*jobs)
            return r.status_code, took
    code, took = asyncio.run(go())
    assert code == 200 and took < 1


def test_the_handed_over_file_is_collected_once(tmp_path):
    f = tmp_path / "s.sorted"; f.write_text("x")
    c = TestClient(create_app(None))
    assert c.post("/api/launch", params={"path": str(f)}).status_code == 200
    assert c.get("/api/launch").json() == {"open": str(f)}
    assert c.get("/api/launch").json() == {"open": None}
    assert c.post("/api/launch", params={"path": str(tmp_path / "no")}).status_code == 400
