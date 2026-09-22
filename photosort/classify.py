"""Zero-shot scene categories on top of the index. Never writes under the shoot root
unless apply_on_disk() is called explicitly (a same-volume move with an undo log)."""
from __future__ import annotations
import csv, os, re, shutil
from pathlib import Path
import numpy as np
from . import db
from .export import export_dir
from .vocab import VOCAB, VOCAB_PEOPLE, VOCAB_LOADED
from .config import CATEGORY_FALLBACK, SURE_MIN, INTERVIEW_MIN_DURATION_S

# One category = several prompts; a photo's category score is the max cosine over its prompts.
# Validated on the first video shoot (DAY-4: Sony A7S III in S-Log3, 144 photos + 126 clips) and re-checked
# on the 3,677-photo index: interview, night, food and sky were what "other" was hiding, road grew a car
# interior, people grew the ceremony crowd and the talking head. The first documentary shoot (630 photos +
# 325 clips) added boat (fishermen on deck had no home between people, ocean and food) and office (29
# empty-office B-roll clips were "interview" because a prompt described the set, not the act: an empty set
# is office, not interview) and a village prompt under building. Wording is calibrated; do not paraphrase.
CATEGORIES: dict[str, list[str]] = {
    "ocean": ["the open sea with waves", "a seascape with the horizon over the water", "boats on the sea",
              "waves crashing on rocks", "the ocean at sunset"],
    "boat": ["fishermen on a fishing boat", "a boat deck with ropes, flags and masts", "boats moored in a harbour"],
    "beach": ["a sandy beach", "the seashore with sand and footprints", "beach umbrellas and sunbeds",
              "a beach with people walking on the sand", "a coastline seen from the beach"],
    "people": ["a portrait of a person", "a group of people posing for a photo", "a crowd of people",
               "a person standing and looking at the camera", "a selfie",
               "a person being interviewed, talking to the camera", "a crowd of people gathered at a ceremony"],
    "interview": ["two people sitting on chairs in a room having an interview",
                  "a person sitting in a chair in a studio talking to the camera",
                  "a person seated in a chair being interviewed, framed pictures and a lamp behind them"],
    "building": ["a building facade", "an old fort or church", "a temple or monument", "a house or hotel",
                 "architecture of a town", "a lighthouse", "a village with huts and small houses"],
    "office": ["an empty office interior", "a meeting room with a long table and chairs",
               "a desk with a lamp, plants and stationery", "framed pictures on an office wall",
               "a company logo on a wall", "a sofa in a waiting room"],
    "road": ["a road with vehicles", "a street in a town", "a highway", "a road through the countryside",
             "a scooter on a road", "the inside of a car with a person driving"],
    "night": ["a street at night with lights", "a city at night", "a shop lit up at night",
              "people outdoors at night under street lights"],
    "food": ["a plate of food", "coconuts and fruit on a street stall", "street food being prepared by a vendor",
             "sweets on a tray", "a cup of tea or a drink", "a fruit and vegetable market"],
    "sky": ["clouds in the sky", "palm trees against the sky", "a dramatic cloudy sky", "the sun behind clouds"],
    "birds-animals": ["a bird", "birds flying", "a dog", "a cow on the road", "a wild animal", "fish",
                      "seabirds flying low over the ocean", "birds over the water"],
}
# A sub-category and its parent: the two do not compete for the margin gate (a seated interview splits the
# softmax between people and interview and would otherwise fail the margin against its own sibling; on the
# first documentary shoot 17 interviews went to "other" that way). Only the margin looks at this; the
# winner, the guess and the folders are whatever category won.
# A people photo keeps its best scene as the guess when the scene reached this much of the softmax;
# below it the person is the whole picture (a studio portrait) and there is no scene to keep.
SCENE_GUESS_MIN_PROB = 0.15

CATEGORY_FAMILY = {"interview": "people"}
# A pseudo-category, not one of CATEGORIES: it competes in the same softmax so things that look like
# nothing on the real list pull probability mass away from whichever real category they happen to
# resemble most. Never becomes a folder name of its own; a win here maps to FALLBACK. Kept separate from
# CATEGORIES so write_manifest's folder list (CATEGORIES keys + FALLBACK) doesn't grow a second "other".
# Negatives describe CONTENT that is not on the list, never image quality: "blurry", "badly lit" and
# "out of focus" all match cinematic shallow-focus footage and flat log profiles, and one such prompt
# became a sink for 35 of the 49 "other" items on the first video shoot.
NEGATIVE_PROMPTS = ["a completely black frame", "a screenshot of a phone or computer screen", "a page of text or a document"]
FALLBACK = CATEGORY_FALLBACK
TEMPERATURE = 100.0   # CLIP's logit scale; turns cosine similarity into a peaked softmax
MIN_PROB = 0.35        # best category must own at least this much of the softmax mass: "other"
MIN_PROB_MARGIN = 0.15  # best minus second-best probability; smaller means ambiguous: "other"
# T=100 amplifies even meaningless cosine gaps into a "confident" softmax: on the calibration set every
# correctly-classified real photo's winning raw cosine was >= 0.1497, while a random (non-photo) unit
# vector's best raw cosine was 0.0814 despite a deceptively "confident" softmax. This absolute floor
# catches that case; the two MIN_PROB* thresholds above then separate genuinely ambiguous real photos.
MIN_COSINE = 0.12
# Drone shots. The index sets photos.aerial from metadata (DJI tags, a DJI_ filename, an .SRT sidecar);
# for rows still 0 a zero-shot aerial/ground prompt pair decides drone shots from other cameras. It is its
# own rule, not a category: a drone shot of a beach stays "beach" and is aerial too. A metadata 1 is never
# re-decided. The prompts avoid the word "drone": it matches a photograph OF a drone (the top false hit on
# the 3,677-photo index). The gate is a raw cosine gap, not a softmax: at TEMPERATURE 100 a two-way
# softmax at 0.7 needs a gap of only 0.0085 and flagged 180 of 3,677 ground-only Sony photos; the 99th
# percentile ground-only gap on two Sony shoots was 0.041 / 0.047, so 0.05 keeps false positives under 1%.
# Recall on real drone footage is unmeasured until 02 Drone is embedded.
AERIAL_PROMPTS = ["an aerial view from high above the ground", "a top-down view of a coastline from the air",
                  "a bird's eye view of a town from the air"]
GROUND_PROMPTS = ["a photo taken at eye level from the ground", "a portrait of a person", "a street seen from the pavement"]
AERIAL_MIN_GAP = 0.05      # best aerial cosine minus best ground cosine; the best aerial cosine must also clear MIN_COSINE
# Discovered categories: k-means over the shoot's embeddings, each cluster named from the vocabulary when
# its members agree on the name, else "group N". Deterministic (random_state=0), no LLM.
DISCOVER_MIN_PHOTOS = 16   # fewer embedded photos than this: nothing to discover
DISCOVER_MIN_SIZE = 8      # a smaller cluster is folded into its nearest neighbour
DISCOVER_MAX_K = 24
# The candidate name is the label that makes the cluster different from the rest of the shoot, not the
# label that fits every photo of the shoot: the label's cosine to the shoot mean, times this, is
# subtracted before the argmax. Measured on two shoots: "blurry motion" became "fruit", "scooter"
# "motorcycle", "team meeting" "panel discussion", "labourer" "foundation pit", while a cluster that IS
# the shoot ("man in a kurta") kept its plain name.
DISCOVER_CONTRAST = 0.5
# The gates a candidate must pass, or the cluster is an unnamed "group N". Calibrated on the first
# corporate documentary (NSG_26 Part 2: interviews, office B-roll, a beach, a fishing boat, no hospital;
# 13 clusters of which 9 were wrongly named, "doctor" and "patient in a hospital" among them) and checked
# on an event shoot (01 Photos, 3,677) and a video shoot (DAY-4, 270). See discover() for the rule.
# Share of members whose own top label is the candidate. Named winners bottom out at 0.30 ("meeting
# room", 14 of 46) and 0.27 ("small painted house"); rejected picks top out at 0.22 ("cricket players" on
# two men in kurtas, "monsoon flood" on a beach) and 0.23 ("balcony" on heritage facades).
DISCOVER_VOTE_SHARE = 0.25
# A VOCAB_LOADED label needs this share instead: "slum" held 0.37 of the painted-hut cluster.
DISCOVER_LOADED_SHARE = 0.6
# Plain cos(centroid, label) of the winner. Never decided a real cluster once the votes had (the lowest
# named plain cosine was 0.203, "fishing boats"; the fixed classifier's single-image floor is 0.12);
# a sanity floor for a cluster of nothing whose members still agree on a word.
DISCOVER_NAME_MIN_COS = 0.16
UNNAMED_GROUP = re.compile(r"^group \d+$")

def is_unnamed_group(name: str) -> bool:
    """True for the reserved "group N" names discover() gives clusters it could not name truthfully."""
    return bool(UNNAMED_GROUP.match(name or ""))

def _prompt_matrix(embedder) -> tuple[list[str], np.ndarray, list[int]]:
    """names includes CATEGORIES keys followed by one pseudo-category "__other__" owning
    NEGATIVE_PROMPTS, so callers that only want the real categories should slice names[:-1]."""
    names, texts, owner = [], [], []
    for i, (cat, prompts) in enumerate(CATEGORIES.items()):
        names.append(cat)
        for p in prompts:
            texts.append(p); owner.append(i)
    neg_idx = len(names)
    names.append("__other__")
    for p in NEGATIVE_PROMPTS:
        texts.append(p); owner.append(neg_idx)
    return names, embedder.encode_text(texts), owner

def _score(M: np.ndarray, T: np.ndarray, owner: np.ndarray, names: list[str]):
    """Per row of M: (category name or FALLBACK, softmax score, margin, guess, guess score, probs) after the
    confidence gates. margin is the winner's lead over the best category of a DIFFERENT family
    (CATEGORY_FAMILY: a sub-category does not compete with its parent for the margin gate; "__other__" is
    its own family). guess is the best REAL category and its probability whatever the gates decided, so a
    photo filed under "other" can still be shown under its guess as "less sure"; probs is every real
    category's probability, for rules that look at more than the winner (the long-interview rule).
    One matrix pass, so photos, videos and segments are scored together on a stacked M."""
    family = [CATEGORY_FAMILY.get(n, n) for n in names]
    S = M @ T.T                                    # (N, prompts)
    per_cat = np.stack([S[:, owner == i].max(axis=1) for i in range(len(names))], axis=1)
    logits = per_cat * TEMPERATURE
    logits -= logits.max(axis=1, keepdims=True)     # numerically stable softmax
    probs = np.exp(logits); probs /= probs.sum(axis=1, keepdims=True)
    out = []
    for k in range(len(M)):
        order = np.argsort(-probs[k]); best = order[0]
        second = next((i for i in order[1:] if family[i] != family[best]), order[1])
        score, margin = float(probs[k, best]), float(probs[k, best] - probs[k, second])
        raw_cos = float(per_cat[k, best])
        name = names[best]
        cat = FALLBACK if name == "__other__" else name
        if cat != FALLBACK and (raw_cos < MIN_COSINE or score < MIN_PROB or margin < MIN_PROB_MARGIN):
            cat = FALLBACK
        real = [i for i in order if names[i] != "__other__"]
        guess, guess_score = names[real[0]], float(probs[k, real[0]])
        out.append((cat, score, margin, guess, guess_score, {names[i]: float(probs[k, i]) for i in real}))
    return out

def _classify_all(root: Path, people_by_faces: bool = True) -> tuple[list[dict], list[dict]]:
    """(photo results, segment results). Photos and videos: rel, sibling, category, score, margin, n_faces,
    guess, guess_score. Segments (of ok videos): id, photo_id, category, score. Both come out of one pass
    over the stacked embedding matrix. A video of INTERVIEW_MIN_DURATION_S or more whose category or best
    real guess is people or interview is "interview": a ten-minute take with a person talking is an
    interview whatever the framing, a beach walk is not; its score is the larger of the two probabilities.
    A face-bearing photo is always "people", with score 1.0: the face detector decided, not the softmax,
    so it is never "less sure"; segments carry no faces, so never, and the face rule never sees a video."""
    from .embed import get_embedder
    root = Path(root); conn = db.connect(root)
    names, T, owner = _prompt_matrix(get_embedder())
    owner = np.array(owner)
    ids, M = db.load_embeds(conn)
    seg_ids, SM = db.load_segment_embeds(conn)
    rows = {r["id"]: r for r in conn.execute("SELECT id, rel, sibling, n_faces, kind, duration FROM photos WHERE status='ok'")}
    seg_photo = {r[0]: r[1] for r in conn.execute("SELECT id, photo_id FROM segments")}
    scored = _score(np.vstack([M, SM]), T, owner, names) if len(M) + len(SM) else []
    photos, segments = [], []
    for k, pid in enumerate(ids.tolist()):
        r = rows.get(pid)
        if r is None:
            continue
        cat, score, margin, guess, guess_score, probs = scored[k]
        if r["kind"] == "video" and (r["duration"] or 0.0) >= INTERVIEW_MIN_DURATION_S and {cat, guess} & {"people", "interview"}:
            score = max(probs.get("people", 0.0), probs.get("interview", 0.0))
            cat, guess, guess_score = "interview", "interview", score
        if people_by_faces and (r["n_faces"] or 0) >= 1:
            # A face makes it a people photo, but the scene it was shot in is kept as the guess
            # (beach, office, road) so the scene's tile still finds it. A people/interview scene
            # or a fallback has nothing to keep.
            scene, sp = max(((k, v) for k, v in probs.items() if k not in ("people", "interview")),
                            key=lambda kv: kv[1], default=(None, 0.0))
            if cat not in ("people", "interview", CATEGORY_FALLBACK):
                guess, guess_score = cat, score
            elif scene is not None and sp >= SCENE_GUESS_MIN_PROB:
                guess, guess_score = scene, sp
            else:
                guess, guess_score = "people", 1.0
            cat, score = "people", 1.0
        photos.append(dict(id=pid, rel=r["rel"], sibling=r["sibling"], category=cat,
                           score=round(score, 4), margin=round(margin, 4), n_faces=r["n_faces"],
                           guess=guess, guess_score=round(guess_score, 4)))
    for k, sid in enumerate(seg_ids.tolist()):
        cat, score = scored[len(ids) + k][:2]
        segments.append(dict(id=sid, photo_id=seg_photo.get(sid), category=cat, score=round(score, 4)))
    photos.sort(key=lambda d: (d["category"], -d["score"]))
    return photos, segments

def classify(root: Path, people_by_faces: bool = True) -> list[dict]:
    """Returns one dict per indexed photo or video: rel, sibling, category, score, margin, n_faces.
    score/margin are softmax probabilities (not raw cosine): score is how much of the probability
    mass the winning bucket (a real category, or the "other" pseudo-category) owns, margin is its
    lead over the runner-up. A face-bearing photo is always "people" regardless of these."""
    return _classify_all(root, people_by_faces=people_by_faces)[0]

def aerial_gap(M: np.ndarray, embedder) -> tuple[np.ndarray, np.ndarray]:
    """Per row of M: (best aerial cosine minus best ground cosine, best aerial cosine), each shape (N,).
    The text matrix is encoded once per embedder and kept on it, re-encoded if the prompt lists change."""
    if len(M) == 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    T = getattr(embedder, "_aerial_T", None)
    if T is None or len(T) != len(AERIAL_PROMPTS) + len(GROUND_PROMPTS):
        T = embedder.encode_text(AERIAL_PROMPTS + GROUND_PROMPTS)
        embedder._aerial_T = T
    S = M @ T.T
    a = S[:, :len(AERIAL_PROMPTS)].max(axis=1)
    g = S[:, len(AERIAL_PROMPTS):].max(axis=1)
    return (a - g).astype(np.float32), a.astype(np.float32)

def flag_aerial(root: Path) -> int:
    """The drone pass, two steps. First a backfill by filename: index_folder never re-probes an unchanged file,
    so rows indexed before the aerial column existed sit at 0 whatever their metadata says; the DJI_ basename
    rule the index applies (video.aerial_by_name, features.exif_info) is re-applied here from the rel alone,
    no disk, no embedding, so it works with the source unmounted. It covers the filename rule only: an
    encoder-tag-only clip, and the camera column, still need a re-index of the file. Then the zero-shot
    step: every ok, embedded row still at 0 gets aerial=1 when its aerial gap is at least AERIAL_MIN_GAP and
    its best aerial cosine clears MIN_COSINE. Rows already 1 (metadata, backfill, an earlier pass) are left
    alone. Returns how many rows were flagged this time, both steps together."""
    from .embed import get_embedder
    root = Path(root); conn = db.connect(root)
    named = [(int(r["id"]),) for r in conn.execute("SELECT id, rel FROM photos WHERE aerial=0")
             if Path(r["rel"]).name.upper().startswith("DJI_")]
    conn.executemany("UPDATE photos SET aerial=1 WHERE id=?", named)
    conn.commit()
    rows = conn.execute("SELECT id, embed FROM photos WHERE status='ok' AND embed IS NOT NULL AND aerial=0 ORDER BY id").fetchall()
    if not rows:
        return len(named)
    M = np.stack([np.frombuffer(r["embed"], np.float16).astype(np.float32) for r in rows])
    gap, best = aerial_gap(M, get_embedder())
    hits = [(int(r["id"]),) for r, d, b in zip(rows, gap, best) if float(d) >= AERIAL_MIN_GAP and float(b) >= MIN_COSINE]
    conn.executemany("UPDATE photos SET aerial=1 WHERE id=?", hits)
    conn.commit()
    return len(named) + len(hits)

def classify_and_store(root: Path, people_by_faces: bool = True) -> dict[str, int]:
    """Runs the stacked pass and persists category + category_score (and the best real guess with its
    probability) onto photos (and videos), and category + score onto their segments, then the separate
    zero-shot drone pass (flag_aerial). Returns counts per category over photos and videos, the same rows
    the Categories tab lists (segments are not counted; aerial is a flag, not a category)."""
    from collections import Counter
    root = Path(root)
    results, segs = _classify_all(root, people_by_faces=people_by_faces)
    conn = db.connect(root)
    conn.executemany("UPDATE photos SET category=?, category_score=?, category_guess=?, category_guess_score=? WHERE id=?",
                      [(r["category"], r["score"], r["guess"], r["guess_score"], r["id"]) for r in results])
    conn.executemany("UPDATE segments SET category=?, category_score=? WHERE id=?",
                      [(r["category"], r["score"], r["id"]) for r in segs])
    conn.commit()
    flag_aerial(root)
    return dict(Counter(r["category"] for r in results))

# Discovered categories

def discover_k(n: int) -> int:
    """Clusters for n embedded photos: sqrt(n / 6) clamped to 4..DISCOVER_MAX_K (270 items -> 7, 3,677 -> 24).
    Finer than the first sqrt(n / 25): on the first video shoot k=4 gave "man in a kurta 99 / bhoomi pujan
    98 / shore 45 / panel discussion 28" while k=8 split out the vendor, the scooter, the shore and the panel
    discussion. DISCOVER_MIN_SIZE folding keeps tiny clusters away at the finer k."""
    return min(DISCOVER_MAX_K, max(4, round((n / 6) ** 0.5)))

def _vocab_matrix(embedder) -> tuple[list[str], np.ndarray]:
    """(labels, unit text matrix) for VOCAB, encoded once per embedder and kept on it."""
    T = getattr(embedder, "_vocab_T", None)
    if T is None or len(T) != len(VOCAB):
        T = embedder.encode_text(list(VOCAB))
        embedder._vocab_T = T
    return list(VOCAB), T

def _fold_small(labels: np.ndarray, M: np.ndarray) -> np.ndarray:
    """Merge every cluster under DISCOVER_MIN_SIZE into the cluster whose centroid is nearest (cosine),
    smallest first, recomputing centroids as it goes, until nothing small is left or one cluster remains.
    Labels come back renumbered 0..m-1 in order of first appearance."""
    labels = labels.copy()
    while True:
        names, sizes = np.unique(labels, return_counts=True)
        if len(names) <= 1:
            break
        small = [(int(s), int(c)) for s, c in zip(sizes, names) if s < DISCOVER_MIN_SIZE]
        if not small:
            break
        _, victim = min(small)
        cents = {int(c): M[labels == c].mean(axis=0) for c in names}
        for c in cents:
            cents[c] /= (np.linalg.norm(cents[c]) or 1.0)
        others = [int(c) for c in names if int(c) != victim]
        sims = [float(cents[victim] @ cents[c]) for c in others]
        labels[labels == victim] = others[int(np.argmax(sims))]
    order = {int(c): i for i, c in enumerate(dict.fromkeys(labels.tolist()))}
    return np.array([order[int(c)] for c in labels], np.int64)

def _vote(top: np.ndarray, scores: np.ndarray, n_labels: int) -> tuple[int, np.ndarray]:
    """top: each member's best label index; scores: that member's score for it. Returns (the label most
    members picked, ties by summed score then index; every label's share of the members)."""
    votes = np.bincount(top, minlength=n_labels)
    summed = np.zeros(n_labels); np.add.at(summed, top, scores)
    order = np.lexsort((np.arange(n_labels), -summed, -votes))
    return int(order[0]), votes / len(top)

def discover(root: Path, k: int | None = None) -> list[dict]:
    """Cluster the shoot's embeddings (photos and videos alike) with k-means and name every cluster from
    VOCAB, or leave it an unnamed "group N" when no label can be trusted. Returns [{id, name, named, size,
    score, photo_ids, photo_scores}] sorted by size desc: score is the centroid's plain cosine to the chosen
    label (0 when unnamed), photo_scores are each member's cosine to the centroid rescaled to 0..1 across
    the cluster (the "less sure" half sits below 0.5). Deterministic for a fixed index. Empty below
    DISCOVER_MIN_PHOTOS embedded photos.

    The candidate is the label with the highest cos(centroid, label) - DISCOVER_CONTRAST * cos(shoot mean,
    label): what makes this cluster different from the rest of the shoot rather than what fits every photo
    of it (on a shoot that is all one man, plain cosine names every cluster after him). The candidate then
    has to be what the members say, one vote each, and every gate below turns the cluster into "group N"
    (numbered by size among the unnamed) rather than hand it a second-best word:
      1. it must be the outright winner of the member vote, on plain cosine for a VOCAB_PEOPLE label
         (who is in the frame is read off the members themselves, or the white shirts of an interview
         set become "doctor"), on contrast cosine for any other label (what sets a beach cluster apart
         is the beach, even though "man in a kurta" fits each frame better);
      2. that share of members must reach DISCOVER_VOTE_SHARE (DISCOVER_LOADED_SHARE for VOCAB_LOADED:
         a centroid of 37 unrelated B-roll clips matched "screenshot" best while no single clip did);
      3. plain cos(centroid, label) must reach DISCOVER_NAME_MIN_COS;
      4. a label already carried by a bigger cluster is not given away to a second-best word: the smaller
         cluster keeps it with a number ("seated interview 2"), because it IS more of the same. It earns the
         number only by passing gates 1 to 3 on its own; a cluster that cannot name itself is still "group N".
         Measured on the first corporate documentary: 5 of the 7 unnamed groups (480 of 955 items) were
         nameless only because a bigger cluster had taken their word, one of them with 89% of its own votes."""
    from sklearn.cluster import KMeans
    root = Path(root); conn = db.connect(root)
    ids, M = db.load_embeds(conn)
    n = len(ids)
    if n < DISCOVER_MIN_PHOTOS:
        return []
    k = min(n, k if k is not None else discover_k(n))
    labels = KMeans(n_clusters=k, n_init=4, random_state=0).fit_predict(M)
    labels = _fold_small(labels, M)
    from .embed import get_embedder
    vocab, T = _vocab_matrix(get_embedder())
    g = M.mean(axis=0); g /= (np.linalg.norm(g) or 1.0)
    shoot_scores = T @ g                                  # how much each label fits the whole shoot
    S = M @ T.T                                           # every member against every label
    C = S - DISCOVER_CONTRAST * shoot_scores
    plain_top, contrast_top = S.argmax(axis=1), C.argmax(axis=1)
    clusters = []
    for c in range(int(labels.max()) + 1):
        idx = np.where(labels == c)[0]
        cent = M[idx].mean(axis=0); cent /= (np.linalg.norm(cent) or 1.0)
        cos = M[idx] @ cent
        lo, hi = float(cos.min()), float(cos.max())
        rescaled = (cos - lo) / (hi - lo) if hi > lo else np.ones_like(cos)
        clusters.append(dict(idx=idx, cent=cent, size=len(idx), first=int(ids[idx].min()), photo_scores=rescaled))
    clusters.sort(key=lambda c: (-c["size"], c["first"]))
    used: set[str] = set()
    out, unnamed = [], 0
    for i, c in enumerate(clusters):
        idx = c["idx"]
        plain = T @ c["cent"]
        p = int(np.argmax(plain - DISCOVER_CONTRAST * shoot_scores))
        name = vocab[p]
        if name in VOCAB_PEOPLE:
            winner, share = _vote(plain_top[idx], S[idx, plain_top[idx]], len(vocab))
        else:
            winner, share = _vote(contrast_top[idx], C[idx, contrast_top[idx]], len(vocab))
        need = DISCOVER_LOADED_SHARE if name in VOCAB_LOADED else DISCOVER_VOTE_SHARE
        ok = bool(winner == p and share[p] >= need and float(plain[p]) >= DISCOVER_NAME_MIN_COS)
        if ok:
            if name in used:                      # the same thing, split by k-means: number it, never rename it
                nth = 2
                while f"{name} {nth}" in used:
                    nth += 1
                name = f"{name} {nth}"
            used.add(name); score = float(plain[p])
        else:
            unnamed += 1; name, score = f"group {unnamed}", 0.0
        out.append(dict(id=i, name=name, named=ok, size=c["size"], score=round(score, 4),
                        photo_ids=[int(q) for q in ids[idx]],
                        photo_scores=[round(float(v), 4) for v in c["photo_scores"]]))
    return out

def discover_and_store(root: Path, k: int | None = None) -> dict[str, int]:
    """Runs discover and persists cluster + cluster_score on photos. Returns {name: size}. A shoot too small
    to discover anything stores nothing and returns {}."""
    root = Path(root)
    found = discover(root, k=k)
    if not found:
        return {}
    conn = db.connect(root)
    conn.execute("UPDATE photos SET cluster=NULL, cluster_score=NULL")
    for c in found:
        conn.executemany("UPDATE photos SET cluster=?, cluster_score=? WHERE id=?",
                         [(c["name"], s, pid) for pid, s in zip(c["photo_ids"], c["photo_scores"])])
    conn.commit()
    return {c["name"]: c["size"] for c in found}

def rename_cluster(root: Path, old: str, new: str) -> int:
    """The user's correction for a discovered category: every status='ok' row whose cluster is `old` takes
    `new` (cluster_score untouched). Returns the rows moved (0 when nothing carries `old`). Sanitised like
    a person's name: stripped, must not be empty, "." or ".." (export folders), must not be a name another
    discovered category already carries, and must not take the reserved "group N" form the UI shows as
    unnamed. Lost on the next Categorise, which names every cluster afresh."""
    from .export import safe_segment
    new = (new or "").strip()
    if not new:
        raise ValueError("give the group a name")
    try:
        safe_segment(new)
    except ValueError:
        raise ValueError("that name cannot be used as a folder")
    if is_unnamed_group(new):
        raise ValueError("group N is reserved for unnamed groups")
    conn = db.connect(Path(root))
    if new != old and new in db.cluster_counts(conn):
        raise ValueError(f"there is already a group called {new!r}")
    cur = conn.execute("UPDATE photos SET cluster=? WHERE cluster=? AND status='ok'", (new, old))
    conn.commit()
    return cur.rowcount

def write_manifest(root: Path, results: list[dict]) -> Path:
    """categories.csv + one folder of symlinks per category under the Desktop export dir.
    Symlinks point at the files on the disk; nothing is copied, nothing is written under root.
    A shoot with per-day/per-location subfolders can have the same filename in several places, so
    each symlink is named after its full relative path ('/' -> '__') rather than the bare filename;
    that also shows at a glance where the photo came from. True collisions (same rel-derived name,
    which only happens if the shoot itself already used '__' in a folder name) fall back to an id prefix."""
    root = Path(root); base = export_dir(root, "categories"); base.mkdir(parents=True, exist_ok=True)
    for cat in list(CATEGORIES) + [FALLBACK]:
        d = base / cat
        if d.exists():
            for old in d.iterdir():
                if old.is_symlink(): old.unlink()
        d.mkdir(exist_ok=True)
    with open(base / "categories.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["category", "score", "margin", "faces", "jpeg", "raw"])
        for r in results:
            src = root / r["rel"]; raw = (root / r["sibling"]) if r["sibling"] else None
            w.writerow([r["category"], r["score"], r["margin"], r["n_faces"], str(src), str(raw) if raw else ""])
            for f, rel in ((src, r["rel"]), (raw, r["sibling"])):
                if f is None: continue
                name = rel.replace("/", "__")
                dst = base / r["category"] / name
                if dst.exists() or dst.is_symlink():
                    dst = base / r["category"] / f"{r['id']}_{name}"
                os.symlink(f, dst)
    return base

def apply_on_disk(root: Path, results: list[dict], dry_run: bool = True) -> Path:
    """EXPLICIT OPT-IN ONLY. Moves each JPEG and its RAW sibling into <root>/_sorted/<category>/
    (same-volume rename, no copy). Writes <export>/undo.csv (new_path,old_path) first so it can be reversed.
    With dry_run=True nothing on the disk changes; the plan is written to <export>/move-plan.csv."""
    root = Path(root); base = export_dir(root, "categories"); base.mkdir(parents=True, exist_ok=True)
    plan = []
    for r in results:
        for rel in (r["rel"], r["sibling"]):
            if not rel: continue
            src = root / rel
            if not src.exists() or "_sorted" in Path(rel).parts: continue
            plan.append((src, root / "_sorted" / r["category"] / src.name))
    with open(base / ("move-plan.csv" if dry_run else "undo.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["new_path", "old_path"])
        for src, dst in plan: w.writerow([str(dst), str(src)])
    if dry_run:
        return base / "move-plan.csv"
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst = dst.with_name(f"{src.stat().st_ino}_{src.name}")
        shutil.move(str(src), str(dst))
    return root / "_sorted"

def undo_on_disk(undo_csv: Path) -> int:
    n = 0
    with open(undo_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            new, old = Path(row["new_path"]), Path(row["old_path"])
            if new.exists() and not old.exists():
                old.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(new), str(old)); n += 1
    return n
