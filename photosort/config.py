import os
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"

# The encoder is a licence choice, not a quality one. MobileCLIP-S1's weights are apple-amlr: research
# only, excludes commercial exploitation, and this app is going to be sold, so it cannot ship as the
# default. ViT-B-16-SigLIP/webli is Apache-2.0 (Google). What it costs: open_clip's published zero-shot
# ImageNet is 0.7604 for SigLIP B/16 against about 0.726 for MobileCLIP-S1 (SigLIP is actually a hair
# ahead); on this M1 over 96 images SigLIP encodes 20.7 img/s against MobileCLIP's 32.4, but the whole
# pipeline runs at about 12 files/s because decode dominates, so a scan costs about 17% more wall time;
# and the weights a buyer downloads on first run go from 325 MB to 812 MB. Each entry is
# (pretrained tag, embed_dim, the zero-shot text template CLIP was trained with).
ENCODERS: dict[str, tuple[str, int, str]] = {
    "MobileCLIP-S1": ("datacompdr", 512, "a photo of {}"),
    "ViT-B-16-SigLIP": ("webli", 768, "a photo of {}."),
}
# PHOTOSORT_CLIP_MODEL picks the encoder; unset is ViT-B-16-SigLIP, the one the product ships with,
# because it is the only one of the two that may be sold. PHOTOSORT_CLIP_MODEL=MobileCLIP-S1 reverses
# this without a code change, and MobileCLIP stays supported and tested. A name outside ENCODERS raises
# here rather than falling through: a typo that silently indexes a shoot with the wrong encoder is
# exactly the failure db.EMBED_BYTES exists to catch, and it is cheaper to catch it at import than
# after 20,000 photos are embedded with it.
CLIP_MODEL = os.environ.get("PHOTOSORT_CLIP_MODEL") or "ViT-B-16-SigLIP"
if CLIP_MODEL not in ENCODERS:
    raise ValueError(f"unknown PHOTOSORT_CLIP_MODEL {CLIP_MODEL!r}; allowed: {sorted(ENCODERS)}")
CLIP_PRETRAINED, EMBED_DIM, CLIP_TEMPLATE = ENCODERS[CLIP_MODEL]
# SigLIP's text side is tokenised by a sentencepiece vocabulary open_clip fetches from the model's own
# Hugging Face repo through transformers.AutoTokenizer. That repo carries no config.json, and
# transformers 5.x asks AutoConfig for one before it will build a tokenizer, so with HF_HUB_OFFLINE=1
# the lookup cannot fall back to the cache and raises: the app would need the network to turn a search
# box into tokens, which breaks the one promise it makes. The three tokenizer files are 2.3 MB, so they
# ship in models/ beside the face networks (Apache-2.0, the same licence as the weights) and are loaded
# from that folder, where AutoTokenizer reads tokenizer_config.json for the class and never asks the hub.
TOKENIZER_DIRS = {"ViT-B-16-SigLIP": MODELS_DIR / "siglip-tokenizer"}
CLIP_TOKENIZER_DIR = TOKENIZER_DIRS.get(CLIP_MODEL)
# What gets stamped into an index (db.stamp_model) so a shoot remembers which encoder filled it.
MODEL_TAG = f"{CLIP_MODEL}/{CLIP_PRETRAINED}"

INDEX_DIRNAME = ".photosort"
DB_NAME = "index.db"
PREVIEW_EDGE = 1024
GRID_EDGE = 320
THUMB_QUALITY = 85

STD_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf", ".orf", ".rw2"}
IMAGE_EXTS = STD_EXTS | RAW_EXTS
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mts", ".avi"}
# Folder names the walk never enters, wherever they sit: photosort-out is our own export folder when it
# sits inside a shoot (dot-dirs are skipped too, in walk.py).
SKIP_DIRS = {"photosort-out"}
# Folders that never hold a shoot and are full of files with photo and video extensions: a package tree, a
# Python environment, a build cache. Skipped anywhere, not only on a whole-Mac scan. On one real home folder
# node_modules alone accounted for 423 of 739 unreadable files, every one of them a TypeScript .d.mts.
DEV_DIRS = {"node_modules", "site-packages", "__pycache__", "Caches", "DerivedData"}
# Folder names pruned only directly under a directory named M4ROOT (any case), the Sony card layout
# (PRIVATE/M4ROOT): one poster JPEG per clip under THMBNL (160 px: 12 of the 16 "other" photos on the
# first video shoot), proxy clips under SUB (C0001S03.MP4, duplicates of CLIP/; when a card carries proxies
# they could feed frame sampling later, for now they are duplicates and must not appear in the grid), and
# bookkeeping under TAKE and GENERAL. Scoped because SUB, TAKE and GENERAL are ordinary words a client's
# own folders may use.
SONY_CARD_DIRS = {"THMBNL", "SUB", "TAKE", "GENERAL"}
SONY_CARD_ROOT = "M4ROOT"

# Videos: no LLM, no faces. ffmpeg samples frames, CLIP embeds them, the clip's embedding is their mean.
VIDEO_FRAMES = 6            # evenly spaced between 5% and 95% of the duration
VIDEO_WORKERS = 3           # each worker runs its own ffmpeg, which is multi-threaded already
SCENE_THRESHOLD = 0.4       # ffmpeg scene score above which two keyframes are a cut
SCENE_MIN_DURATION_S = 8.0  # shorter clips skip the scene pass and are one segment
SCENE_MAX_DURATION_S = 300.0  # longer clips skip it too and get fixed windows: the keyframe pass is decode-bound
# An all-intra clip (Sony XAVC S-I: every frame is a keyframe, so -skip_frame nokey skips nothing and the scene
# pass was a full decode, 41 s per 87 s 4K clip on an M1) is read in ONE ffmpeg pass that drops every packet but
# one per SCENE_STEP_S before the decoder, scores those frames for cuts, and keeps one 1024 px frame per
# FRAME_STEP_S; the sampled frames and the segment midpoints are the nearest kept frames (within FRAME_STEP_S/2).
SCENE_STEP_S = 0.5          # decode cadence of the single pass (keyframes of a long-GOP clip sit 0.5 to 2 s apart)
FRAME_STEP_S = 1.0          # cadence of the frames the single pass keeps for thumbs, grid, frames/ and the embedder
INTRA_PROBE_PACKETS = 40    # packets read to call a clip all-intra: every one a keyframe (a GOP is 12 to 120)
LONG_SEGMENT_S = 120.0      # the window on a long clip (widened evenly when MAX_SEGMENTS would be exceeded)
FFMPEG_HWACCEL = "videotoolbox"   # macOS hardware decode; retried without it once if a codec is not accelerated
MAX_SEGMENTS = 24           # longest segments kept when a clip has more cuts than this
MIN_SEGMENT_S = 1.0         # a cut that would leave a shorter segment is merged into the previous one
# A clip at or above this container frame rate is slow motion. 100 and 120 are what the backlog asked for
# by name and they are unambiguous: nobody shoots a 120 fps timeline. 48, 50 and 60 are deliberately left
# out. They are a delivery frame rate as often as they are an over-crank, so half of the clips a 50 fps
# floor caught would be ordinary coverage, and a facet that returns ordinary coverage under the word
# "slow motion" teaches an editor to stop trusting it. Raise or lower it here if a shoot says otherwise.
# What this cannot see, and no container field can: Sony S&Q and the like conform in camera, writing 25
# fps into a file whose pictures are already slowed. Those clips read as normal speed and that is a real
# gap, not a bug to hide. Only a look at the footage would find them.
SLOWMO_MIN_FPS = 100.0
# Sony's S&Q is now the one case the sidecar can see: <stem>M01.XML records captureFps beside formatFps, so a
# clip shot at 100 and conformed in camera to 25 says captureFps="100p". video.read_sidecar keeps that as
# photos.capture_fps and the slow motion facet takes the larger of the two rates. Phones and DJI write no
# such sidecar, so for them the gap above still stands.

# Clip length, four buckets over photos.duration, which every clip row has had since video support landed, so
# this needs no catch-up pass. Measured on the two client shoots on this Mac:
#   GT 1 TB (1,140 clips, event coverage):  548 under 10 s, 444 from 10 to 30 s, 120 to 2 min, 28 longer
#   NSG_26 Part 2 (325 clips, documentary): 106, 115, 46, 58
# The edges are the ones an editor already uses when asking for footage (a cutaway, a moment, a take, an
# interview) and no bucket is empty or swallows a shoot on either. (key, lower bound inclusive, upper bound
# exclusive, in seconds); the keys are what the API and the URL carry.
DURATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("lt10", 0.0, 10.0),
    ("10to30", 10.0, 30.0),
    ("30to120", 30.0, 120.0),
    ("gt120", 120.0, float("inf")),
)

# The thumbnail pass (JPEG/HEIC decode, thumbs, sharpness, faces) runs one process per core but one, capped:
# the main process stores rows and drives progress. RAW stays at 2 (a rawpy decode holds ~10x the memory of
# a JPEG preview); videos stay at 3 (each drives a multi-threaded ffmpeg). The CLIP pass is not here: it
# runs in the main process under the single MPS lock. Measured over 1,050 24 MP JPEGs on an M1 (4P + 4E
# cores, on battery): 4 -> 7 workers is 19.5 s -> 16.1 s with faces off and 35.3 s -> 29.9 s with faces on,
# about what four efficiency cores add; 8 workers gave 15.4 s and were not worth starving the main process.
JPEG_WORKERS_MAX = 8
def thumb_workers(ncpu: int | None) -> int:
    """cores minus one, at least 2, at most JPEG_WORKERS_MAX; an unknown core count reads as 4."""
    return max(2, min((ncpu or 4) - 1, JPEG_WORKERS_MAX))
JPEG_WORKERS = thumb_workers(os.cpu_count())
# Bursts: a run of photos in one folder with consecutive frame numbers, each shot within BURST_GAP_S of the last
# (10 fps bursts span several seconds, so the gap is per pair, not one second for the run) and, where both
# frames have embeddings, cosine BURST_SIM or closer. Duplicates need no threshold: the same qhash is the same file.
BURST_GAP_S = 1.0
BURST_SIM = 0.9
# Another camera, same moment (search.angles): how far either side of a match to look for the same moment
# covered by a different camera. Measured on a client shoot of 1,627 rows, 888 of them carrying a camera
# across twelve bodies: where the camera changes from one frame to the next, 33% of those handovers are
# within 30 s, 49% within a minute, 62% within two, 74% within five, and 75% within ten. The curve is flat
# past five minutes, so doubling the window buys one more frame. Tight sync barely exists on a shoot like
# this (2.4% of handovers are under a second), so "the same moment" here means the same setup, which is
# minutes, and taken_at comes off each body's own clock: a Sony, a DJI and an iPhone are never synced, and
# minutes of drift are normal. Five minutes absorbs that while staying far under the 88 minute median gap
# to the next camera on a day when several were shooting, so a window still separates a setup from a day.
ANGLE_WINDOW_S = 300.0

RAW_WORKERS = 2
EMBED_BATCH = 32

FACE_SCORE_MIN = 0.7
# Grouping is conservative on purpose: a tight eps gives PURE groups (one person split across angles
# and sunglasses is fixed by a "same person?" yes; a group with two people in it is not). Calibrated on
# the 3,677-photo Diu shoot (6,569 faces), contact sheets in .superpowers/sdd/task-face-merge-report.md:
# 0.5 made one 5,388-face blob, 0.38 a 956-face blob of five people, 0.35 a 369-face group of two men,
# 0.32 kept the six biggest groups pure and the two main people whole (726 and 553 faces).
FACE_CLUSTER_EPS = 0.32     # cosine distance between faces for DBSCAN
# Faces that never enter a group (they stay in the index and reference matching still finds them):
# shorter than this on the short side in preview pixels, or with an eye-strip Laplacian variance
# (faces.eye_sharp) under this floor. On the Diu shoot size does not separate blur from real faces
# (the 115-face blob of out-of-focus background faces has a median edge of 32 px while three of the six
# biggest groups run down to 13 px), focus does: eye_sharp < 40 drops 97 of the 115 blob faces, the rest
# fall into two groups of 5 and 4, and none of the six biggest groups loses a face. The edge floor only
# removes detections too small to carry identity (8 to 9 px heads in the background).
FACE_CLUSTER_MIN_EDGE = 10
FACE_CLUSTER_MIN_EYE_SHARP = 40.0
FACE_MIN_SAMPLES = 2
GROUP_MIN_FACES = 3
# Two groups whose centroids (mean of unit embeddings, re-normalised) sit at or above this cosine
# similarity are offered as "same person?"; a "different" answer hides the pair for good. On the Diu
# shoot the top 30 pairs (0.65 and up) were all one person, 0.50 to 0.55 was about a third right and
# below 0.50 mostly wrong, so this sits at the single-face threshold.
FACE_MERGE_SUGGEST_SIM = 0.55
# At or above this centroid similarity two groups merge on their own at every re-cluster, unless a
# "different" link exists between them. 1.0 disables it: nothing merges without a yes. Kept off because
# two of the six pairs above 0.70 on the Diu shoot were blur-against-blur and could not be verified.
FACE_MERGE_AUTO_SIM = 1.0
# OpenCV's published SFace cosine threshold is 0.363, but that is for verified crops. On a real
# event shoot with thousands of small faces, 0.5 and below is noise (a different bearded man at
# 0.5, 1,039 "matches" at 0.3); 0.55 keeps the same person across lighting and sunglasses.
FACE_MATCH_MIN_SIM = 0.55
FACE_REF_MIN_EDGE = 48      # a reference face smaller than this (preview px, 1024 decode) is not trusted

# Fixed categories: the bin for photos the confidence gates rejected, and the probability below which a
# match (a category score, a discovered cluster's rescaled cosine) is "less sure": listed after a divider,
# sorted by confidence, exported only on request.
CATEGORY_FALLBACK = "other"
SURE_MIN = 0.5
# A video at least this long whose category (or best real guess) is people or interview is an interview:
# a ten-minute take with a person talking is an interview whatever the framing; a beach walk is not.
INTERVIEW_MIN_DURATION_S = 600.0

SOFT_PERCENTILE = 15        # bottom 15% of sharpness in a shoot = "soft"
SHARP_TILE_GRID = 8
# The on-demand focus pass (focus.check_focus) labels rows ok / soft / bad. A row is bad only when it is in
# the bottom FOCUS_BAD_PERCENTILE of its kind in the shoot AND its score (tile p90 for a photo, the median
# of that over the sampled frames for a clip) is under FOCUS_BAD_ABS AND nothing in it proves focus: the
# sharpest tile (sharp_max, or the sharpest frame's) and the eye strip (sharp_eye) both under
# FOCUS_BAD_MAX_ABS. Calibrated on two real shoots (contact sheets in .superpowers/sdd/task-focus-report.md):
# a flat log-profile shoot of 630 stills sits at p5 = 66 and a normal one of 3,677 at p5 = 191, so the
# percentile alone would hide haze, backlit portraits and night frames; 18 and 60 keep exactly the frames
# nothing is sharp in (4 of 630, 2 of 3,677). The sharpest-tile rescue exists for a drone on a flat sky:
# tile p90 of 3 (one tile in 64 holds the subject) with a sharpest tile of 1,149, and for a night clip that
# racks focus onto its subject in one frame out of six.
FOCUS_BAD_PERCENTILE = 5
FOCUS_BAD_ABS = 18.0
FOCUS_BAD_MAX_ABS = 60.0
FOCUS_FALLBACK_FRAMES = 3   # frames decoded from the clip when an index lost its frames/ (an imported bundle)

def app_home() -> Path:
    return Path(os.environ.get("PHOTOSORT_HOME") or (Path.home() / "Library" / "Application Support" / "photosort"))

def export_root() -> Path:
    return Path(os.environ.get("PHOTOSORT_EXPORT_DIR") or (Path.home() / "Desktop" / "photosort-out"))

def settings_path() -> Path:
    return app_home() / "settings.json"

def shoot_slug(root: Path) -> str:
    r = Path(root).resolve()
    return f"{r.name or 'root'}-{hashlib.sha1(str(r).encode()).hexdigest()[:8]}"

# ===== shoot modes (v0.7, docs/shoot-modes.md) =====
# Exposure brackets: a run of stills in one folder off one body, consecutive frame numbers, each within
# BRACKET_GAP_S (plus the previous frame's own shutter time, so a 4 s night bracket still chains) of the last,
# every neighbour's exposure differing by at least BRACKET_MIN_STEP stops, and the same framing. Framing is a
# brightness-blind compare: the correlation of log-luminance gradients on a 32 px grid thumb, best over a +-2 px
# shift. Calibrated on the 50 landing-page photos pushed -2 and +2 stops and cropped up to 4% (handheld drift):
# same frame 0.62 worst / 0.82 median; two different photos 0.19 at the 95th percentile, 0.78 worst (two frames
# of the same scene a minute apart). SigLIP cosine on the same pairs overlapped (same frame 0.63 worst, different
# photos 0.91 at the 95th), which is why the embedding is not the framing test here.
BRACKET_GAP_S = 2.0
BRACKET_MIN_STEP = 0.3          # stops; a third of a stop is the smallest step any body brackets in
BRACKET_FRAMING_MIN = 0.6
BRACKET_MIN_FRAMES = 3          # two frames at different exposures are a photographer adjusting, unless the camera said bracket (ExposureMode 2)
# Auto exposure drifts: consecutive frames of one scene in auto ISO move by a third of a stop (the nine real ILCE-7RM5
# frames went 8000, 12800, 10000). So a run the camera did not flag as a bracket has to span BRACKET_MIN_SPAN stops
# from darkest to brightest, and a burst only breaks where neighbours are BURST_MAX_STEP stops apart: a 10 fps burst
# panning from shade into sun stays one burst. Third-stop AEB is caught by the camera's own ExposureMode 2.
BRACKET_MIN_SPAN = 1.0
BURST_MAX_STEP = 1.0
# Camera facets: (key, upper bound inclusive, label) in order; a value goes in the first bucket whose bound it is
# under. Focal length is the 35 mm equivalent when the camera wrote one.
CAMERA_BUCKETS: dict[str, tuple[tuple[str, float, str], ...]] = {
    "focal": (("lt20", 19.5, "under 20 mm"), ("20to35", 35.5, "20 to 35 mm"), ("36to70", 70.5, "36 to 70 mm"),
              ("71to135", 135.5, "71 to 135 mm"), ("gt135", float("inf"), "over 135 mm")),
    "iso": (("le400", 400, "ISO 400 and under"), ("to1600", 1600, "ISO 500 to 1600"), ("to6400", 6400, "ISO 2000 to 6400"),
            ("gt6400", float("inf"), "over ISO 6400")),
    "shutter": (("fast", 1 / 1000 * 1.05, "1/1000 and faster"), ("action", 1 / 250 * 1.05, "1/800 to 1/250"),
                ("normal", 1 / 60 * 1.05, "1/200 to 1/60"), ("slow", 0.99, "slower than 1/60"), ("long", float("inf"), "1 s and longer")),
    # not a range: search.bucket reads the EXIF Flash bit. Listed here for the keys and words.
    "flash": (("fired", 1, "flash fired"), ("none", 0, "no flash recorded")),
    "aperture": (("wide", 2.0, "f/2 and wider"), ("mid", 4.0, "f/2.2 to f/4"), ("small", 8.0, "f/4.5 to f/8"),
                 ("tiny", float("inf"), "f/9 and narrower")),
}
