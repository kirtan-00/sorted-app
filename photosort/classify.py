"""Zero-shot scene categories on top of the index. Never writes under the shoot root
unless apply_on_disk() is called explicitly (a same-volume move with an undo log)."""
from __future__ import annotations
import csv, os, re, shutil
from pathlib import Path
import numpy as np
from . import db
from .export import export_dir
from .vocab import VOCAB, VOCAB_PEOPLE, VOCAB_LOADED
from .config import CATEGORY_FALLBACK, CLIP_MODEL, SURE_MIN, INTERVIEW_MIN_DURATION_S

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
# Prompts one encoder needs and the other must not have. CATEGORIES above is the set measured under
# MobileCLIP and it does not move; this table is appended to it for the running encoder only, and the
# keys of CATEGORIES (the folder list, the tile order) are untouched either way.
#
# What is in here and why. Under SigLIP, 163 of the 955 photographs of NSG_26 Part 2 were tight single
# subjects on the interview set, same lamp, same wall, that MobileCLIP filed under interview and SigLIP
# filed under people. The suspicion was a count bias, interview having 3 prompts against people's 7, and
# it is wrong: over the 12 categories the correlation between a category's prompt count and its share
# of the shoot is NEGATIVE under both encoders (-0.40 MobileCLIP, -0.24 SigLIP; interview holds 630 and
# 466 photos on 3 prompts), and against 1,000 contentless random unit vectors the count correlates at
# +0.58 under MobileCLIP but -0.05 under SigLIP, so whatever structural bias exists is not SigLIP's and
# photograph content swamps it anyway. The real cause is one prompt: "a person standing and looking at
# the camera" wins 160 of the 163, and of the 35 ways to cut people back to 3 prompts every one that
# keeps that prompt flips none of the 163 while cutting it flips most of them. Adding these four beats
# it on those frames: 126 of the 163 come back to interview, the two encoders go from agreeing on 740
# of 955 to agreeing on 866, all 740 earlier agreements hold, no other photograph of the shoot moves,
# and "other" stays at 12. Deleting the offending people prompt instead was measured and is worse: 54
# of 163, and "other" rises to 16.
#
# MobileCLIP does not get them, and that is a measurement rather than caution. The same four prompts
# under MobileCLIP move 9 photographs and 4 of the 9 are wrong: two empty offices and two beach
# two-shots become interviews, which is the exact failure the CATEGORIES comment above records ("an
# empty set is office, not interview"). Under SigLIP office holds 41 of 41 and beach 40 of 40.
# MobileCLIP's four thresholds carry evidence from a 3,677 photo event shoot and the Diu shoot whose
# disks are unplugged, so its scoring cannot be changed and then re-derived.
EXTRA_PROMPTS: dict[str, dict[str, list[str]]] = {
    "MobileCLIP-S1": {},
    "ViT-B-16-SigLIP": {
        "interview": ["a close up of a person answering questions on an interview set",
                      "an interview subject seated in front of a plain wall with a lamp beside them",
                      "a person mid sentence during a filmed interview, chair and framed pictures behind them",
                      "a single person filmed for an interview, seated and facing the camera"],
    },
}

def prompts_for(model: str) -> dict[str, list[str]]:
    """CATEGORIES with one encoder's measured extra prompts appended, in CATEGORIES order. An encoder
    nobody has measured is an error for the same reason calibration_for raises: lending it another
    encoder's prompts produces a scan that finishes and reports no failure."""
    try:
        extra = EXTRA_PROMPTS[model]
    except KeyError:
        raise ValueError(f"no classify prompts for {model!r}; measured: {sorted(EXTRA_PROMPTS)}") from None
    return {cat: prompts + extra.get(cat, []) for cat, prompts in CATEGORIES.items()}

PROMPTS = prompts_for(CLIP_MODEL)
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
# Every threshold below is a property of one encoder's similarity distribution, not of the categories, so
# they sit in a table keyed by CLIP_MODEL rather than as bare numbers. Swapping the encoder moves all of
# them at once: SigLIP's image-to-text cosines run about a third of MobileCLIP's, and MobileCLIP's
# MIN_COSINE of 0.12 sent 866 of the 955 photos of NSG_26 Part 2 to "other" and left all 13 of that
# shoot's discovered clusters unnamed. An encoder with no entry here raises at import rather than
# borrowing MobileCLIP's numbers, because borrowing them looks like it worked.
CALIBRATION: dict[str, dict[str, float]] = {
    "MobileCLIP-S1": dict(
        TEMPERATURE=100.0,   # CLIP's logit scale; turns cosine similarity into a peaked softmax
        MIN_PROB=0.35,        # best category must own at least this much of the softmax mass: "other"
        MIN_PROB_MARGIN=0.15,  # best minus second-best probability; smaller means ambiguous: "other"
        # T=100 amplifies even meaningless cosine gaps into a "confident" softmax: on the calibration set every
        # correctly-classified real photo's winning raw cosine was >= 0.1497, while a random (non-photo) unit
        # vector's best raw cosine was 0.0814 despite a deceptively "confident" softmax. This absolute floor
        # catches that case; the two MIN_PROB* thresholds above then separate genuinely ambiguous real photos.
        MIN_COSINE=0.12,
        # best aerial cosine minus best ground cosine; the best aerial cosine must also clear MIN_COSINE.
        # The gate is a raw cosine gap, not a softmax: at TEMPERATURE 100 a two-way
        # softmax at 0.7 needs a gap of only 0.0085 and flagged 180 of 3,677 ground-only Sony photos; the 99th
        # percentile ground-only gap on two Sony shoots was 0.041 / 0.047, so 0.05 keeps false positives under 1%.
        # Recall on real drone footage is unmeasured until 02 Drone is embedded.
        AERIAL_MIN_GAP=0.05,
        # Plain cos(centroid, label) of the winner. Never decided a real cluster once the votes had (the lowest
        # named plain cosine was 0.203, "fishing boats"; the fixed classifier's single-image floor is 0.12);
        # a sanity floor for a cluster of nothing whose members still agree on a word.
        DISCOVER_NAME_MIN_COS=0.16,
    ),
    "ViT-B-16-SigLIP": dict(
        # SigLIP's own trained logit scale, read off model.logit_scale.exp(). MobileCLIP-S1's is really
        # 64.25; the 100.0 above is CLIP's textbook value rather than that model's, and it stays because
        # the two MIN_PROB numbers under it were calibrated against a softmax at 100. SigLIP also carries
        # a logit_bias of -12.93, which is deliberately not used: it is a constant added to every logit
        # and a softmax is shift invariant, so it cancels top and bottom and cannot change a winner, a
        # share or a margin. It is worth knowing anyway, because it is the plainest reading of why
        # SigLIP's cosines look small: 12.93 / 117.33 = 0.110 is where SigLIP itself starts calling a
        # caption true, and the median winning cosine over 955 real photos is 0.1161 (it was 0.0986
        # before EXTRA_PROMPTS gave interview its four extra descriptions, which is the whole of the
        # difference: a max over more prompts can only rise).
        TEMPERATURE=117.33,
        # Measured, not inherited, and re-measured under the seven interview prompts. Over the same 955
        # photos of NSG_26 Part 2 these two reject 12 photos at SigLIP's temperature against 13 at
        # MobileCLIP's, which is not a difference worth moving a threshold for, and the four added
        # prompts leave that 12 exactly where it was. On that shoot the margin does nearly all the work
        # and MIN_PROB almost none: 0 photos under SigLIP, 1 under MobileCLIP. The 12 is 11 caught by
        # the margin plus the one photo that wins "__other__" outright.
        MIN_PROB=0.35,
        MIN_PROB_MARGIN=0.15,
        # Not the same kind of number as the 0.12 above, and that comment does not carry over. Over the
        # 955 photos, 1,000 random unit vectors reach a best cosine of 0.1276 at the 99th percentile while
        # the photos' own median is 0.1161: under SigLIP a random direction outscores a real photograph,
        # so no floor separates them. (Repeating that measurement at 1,000 draws puts MobileCLIP's random
        # 99th at 0.1409, above its own 0.12, so that floor never separated them either; the 0.0814 above
        # was a single draw.) What actually catches a frame of nothing, under both encoders, is
        # NEGATIVE_PROMPTS: flat black, white and grey frames all win "__other__" outright. So this is set
        # to do the one job the floor demonstrably does, sit under every real photograph of a shoot. It
        # rejects 0 of the 955 (their lowest winning cosine is 0.0210, the same photograph and the same
        # number under the seven interview prompts as under three) and still catches a flat blue frame,
        # which scores 0.0038 and would otherwise be filed under "ocean".
        MIN_COSINE=0.02,
        # The same rule as the 0.05 above, applied to the one ground-only shoot measured under SigLIP. The
        # highest ground gap over the 955 frames of NSG_26 Part 2 is 0.0133 under SigLIP and 0.0469 under
        # MobileCLIP, and 0.05 is 1.07 times that, so 0.015 is 1.07 times SigLIP's. It flags 0 of 955.
        # This is the weakest of the five: one shoot where 0.05 had two, a maximum rather than a 99th
        # percentile because one shoot's 99th leaves 9 known false positives on the table, and recall on
        # real drone footage unmeasured under this encoder exactly as it is under the other.
        AERIAL_MIN_GAP=0.015,
        # 0.785 of the lowest cluster cosine that named itself correctly on NSG_26 Part 2 (0.0637, a beach
        # cluster of 45 checked against its contact sheet), which is the same fraction 0.16 is of the 0.203
        # above. Six of that shoot's 13 clusters clear both vote gates under SigLIP and all six were
        # stopped by the 0.16 floor alone; 0.05 names all six and touches none of the seven the votes had
        # already rejected. Still a floor and not a ranking: the highest of the six (0.1099, "man in a
        # kurta" on men in ordinary shirts) is the one that is wrong and the four lowest are all right.
        DISCOVER_NAME_MIN_COS=0.05,
    ),
}

def calibration_for(model: str) -> dict[str, float]:
    """The six thresholds measured for one encoder. An encoder nobody has measured is an error, not a
    fallback: MobileCLIP's numbers on another encoder produce a scan that finishes, reports no failure,
    and files most of the shoot under "other"."""
    try:
        return CALIBRATION[model]
    except KeyError:
        raise ValueError(f"no classify calibration for {model!r}; measured: {sorted(CALIBRATION)}") from None

_CAL = calibration_for(CLIP_MODEL)
TEMPERATURE = _CAL["TEMPERATURE"]
MIN_PROB = _CAL["MIN_PROB"]
MIN_PROB_MARGIN = _CAL["MIN_PROB_MARGIN"]
MIN_COSINE = _CAL["MIN_COSINE"]
AERIAL_MIN_GAP = _CAL["AERIAL_MIN_GAP"]
# Drone shots. The index sets photos.aerial from metadata (DJI tags, a DJI_ filename, an .SRT sidecar);
# for rows still 0 a zero-shot aerial/ground prompt pair decides drone shots from other cameras. It is its
# own rule, not a category: a drone shot of a beach stays "beach" and is aerial too. A metadata 1 is never
# re-decided. The prompts avoid the word "drone": it matches a photograph OF a drone (the top false hit on
# the 3,677-photo index). AERIAL_MIN_GAP is in CALIBRATION above, with the measurement that set it.
AERIAL_PROMPTS = ["an aerial view from high above the ground", "a top-down view of a coastline from the air",
                  "a bird's eye view of a town from the air"]
GROUND_PROMPTS = ["a photo taken at eye level from the ground", "a portrait of a person", "a street seen from the pavement"]

# ===== shot size =====
# close / medium / wide, the axis an editor reaches for first. Zero-shot over the embeddings the index
# already holds, scored by _score exactly as CATEGORIES are, so it costs a matrix multiply and no disk.
#
# Why the prompts are shaped like this, measured on the client's 1,627 item index (487 stills, 1,140
# clips, all embedded under ViT-B-16-SigLIP). The distribution that ships is medium 679 (41.7%), wide 547
# (33.6%), unknown 360 (22.1%), close 41 (2.5%). Nothing collapses into one bucket, which was the failure
# to watch for on an event shoot where almost everything is a room with people in it.
#
# The one prompt that was cut is worth recording, because it moved 90 items on its own: "a medium shot of
# a small group at a table" pulled whole meeting rooms into medium, and on the contact sheets those are
# wide shots of a room that happens to contain a table. Dropping it moves office from 49% medium to 47%
# wide and lifts the corroborating face measurement below. Adding a fifth close prompt ("a close up of a
# single object on a table") was measured too and is worse: close grows from 41 to 110 but 22 of the new
# ones are office frames, so it buys volume by being wrong.
#
# Face geometry is NOT the primary signal, and the numbers are the reason rather than a preference. Only
# 378 of the 1,627 rows carry a face at all, so it says nothing whatsoever about the other 1,249, and the
# faces that exist are tiny: measured against the 1024 px preview the detector actually ran on (NOT
# against photos.width/height, which are the original 6000 px dimensions and deflate the share by up to
# 86x), the median face is 0.16% of the frame and the largest in the whole shoot is 6.1%. A real close up
# puts a face at 10% or more. So geometry alone would file essentially the entire shoot as wide.
# What it is good for is corroboration, and it corroborates: over the 378 rows that have a face, the
# median face share is 0.0066 for rows this pass calls medium, 0.0037 for unknown and 0.0022 for wide, a
# clean 3x ordering that nothing in the prompts knows about.
#
# Measured under SigLIP only. MobileCLIP-S1 gets the same table because these are descriptions of framing
# rather than of an encoder's similarity distribution, but its distribution on a real shoot is unmeasured;
# the thresholds underneath (TEMPERATURE, MIN_PROB, MIN_PROB_MARGIN, MIN_COSINE) are already per encoder.
SHOT_SIZES: dict[str, list[str]] = {
    "close": ["a close up of a face filling the frame",
              "a tight head and shoulders portrait",
              "an extreme close up of a detail, shallow depth of field",
              "a close up of hands holding something"],
    "medium": ["a medium shot of a person from the waist up",
               "a person framed from the chest up, some of the room visible behind",
               "two people at half length talking to each other"],
    "wide": ["a wide establishing shot of a whole room",
             "a wide shot of a street with the buildings visible top to bottom",
             "a distant view of a landscape with a horizon",
             "a full length shot of people standing, the whole body in frame",
             "an aerial wide view of a town"],
}
# What a row gets when the encoder will not commit. It is a real answer, not a bucket to hide in: 22% of
# the client's shoot lands here and the contact sheet for it is shop signage, a flag on a pole, a display
# shelf, a hand pointing at a poster, all of them genuinely between medium and wide. A filter that
# guessed at those would be a filter that lies, so the row says unknown and the rail can count it.
SHOT_SIZE_UNKNOWN = "unknown"
# Every value the column is ever allowed to hold, in the order the rail shows them. NULL is not in here:
# that is the absence of an answer, not one of the answers.
SHOT_SIZE_NAMES = (*SHOT_SIZES, SHOT_SIZE_UNKNOWN)

# Clips get a second opinion from their faces. A clip's frames are 1024 px video frames where a person is
# usually the subject, not the 6000 px event stills the paragraph above measured, and there the face is
# neither rare nor tiny: on the 476 pinned segment frames of NSG_26 Part 2 (tests/fixtures), YuNet finds a
# face on 358, and the largest face's height as a share of the frame height has a median of 0.239 on the
# frames hand labelled medium against 0.096 on the ones labelled wide. That is the split the embedding misses:
# it calls 73 of 183 wide frames medium (wide recall 0.33), because "a person, some room behind them" reads as
# medium whatever the distance.
#
# The rule (refine_shot_size): no face, keep the embedding's answer; the embedding said close, keep close (the
# close ups of this shoot are hands, logos and texture, which have no face to measure); a face at least
# FACE_CLOSE_MIN_H of the frame is close; at least FACE_MEDIUM_MIN_H is medium; smaller is wide. Measured
# against hand labels of the 474 labelled frames (counting "unknown" as wrong): the embedding alone gets
# 61.6%, the rule 77.0%. FACE_MEDIUM_MIN_H was picked on half the frames and checked on the other half both
# ways round: 0.15 is best or tied best on each half alone (78.4% on the even frames, 75.6% on the odd), and
# scores 75.6% on the odd half against the embedding's 61.3% there and 78.4% on the even half against 61.9%,
# so it is not fitted to the numbers it reports.
# At clip level, where the filter acts (face_h is the median over a clip's frames, index.clip_face_summary),
# the 318 clips whose segments agree on a label go from 58.2% to 77.4%. FACE_CLOSE_MIN_H sits above the 90th
# percentile of the medium frames' faces (0.342) and changes one frame of 476; it is there so a real face close
# up is never filed as medium, not because this shoot had many.
#
# face_h is measured against the frame's short edge (index.clip_face_summary says why: on a vertical phone
# clip the height is the long edge and a waist up face is half the share it is in landscape). The NSG frames
# are all landscape, so every number above is the same under either reading. On the GT 1 TB event shoot,
# which is mostly vertical phone footage and has no hand labels, a by-eye check of 29 clips the rule moved
# from medium to wide found 16 right measured against the height and 25 against the short edge. Over that
# shoot's 1,140 clips the rule moves 147 from medium to wide, 40 the other way and 20 to close; a spot check, not a
# measurement, and the place to look first if wide shots of that shoot read wrong.
#
# Photos are left to the embedding alone. Their face table is measured on a different preview and the
# paragraph above says what it found there; nothing here was checked on stills.
FACE_CLOSE_MIN_H = 0.45
FACE_MEDIUM_MIN_H = 0.15

def refine_shot_size(size: str, face_h: float | None) -> str:
    """The zero-shot shot size, second-guessed by the clip's face size (see FACE_MEDIUM_MIN_H for the rule and
    the measurement). face_h None or 0 means no face was found, or none was looked for: the embedding stands."""
    if not face_h or face_h <= 0 or size == "close":
        return size
    if face_h >= FACE_CLOSE_MIN_H:
        return "close"
    return "medium" if face_h >= FACE_MEDIUM_MIN_H else "wide"

# Camera, locked off versus moving, is deliberately NOT here, and this is the measurement rather than a
# shrug. The frames are on disk (7,757 of them under frames/, so no shoot disk is needed) and global
# motion between consecutive frames does separate the extremes: the five stillest clips of the shoot are
# four phone screen recordings and a logo animation, and the five busiest are a flying drone and handheld
# coverage walking through a meeting room. It falls apart in the middle, for a reason no threshold fixes.
# The sampled frames sit 1.5 to 10 seconds apart (VIDEO_FRAMES = 6 across 5% to 95% of the clip), and at
# that spacing the measurement cannot tell a camera that moved from a subject that walked across a still
# frame. Two numbers say so. Per second of clip, the displacement correlates with log duration at -0.50,
# so half of what it measures is how long the clip is; the duration-independent form (total displacement
# per pair) drops that to 0.145 and is the one worth quoting. On that form the 166 drone clips, the only
# class in the shoot with a metadata ground truth and the only one that is moving by definition, have a
# median of 0.0148 against 0.0330 for handheld "people" coverage: the detector ranks flying drones as
# less moving than the shoot average. Set a two-sided threshold anywhere useful and 14 of the 166 drone
# clips come back labelled locked off, which is the exact bucket an editor opens to find tripod shots.
# What would work is frames a fraction of a second apart, three at each sample point rather than one.
# That is a decode pass over 1,140 clips off the shoot disk, which is the rescan this task exists to
# avoid, so the honest answer is that the facet stays off until someone is willing to pay for it.
# Looked at again once clips had faces (2026-10): the face boxes sit on the same frames, seconds apart, so a
# face that stays put says the subject AND the camera held still and a face that moves says one of them did.
# That is the same confound with a smaller sample, and the case it does catch, a seated talking head in a
# fixed frame, is already the interview category. No new signal, so still off.

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
# Plain cos(centroid, label) of the winner: a sanity floor for a cluster of nothing whose members still
# agree on a word. Per encoder, in CALIBRATION above with the measurement that set it.
DISCOVER_NAME_MIN_COS = _CAL["DISCOVER_NAME_MIN_COS"]
UNNAMED_GROUP = re.compile(r"^group \d+$")

def is_unnamed_group(name: str) -> bool:
    """True for the reserved "group N" names discover() gives clusters it could not name truthfully."""
    return bool(UNNAMED_GROUP.match(name or ""))

def _prompt_matrix(embedder) -> tuple[list[str], np.ndarray, list[int]]:
    """names includes CATEGORIES keys followed by one pseudo-category "__other__" owning
    NEGATIVE_PROMPTS, so callers that only want the real categories should slice names[:-1].
    The prompts come from PROMPTS, which is CATEGORIES plus the running encoder's measured extras, so
    the names (and the folders behind them) are the same list under every encoder."""
    names, texts, owner = [], [], []
    for i, (cat, prompts) in enumerate(PROMPTS.items()):
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

def _shot_matrix(embedder) -> tuple[list[str], np.ndarray, np.ndarray]:
    """(names, unit text matrix, owner) for SHOT_SIZES, with the same "__other__" pseudo-bucket owning
    NEGATIVE_PROMPTS that _prompt_matrix gives the categories: a frame of nothing must be able to win
    outright rather than being forced into the nearest framing. Encoded once per embedder and kept on it,
    re-encoded if the prompt lists change, exactly as aerial_gap and _vocab_matrix do."""
    names, texts, owner = [], [], []
    for i, (size, prompts) in enumerate(SHOT_SIZES.items()):
        names.append(size)
        for p in prompts:
            texts.append(p); owner.append(i)
    neg = len(names)
    names.append("__other__")
    for p in NEGATIVE_PROMPTS:
        texts.append(p); owner.append(neg)
    T = getattr(embedder, "_shot_T", None)
    if T is None or len(T) != len(texts):
        T = embedder.encode_text(texts)
        embedder._shot_T = T
    return names, T, np.array(owner)

def shot_sizes(M: np.ndarray, embedder) -> list[tuple[str, float]]:
    """Per row of M: (close | medium | wide | SHOT_SIZE_UNKNOWN, the winner's softmax share). Straight
    through _score, so the four confidence gates that decide a category decide a shot size too and the
    two can never drift apart. _score answers FALLBACK both for a "__other__" win and for a real winner
    that failed a gate; both mean the same thing here and both become unknown."""
    if len(M) == 0:
        return []
    names, T, owner = _shot_matrix(embedder)
    return [(SHOT_SIZE_UNKNOWN if cat == FALLBACK else cat, round(score, 4))
            for cat, score, *_ in _score(M, T, owner, names)]

def classify_shot_size(root: Path) -> dict[str, int]:
    """Score every ok, embedded row for shot size and store it. No disk beyond the index: the embeddings
    are already there, which is the whole point of doing this zero-shot rather than from the frames. A
    row with no embedding is left NULL (unscored) rather than guessed at. Returns the counts it wrote."""
    from collections import Counter
    from .embed import get_embedder
    root = Path(root); conn = db.connect(root)
    rows = conn.execute("SELECT id, embed, kind, face_h FROM photos WHERE status='ok' AND embed IS NOT NULL "
                        "AND length(embed)=? ORDER BY id", (db.EMBED_BYTES,)).fetchall()
    if not rows:
        return {}
    M = np.stack([np.frombuffer(r["embed"], np.float16).astype(np.float32) for r in rows])
    # a clip's face size gets the last word over medium and wide (refine_shot_size); stills keep the embedding's
    out = [(refine_shot_size(size, r["face_h"]) if r["kind"] == "video" else size, score)
           for r, (size, score) in zip(rows, shot_sizes(M, get_embedder()))]
    conn.executemany("UPDATE photos SET shot_size=?, shot_size_score=? WHERE id=?",
                     [(size, score, int(r["id"])) for r, (size, score) in zip(rows, out)])
    conn.commit()
    return dict(Counter(size for size, _ in out))

def classify_and_store(root: Path, people_by_faces: bool = True) -> dict[str, int]:
    """Runs the stacked pass and persists category + category_score (and the best real guess with its
    probability) onto photos (and videos), and category + score onto their segments, then the separate
    zero-shot drone pass (flag_aerial) and the zero-shot shot size pass (classify_shot_size). Returns
    counts per category over photos and videos, the same rows the Categories tab lists (segments are not
    counted; aerial and shot size are their own axes, not categories)."""
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
    classify_shot_size(root)
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
