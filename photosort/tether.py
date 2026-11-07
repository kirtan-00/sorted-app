"""Direct camera connection for Live: the camera talks to sorted, and no maker's tether app sits in between.

Both receivers here only drop finished files into the folder Live already watches. The watcher, RAW and JPEG pairing,
sorting and the drive copy never learn where a frame came from.

Wi-Fi is FtpReceiver: the FTP transfer built into Sony, Canon and Nikon bodies, pointed at a small server here. It is written on
the standard library alone because the job is narrow and the rules are strict: a frame is only ever written under the
chosen folder, nothing can be read back or deleted, and the reply that tells the camera a frame is safe (a camera may
be set to delete it from the card on that reply) goes out only after the frame is flushed and in place. pyftpdlib sends
that reply first.

USB is UsbReceiver: Apple's ImageCaptureCore in a helper process, which reaches the camera through ptpcamerad instead of
fighting it. It needs pyobjc, which is optional, and it says so when missing. See docs/direct-camera.md for what is
measured, what comes from which manual, and what still needs a real camera.

A frame in flight lives in <folder>/.sorted-incoming/. That is the same disk, so the last step is a rename, and a dot
folder, so Live's walker, a scan and the Finder all pass it by. A dropped upload stays there and is never promoted, never
deleted. This module imports only the standard library at the top: `python -m photosort.tether` must run without the
models.
"""
from __future__ import annotations
import argparse, collections, ctypes, errno, fcntl, filecmp, hashlib, hmac, ipaddress, json, os, posixpath, random, re, secrets, select
import selectors, shutil, signal, socket, ssl, stat, struct, subprocess, sys, threading, time, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STAGE_DIR = ".sorted-incoming"
FTP_PORT = 2121           # macOS lets an ordinary user bind 21 on 0.0.0.0 only, never on one address. Every body takes a typed port
PORT_TRIES = 20           # a taken port moves up by one, and the camera menu just shows the new number
# Nikon lists TCP 32768 through 61000 beside 21 for FTP, so the data ports stay inside that for every body.
PASV_PORTS = range(50000, 50400)
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # typed on a camera's soft keyboard: lower case, no 0/o, 1/l/i
PASSWORD_LEN = 10         # about 49 bits, against an online guesser held to the lockout below
MIN_FREE = 256 * 1024**2  # refuse a frame below this, so the camera keeps it on the card and says so
MAX_CONNS, MAX_PER_IP = 16, 8
IDLE_S = 1800.0           # a body sits silent between frames for as long as the art director takes
STALL_S = 60.0            # no bytes on a data connection for this long and the upload is called dropped
DATA_WAIT_S = 30.0
LOGIN_DELAY_S = 1.0
LOGIN_TRIES = 3           # per connection
LOCK_AFTER, LOCK_S = 10, 60.0   # per address: a body with a mistyped password gets back in after a minute
RESCAN_S = 3.0            # how often the Mac's addresses are looked at, for a network joined or lost mid shoot
LOG_KEEP = 2000
CHUNK = 1 << 20
TAIL = 1 << 16
Emit = Callable[..., None]


# ===== where the camera can reach this Mac =====

_SKIP_IF = ("lo", "gif", "stf", "utun", "awdl", "llw", "anpi", "ipsec", "ppp", "ap", "vmenet")


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_ifconfig(text: str) -> list[tuple[str, str]]:
    """(interface, IPv4) for every private address on an interface a camera could be on. VPN tunnels, loopback and
    Apple's peer-to-peer links are left out, and so is anything public: this is for the room, not the internet."""
    out, name = [], ""
    for line in text.splitlines():
        m = re.match(r"^([a-z]+\d*): flags=", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and name and not name.startswith(_SKIP_IF) and ipaddress.ip_address(m.group(1)).is_private:
            out.append((name, m.group(1)))
    return out


def _port_names() -> dict[str, str]:
    names, port = {}, ""
    for line in _run("/usr/sbin/networksetup", "-listallhardwareports").splitlines():
        if line.startswith("Hardware Port: "):
            port = line.split(": ", 1)[1]
        elif line.startswith("Device: "):
            names[line.split(": ", 1)[1]] = port
    return names


def lan_addresses() -> list[dict]:
    """The addresses to offer, the default route's first. Interface names are read, never assumed: en0 is not always Wi-Fi.
    bridge100 and up is the Mac's own Internet Sharing network."""
    names = _port_names()
    m = re.search(r"interface: (\S+)", _run("/sbin/route", "-n", "get", "default"))
    default = m.group(1) if m else None
    found = [{"address": a, "interface": i, "default": i == default,
              "kind": "hotspot" if re.fullmatch(r"bridge1\d\d", i) else names.get(i) or "network"}
             for i, a in parse_ifconfig(_run("/sbin/ifconfig"))]
    return sorted(found, key=lambda x: (not x["default"], x["kind"] != "hotspot", x["interface"]))


def firewall() -> dict:
    """Read only. With the firewall on, macOS asks once whether python may accept incoming connections, on the Mac's own
    screen. Block all incoming refuses the camera outright and asks nothing."""
    tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    on, block = _run(tool, "--getglobalstate"), _run(tool, "--getblockall")
    return {"on": "enabled" in on.lower(), "block_all": "set to enabled" in block.lower()}


# ===== is this file whole =====

def _tiff_end(f, size: int) -> int | None:
    """The furthest byte the file's own directories point at, or None when it is not a TIFF we can read. Sony, Nikon and
    Canon's older RAWs and DNG are all TIFF. Only image data offsets count, not maker notes, which are full of oddities."""
    f.seek(0)
    head = f.read(8)
    if len(head) < 8 or head[:4] not in (b"II*\0", b"MM\0*"):
        return None
    e = "<" if head[:2] == b"II" else ">"
    widths = {1: 1, 3: 2, 4: 4}
    todo, seen, end = [struct.unpack(e + "I", head[4:])[0]], set(), 0

    def values(typ: int, n: int, raw: bytes) -> list[int]:
        w = widths.get(typ)
        if not w or n > 4096:
            return []
        data = raw
        if n * w > 4:
            off = struct.unpack(e + "I", raw)[0]
            if off + n * w > size:
                return [size + 1]
            at = f.tell()
            f.seek(off)
            data = f.read(n * w)
            f.seek(at)
        fmt = e + {1: "B", 3: "H", 4: "I"}[typ] * n
        return list(struct.unpack(fmt, data[:n * w]))

    while todo:
        off = todo.pop()
        if off in seen or len(seen) > 64 or off == 0:
            continue
        seen.add(off)
        if off + 2 > size:
            return size + 1
        f.seek(off)
        raw = f.read(2)
        count = struct.unpack(e + "H", raw)[0]
        if off + 2 + count * 12 + 4 > size:
            return size + 1
        tags: dict[int, list[int]] = {}
        for _ in range(count):
            tag, typ, n = struct.unpack(e + "HHI", f.read(8))
            raw = f.read(4)
            if tag in (0x0111, 0x0117, 0x0201, 0x0202, 0x0144, 0x0145, 0x014A):
                tags[tag] = values(typ, n, raw)
        nxt = f.read(4)
        if len(nxt) == 4:
            todo.append(struct.unpack(e + "I", nxt)[0])
        todo.extend(tags.get(0x014A, []))
        for a, b in ((0x0111, 0x0117), (0x0201, 0x0202), (0x0144, 0x0145)):
            for start, length in zip(tags.get(a, []), tags.get(b, [])):
                end = max(end, start + length)
    return end


def _boxes_fit(f, size: int) -> bool | None:
    """ISO media files (CR3, HEIF, MP4, MOV) are a chain of sized boxes: the last one says whether it was all sent."""
    f.seek(4)
    if f.read(4) != b"ftyp":
        return None
    at = 0
    while at + 8 <= size:
        f.seek(at)
        head = f.read(16)
        n = struct.unpack(">I", head[:4])[0]
        if n == 1 and len(head) == 16:
            n = struct.unpack(">Q", head[8:16])[0]
        if n == 0:
            return None           # "to the end of the file", which says nothing either way
        if n < 8:
            return None
        at += n
    return at <= size


def whole(path: str | os.PathLike) -> bool | None:
    """True when the file's own structure says all of it came, False when it says some did not, None when it cannot tell. FTP
    has no length: a body that closes the data connection in the middle of a frame looks exactly like one that finished,
    so the file is the only witness. It is a witness, not a judge: nothing is refused on this, see _Control._finish."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:2] == b"\xff\xd8":
            f.seek(max(0, size - TAIL))
            return b"\xff\xd9" in f.read()
        try:
            end = _tiff_end(f, size)
            return end <= size if end is not None else _boxes_fit(f, size)
        except (struct.error, OSError, ValueError, OverflowError):
            return None


def looks_complete(path: str | os.PathLike) -> bool:
    return whole(path) is not False


def expected_size(path: str | os.PathLike) -> int | None:
    """How long a TIFF-family file says it should be, for telling a real cut from this check being wrong about a new body."""
    try:
        with open(path, "rb") as f:
            return _tiff_end(f, 1 << 62)
    except (struct.error, OSError, ValueError, OverflowError):
        return None


# ===== landing a finished file =====

_libc = ctypes.CDLL(None, use_errno=True)
RENAME_EXCL = 0x4
_UNSUPPORTED = (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS, errno.EPERM, errno.EXDEV)


def _rename_new(src: str, dst: str) -> None:
    """src onto dst, or FileExistsError. Never replaces a file that is there. APFS and HFS+ do this in one call. exFAT, what most
    shoot SSDs are, answers ENOTSUP, so there the name is claimed with O_EXCL first and the rename replaces only our own
    empty claim. Live's watcher can catch that claim but not call it landed: the size changes before a second poll."""
    if _libc.renamex_np(os.fsencode(src), os.fsencode(dst), RENAME_EXCL) == 0:
        return
    e = ctypes.get_errno()
    if e == errno.EEXIST:
        raise FileExistsError(e, os.strerror(e), dst)
    if e not in _UNSUPPORTED:
        raise OSError(e, os.strerror(e), dst)
    os.close(os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    try:
        os.replace(src, dst)
    except OSError:
        os.unlink(dst)                       # our empty claim, or Live would meet a 0-byte frame
        raise


def _flush(fd: int) -> None:
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)   # plain fsync on a Mac stops at the drive's cache
    except OSError:
        os.fsync(fd)


def _flush_dir(d: str) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass                                 # exFAT refuses a directory fsync; the file itself was flushed
    finally:
        os.close(fd)


class Lander:
    """Moves finished files into the watched folder under a name that is theirs. The only code here that touches it.

    Two bodies shoot DSC00001 at the same time, and a RAW must end up beside its own JPEG, so the first owner of a stem
    keeps it and a second owner gets stem_1 for both halves. The same bytes sent again (a camera that never heard the
    reply) land once."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.stage = self.root / STAGE_DIR
        self.lock = threading.Lock()
        self._stems: dict[str, dict[str, str | None]] = {}
        self._alias: dict[tuple[str, str, str], str] = {}

    def _in(self, d: Path) -> dict[str, str | None]:
        key = str(d)
        if key not in self._stems:
            with os.scandir(d) as it:
                self._stems[key] = {Path(e.name).stem.lower(): None for e in it}
        return self._stems[key]

    def _stem(self, d: Path, stem: str, owner: str) -> str:
        key = (str(d), stem.lower(), owner)
        if key in self._alias:
            return self._alias[key]
        stems, cand, k = self._in(d), stem, 0
        while cand.lower() in stems and stems[cand.lower()] != owner:
            k += 1
            cand = f"{stem}_{k}"
        stems[cand.lower()] = owner
        self._alias[key] = cand
        return cand

    def land(self, tmp: str | Path, rel: str, owner: str) -> tuple[Path, bool]:
        """Returns (where it is, whether it was a repeat of a file already there, in which case tmp is gone)."""
        tmp = str(tmp)
        rel_path = Path(rel)
        with self.lock:
            d = self.root / rel_path.parent
            d.mkdir(parents=True, exist_ok=True)
            first = d / rel_path.name
            size = os.path.getsize(tmp)
            if first.is_file() and first.stat().st_size == size and filecmp.cmp(tmp, first, shallow=False):
                os.unlink(tmp)
                return first, True
            stem, ext = self._stem(d, rel_path.stem, owner), rel_path.suffix
            k = 0
            while True:
                final = d / (f"{stem}{ext}" if k == 0 else f"{stem}_{k}{ext}")
                try:
                    _rename_new(tmp, str(final))
                    break
                except FileExistsError:
                    if final.is_file() and final.stat().st_size == size and filecmp.cmp(tmp, final, shallow=False):
                        os.unlink(tmp)
                        return final, True
                    k += 1
            self._in(d)[final.stem.lower()] = owner
            _flush_dir(str(d))
            return final, False


# ===== the FTP receiver =====

@dataclass
class _Upload:
    who: str
    name: str
    done: int = 0


class Refused(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code, self.text = code, text


_RESENT = re.compile(r"^(.+)_a?\d+$")     # Sony DSC00001_1, Canon IMG_0003_1 and IMG_0003_a1: a frame sent again after a drop


class _Control(threading.Thread):
    """One camera's control connection. A thread each: a flush on one body never holds up another."""

    def __init__(self, srv: "FtpReceiver", sock: socket.socket, peer: str):
        super().__init__(daemon=True, name=f"ftp-{peer}")
        self.srv, self.sock, self.peer = srv, sock, peer
        self.buf = b""
        self.user: str | None = None
        self.authed = False
        self.tries = 0
        self.cwd = "/"
        self.rest = 0
        self.alloc: int | None = None
        self.pasv: socket.socket | None = None
        self.port_to: tuple[str, int] | None = None
        self.tls = False
        self.prot_p = False
        self.bye = False

    # -- the wire --
    def reply(self, code: int, text: str) -> None:
        self.sock.sendall(f"{code} {text}\r\n".encode("utf-8", "replace"))

    def _line(self) -> str | None:
        while b"\n" not in self.buf:
            if len(self.buf) > 8192:
                self.buf = b""
                self.reply(500, "Line too long.")
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        try:
            return raw.rstrip(b"\r").decode("utf-8")
        except UnicodeDecodeError:
            return raw.rstrip(b"\r").decode("latin-1")

    def run(self) -> None:
        try:
            self.sock.settimeout(IDLE_S)
            self.reply(220, "sorted ready.")
            while not self.bye and not self.srv.stopping.is_set():
                line = self._line()
                if line is None:
                    break
                self._dispatch(line)
        except (socket.timeout, TimeoutError):
            try:
                self.reply(421, "Idle too long.")
            except OSError:
                pass
        except (OSError, ssl.SSLError):
            pass                              # the body went away, which is what bodies do on Wi-Fi
        finally:
            self._drop_data()
            try:
                self.sock.close()
            except OSError:
                pass
            self.srv._gone(self)

    def _dispatch(self, line: str) -> None:
        # ABOR arrives with Telnet interrupt bytes in front when the client follows the RFC; they are not part of the word
        line = line.lstrip("\xff\xf4\xf2� \t")
        cmd, _, arg = line.partition(" ")
        cmd = cmd.upper()
        self.srv._log(self.peer, f"PASS ***" if cmd == "PASS" else line[:200])
        handler = getattr(self, "do_" + cmd, None) if cmd.isalpha() else None
        try:
            if handler is None:
                self.srv._note_refused(self.peer, cmd)
                self.reply(502, "Not implemented.")
            elif not self.authed and cmd not in OPEN_COMMANDS:
                self.reply(530, "Log in first.")
            else:
                handler(arg.strip())
        except Refused as r:
            self.reply(r.code, r.text)
        except (OSError, ssl.SSLError):
            raise
        except Exception as e:                # a bug here must cost one reply, not the connection a body is shooting through
            self.srv._log(self.peer, f"error {e!r}")
            self.reply(451, "Something went wrong here.")

    # -- login --
    def do_USER(self, arg: str) -> None:
        if self.srv.ssl_ctx and not self.tls:
            raise Refused(530, "Use AUTH TLS first.")
        self.user, self.authed = arg, False
        self.reply(331, "Password, please.")

    def do_PASS(self, arg: str) -> None:
        if self.user is None:
            raise Refused(503, "USER first.")
        if self.srv._locked(self.peer):
            raise Refused(530, "Too many wrong passwords. Wait a minute.")
        ok = hmac.compare_digest(self.user.encode(), self.srv.user.encode()) & hmac.compare_digest(arg.encode(), self.srv.password.encode())
        if ok:
            self.authed = True
            self.srv._login(self.peer, self.sock.getsockname()[0])
            self.reply(230, "Logged in.")
            return
        self.srv._failed_login(self.peer, self.user)
        self.user = None
        self.tries += 1
        time.sleep(LOGIN_DELAY_S)
        self.reply(530, "Wrong user or password.")
        if self.tries >= LOGIN_TRIES:
            self.bye = True

    def do_QUIT(self, arg: str) -> None:
        self.reply(221, "Bye.")
        self.bye = True

    def do_NOOP(self, arg: str) -> None:
        self.reply(200, "OK.")

    def do_SYST(self, arg: str) -> None:
        self.reply(215, "UNIX Type: L8")

    def do_CLNT(self, arg: str) -> None:
        self.reply(200, "OK.")

    def do_OPTS(self, arg: str) -> None:
        self.reply(200, "OK.") if arg.upper().startswith("UTF8") else self.reply(501, "Unknown option.")

    def do_HELP(self, arg: str) -> None:
        self.reply(214, "sorted takes pictures and gives nothing back.")

    def do_FEAT(self, arg: str) -> None:
        lines = ["SIZE", "MDTM", "REST STREAM", "UTF8", "EPSV"]
        if self.srv.ssl_ctx:
            lines += ["AUTH TLS", "PBSZ", "PROT"]
        self.sock.sendall(("211-Features:\r\n" + "".join(f" {x}\r\n" for x in lines) + "211 End\r\n").encode())

    def do_AUTH(self, arg: str) -> None:
        if not self.srv.ssl_ctx:
            raise Refused(502, "No TLS here. Use plain FTP.")
        if arg.upper() not in ("TLS", "TLS-C", "SSL"):
            raise Refused(504, "Only TLS.")
        self.reply(234, "Go ahead.")
        self.sock = self.srv.ssl_ctx.wrap_socket(self.sock, server_side=True)
        self.tls, self.buf = True, b""

    def do_PBSZ(self, arg: str) -> None:
        self.reply(200, "PBSZ=0")

    def do_PROT(self, arg: str) -> None:
        if arg.upper() not in ("C", "P"):
            raise Refused(504, "C or P.")
        if arg.upper() == "C" and self.srv.ssl_ctx:
            raise Refused(534, "Data must be encrypted here.")
        self.prot_p = arg.upper() == "P"
        self.reply(200, "OK.")

    # -- settings that have one sensible answer --
    def do_TYPE(self, arg: str) -> None:
        self.reply(200, "OK.") if arg.upper().split()[0:1] in (["A"], ["I"], ["L"]) else self.reply(504, "A or I.")

    def do_MODE(self, arg: str) -> None:
        self.reply(200, "OK.") if arg.upper() == "S" else self.reply(504, "Stream only.")

    def do_STRU(self, arg: str) -> None:
        self.reply(200, "OK.") if arg.upper() == "F" else self.reply(504, "File only.")

    def do_ALLO(self, arg: str) -> None:
        self.alloc = int(arg) if arg.isdigit() else None
        self.reply(200, "OK.")

    def do_REST(self, arg: str) -> None:
        if not arg.isdigit():
            raise Refused(501, "A byte count.")
        self.rest = int(arg)
        self.reply(350, f"Restarting at {self.rest}.")

    # -- paths --
    def _vpath(self, arg: str) -> str:
        """A path as the camera sees it, / being the chosen folder. Anything that climbs out of it, names a dot file
        (the staging folder, and dot files would never be seen by the watcher anyway) or holds a control character is refused
        here. `..` inside the folder is fine: a camera walking back up its own tree is not an attack."""
        if not arg or any(c in arg for c in "\0\r\n\\"):
            raise Refused(553, "Bad path.")
        parts: list[str] = [] if arg.startswith("/") else [p for p in self.cwd.split("/") if p]
        for seg in arg.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    raise Refused(550, "Outside the shoot folder.")
                parts.pop()
            elif seg.startswith("."):
                raise Refused(550, "No such name here.")
            else:
                parts.append(seg)
        return "/" + "/".join(parts)

    def _real(self, vpath: str) -> Path:
        """On disk, with symlinks followed and checked: a link planted in the folder leads nowhere."""
        root = os.path.realpath(self.srv.root)
        real = os.path.realpath(os.path.join(root, vpath.lstrip("/")))
        if real != root and not real.startswith(root + os.sep):
            raise Refused(550, "Outside the shoot folder.")
        return Path(real)

    def do_PWD(self, arg: str) -> None:
        self.reply(257, '"' + self.cwd.replace('"', '""') + '" is the folder.')

    do_XPWD = do_PWD

    def do_CWD(self, arg: str) -> None:
        v = self._vpath(arg or "/")
        if not self._real(v).is_dir():
            raise Refused(550, "No such folder.")
        self.cwd = v
        self.reply(250, "OK.")

    do_XCWD = do_CWD

    def do_CDUP(self, arg: str) -> None:
        self.cwd = posixpath.dirname(self.cwd) or "/"
        self.reply(250, "OK.")

    do_XCUP = do_CDUP

    def do_MKD(self, arg: str) -> None:
        v = self._vpath(arg)
        real = self._real(v)
        try:
            real.mkdir()
        except FileExistsError:
            raise Refused(550, "Already there.")
        except OSError as e:
            raise Refused(550, e.strerror or "Could not make it.")
        self.reply(257, f'"{v}" made.')

    do_XMKD = do_MKD

    def do_SIZE(self, arg: str) -> None:
        v = self._vpath(arg)
        real = self._real(v)
        if real.is_file():
            self.reply(213, str(real.stat().st_size))
            return
        part = self.srv._partial(self.peer, v)
        if not part:
            raise Refused(550, "No such file.")
        self.reply(213, str(os.path.getsize(part)))     # what a body resuming a dropped frame needs to hear

    def do_MDTM(self, arg: str) -> None:
        real = self._real(self._vpath(arg))
        if not real.is_file():
            raise Refused(550, "No such file.")
        self.reply(213, time.strftime("%Y%m%d%H%M%S", time.gmtime(real.stat().st_mtime)))

    # -- data connections --
    def _drop_data(self) -> None:
        if self.pasv:
            try:
                self.pasv.close()
            except OSError:
                pass
        self.pasv = self.port_to = None

    def _listen(self) -> int:
        self._drop_data()
        host = self.sock.getsockname()[0]
        for port in random.sample(PASV_PORTS, min(64, len(PASV_PORTS))):
            s = socket.socket()
            try:
                s.bind((host, port))
                s.listen(2)
                self.pasv = s
                return port
            except OSError:
                s.close()
        raise Refused(425, "No free data port.")

    def do_PASV(self, arg: str) -> None:
        port = self._listen()
        host = self.sock.getsockname()[0].replace(".", ",")
        self.reply(227, f"Entering Passive Mode ({host},{port >> 8},{port & 255}).")

    def do_EPSV(self, arg: str) -> None:
        if arg.upper() == "ALL":
            self.reply(200, "OK.")
            return
        self.reply(229, f"Entering Extended Passive Mode (|||{self._listen()}|)")

    def _active(self, host: str, port: int) -> None:
        # only ever back to the body that asked, and never a privileged port: the old FTP bounce attack is this command
        if host != self.peer or port < 1024:
            raise Refused(500, "Only to yourself.")
        self._drop_data()
        self.port_to = (host, port)
        self.srv._note_active(self.peer)
        self.reply(200, "OK. Passive mode is better.")

    def do_PORT(self, arg: str) -> None:
        try:
            n = [int(x) for x in arg.split(",")]
            if len(n) != 6 or not all(0 <= x <= 255 for x in n):
                raise ValueError
        except ValueError:
            raise Refused(501, "Six numbers.")
        self._active(".".join(map(str, n[:4])), n[4] * 256 + n[5])

    def do_EPRT(self, arg: str) -> None:
        bits = arg.split(arg[:1]) if arg else []
        if len(bits) < 5 or bits[1] != "1" or not bits[3].isdigit():
            raise Refused(522, "IPv4 only. (1)")
        self._active(bits[2], int(bits[3]))

    def _open_data(self) -> socket.socket:
        if self.pasv:
            self.pasv.settimeout(DATA_WAIT_S)
            end = time.monotonic() + DATA_WAIT_S
            while True:
                conn, addr = self.pasv.accept()
                if addr[0] == self.peer:
                    break
                conn.close()                 # someone else raced for the port: never take a frame or a listing from them
                if time.monotonic() > end:
                    raise socket.timeout
            self._drop_data()
        elif self.port_to:
            conn = socket.create_connection(self.port_to, timeout=15)
            self._drop_data()
        else:
            raise Refused(425, "Send PASV first.")
        conn.settimeout(STALL_S)
        if self.prot_p:
            conn = self.srv.ssl_ctx.wrap_socket(conn, server_side=True)
        return conn

    def _start_data(self, text: str) -> socket.socket:
        if not (self.pasv or self.port_to):
            raise Refused(425, "Send PASV first.")
        self.reply(150, text)
        try:
            return self._open_data()
        except (OSError, ssl.SSLError):
            # in active mode this is what macOS's Local Network permission looks like from here
            raise Refused(425, "Could not open the data connection.")

    @staticmethod
    def _close_data(conn: socket.socket) -> None:
        try:
            if isinstance(conn, ssl.SSLSocket):
                conn.settimeout(2)
                conn = conn.unwrap()
        except (OSError, ssl.SSLError, ValueError):
            pass
        try:
            conn.close()
        except OSError:
            pass

    # -- listings, inside the folder only --
    def _entries(self, arg: str) -> list[tuple[str, os.stat_result]]:
        args = [a for a in arg.split() if not a.startswith("-")]
        real = self._real(self._vpath(args[0] if args else self.cwd))
        if real.is_file():
            return [(real.name, real.stat())]
        if not real.is_dir():
            raise Refused(550, "No such folder.")
        out = []
        with os.scandir(real) as it:
            for e in sorted(it, key=lambda e: e.name):
                if not e.name.startswith("."):
                    try:
                        out.append((e.name, e.stat(follow_symlinks=False)))
                    except OSError:
                        pass
        return out

    def _send(self, text: str) -> None:
        conn = self._start_data("Here it comes.")
        try:
            conn.sendall(text.encode("utf-8", "replace"))
        finally:
            self._close_data(conn)
        self.reply(226, "Done.")

    def do_LIST(self, arg: str) -> None:
        rows = []
        for name, st in self._entries(arg):
            kind = "d" if stat.S_ISDIR(st.st_mode) else "-"
            when = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
            rows.append(f"{kind}rw-r--r-- 1 sorted sorted {st.st_size:>12} {when} {name}\r\n")
        self._send("".join(rows))

    def do_NLST(self, arg: str) -> None:
        self._send("".join(f"{n}\r\n" for n, _ in self._entries(arg)))

    # -- taking a frame --
    def do_STOR(self, arg: str) -> None:
        self._take(arg, append=False)

    def do_APPE(self, arg: str) -> None:
        self._take(arg, append=True)

    def _take(self, arg: str, append: bool) -> None:
        srv, v = self.srv, self._vpath(arg)
        rest, self.rest = self.rest, 0
        alloc, self.alloc = self.alloc, None
        real = self._real(v)
        if v == "/" or real.is_dir():
            raise Refused(553, "That is a folder.")
        try:
            free = shutil.disk_usage(srv.root).free
        except OSError:
            raise Refused(451, "The shoot folder is not there.")
        if free < MIN_FREE:
            srv._note_low_space(free)
            raise Refused(452, "The disk is full. The frame stays on the card.")
        try:
            part, offset = srv._open_part(self.peer, v, real, rest, append)
        except OSError as e:
            raise Refused(451, e.strerror or "Could not stage the frame.")
        upload = _Upload(self.peer, v.lstrip("/"), offset)
        with srv.lock:
            srv.uploads[str(part)] = upload
        conn = None
        try:
            conn = self._start_data("Send it.")
            how = self._receive(conn, part, offset, upload)
            self._close_data(conn)
            conn = None
            self._finish(part, v, how, alloc)
        finally:
            if conn is not None:
                self._close_data(conn)
            srv._release(part)
            if part.exists() and part.stat().st_size == 0:
                part.unlink()                 # the data connection never came: there is nothing to keep

    def _receive(self, conn: socket.socket, part: Path, offset: int, upload: _Upload) -> str:
        """Bytes from the camera into the staged file until it stops. Watches the control connection too, for ABOR."""
        watch = [conn] if self.tls else [conn, self.sock]    # reading half a TLS record off the control socket would block
        heard = time.monotonic()
        orphan = False
        with open(part, "r+b") as f:
            f.seek(offset)
            f.truncate()
            try:
                while True:
                    pending = isinstance(conn, ssl.SSLSocket) and conn.pending() > 0
                    ready = [conn] if pending else select.select(watch, [], [], 0.5)[0]
                    if self.srv.stopping.is_set():
                        how = "dropped"
                        break
                    if not ready:
                        if time.monotonic() - heard > STALL_S:
                            how = "stalled"
                            break
                        continue
                    if self.sock in ready:
                        chunk = self.sock.recv(4096)
                        if not chunk:
                            watch.remove(self.sock)      # the control connection fell; the data may yet finish
                            orphan = True
                        else:
                            self.buf += chunk
                            if b"ABOR" in self.buf.upper():
                                self.buf = b""
                                how = "aborted"
                                break
                    if conn in ready:
                        chunk = conn.recv(CHUNK)
                        if not chunk:
                            how = "end"
                            break
                        f.write(chunk)
                        upload.done += len(chunk)
                        heard = self.srv.last_moved = time.monotonic()
            except (socket.timeout, TimeoutError):
                how = "stalled"
            except (OSError, ssl.SSLError, ValueError):
                how = "dropped"
            f.flush()
            _flush(f.fileno())
        return "dropped" if orphan and how == "end" else how

    def _control_gone(self) -> bool:
        if self.tls:
            return False
        try:
            if select.select([self.sock], [], [], 0.1)[0]:
                return self.sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True
        return False

    def _finish(self, part: Path, v: str, how: str, alloc: int | None) -> None:
        """Lands whatever ended cleanly: refusing a good frame on an unproven check is the worse mistake, and a refusal a body
        retries is a loop. A file that looks cut still lands, and the photographer is told. Only a connection that really
        failed stays out, and even then not when the frame proves whole (some stacks reset instead of closing) or when
        the body has failed the same way twice: a third 426 would just bring it back."""
        srv = self.srv
        size = os.path.getsize(part)
        if size == 0 and how == "end":
            self.reply(226, "Nothing sent.")
            return
        proof = whole(part)
        if how == "end" and proof is False and self._control_gone():
            how = "dropped"                   # the body died: its data socket just closed a moment before this one
        ended = how == "end" or (how in ("dropped", "stalled") and (proof is True or srv._failed_twice(self.peer, v, size)))
        if not ended:
            srv._kept(self.peer, v, how, size)
            self.reply(426, "Upload not complete. It is kept and can be resumed.")
            if how == "aborted":
                self.reply(226, "Aborted.")
            return
        short = alloc is not None and size < alloc
        expected = alloc if short else expected_size(part)
        try:
            final, repeat = srv._land(part, v, self.peer, suspect=proof is False or short, expected=expected)
        except OSError as e:
            srv._kept(self.peer, v, e.strerror or "could not land", size)
            self.reply(451, "Could not put the frame in the folder. It stays on the card.")
            return
        self.reply(226, "Safe." if not repeat else "Already here.")

    # -- deliberately not here --
    def _no(self, arg: str) -> None:
        raise Refused(550, "Nothing leaves this folder and nothing in it changes.")

    do_RETR = do_DELE = do_RMD = do_XRMD = do_RNFR = do_RNTO = do_STOU = do_SITE = do_MFMT = do_MFF = _no

    def do_ABOR(self, arg: str) -> None:
        self.reply(225, "Nothing to abort.")

    def do_STAT(self, arg: str) -> None:
        self.reply(211, "sorted is taking pictures.")


OPEN_COMMANDS = {"USER", "PASS", "QUIT", "NOOP", "SYST", "FEAT", "AUTH", "PBSZ", "PROT", "OPTS", "HELP", "CLNT"}


# ===== the login a camera keeps =====

def _new_login() -> dict:
    return {"user": "cam" + "".join(secrets.choice(ALPHABET) for _ in range(4)),
            "password": "".join(secrets.choice(ALPHABET) for _ in range(PASSWORD_LEN)), "port": FTP_PORT, "host": None}


def save_login(path: str | os.PathLike, login: dict) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(login, f)
    os.replace(tmp, path)
    return login


def load_login(path: str | os.PathLike) -> dict:
    """The login for this Mac, made the first time. A camera menu takes minutes to fill in on a soft keyboard, so it is set
    once. host is where a camera last really reached this Mac, which is what its menu holds."""
    try:
        login = json.loads(Path(path).read_text())
        if login.get("user") and login.get("password"):
            return {**_new_login(), "host": None, **login}
    except (OSError, ValueError):
        pass
    return save_login(path, _new_login())


def reset_login(path: str | os.PathLike) -> dict:
    return save_login(path, {**_new_login(), "port": load_login(path)["port"]})


def moved(login: dict, addresses: list[dict], port: int | None = None) -> dict | None:
    """What the camera still holds, when this Mac can no longer be reached there. None before any camera has connected."""
    now = [a["address"] for a in addresses]
    if not login.get("host") or (login["host"] in now and port in (None, login["port"])):
        return None
    return {"was": login["host"], "was_port": login["port"], "now": now[0] if now else None, "now_port": port or login["port"]}


QUIET_FIRST_S = 120.0     # a camera that logged in and sent nothing by now has transfer off, or is waiting on a setting
QUIET_GONE_S = 300.0      # the camera left and nothing came since: said in the line, never as an alarm, because that is also lunch


def camera_line(st: dict, now: float | None = None) -> dict:
    """The running status in words. warn is for something the photographer must fix; a pause is not one."""
    now = time.time() if now is None else now
    at = lambda t: time.strftime("%H:%M", time.localtime(t))
    mv = st.get("moved")
    if mv and mv.get("now"):
        port = f" and Port to {mv['now_port']}" if mv["now_port"] != mv["was_port"] else ""
        return {"text": "Camera address changed", "warn": f"This Mac's address changed. In the camera, set Host to {mv['now']}{port}. It still has {mv['was']}."}
    if not st.get("addresses"):
        return {"text": "No network", "warn": "This Mac is on no network. Join the same Wi-Fi as the camera."}
    if st.get("draining"):
        return {"text": "Waiting for the last frame from the camera", "warn": None}
    if st.get("uploads"):
        return {"text": "Camera sending", "warn": None}
    connected, frame = st.get("last_connected"), st.get("last_frame")
    if not connected:
        return {"text": "Waiting for the camera", "warn": None}
    if not frame:
        if now - connected >= QUIET_FIRST_S:
            return {"text": f"Camera connected {at(connected)}", "warn": f"The camera connected at {at(connected)} but no shots have arrived. Check that Auto FTP Transfer is on."}
        return {"text": f"Camera connected {at(connected)}", "warn": None}
    if not st.get("connected") and now - frame >= QUIET_GONE_S:
        return {"text": f"No shots since {at(frame)}, camera not connected", "warn": None}
    return {"text": f"Camera connected {at(connected)}, last shot {at(frame)}", "warn": None}


def make_certificate(folder: Path, addresses: list[str]) -> tuple[Path, Path]:
    """A self-signed certificate for FTPS, kept between shoots: a camera imports it from a card once. The cert's file goes on the
    card as cacert.pem for Sony, ROOT.PEM for Canon."""
    folder = Path(folder)
    cert, key = folder / "tether-cert.pem", folder / "tether-key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sorted")])
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=3650))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    pem = b.sign(k, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(key, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert.write_bytes(pem)
    return cert, key


class FtpReceiver:
    """The server, for as long as a Live session runs. start() returns what the camera menu needs; stop() ends it.

    on_event(kind, **data) fires from the server's threads: login, login_failed, landed, repeat, suspect, kept, address,
    low_space, active_mode, refused.
    host pins one address, for tests. Left out, every LAN address is served and the list follows the Mac's networks, so
    joining a camera's own Wi-Fi after Start just works, and so does pressing Start before there is a network at all."""

    def __init__(self, folder: str | os.PathLike, on_event: Emit | None = None, port: int = FTP_PORT, host: str | None = None,
                 user: str | None = None, password: str | None = None, tls_dir: str | os.PathLike | None = None):
        self.root = Path(folder)
        self.on_event = on_event or (lambda kind, **data: None)
        self.want_port, self.host = port, host
        self.port = 0
        self.user = user or "cam" + "".join(secrets.choice(ALPHABET) for _ in range(4))
        self.password = password or "".join(secrets.choice(ALPHABET) for _ in range(PASSWORD_LEN))
        self.tls_dir = Path(tls_dir) if tls_dir else None
        self.ssl_ctx: ssl.SSLContext | None = None
        self.cert: Path | None = None
        self.lander = Lander(self.root)
        self.stage = self.lander.stage
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.controls: set[_Control] = set()
        self.uploads: dict[str, _Upload] = {}
        self.fails: dict[str, list[float]] = {}
        self.listeners: dict[str, socket.socket] = {}
        self.addresses: list[dict] = []
        self.log: collections.deque = collections.deque(maxlen=LOG_KEEP)
        self.landed = self.landed_bytes = self.repeats = self.refused_logins = 0
        self.bodies: dict[str, dict] = {}
        self.last_file: str | None = None
        self.last_connected: float | None = None     # wall clock, for the status line
        self.last_frame: float | None = None
        self.last_moved = 0.0                        # monotonic: bytes or a landing, what Finish waits on
        self.failed: dict[tuple[str, str], int] = {}
        self.active_mode = False
        self.low_space = False
        self.not_implemented: collections.Counter = collections.Counter()
        self.draining = False
        self.thread: threading.Thread | None = None

    # -- life --
    def start(self) -> dict:
        if not self.root.is_dir():
            raise FileNotFoundError(self.root)
        self.stage.mkdir(exist_ok=True)
        self.addresses = [{"address": self.host, "interface": "", "default": True, "kind": "fixed"}] if self.host else lan_addresses()
        if self.tls_dir:
            self.cert, key = make_certificate(self.tls_dir, [a["address"] for a in self.addresses])
            self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self.ssl_ctx.load_cert_chain(self.cert, key)
        self._bind_all()
        self.thread = threading.Thread(target=self._serve, daemon=True, name="ftp-accept")
        self.thread.start()
        return self.menu()

    def _bind_all(self) -> None:
        addrs = [a["address"] for a in self.addresses]
        if not addrs:                        # no network yet: the rescan binds the moment there is one
            self.port = self.want_port
            return
        last: OSError | None = None
        for port in ([self.want_port] if self.want_port == 0 else range(self.want_port, self.want_port + PORT_TRIES)):
            socks: dict[str, socket.socket] = {}
            try:
                for a in addrs:
                    socks[a] = self._bind(a, port or (next(iter(socks.values())).getsockname()[1] if socks else 0))
                self.listeners = socks
                self.port = next(iter(socks.values())).getsockname()[1]
                return
            except OSError as e:
                last = e
                for s in socks.values():
                    s.close()
        raise last or OSError(errno.EADDRINUSE, "No free port.")

    @staticmethod
    def _bind(address: str, port: int) -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, port))
            s.listen(16)
        except OSError:
            s.close()
            raise
        s.setblocking(False)
        return s

    def _serve(self) -> None:
        sel = selectors.DefaultSelector()
        for s in self.listeners.values():
            sel.register(s, selectors.EVENT_READ)
        next_scan = time.monotonic() + RESCAN_S
        try:
            while not self.stopping.is_set():
                for key, _ in sel.select(0.2):
                    try:
                        sock, addr = key.fileobj.accept()
                    except OSError:
                        continue
                    self._accept(sock, addr[0])
                if not self.host and time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + RESCAN_S
                    self._rescan(sel)
        finally:
            sel.close()
            for s in self.listeners.values():
                s.close()
            self.listeners = {}

    def _accept(self, sock: socket.socket, peer: str) -> None:
        sock.setblocking(True)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_OOBINLINE, 1)   # ABOR may come as urgent data
        with self.lock:
            full = len(self.controls) >= MAX_CONNS or sum(c.peer == peer for c in self.controls) >= MAX_PER_IP
            if not full:
                c = _Control(self, sock, peer)
                self.controls.add(c)
        if full:
            try:
                sock.sendall(b"421 Too many connections.\r\n")
            except OSError:
                pass
            sock.close()
            return
        c.start()

    def _rescan(self, sel) -> None:
        now = lan_addresses()
        have, want = set(self.listeners), {a["address"] for a in now}
        for a in have - want:
            s = self.listeners.pop(a)
            sel.unregister(s)
            s.close()
        failed = set()
        for a in want - have:
            try:
                s = self._bind(a, self.port)
            except OSError:
                failed.add(a)                # the port is taken there; the other addresses still work
                continue
            self.listeners[a] = s
            sel.register(s, selectors.EVENT_READ)
        now = [a for a in now if a["address"] not in failed]
        if {a["address"] for a in now} != {a["address"] for a in self.addresses}:
            self.addresses = now
            self.on_event("address", addresses=[a["address"] for a in now])

    def stop(self, drain_s: float = 90.0, quiet_s: float = 1.5, stuck_s: float = 10.0) -> dict:
        """Waits for what is on the wire: until nothing has moved for quiet_s (a RAW's JPEG starts just after the RAW's reply),
        no bytes have come for stuck_s from an upload that hung, or drain_s in all. What is cut off stays staged."""
        if not self.thread:
            return self.status()
        self.draining = True
        end = time.monotonic() + drain_s
        while time.monotonic() < end:
            with self.lock:
                moving = bool(self.uploads)
            idle = time.monotonic() - self.last_moved
            if idle >= (stuck_s if moving else quiet_s):
                break
            time.sleep(0.05)
        self.stopping.set()
        self.thread.join(5)
        with self.lock:
            live = list(self.controls)
        for c in live:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.sock.close()
            except OSError:
                pass
        for c in live:
            c.join(3)
        self.thread = None
        self.draining = False
        return self.status()

    # -- what the camera menu and the screen need --
    def menu(self) -> dict:
        return {"address": self.addresses[0]["address"] if self.addresses else None, "addresses": list(self.addresses), "port": self.port,
                "user": self.user, "password": self.password, "passive": True, "secure": bool(self.ssl_ctx),
                "certificate": str(self.cert) if self.cert else None}

    def status(self) -> dict:
        with self.lock:
            uploads = [{"who": u.who, "name": u.name, "bytes": u.done} for u in self.uploads.values()]
            bodies = [{"address": k, **v} for k, v in self.bodies.items()]
            connected = {c.peer for c in self.controls if c.authed}
        for b in bodies:
            b["connected"] = b["address"] in connected
        parts = []
        for p in (self.stage.glob("*.part") if self.stage.is_dir() else ()):
            try:
                parts.append(p.stat().st_size)
            except OSError:
                pass                          # landed between the listing and the look
        return {**self.menu(), "running": bool(self.thread) and not self.stopping.is_set(), "bodies": bodies, "uploads": uploads, "landed": self.landed,
                "landed_bytes": self.landed_bytes, "repeats": self.repeats, "kept": len(parts), "kept_bytes": sum(parts),
                "last_file": self.last_file, "refused_logins": self.refused_logins, "active_mode": self.active_mode,
                "low_space": self.low_space, "not_implemented": dict(self.not_implemented), "connected": bool(connected),
                "last_connected": self.last_connected, "last_frame": self.last_frame, "draining": self.draining}

    def command_log(self) -> list[str]:
        """Every command every body sent, passwords masked. The first shoot with a new body is worth saving: no manual
        documents the sequence a camera actually sends."""
        return list(self.log)

    # -- called from the control threads --
    def _log(self, peer: str, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {peer} {line}")

    def _gone(self, c: _Control) -> None:
        with self.lock:
            self.controls.discard(c)

    def _locked(self, peer: str) -> bool:
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.fails.get(peer, []) if now - t < LOCK_S]
            self.fails[peer] = recent
        return len(recent) >= LOCK_AFTER

    def _failed_login(self, peer: str, user: str) -> None:
        with self.lock:
            self.fails.setdefault(peer, []).append(time.monotonic())
            self.refused_logins += 1
        self.on_event("login_failed", who=peer, user=user[:32])

    def _login(self, peer: str, host: str) -> None:
        with self.lock:
            self.fails.pop(peer, None)
            self.last_connected = time.time()
            self.bodies.setdefault(peer, {"frames": 0, "last_seen": None})["last_seen"] = time.time()
        self.on_event("login", who=peer, host=host, port=self.port)    # where this camera really reached us: what its menu holds

    def _note_active(self, peer: str) -> None:
        if not self.active_mode:
            self.active_mode = True
            self.on_event("active_mode", who=peer)

    def _note_refused(self, peer: str, cmd: str) -> None:
        self.not_implemented[cmd[:16]] += 1
        self.on_event("refused", who=peer, command=cmd[:16])

    def _note_low_space(self, free: int) -> None:
        if not self.low_space:
            self.low_space = True
            self.on_event("low_space", free=free)

    # -- staged files. Named so a stranded one still says what it is, and keyed so a resume finds it across a restart --
    @staticmethod
    def _key(peer: str, v: str) -> str:
        return hashlib.sha1(f"{peer}|{v}".encode("utf-8", "replace")).hexdigest()[:12]

    def _parts(self, peer: str, v: str) -> list[Path]:
        if not self.stage.is_dir():
            return []
        key = self._key(peer, v)
        found = [p for p in self.stage.iterdir() if p.name.endswith(".part") and f".{key}." in p.name]
        return sorted(found, key=lambda p: p.stat().st_mtime)

    def _partial(self, peer: str, v: str) -> Path | None:
        with self.lock:
            idle = [p for p in self._parts(peer, v) if str(p) not in self.uploads]
        return idle[-1] if idle else None

    def _new_part(self, peer: str, v: str) -> Path:
        base = re.sub(r"[^\w.-]", "_", posixpath.basename(v))[:80]
        while True:
            p = self.stage / f"{base}.{self._key(peer, v)}.{uuid.uuid4().hex[:6]}.part"
            try:
                os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
                return p
            except FileExistsError:
                continue

    def _open_part(self, peer: str, v: str, real: Path, rest: int, append: bool) -> tuple[Path, int]:
        """Where these bytes go, and from what offset. A plain STOR is always a fresh file, even when a stale connection of the
        same body still holds the last try. REST and APPE continue what this body left, and never touch a landed
        frame: resuming one that already landed starts from a copy of it."""
        if not rest and not append:
            return self._new_part(peer, v), 0
        with self.lock:
            busy = [p for p in self._parts(peer, v) if str(p) in self.uploads]
            idle = [p for p in self._parts(peer, v) if str(p) not in self.uploads]
            if busy and not idle:
                raise Refused(450, "That frame is still arriving on another connection.")
            part = idle[-1] if idle else None
            if part:
                self.uploads[str(part)] = _Upload(peer, v.lstrip("/"), 0)   # claimed before the lock goes, so two resumes cannot share it
        if part is None:
            if real.is_file():
                part = self._new_part(peer, v)
                shutil.copyfile(real, part)
            elif append:
                return self._new_part(peer, v), 0
            else:
                raise Refused(550, "Nothing to resume. Send the whole file.")
        size = os.path.getsize(part)
        offset = size if append and not rest else rest
        if offset > size:
            self._release(part)
            raise Refused(550, f"Only {size} bytes are here.")
        return part, offset

    def _release(self, part: Path) -> None:
        with self.lock:
            self.uploads.pop(str(part), None)

    def _kept(self, peer: str, v: str, why: str, size: int) -> None:
        self.on_event("kept", who=peer, name=v.lstrip("/"), why=why, bytes=size)

    def _failed_twice(self, peer: str, v: str, size: int) -> bool:
        with self.lock:
            again = self.failed.get((peer, v)) == size
            self.failed[(peer, v)] = size
        return again

    def _land(self, part: Path, v: str, peer: str, suspect: bool = False, expected: int | None = None) -> tuple[Path, bool]:
        """A frame interrupted and sent again comes back as DSC00001_1.ARW (Sony and Canon both say so), which would part it
        from its JPEG. If this body left a partial of the plain name and nothing landed under it, it is that frame."""
        target = v
        m = _RESENT.match(Path(v).stem)
        if m:
            base = posixpath.join(posixpath.dirname(v), m.group(1) + Path(v).suffix)
            if self._partial(peer, base) and not (self.root / base.lstrip("/")).exists():
                target = base
        size = os.path.getsize(part)
        final, repeat = self.lander.land(part, target.lstrip("/"), peer)
        with self.lock:
            active = set(self.uploads)
        for name in {v, target}:
            for old in self._parts(peer, name):
                if old != part and str(old) not in active:   # never one still being written, even by a connection that looks dead
                    try:                     # superseded, and the frame it was part of is safe. Kept, out of the count.
                        os.rename(old, old.with_suffix(".stale"))
                    except OSError:
                        pass
        rel = str(final.relative_to(self.root))
        with self.lock:
            if repeat:
                self.repeats += 1
            else:
                self.landed += 1
                self.landed_bytes += size
                self.last_file = rel
                self.last_frame = time.time()
                body = self.bodies.setdefault(peer, {"frames": 0, "last_seen": None})
                body["frames"] += 1
                body["last_seen"] = time.time()
            self.last_moved = time.monotonic()
        if suspect and not repeat:           # before landed: the watcher must already know when it reaches this file
            self.on_event("suspect", who=peer, name=rel, bytes=size, expected=expected)
        self.on_event("repeat" if repeat else "landed", who=peer, name=rel, bytes=size)
        return final, repeat


# ===== USB, through ImageCaptureCore =====

def usb_available() -> tuple[bool, str | None]:
    try:
        import ImageCaptureCore  # noqa: F401
        return True, None
    except ImportError:
        return False, "USB needs pyobjc-framework-ImageCaptureCore, which this install does not have."


class UsbCore:
    """What the helper decides, kept apart from the Objective-C so it can be tested with no camera. The delegate below only
    forwards. Frames on the card before the session opened are never fetched: a shoot starts now. Nothing on the card is ever
    deleted."""

    def __init__(self, root: Path, say: Emit, fetch: Callable[[object, str], None]):
        self.lander = Lander(root)
        self.say, self.fetch = say, fetch
        self.lander.stage.mkdir(exist_ok=True)
        self.ready = False
        self.waiting: dict[str, object] = {}

    def camera_found(self, name: str, capabilities: list[str], transport: str | None) -> None:
        self.say("camera", name=name, transport=transport, capabilities=capabilities,
                 shutter_on_camera="ICCameraDeviceCanTakePictureUsingShutterReleaseOnCamera" in capabilities or any("ShutterRelease" in c for c in capabilities),
                 remote_shutter=any(c.endswith("CanTakePicture") for c in capabilities))

    def catalog_done(self, files: int) -> None:
        self.ready = True
        self.say("ready", on_card=files)

    def items_added(self, items: list) -> None:
        if not self.ready:
            return                           # the card's old contents, arriving one by one
        for item in items:
            tmp = f"usb-{uuid.uuid4().hex[:12]}{Path(item.name()).suffix}"
            self.waiting[tmp] = item
            self.fetch(item, tmp)

    def tmp_for(self, item: object) -> str | None:
        return next((t for t, i in self.waiting.items() if i == item), None)

    def downloaded(self, tmp: str, error: str | None, saved: str | None = None) -> None:
        item = self.waiting.pop(tmp, None)
        name = os.path.basename(str(item.name())).lstrip(".") if item is not None else ""
        name = name or tmp                   # the name comes off the camera, so it is treated like one from the network
        path = self.lander.stage / (saved or tmp)
        if error or not path.is_file():
            self.say("failed", name=name, why=error or "nothing arrived")
            return
        with open(path, "rb+") as f:
            _flush(f.fileno())
        final, repeat = self.lander.land(path, name, "usb")
        self.say("repeat" if repeat else "landed", name=str(final.relative_to(self.lander.root)), bytes=final.stat().st_size)


def usb_main(folder: str, list_only: bool = False, seconds: float | None = None) -> int:
    """The helper: one JSON object a line on stdout. Its own process, because it needs the main thread's run loop, a crash in the
    bridge must not take a shoot down, and a stuck camera can then simply be killed. Opens no window."""
    def say(kind: str, **data) -> None:
        print(json.dumps({"event": kind, **data}), flush=True)

    ok, why = usb_available()
    if not ok:
        say("unavailable", why=why)
        return 3
    import objc
    import ImageCaptureCore as IC
    from Foundation import NSDate, NSDictionary, NSNumber, NSObject, NSRunLoop, NSURL

    core: UsbCore | None = None if list_only else UsbCore(Path(folder), say, lambda item, tmp: None)
    found: list = []

    class Delegate(NSObject):
        # ICDeviceBrowserDelegate
        def deviceBrowser_didAddDevice_moreComing_(self, browser, device, more):
            caps = [str(c) for c in (device.capabilities() or [])]
            found.append(device)
            if core:
                core.camera_found(str(device.name()), caps, str(device.transportType()))
            else:
                say("camera", name=str(device.name()), transport=str(device.transportType()), capabilities=caps)
            if not list_only:
                device.setDelegate_(self)
                device.requestOpenSession()

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, more):
            if device in found:
                found.remove(device)
            say("gone", name=str(device.name()))
            if not found:
                say("waiting")

        def deviceBrowserDidEnumerateLocalDevices_(self, browser):
            if not found:
                say("waiting")

        # ICDeviceDelegate
        def device_didOpenSessionWithError_(self, device, error):
            if error:
                say("session_failed", name=str(device.name()), why=str(error.localizedDescription()))

        def device_didCloseSessionWithError_(self, device, error):
            say("session_closed", name=str(device.name()))

        def didRemoveDevice_(self, device):
            pass

        # ICCameraDeviceDelegate
        def deviceDidBecomeReadyWithCompleteContentCatalog_(self, device):
            core.catalog_done(len(device.mediaFiles() or []))

        def cameraDevice_didAddItems_(self, camera, items):
            core.items_added([i for i in items if i.isKindOfClass_(IC.ICCameraFile)])

        def cameraDevice_didRemoveItems_(self, camera, items):
            pass

        def cameraDevice_didRenameItems_(self, camera, items):
            pass

        def cameraDevice_didReceiveThumbnail_forItem_error_(self, camera, thumb, item, error):
            pass

        def cameraDevice_didReceiveMetadata_forItem_error_(self, camera, meta, item, error):
            pass

        def cameraDeviceDidChangeCapability_(self, camera):
            pass

        def cameraDeviceDidRemoveAccessRestriction_(self, device):
            pass

        def cameraDeviceDidEnableAccessRestriction_(self, device):
            pass

        def cameraDevice_didReceivePTPEvent_(self, camera, data):
            # how a body in PC Remote mode will show itself, if it gives no didAddItems. Evidence for the first real test.
            say("ptp_event", hex=bytes(data)[:32].hex())

        def cameraDevice_shouldGetThumbnailOfItem_(self, camera, item):
            return False

        def cameraDevice_shouldGetMetadataOfItem_(self, camera, item):
            return False

        # ICCameraDeviceDownloadDelegate, by selector
        @objc.typedSelector(b"v@:@@@^v")
        def didDownloadFile_error_options_contextInfo_(self, file, error, options, context):
            tmp = options.get(IC.ICSaveAsFilename) or core.tmp_for(file)
            saved = options.get(IC.ICSavedFilename)
            core.downloaded(str(tmp), str(error.localizedDescription()) if error else None, str(saved) if saved else None)

    delegate = Delegate.alloc().init()

    def fetch(item, tmp: str) -> None:
        options = NSDictionary.dictionaryWithDictionary_({IC.ICDownloadsDirectoryURL: NSURL.fileURLWithPath_(str(core.lander.stage)),
                                                          IC.ICSaveAsFilename: tmp, IC.ICOverwrite: NSNumber.numberWithBool_(False),
                                                          IC.ICDeleteAfterSuccessfulDownload: NSNumber.numberWithBool_(False)})
        item.device().requestDownloadFile_options_downloadDelegate_didDownloadSelector_contextInfo_(item, options, delegate, b"didDownloadFile:error:options:contextInfo:", None)

    if core:
        core.fetch = fetch
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    browser = IC.ICDeviceBrowser.alloc().init()
    browser.setDelegate_(delegate)
    browser.setBrowsedDeviceTypeMask_(IC.ICDeviceTypeMaskCamera | IC.ICDeviceLocationTypeMaskLocal)
    browser.start()
    end = time.monotonic() + seconds if seconds else None
    loop = NSRunLoop.currentRunLoop()
    while not stop.is_set() and (end is None or time.monotonic() < end):
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
    for d in list(found):
        if d.hasOpenSession():
            d.requestCloseSession()
    browser.stop()
    loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.3))
    return 0


class UsbReceiver:
    """Runs the helper and keeps its last word. on_event gets the helper's events as they come: unavailable, waiting, camera, ready,
    landed, repeat, failed, gone, session_failed, session_closed, ptp_event."""

    def __init__(self, folder: str | os.PathLike, on_event: Emit | None = None, python: str | None = None):
        self.folder = str(folder)
        self.on_event = on_event or (lambda kind, **data: None)
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self.state: dict = {"state": "off"}
        self.cameras: list[dict] = []
        self.landed = 0
        self.thread: threading.Thread | None = None

    def start(self) -> dict:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")])}
        self.proc = subprocess.Popen([self.python, "-m", "photosort.tether", "usb", self.folder], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.state = {"state": "starting"}
        self.thread = threading.Thread(target=self._read, daemon=True, name="usb-helper")
        self.thread.start()
        return self.state

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.pop("event", "")
            if kind == "camera":
                self.cameras.append(msg)
                self.state = {"state": "connected", "camera": msg.get("name")}
            elif kind in ("waiting", "unavailable"):
                self.cameras, self.state = [], {"state": kind, **msg}
            elif kind == "landed":
                self.landed += 1
            self.on_event(kind, **msg)
        code = self.proc.wait()
        if self.state.get("state") != "unavailable":
            self.state = {"state": "off", **({"exit": code} if code else {})}

    def status(self) -> dict:
        return {**self.state, "cameras": list(self.cameras), "landed": self.landed}

    def stop(self) -> dict:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread:
            self.thread.join(2)
        return self.status()


def main(argv: list[str] | None = None) -> int:
    """`python -m photosort.tether ftp <folder>` to try a body before Live is wired, `usb <folder>` or `usb --list` for the helper."""
    ap = argparse.ArgumentParser(prog="photosort.tether")
    sub = ap.add_subparsers(dest="what", required=True)
    u = sub.add_parser("usb")
    u.add_argument("folder", nargs="?", default=".")
    u.add_argument("--list", action="store_true")
    u.add_argument("--seconds", type=float)
    f = sub.add_parser("ftp")
    f.add_argument("folder")
    f.add_argument("--port", type=int, default=FTP_PORT)
    a = ap.parse_args(argv)
    if a.what == "usb":
        return usb_main(a.folder, a.list, a.seconds or (5 if a.list else None))
    r = FtpReceiver(a.folder, port=a.port, on_event=lambda k, **d: print(k, d, flush=True))
    print(json.dumps(r.start(), indent=1), flush=True)
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    print(json.dumps(r.stop(), indent=1, default=str))
    print("\n".join(r.command_log()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
