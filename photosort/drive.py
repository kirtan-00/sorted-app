"""Google Drive as an export destination: paste a folder link, the app uploads into it.

Credentials live under app_home(): google_client.json is the OAuth Desktop client the owner
downloads from the Google Cloud console once (docs/google-drive.md), google_token.json is the
signed-in account's refresh token (0600). Every network call goes through _build_service(), which
the tests replace with a fake, so nothing here needs a real account to be exercised.

Uploads are resumable and remembered: a manifest per shoot and folder (app_home()/drive-uploads/)
maps each placed file to its Drive id, so a re-run skips what is already there and only fills the
gaps. The shoot root is only ever read; web-size copies are made in a temp dir."""
from __future__ import annotations
import json, mimetypes, os, re, tempfile, time
from pathlib import Path
from .config import app_home, shoot_slug, STD_EXTS, VIDEO_EXTS

# Full Drive scope, not drive.file: drive.file only sees files this app created, and the whole point
# is uploading into a folder the client shared with the owner, which the app never created. openid is
# listed because Google adds it whenever userinfo.email is asked for, and oauthlib refuses a token
# whose scopes differ from the request.
SCOPES = ["https://www.googleapis.com/auth/drive", "openid", "https://www.googleapis.com/auth/userinfo.email"]
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024
RETRIES = 3                      # after the first attempt
RETRY_STATUSES = {429, 500, 502, 503, 504}
QUOTA_HEADROOM = 512 * 1024 * 1024
WEB_QUALITY = 90

try:
    from googleapiclient.http import MediaFileUpload
except ImportError:              # the module still imports (link parsing, status) without the libraries
    MediaFileUpload = None

_sleep = time.sleep              # patched to a no-op in tests


class NotSignedIn(Exception):
    def __init__(self):
        super().__init__("sign in to Google first")


class NoClientConfig(Exception):
    def __init__(self):
        super().__init__(f"put your Google OAuth client file at {client_path()}; see docs/google-drive.md")


def client_path() -> Path:
    return app_home() / "google_client.json"


def token_path() -> Path:
    return app_home() / "google_token.json"


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


# links

_ID = r"[A-Za-z0-9_-]+"
_BARE_ID = r"[A-Za-z0-9_-]{10,}"     # a bare id has to look like one; a real folder id is 25 characters or more


def parse_folder_link(text: str) -> str:
    """The folder id in a pasted Drive link: /drive/folders/<id>, /drive/u/0/folders/<id>, open?id=<id>,
    ?id=<id>, or a bare id, with or without ?usp=... A Docs, Sheets, Slides or file link is refused
    with a message that says what to paste instead."""
    s = (text or "").strip()
    if re.fullmatch(_BARE_ID, s):
        return s
    if "drive.google.com" not in s and "docs.google.com" not in s:
        raise ValueError("not a Google Drive link")
    if re.search(r"/(document|spreadsheets|presentation|file/d)/", s):
        raise ValueError("that is a document link, paste a folder link")
    m = re.search(rf"/folders/({_ID})", s) or re.search(rf"[?&]id=({_ID})", s)
    if not m:
        raise ValueError("not a Google Drive link")
    return m.group(1)


# credentials

def _read_token() -> dict | None:
    p = token_path()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not {"refresh_token", "client_id", "client_secret"} <= set(d):
        return None
    return d


def is_signed_in() -> bool:
    return _read_token() is not None


def signed_in_email() -> str | None:
    d = _read_token()
    return d.get("email") if d else None


def _save_token(creds, email: str | None) -> None:
    """The refresh token plus the account's email, private to the owner (0600)."""
    p = token_path(); p.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(creds.to_json()); d["email"] = email
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(d, indent=2))
    os.chmod(p, 0o600)


def _load_creds():
    """Credentials from the token file, refreshed (and written back) when the access token has expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    d = _read_token()
    if d is None:
        raise NotSignedIn()
    from google.auth.exceptions import RefreshError
    creds = Credentials.from_authorized_user_info(d, d.get("scopes") or SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:                  # revoked, or expired after six months idle in Testing mode
            sign_out()
            raise NotSignedIn()
        _save_token(creds, d.get("email"))
    return creds


def _build_service():
    """The Drive v3 client. The one seam to the network: tests replace it with a fake."""
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_load_creds(), cache_discovery=False)


def _email_of(service) -> str | None:
    me = service.about().get(fields="user(emailAddress)").execute()
    return (me.get("user") or {}).get("emailAddress")


def sign_in() -> dict:
    """Open the browser on Google's consent screen and keep the token. Blocks until the owner
    finishes, so the server runs it in a thread. Returns {email}."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    cp = client_path()
    if not cp.is_file():
        raise NoClientConfig()
    flow = InstalledAppFlow.from_client_secrets_file(str(cp), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    email = _email_of(build("drive", "v3", credentials=creds, cache_discovery=False))
    _save_token(creds, email)
    return {"email": email}


def sign_out() -> None:
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass


# folders

def _status(err) -> int | None:
    resp = getattr(err, "resp", None)
    return getattr(resp, "status", None)


def inspect_folder(folder_id: str) -> dict:
    """{id, name, owner_email, shared_drive, quota_applies, free_bytes} for a folder. free_bytes is the
    signed-in account's own free space. quota_applies says whether that is the limit: it is for any
    folder in a My Drive, ours or the client's, because a file uploaded into a folder someone shared
    is owned by the uploader and counts against the uploader's storage; only a Shared Drive pools
    storage with its organisation, and there free_bytes is None."""
    from googleapiclient.errors import HttpError
    svc = _build_service()
    try:
        f = svc.files().get(fileId=folder_id, fields="id,name,mimeType,owners(emailAddress),driveId",
                            supportsAllDrives=True).execute()
    except HttpError as e:
        if _status(e) == 404:
            raise ValueError("no folder with that id; check the link and that it was shared with you")
        raise
    if f.get("mimeType") != FOLDER_MIME:
        raise ValueError("that link is a file, not a folder")
    about = svc.about().get(fields="user(emailAddress),storageQuota").execute()
    me = (about.get("user") or {}).get("emailAddress")
    owner = ((f.get("owners") or [{}])[0]).get("emailAddress")
    shared_drive = bool(f.get("driveId"))
    quota_applies = not shared_drive
    free = None
    q = about.get("storageQuota") or {}
    if quota_applies and q.get("limit") is not None:        # no limit: an unlimited plan
        free = max(int(q["limit"]) - int(q.get("usage") or 0), 0)
    return {"id": f["id"], "name": f.get("name"), "owner_email": owner, "shared_drive": shared_drive,
            "quota_applies": quota_applies, "free_bytes": free}


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ensure_subfolder(parent_id: str, name: str, service=None) -> str:
    """The id of the folder called exactly `name` under parent_id, created when missing."""
    svc = service or _build_service()
    q = f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=10, supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    found = [f for f in res.get("files", []) if f.get("name") == name]
    if found:
        return found[0]["id"]
    made = svc.files().create(body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                              fields="id", supportsAllDrives=True).execute()
    return made["id"]


def ensure_path(parent_id: str, sub: str, service=None, cache: dict | None = None) -> str:
    """ensure_subfolder for a nested "categories/beach", one segment at a time. cache (sub -> id)
    keeps a run from re-listing the same folder for every file."""
    svc = service or _build_service()
    cache = cache if cache is not None else {}
    cur = parent_id; walked = []
    for seg in [s for s in sub.split("/") if s]:
        walked.append(seg); key = "/".join(walked)
        if key not in cache:
            cache[key] = ensure_subfolder(cur, seg, svc)
        cur = cache[key]
    return cur


# uploads

def manifest_path(root: Path, folder_id: str) -> Path:
    return app_home() / "drive-uploads" / f"{shoot_slug(root)}-{folder_id}.json"


def _read_manifest(p: Path, folder_id: str) -> dict:
    try:
        d = json.loads(p.read_text())
        if isinstance(d, dict) and d.get("folder") == folder_id and isinstance(d.get("files"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"folder": folder_id, "files": {}}


def _write_manifest(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)


def _still_there(svc, file_id: str) -> bool:
    """One files.get on a remembered id: gone (404) or binned means upload again. Any other error
    counts as unknown, and the upload loop's own retries take it from there."""
    from googleapiclient.errors import HttpError
    try:
        f = svc.files().get(fileId=file_id, fields="id,trashed", supportsAllDrives=True).execute()
    except HttpError:
        return False
    return not f.get("trashed", False)


def _mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _web_copy(src: Path, dst_dir: Path, web_size: int) -> Path | None:
    """A copy of a photo at most web_size px on its long edge, orientation applied, the rest of the
    EXIF kept, saved in the photo's own format (JPEG at quality 90). None when Pillow cannot read it,
    in which case the original goes up as is."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif; pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        with Image.open(src) as im:
            fmt = im.format or "JPEG"
            im = ImageOps.exif_transpose(im)
            im.thumbnail((web_size, web_size), Image.LANCZOS)
            exif = im.info.get("exif")
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = dst_dir / src.name
            kw = {"quality": WEB_QUALITY} if fmt in ("JPEG", "WEBP") else {}
            if exif:
                kw["exif"] = exif
            im.save(out, format=fmt, **kw)
            return out
    except Exception:
        return None


def _upload_once(svc, parent_id: str, name: str, path: Path) -> str:
    media = MediaFileUpload(str(path), mimetype=_mime(name), chunksize=CHUNK, resumable=True)
    made = svc.files().create(body={"name": name, "parents": [parent_id]}, media_body=media, fields="id",
                              supportsAllDrives=True).execute()
    return made["id"]


def _upload(svc, parent_id: str, name: str, path: Path) -> str:
    """One file with RETRIES retries (1, 2, 4 s) on 429, 5xx and transport trouble (a dropped
    socket, a DNS blip, a timeout). Any other HttpError is final. Raises OSError with a plain message."""
    import httplib2
    from googleapiclient.errors import HttpError
    delay = 1.0
    for attempt in range(RETRIES + 1):
        try:
            return _upload_once(svc, parent_id, name, path)
        except HttpError as e:
            st = _status(e)
            if st not in RETRY_STATUSES or attempt == RETRIES:
                raise OSError(f"Drive returned {st}" if st else f"Drive error: {e}")
        except (OSError, httplib2.HttpLib2Error) as e:
            if attempt == RETRIES:
                raise OSError(f"connection failed: {e}")
        _sleep(delay); delay *= 2
    raise OSError("upload gave up")           # unreachable


def upload_files(root: Path, folder_id: str, jobs: list[tuple[int, str, str]], web_size: int | None = None,
                 skip_videos: bool = False, progress=None, manifest: Path | None = None) -> dict:
    """The per-file loop for Drive, the twin of export.transfer_files. jobs are (photo id, rel, sub):
    root/rel goes into the subfolder `sub` ("categories/beach", "people/Meera", "selection") of
    folder_id, created on demand. A file already uploaded by an earlier run (same sub, rel, size,
    mtime and web_size, and still on Drive) is skipped; a name already taken in a subfolder by a
    different source goes up as {id}_{name}. Each failure is recorded and the run goes on. With
    web_size, photos go up shrunk to that long edge; videos and RAW files always go up as they are,
    or videos stay home when skip_videos is also set. progress sees {done, total, failed, skipped,
    bytes, current} before and after every file. Returns {done, total, failed, skipped,
    skipped_videos, failures, bytes}."""
    root = Path(root); notify = progress or (lambda d: None)
    svc = _build_service()
    mpath = manifest or manifest_path(root, folder_id)
    man = _read_manifest(mpath, folder_id)
    files: dict = man["files"]
    folders: dict[str, str] = {}
    taken: dict[str, dict[str, str]] = {}          # sub -> name -> rel, from the manifest then this run
    for k, v in files.items():
        taken.setdefault(v["sub"], {})[v["name"]] = v["rel"]
    failures: list[str] = []; skipped = 0; skipped_videos = 0; total_bytes = 0; total = len(jobs)
    web = int(web_size) if web_size else 0
    def snap(done, current):
        notify({"done": done, "total": total, "failed": len(failures), "skipped": skipped,
                "bytes": total_bytes, "current": current})
    if not jobs:
        snap(0, None)
        return {"done": 0, "total": 0, "failed": 0, "skipped": 0, "skipped_videos": 0, "failures": [], "bytes": 0}
    with tempfile.TemporaryDirectory(prefix="photosort-drive-") as tmp:
        tmp = Path(tmp)
        for n, (pid, rel, sub) in enumerate(jobs, 1):
            src = root / rel; name = Path(rel).name; ext = src.suffix.lower()
            key = f"{sub}|{rel}|{web}"
            snap(n - 1, rel)
            try:
                if ext in VIDEO_EXTS and web and skip_videos:
                    skipped += 1; skipped_videos += 1
                    continue
                st = src.stat()
                rec = files.get(key)
                if rec and rec["size"] == st.st_size and rec["mtime"] == int(st.st_mtime) and _still_there(svc, rec["id"]):
                    skipped += 1
                    continue
                names = taken.setdefault(sub, {})
                if name in names and names[name] != rel:
                    name = f"{pid}_{name}"
                path = src
                if web and ext in STD_EXTS:
                    path = _web_copy(src, tmp, web) or src
                parent = ensure_path(folder_id, sub, svc, folders)
                fid = _upload(svc, parent, name, path)
                sent = path.stat().st_size
                total_bytes += sent
                files[key] = {"id": fid, "rel": rel, "sub": sub, "name": name, "size": st.st_size,
                              "mtime": int(st.st_mtime), "web": web, "bytes": sent}
                names[name] = rel
                _write_manifest(mpath, man)
                if path != src:
                    path.unlink(missing_ok=True)
            except Exception as e:            # one file's trouble never ends the run
                files.pop(key, None)              # whatever was remembered for it is not to be trusted now
                failures.append(f"{rel}\t{e}")
            finally:
                snap(n, rel)
    _write_manifest(mpath, man)
    return {"done": total, "total": total, "failed": len(failures), "skipped": skipped,
            "skipped_videos": skipped_videos, "failures": failures, "bytes": total_bytes}


def preflight(folder_id: str, total_bytes: int) -> str | None:
    """A message when the signed-in account's own quota is the limit (any My Drive folder, ours or
    the client's) and cannot take total_bytes plus QUOTA_HEADROOM; None when it fits, when the plan
    has no limit, or when the folder is on a Shared Drive (its organisation's storage)."""
    info = inspect_folder(folder_id)
    free = info["free_bytes"]
    if not info["quota_applies"] or free is None:
        return None
    if total_bytes + QUOTA_HEADROOM > free:
        return (f"upload needs {total_bytes / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free in your Google Drive. "
                "Free some space, use the web-size option, or upload fewer photos.")
    return None
