"""XMP sidecars: the flags, stars and colour labels set in sorted, written where Lightroom, Bridge and Capture One
look for them, so a cull does not stop at sorted.

What goes in one sidecar (an XMP packet, UTF-8, one rdf:Description):
  xmp:Rating    0 to 5, or -1 for a rejected item. -1 is the only reject that travels: Lightroom writes and reads it
                as Rejected, Bridge shows it as Reject, exiftool documents it as "-1 for rejected". A reject wins
                over any stars in the sidecar; the index keeps both, so taking the reject off brings the stars back.
  xmp:Label     Red / Yellow / Green / Blue / Purple, Lightroom's default label set by name (Lightroom and Bridge
                match labels by this text; Capture One maps them to its colour tags).
  xmpDM:pick    1 pick, -1 reject, 0 neither (Adobe's Dynamic Media schema; exiftool XMP-xmpDM:Pick).
  xmpDM:good    True for a pick, False for a reject (XMP-xmpDM:Good, the "keeper" box Bridge and Premiere read for
                clips). Lightroom's own pick flag lives only in its catalog and has no XMP field at all, so a pick
                reaches Lightroom as nothing; this is the closest thing the format has.

Where: <stem>.xmp beside the file it describes (IMG_0001.CR3 -> IMG_0001.xmp), Adobe's naming, which Capture One
also reads. Never under the shoot: shoot disks are read-only apart from the project file and Reorganise, so every
write here checks the resolved path against the shoot root and raises. A .xmp already at the destination that
sorted did not write (no x:xmptk="sorted ...") is somebody's develop settings and is left alone."""
from __future__ import annotations
import os, tempfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

TOOLKIT = "sorted"                     # x:xmptk starts with this; it is how a sidecar is known to be ours
LABEL_NAMES = {"red": "Red", "yellow": "Yellow", "green": "Green", "blue": "Blue", "purple": "Purple"}
NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_XMP = "http://ns.adobe.com/xap/1.0/"
NS_XMPDM = "http://ns.adobe.com/xmp/1.0/DynamicMedia/"


def _toolkit() -> str:
    try:
        from .version import app_version
        return f"{TOOLKIT} {app_version()}"
    except Exception:
        return TOOLKIT


def sidecar_xml(marks: dict) -> str:
    """The whole sidecar for one item's {flag, rating, label}. Rating is always written (0 included), so a
    sidecar rewritten after the marks came off says so instead of keeping yesterday's stars."""
    flag = marks.get("flag"); rating = int(marks.get("rating") or 0); label = marks.get("label")
    attrs = [("xmp:Rating", str(-1 if flag == "reject" else rating))]
    if label in LABEL_NAMES:
        attrs.append(("xmp:Label", LABEL_NAMES[label]))
    attrs.append(("xmpDM:pick", {"pick": "1", "reject": "-1"}.get(flag, "0")))
    if flag in ("pick", "reject"):
        attrs.append(("xmpDM:good", "True" if flag == "pick" else "False"))
    body = "".join(f"\n    {k}={quoteattr(v)}" for k, v in attrs)
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            f'<x:xmpmeta xmlns:x="{NS_X}" x:xmptk={quoteattr(_toolkit())}>\n'
            f' <rdf:RDF xmlns:rdf="{NS_RDF}">\n'
            f'  <rdf:Description rdf:about=""\n    xmlns:xmp="{NS_XMP}"\n    xmlns:xmpDM="{NS_XMPDM}"{body}/>\n'
            ' </rdf:RDF>\n'
            '</x:xmpmeta>\n'
            '<?xpacket end="w"?>\n')


def sidecar_path(media: Path) -> Path:
    """<stem>.xmp next to the media file (IMG_0001.CR3 -> IMG_0001.xmp)."""
    media = Path(media)
    return media.with_suffix(".xmp") if media.suffix else media.with_name(media.name + ".xmp")


def is_ours(path: Path) -> bool:
    """True when the file at path is a sidecar sorted wrote (its x:xmptk names sorted)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return f'x:xmptk="{TOOLKIT}'.encode() in head


def check_outside(path: Path, root: Path) -> None:
    """ValueError when path, with every symlink in its folders resolved, lands at or under the shoot root."""
    root_res = Path(root).resolve()
    where = Path(path).parent.resolve() / Path(path).name
    if where == root_res or where.is_relative_to(root_res):
        raise ValueError(f"an XMP sidecar never goes inside the source folder (the shoot): {path}")


def write_sidecar(path: Path, marks: dict, root: Path) -> str:
    """Write one sidecar at path (atomically: a temp file in the same folder, then a rename). Returns "written",
    or "foreign" when a file sorted did not write is already there (or path is a symlink), which is left alone.
    Raises ValueError for a path under the shoot root, before anything is touched."""
    path = Path(path)
    check_outside(path, root)
    if path.is_symlink() or (path.exists() and not is_ours(path)):
        return "foreign"
    fd, tmp = tempfile.mkstemp(prefix=".sorted-xmp-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sidecar_xml(marks))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    return "written"
