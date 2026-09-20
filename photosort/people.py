from __future__ import annotations
from collections import Counter
from pathlib import Path
import numpy as np
import sklearn
from sklearn.cluster import DBSCAN
from . import db
from .config import (FACE_CLUSTER_EPS, FACE_MIN_SAMPLES, GROUP_MIN_FACES, FACE_MATCH_MIN_SIM, FACE_REF_MIN_EDGE,
                     FACE_MERGE_SUGGEST_SIM, FACE_MERGE_AUTO_SIM, FACE_CLUSTER_MIN_EDGE, FACE_CLUSTER_MIN_EYE_SHARP)

# Grouping is DBSCAN at a tight eps (pure groups, one person may be split across angles), then the
# remembered answers: every "same" link joins its two groups (and pulls a noise face into a group),
# every "different" link keeps its two groups apart when the automatic centroid merge would join them.
# Links are face ids, so they outlive the people rows, which are rebuilt here every time.

class _Union:
    def __init__(self):
        self.parent: dict[int, int] = {}
    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]; x = self.parent[x]
        return x
    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

def _centroids(labels: np.ndarray, F: np.ndarray) -> tuple[list[int], np.ndarray]:
    """(group labels, unit centroids) for every label except -1, in label order."""
    labs = sorted(set(labels.tolist()) - {-1})
    if not labs:
        return labs, np.zeros((0, F.shape[1]), np.float32)
    C = np.stack([F[labels == l].mean(axis=0) for l in labs])
    C /= np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
    return labs, C

def _apply_links(labels: np.ndarray, fids: np.ndarray, F: np.ndarray, links: list[tuple[int, int, str]],
                 auto_sim: float) -> np.ndarray:
    """DBSCAN labels -> final labels. 1) must-links: a "same" pair in two groups joins them; a noise face
    with a "same" link joins its partner's group (two noise faces make a group of their own). 2) auto-merge:
    while the two closest groups have centroid cosine >= auto_sim and no "different" link crosses them,
    join them (best pair first, centroids recomputed after every join). A "same" chain beats a "different"
    link between the same two groups: the explicit yes was given later or on purpose, and DBSCAN itself
    never splits a group on a no. Labels come back compact, 0..k-1, noise stays -1."""
    lab = labels.copy(); pos = {int(f): i for i, f in enumerate(fids)}
    uf = _Union(); nxt = int(lab.max()) + 1 if len(lab) else 0
    for a, b, d in links:
        if d != "same" or a not in pos or b not in pos:
            continue
        ia, ib = pos[a], pos[b]; la, lb = int(lab[ia]), int(lab[ib])
        if la == -1 and lb == -1:
            lab[ia] = lab[ib] = nxt; nxt += 1
        elif la == -1:
            lab[ia] = lb
        elif lb == -1:
            lab[ib] = la
        else:
            uf.union(la, lb)
    for i in np.where(lab != -1)[0]:
        lab[i] = uf.find(int(lab[i]))
    apart = [(pos[a], pos[b]) for a, b, d in links if d == "different" and a in pos and b in pos]
    def blocked(x: int, y: int) -> bool:
        return any({uf.find(int(lab[i])), uf.find(int(lab[j]))} == {x, y} for i, j in apart if lab[i] != -1 and lab[j] != -1)
    if auto_sim < 1.0:
        while True:
            labs, C = _centroids(lab, F)
            if len(labs) < 2:
                break
            S = C @ C.T; np.fill_diagonal(S, -1.0)
            order = np.dstack(np.unravel_index(np.argsort(-S, axis=None), S.shape))[0]
            joined = False
            for i, j in order:
                if i >= j or S[i, j] < auto_sim:
                    if S[i, j] < auto_sim: break
                    continue
                if not blocked(labs[i], labs[j]):
                    uf.union(labs[i], labs[j])
                    for k in np.where((lab == labs[i]) | (lab == labs[j]))[0]:
                        lab[k] = uf.find(int(lab[k]))
                    joined = True; break
            if not joined:
                break
    keep = {l: n for n, l in enumerate(sorted(set(lab.tolist()) - {-1}))}
    return np.array([keep.get(int(l), -1) for l in lab], np.int64)

def cluster_faces(root: Path, eps: float | None = None, min_samples: int = FACE_MIN_SAMPLES) -> list[dict]:
    """Rebuild the people table from every face big and sharp enough to trust (FACE_CLUSTER_MIN_EDGE,
    FACE_CLUSTER_MIN_EYE_SHARP; the rest keep person_id NULL): DBSCAN at eps (FACE_CLUSTER_EPS when
    None), then the remembered "same"/"different" answers (see _apply_links). Names survive by majority vote."""
    conn = db.connect(root)
    fids, pids, F = db.load_face_embeds(conn)
    _q, edge, eye = db.load_face_quality(conn)
    keep = (edge >= FACE_CLUSTER_MIN_EDGE) & (eye >= FACE_CLUSTER_MIN_EYE_SHARP)
    fids, pids, F = fids[keep], pids[keep], F[keep]
    # Names survive a recluster: remember which face belonged to a named person,
    # then hand each new cluster the majority name among its faces.
    old_names = {r[0]: r[1] for r in conn.execute(
        "SELECT f.id, pe.name FROM faces f JOIN people pe ON pe.id=f.person_id WHERE pe.name IS NOT NULL")}
    conn.execute("UPDATE faces SET person_id=NULL"); conn.execute("DELETE FROM people"); conn.commit()
    if len(fids) == 0:
        return []
    # working_memory caps the pairwise-distance chunks DBSCAN builds (MiB); the default
    # 1024 can spike RSS on a big shoot.
    with sklearn.config_context(working_memory=128):
        labels = DBSCAN(eps=FACE_CLUSTER_EPS if eps is None else eps, min_samples=min_samples, metric="cosine", n_jobs=1).fit_predict(F)
    labels = _apply_links(labels, fids, F, db.face_links(conn), FACE_MERGE_AUTO_SIM)
    for lab in sorted(set(labels) - {-1}):
        idx = np.where(labels == lab)[0]
        members = [int(f) for f in fids[idx]]
        n_photos = len(set(pids[idx].tolist()))
        best = conn.execute(f"SELECT id FROM faces WHERE id IN ({','.join('?'*len(members))}) ORDER BY score DESC LIMIT 1", members).fetchone()[0]
        votes = Counter(old_names[f] for f in members if f in old_names)
        name = votes.most_common(1)[0][0] if votes else None
        cur = conn.execute("INSERT INTO people(name, cover_face_id, n) VALUES(?, ?, ?)", (name, best, n_photos))
        conn.executemany("UPDATE faces SET person_id=? WHERE id=?", [(cur.lastrowid, f) for f in members])
    conn.commit()
    return list_people(root)

def list_people(root: Path) -> list[dict]:
    conn = db.connect(root)
    # Heal covers whose face row is gone (photo re-indexed or culled): fall back to the
    # best-scoring face still attached to that person.
    conn.execute("""UPDATE people SET cover_face_id = (SELECT id FROM faces WHERE person_id=people.id ORDER BY score DESC LIMIT 1)
                    WHERE cover_face_id IS NULL OR cover_face_id NOT IN (SELECT id FROM faces)""")
    conn.commit()
    rows = conn.execute("""SELECT pe.id, pe.name, pe.n, pe.cover_face_id, p.qhash, f.x, f.y, f.w, f.h
                           FROM people pe LEFT JOIN faces f ON f.id=pe.cover_face_id LEFT JOIN photos p ON p.id=f.photo_id
                           ORDER BY pe.n DESC, pe.id""").fetchall()
    return [dict(id=r[0], name=r[1], n=r[2], cover_face_id=r[3], cover_qhash=r[4],
                 cover_box=[r[5] or 0, r[6] or 0, r[7] or 0, r[8] or 0]) for r in rows]

def _face_crops(conn, person_id: int, exclude: int | None, limit: int) -> list[dict]:
    """[{qhash, box}] for up to `limit` best-scoring faces of a person, skipping face `exclude` (the cover)."""
    rows = conn.execute("""SELECT p.qhash, f.x, f.y, f.w, f.h FROM faces f JOIN photos p ON p.id=f.photo_id
                           WHERE f.person_id=? AND f.id IS NOT ? ORDER BY f.score DESC LIMIT ?""", (person_id, exclude, limit)).fetchall()
    return [{"qhash": r[0], "box": [r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0]} for r in rows]

def suggest_merges(root: Path, limit: int = 50) -> list[dict]:
    """Pairs of groups that look like one person, best first: centroid cosine >= FACE_MERGE_SUGGEST_SIM,
    at least one side with GROUP_MIN_FACES faces or more (a pair of two tiny groups has centroids too
    noisy to trust), and no "different" link between any face of one and any face of the other. Each
    carries the cover and up to 3 more faces per side as {qhash, box} in preview coordinates, the same
    shape /api/people uses for covers. Ordered by rank = sim * (1 + log10(min(a_n, b_n))): at equal
    similarity a 34-face burst next to a 700-face person outranks two 3-face blur groups. sim is the raw
    centroid cosine; the threshold applies to sim."""
    conn = db.connect(root)
    fids, _pids, F = db.load_face_embeds(conn)
    _f2, owners = db.load_face_people(conn)
    labs, C = _centroids(owners, F)
    if len(labs) < 2 or limit <= 0:
        return []
    counts = Counter(owners[owners != -1].tolist())
    people = {p["id"]: p for p in list_people(root)}
    pos = {int(f): i for i, f in enumerate(fids)}
    apart: set[tuple[int, int]] = set()
    for a, b, d in db.face_links(conn):
        if d == "different" and a in pos and b in pos:
            x, y = int(owners[pos[a]]), int(owners[pos[b]])
            if x != -1 and y != -1:
                apart.add((min(x, y), max(x, y)))
    S = C @ C.T
    iu, ju = np.triu_indices(len(labs), k=1)
    sims = S[iu, ju]
    small = np.array([min(counts[labs[i]], counts[labs[j]]) for i, j in zip(iu, ju)], np.float32)
    ranks = sims * (1 + np.log10(np.maximum(small, 1)))
    cand = np.where(sims >= FACE_MERGE_SUGGEST_SIM)[0]
    order = cand[np.argsort(-ranks[cand], kind="stable")]
    out = []
    for k in order:
        sim = float(sims[k])
        a, b = labs[iu[k]], labs[ju[k]]
        if max(counts[a], counts[b]) < GROUP_MIN_FACES or (min(a, b), max(a, b)) in apart or a not in people or b not in people:
            continue
        pa, pb = people[a], people[b]
        out.append({"a": a, "b": b, "sim": round(sim, 4), "rank": round(float(ranks[k]), 4), "a_name": pa["name"], "b_name": pb["name"],
                    "a_n": counts[a], "b_n": counts[b],
                    "a_cover": {"qhash": pa["cover_qhash"], "box": pa["cover_box"]},
                    "b_cover": {"qhash": pb["cover_qhash"], "box": pb["cover_box"]},
                    "a_faces": _face_crops(conn, a, pa["cover_face_id"], 3),
                    "b_faces": _face_crops(conn, b, pb["cover_face_id"], 3)})
        if len(out) >= limit:
            break
    return out

def _person_row(conn, pid: int):
    row = conn.execute("SELECT id, name, cover_face_id FROM people WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise ValueError(f"no such person: {pid}")
    return row

def _cover_or_best(conn, pid: int, cover: int | None) -> int | None:
    if cover is not None and conn.execute("SELECT 1 FROM faces WHERE id=? AND person_id=?", (cover, pid)).fetchone():
        return cover
    r = conn.execute("SELECT id FROM faces WHERE person_id=? ORDER BY score DESC LIMIT 1", (pid,)).fetchone()
    return r[0] if r else None

def merge_people(root: Path, keep: int, drop: int) -> dict:
    """The user said yes: every face of `drop` moves to `keep`, `drop` goes away, the name is keep's
    (or drop's when keep has none), and a "same" link between the two covers makes the join stick on
    every later re-cluster. Returns the updated person. ValueError on a self-merge or an unknown id."""
    if keep == drop:
        raise ValueError("cannot merge a person into itself")
    conn = db.connect(root)
    k, d = _person_row(conn, keep), _person_row(conn, drop)
    ck, cd = _cover_or_best(conn, keep, k[2]), _cover_or_best(conn, drop, d[2])
    conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (keep, drop))
    n = conn.execute("SELECT count(DISTINCT photo_id) FROM faces WHERE person_id=?", (keep,)).fetchone()[0]
    conn.execute("UPDATE people SET n=?, name=?, cover_face_id=? WHERE id=?", (n, k[1] or d[1], ck, keep))
    conn.execute("DELETE FROM people WHERE id=?", (drop,))
    conn.commit()
    if ck is not None and cd is not None:
        db.add_face_link(conn, ck, cd, "same")
    return next(p for p in list_people(root) if p["id"] == keep)

def reject_merge(root: Path, a: int, b: int) -> None:
    """The user said no: a "different" link between the two covers hides the pair from suggestions
    and blocks the automatic merge for good. ValueError on the same id twice or an unknown id."""
    if a == b:
        raise ValueError("cannot reject a person against itself")
    conn = db.connect(root)
    ra, rb = _person_row(conn, a), _person_row(conn, b)
    ca, cb = _cover_or_best(conn, a, ra[2]), _cover_or_best(conn, b, rb[2])
    if ca is None or cb is None:
        raise ValueError("one of those groups has no faces left")
    db.add_face_link(conn, ca, cb, "different")

def name_person(root: Path, person_id: int, name: str) -> None:
    conn = db.connect(root); conn.execute("UPDATE people SET name=? WHERE id=?", (name.strip() or None, person_id)); conn.commit()

class ReferenceUnreadable(Exception):
    """The reference image exists but could not be decoded or scanned for faces."""

def _reference_faces(image_path: Path) -> list:
    """Faces in a reference image. One seam so tests can hand in synthetic faces."""
    from .decode import load_preview
    from .faces import FaceEngine
    return FaceEngine().detect(load_preview(image_path))

def _pick_reference(image_path: Path):
    """Detect faces in a reference image and pick the one to match on: the largest, unless it is
    smaller than FACE_REF_MIN_EDGE (a tiny "face" is usually a false positive and matching it
    floods the grid with strangers). Returns (n_faces, face or None, too_small). Decode and
    detect failures come back as ReferenceUnreadable; DB errors are not involved here."""
    try:
        faces = _reference_faces(image_path)
    except Exception as e:
        raise ReferenceUnreadable(str(e)) from e
    if not faces:
        return 0, None, False
    ref = max(faces, key=lambda f: f.w * f.h)
    if max(ref.w, ref.h) < FACE_REF_MIN_EDGE:
        return len(faces), None, True
    return len(faces), ref, False

def band_floor(min_sim: float) -> float:
    """Where the "less sure" band below the slider starts: 0.1 under it, never below 0.4, never above min_sim."""
    return min(min_sim, round(max(min_sim - 0.1, 0.4), 4))

def _best_per_photo(sims: np.ndarray, fids: np.ndarray, pids: np.ndarray, min_sim: float, unsure_band: bool) -> list[dict]:
    """One match per photo (its best face) with sim >= min_sim, or >= band_floor(min_sim) with unsure_band,
    sorted by sim desc so the sure ones come first. Each carries sure = sim >= min_sim."""
    keep = np.where(sims >= (band_floor(min_sim) if unsure_band else min_sim))[0]
    keep = keep[np.argsort(-sims[keep], kind="stable")]
    best: dict[int, dict] = {}
    for i in keep:   # first sight of a photo is its best face
        pid = int(pids[i])
        if pid not in best:
            best[pid] = {"photo_id": pid, "sim": float(sims[i]), "face_id": int(fids[i]), "sure": bool(sims[i] >= min_sim)}
    return list(best.values())

def find_by_reference(root: Path, image_path: Path, min_sim: float = FACE_MATCH_MIN_SIM, unsure_band: bool = False) -> dict:
    """Match the largest face in image_path against every indexed face (not just cluster
    centroids, so it works before clustering and survives a bad cluster). One match per
    photo, the best face in it, sim >= min_sim, sorted by sim desc, each with sure=True.
    With unsure_band the band from band_floor(min_sim) up to min_sim follows, sure=False.
    person_id is the cluster of the single best face, if it has one."""
    n_faces, ref, too_small = _pick_reference(image_path)
    out = {"faces_in_reference": n_faces, "matches": [], "person_id": None}
    if too_small:
        out["reference_face_too_small"] = True
    if ref is None:
        return out
    q = ref.embed
    conn = db.connect(root); fids, pids, F = db.load_face_embeds(conn)
    if len(fids) == 0:
        return out
    out["matches"] = _best_per_photo(F @ q, fids, pids, min_sim, unsure_band)
    if out["matches"]:
        row = conn.execute("SELECT person_id FROM faces WHERE id=?", (out["matches"][0]["face_id"],)).fetchone()
        out["person_id"] = int(row[0]) if row and row[0] is not None else None
    return out

# Named people: a reference photo saved under a name. Several references may share a name
# (more angles of the same person make matching more robust); a photo matches a name when
# its best face is close to ANY reference of that name.

def save_reference(root: Path, name: str, image_path: Path) -> dict:
    """Save the largest face in image_path as a reference for `name`. Same face-picking rule as
    find_by_reference. ValueError when there is nothing worth saving (blank name, no face, face
    too small); ReferenceUnreadable when the image cannot be decoded. The image is only read."""
    name = (name or "").strip()
    if not name:
        raise ValueError("give the person a name")
    from .export import safe_segment
    try:
        safe_segment(name)
    except ValueError:
        raise ValueError("that name cannot be used as a folder")
    n_faces, ref, too_small = _pick_reference(Path(image_path))
    if too_small:
        raise ValueError("no usable face: the face in that photo is too small to match, pick a closer shot")
    if ref is None:
        raise ValueError("no usable face: no face found in that photo")
    conn = db.connect(root)
    ref_id = db.add_reference(conn, name, ref.embed, str(image_path))
    return {"id": ref_id, "name": name, "faces_in_reference": n_faces, "reference_face_too_small": False}

def match_references(root: Path, min_sim: float = FACE_MATCH_MIN_SIM, unsure_band: bool = False) -> dict[str, list[dict]]:
    """name -> [{photo_id, sim, face_id, sure}] for every saved name: the photos whose best face has cosine
    >= min_sim against any reference of that name, sim = that max, sorted by sim desc (with unsure_band,
    the band down to band_floor(min_sim) follows, sure=False). One load of the face matrix and one
    F @ R.T for every name. A frame with two known people appears under both names, that is correct.
    Names with no match at min_sim map to []."""
    conn = db.connect(root)
    ref_ids, names, R = db.load_reference_embeds(conn)
    order = list(dict.fromkeys(names))          # first-saved order, one key per distinct name
    out: dict[str, list[dict]] = {n: [] for n in order}
    if not order:
        return out
    fids, pids, F = db.load_face_embeds(conn)
    if len(fids) == 0:
        return out
    S = F @ R.T                                  # (faces, references)
    name_arr = np.array(names)
    for n in order:
        out[n] = _best_per_photo(S[:, name_arr == n].max(axis=1), fids, pids, min_sim, unsure_band)
    return out

def export_references_ids(root: Path, names: list[str] | None, min_sim: float = FACE_MATCH_MIN_SIM) -> dict[str, list[int]]:
    """Export folder segment -> photo ids for the per-person export. names=None means every saved
    name; a name nobody saved is skipped. Two different names that sanitise to the same segment
    get folders of their own: the later one (in save order) is suffixed _2, _3, ... instead of
    silently merging into the first's folder."""
    from .export import safe_segment
    matched = match_references(root, min_sim)
    wanted = list(matched) if names is None else [n for n in names if n in matched]
    out: dict[str, list[int]] = {}
    taken: dict[str, str] = {}   # segment already claimed -> the name that claimed it
    for n in wanted:
        seg = safe_segment(n)
        if seg in taken and taken[seg] != n:
            i = 2
            while f"{seg}_{i}" in taken:
                i += 1
            seg = f"{seg}_{i}"
        taken[seg] = n
        out[seg] = [m["photo_id"] for m in matched[n]]
    return out

def references_bytes(root: Path, names: list[str] | None, include_raw: bool = False, min_sim: float = FACE_MATCH_MIN_SIM) -> int:
    from .export import folders_bytes
    return folders_bytes(root, export_references_ids(root, names, min_sim), include_raw)

def export_references(root: Path, names: list[str] | None, mode: str = "copy", include_raw: bool = False,
                      base: Path | None = None, progress=None, min_sim: float = FACE_MATCH_MIN_SIM) -> Path:
    """<base>/<shoot>/people/<name>/ for each saved (or listed) name, RAW siblings next to the
    JPEGs when include_raw, failed.txt at <base>/<shoot>/people/failed.txt. Returns the people folder."""
    from .export import export_folders
    return export_folders(Path(root), "people", export_references_ids(root, names, min_sim), mode, include_raw, base, progress)

def assign_from_reference(root: Path, image_path: Path) -> int | None:
    faces = _reference_faces(image_path)
    if not faces:
        return None
    q = max(faces, key=lambda f: f.w * f.h).embed
    conn = db.connect(root); fids, pids, F = db.load_face_embeds(conn)
    labels = np.array([r[0] or -1 for r in conn.execute(
        "SELECT f.person_id FROM faces f JOIN photos p ON p.id=f.photo_id WHERE p.status='ok' ORDER BY f.id")])
    best, best_sim = None, 0.5
    for lab in set(labels.tolist()) - {-1}:
        c = F[labels == lab].mean(axis=0); c /= np.linalg.norm(c)
        s = float(c @ q)
        if s > best_sim: best, best_sim = lab, s
    return best

def export_people_ids(root: Path) -> dict[str, list[int]]:
    """Export folder name -> photo ids for the people/groups/solo bundle. One photo can appear
    under several folders (each person in it, plus groups or solo), and each appearance is a
    separate copy, so callers sizing the export sum over every folder. Two people whose names
    sanitise to the same segment share a folder rather than one silently dropping the other."""
    from .export import safe_segment
    root = Path(root); conn = db.connect(root); out: dict[str, list[int]] = {}
    for p in list_people(root):
        ids = [r[0] for r in conn.execute("SELECT DISTINCT photo_id FROM faces WHERE person_id=?", (p["id"],))]
        nm = safe_segment(p["name"] or f"person_{p['id']:02d}")
        out.setdefault(f"people/{nm}", []).extend(ids)
    out["groups"] = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' AND n_faces>=?", (GROUP_MIN_FACES,))]
    out["solo"] = [r[0] for r in conn.execute("SELECT id FROM photos WHERE status='ok' AND n_faces=1")]
    return out

def export_people(root: Path, mode: str = "copy", base: Path | None = None) -> Path:
    from .export import export_ids
    from .config import export_root
    root = Path(root)
    for name, ids in export_people_ids(root).items():
        export_ids(root, ids, name, mode, base=base)
    return Path(base if base is not None else export_root()).resolve() / root.resolve().name
