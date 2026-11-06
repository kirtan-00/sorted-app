# Live shoot

The camera shoots, the shot lands on the MacBook, sorted checks it and shows it on the TV. Branch `install-merge`: v0.7.0 plus
Client pick, the scan desktop and Live. Code: `photosort/live.py`, `photosort/flicker.py`, `photosort/ui/live.*`,
`photosort/ui/scene.*`.

## Using it

1. Tether with any app that writes shots to a folder: Sony Imaging Edge Remote, Canon EOS Utility, Lightroom.
2. In sorted, click Live, or Start a live shoot on the welcome screen. Pick that folder, the drive to keep the sorted
   photos on, and a shoot name. Find people and Find products are ticked; anything left off can be run later from the
   People and Products tabs. Scanning has the same choice: Find products sits under Detect faces, off by default.
3. Photographer window: the latest shot with blur and light alerts. When the next shot lands, it moves into its product
   or model on the right; Hide panel takes that away. Zooming to 100% holds the picture and counts what arrives.
4. Client window: Open client window puts it on the second screen, press F there. It shows the shot just taken and
   nothing else, no text and no quality message, soft frames included unless you press Hide from client. The
   photographer window works alone too.
5. Type or scan a product and a model name as you go. Without one, sorted opens "product 1", "product 2" by itself.
6. Export for store writes `<product>/<product>_01.jpg`, sharp frames only, longest side 4472 px.

`/ui/live.html?demo=1` plays a scripted shoot with no camera, from the repo only (it reads `site/img`).

## The drive

`<drive>/<shoot>/products/<product>/`, or `models/<model>/` when a model is named, `unsorted/` with Find products off.
Soft frames go in a `soft/` folder beside the sharp ones, RAW travels with its JPEG, and `manifest.csv` lists every
frame. The source folder is only read. Each frame is written as `.part`, flushed, size checked, renamed, marked in the
index, then hashed back. If the drive is pulled or fills up, copying pauses, shooting carries on, and it resumes on
remount. Renaming a product moves its files.

## What each shot gets

- **Sharp or soft**, judged on absolute floors, because shoot-relative labels drift while a shoot grows. Soft says
  where: on the eyes when there is a face, otherwise on the product. Product sharpness is measured on the product's
  pixels, not the frame.
- **Product**: nearest running centroid of grey embeddings at cosine 0.89, else a new product. A soft frame never
  opens a product when one exists, and never moves a centroid. An active product name overrides all of this.
- **Burst and bracket**: the v0.7.0 rules, run on the last frames.
- **Flash**: EXIF Flash bit 0. A radio trigger can leave it clear on a lit frame, so live treats clear as unknown and
  the rail says "no flash recorded". The session's Strobe switch declares it instead.

## Video flicker

`flicker.check_clip` decodes up to three short runs of consecutive frames and looks for the beat between mains light
(100 or 120 Hz) and the frame rate, plus rolling bands. Filters: Pulsing, Banding, Clean. Runs at scan time; a shoot
scanned earlier gets "Check for flicker". "unknown" means not enough steady footage, so a pan is never called flicker.

## Measured

- 280 to 335 ms a shot on the M1, warm, on a 9504x6336 JPEG of 9.9 MB. A real 25 to 33 MB frame decodes slower.
- Flicker, synthetic only: no false positives in 400 frame runs and 36 re-encoded clips. Static or moving subject
  40 of 40 at 0.5% strength, banding 40 of 40 at 1%, handheld about 75% at 2% and above.
- Simulated shoot through the real server, 10 frames: blurred product and face frames flagged, no page errors,
  nothing written in the watched folder.

## Limits

- Not run with a real camera, real RAW files or real LED footage yet.
- Soft product on a textured backdrop is mostly missed (4% caught on the drawn set). On plain sweeps every soft frame
  was caught and 8% of flat clean frames read soft.
- The verdict and the hero picture are the 1024 px thumb, so slight misfocus can pass.
- Not tested on a real SSD, exFAT, a real pull of the cable, or a real TV as second screen. Light alert thresholds are
  starting values.
- No direct USB (macOS holds the camera, needs sudo). No camera FTP receiver yet. No Shopify or WooCommerce upload.
- Clips in the watched folder are ignored. Categories do not run on Stop. Re-clustering products is offered, not run.
- On the Scan tab, product folders appear once Find products has run. Live draws them as shots land.
