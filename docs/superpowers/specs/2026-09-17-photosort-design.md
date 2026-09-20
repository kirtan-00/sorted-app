# photosort (working name) : build plan, 2026-09-17

Local-first photo sorter for designers and photographers. Indexes a loose folder IN PLACE
(no import, no library), scores every photo, then answers "show me the balcony shots that are
actually sharp" and "give me every photo of this person, split into groups vs solo".
Zero LLM in the indexing path.

Target box: Kirtan's M1 MacBook Pro 13", 8 GB RAM, 19 GB free internal. Photos live on an
external drive. Everything below is sized for that.

## 1. Options matrix

| | A. Build photosort (recommended) | B. Don't build: Excire + digiKam | C. Apple Photos wrapper (osxphotos) |
|---|---|---|---|
| Independence | Full. One Python CLI + tiny local web grid, MIT models, runs offline forever | Excire is closed, $129 one-time; digiKam is GPL desktop app | Locked to Apple Photos and its import model |
| Efficiency | Index 10k photos in ~10-30 min (modelled, see §4); re-runs seconds | Excire index ~1 hr+ per review; digiKam faces separate pass | Photos indexes in background over hours/days; no blur filter |
| Economy | Rs 0 running cost; optional Claude Haiku for brief expansion, ~Rs 0.02/search | ~Rs 11k one-time | Rs 0 |
| Dev cost | ~5-7 working days to v1 (§6) | 0 | ~1 day, fragile |
| His cost | His attention on the benchmark + one review pass per phase | Learn two apps, both want their own catalog, none write folders back | 100 GB import needs disk he doesn't have |
| Kills the wedge? | Only tool that writes `sharp/ soft/ person_NN/ groups/ solo/` back next to the shoot | No in-place indexing, no group/solo, no candid/posed | No export of People as folders, no blur score |

Killed on purpose: a Kwikpic clone (guest selfie search). Kwikpic + ten Indian clones own it, it is
cloud by definition, and the face model is the commodity part. The local-first move that helps a
wedding shooter is grouping by face BEFORE upload so the per-photo Kwikpic bill drops.

## 2. Model stack (all fit in ~2 GB peak on 8 GB)

| Job | Model | Why | Licence |
|---|---|---|---|
| Decode | libjpeg-turbo DCT-scaled to ~1024 px (PIL `draft`), rawpy embedded preview for RAW | Decode ONCE, derive everything from that buffer | BSD |
| Semantic search | MobileCLIP-S1 via CoreML (ANE) | 21M params, ~150-300 img/s batch on M1, text encoder handles "balcony" natively | CHECK before selling; fallback OpenCLIP ViT-B/32 at ~3x RAM |
| Faces | YuNet detect + SFace embed (OpenCV Zoo, ONNX) | MIT; InsightFace weights are non-commercial, so no | MIT |
| Face clustering | DBSCAN on cosine, threshold tuned on his data (Immich's 0.5 distance was tuned for ArcFace, SFace is 128-d and weaker on look-alikes) | | |
| Sharpness | Laplacian variance ON THE SUBJECT: eye crop when a face exists, else 90th-percentile over tiles | Whole-image Laplacian flags every shallow-DOF portrait as blurry. This is the trust-or-uninstall detail | |
| Burst / dupes | capture time within 3 s AND (pHash Hamming ≤ 10 OR embedding cosine ≥ 0.9) | never all-pairs | |
| Store | SQLite, in `~/Library/Application Support/photosort/<shoot-slug>/` on the Mac | brute-force 50k x 512 ≈ 3 ms; the source drive is READ-ONLY, nothing is ever written next to the photos | |

RAW gotcha: Sony bodies before the A7IV embed only a 1616x1080 preview. Eye-sharpness on those
needs a `half_size` demosaic (~300-400 ms, 2 workers max for RAM). Canon CR3 and Nikon NEF embed
full-size JPEGs, fast path.

RAW+JPEG pairs: dedupe by stem, index the JPEG, tag both.

## 3. What it outputs

- `photosort index <folder>` writes `index.db` + 1024 px thumbs under `~/Library/Application Support/photosort/`; the shoot folder is never written to, moved, or deleted from
- `photosort find "balcony" --sharp` prints matches; `--out <name>` COPIES them to `~/Desktop/photosort-out/<shoot>/<name>/` (symlink and csv modes optional)
- `photosort people` copies into `~/Desktop/photosort-out/<shoot>/people/person_NN/`, `groups/` (≥3 faces),
  `solo/` (1 face), and `_unassigned/`; name a person by dropping one reference JPEG into their folder
- `photosort candid` (last phase, EXPERIMENTAL): gaze-at-camera ratio + face count + pose spread,
  manual override, no prior art anywhere
- Local web grid at localhost:7777 to eyeball results and hand-fix. Not a photo editor.

LLM line: none in indexing. One optional user-triggered call: designer types a brief
("warm evening shots for a launch post"), Claude Haiku expands it into 5 CLIP queries. ~200 tokens.

## 4. Time to crunch 100 GB (MODELLED, not measured. Step 0 measures it.)

Per-photo cost single-stream on M1: preview decode ~30 ms + YuNet 10 + SFace ~18/face +
MobileCLIP-S1 ~5 + hash/blur ~5 ≈ 100-130 ms. 4 P-core workers → ~40-50 ms effective.

| Scenario | Files | Compute (4 workers) | End-to-end incl. faces + clustering | If estimates are 3x optimistic |
|---|---|---|---|---|
| 100 GB of 24 MP JPEG (~10 MB each) | ~10,000 | ~6-8 min | **10-30 min** | ~1 hr |
| 100 GB RAW, modern body (CR3/NEF/A7IV+) | ~3,500 | ~3-4 min | **5-15 min** | ~40 min |
| 100 GB RAW, old Sony (small preview) | ~3,500 | +~10 min demosaic | **15-25 min** | ~1 hr |
| Re-run after adding 500 photos | 500 | seconds | ~1 min | |

I/O floor (missing from every vendor number): 100 GB must be READ once.
- USB-C NVMe SSD (~800 MB/s): ~2 min floor
- USB-3 SATA SSD (~400 MB/s): ~4 min floor
- Spinning HDD (~120 MB/s): ~14 min floor and IT becomes the bottleneck, compute idles
- Google Drive / network: hours. Don't. Sync to a drive first.

Reference: Aftershoot takes ~90 min per 4,000 images with faces on, on an M3.
Face clustering at the end: seconds for ~30k faces. Search queries: ~3 ms.

## 5. Correctness traps (named so a green build can't hide them)

1. Sharpness on the subject, not the frame (above). Test set must include shallow-DOF portraits.
2. CoreML EP in onnxruntime silently falls back to CPU on dynamic shapes. Verify with ORT logging;
   don't assume the speedup.
3. Never full-demosaic when a preview exists; cap RAW workers at 2 (each peaks ~400-500 MB).
4. Face cluster threshold must be tuned on HIS wedding set, not copied from Immich.
5. Candid/posed is a heuristic. Ship it labelled, with override, last.

## 6. Build phases (each is its own approval gate)

0. Benchmark: 200 real files off the SSD, measure decode + embed + faces per image. 10 min. Converts §4 from guess to number.
1. Index core: walk, hash+mtime, decode-once, EXIF, pHash, subject-sharpness, MobileCLIP → SQLite. ~1.5 days
2. Search + export: `find`, filters, symlink/copy/csv/xmp writers. ~1 day
3. Faces: YuNet + SFace, DBSCAN, person folders, groups/solo, reference-photo naming. ~1.5 days
4. Review grid: local web page, thumbnails, mark/unmark, rerun. ~1 day
5. Candid/posed experiment + designer brief expansion (the one LLM call). ~1 day

Total ~5-7 working days to a v1 Kirtan would actually run on a shoot.

## Sources
Throughput and model numbers: apple/ml-mobileclip, Immich CLIP guide (#11862), mlx-uniface M2 Pro
benchmarks, pyiqa benchmark, sqlite-vec M1 mini benchmarks, libjpeg-turbo #651, LibRaw forum.
Market: Immich ML README, Ente ML, digiKam 8.6, rclip, FilterPixel/Narrative/Aftershoot pricing pages,
Kwikpic helpdesk + G2, Apple Photos macOS 26 guide.


## Amendment 2026-09-17 (Kirtan)
The SSD/HDD holds irreplaceable data. Rule: the source is read-only. Index and thumbnails live on the Mac under Application Support; filtered results are COPIED to a folder on the Desktop. Nothing under the shoot root is ever created, moved, renamed, or deleted.
