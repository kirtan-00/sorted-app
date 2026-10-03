"""XMP sidecars (selling checklist B3): the marks written out where Lightroom, Bridge and Capture One read them.
Never under the shoot."""
import json, os, shutil, subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest
from photosort import xmp

NS = {"x": "adobe:ns:meta/", "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
      "xmp": "http://ns.adobe.com/xap/1.0/", "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/"}
EXIFTOOL = os.environ.get("PHOTOSORT_EXIFTOOL") or shutil.which("exiftool")
needs_exiftool = pytest.mark.skipif(not EXIFTOOL, reason="exiftool not installed (set PHOTOSORT_EXIFTOOL to run the read-back)")


def _desc(text: str):
    root = ET.fromstring(text.split("?>", 1)[1].rsplit("<?xpacket", 1)[0])     # the packet minus its two PIs
    assert root.tag == "{adobe:ns:meta/}xmpmeta"
    d = root.find("rdf:RDF/rdf:Description", NS)
    assert d is not None and d.get(f"{{{NS['rdf']}}}about") == ""
    return d


def _attr(d, ns, name):
    return d.get(f"{{{NS[ns]}}}{name}")


def test_sidecar_is_a_well_formed_xmp_packet():
    text = xmp.sidecar_xml({"flag": "pick", "rating": 4, "label": "red"})
    assert text.startswith('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>')
    assert text.rstrip().endswith('<?xpacket end="w"?>')
    d = _desc(text)
    assert _attr(d, "xmp", "Rating") == "4"
    assert _attr(d, "xmp", "Label") == "Red"
    assert _attr(d, "xmpDM", "good") == "True"
    assert _attr(d, "xmpDM", "pick") == "1"


def test_reject_is_rating_minus_one_whatever_the_stars():
    d = _desc(xmp.sidecar_xml({"flag": "reject", "rating": 3, "label": None}))
    assert _attr(d, "xmp", "Rating") == "-1"
    assert _attr(d, "xmpDM", "pick") == "-1"
    assert _attr(d, "xmpDM", "good") == "False"
    assert _attr(d, "xmp", "Label") is None


def test_cleared_marks_still_write_a_zero_rating():
    """A sidecar written for an item whose marks were taken off says so, rather than keeping the old stars."""
    d = _desc(xmp.sidecar_xml({"flag": None, "rating": 0, "label": None}))
    assert _attr(d, "xmp", "Rating") == "0"
    assert _attr(d, "xmpDM", "pick") == "0"
    assert _attr(d, "xmp", "Label") is None and _attr(d, "xmpDM", "good") is None


def test_every_label_maps_to_the_lightroom_name():
    for lb, name in [("red", "Red"), ("yellow", "Yellow"), ("green", "Green"), ("blue", "Blue"), ("purple", "Purple")]:
        assert _attr(_desc(xmp.sidecar_xml({"flag": None, "rating": 0, "label": lb})), "xmp", "Label") == name


def test_sidecar_path_is_the_adobe_stem_convention(tmp_path):
    assert xmp.sidecar_path(tmp_path / "IMG_0001.CR3") == tmp_path / "IMG_0001.xmp"
    assert xmp.sidecar_path(tmp_path / "C0001.MP4") == tmp_path / "C0001.xmp"
    assert xmp.sidecar_path(tmp_path / "noext") == tmp_path / "noext.xmp"


def test_write_sidecar_writes_and_rewrites_its_own(tmp_path):
    shoot = tmp_path / "shoot"; shoot.mkdir(); out = tmp_path / "out"; out.mkdir()
    p = out / "a.xmp"
    assert xmp.write_sidecar(p, {"flag": "pick", "rating": 2, "label": None}, shoot) == "written"
    assert _attr(_desc(p.read_text("utf-8")), "xmp", "Rating") == "2"
    assert xmp.write_sidecar(p, {"flag": None, "rating": 5, "label": "green"}, shoot) == "written"
    assert _attr(_desc(p.read_text("utf-8")), "xmp", "Rating") == "5"
    assert sorted(os.listdir(out)) == ["a.xmp"]                         # no temp file left behind


def test_write_sidecar_never_overwrites_someone_elses(tmp_path):
    """An .xmp that sorted did not write is somebody's Lightroom develop settings: left exactly as it is."""
    shoot = tmp_path / "shoot"; shoot.mkdir(); out = tmp_path / "out"; out.mkdir()
    p = out / "a.xmp"
    foreign = '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0"><crs:Exposure2012>+0.5</crs:Exposure2012></x:xmpmeta>'
    p.write_text(foreign)
    assert xmp.write_sidecar(p, {"flag": "pick", "rating": 2, "label": None}, shoot) == "foreign"
    assert p.read_text() == foreign


def test_write_sidecar_refuses_anywhere_under_the_shoot(tmp_path):
    shoot = tmp_path / "shoot"; (shoot / "card").mkdir(parents=True)
    before = sorted(str(p) for p in shoot.rglob("*"))
    for bad in (shoot / "a.xmp", shoot / "card" / "a.xmp"):
        with pytest.raises(ValueError):
            xmp.write_sidecar(bad, {"flag": "pick", "rating": 1, "label": None}, shoot)
    # a symlinked folder in the export that points back into the shoot is caught too
    out = tmp_path / "out"; out.mkdir(); (out / "back").symlink_to(shoot / "card")
    with pytest.raises(ValueError):
        xmp.write_sidecar(out / "back" / "a.xmp", {"flag": "pick", "rating": 1, "label": None}, shoot)
    assert sorted(str(p) for p in shoot.rglob("*")) == before


def test_write_sidecar_will_not_follow_a_symlinked_sidecar(tmp_path):
    shoot = tmp_path / "shoot"; shoot.mkdir(); out = tmp_path / "out"; out.mkdir()
    target = tmp_path / "elsewhere.xmp"; target.write_text("keep")
    (out / "a.xmp").symlink_to(target)
    assert xmp.write_sidecar(out / "a.xmp", {"flag": "pick", "rating": 1, "label": None}, shoot) == "foreign"
    assert target.read_text() == "keep"


@needs_exiftool
def test_exiftool_reads_back_what_lightroom_and_bridge_read(tmp_path):
    shoot = tmp_path / "shoot"; shoot.mkdir()
    cases = {"a": {"flag": "pick", "rating": 4, "label": "yellow"}, "b": {"flag": "reject", "rating": 2, "label": "purple"},
             "c": {"flag": None, "rating": 0, "label": None}}
    for k, m in cases.items():
        xmp.write_sidecar(tmp_path / f"{k}.xmp", m, shoot)
    out = subprocess.run([EXIFTOOL, "-j", "-G1", "-validate", "-warning", "-error", "-XMP-xmp:Rating", "-XMP-xmp:Label",
                          "-XMP-xmpDM:Good", "-XMP-xmpDM:Pick", "-n", *(str(tmp_path / f"{k}.xmp") for k in cases)],
                         capture_output=True, text=True, check=True)
    got = {Path(r["SourceFile"]).stem: r for r in json.loads(out.stdout)}
    for k, r in got.items():
        assert r.get("ExifTool:Validate") in (None, "0 0 0", "OK"), r            # no errors, no warnings
        assert "ExifTool:Warning" not in r and "ExifTool:Error" not in r, r
    assert got["a"]["XMP-xmp:Rating"] == 4 and got["a"]["XMP-xmp:Label"] == "Yellow"
    assert got["a"]["XMP-xmpDM:Good"] in (True, "True", 1) and got["a"]["XMP-xmpDM:Pick"] == 1
    assert got["b"]["XMP-xmp:Rating"] == -1 and got["b"]["XMP-xmp:Label"] == "Purple" and got["b"]["XMP-xmpDM:Pick"] == -1
    assert got["c"]["XMP-xmp:Rating"] == 0 and "XMP-xmp:Label" not in got["c"]
