"""The local server against the web: DNS rebinding, cross-site POSTs, and the ordinary use that must keep working.
The first half talks to a real socket, because TestClient's host is allowed by conftest.py and because the port rule
needs a real port. The second half walks every route."""
import http.client
import re
import socket
import threading
import time
import pytest
import uvicorn
from fastapi.testclient import TestClient
from photosort.index import index_folder
from photosort.server import create_app, _origin_of


@pytest.fixture
def shoot(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    return tmp_path


@pytest.fixture
def live_port(shoot):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(shoot), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    yield port
    server.should_exit = True
    t.join(5)


def ask(port, method, path, **headers):
    """One request with exactly these headers. http.client, because a client library would fix the Host for us."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    headers.setdefault("Host", f"127.0.0.1:{port}")
    for k, v in headers.items():
        c.putheader(k.replace("_", "-"), v)
    c.endheaders()
    r = c.getresponse(); r.read(); c.close()
    return r.status


@pytest.mark.parametrize("path", ["/", "/ui/app.js", "/api/folder", "/api/media/1", "/api/search"])
def test_a_rebound_name_gets_nothing(live_port, path):
    assert ask(live_port, "GET", path) == 200                                       # the same path, asked properly
    assert ask(live_port, "GET", path, Host="evil.example.com") == 403
    assert ask(live_port, "GET", path, Host=f"evil.example.com:{live_port}") == 403


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
def test_each_local_name_works_on_the_serving_port_only(live_port, host):
    assert ask(live_port, "GET", "/api/folder", Host=f"{host}:{live_port}") == 200
    assert ask(live_port, "GET", "/api/folder", Host=f"{host}:{live_port + 1}") == 403
    assert ask(live_port, "GET", "/api/folder", Host=host) == 403                   # no port means 80


@pytest.mark.parametrize("host", ["localhost.evil.example.com", "127.0.0.1.nip.io", "evil@127.0.0.1", "127.0.0.1:1:2", "", "0.0.0.0"])
def test_lookalike_hosts_are_refused(live_port, host):
    h = host.replace(":1:2", f":{live_port}:2") if ":1:2" in host else (f"{host}:{live_port}" if host else "")
    assert ask(live_port, "GET", "/api/folder", Host=h) == 403


@pytest.mark.parametrize("path", ["/api/focus", "/api/drive/signout", "/api/project/save", "/api/reorganise/undo"])
def test_a_body_less_post_from_another_site_is_refused(live_port, path):
    assert ask(live_port, "POST", path, Origin="http://evil.example.com", Content_Length="0") == 403
    assert ask(live_port, "POST", path, Origin="null", Content_Length="0") == 403
    assert ask(live_port, "POST", path, Referer="http://evil.example.com/x", Content_Length="0") == 403
    # another port of this same Mac is another origin: a dev server on 3000 must not drive sorted either
    assert ask(live_port, "POST", path, Origin=f"http://127.0.0.1:{live_port + 1}", Content_Length="0") == 403
    assert ask(live_port, "POST", path, Origin=f"https://127.0.0.1:{live_port}", Content_Length="0") == 403


def test_a_refused_post_never_reaches_its_handler(shoot, monkeypatch):
    from photosort import drive
    calls = []
    monkeypatch.setattr(drive, "sign_out", lambda: calls.append(1))
    c = TestClient(create_app(shoot))
    assert c.post("/api/drive/signout", headers={"origin": "http://evil.example.com"}).status_code == 403
    assert calls == []
    assert c.post("/api/drive/signout", headers={"origin": "http://testserver"}).status_code == 200
    assert calls == [1]


def test_same_origin_use_passes(live_port):
    for host in (f"127.0.0.1:{live_port}", f"localhost:{live_port}"):
        me = f"http://{host}"
        assert ask(live_port, "POST", "/api/drive/signout", Host=host, Origin=me, Referer=me + "/ui/live.html?client=1",
                   Sec_Fetch_Site="same-origin", Content_Length="0") == 200
    # a page on 127.0.0.1 posting to localhost is cross-origin in the browser's eyes, and here
    assert ask(live_port, "POST", "/api/drive/signout", Host=f"localhost:{live_port}", Origin=f"http://127.0.0.1:{live_port}",
               Content_Length="0") == 403


def test_the_launcher_and_curl_send_no_origin_and_pass(live_port):
    assert ask(live_port, "POST", "/api/drive/signout", Content_Length="0") == 200


def test_page_loads_and_images_keep_working(live_port):
    me = f"http://127.0.0.1:{live_port}"
    same = dict(Sec_Fetch_Site="same-origin", Referer=me + "/")
    for path in ("/", "/ui/live.html", "/ui/live.html?client=1", "/ui/live.js", "/ui/live.css", "/ui/app.js", "/ui/style.css"):
        assert ask(live_port, "GET", path, **same) == 200, path
    assert ask(live_port, "GET", "/api/media/1", **same) == 200
    # the address bar, or `open http://127.0.0.1:7777` from the launcher
    assert ask(live_port, "GET", "/", Sec_Fetch_Site="none") == 200
    # a link to the app from a mail or a doc opens the app; only /api/ is closed to other sites
    assert ask(live_port, "GET", "/", Sec_Fetch_Site="cross-site") == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_another_site_cannot_embed_an_original_or_consume_the_launch(live_port, site):
    assert ask(live_port, "GET", "/api/media/1", Sec_Fetch_Site=site) == 403
    assert ask(live_port, "GET", "/api/launch", Sec_Fetch_Site=site) == 403


def routes(app):
    """(method, path) of everything the app serves, /api/live/* included: openapi sees through included routers."""
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            yield method.upper(), re.sub(r"\{[^}]*\}", "1", path)


def test_every_state_changing_route_refuses_another_site(shoot):
    """Walks the app rather than a list, so a route added later is covered the day it is added."""
    c = TestClient(create_app(shoot), raise_server_exceptions=False)
    changing = [(m, p) for m, p in routes(c.app) if m not in ("GET", "HEAD", "OPTIONS")]
    assert len(changing) > 60 and any(p.startswith("/api/live/") for _, p in changing)
    for method, path in changing:
        got = c.request(method, path, headers={"origin": "http://evil.example.com"})
        assert got.status_code == 403, (method, path, got.status_code)


def test_every_route_refuses_a_rebound_host(shoot):
    c = TestClient(create_app(shoot), raise_server_exceptions=False)
    for method, path in routes(c.app):
        assert c.request(method, path, headers={"host": "evil.example.com"}).status_code == 403, (method, path)


def test_the_drive_sign_in_redirect_does_not_come_back_through_this_server(shoot):
    """Google redirects to the small server google_auth_oauthlib opens on a port of its own (port=0), so the
    middleware never sees it. If that ever moves onto this app, this test is the reminder to look at LocalOnly."""
    import inspect
    from photosort import drive
    assert "run_local_server(port=0" in inspect.getsource(drive)
    paths = list(create_app(shoot).openapi()["paths"])
    assert not [p for p in paths if "oauth" in p or "callback" in p]


def test_a_websocket_from_another_site_is_refused(shoot):
    from starlette.websockets import WebSocket, WebSocketDisconnect
    app = create_app(shoot)

    @app.websocket("/ws-probe")
    async def probe(ws: WebSocket):
        await ws.accept(); await ws.close()
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws-probe", headers={"origin": "http://evil.example.com"}):
            pass
    with c.websocket_connect("/ws-probe", headers={"origin": "http://testserver"}):
        pass


@pytest.mark.parametrize("url,want", [("null", None), ("http://[bad", None), ("http://a:b@127.0.0.1:7777", None),
                                      ("http://127.0.0.1:7777/ui/x?y=1", ("http", "127.0.0.1", 7777)),
                                      ("http://[::1]:7777", ("http", "[::1]", 7777)), ("http://localhost", ("http", "localhost", 80))])
def test_origin_parsing(url, want):
    assert _origin_of(url) == want
