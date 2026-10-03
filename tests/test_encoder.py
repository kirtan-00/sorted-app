"""The encoder table (config.ENCODERS) and the byte-length guard it makes possible (db.EMBED_BYTES):
a bad PHOTOSORT_CLIP_MODEL must fail loud at import, and a blob left over from a different encoder must
be treated as absent everywhere an index reads embeddings, never as data of the wrong width."""
import importlib
import os
import numpy as np
import pytest
import photosort.config as config
from photosort import db
from photosort.config import EMBED_DIM, MODEL_TAG

BASE_ROW = dict(size=1, mtime=1.0, qhash="h", sibling=None, width=10, height=10, taken_at=None,
                 camera=None, phash="0" * 16, sharp_tile=1.0, sharp_max=2.0, sharp_eye=None, sharp=1.0,
                 n_faces=0, status="ok")

def _row(rel, **kw):
    r = dict(BASE_ROW); r.update(rel=rel); r.update(kw)
    return r

def _wrong_width_blob() -> bytes:
    """One float16 short of EMBED_DIM: wrong under either encoder, never a coincidental match."""
    return np.ones(EMBED_DIM + 1, np.float16).tobytes()

def test_unknown_encoder_name_raises_and_names_the_allowed_ones(monkeypatch):
    """A typo in PHOTOSORT_CLIP_MODEL must fail at import, not fall through to a default: that default
    is how a shoot gets silently indexed with the wrong encoder. The message has to name the bad value
    and the names that are actually allowed, or the person hitting this has nothing to go on."""
    old = os.environ.get("PHOTOSORT_CLIP_MODEL")
    monkeypatch.setenv("PHOTOSORT_CLIP_MODEL", "ViT-B-16-SigLip-Typo")
    try:
        with pytest.raises(ValueError) as ei:
            importlib.reload(config)
        msg = str(ei.value)
        assert "ViT-B-16-SigLip-Typo" in msg
        assert "MobileCLIP-S1" in msg and "ViT-B-16-SigLIP" in msg
    finally:
        # Leave the module in a good state for every test that runs after this one, whatever the
        # env was before we touched it (unset, or already ViT-B-16-SigLIP for the second suite run).
        if old is None:
            monkeypatch.delenv("PHOTOSORT_CLIP_MODEL", raising=False)
        else:
            monkeypatch.setenv("PHOTOSORT_CLIP_MODEL", old)
        importlib.reload(config)

def test_load_embeds_skips_a_wrong_width_blob_and_keeps_the_rest(tmp_path):
    """The hazard this task exists to remove: a 512-dim blob under a 768-dim build (or the reverse)
    must not reach np.stack, where it would either raise on a ragged shape or, worse, silently hand a
    wrong-width matrix to a search. load_embeds has to skip it and still return the row beside it."""
    conn = db.connect(tmp_path)
    good_id = db.upsert_photo(conn, _row("good.jpg"))
    db.set_embed(conn, good_id, np.ones(EMBED_DIM, np.float32))
    bad_id = db.upsert_photo(conn, _row("bad.jpg"))
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (_wrong_width_blob(), bad_id))
    conn.commit()
    ids, M = db.load_embeds(conn)
    assert ids.tolist() == [good_id] and M.shape == (1, EMBED_DIM)

def test_photos_missing_embed_includes_a_wrong_width_row(tmp_path):
    """A stale blob has to come back as work to do, not as a row that already has an embedding: this
    is what lets the ordinary Continue pass re-embed it with no UI change."""
    conn = db.connect(tmp_path)
    good_id = db.upsert_photo(conn, _row("good.jpg"))
    db.set_embed(conn, good_id, np.ones(EMBED_DIM, np.float32))
    bad_id = db.upsert_photo(conn, _row("bad.jpg"))
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (_wrong_width_blob(), bad_id))
    conn.commit()
    missing = dict(db.photos_missing_embed(conn))
    assert bad_id in missing and good_id not in missing

def test_segment_loaders_skip_a_wrong_width_blob_too(tmp_path):
    """segments carry the same kind of blob as photos (a video's clip-level embedding lives in photos,
    its per-scene embeddings live in segments), so load_segment_embeds and segments_missing_embed need
    the same byte-length guard, not a copy that was forgotten for the video path."""
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, _row("clip.mp4", kind="video"))
    db.replace_segments(conn, pid, [dict(idx=0, start=0.0, end=1.0, frame="f0.jpg"),
                                    dict(idx=1, start=1.0, end=2.0, frame="f1.jpg")])
    segs = db.list_segments(conn, pid)
    good_seg, bad_seg = segs[0]["id"], segs[1]["id"]
    db.set_segment_embed(conn, good_seg, np.ones(EMBED_DIM, np.float32))
    conn.execute("UPDATE segments SET embed=? WHERE id=?", (_wrong_width_blob(), bad_seg))
    conn.commit()

    ids, M = db.load_segment_embeds(conn)
    assert ids.tolist() == [good_seg] and M.shape == (1, EMBED_DIM)

    missing_ids = [s[0] for s in db.segments_missing_embed(conn)]
    assert bad_seg in missing_ids and good_seg not in missing_ids

def test_set_embed_refuses_a_short_write(tmp_path):
    """A short write here is what poisons an index (the hazard this whole task exists to remove), so
    set_embed must refuse before the blob ever reaches the column, and name both lengths. ValueError rather
    than an assert: python -O strips asserts and a shipped app may well run that way."""
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, _row("a.jpg"))
    with pytest.raises(ValueError, match=str(EMBED_DIM)):
        db.set_embed(conn, pid, np.ones(EMBED_DIM + 1, np.float32))
    assert conn.execute("SELECT embed FROM photos WHERE id=?", (pid,)).fetchone()[0] is None

def test_set_segment_embed_refuses_a_short_write(tmp_path):
    conn = db.connect(tmp_path)
    pid = db.upsert_photo(conn, _row("clip.mp4", kind="video"))
    db.replace_segments(conn, pid, [dict(idx=0, start=0.0, end=1.0, frame="f0.jpg")])
    seg_id = db.list_segments(conn, pid)[0]["id"]
    with pytest.raises(ValueError, match=str(EMBED_DIM)):
        db.set_segment_embed(conn, seg_id, np.ones(EMBED_DIM + 1, np.float32))

def test_stamp_model_then_embed_model_round_trips_and_defaults_to_none(tmp_path):
    """embed_model has to read back exactly what stamp_model wrote (MODEL_TAG), and a fresh index
    (nothing scanned yet, or one written before this task) has to say None, which is what tells a
    caller "assume nothing, trust the byte-length guard instead"."""
    conn = db.connect(tmp_path)
    assert db.embed_model(conn) is None
    db.stamp_model(conn)
    assert db.embed_model(conn) == MODEL_TAG

def test_scan_counts_treats_a_wrong_width_blob_as_unembedded_and_stale(tmp_path):
    """unembedded is what the Scan tab's existing Continue affordance already reads, so a stale blob has
    to count there with no UI change; stale_embeds is the new number, for "N photos were embedded with
    a different model", and must not be confused with a photo that was simply never embedded."""
    conn = db.connect(tmp_path)
    good_id = db.upsert_photo(conn, _row("good.jpg"))
    db.set_embed(conn, good_id, np.ones(EMBED_DIM, np.float32))
    bad_id = db.upsert_photo(conn, _row("bad.jpg"))
    conn.execute("UPDATE photos SET embed=? WHERE id=?", (_wrong_width_blob(), bad_id))
    db.upsert_photo(conn, _row("never.jpg"))   # embed left NULL: never embedded, not stale
    conn.commit()

    c = db.scan_counts(conn)
    assert c["scanned"] == 3 and c["embedded"] == 1 and c["unembedded"] == 2
    assert c["stale_embeds"] == 1          # only the wrong-width row, "never.jpg" is simply unembedded
    assert c["embed_model"] is None

def test_every_encoder_tokenises_without_the_network():
    """The app promises nothing leaves the Mac, and a search box is text before it is a vector, so the
    tokenizer has to load with HF_HUB_OFFLINE=1. SigLIP's does not: open_clip fetches its sentencepiece
    vocabulary from timm/ViT-B-16-SigLIP, that repo carries no config.json, and transformers 5.x asks
    AutoConfig for one first, so offline it raises and every encode_text in the process fails afterwards.
    This walks the whole table rather than naming SigLIP, so the next encoder added cannot skip the check.
    No weights are loaded here: a tokenizer is 2.3 MB of vocabulary, the model is 812 MB."""
    import importlib
    for name in config.ENCODERS:
        env = dict(os.environ, PHOTOSORT_CLIP_MODEL=name, HF_HUB_OFFLINE="1")
        code = ("import photosort.embed as e;"
                "t = e._tokenizer();"
                "ids = t(['a photo of a fishing boat.']);"
                "print(tuple(ids.shape))")
        r = __import__("subprocess").run([__import__("sys").executable, "-c", code], env=env,
                                         capture_output=True, text=True)
        assert r.returncode == 0, f"{name} cannot tokenise offline:\n{r.stderr[-1500:]}"
        assert r.stdout.strip().startswith("(1,"), r.stdout


def test_switching_encoder_re_embeds_the_shoot_end_to_end(tmp_path, monkeypatch):
    """The integration the unit tests above did not cover.

    photos_missing_embed has always counted a wrong-width blob as work to do, and it has been tested
    since the byte-width guard went in. index.py asked a different question - "embed IS NULL" - so a
    shoot indexed under one encoder and reopened under another kept its stale vectors, stayed invisible
    to search behind the EMBED_BYTES guard, and reported nothing left for Continue to do. Segments were
    fine, because that path did call segments_missing_embed. Only photos were stranded.

    Flipping the default to SigLIP is what makes this reachable: every index written before it is
    MobileCLIP-width.
    """
    from photosort import db as D, index as I
    from photosort.config import EMBED_DIM
    import numpy as np
    from conftest import make_image

    for i in range(3):
        make_image(tmp_path, f"p{i}.jpg", seed=i)
    I.index_folder(tmp_path, faces=False, workers=1)
    conn = D.connect(tmp_path)
    assert D.scan_counts(conn)["embedded"] == 3

    # stand in for "indexed under the other encoder": same rows, blobs of the other width
    other = np.ones(EMBED_DIM + 7, np.float16).tobytes()
    conn.execute("UPDATE photos SET embed=? WHERE status='ok'", (other,))
    conn.commit()
    k = D.scan_counts(conn)
    assert k["embedded"] == 0 and k["unembedded"] == 3 and k["stale_embeds"] == 3

    I.index_folder(tmp_path, faces=False, workers=1)
    k = D.scan_counts(D.connect(tmp_path))
    assert k["embedded"] == 3 and k["unembedded"] == 0 and k["stale_embeds"] == 0, (
        "a re-run left stale vectors in place; the shoot's photos are unsearchable and "
        "Continue thinks it is finished")
