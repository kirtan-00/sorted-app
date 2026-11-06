# Live shoot

The camera shoots, the shot lands on the MacBook, sorted checks it and shows it on the TV. Code: `photosort/live.py`,
`photosort/flicker.py`, `photosort/ui/live.*`.

## Using it

1. Tether with any app that writes shots to a folder: Sony Imaging Edge Remote, Canon EOS Utility, Lightroom.
2. In sorted, click Live, or Start a live shoot on the welcome screen. The start screen comes filled in: the folder you
   last used, the drive, and a shoot name. Press Start.
   - **Where your shots land**: Change opens the folder picker. Or take one test frame: sorted looks for a folder an
     image was written to in the last minute, in the home folder and on the disks, and offers it.
   - **Copy sorted photos to**: the drive used last if it is plugged in, else the writable disk with the most free
     space, never a camera card. Somewhere else holds a folder of your own, or This Mac only for no second copy.
   - **More options**: Flash (Auto, Strobes, No flash), Find people, Find products. Anything left off can be run later
     from the main window.
   - When Start cannot be pressed, the screen says why, a running scan included.
3. If the folder already has photos, the screen asks once. Start, only new shots leaves them out of the shoot; Take all
   N too brings them in. Either is a real new shoot, even in a folder used before: the last shoot's names and queue step
   aside and nothing old goes on the drive under the new name. Back to the last shoot carries the old one on instead.
   The watched folder is never written to. Libraries and catalogs inside it (`.photoslibrary`, `.cocatalog`, `.lrdata`
   and the like) are never walked.
4. Photographer window: the latest shot with blur and light alerts. When the next shot lands, it moves into its product
   or model on the right. Click the picture or press Z for 100%. Hold C to see the last shot. Space, or Hold this shot,
   keeps the shot on stage and counts what arrives; Back to live lets go. View holds Fullscreen (F) and Hide sorted
   shots.
5. Client window: Show on TV puts it on the second screen, press F there. It shows the shot just taken and nothing
   else, no text and no quality message, soft frames included unless you press Hide from client. The photographer window
   works alone too.
6. Type or scan a product and a model name as you go. Without one, sorted opens "product 1", "product 2" by itself. If
   both are typed, the model wins: the shot goes in the model's folder and the product stays out of the path, kept only
   in the manifest. Enter on an empty field clears it; a field emptied and left, or spaces alone, change nothing. After
   Enter the name stays selected for the next scan, and Space is the hold.
7. Finish shoot shows the counts, where the files went, and the project file. Save store copies writes
   `<product>/<product>_01.jpg`, sharp frames only, longest side 4472 px. Open in sorted goes to the main window on this
   shoot. Carry on this shoot starts watching again.

`/ui/live.html?demo=1` plays a scripted shoot with no camera, from the repo only (it reads `site/img`).

## The drive

`<drive>/<shoot>/products/<product>/`, or `models/<model>/` when a model is named, `unsorted/` with Find products off.
Soft frames go in a `soft/` folder beside the sharp ones, RAW travels with its JPEG, and `manifest.csv` lists every
frame. The source folder is only read. Each frame is written as `.part`, flushed, size checked, renamed, marked in the
index, then hashed back. If the drive is pulled or fills up, copying pauses, shooting carries on, and it resumes on
remount. Renaming a product moves its files. A new shoot never shares a folder with an old one: a name already used on
the drive becomes `<shoot> 2`. Starting a new shoot while the last one still has shots waiting leaves those on the Mac,
and the start screen says so first.

Each new shoot saves its own project file, `sorted_<shoot>.sorted`, in the folder the last project went to or the export
folder, so a second shoot in the same folder does not write over the first.

## What each shot gets

- **Sharp or soft**, judged on absolute floors, because shoot-relative labels drift while a shoot grows. Soft says
  where: on the eyes when there is a face, otherwise on the product. Product sharpness is measured on the product's
  pixels, not the frame.
- **Product**: nearest running centroid of grey embeddings at cosine 0.89, else a new product. A soft frame never
  opens a product when one exists, and never moves a centroid. An active product name overrides all of this.
- **Burst and bracket**: the v0.7.0 rules, run on the last frames.
- **Flash**: EXIF Flash bit 0. A radio trigger can leave it clear on a lit frame, so live treats clear as unknown. The
  session's Strobes switch declares it instead.
- **Light alerts**: flash may not have fired, darker than the last shots, blown out on the product, ISO jumped, shutter
  too slow to hand hold. The shutter one comes once per setting, not on every frame at that setting.

## Video flicker

`flicker.check_clip` decodes up to three short runs of consecutive frames and looks for the beat between mains light
(100 or 120 Hz) and the frame rate, plus rolling bands. Filters: Pulsing, Banding, Clean. Runs at scan time; a shoot
scanned earlier gets "Check for flicker". "unknown" means not enough steady footage, so a pan is never called flicker.

## Measured

- 280 to 335 ms a shot on the M1, warm, on a 9504x6336 JPEG of 9.9 MB. A real 25 to 33 MB frame decodes slower.
- Flicker, synthetic only: no false positives in 400 frame runs and 36 re-encoded clips. Static or moving subject
  40 of 40 at 0.5% strength, banding 40 of 40 at 1%, handheld about 75% at 2% and above.
- Simulated shoot through the real server: 4 photos already in the folder and a Photos library inside it, Only new
  shots, 4 frames dropped in. The blurred frame was flagged, none of the 4 old ones reached the drive, the source files
  were unchanged, the client window had no text, no page errors. A second shoot in the same folder started empty.

## Limits

- Not run with a real camera, real RAW files or real LED footage yet.
- Soft product on a textured backdrop is mostly missed (4% caught on the drawn set). On plain sweeps every soft frame
  was caught and 8% of flat clean frames read soft.
- The verdict and the hero picture are the 1024 px thumb, so slight misfocus can pass.
- Not tested on a real SSD, exFAT, a real pull of the cable, or a real TV as second screen. Light alert thresholds are
  starting values.
- No direct USB (macOS holds the camera, needs sudo). No camera FTP receiver yet. No Shopify or WooCommerce upload.
- Clips in the watched folder are ignored. Categories do not run on Finish shoot. Re-clustering products is not run.
- On the Scan tab, product folders appear once Find products has run. Live draws them as shots land.
- Only the last shoot in a folder can be carried on. The one before it is kept in the index (`*_prev` tables), with no
  button to bring it back.
- The folder finder is limited to four levels under each place it looks, one second a look, and a frame written in the
  last minute. Looking in Documents or Desktop can raise the Mac's own permission prompt the first time.
