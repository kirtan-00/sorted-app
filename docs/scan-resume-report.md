# A scan never comes back half done

21 September 2026. Roadmap group 1 (Trust), all five items, on branch diu-scale in the
worktree agent-a5f2ec0eed9a78a61, four commits on top of a9b0e02:

- a9b0e02 a scan lists every file first, so an interrupted scan knows what is left
- 505cf68 the server knows when a scan was cut short, keeps the Mac awake, and names the one fix
- bf1e05a a scan file says how far its scan got, and loading one replies with it
- 4ca51e7 ui: a scan that stopped says so everywhere it matters, with one Continue

Full suite: 322 passed, 1 skipped (ffmpeg-less case), 2 min 17 s. 17 new tests.

## What Dhrumil sees now

Unplug the disk at 2,080 of 4,315. The scan stops before it writes a single error row.
The welcome screen comes back and says "Connect Prod_02 or scan another folder. 2,080 of
4,315 scanned. The scan continues from where it stopped once the disk is back."

Plug it in, open the shoot. The Search tab has an amber bar under the search box: "The last
scan stopped while reading photos and clips: 2,080 of 4,315 scanned. Search covers only
what is scanned so far. [Continue scan]". The Scan tab has the same in a block above the
readout, and under it the health line: "4,315 on the disk · 2,080 scanned · 2,080 searchable
· 2,080 checked for faces" with the one button that closes the gap.

Press Continue. Only the 2,235 files that were never read are read. While it runs, the bar and
the Scan tab both show "Scanning 2,612 of 4,315, reading photos and clips" and a small
"keeping the Mac awake" note. Same thing if the Mac died or the app was quit mid-scan: the
next open marks the run as interrupted and everything above still applies.

Load a scan file of a half-scanned shoot and the status line says "loaded NSG_26_Part 1:
2,080 of 4,315 scanned. This scan file is incomplete; press Continue scan with the disk
connected." The bar is there with the button. No more quietly showing fewer items.

## How it works, by layer

### Index (photosort/index.py, db.py)
- Before the first decode, every file the walk found gets a row with status `pending`
  (`db.add_pending`). A row moves to `ok` when its features are stored, `error` when the
  read failed. `known_files` skips pending rows, so a second scan reads exactly those.
- `db.scan_counts(conn)` answers "how far did the scan get" from the index alone, no disk:
  items, scanned, embedded (searchable), faced, errors, pending, unembedded, photo/clip
  splits, and `complete` (nothing pending, everything scanned is searchable; faces are
  reported apart because they are optional).
- A `jobs` table: one row per scan or focus pass with state running / done / failed /
  interrupted and the last progress (written at most every 2 s). `interrupt_running_jobs`
  runs whenever a shoot is opened: nothing can be running yet, so a row still "running" was
  cut short.
- Mid-scan unplug guard: when a file read fails and the root is no longer a directory, the
  scan raises `SourceUnavailable` before writing that file down as an error. The rows already
  read stay ok, the rest stay pending, the job row says interrupted.
- `meta.scan_faces` remembers whether the scan was asked for faces, so Continue does not grow
  or lose a faces pass.

### Server (photosort/server.py, awake.py, focus.py)
- `/api/stats` and `/api/folder` carry a `scan` block: the counts plus `faces` and
  `interrupted` ({kind scan|focus, stage, done, total, started, error} or null). An
  interrupted scan whose work is done by now is history, not a warning.
- `GET /api/scan/health`: the scan block plus `on_disk` (a fresh walk; null while the disk is
  away or a scan is running, when the walk would only race it), `new_on_disk` (files the index
  has no row for), `faces_pending`, and `fix`: `continue` / `rescan` / `faces` / `focus` / null.
- `POST /api/index {resume: true}`: faces as the interrupted scan had it, `retry_errors`
  off, 409 with the disk's name while it is not connected.
- A mid-scan unplug ends as `{stage: "paused", error, done, total}` on `/api/progress`, not
  `error`, and the endpoint is not wedged.
- `awake.py`: one `caffeinate -i -w <server pid>` child held while any background job runs
  (scan, classify, focus, every export, Drive upload, reorganise, bundle export), released
  when the last one ends. `-w` means it dies with the server on its own. Every progress
  endpoint reports `awake`. A Mac without caffeinate is a quiet no-op. Tests point
  `AWAKE_CMD` at a python sleeper, so no test touches the real power state.
- The focus pass keeps its own job row (done / failed / interrupted); the health line's fix
  for a cut-short focus pass is "Finish the focus check" (POST /api/focus), never a scan.

### Scan file (photosort/bundle.py)
- bundle.json carries `scan` (counts plus faces). The format string is unchanged, the key is
  optional, an older bundle loads as before. The import reply carries the installed index's
  own scan block, so an old bundle gets the same answer.

### UI (photosort/ui)
- `#scanbar` under the context bar in the search head: idle and short = summary plus Continue;
  running = the same readout as the Scan tab plus "keeping the Mac awake"; complete = hidden.
- Welcome: `#welcome-scanleft` under the "Connect Prod_02" line while the disk is away.
- Scan tab: `#scan-resume` block above the readout (Continue), `#scan-awake` under it,
  `#scan-health` with `#scan-fix` (labels: Continue scan / Scan the new files / Detect faces
  now / Finish the focus check). The health line is fetched when the tab opens and again
  after a scan ends; it is blank while a scan runs. The subbar's old "Detect faces now"
  button is gone: the health line is the one place.
- `formatProgress` has a `paused` branch; the poller's end says "scan paused: the disk went
  away" instead of "scan finished".
- One new colour token, `--notice: #d9a441`, for the bar's dot and the block's left edge.

## What was checked
- Stubbed renders at 1280 x 860 of nine states (search short, search running, scan short,
  scan running, scan ok, new files, faces, focus cut short, welcome with the disk away):
  `scratchpad/resume_ui/*.png`, copies in ~/Desktop/sorted-scan-resume/.
- Live click-through against a scratch server on port 7796 with an 8-photo shoot read but not
  embedded: bar shows Continue, click, awake line appears, scan ends, bar and resume block
  disappear, health line says "8 on the disk · 8 scanned · 8 searchable · 8 not checked for
  faces" with Detect faces now. Zero console errors, zero 4xx.
- Unit and API tests cover: pending rows and counts per stage, the unplug at file 2 of 6 and
  the Continue that finishes it, pending clips counted apart and dropped when the file is
  gone, running rows marked interrupted on open, the stats scan block with and without a
  folder, resume reusing the scan's faces choice, 409 while the disk is away, the paused
  stage, the health fix walking continue → faces → none → rescan → focus, awake held for a
  scan and reported on all four progress endpoints, the focus job row, bundle.json scan
  counts, an old bundle without the key, the import reply.

## Not done, and what to watch
- The resume is per file, not per stage inside a file: a clip whose frames were half
  extracted is read again from the start. Fine for the shoot sizes we have.
- `on_disk` walks the whole shoot every time the Scan tab opens. Seconds on 20k items; a
  minute on a slow HDD. If that annoys, cache it by folder mtime.
- Pausing the Mac by closing the lid still sleeps it; caffeinate -i only stops idle sleep.
  The awake note says so.
- The welcome's "scan continues" line depends on `/api/folder` carrying `scan`; an older
  server without it simply shows the old line.
- The Mac was on battery (62 %) for this session. Nothing ran long enough to matter.

## Try it
```
cd "~/Desktop/claude/claude projects/sorted/repo/.claude/worktrees/agent-a5f2ec0eed9a78a61"
source ../../../.venv/bin/activate
python -m photosort.cli serve --port 7790 "/Volumes/Prod_02/NSG_26_Part 1"
```
Start a scan, pull the disk at a few hundred, watch the welcome, plug it back, press Continue.
