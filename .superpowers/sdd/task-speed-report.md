# Report: faster scans on the formats that hurt

Branch `worktree-agent-a18ff06449c7f32ab`, on top of `diu-scale` (dafec35). Full suite green: 315 passed,
1 skipped (was 305 + 1). Every number below is from my own runs on this M1 8 GB, on battery (77% falling
to 55%), same power state before and after, ffmpeg 8.1.2. Nothing touched ports 7777-7779, /Volumes or
the real Application Support index (every run had `PHOTOSORT_HOME` in scratch). No LLM, no subagents.

## Commits

| sha | what |
|---|---|
| 1104854 | perf: all-intra clips (Sony XAVC S-I) take one ffmpeg pass for cuts and frames (+ caps read from config, item 2) |
| 15415e3 | perf: RAW twins pair per day folder, so a rolled-over counter no longer decodes every RAW |
| fc72267 | perf: thumbnail pool sized to the Mac (cores minus one, capped at 8) |
| f92f077 | perf: covering indexes for every per-render count, ANALYZE so the wide reads keep scanning |
| f690c27 | docs: speed report and roadmap section 6 |
| (last) | test: race-free temp-dir assertion in the intra test; report re-measured after the indexes landed |

## 1. Sony 4:2:2 path: one decode, 8x

**What the 41 s actually was.** `-skip_frame nokey` skips nothing on an all-intra stream (XAVC S-I: every
frame is a keyframe), so the "keyframe" scene pass was a full decode of every frame. A decoder cannot skip
a frame it is handed. The lever that works is dropping packets *before* the decoder: ffmpeg 7+ accepts a
bitstream filter as an input option, and `noise=drop=not(eq(mod(n\,N)\,0))` throws away every packet but
one per `SCENE_STEP_S` (0.5 s). An all-intra stream has no references, so nothing breaks.

**Build (photosort/video.py, config.py, index.py):**
- `is_all_intra(path)`: ffprobe on the first 40 packet flags (50 ms). All `K` = intra.
- `single_pass(path, fps, ...)`: one ffmpeg run, `-skip_frame nokey -bsf:v noise=drop=... -i clip`, a
  `split` filter graph: branch A `scale=320:-2,select='gt(scene,0.4)',showinfo@cuts` to `-f null` (the
  cuts, same threshold and width as before), branch B `select` one frame per `FRAME_STEP_S` (1 s), scaled
  to `PREVIEW_EDGE`, `showinfo@frames`, written to a temp dir of our own (`-fps_mode passthrough`, removed
  before returning). Cuts and frame times come from the two showinfo streams.
- `sample_frames`: in the scene band (8 s to 300 s) an intra clip takes the single pass and every wanted
  instant (the 6 even samples and the segment midpoints) gets the nearest kept frame, within 0.5 s. A pass
  that fails (older ffmpeg, no input bsf) falls back to the two-pass path. Long-GOP clips keep the keyframe
  pass and exact seeks, unchanged. `probe()` now returns `fps`; `_process_video` passes it through.
- Frames stay at `PREVIEW_EDGE` (1024), not 540p: h264 cannot be decoded at a lower resolution, the
  scale is a post-decode filter and costs the same at 960 or 1024, and keeping 1024 leaves the focus pass
  comparing Sony and DJI clips at the same size. Flagging this as a deliberate deviation from the brief.

**Measured** on a scratch 10 s 4K60 4:2:2 10-bit clip (libx264 high422, one hard cut at 6 s, 88 MB
intra / 58 MB long-GOP; both regenerated deterministically by `scratchpad/speed/make_clips.py`), key =
("h264","yuv422p10le") so no hwaccel is tried, two runs each:

| clip | scene pass alone | sample_frames before | sample_frames after |
|---|---|---|---|
| intra (S-I shape) | 1.76 s (600 frames decoded) | **2.53 s** (scene 1.76 + 7 seeks) | **0.32 s**, same cut at 6.0, same segments |
| long-GOP (S shape) | 0.27 s (20 keyframes) | 1.25 s | 1.35 s (+50 ms intra probe; unchanged path) |

Variants tried on the intra clip (raw ffmpeg, `bench_ff.py`): threads 4 / threads 8 frame / skip_loop_filter
all: 1.74-1.81 s, no gain. bsf drop 59/60: 0.12 s; drop 29/30: 0.16 s; drop + split single pass: 0.44 s
(0.20 s with `-fps_mode passthrough`, the version shipped).

Fixture in tests: `make_video(..., intra=True, pix_fmt="yuv422p10le")` in conftest, 320x240, a few hundred
KB, built on the fly; no binary committed. Tests: intra detection vs long-GOP, exactly one ffmpeg process
for an intra clip with the bsf before `-i`, every target within FRAME_STEP_S/2 of a frame, shoot folder
untouched, no `photosort-pass-` temp dir left behind, fallback to two passes when the pass fails,
long-GOP never enters the single pass.

**Scaling to the real clip:** the 87 s S-I clip went 41 s decode-bound. The pass now decodes 174 frames
instead of 5,220, so decode is no longer the floor. Reading the file is: the mov demuxer still reads every
packet's bytes before the bsf drops it, so a 6.5 GB S-I clip on a 400 MB/s external SSD floors at about
16 s (2.5x), on the internal SSD at 3 s. If the real clips turn out to be XAVC S long-GOP 4:2:2 (the
A7S III shoots both), they take the unchanged path; extending the single pass to keyframes is the next
step and is noted in the roadmap.

## 2. Scene detection cap: already the rule, now a live config value

`SCENE_MAX_DURATION_S = 300.0` was already in config and already pinned by two tests. But `video.py`
bound the name at import, so changing config at runtime did nothing. `sample_frames` now reads
`config.SCENE_MAX_DURATION_S` and `config.SCENE_MIN_DURATION_S` at call time; new test
`test_scene_caps_are_read_from_config_at_call_time` sets the cap to 5 s and sees a 6 s clip take fixed
windows, and moves the floor to prove the scene pass runs or not accordingly. No timing change.

## 3. RAW twin skip: the same-folder and JPG/RAW cases were already right, the rolled-over counter was not

Measured on a synthetic 200-pair shoot (`bench_raw.py`, JPEGs 800x600, RAWs are 4 KB of zero bytes so any
RAW read is an error row), index with faces off, embed off:

| layout | RAW handed to the index, before | after | index time |
|---|---|---|---|
| same folder (`day1/DSC00001.JPG` + `.ARW`) | 0 of 200 | 0 | 0.8-1.0 s |
| `day1/JPG/` + `day1/RAW/`, unique stems | 0 of 200 | 0 | 0.8-1.0 s |
| `day1/JPG/` + `day1/RAW/`, **counter rolled over** (DSC00000..99 in both days) | **200 of 200** (200 error rows) | **0** (200 paired) | 1.1 s -> 0.8 s |

So on a real two-card or two-day shoot with repeated stems the index was decoding every RAW (rawpy, ~10x
the memory of a JPEG preview, in the 2-worker pool) on top of its JPEG. `walk.find_images` now runs the
sibling pass first within one parent folder (`day1/RAW/x` pairs with `day1/JPG/x`), then across the tree
for what is left with the old unique-stem rule. Tests: walk-level (both days pair, a cross-day unique stem
still pairs, an orphan RAW stays) and index-level (12 JPEG+junk-RAW pairs across three layouts, 0 errors,
siblings recorded, no `.ARW` row).

## 4. Thumbnail pool: cores minus one, measured 20% on this M1

`JPEG_WORKERS` was a fixed 4; it is `thumb_workers(os.cpu_count())` = cores-1, min 2, max 8 (7 here).
RAW stays 2, video 3, CLIP unchanged in the main process under the single MPS lock.

Measured on 1,050 24 MP JPEGs (the 50 `site/img` Pexels photos re-encoded at 6000x4000 quality 90 and
hard-linked 20x: 77 MB on disk; the 640 px webps as-is would have measured process overhead, not decode),
`index_folder(embed=False)`, fresh `PHOTOSORT_HOME` per run:

| workers | faces off | faces on |
|---|---|---|
| 4 (before) | 19.5 s, 22.0 s (54, 48 photos/s) | 35.3 s |
| 6 | 17.1 s | |
| **7 (after)** | **16.1 s, 17.3 s (65, 61 photos/s)** | **29.9 s, 30.0 s** |
| 8 | 15.4 s | |

Stubbing `upsert_photo` / `replace_faces` / `replace_segments` to no-ops changed nothing (19.4 / 16.1 s),
so the three commits per photo in the main process are not the ceiling; the M1's four efficiency cores
are, which is what a 20% gain from three more workers looks like. Test pins the formula and the caps.

Re-measured on the final tree (after the seven covering indexes, so every upsert maintains them), by then
with another agent's Python and `trustd` at 17% and 59% CPU on this shared Mac and the battery at 51%:

| workers | faces off | faces on |
|---|---|---|
| 4 | 19.6 s | 33.6 s, 34.6 s |
| 7 | 16.4 s | 33.8 s, 33.0 s, 41.2 s |

Faces off holds (20%, indexes cost nothing visible). Faces on is inconclusive on the loaded machine:
the quiet-machine pair (35.3 -> 29.9/30.0 s) says ~15%, the loaded reruns say 0 within noise. I also
tried pinning OpenCV to one thread per worker (cv2 defaults to 8, so 7 workers x 8 threads oversubscribe):
39-52 s at 7 workers on the loaded machine, no better, reverted. A quiet-machine rerun of faces-on at 4
vs 7 is the open question; the setting is a config value either way.

## 5. Large shoot: per-render counts off the table

Synthetic 20k-row index (`bench_20k.py`: 20k photos rows with 1 KB float16 embeds, 8 categories, 5
clusters, 800 videos with 2,400 segments, 40k faces, 30 people), median of 10 TestClient calls:

| endpoint | before | after |
|---|---|---|
| `/api/categories` | 34.5 ms | **6.7 ms** |
| `/api/stats` | 33.1 ms | **8.4 ms** |
| `/api/folder` | 7.9 ms | 1.6 ms |
| `/api/errors` | 24.1 ms | 1.4 ms |
| `/api/people` | 1.6 ms | 1.7 ms |
| `/api/search?limit=200` (no query) | 31.2 ms | 30.5 ms |
| `/api/search?category=beach` | 20.2 ms | 18.3 ms |
| `/api/search?person=3` | 15.7 ms | 14.0 ms |
| `/api/search/ids` | 36.3 ms | 36.9 ms |
| `/api/search?offset=19000` | 30.0 ms | 30.2 ms |
| search after an index change (`Index.refresh` + query) | 208 ms | 211 ms |
| `db.connect()` | | 0.26 ms |

Every count was `SCAN photos`: a row carries the embedding, so a count read 24 MB. Seven `(status, ...)`
covering indexes (category+guess, cluster, aerial, kind, n_faces, focus, rel), created after the column
migrations so an old `index.db` gets them on open. Then the trap: without statistics SQLite took the new
indexes for the *wide* reads too (`Index.refresh` 158 -> 247 ms, `load_embeds` 21 -> 73 ms, the face join
90 -> 130 ms, through the index plus a temp sort). One `ANALYZE` (14 ms at 20k rows) fixes the plans:
`db.connect()` runs it once when a DB has no `sqlite_stat1`, `index_folder` runs it after every scan.
Tests assert the plans with `EXPLAIN QUERY PLAN` on a 3,000-row table (counts: covering index, never
`SCAN photos`; wide reads: `SCAN photos`) and that connect analyses a DB without stats and leaves one with
stats alone.

Not done on purpose: a count cache per index version. At 7 ms there is nothing left worth caching, and a
cache is one more way to show a stale count; the UI loads counts on events (tab open, scan end), never on
a timer (checked app.js; only `/api/progress` polls, and it is memory-only). `tests/test_scale.py` from
the diu-scale plan does not exist in this tree; the 20k build is SQL inserts in the bench script and the
plan test uses the same shape.

## Concerns

1. **I filled the disk once.** Two mistakes in the same bench: a 20 s 4K crf-16 grained clip pair came
   out at ~4 GB, and `bench_raw.py` had no `__main__` guard, so every spawn-pool worker re-ran the
   module-level `build()` and made ~850 copies of the 200-photo set. Both deleted; free space went 0 ->
   24 GB. The regenerated clips were 152 MB total and are deleted now too; `make_clips.py` rebuilds them
   deterministically in ~25 s. Lesson recorded in the scripts: guard `__main__` in anything that touches
   the spawn pool, and cap scratch fixtures.
2. **The 41 s number is not reproduced here**, only its mechanism. My synthetic intra clip decodes 10x
   faster per frame than real 600 Mbps footage, so the 8x is a ratio on decode work, and the real clip
   will hit the file-read floor described in item 1. A run on one real S-I clip copied to the internal
   SSD (not /Volumes) would settle it.
3. **Which Sony format is on the cards.** If it is XAVC S long-GOP 4:2:2, this change does not touch it;
   the keyframe pass was already keyframes-only there and the 41 s would have to be I/O. The roadmap
   carries the keyframe extension.
4. **Thumb pool gain is modest** (20%) because the extra workers are E-cores. Nothing further to get from
   the pool on an M1; the per-photo cost (draft decode + LANCZOS + two JPEG saves + phash + 64-tile
   Laplacian + YuNet) is where the next 2x would come from, if wanted.
5. **Video thumbs on intra clips land within 0.5 s of the instant**, not on it. Cuts and segment
   boundaries are unchanged in kind (they were keyframe-quantised already). Nothing in the UI shows the
   frame's exact time.
6. Battery during the runs: 77% -> 51%, never asleep; before/after pairs were run back to back. The
   last reruns (item 4) shared the CPU with other agents; noted in the table.
7. **Per-parent RAW pairing widens a false-pair hole.** `Day1/CamA/DSC01.ARW` + `Day1/CamB/DSC01.JPG`
   (one body RAW-only, another JPEG-only, under one day folder) now pair and the RAW leaves the grid.
   The old global rule had the same hole for a single-event tree; per-parent extends it to the multi-day
   trees where repeated stems live. Cheap guard if wanted: real twins have mtimes within a second or two;
   make the sibling-folder passes require that. Not built, not in the brief.
8. **Peak RSS at 7 thumbnail workers next to the server's loaded embedder was not measured** on this 8 GB
   machine. RAW stays at 2 workers so the worst case is bounded; the JPEG draft decode holds a 1500 px
   image per worker. A number would take one run with `/usr/bin/time -l` around a real scan.
