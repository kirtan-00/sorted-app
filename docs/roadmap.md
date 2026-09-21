# sorted, what to build next

21 September 2026. Ranked inside each group; the first three groups are what the beta needs,
the rest is the product after that. Marked [S] small (a day or less), [M] a few days, [L] a
week or more.

## 1. Trust: the scan must never come back half done
All five shipped 21 September (docs/scan-resume-report.md).
- [M] Resume a scan after the disk is unplugged or the Mac sleeps: keep a per-file done mark,
  on reopen say "2,080 of 4,315 scanned, continue?" and continue. This is exactly what bit
  Dhrumil (Part 1).
- [S] A scan file (.photosort-index.zip) that is incomplete says so on load, with a Continue
  scan button, instead of quietly showing fewer items.
- [S] Health line on the Scan tab: items on disk vs items scanned vs items with search ready,
  in plain words, with one button to fix the gap.
- [M] Faces and focus as background jobs that survive a restart (job table in the index).
- [S] Sleep guard while scanning (caffeinate) with a one-line "keeping the Mac awake" note.

## 2. Install and update, no Terminal
- [L] Signed, notarised sorted.app (DMG, drag to Applications, zero dialogs). Needs the Apple
  Developer enrolment. Python, models and ffmpeg inside the bundle.
- [M] In-app update: the app checks one static version file on the site (opt-in, sends
  nothing), shows "0.3 is out, what changed", one click to update.
- [S] The app knows its own version (git sha or a number) and shows it in How it works and in
  the feedback report.
- [S] Intel Macs: a clear refusal page instead of a failed install.

## 3. First run and learning the app
- [S] Two-minute tour on first open (five hotspots: search, people, categories, export, scan
  file), skippable, never shown again.
- [S] Empty states everywhere say what to do next (no people yet, no categories yet, nothing
  matches: try fewer words).
- [M] Example shoot: a 60-photo, 6-clip free set that downloads once so a new user can try
  search and people before plugging a disk in.
- [S] Keyboard cheat sheet (press ?).

## 4. Search and understanding
- [M] Faces on video: sample frames per scene, group with the same people, so "every clip of
  Dhaval" works. The biggest missing promise on the landing page.
- [M] Person search across shoots: pick a saved person, search every scan file on this Mac.
- [M] Better video scene breakdown: one thumbnail strip per clip with the scene cuts, click a
  scene to jump; export a segment list (EDL/CSV) an editor can import.
- [M] Text in frames (OCR on stills): signs, slates, name cards, so "find the shot with the
  Nagar Seth board" works. On-device, no LLM.
- [S] Search history and saved searches per shoot.
- [M] Duplicate and burst grouping: same frame on disk twice, and 12-shot bursts folded into
  one tile with "pick the sharpest".
- [S] Combined filters (person AND category AND drone) instead of one context at a time.
- [M] Audio: detect speech vs no speech per clip, "interview" from sound, silence trim points.

## 5. Deliver
- [S] Export presets: "client web set 2048 px", "editor selects, links only", "full res".
- [M] Dropbox, OneDrive, WeTransfer as destinations (the plugin promise on the landing page).
- [M] Contact sheet PDF and a shareable HTML gallery (static, offline, on a stick or in Drive).
- [S] Export history: what went where, when, re-run it.
- [M] Reorganise: dry-run diff view, per-folder undo, and a "rename files by date and person"
  option.

## 6. Speed and formats
- [M] Sony 10-bit 4:2:2 clips: software decode is 40x slower than DJI hardware decode. Proxy
  path: decode once at 540p in the background, index from that.
- [S] Scan the RAW's embedded preview only when there is no JPEG twin (saves half the time on
  RAW+JPEG shoots).
- [S] HEIC and ProRes coverage check on real cards (iPhone shoots, FX3 ProRes).
- [M] Large-shoot mode above 20k items: paged grids, lazy thumbs, index in chunks.

## 7. Working together
- [M] Hand-off: the scan file already travels; add "notes and picks" so an editor's selects
  come back to the photographer in the same file.
- [M] Named people that travel between shoots (a people book on the Mac, opt-in per shoot).
- [L] Two Macs, one disk: merge two scan files of the same shoot.

## 8. Business
- [S] Pricing page on the site: one-time licence for the app, no subscription for compute;
  decide the number (competitor anchors in docs/competitors.md).
- [M] Licence key in the app (offline check, one key per Mac, nothing phones home).
- [S] Feedback loop: the report zip, a reply template, a changelog page on the site.
- [S] Testimonials block on the landing page once three testers have used it for real work.
- [M] The in-browser "try it on 200 photos" demo on the landing page (photos only, WebGPU).

## What I would do first, in order
1. Resume-after-unplug and the incomplete-scan warning (group 1, first three items). Done.
2. Signed app (group 2) as soon as the developer account exists.
3. Faces on video (group 4).
4. Pricing page and licence (group 8), because the beta will ask.
