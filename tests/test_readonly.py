"""The shoot disk is only ever read. Proved by manifest, not by assumption.

The standing rule is that a client's card is read and never written, with Reorganise as the single
guarded exception (it is not exercised here: it is the one pass whose whole job is to move files, and
it is off unless asked). Everything else - the scan, the focus pass, the places pass, an export - must
leave the shoot byte-identical.

test_index.py already asserted that the top-level file NAMES are unchanged after a scan. That would
not notice a file rewritten in place, an mtime moved by a failed read, a sidecar dropped in a
subfolder, or anything at all done by the other three passes. So the check here is a full manifest:
every file at any depth, its size, its mtime to the nanosecond, and a sha256 of its bytes.

Faces and video are ON. Both write more than a plain photo scan (frames, segment rows, face crops)
and are therefore the passes most likely to reach for the source folder as scratch space.
"""
import hashlib
import os
from pathlib import Path

import pytest

from photosort import db, focus, places
from photosort.index import index_folder


def _manifest(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under root: relative path -> (size, mtime_ns, sha256). Directories are captured
    implicitly by the paths, and an added or removed file changes the key set."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns,
                                             hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _diff(before: dict, after: dict) -> list[str]:
    """Human-readable, because a failure here stops a client SSD run and needs to say why."""
    bad = []
    for k in sorted(set(after) - set(before)):
        bad.append(f"ADDED {k}")
    for k in sorted(set(before) - set(after)):
        bad.append(f"REMOVED {k}")
    for k in sorted(set(before) & set(after)):
        b, a = before[k], after[k]
        if b[0] != a[0]:
            bad.append(f"SIZE {k}: {b[0]} -> {a[0]}")
        elif b[2] != a[2]:
            bad.append(f"CONTENT {k}: sha256 changed")
        elif b[1] != a[1]:
            bad.append(f"MTIME {k}: {b[1]} -> {a[1]}")
    return bad


@pytest.fixture
def shoot(tmp_path, tmp_path_factory):
    """A small shoot with the shapes that make a scan write things: sharp and blurry stills, a RAW-ish
    sibling name, an unreadable file, a subfolder, and a clip with a hard cut so segments exist."""
    from conftest import make_image, make_video, FFMPEG
    root = tmp_path / "shoot"
    (root / "day1").mkdir(parents=True)
    for i in range(4):
        make_image(root, f"p{i}.jpg", kind="sharp" if i < 3 else "blurry", seed=i)
    make_image(root / "day1", "nested.jpg", seed=9)
    (root / "junk.jpg").write_bytes(b"nope")          # the error path, at least once
    if FFMPEG:
        make_video(root / "clip.mp4", scenes=2, work=tmp_path_factory.mktemp("work"))
    return root


def test_scan_with_faces_and_video_never_writes_to_the_shoot(shoot):
    before = _manifest(shoot)
    index_folder(shoot, faces=True, workers=2)
    assert db.connect(shoot).execute(
        "SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] >= 5
    assert _diff(before, _manifest(shoot)) == []


def test_focus_pass_never_writes_to_the_shoot(shoot):
    index_folder(shoot, faces=True, workers=2)
    before = _manifest(shoot)
    focus.check_focus(shoot)
    assert _diff(before, _manifest(shoot)) == []


def test_places_pass_never_writes_to_the_shoot(shoot):
    index_folder(shoot, faces=False, workers=2)
    before = _manifest(shoot)
    places.read_locations(shoot)
    assert _diff(before, _manifest(shoot)) == []


def test_export_never_writes_to_the_shoot(shoot, tmp_path):
    """An export reads every source file and writes copies somewhere else. Somewhere else is the part
    being checked: the destination is outside the shoot and the shoot is untouched by the copy."""
    from photosort import export
    index_folder(shoot, faces=False, workers=2)
    conn = db.connect(shoot)
    ids = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' ORDER BY id")]
    dest = tmp_path / "exports"
    dest.mkdir()
    before = _manifest(shoot)
    export.export_ids(shoot, ids, "e2", mode="copy", base=dest)
    assert any(dest.rglob("*.jpg")), "export produced nothing, so it proved nothing"
    assert _diff(before, _manifest(shoot)) == []


def test_the_index_lives_outside_the_shoot(shoot):
    """Where the writes actually go. INDEX_DIRNAME = '.photosort' survives in config as a leftover of
    the abandoned index-in-place design and is referenced nowhere; if it ever comes back, the index
    lands inside the client's card and every test above starts failing for the right reason."""
    index_folder(shoot, faces=False, workers=2)
    idx = db.index_dir(shoot)
    assert shoot.resolve() not in idx.resolve().parents
    assert not (shoot / ".photosort").exists()
