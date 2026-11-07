"""The camera receivers, tested the way a body uses them: python's ftplib against the real server on a real socket.

No manual documents the commands a camera sends, so the sequences here are built from what the manuals say a body does
(Sony Same as in Camera, Canon's automatic target folder, a re-send under a new name), not copied from a log.
"""
import ftplib, hashlib, json, os, shutil, socket, struct, subprocess, sys, threading, time
from pathlib import Path
import pytest
from photosort import tether
from photosort.tether import FtpReceiver, Lander, STAGE_DIR, UsbCore, UsbReceiver, looks_complete, parse_ifconfig

FIXTURES = Path(__file__).parent / "fixtures" / "exif"
MB = 1024 * 1024


def frame(size: int = 30 * MB, seed: int = 1) -> bytes:
    """A JPEG in shape: right at both ends, so the receiver can tell it from one cut short."""
    block = hashlib.sha256(str(seed).encode()).digest() * 2048
    body = (block * (size // len(block) + 1))[:size - 4]
    return b"\xff\xd8" + body.replace(b"\xff\xd9", b"\xff\x00") + b"\xff\xd9"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Pipe:
    """A file object feeding storbinary, for cutting an upload at an exact byte."""

    def __init__(self, data: bytes, stop_at: int | None = None, then=None):
        self.data, self.at, self.stop_at, self.then = data, 0, stop_at, then

    def read(self, n: int) -> bytes:
        if self.stop_at is not None and self.at >= self.stop_at:
            if self.then:
                self.then()
            return b""
        end = self.at + n if self.stop_at is None else min(self.at + n, self.stop_at)
        out = self.data[self.at:end]
        self.at = end
        return out


@pytest.fixture
def shoot(tmp_path):
    d = tmp_path / "shoot"
    d.mkdir()
    return d


@pytest.fixture
def srv(shoot):
    events: list = []
    r = FtpReceiver(shoot, host="127.0.0.1", port=0, on_event=lambda kind, **data: events.append((kind, data)))
    r.start()
    r.events = events
    yield r
    r.stop(drain_s=0)


def body(r: FtpReceiver, login: bool = True) -> ftplib.FTP:
    f = ftplib.FTP()
    f.connect("127.0.0.1", r.port, timeout=30)
    f.set_pasv(True)
    if login:
        f.login(r.user, r.password)
    return f


def photos(root: Path) -> dict[str, bytes]:
    out = {}
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for n in names:
            if not n.startswith("."):
                p = Path(d) / n
                out[str(p.relative_to(root))] = p.read_bytes()
    return out


def staged(root: Path) -> list[Path]:
    return sorted((root / STAGE_DIR).glob("*.part"))


def wait(test, s: float = 5.0) -> None:
    end = time.monotonic() + s
    while time.monotonic() < end:
        if test():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


# ===== login =====

def test_each_session_gets_its_own_login_and_the_menu_has_what_a_camera_needs(shoot):
    a, b = FtpReceiver(shoot, host="127.0.0.1", port=0), FtpReceiver(shoot, host="127.0.0.1", port=0)
    m = a.start()
    try:
        assert {"address", "port", "user", "password", "passive"} <= set(m)
        assert m["address"] == "127.0.0.1" and m["port"] > 0 and m["passive"] is True
        assert (a.user, a.password) != (b.user, b.password)
        assert len(a.password) == tether.PASSWORD_LEN and set(a.password) <= set(tether.ALPHABET)
    finally:
        a.stop(drain_s=0)


def test_wrong_password_and_anonymous_get_nothing(srv, shoot):
    for user, pw in ((srv.user, "wrong"), ("anonymous", "cam@example.com"), ("ftp", ""), ("", srv.password)):
        f = body(srv, login=False)
        with pytest.raises(ftplib.error_perm) as e:
            f.login(user, pw)
        assert str(e.value).startswith("530")
        f.close()
    assert srv.status()["refused_logins"] == 4
    assert not srv.status()["bodies"]


def test_nothing_works_before_login(srv):
    f = body(srv, login=False)
    for cmd in ("PWD", "CWD /", "MKD x", "PASV", "SIZE x", "STOR x", "LIST", "NLST"):
        with pytest.raises(ftplib.error_perm) as e:
            f.sendcmd(cmd)
        assert str(e.value).startswith("530"), cmd
    f.close()


def test_a_guesser_is_locked_out_and_the_real_password_waits_too(srv, monkeypatch):
    monkeypatch.setattr(tether, "LOGIN_DELAY_S", 0)
    monkeypatch.setattr(tether, "LOCK_AFTER", 4)
    for _ in range(4):
        f = body(srv, login=False)
        with pytest.raises(ftplib.error_perm):
            f.login(srv.user, "nope")
        f.close()
    f = body(srv, login=False)
    with pytest.raises(ftplib.error_perm):
        f.login(srv.user, srv.password)
    f.close()


def test_the_password_never_reaches_the_command_log(srv):
    f = body(srv)
    f.quit()
    text = "\n".join(srv.command_log())
    assert srv.password not in text and "PASS ***" in text and f"USER {srv.user}" in text


# ===== what bodies do =====

def test_sony_standard_a_30mb_frame_lands_whole(srv, shoot):
    data = frame()
    f = body(srv)
    f.voidcmd("TYPE I")
    assert f.pwd() == "/"
    f.storbinary("STOR DSC00001.JPG", Pipe(data))
    f.quit()
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    assert srv.status()["landed"] == 1 and srv.status()["landed_bytes"] == len(data)
    assert not staged(shoot)
    assert ("landed", {"who": "127.0.0.1", "name": "DSC00001.JPG", "bytes": len(data)}) in srv.events


def test_sony_same_as_in_camera_walks_down_and_back_up(srv, shoot):
    f = body(srv)
    f.voidcmd("TYPE I")
    for d in ("A", "DCIM", "100MSDCF"):
        with pytest.raises(ftplib.error_perm):
            f.cwd(d)
        f.mkd(d)
        f.cwd(d)
    assert f.pwd() == "/A/DCIM/100MSDCF"
    f.storbinary("STOR DSC00001.JPG", Pipe(frame(MB)))
    f.cwd("..")
    f.cwd("/")
    f.storbinary("STOR A/DCIM/100MSDCF/DSC00002.JPG", Pipe(frame(MB, 2)))
    f.quit()
    assert set(photos(shoot)) == {"A/DCIM/100MSDCF/DSC00001.JPG", "A/DCIM/100MSDCF/DSC00002.JPG"}


def test_canon_makes_its_target_folder_and_checks_before_it_sends(srv, shoot):
    data = frame(2 * MB)
    f = body(srv)
    f.sendcmd("OPTS UTF8 ON")
    assert "SIZE" in f.sendcmd("FEAT")
    f.sendcmd("SYST")
    f.voidcmd("TYPE I")
    f.mkd("EOSR5")
    f.cwd("EOSR5")
    with pytest.raises(ftplib.error_perm):
        f.size("IMG_0003.JPG")
    f.storbinary("STOR IMG_0003.JPG", Pipe(data))
    f.voidcmd("TYPE I")
    assert f.size("IMG_0003.JPG") == len(data)
    assert f.sendcmd("MDTM IMG_0003.JPG").startswith("213 ")
    assert "IMG_0003.JPG" in f.nlst()
    assert "IMG_0003.JPG" in "\n".join(_lines(f))
    f.quit()


def _lines(f) -> list[str]:
    out: list[str] = []
    f.retrlines("LIST -la", out.append)
    return out


def test_a_folder_the_camera_assumed_exists_is_made_rather_than_losing_the_frame(srv, shoot):
    f = body(srv)
    f.storbinary("STOR Z9/100NZ9__/DSC_0001.JPG", Pipe(frame(MB)))
    f.quit()
    assert "Z9/100NZ9__/DSC_0001.JPG" in photos(shoot)


def test_burst_on_one_connection_each_frame_arrives_in_order_and_intact(srv, shoot):
    f = body(srv)
    frames = {f"DSC{i:05}.ARW": frame(30 * MB if i % 4 == 0 else 8 * MB, i) for i in range(12)}
    for name, data in frames.items():
        f.storbinary(f"STOR {name}", Pipe(data))
    f.quit()
    got = photos(shoot)
    assert {k: sha(v) for k, v in got.items()} == {k: sha(v) for k, v in frames.items()}
    assert [e[1]["name"] for e in srv.events if e[0] == "landed"] == list(frames)


def test_two_bodies_at_once_lose_nothing(srv, shoot):
    frames = {who: {f"DSC{i:05}.JPG": frame(6 * MB, who * 100 + i) for i in range(6)} for who in (1, 2)}
    errors = []

    def run(who):
        try:
            f = body(srv)
            for name, data in frames[who].items():
                f.storbinary(f"STOR {name}", Pipe(data))
            f.quit()
        except Exception as e:      # noqa: BLE001 - reported on the main thread
            errors.append(e)

    ts = [threading.Thread(target=run, args=(w,)) for w in (1, 2)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errors
    want = sorted(sha(d) for w in frames.values() for d in w.values())
    assert sorted(sha(v) for v in photos(shoot).values()) == want      # same names on both, every frame still there
    assert srv.status()["landed"] == 12


def test_a_frame_is_never_visible_in_the_folder_until_it_is_whole(srv, shoot):
    """What matters to Live: its own walker, not ours, never meets a partial."""
    live = pytest.importorskip("photosort.live")
    data = frame(12 * MB)
    seen, halfway = [], threading.Event()

    def look():
        while not halfway.is_set():
            seen.append(dict(live.photo_files(shoot)))
            time.sleep(0.002)

    t = threading.Thread(target=look)
    t.start()
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(data), blocksize=256 * 1024)
    halfway.set()
    t.join()
    f.quit()
    sizes = {s["DSC00001.JPG"][0] for s in seen if "DSC00001.JPG" in s}
    assert sizes <= {len(data)}
    assert set(live.photo_files(shoot)) == {"DSC00001.JPG"} and len(seen) > 5


# ===== never lose, never overwrite =====

def test_same_name_different_frame_gets_its_own_name(srv, shoot):
    a, b = frame(MB, 1), frame(MB, 2)
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(a))
    f.storbinary("STOR DSC00001.JPG", Pipe(b))
    f.quit()
    got = photos(shoot)
    assert sha(got["DSC00001.JPG"]) == sha(a) and sha(got["DSC00001_1.JPG"]) == sha(b)


def test_same_bytes_again_land_once(srv, shoot):
    """A camera that never heard the reply sends the frame again. That is not a second photograph."""
    a = frame(MB)
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(a))
    assert f.storbinary("STOR DSC00001.JPG", Pipe(a)).startswith("226")
    f.quit()
    assert list(photos(shoot)) == ["DSC00001.JPG"]
    assert srv.status()["landed"] == 1 and srv.status()["repeats"] == 1


def test_a_file_already_in_the_folder_is_not_touched(srv, shoot):
    (shoot / "DSC00001.JPG").write_bytes(b"from the last shoot")
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(frame(MB)))
    f.quit()
    assert (shoot / "DSC00001.JPG").read_bytes() == b"from the last shoot"
    assert "DSC00001_1.JPG" in photos(shoot)


def test_landing_is_exclusive_on_a_disk_with_no_exclusive_rename(shoot, monkeypatch):
    """exFAT answers ENOTSUP to RENAME_EXCL, measured on a real image, so the O_EXCL claim has to hold on its own."""
    monkeypatch.setattr(tether._libc, "renamex_np", _enotsup, raising=False)
    (shoot / "a").write_bytes(b"one")
    (shoot / "b").write_bytes(b"two")
    with pytest.raises(FileExistsError):
        tether._rename_new(str(shoot / "a"), str(shoot / "b"))
    assert (shoot / "a").read_bytes() == b"one" and (shoot / "b").read_bytes() == b"two"
    tether._rename_new(str(shoot / "a"), str(shoot / "c"))
    assert (shoot / "c").read_bytes() == b"one" and not (shoot / "a").exists()


def test_a_failed_replace_leaves_no_empty_frame_behind(shoot, monkeypatch):
    monkeypatch.setattr(tether._libc, "renamex_np", _enotsup, raising=False)

    def fail(*a):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(tether.os, "replace", fail)
    (shoot / "a").write_bytes(b"one")
    with pytest.raises(OSError):
        tether._rename_new(str(shoot / "a"), str(shoot / "b"))
    assert not (shoot / "b").exists() and (shoot / "a").read_bytes() == b"one"


def _enotsup(*a):
    import ctypes, errno
    ctypes.set_errno(errno.ENOTSUP)
    return -1


def test_two_bodies_with_the_same_numbers_keep_their_raw_beside_their_jpeg(shoot):
    """RAW+JPEG from two bodies, interleaved the worst way. A pair split across bodies would be one wrong frame in Live."""
    l = Lander(shoot)
    l.stage.mkdir()

    def send(name, owner, text):
        p = l.stage / f"t{time.monotonic_ns()}"
        p.write_bytes(text)
        return l.land(p, name, owner)[0].name

    assert send("DSC00001.JPG", "bodyA", b"a jpeg") == "DSC00001.JPG"
    assert send("DSC00001.ARW", "bodyB", b"b raw") == "DSC00001_1.ARW"
    assert send("DSC00001.ARW", "bodyA", b"a raw") == "DSC00001.ARW"
    assert send("DSC00001.JPG", "bodyB", b"b jpeg") == "DSC00001_1.JPG"


def test_a_failed_landing_tells_the_camera_and_keeps_the_frame(srv, shoot, monkeypatch):
    """The reply is the only thing that can make a body delete from its card, so it must not go out on a failure."""
    import errno

    def full(*a):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(srv.lander, "land", full)
    data = frame(MB)
    f = body(srv)
    with pytest.raises(ftplib.error_temp) as e:
        f.storbinary("STOR DSC00001.JPG", Pipe(data))
    assert str(e.value).startswith("451")
    f.close()
    assert not photos(shoot)
    assert staged(shoot)[0].read_bytes() == data


def test_low_disk_refuses_before_taking_a_byte(srv, shoot, monkeypatch):
    real = shutil.disk_usage
    monkeypatch.setattr(tether.shutil, "disk_usage", lambda p: real(p)._replace(free=1000))
    f = body(srv)
    with pytest.raises(ftplib.error_temp) as e:
        f.storbinary("STOR DSC00001.JPG", Pipe(frame(MB)))
    assert str(e.value).startswith("452")
    f.close()
    assert not photos(shoot) and not staged(shoot) and srv.status()["low_space"]


# ===== dropped, aborted, resumed =====

def _cut(srv, name: str, data: bytes, at: int, how: str) -> None:
    """Starts an upload and kills it at byte `at`, as a camera losing Wi-Fi or being switched off would."""
    f = body(srv)
    f.voidcmd("TYPE I")
    conn = f.transfercmd(f"STOR {name}")
    conn.sendall(data[:at])
    # a reset throws away whatever the server's kernel still holds, so cut only once it has really read them
    wait(lambda: srv.status()["uploads"] and srv.status()["uploads"][0]["bytes"] >= at)
    if how == "fin":
        conn.close()                    # an orderly close: to FTP this looks exactly like the end of the file
        f.getresp()                     # and the body stays on the line for its reply, as a working one does
    elif how == "reset":
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()                    # RST
    elif how == "control":
        f.sock.shutdown(socket.SHUT_RDWR)   # the whole body goes, control first
        conn.close()
    elif how == "abor":
        f.sock.sendall(b"\xff\xf4\xff\xf2ABOR\r\n")
        conn.close()
    f.close()


@pytest.mark.parametrize("how", ["reset", "control", "abor"])
def test_a_dropped_upload_never_reaches_the_folder(srv, shoot, how):
    data = frame(4 * MB)
    _cut(srv, "DSC00001.JPG", data, 3 * MB, how)
    wait(lambda: any(k == "kept" for k, _ in srv.events))
    wait(lambda: not srv.status()["uploads"])
    assert not photos(shoot)
    p = staged(shoot)
    assert len(p) == 1 and p[0].stat().st_size == 3 * MB and p[0].name.startswith("DSC00001.JPG")
    assert srv.status()["kept"] == 1 and srv.status()["landed"] == 0


def test_a_clean_close_mid_frame_lands_and_is_reported(srv, shoot):
    """To FTP a FIN is the end of the file, so this is an upload that finished. Only the file can say otherwise, and it may
    not refuse: the frame lands, and the photographer hears."""
    data = frame(4 * MB)
    _cut(srv, "DSC00001.JPG", data, 3 * MB, "fin")
    wait(lambda: "DSC00001.JPG" in photos(shoot))
    assert any(k == "suspect" and d["name"] == "DSC00001.JPG" for k, d in srv.events)
    assert srv.status()["kept"] == 0


def test_a_half_sent_raw_lands_flagged_and_the_resend_lands_beside_it(srv, shoot):
    """A cut ARW has no end marker. Its IFDs still say how long it should have been."""
    head = bytearray(b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 2))
    head += struct.pack("<HHII", 0x0111, 4, 1, 64) + struct.pack("<HHII", 0x0117, 4, 1, 10 * MB) + struct.pack("<I", 0)
    head = bytes(head).ljust(64, b"\0")
    whole = head + b"\x55" * (10 * MB)
    _cut(srv, "DSC00001.ARW", whole, 3 * MB, "fin")
    wait(lambda: any(k == "suspect" and d["expected"] == 64 + 10 * MB for k, d in srv.events))
    f = body(srv)
    f.storbinary("STOR DSC00001.ARW", Pipe(whole))
    f.quit()
    got = photos(shoot)
    assert len(got["DSC00001.ARW"]) == 3 * MB and sha(got["DSC00001_1.ARW"]) == sha(whole)


def test_a_raw_that_looks_short_lands_says_so_and_never_gets_a_426(srv, shoot):
    """The check is unproven on a whole RAW, so it can only speak. The upload is accepted and the event carries the numbers."""
    head = bytearray(b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 2))
    head += struct.pack("<HHII", 0x0111, 4, 1, 64) + struct.pack("<HHII", 0x0117, 4, 1, 5000) + struct.pack("<I", 0)
    short = bytes(head).ljust(64, b"\0") + b"x" * 100
    f = body(srv)
    assert f.storbinary("STOR DSC00001.ARW", Pipe(short)).startswith("226")
    f.quit()
    assert photos(shoot)["DSC00001.ARW"] == short
    assert ("suspect", {"who": "127.0.0.1", "name": "DSC00001.ARW", "bytes": len(short), "expected": 5064}) in srv.events
    assert [k for k, _ in srv.events].index("suspect") < [k for k, _ in srv.events].index("landed")
    assert not any(k == "kept" for k, _ in srv.events)


def test_a_stack_that_resets_instead_of_closing_gets_its_frame_landed(srv, shoot):
    """All the bytes came, then an RST where a FIN should be. With a 426 here that body would resend the same frame for ever."""
    data = frame(MB)
    _cut(srv, "DSC00001.JPG", data, len(data), "reset")
    wait(lambda: "DSC00001.JPG" in photos(shoot))
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)


def test_the_same_failure_twice_stops_being_refused(srv, shoot):
    """Nothing proves this file whole, so the first reset is believed. The second identical one means it is how this body ends."""
    data = b"not a format anyone can read" * 40000
    _cut(srv, "clip.xyz", data, len(data), "reset")
    wait(lambda: srv.status()["kept"] == 1 and not srv.status()["uploads"])
    assert not photos(shoot)
    _cut(srv, "clip.xyz", data, len(data), "reset")
    wait(lambda: "clip.xyz" in photos(shoot))
    assert photos(shoot)["clip.xyz"] == data


def test_an_empty_upload_leaves_nothing(srv, shoot):
    f = body(srv)
    assert f.storbinary("STOR DSC00001.JPG", Pipe(b"")).startswith("226")
    f.quit()
    assert not photos(shoot) and not staged(shoot)


def test_a_body_that_said_how_much_and_sent_less_is_reported(srv, shoot):
    f = body(srv)
    f.voidcmd("TYPE I")
    f.sendcmd(f"ALLO {1000}")
    f.voidcmd("TYPE I")
    f.storbinary("STOR notes.xml", Pipe(b"x" * 10))
    f.quit()
    assert ("suspect", {"who": "127.0.0.1", "name": "notes.xml", "bytes": 10, "expected": 1000}) in srv.events


def test_resend_under_a_new_name_goes_back_beside_its_jpeg(srv, shoot):
    """Sony: an interrupted image comes back with an underscore and number even on Overwrite. Canon: _1 or _a1. Left alone
    that RAW would never pair with DSC00001.JPG."""
    raw, jpg = frame(4 * MB, 1), frame(MB, 2)
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(jpg))
    f.quit()
    _cut(srv, "DSC00001.ARW", raw, 2 * MB, "reset")
    wait(lambda: any(k == "kept" for k, _ in srv.events))
    wait(lambda: not srv.status()["uploads"])
    f = body(srv)
    f.storbinary("STOR DSC00001_1.ARW", Pipe(raw))
    f.quit()
    got = photos(shoot)
    assert set(got) == {"DSC00001.JPG", "DSC00001.ARW"} and sha(got["DSC00001.ARW"]) == sha(raw)
    assert srv.status()["kept"] == 0


def test_a_true_second_frame_named_like_a_resend_stays_itself(srv, shoot):
    f = body(srv)
    f.storbinary("STOR DSC00001_1.JPG", Pipe(frame(MB)))
    f.quit()
    assert list(photos(shoot)) == ["DSC00001_1.JPG"]


def test_resume_with_rest_finishes_the_same_frame(srv, shoot):
    data = frame(6 * MB)
    _cut(srv, "DSC00001.JPG", data, 2 * MB, "reset")
    wait(lambda: any(k == "kept" for k, _ in srv.events))
    wait(lambda: not srv.status()["uploads"])
    f = body(srv)
    f.voidcmd("TYPE I")
    at = f.size("DSC00001.JPG")
    assert at == 2 * MB
    f.storbinary("STOR DSC00001.JPG", Pipe(data[at:]), rest=at)
    f.quit()
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    assert not staged(shoot)


def test_resume_with_appe_finishes_the_same_frame(srv, shoot):
    data = frame(6 * MB)
    _cut(srv, "DSC00001.JPG", data, 2 * MB, "reset")
    wait(lambda: any(k == "kept" for k, _ in srv.events))
    wait(lambda: not srv.status()["uploads"])
    f = body(srv)
    f.voidcmd("TYPE I")
    at = f.size("DSC00001.JPG")
    f.storbinary("APPE DSC00001.JPG", Pipe(data[at:]))
    f.quit()
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)


def test_resuming_past_what_is_here_is_refused(srv, shoot):
    data = frame(4 * MB)
    _cut(srv, "DSC00001.JPG", data, MB, "reset")
    wait(lambda: any(k == "kept" for k, _ in srv.events))
    wait(lambda: not srv.status()["uploads"])
    f = body(srv)
    f.voidcmd("TYPE I")
    with pytest.raises(ftplib.error_perm):
        f.storbinary("STOR DSC00001.JPG", Pipe(data[2 * MB:]), rest=2 * MB)
    f.close()
    assert not photos(shoot) and staged(shoot)[0].stat().st_size == MB


def test_resuming_a_landed_frame_changes_nothing_that_landed(srv, shoot):
    data = frame(2 * MB)
    f = body(srv)
    f.storbinary("STOR DSC00001.JPG", Pipe(data))
    f.voidcmd("TYPE I")
    f.storbinary("APPE DSC00001.JPG", Pipe(b"\xff\xd8more\xff\xd9"))
    f.quit()
    got = photos(shoot)
    assert sha(got["DSC00001.JPG"]) == sha(data) and "DSC00001_1.JPG" in got


def test_nothing_to_resume_says_so(srv):
    f = body(srv)
    f.voidcmd("TYPE I")
    with pytest.raises(ftplib.error_perm):
        f.storbinary("STOR DSC00009.JPG", Pipe(b"x"), rest=100)
    f.close()


def test_a_kept_partial_survives_a_new_session_and_can_still_be_resumed(shoot):
    a = FtpReceiver(shoot, host="127.0.0.1", port=0, user="cam", password="pw")
    a.start()
    data = frame(4 * MB)
    _cut(a, "DSC00001.JPG", data, MB, "reset")
    wait(lambda: a.status()["kept"] == 1 and not a.status()["uploads"])
    a.stop(drain_s=0)
    b = FtpReceiver(shoot, host="127.0.0.1", port=0, user="cam", password="pw")
    b.start()
    try:
        assert b.status()["kept"] == 1
        f = body(b)
        f.voidcmd("TYPE I")
        assert f.size("DSC00001.JPG") == MB
        f.storbinary("STOR DSC00001.JPG", Pipe(data[MB:]), rest=MB)
        f.quit()
        assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    finally:
        b.stop(drain_s=0)


def test_a_stale_connection_does_not_block_the_retry(srv, shoot):
    """Wi-Fi went, the old data connection is still open on our side, the body is back and sends again."""
    data = frame(2 * MB)
    f = body(srv)
    f.voidcmd("TYPE I")
    ghost = f.transfercmd("STOR DSC00001.JPG")
    ghost.sendall(data[:MB])
    wait(lambda: srv.status()["uploads"] and srv.status()["uploads"][0]["bytes"] >= MB)
    g = body(srv)
    g.storbinary("STOR DSC00001.JPG", Pipe(data))
    g.quit()
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    ghost.close()
    f.close()


def test_a_silent_data_connection_is_called_dropped(srv, shoot, monkeypatch):
    monkeypatch.setattr(tether, "STALL_S", 0.3)
    f = body(srv)
    f.voidcmd("TYPE I")
    conn = f.transfercmd("STOR DSC00001.JPG")
    conn.sendall(b"\xff\xd8half")
    wait(lambda: any(k == "kept" and d["why"] == "stalled" for k, d in srv.events))
    conn.close()
    f.close()
    assert not photos(shoot)


# ===== the walls =====

@pytest.mark.parametrize("path", ["../evil.jpg", "../../evil.jpg", "a/../../evil.jpg", "/../evil.jpg", "/../../../etc/evil", "..",
                                  ".sorted-incoming/evil.jpg", ".evil.jpg", "a/.hidden/evil.jpg"])
def test_traversal_is_refused_not_quietly_clamped(srv, shoot, path):
    f = body(srv)
    f.voidcmd("TYPE I")
    with pytest.raises(ftplib.error_perm):
        f.storbinary(f"STOR {path}", Pipe(frame(1024)))
    f.close()
    assert not photos(shoot)
    assert not (shoot.parent / "evil.jpg").exists()


@pytest.mark.parametrize("cmd", ["CWD ..", "CWD ../..", "CWD /../..", "CWD .sorted-incoming", "MKD ../evil", "MKD .sneak", "SIZE ../shoot/x",
                                 "MDTM ../shoot", "SIZE /etc/passwd", "CWD /etc"])
def test_no_command_walks_out(srv, shoot, cmd):
    f = body(srv)
    with pytest.raises(ftplib.error_perm):
        f.sendcmd(cmd)
    assert f.pwd() == "/"
    f.close()
    assert not (shoot.parent / "evil").exists()


@pytest.mark.parametrize("line", ["../", "/etc", ".sorted-incoming", "/../.."])
def test_listing_stays_inside(srv, line):
    f = body(srv)
    with pytest.raises(ftplib.error_perm):
        f.nlst(line)
    f.close()


def test_a_listing_hides_the_staging_folder(srv, shoot):
    (shoot / ".DS_Store").write_bytes(b"x")
    (shoot / "seen.jpg").write_bytes(b"x")
    f = body(srv)
    assert f.nlst() == ["seen.jpg"]
    f.quit()


def test_symlinks_lead_nowhere(srv, shoot, tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_bytes(b"s" * 77)
    (shoot / "door").symlink_to(out)
    (shoot / "file").symlink_to(out / "secret.txt")
    f = body(srv)
    f.voidcmd("TYPE I")
    for cmd in ("CWD door", "SIZE door/secret.txt", "SIZE file", "MKD door/x"):
        with pytest.raises(ftplib.error_perm):
            f.sendcmd(cmd)
    with pytest.raises(ftplib.error_perm):
        f.storbinary("STOR door/in.jpg", Pipe(frame(1024)))
    f.close()
    assert sorted(p.name for p in out.iterdir()) == ["secret.txt"]


@pytest.mark.parametrize("cmd", ["RETR DSC00001.JPG", "DELE DSC00001.JPG", "RMD sub", "XRMD sub", "RNFR DSC00001.JPG", "RNTO other.jpg", "STOU",
                                 "SITE CHMOD 777 DSC00001.JPG", "MFMT 20200101000000 DSC00001.JPG"])
def test_nothing_comes_back_nothing_goes_away(srv, shoot, cmd):
    (shoot / "DSC00001.JPG").write_bytes(b"mine")
    (shoot / "sub").mkdir()
    before = (shoot / "DSC00001.JPG").stat().st_mtime_ns
    f = body(srv)
    with pytest.raises(ftplib.error_perm):
        f.sendcmd(cmd)
    f.close()
    assert (shoot / "DSC00001.JPG").read_bytes() == b"mine" and (shoot / "sub").is_dir()
    assert (shoot / "DSC00001.JPG").stat().st_mtime_ns == before


def test_a_port_command_cannot_aim_the_server_at_someone_else(srv):
    f = body(srv)
    for cmd in ("PORT 8,8,8,8,0,80", "PORT 127,0,0,1,0,80", "EPRT |1|8.8.8.8|4000|", "EPRT |2|::1|4000|"):
        with pytest.raises(ftplib.error_perm):
            f.sendcmd(cmd)
    f.close()


def test_someone_else_cannot_steal_a_passive_port(srv, shoot):
    """The check is by address, so it needs a second address: the stranger is simulated by changing who the session thinks it is."""
    f = body(srv)
    f.voidcmd("TYPE I")
    c = next(iter(srv.controls))
    c.peer = "10.9.9.9"
    host, port = f.makepasv()
    probe = socket.create_connection((host, port), timeout=5)
    c.pasv.settimeout(0.3)
    keep, tether.DATA_WAIT_S = tether.DATA_WAIT_S, 0.2
    try:
        with pytest.raises(OSError):
            c._open_data()
    finally:
        tether.DATA_WAIT_S = keep
    probe.close()
    f.close()


def test_garbage_does_not_end_the_session(srv):
    f = body(srv)
    for line in ("XYZZY", "\x00\x01", "A" * 5000):
        try:
            f.sendcmd(line)
        except ftplib.all_errors:
            pass
    assert f.pwd() == "/"
    f.quit()
    assert srv.status()["not_implemented"].get("XYZZY") == 1


def test_a_line_feed_cannot_smuggle_a_second_name(srv, shoot):
    f = body(srv)
    f.sock.sendall(b"STOR a\x00b.jpg\r\n")
    with pytest.raises(ftplib.error_perm):
        f.voidresp()
    f.close()


# ===== session life =====

def test_stop_closes_the_door(shoot):
    r = FtpReceiver(shoot, host="127.0.0.1", port=0)
    r.start()
    port = r.port
    f = body(r)
    r.stop(drain_s=0)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    with pytest.raises((EOFError, OSError, ftplib.Error)):
        f.pwd()


def test_stop_waits_for_a_frame_already_on_the_wire(shoot):
    r = FtpReceiver(shoot, host="127.0.0.1", port=0)
    r.start()
    data = frame(4 * MB)
    f = body(r)
    f.voidcmd("TYPE I")
    conn = f.transfercmd("STOR DSC00001.JPG")
    conn.sendall(data[:MB])
    wait(lambda: r.status()["uploads"])
    threading.Timer(0.3, lambda: (conn.sendall(data[MB:]), conn.close())).start()
    r.stop(drain_s=5)
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)


def test_stop_waits_for_the_jpeg_that_follows_a_raw(shoot):
    """The camera's next frame starts just after the last reply, so an empty moment is not the end."""
    r = FtpReceiver(shoot, host="127.0.0.1", port=0)
    r.start()
    f = body(r)
    f.storbinary("STOR DSC00001.ARW", Pipe(frame(MB)))

    def jpeg():
        time.sleep(0.5)
        f.storbinary("STOR DSC00001.JPG", Pipe(frame(MB, 2)))
    t = threading.Thread(target=jpeg)
    t.start()
    r.stop(quiet_s=1.0)
    t.join()
    assert set(photos(shoot)) == {"DSC00001.ARW", "DSC00001.JPG"}


def test_stop_with_nothing_arriving_is_immediate(srv):
    t = time.monotonic()
    srv.stop()
    assert time.monotonic() - t < 1


def test_stop_cuts_a_silent_upload_and_keeps_what_came(shoot):
    r = FtpReceiver(shoot, host="127.0.0.1", port=0)
    r.start()
    f = body(r)
    f.voidcmd("TYPE I")
    conn = f.transfercmd("STOR DSC00001.JPG")
    conn.sendall(frame(MB)[:1000])
    wait(lambda: r.status()["uploads"] and r.status()["uploads"][0]["bytes"] == 1000)
    t = time.monotonic()
    r.stop(stuck_s=0.3)
    assert time.monotonic() - t < 5
    conn.close()
    assert not photos(shoot) and staged(shoot)[0].stat().st_size == 1000


def test_a_taken_port_moves_up(shoot):
    a = FtpReceiver(shoot, host="127.0.0.1", port=0)
    a.start()
    b = FtpReceiver(shoot, host="127.0.0.1", port=a.port)
    b.start()
    try:
        assert b.port == a.port + 1 or b.port > a.port
    finally:
        b.stop(drain_s=0)
        a.stop(drain_s=0)


def test_a_missing_folder_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        FtpReceiver(tmp_path / "gone", host="127.0.0.1").start()


def test_start_before_there_is_a_network_then_join_one(shoot, monkeypatch):
    now: list = []
    monkeypatch.setattr(tether, "lan_addresses", lambda: list(now))
    monkeypatch.setattr(tether, "RESCAN_S", 0.05)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    r = FtpReceiver(shoot, port=free)
    m = r.start()
    try:
        assert m["address"] is None and m["port"] == free
        now[:] = [{"address": "127.0.0.1", "interface": "en0", "default": True, "kind": "Wi-Fi"}]
        wait(lambda: r.addresses)
        f = body(r)
        f.storbinary("STOR DSC00001.JPG", Pipe(frame(1024)))
        f.quit()
        assert "DSC00001.JPG" in photos(shoot)
    finally:
        r.stop(drain_s=0)


def test_the_folder_going_away_is_a_clean_refusal(srv, shoot, tmp_path):
    f = body(srv)
    moved = tmp_path / "moved"
    shoot.rename(moved)
    try:
        with pytest.raises(ftplib.Error):
            f.storbinary("STOR DSC00001.JPG", Pipe(frame(1024)))
        assert f.voidcmd("NOOP").startswith("200")
    finally:
        moved.rename(shoot)
    f.close()


def test_the_address_list_follows_the_macs_networks(shoot, monkeypatch):
    """Join the camera's own Wi-Fi after Start and the server is there too, on the same port."""
    now = [{"address": "127.0.0.1", "interface": "en0", "default": True, "kind": "Wi-Fi"}]
    monkeypatch.setattr(tether, "lan_addresses", lambda: list(now))
    monkeypatch.setattr(tether, "RESCAN_S", 0.05)
    seen = []
    r = FtpReceiver(shoot, port=0, on_event=lambda k, **d: seen.append((k, d)))
    r.start()
    try:
        port = r.port
        now[:] = [{"address": "127.0.0.1", "interface": "en0", "default": True, "kind": "Wi-Fi"},
                  {"address": "0.0.0.0", "interface": "en9", "default": False, "kind": "hotspot"}]
        wait(lambda: len(r.addresses) == 2)
        assert r.port == port and ("address", {"addresses": ["127.0.0.1", "0.0.0.0"]}) in seen
        now[:] = [{"address": "127.0.0.1", "interface": "en0", "default": True, "kind": "Wi-Fi"}]
        wait(lambda: len(r.addresses) == 1)
    finally:
        r.stop(drain_s=0)


def test_too_many_connections_are_turned_away_politely(srv, monkeypatch):
    monkeypatch.setattr(tether, "MAX_PER_IP", 2)
    keep = [body(srv, login=False) for _ in range(2)]
    with pytest.raises((ftplib.error_temp, EOFError, OSError)):
        body(srv, login=False)
    [k.close() for k in keep]


# ===== active mode and FTPS =====

def test_active_mode_works_for_a_body_left_on_its_default(srv, shoot):
    data = frame(MB)
    f = body(srv)
    f.set_pasv(False)
    f.storbinary("STOR DSC00001.JPG", Pipe(data))
    f.quit()
    assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    assert srv.status()["active_mode"] and any(k == "active_mode" for k, _ in srv.events)


def test_upload_with_no_data_connection_asked_for_is_refused(srv):
    f = body(srv)
    with pytest.raises(ftplib.error_temp):
        f.sendcmd("STOR DSC00001.JPG")
    f.close()


def test_ftps_end_to_end_and_plain_is_turned_away(shoot, tmp_path):
    pytest.importorskip("cryptography")
    import ssl
    r = FtpReceiver(shoot, host="127.0.0.1", port=0, tls_dir=tmp_path / "tls")
    m = r.start()
    try:
        assert m["secure"] and Path(m["certificate"]).is_file()
        assert oct((tmp_path / "tls" / "tether-key.pem").stat().st_mode)[-3:] == "600"
        plain = body(r, login=False)
        with pytest.raises(ftplib.error_perm):
            plain.login(r.user, r.password)
        plain.close()
        ctx = ssl.create_default_context(cafile=m["certificate"])
        ctx.check_hostname = False
        data = frame(3 * MB)
        f = ftplib.FTP_TLS(context=ctx)
        f.connect("127.0.0.1", r.port, timeout=30)
        f.auth()
        f.login(r.user, r.password)
        f.prot_p()
        f.storbinary("STOR DSC00001.JPG", Pipe(data))
        assert f.nlst() == ["DSC00001.JPG"]
        f.quit()
        assert sha(photos(shoot)["DSC00001.JPG"]) == sha(data)
    finally:
        r.stop(drain_s=0)


def test_ftps_certificate_is_kept_between_shoots(tmp_path):
    pytest.importorskip("cryptography")
    a = tether.make_certificate(tmp_path, ["192.168.1.5"])[0].read_bytes()
    assert tether.make_certificate(tmp_path, ["10.0.0.5"])[0].read_bytes() == a


# ===== is it whole =====

def _write(p: Path, b: bytes) -> Path:
    p.write_bytes(b)
    return p


def test_whole_jpegs_pass_and_cut_ones_do_not(tmp_path):
    data = frame(2 * MB)
    assert looks_complete(_write(tmp_path / "a.jpg", data))
    assert not looks_complete(_write(tmp_path / "b.jpg", data[:MB]))


def test_a_real_encoder_output_passes_and_its_cut_copy_does_not(tmp_path):
    """Pillow's bytes, not ours. 2,733 real files on the owner's Mac passed by hand as well: see docs/direct-camera.md."""
    from PIL import Image
    import numpy as np
    p = tmp_path / "a.jpg"
    Image.fromarray(np.random.default_rng(1).integers(0, 255, (1500, 1500, 3), dtype=np.uint8)).save(p, quality=95, exif=b"Exif\0\0MM\0*\0\0\0\x08\0\0")
    assert p.stat().st_size > 2 * tether.TAIL and looks_complete(p)
    with open(p, "r+b") as f:
        f.truncate(p.stat().st_size // 2)
    assert not looks_complete(p)


def test_the_real_raw_headers_in_the_fixtures_are_read_and_found_cut(tmp_path):
    """These fixtures are the first few KB of real files: exactly what a dropped upload leaves. Sony ARW and DNG both."""
    names = list(FIXTURES.glob("*.ARW")) + list(FIXTURES.glob("*.DNG"))
    assert len(names) == 4
    for p in names:
        assert not looks_complete(p), p.name


def test_a_real_dng_padded_to_its_declared_length_passes(tmp_path):
    src = FIXTURES / "dji-fc7303-0109.DNG"
    with open(src, "rb") as f:
        end = tether._tiff_end(f, 1 << 40)
    assert end and end > src.stat().st_size
    p = tmp_path / "x.dng"
    shutil.copy(src, p)
    with open(p, "r+b") as f:
        f.truncate(end)
    assert looks_complete(p)
    with open(p, "r+b") as f:
        f.truncate(end - 1)
    assert not looks_complete(p)


def _box(kind: bytes, n: int) -> bytes:
    return struct.pack(">I", n + 8) + kind + b"\0" * n


def test_iso_boxes(tmp_path):
    whole = _box(b"ftyp", 16) + _box(b"moov", 100) + _box(b"mdat", 5000)
    assert looks_complete(_write(tmp_path / "a.cr3", whole))
    assert not looks_complete(_write(tmp_path / "b.cr3", whole[:3000]))
    assert looks_complete(_write(tmp_path / "c.hif", _box(b"ftyp", 16) + struct.pack(">I", 0) + b"mdat" + b"x" * 100))


@pytest.mark.parametrize("data", [b"", b"x", b"plain text sidecar", b"II*\0", b"II*\0\xff\xff\xff\xff", b"\x00\x00\x00\x08ftyp", b"MM\0*" + b"\xff" * 60])
def test_what_cannot_be_read_never_crashes(tmp_path, data):
    looks_complete(_write(tmp_path / "x.bin", data))


def test_an_unknown_file_passes(tmp_path):
    assert looks_complete(_write(tmp_path / "a.xml", b"<clip/>"))
    assert looks_complete(_write(tmp_path / "a.raf", b"FUJIFILMCCD-RAW 0201" + b"\0" * 100))


# ===== the login a camera keeps, and the running line =====

def test_the_login_is_made_once_and_stays_private(tmp_path):
    p = tmp_path / "home" / "camera-login.json"
    a = tether.load_login(p)
    assert tether.load_login(p) == a and a["port"] == tether.FTP_PORT and a["host"] is None
    assert oct(p.stat().st_mode)[-3:] == "600"
    b = tether.reset_login(p)
    assert (b["user"], b["password"]) != (a["user"], a["password"]) and tether.load_login(p) == b


def test_a_damaged_login_file_is_replaced(tmp_path):
    p = tmp_path / "camera-login.json"
    p.write_text("{not json")
    assert tether.load_login(p)["user"].startswith("cam")


def test_moved_compares_what_the_camera_holds(tmp_path):
    login = {"user": "u", "password": "p", "port": 2121, "host": "192.168.1.5"}
    here = [{"address": "192.168.1.5"}]
    assert tether.moved({**login, "host": None}, [{"address": "10.0.0.2"}]) is None
    assert tether.moved(login, here) is None and tether.moved(login, [{"address": "10.0.0.2"}, *here]) is None
    assert tether.moved(login, [{"address": "10.0.0.2"}]) == {"was": "192.168.1.5", "was_port": 2121, "now": "10.0.0.2", "now_port": 2121}
    assert tether.moved(login, here, port=2122)["now_port"] == 2122


T0 = 1_800_000_000.0
LINE = {"addresses": [{"address": "10.0.0.2"}]}


def test_the_line_waits_quietly_then_says_when_the_camera_connected():
    assert tether.camera_line(LINE, T0) == {"text": "Waiting for the camera", "warn": None}
    got = tether.camera_line({**LINE, "last_connected": T0}, T0 + 60)
    assert got["text"].startswith("Camera connected") and got["warn"] is None


def test_connected_and_silent_warns_once_it_has_been_a_while():
    got = tether.camera_line({**LINE, "last_connected": T0, "connected": True}, T0 + tether.QUIET_FIRST_S)
    assert "no shots have arrived" in got["warn"]


def test_a_pause_is_not_a_problem():
    st = {**LINE, "last_connected": T0, "last_frame": T0, "connected": True}
    assert tether.camera_line(st, T0 + 7200)["warn"] is None
    gone = tether.camera_line({**st, "connected": False}, T0 + tether.QUIET_GONE_S)
    assert gone["warn"] is None and "camera not connected" in gone["text"]


def test_an_address_change_outranks_everything_and_names_both_ends():
    mv = {"was": "192.168.1.5", "was_port": 2121, "now": "10.0.0.2", "now_port": 2122}
    got = tether.camera_line({**LINE, "moved": mv, "uploads": [1]}, T0)
    assert "Host to 10.0.0.2 and Port to 2122" in got["warn"] and "192.168.1.5" in got["warn"]


def test_no_network_is_said():
    assert "no network" in tether.camera_line({"addresses": []}, T0)["warn"]


# ===== finding the Mac =====

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.68.103 netmask 0xffffff00 broadcast 192.168.68.255
en7: flags=8863<UP> mtu 1500
\tinet 169.254.20.4 netmask 0xffff0000
bridge100: flags=8863<UP> mtu 1500
\tinet 192.168.2.1 netmask 0xffffff00
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffff00
awdl0: flags=8863<UP> mtu 1500
\tinet 192.168.7.7 netmask 0xffffff00
en9: flags=8863<UP> mtu 1500
\tinet 8.8.4.4 netmask 0xffffff00
"""


def test_only_addresses_a_camera_could_reach_are_offered():
    assert parse_ifconfig(IFCONFIG) == [("en0", "192.168.68.103"), ("en7", "169.254.20.4"), ("bridge100", "192.168.2.1")]


def test_the_default_route_comes_first_and_the_hotspot_is_named(monkeypatch):
    out = {"/sbin/ifconfig": IFCONFIG, "/sbin/route": "   interface: en7\n",
           "/usr/sbin/networksetup": "Hardware Port: Wi-Fi\nDevice: en0\n\nHardware Port: USB LAN\nDevice: en7\n"}
    monkeypatch.setattr(tether, "_run", lambda *cmd: out[cmd[0]])
    got = tether.lan_addresses()
    assert [a["address"] for a in got] == ["169.254.20.4", "192.168.2.1", "192.168.68.103"]
    assert got[1]["kind"] == "hotspot" and got[2]["kind"] == "Wi-Fi" and got[0]["default"]


def test_firewall_is_read_not_changed(monkeypatch):
    calls = []

    def fake(*cmd):
        calls.append(cmd)
        return "Firewall is enabled. (State = 1)" if "--getglobalstate" in cmd else "Firewall has block all state set to disabled."
    monkeypatch.setattr(tether, "_run", fake)
    assert tether.firewall() == {"on": True, "block_all": False}
    assert all(c[1].startswith("--get") for c in calls)


# ===== USB, the part that needs no camera =====

class Item:
    def __init__(self, name):
        self._n = name

    def name(self):
        return self._n


@pytest.fixture
def usb(shoot):
    said, fetched = [], []
    core = UsbCore(shoot, lambda kind, **d: said.append((kind, d)), lambda item, tmp: fetched.append((item, tmp)))
    core.said, core.fetched = said, fetched
    return core


def test_what_is_on_the_card_already_is_left_alone(usb):
    usb.items_added([Item("OLD00001.JPG"), Item("OLD00002.JPG")])
    usb.catalog_done(2)
    assert not usb.fetched and ("ready", {"on_card": 2}) in usb.said


def test_a_new_frame_is_fetched_and_landed(usb, shoot):
    usb.catalog_done(0)
    usb.items_added([Item("DSC00001.JPG")])
    item, tmp = usb.fetched[0]
    (usb.lander.stage / tmp).write_bytes(b"pixels")
    usb.downloaded(tmp, None)
    assert (shoot / "DSC00001.JPG").read_bytes() == b"pixels"
    assert ("landed", {"name": "DSC00001.JPG", "bytes": 6}) in usb.said


def test_a_failed_download_is_said_and_nothing_lands(usb, shoot):
    usb.catalog_done(0)
    usb.items_added([Item("DSC00001.JPG")])
    usb.downloaded(usb.fetched[0][1], "the cable came out")
    assert not photos(shoot)
    assert ("failed", {"name": "DSC00001.JPG", "why": "the cable came out"}) in usb.said


def test_usb_cannot_overwrite_either(usb, shoot):
    (shoot / "DSC00001.JPG").write_bytes(b"first")
    usb.catalog_done(0)
    usb.items_added([Item("DSC00001.JPG")])
    tmp = usb.fetched[0][1]
    (usb.lander.stage / tmp).write_bytes(b"second")
    usb.downloaded(tmp, None)
    assert (shoot / "DSC00001.JPG").read_bytes() == b"first"
    assert (shoot / "DSC00001_1.JPG").read_bytes() == b"second"


def test_a_name_from_the_camera_cannot_climb_out(usb, shoot):
    usb.catalog_done(0)
    usb.items_added([Item("../../evil.jpg")])
    tmp = usb.fetched[0][1]
    (usb.lander.stage / tmp).write_bytes(b"x")
    usb.downloaded(tmp, None)
    assert not (shoot.parent / "evil.jpg").exists() and "evil.jpg" in photos(shoot)


def test_capabilities_are_reported_as_found(usb):
    usb.camera_found("ILCE-7M4", ["ICCameraDeviceCanTakePicture", "ICCameraDeviceCanAcceptPTPCommands"], "ICTransportTypeUSB")
    kind, d = usb.said[0]
    assert kind == "camera" and d["remote_shutter"] and not d["shutter_on_camera"]


def test_the_helper_says_when_it_cannot_run(shoot):
    """In this venv there is no pyobjc, which is the install as it stands. With pyobjc it reports waiting instead."""
    seen = []
    u = UsbReceiver(shoot, on_event=lambda k, **d: seen.append(k))
    u.start()
    wait(lambda: u.status()["state"] in ("unavailable", "waiting"), 15)
    st = u.stop()
    assert st["cameras"] == [] and st["landed"] == 0
    assert seen[0] in ("unavailable", "waiting")
    assert u.proc.poll() is not None


def test_the_module_imports_with_no_models_loaded():
    code = "import sys; import photosort.tether; print(json.dumps(sorted(m for m in ('torch','cv2','numpy') if m in sys.modules)))"
    out = subprocess.run([sys.executable, "-c", "import json; " + code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(Path(tether.__file__).parent.parent)})
    assert json.loads(out.stdout) == []


def test_two_bodies_saving_the_login_at_once_both_succeed(tmp_path):
    # Found in a stress run: two cameras logging in together each saved the host through the same temp name. The second
    # os.replace found it gone, and the OSError closed that camera's connection at PASS, with no reply.
    path = tmp_path / "camera-login.json"
    login = tether.load_login(path)
    errors, go = [], threading.Barrier(8)
    def save(i):
        go.wait()
        try:
            for _ in range(50):
                tether.save_login(path, {**login, "host": f"192.168.1.{i}"})
        except OSError as e:
            errors.append(e)
    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    assert tether.load_login(path)["password"] == login["password"]
    assert [p.name for p in tmp_path.iterdir()] == ["camera-login.json"]
