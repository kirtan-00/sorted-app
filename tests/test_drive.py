"""Google Drive destination, against a fake Drive service. No network, no sign-in, no real upload:
the fake speaks the googleapiclient chain (files().get(...).execute()) and raises real HttpErrors."""
import json, os, re, stat, time
from pathlib import Path
import pytest
from PIL import Image

FOLDER_MIME = "application/vnd.google-apps.folder"


def _http_error(status: int):
    import httplib2
    from googleapiclient.errors import HttpError
    return HttpError(httplib2.Response({"status": status}), b"fake failure")


class _Req:
    def __init__(self, fn):
        self._fn = fn
    def execute(self, **kw):
        return self._fn()


class FakeMedia:
    """Stand-in for googleapiclient.http.MediaFileUpload: remembers the path and its bytes."""
    def __init__(self, filename, mimetype=None, chunksize=-1, resumable=False):
        self.path = Path(filename); self.mimetype = mimetype
        self.chunksize = chunksize; self.resumable = resumable
        self.bytes = self.path.read_bytes()


class FakeService:
    """A Drive v3 look-alike. records: id -> {name, mimeType, parents, owners, driveId, trashed}.
    uploads: every files().create with media, in order. fail[name] is a queue of HTTP statuses the
    next create of that name raises, popped one per attempt. slow adds a sleep per upload."""
    def __init__(self, me="owner@example.com", limit=10 ** 12, usage=0):
        self.me = me; self.limit = limit; self.usage = usage
        self.records: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.fail: dict[str, list[int]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.slow = 0.0
        self._n = 0

    def add_folder(self, fid, name, owner=None, drive_id=None, parent=None):
        self.records[fid] = {"id": fid, "name": name, "mimeType": FOLDER_MIME, "parents": [parent] if parent else [],
                             "owners": [{"emailAddress": owner or self.me}], "driveId": drive_id, "trashed": False}
        return fid

    def add_file(self, fid, name, parent, owner=None):
        self.records[fid] = {"id": fid, "name": name, "mimeType": "image/jpeg", "parents": [parent],
                             "owners": [{"emailAddress": owner or self.me}], "driveId": None, "trashed": False}
        return fid

    def _new_id(self):
        self._n += 1
        return f"id{self._n:03d}"

    def files(self):
        return _Files(self)

    def about(self):
        return _About(self)


class _Files:
    def __init__(self, svc):
        self.svc = svc

    def get(self, fileId, **kw):
        self.svc.calls.append(("get", dict(kw, fileId=fileId)))
        def run():
            rec = self.svc.records.get(fileId)
            if rec is None:
                raise _http_error(404)
            out = dict(rec)
            if rec.get("driveId") is None:
                out.pop("driveId")
            return out
        return _Req(run)

    def list(self, q="", **kw):
        self.svc.calls.append(("list", dict(kw, q=q)))
        def run():
            parent = re.search(r"'([^']*)' in parents", q)
            name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            want_name = name.group(1).replace("\\'", "'").replace("\\\\", "\\") if name else None
            out = []
            for rec in self.svc.records.values():
                if rec["trashed"]: continue
                if parent and parent.group(1) not in rec["parents"]: continue
                if want_name is not None and rec["name"] != want_name: continue
                if "mimeType = '" in q and rec["mimeType"] != re.search(r"mimeType = '([^']*)'", q).group(1): continue
                out.append({"id": rec["id"], "name": rec["name"]})
            return {"files": out}
        return _Req(run)

    def create(self, body=None, media_body=None, **kw):
        self.svc.calls.append(("create", dict(kw, body=body, media=media_body is not None)))
        def run():
            name = body["name"]
            queue = self.svc.fail.get(name)
            if queue:
                item = queue.pop(0)
                raise item if isinstance(item, BaseException) else _http_error(item)
            if self.svc.slow:
                time.sleep(self.svc.slow)
            fid = self.svc._new_id()
            parent = (body.get("parents") or [None])[0]
            self.svc.records[fid] = {"id": fid, "name": name, "mimeType": body.get("mimeType", media_body.mimetype if media_body else "application/octet-stream"),
                                     "parents": [parent], "owners": [{"emailAddress": self.svc.me}], "driveId": None, "trashed": False}
            if media_body is not None:
                self.svc.uploads.append({"id": fid, "name": name, "parent": parent, "bytes": media_body.bytes,
                                         "mimetype": media_body.mimetype, "resumable": media_body.resumable})
            return {"id": fid, "name": name}
        return _Req(run)


class _About:
    def __init__(self, svc):
        self.svc = svc

    def get(self, fields=""):
        self.svc.calls.append(("about", {"fields": fields}))
        def run():
            quota = {"usage": str(self.svc.usage)}
            if self.svc.limit is not None:
                quota["limit"] = str(self.svc.limit)
            return {"user": {"emailAddress": self.svc.me}, "storageQuota": quota}
        return _Req(run)


@pytest.fixture
def fake(monkeypatch):
    """A FakeService wired into photosort.drive: no network, no sleeps, MediaFileUpload replaced."""
    from photosort import drive
    svc = FakeService()
    svc.add_folder("root1", "Client delivery")
    monkeypatch.setattr(drive, "_build_service", lambda: svc)
    monkeypatch.setattr(drive, "MediaFileUpload", FakeMedia)
    monkeypatch.setattr(drive, "_sleep", lambda s: None)
    return svc


def fake_token(email="owner@example.com"):
    """A token file that reads as signed in without any network: no expiry, so nothing refreshes
    until a real service is built, and tests always replace that."""
    from photosort import drive
    p = drive.token_path(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                             "scopes": drive.SCOPES, "email": email}))
    os.chmod(p, 0o600)
    return p


# links

def test_parse_folder_link_accepts_every_folder_shape():
    from photosort.drive import parse_folder_link
    fid = "1AbC_dEf-GhIjKlMnOpQrStUvWxYz012"
    for link in [f"https://drive.google.com/drive/folders/{fid}",
                 f"https://drive.google.com/drive/folders/{fid}?usp=sharing",
                 f"https://drive.google.com/drive/u/0/folders/{fid}",
                 f"https://drive.google.com/drive/u/1/folders/{fid}?usp=drive_link",
                 f"https://drive.google.com/open?id={fid}",
                 f"https://drive.google.com/drive/folders?id={fid}",
                 f"  {fid}  ",
                 f"drive.google.com/drive/folders/{fid}/"]:
        assert parse_folder_link(link) == fid, link


def test_parse_folder_link_rejects_documents_and_junk():
    from photosort.drive import parse_folder_link
    for link in ["https://docs.google.com/document/d/1abcdefghijklmnop/edit",
                 "https://docs.google.com/spreadsheets/d/1abcdefghijklmnop/edit#gid=0",
                 "https://docs.google.com/presentation/d/1abcdefghijklmnop/edit",
                 "https://drive.google.com/file/d/1abcdefghijklmnop/view?usp=sharing"]:
        with pytest.raises(ValueError, match="document link, paste a folder link"):
            parse_folder_link(link)
    for link in ["https://example.com/folders/1abcdefghijklmnop", "", "not a link", "https://drive.google.com/drive/my-drive"]:
        with pytest.raises(ValueError, match="not a Google Drive link"):
            parse_folder_link(link)


# credentials on disk

def test_sign_in_state_and_sign_out(tmp_path):
    from photosort import drive
    assert drive.client_path().name == "google_client.json" and drive.token_path().name == "google_token.json"
    assert not drive.is_signed_in() and drive.signed_in_email() is None
    with pytest.raises(drive.NotSignedIn, match="sign in to Google first"):
        drive.inspect_folder("root1")
    with pytest.raises(drive.NoClientConfig, match="google_client.json"):
        drive.sign_in()
    p = fake_token("me@example.com")
    assert drive.is_signed_in() and drive.signed_in_email() == "me@example.com"
    drive.sign_out()
    assert not p.exists() and not drive.is_signed_in()
    drive.sign_out()      # twice is fine


def test_token_is_written_private(tmp_path):
    from photosort import drive
    from google.oauth2.credentials import Credentials
    creds = Credentials(token="t", refresh_token="r", token_uri="https://oauth2.googleapis.com/token",
                        client_id="c", client_secret="s", scopes=drive.SCOPES)
    drive._save_token(creds, "me@example.com")
    p = drive.token_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    d = json.loads(p.read_text())
    assert d["refresh_token"] == "r" and d["email"] == "me@example.com"
    assert drive.is_signed_in()


# folders

def test_inspect_folder_reports_owner_and_quota(fake):
    from photosort import drive
    fake_token()
    fake.limit = 100; fake.usage = 40
    info = drive.inspect_folder("root1")
    assert info == {"id": "root1", "name": "Client delivery", "owner_email": "owner@example.com", "shared_drive": False,
                    "quota_applies": True, "free_bytes": 60}
    fake.add_folder("theirs", "Their folder", owner="client@example.com")
    info = drive.inspect_folder("theirs")           # the client's My Drive folder: what we upload is ours, our quota
    assert info["owner_email"] == "client@example.com" and info["quota_applies"] is True and info["free_bytes"] == 60
    fake.add_folder("sd1", "Team folder", drive_id="0ADriveId")
    info = drive.inspect_folder("sd1")
    assert info["shared_drive"] is True and info["quota_applies"] is False and info["free_bytes"] is None
    fake.limit = None
    assert drive.inspect_folder("root1")["free_bytes"] is None
    with pytest.raises(ValueError, match="no folder with that id"):
        drive.inspect_folder("nope")
    fake.add_file("f1", "a.jpg", "root1")
    with pytest.raises(ValueError, match="not a folder"):
        drive.inspect_folder("f1")
    assert all(kw.get("supportsAllDrives") for m, kw in fake.calls if m == "get")


def test_ensure_subfolder_finds_then_creates(fake):
    from photosort import drive
    fake_token()
    existing = fake.add_folder("b1", "beach", parent="root1")
    assert drive.ensure_subfolder("root1", "beach") == existing
    assert not [c for c in fake.calls if c[0] == "create"]
    new = drive.ensure_subfolder("root1", "Meera's day")
    assert new != existing and fake.records[new]["parents"] == ["root1"] and fake.records[new]["mimeType"] == FOLDER_MIME
    assert drive.ensure_subfolder("root1", "Meera's day") == new       # second call finds it
    q = [kw["q"] for m, kw in fake.calls if m == "list"][-1]
    assert "Meera\\'s day" in q and "trashed = false" in q
    assert all(kw.get("supportsAllDrives") and kw.get("includeItemsFromAllDrives") for m, kw in fake.calls if m == "list")
    nested = drive.ensure_path("root1", "categories/beach")
    cats = fake.records[nested]["parents"][0]
    assert fake.records[cats]["name"] == "categories" and fake.records[nested]["name"] == "beach"
    assert drive.ensure_path("root1", "categories/beach") == nested


# uploads

def _three_files(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    (tmp_path / "day2").mkdir(); make_image(tmp_path / "day2", "c.jpg", seed=3)
    jobs = [(1, "a.jpg", "categories/beach"), (2, "b.jpg", "categories/beach"), (3, "day2/c.jpg", "people/Meera")]
    return jobs, sorted(os.listdir(tmp_path))


def test_upload_files_lands_in_subfolders_and_writes_the_manifest(fake, tmp_path):
    from photosort import drive
    fake_token()
    jobs, before = _three_files(tmp_path)
    seen = []
    res = drive.upload_files(tmp_path, "root1", jobs, progress=seen.append)
    assert res["done"] == 3 and res["failed"] == 0 and res["skipped"] == 0 and res["failures"] == []
    assert res["bytes"] == sum(len(u["bytes"]) for u in fake.uploads) > 0
    assert [u["name"] for u in fake.uploads] == ["a.jpg", "b.jpg", "c.jpg"]
    by_name = {u["name"]: u for u in fake.uploads}
    beach = by_name["a.jpg"]["parent"]
    assert by_name["b.jpg"]["parent"] == beach and fake.records[beach]["name"] == "beach"
    assert fake.records[fake.records[beach]["parents"][0]]["name"] == "categories"
    meera = by_name["c.jpg"]["parent"]
    assert fake.records[meera]["name"] == "Meera" and fake.records[fake.records[meera]["parents"][0]]["name"] == "people"
    assert by_name["a.jpg"]["bytes"] == (tmp_path / "a.jpg").read_bytes()
    assert by_name["a.jpg"]["mimetype"] == "image/jpeg" and by_name["a.jpg"]["resumable"]
    assert all(kw.get("supportsAllDrives") for m, kw in fake.calls if m == "create")
    assert seen[-1] == {"done": 3, "total": 3, "failed": 0, "skipped": 0, "bytes": res["bytes"], "current": "day2/c.jpg"}
    assert all(set(d) == {"done", "total", "failed", "skipped", "bytes", "current"} for d in seen)
    m = drive.manifest_path(tmp_path, "root1")
    assert m.is_file() and m.parent.name == "drive-uploads" and m.name.endswith("-root1.json")
    data = json.loads(m.read_text())
    assert data["folder"] == "root1" and len(data["files"]) == 3
    assert {v["id"] for v in data["files"].values()} == {u["id"] for u in fake.uploads}
    assert sorted(os.listdir(tmp_path)) == before


def test_upload_files_second_run_skips_and_reuploads_a_vanished_file(fake, tmp_path):
    from photosort import drive
    fake_token()
    jobs, before = _three_files(tmp_path)
    drive.upload_files(tmp_path, "root1", jobs)
    n = len(fake.uploads)
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["skipped"] == 3 and res["done"] == 3 and len(fake.uploads) == n
    gone = next(u["id"] for u in fake.uploads if u["name"] == "b.jpg")
    del fake.records[gone]                        # deleted on Drive since
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["skipped"] == 2 and res["failed"] == 0 and len(fake.uploads) == n + 1 and fake.uploads[-1]["name"] == "b.jpg"
    data = json.loads(drive.manifest_path(tmp_path, "root1").read_text())
    assert fake.uploads[-1]["id"] in {v["id"] for v in data["files"].values()} and gone not in {v["id"] for v in data["files"].values()}
    trashed = next(u["id"] for u in fake.uploads if u["name"] == "a.jpg")
    fake.records[trashed]["trashed"] = True       # binned on Drive: also gone
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["skipped"] == 2 and fake.uploads[-1]["name"] == "a.jpg"
    (tmp_path / "a.jpg").write_bytes((tmp_path / "a.jpg").read_bytes() + b"x")    # changed locally: a new upload
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["skipped"] == 2 and fake.uploads[-1]["name"] == "a.jpg"
    (tmp_path / "a.jpg").write_bytes((tmp_path / "a.jpg").read_bytes()[:-1])
    assert sorted(os.listdir(tmp_path)) == before


def test_upload_files_retries_on_5xx_and_records_a_dead_file(fake, tmp_path):
    from photosort import drive
    fake_token()
    jobs, before = _three_files(tmp_path)
    fake.fail["a.jpg"] = [500, 503]
    fake.fail["b.jpg"] = [500, 500, 429, 500]
    seen = []
    res = drive.upload_files(tmp_path, "root1", jobs, progress=seen.append)
    assert [u["name"] for u in fake.uploads] == ["a.jpg", "c.jpg"]
    assert res["done"] == 3 and res["failed"] == 1 and res["skipped"] == 0
    assert len(res["failures"]) == 1 and res["failures"][0].startswith("b.jpg\t") and "500" in res["failures"][0]
    assert seen[-1]["failed"] == 1 and seen[-1]["done"] == 3
    data = json.loads(drive.manifest_path(tmp_path, "root1").read_text())
    assert len(data["files"]) == 2                 # the dead one is not remembered
    fake.fail["b.jpg"] = [403]
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["failed"] == 1 and res["skipped"] == 2 and "403" in res["failures"][0]     # a 4xx is not retried
    assert len(fake.uploads) == 2
    (tmp_path / "day2" / "c.jpg").unlink()
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["failed"] == 1 and res["failures"][0].startswith("day2/c.jpg\t")           # vanished source: a failure, not a crash
    from conftest import make_image
    make_image(tmp_path / "day2", "c.jpg", seed=3)
    assert sorted(os.listdir(tmp_path)) == before


def test_upload_files_retries_transport_errors_and_survives_a_surprise(fake, tmp_path):
    import httplib2
    from photosort import drive
    fake_token()
    jobs, before = _three_files(tmp_path)
    fake.fail["a.jpg"] = [httplib2.ServerNotFoundError("Unable to find the server"), ConnectionResetError("reset")]
    fake.fail["b.jpg"] = [TimeoutError("t"), TimeoutError("t"), TimeoutError("t"), TimeoutError("t")]
    fake.fail["c.jpg"] = [RuntimeError("something the fake never saw coming")]
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert [u["name"] for u in fake.uploads] == ["a.jpg"]
    assert res["done"] == 3 and res["failed"] == 2
    assert res["failures"][0].startswith("b.jpg\tconnection failed") and res["failures"][1].startswith("day2/c.jpg\tsomething")
    assert sorted(os.listdir(tmp_path)) == before


def test_dead_refresh_token_reads_as_signed_out(tmp_path, monkeypatch):
    from photosort import drive
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    p = fake_token("me@example.com")
    d = json.loads(p.read_text()); d["expiry"] = "2020-01-01T00:00:00Z"; p.write_text(json.dumps(d))
    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")
    monkeypatch.setattr(Credentials, "refresh", refuse)
    with pytest.raises(drive.NotSignedIn):
        drive._load_creds()
    assert not p.exists() and not drive.is_signed_in()


def _oriented_photo(tmp_path, name="big.jpg", size=(4000, 3000)):
    """A landscape file tagged orientation 6 (rotate 90 CW to view), so the viewed image is portrait."""
    import numpy as np
    rng = np.random.default_rng(7)
    arr = (rng.random((size[1] // 8, size[0] // 8, 3)) * 255).astype("uint8")
    im = Image.fromarray(arr).resize(size, Image.NEAREST)
    exif = Image.Exif(); exif[0x0112] = 6; exif[0x010F] = "TestCam"
    p = tmp_path / name; im.save(p, quality=90, exif=exif.tobytes())
    return p


def test_upload_files_web_size_shrinks_and_orients_photos_only(fake, tmp_path):
    import io
    from photosort import drive
    from conftest import make_video, needs_ffmpeg, FFMPEG
    fake_token()
    _oriented_photo(tmp_path)
    (tmp_path / "raw.ARW").write_bytes(b"raw bytes, never decoded")
    clip = None
    if FFMPEG:
        clip = make_video(tmp_path / "clip.mp4", scenes=1, work=tmp_path.parent / "work")
    before = sorted(os.listdir(tmp_path))
    jobs = [(1, "big.jpg", "selection"), (2, "raw.ARW", "selection")] + ([(3, "clip.mp4", "selection")] if clip else [])
    res = drive.upload_files(tmp_path, "root1", jobs, web_size=1000)
    assert res["failed"] == 0 and res["done"] == len(jobs)
    by_name = {u["name"]: u for u in fake.uploads}
    im = Image.open(io.BytesIO(by_name["big.jpg"]["bytes"]))
    assert im.size == (750, 1000) and im.format == "JPEG"
    exif = im.getexif()
    assert exif.get(0x0112, 1) == 1 and exif.get(0x010F) == "TestCam"
    assert len(by_name["big.jpg"]["bytes"]) < (tmp_path / "big.jpg").stat().st_size
    assert by_name["raw.ARW"]["bytes"] == b"raw bytes, never decoded"
    if clip:
        assert by_name["clip.mp4"]["bytes"] == clip.read_bytes() and by_name["clip.mp4"]["mimetype"] == "video/mp4"
    assert "big.jpg" in by_name and all(not u["name"].endswith("_web.jpg") for u in fake.uploads)
    # web_size is part of what was uploaded: a full-size run after a web run is a fresh upload, and back
    res = drive.upload_files(tmp_path, "root1", jobs[:1])
    assert res["skipped"] == 0 and len(by_name["big.jpg"]["bytes"]) < len(fake.uploads[-1]["bytes"])
    assert drive.upload_files(tmp_path, "root1", jobs[:1], web_size=1000)["skipped"] == 1
    if clip:
        n = len(fake.uploads)
        res = drive.upload_files(tmp_path, "root1", jobs, web_size=1000, skip_videos=True)
        assert res["skipped_videos"] == 1 and res["skipped"] == 3 and res["done"] == 3 and len(fake.uploads) == n
        res = drive.upload_files(tmp_path, "root1", jobs, skip_videos=True)      # without web_size videos still go
        assert res["skipped_videos"] == 0 and fake.uploads[-1]["name"] == "clip.mp4"
    assert sorted(os.listdir(tmp_path)) == before
    assert not list(tmp_path.glob("*_web*")) and not list(tmp_path.glob(".*"))


def test_upload_files_keeps_two_sources_with_one_name_apart(fake, tmp_path):
    from photosort import drive
    from conftest import make_image
    fake_token()
    (tmp_path / "day1").mkdir(); (tmp_path / "day2").mkdir()
    make_image(tmp_path / "day1", "a.jpg", seed=1); make_image(tmp_path / "day2", "a.jpg", seed=2)
    jobs = [(1, "day1/a.jpg", "selection"), (2, "day2/a.jpg", "selection")]
    res = drive.upload_files(tmp_path, "root1", jobs)
    assert res["done"] == 2 and res["failed"] == 0
    assert sorted(u["name"] for u in fake.uploads) == ["2_a.jpg", "a.jpg"]
    assert drive.upload_files(tmp_path, "root1", jobs)["skipped"] == 2


def test_upload_files_empty_job_list_reports_zero(fake, tmp_path):
    from photosort import drive
    fake_token()
    seen = []
    res = drive.upload_files(tmp_path, "root1", [], progress=seen.append)
    assert res["done"] == 0 and res["total"] == 0 and seen == [{"done": 0, "total": 0, "failed": 0, "skipped": 0, "bytes": 0, "current": None}]


# preflight

def test_preflight_message_only_when_our_quota_is_the_limit(fake):
    from photosort import drive
    fake_token()
    fake.limit = 10 * 2 ** 30; fake.usage = 9 * 2 ** 30
    msg = drive.preflight("root1", 2 ** 30)
    assert msg and "GB" in msg and "free" in msg
    assert drive.preflight("root1", 100) is None
    fake.add_folder("sd1", "Team", drive_id="0ADriveId")
    assert drive.preflight("sd1", 10 ** 15) is None
    fake.add_folder("theirs", "Theirs", owner="client@example.com")
    assert drive.preflight("theirs", 10 ** 15)            # a shared My Drive folder bills the uploader
    fake.limit = None
    assert drive.preflight("root1", 10 ** 15) is None
