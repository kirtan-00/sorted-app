---
format: 1080x1920
fps: 30
duration: 32s
message: "Plug in the disk. Search the whole shoot."
arc: Pile → Search → People → Video → Proof → Deliver → Private → CTA
audience: editors, wedding photographers, agencies and filmmakers with a pile of shoot data
mode: autonomous
music: none
---

## Video direction

The landing page in motion, 9:16, 30 fps, no voiceover, no music. Paper ground with the five
drifting pastel blobs under every light frame; pastel blocks (butter for the numbers, sky for
delivery) with the page's 28px radius; the dark app only ever as a real capture in a rounded
window with the page's soft shadow. Archivo semi-expanded 700 for the big lines, revealed line by
line from a mask like the page's `data-reveal="lines"`; Source Sans 3 for the small lines. Calm
moves (0.5 to 0.8s, power3.out), holds long enough to read, captures push in slowly (1.0 to 1.05)
instead of sitting still. Every word and the mark inside x[70..930] y[240..1560]. No em dashes,
no "--", no "AI".

Cuts: blur-crossfade between light and dark-heavy frames, plain crossfade between siblings.

## Frame 1 — The pile

- scene: Fourteen loose photos tumble onto the paper and settle as a scattered pile; the line lands
- duration: 3.5s
- poster: 2.5s
- transition_in: cut
- status: animated
- src: compositions/frames/01-pile.html
- blueprint: compose
- focal: the headline
- roles: tiles = the shoot as it arrives; blobs = atmosphere
- asset_candidates: assets/photos/boats1.webp — fishing boats; assets/photos/boats2.webp — boats; assets/photos/coast.webp — aerial coast; assets/photos/hari1.webp — sadhu portrait; assets/photos/hari2.webp — sadhu; assets/photos/hari3.webp — sadhu; assets/photos/sari.webp — woman in a sari; assets/photos/harbour.webp — harbour aerial; assets/photos/site.webp — aerial site; assets/photos/road.webp — aerial road; assets/photos/sea.webp — aerial sea; assets/photos/boatman.webp — boatman; assets/photos/face1.webp — face; assets/photos/golden.webp — golden hour
- sfx: none

Scene 1 (0.0–1.6s): paper ground with the blobs already drifting. The tiles (3:2 photos on a
4px white mount, rotated -9 to +9 degrees, 240px wide) drop in from above one after another
(waterfall-entry, stagger 0.07s, power3.out, a little overshoot on the rotation) and settle into a
scattered pile filling y 560 to 1480. Layout: pile centered, spanning the safe width.
Scene 2 (1.2–2.4s): the line "A shoot arrives as a pile." reveals from a line mask at the top
(y 300 to 460, two lines, Archivo 92px). Scene 3 (2.4–3.5s): a small Source Sans line fades in
under it: "955 items. 800 GB. All named DSC05455." Tiles keep a slow sine drift (finite yoyo).

## Frame 2 — Search the whole shoot

- scene: The real app: "boat" types into the search field, the grid answers; three more searches cut through
- duration: 6.5s
- poster: 3s
- transition_in: blur-crossfade
- status: animated
- src: compositions/frames/02-search.html
- blueprint: compose
- focal: the app window
- roles: app captures = the product working; headline = the promise
- asset_candidates: assets/captures/app-empty.jpg — the app with an empty search box; assets/captures/app-boat-t01.jpg — "b" typed; assets/captures/app-boat-t02.jpg — "bo"; assets/captures/app-boat-t03.jpg — "boa"; assets/captures/app-boat-t04.jpg — "boat"; assets/captures/app-boat.jpg — boat results; assets/captures/app-sadhu.jpg — sadhu results; assets/captures/app-aerial.jpg — aerial results; assets/captures/app-woman-in-a-sari.jpg — woman in a sari results
- sfx: none

Scene 1 (0.0–1.2s): the headline "Plug in the disk." then "Search the whole shoot." reveal line by
line at the top (y 280 to 470, Archivo 84px). Under it the app window (a real capture, 820px wide,
rounded 12px, the page's soft shadow, x 130 to 950, y 520 to 1556, showing the top of the capture,
bottom cropped) fades up from y+24 with the empty search box. Scene 2 (1.2–2.6s): discrete image
sequence (discrete-text-sequence, applied to the captures): "b" at 1.3s, "bo" at 1.5s, "boa" at
1.65s, "boat" at 1.85s; at 2.4s the results capture replaces it (the grid answers: boats first).
Scene 3 (2.6–6.5s): hold 1.4s, then hard cuts to "sadhu" at 4.0s, "aerial" at 4.9s, "woman in a
sari" at 5.7s, each a full capture swap. The window pushes in 1.0 to 1.04 across the whole frame.
A small Source Sans line under the headline at 2.6s: "Plain words. Photos and 4K clips."

## Frame 3 — Every face, one person

- scene: Same person? Same. Two face groups fold into one, "Hari, 72 photos"; below it the real app's face groups
- duration: 4.5s
- poster: 3s
- transition_in: crossfade
- status: animated
- src: compositions/frames/03-people.html
- blueprint: compose
- focal: the merge row
- roles: replica merge captures = the Same or Different moment; app people capture = the real thing
- asset_candidates: assets/captures/replica-merge-034.jpg — Same person? row, 71% alike, before; assets/captures/replica-merge-038.jpg — Same pressed; assets/captures/replica-merge-041.jpg — faces in flight; assets/captures/replica-merge-045.jpg — faces landing; assets/captures/replica-merge-050.jpg — merged, Hari 72 photos; assets/captures/app-people.jpg — the real app People tab with three face groups
- sfx: none

Scene 1 (0.0–1.0s): headline "Every face, one person." reveals at the top (y 280 to 400, Archivo
84px). Scene 2 (0.6–3.0s): a dark window (x 110 to 970, y 470 to 990) shows the "Same person?"
panel cropped from the replica capture (the two merge rows, the 71% alike, Same and Different),
scaled so the row reads. Discrete sequence: before until 1.6s, Same pressed at 1.6s, faces in
flight at 1.85s, landing at 2.05s, merged "Hari, 72 photos" at 2.3s. Scene 3 (2.6–4.5s): a second
dark window (y 1040 to 1556) fades up with the real app's People tab cropped to the FACE GROUPS
rows (person_01, person_02, person_03). Small line between the windows at 3.0s: "One photo of
someone finds every frame of them."

## Frame 4 — Photos and video

- scene: The replica with Drone shots on: four 4K clips selected, durations on the tiles, the inspector reads DJI Mavic 3, 3840 x 2160, 0:21
- duration: 3.0s
- poster: 1.8s
- transition_in: crossfade
- status: animated
- src: compositions/frames/04-video.html
- blueprint: compose
- focal: the clip tiles
- roles: replica-drone capture = video proof; app-videos = the real app filtered to clips
- asset_candidates: assets/captures/replica-drone.jpg — the replica grid with four drone clips selected and the inspector open; assets/captures/app-videos.jpg — the real app filtered to Videos
- sfx: none

Scene 1 (0.0–0.8s): headline "Photos and video." reveals at the top (y 280 to 400). Scene 2
(0.3–3.0s): a dark window (x 110 to 970, y 460 to 1100) shows the replica capture cropped to the
grid's top rows with the four selected clip tiles and their durations, pushing in slowly. Scene 3
(1.2–3.0s): a second dark window (y 1140 to 1556) fades up with the real app's Videos filter (the
clip tiles with 0:08 badges). Small line at 1.4s between them: "A clip is cut into scenes. Every
scene is searchable."

## Frame 5 — A five-year-old Mac is enough

- scene: Butter block. Three bars fill and their times count up: 10 min 42 s, 44 min 36 s, 1.0 s
- duration: 6.0s
- poster: 4s
- transition_in: blur-crossfade
- status: animated
- src: compositions/frames/05-numbers.html
- blueprint: compose
- focal: the three values
- roles: bars = proof; sub line = the machine
- asset_candidates: none — typographic frame
- sfx: none

Scene 1 (0.0–1.0s): a butter block (28px radius, x 40 to 1040, y 240 to 1580) settles onto the
paper; the headline "A five-year-old Mac is enough." reveals inside it (Archivo 80px), then the
small line "2020 MacBook Air, M1, 8 GB, the base model." Scene 2 (1.0–5.5s): three bar rows, one
every 1.3s (stat-bars-and-fills: label above, ink fill scaleX from the left on a translucent
track, value right of the track counting up with counting-dynamic-scale, tabular digits):
"3,677 photos, 147 GB" → "10 min 42 s" (fill 0.24); "955 items, 800 GB, photos and 4K clips" →
"44 min 36 s" (fill 1.0); "One 4K drone clip, 23 s" → "1.0 s" (fill 0.03). Scene 3 (5.0–6.0s):
a closing small line: "No GPU farm. No upload. No subscription for compute."

## Frame 6 — Send the client their frames tonight

- scene: Sky block. SSD on top, the export panel, Google Drive folder filling: 0 of 372, 364 of 372, 372 of 372
- duration: 4.5s
- poster: 3s
- transition_in: crossfade
- status: animated
- src: compositions/frames/06-deliver.html
- blueprint: compose
- focal: the Drive folder filling
- roles: flow captures = the page's own delivery flow, stacked
- asset_candidates: assets/captures/flow-1.jpg — ready, 0 of 372; assets/captures/flow-3.jpg — uploading, first rows; assets/captures/flow-5.jpg — 364 of 372; assets/captures/flow-8.jpg — 372 of 372, manifest written
- sfx: none

Scene 1 (0.0–1.0s): a sky block (x 40 to 1040, y 240 to 1580) settles; the headline "Send the
client their frames tonight." reveals (Archivo 76px, y 300 to 470). Scene 2 (0.6–4.5s): the
stacked flow capture (SSD, export panel, Drive folder; 720px wide, x 180 to 900, y 520 to 1390)
fades up and runs as a discrete sequence: ready until 1.6s, first rows at 1.6s, 364 of 372 at
2.5s, 372 of 372 at 3.4s. Scene 3 (3.0–4.5s): small line at the bottom of the block (y 1440):
"A folder on any disk, or straight into their Drive."

## Frame 7 — Nothing leaves the Mac

- scene: Paper and blobs, one line, the mark
- duration: 2.5s
- poster: 1.5s
- transition_in: crossfade
- status: animated
- src: compositions/frames/07-private.html
- blueprint: compose
- focal: the line
- roles: mark = the brand in the corner of the statement
- asset_candidates: assets/brand/mark.svg — the mark
- sfx: none

Scene 1 (0.0–0.9s): the mark (120px) fades in at y 620, centered. Scene 2 (0.3–1.5s): "Nothing
leaves the Mac." reveals under it, centered (Archivo 92px, two lines). Scene 3 (1.3–2.5s): small
line: "Offline. No upload. No account needed to sort."

## Frame 8 — End card

- scene: Mark over the wordmark, the beta badge, the URL, "Runs on a Mac. Be first to run it."
- duration: 3.5s
- poster: 2.5s
- transition_in: blur-crossfade
- status: animated
- src: compositions/frames/08-end.html
- blueprint: compose
- focal: the lockup
- roles: mark + wordmark = the brand; badge = the status; URL = the CTA
- asset_candidates: assets/brand/mark.svg — the mark; assets/brand/wordmark.svg — the wordmark
- sfx: none

Scene 1 (0.0–1.0s): the stacked lockup (mark 200px over the wordmark 420px wide) fades and scales
in (0.96 to 1) centered at y 700. Scene 2 (0.8–1.8s): the mint badge "public beta, testing right
now" with its dot pops in under the lockup (y 1000). Scene 3 (1.4–2.6s): the URL
"kirtan-00.github.io/sorted" (Source Sans 600, 44px) fades in at y 1120, then the line "Runs on a
Mac. Be first to run it." (Source Sans 400, 34px, ink-2) at y 1210. Hold to the end; a quiet
settle only.
