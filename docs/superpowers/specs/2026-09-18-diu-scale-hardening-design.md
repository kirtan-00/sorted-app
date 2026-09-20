# Diu scale hardening (Plan A)

Kirtan, 2026-09-18: "our big photo batch has so many photos = dont break it make sure it works."

The real Diu shoot is thousands of photos on `/Volumes/One Touch/` (exact count unknown, disk not
mounted while this was written). Every number in the tool so far was measured on 46-60 files. This
pass makes the existing pipeline survive that batch. New features (bursts, discovered categories,
blurry/out-of-focus tiles, manual override, final export) are Plan B and come after this ships and
the scale gate passes.

## Design target

20,000 photos, 24 MP camera JPEGs (+ RAW siblings), nested day/location folders, videos mixed in,
M1 8 GB Mac, disk that may be unplugged between sessions.

## Hazards found in the code, ranked by what costs a full re-run

1. **Unmounted root wipes the index.** `find_images` on an absent path returns `[]`; `mark_missing`
   flips every row to `missing`; `known_files` excludes missing rows; the next mount re-processes
   everything. One click on "Index this folder" with the disk out.
2. **Export runs inside the HTTP handler.** Copying a 3,000-photo category (tens of GB into 32 GB
   free) blocks the request, has no progress, no disk-space check, and a mid-copy failure reports
   nothing.
3. **Shoot needs the disk mounted to open.** Everything needed to browse, search, curate and select
   is already on the Mac, but `/api/folder` refuses a path that is not a directory.
4. **Per-file failures are invisible** and a long run has no rate or ETA.
5. **No sleep guard.** Idle sleep kills an hour-long index. Lid-close on battery cannot be prevented
   by software; the UI must say "plug in, lid open".
6. **Videos are silently skipped**, not even counted.
7. **Search has no pagination**; a filtered category is one 200-row page with no way to reach the rest
   or select all matching without the client fetching every row.
8. **People tab at scale** shows hundreds of two-face stranger clusters.

## What this pass builds

| # | Change | Where |
|---|--------|-------|
| 1 | Synthetic shoot generator (nested days, EXIF dates, bursts, corrupt files, video stubs) | `scripts/make_scale_set.py` |
| 2 | Refuse to index when the root is empty/unmounted and the DB already has photos; a missing row that returns with identical size+mtime flips back to `ok` without re-processing | `photosort/index.py`, `photosort/db.py` |
| 3 | `caffeinate -i -w <pid>` for the life of an index run; README + Index tab say plug in, lid open | `photosort/index.py`, launcher, UI |
| 4 | Progress carries `started_at`; UI shows rate + ETA; stats carries `errors`; `/api/errors` lists failed files | `index.py`, `server.py`, `app.js` |
| 5 | `/api/search` gains `offset` + `total`; `/api/search/ids` returns matching ids only; grid gets "Show more" and "Select all N matching" | `search.py`, `server.py`, `app.js` |
| 6 | Export is a background job with progress, a free-space preflight, and partial-failure reporting; link-mode caveat in the UI | `export.py`, `server.py`, `app.js` |
| 7 | `meta.root` stored at index time; `/api/shoots` lists every index on the Mac with `mounted`; `/api/folder` opens an unmounted shoot from its index; banner disables Index + copy export offline | `db.py`, `server.py`, `app.js`, `index.html` |
| 8 | Video files tallied at index time into `meta.videos`; stats shows "N videos (not indexed)" | `walk.py`, `index.py`, `server.py`, `app.js` |
| 9 | People tab hides clusters under `GROUP_MIN_PHOTOS` (default 3) behind a "show small groups" toggle | `people.py`, `server.py`, `app.js` |
| 10 | Scale gate: `slow`-marked pytest over ~2,000 generated photos + 20,000 synthetic face vectors; manual 3k x 24 MP run with peak RSS recorded in `docs/SCALE.md` | `tests/test_scale.py`, `docs/SCALE.md` |

## Hard rules carried over

- Source folder is read-only. Generator writes to scratch only. Every scale test asserts the source
  listing is unchanged afterwards.
- Never start `serve` from the assistant's shell while Kirtan is in the app.
- No em dashes, no `--` in anything Kirtan reads.
- Match the existing one-file-per-concern layout and test style (`make_image`, `PHOTOSORT_HOME`
  isolation). Do not touch `apply_on_disk` or the classify thresholds.
- The 3k x 24 MP manual run is on mains power only.

## Step zero when the disk arrives

`diu Photos` has 0 rows indexed today. Index it and run `bench --n 200` before anything in Plan B.
