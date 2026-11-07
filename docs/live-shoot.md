# Live shoot

The camera shoots, the shot lands on the MacBook, sorted checks it and shows it on the TV. Code: `photosort/live.py`,
`photosort/flicker.py`, `photosort/ui/live.*`.

## Using it

1. Either tether with any app that writes shots to a folder (Sony Imaging Edge Remote, Canon EOS Utility, Lightroom), or
   send straight from the camera over Wi-Fi with its own FTP transfer, no app in between (docs/direct-camera.md).
2. In sorted, click Live, or Start a live shoot on the welcome screen. The start screen comes filled in: the folder you
   last used, the drive, and a shoot name. Press Start.
   - **Where your shots land**: Change opens the folder picker. Or take one test frame: sorted looks for a folder an
     image was written to in the last minute, in the home folder and on the disks, and offers it.
     **Straight from the camera (Wi-Fi)** needs no folder: sorted makes `<export folder>/Live/<shoot name>/`. It shows
     Host, Port, User and Password for the camera menu, each copied by a click, with short Sony steps behind a link. The
     login is kept per Mac, so the menu is set once; Reset login makes a new one. If the Mac's address changes, the screen
     says what the camera still has and what to change it to.
   - **Copy sorted photos to**: the drive used last if it is plugged in, else the writable disk with the most free
     space, never a camera card. Somewhere else holds a folder of your own, or This Mac only for no second copy.
   - **More options**: Flash (Auto, Strobes, No flash), Find people, Find products. Anything left off can be run later
     from the main window.
   - When Start cannot be pressed, the screen says why, a running scan included.
3. If the folder already has photos, the screen asks once. Start, only new shots leaves them out of the shoot; Take all
   N too brings them in. Either is a real new shoot, even in a folder used before: the last shoot's names and queue step
   aside and nothing old goes on the drive under the new name. Back to the last shoot carries the old one on instead.
   sorted never writes into a tether app's folder. Straight from the camera, its own receiver does, and only finished
   frames: each arrives in a hidden `.sorted-incoming/` and is renamed in when whole, never over another file. An upload
   that breaks off stays there, out of the shoot. One that ends but looks cut short still comes in, and sorted says "may
   be incomplete" so it can be checked on the card. Libraries and catalogs inside the folder (`.photoslibrary`,
   `.cocatalog`, `.lrdata` and the like) are never walked.
   While shooting, the status line says when the camera connected and the last shot came. It warns if the camera
   connected and sent nothing for two minutes, never during a pause. Finish shoot waits for a frame still arriving.
4. Photographer window: the latest shot with blur and light alerts. When the next shot lands, it moves into its product
   or model on the right. Click the picture or press Z for 100%. Hold C to see the last shot. Space, or Hold this shot,
   keeps the shot on stage and counts what arrives; Back to live lets go. View holds Fullscreen (F) and Hide sorted
   shots.
5. Client window: Show on TV puts it on the second screen, press F there. It shows the shot just taken and nothing
   else, no text and no quality message, soft frames included unless you press Hide from client. The photographer window
   works alone too.
6. Type or scan a product and a model name as you go. Without one, sorted opens "product 1", "product 2" by itself, and
   with Find people on "model 1", "model 2" as new faces land. The numbers count piles on screen: a model's shot opens a
   product nobody sees, named "other 1", which takes the next product number only if a shot ever shows it. If both are typed, the model wins: the shot goes in the
   model's folder and the product stays out of the path, kept only in the manifest. Enter on an empty field clears it; a field emptied and left, or spaces alone, change nothing. After
   Enter the name stays selected for the next scan, and Space is the hold.
   In the Models tab, click a name to rename it. Type a name that is already there and the two become one, which is the
   fix when light or an angle splits a person in two. Not a model, beside the shot, sends a shot back to its product.
7. Finish shoot shows the counts, where the files went, and the project file. Save store copies writes
   `<product>/<product>_01.jpg`, sharp frames only, longest side 4472 px. Open in sorted goes to the main window on this
   shoot. Carry on this shoot starts watching again.

`/ui/live.html?demo=1` plays a scripted shoot with no camera, from the repo only (it reads `site/img`).

## The drive

`<drive>/<shoot>/products/<product>/`, or `models/<model>/` when the shot has a model, `unsorted/` with Find products
off. A typed model always wins. A model sorted found wins over a product it opened, but a typed product wins over a model
it found: the shot stays in `products/<SKU>/`, and the model is still recorded, in the manifest and the Models tab count.
Soft frames go in a `soft/` folder beside the sharp ones, RAW travels with its JPEG, and `manifest.csv` lists every
frame. The source folder is only read. Each frame is written as `.part`, flushed, size checked, renamed, marked in the
index, then hashed back. If the drive is pulled or fills up, copying pauses, shooting carries on, and it resumes on
remount. Renaming a product or a model moves its files, and merging two models moves them into one folder. A new shoot never shares a folder with an old one: a name already used on
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
- **Model**, with Find people on. Only the main face counts: the largest, at least 0.08 of the frame's long edge and 48 px,
  detection score 0.8 or more, so someone behind the subject opens nothing. Two equal faces in a frame join the larger.
  It joins the nearest running centroid of SFace embeddings at cosine 0.55. It opens a new model only below 0.40, and
  between the two it waits, so one doubtful frame does not split a person. A frame with no main face, or one that waits,
  goes where it would have anyway, to its product. A soft face never opens a model, even the first, and never moves a
  centroid; it joins only at 0.55 like any other, so a badly blurred shot stays in its product's `soft/` folder. A typed
  model overrides this and teaches the centroid. The first frame under a newly typed name, if its face matches a model
  still called "model N", renames that model, so the person on set does not become two. If the face does not match,
  the name is someone new.
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
- Models, on the 23 people in `site/img`, 1,076 faces: 26 photos varied by crop, flip, exposure, contrast, rotation,
  downscale and blur, at most 640 px. Different people: 543,117 pairs, closest 0.53, 99% under 0.42. Same photo varied:
  22,418 pairs, 1.3% under 0.55. The two people with two real photos: 3,698 pairs, 5.1% under 0.55, 0% under 0.40. People's
  own cut (0.68, cosine distance 0.32) would part 44% of those, so it is not used. 0.55 is `FACE_MATCH_MIN_SIM`, from a
  real shoot, where 0.50 to 0.55 was only a third right. Simulated shoots, 300 of 4 people with 20 frames each, 1,200 people
  in all: 0.9% of people split, none merged, 1% of frames waited. With a single cut at 0.55, 5.1% split. Identity holds to a
  48 px face (cosine 0.88 to the full size) and falls away by 32. Blur past 3% of the face's size takes a face below 0.58, and
  heavy blur leaves it closer to strangers (0.28) than to itself (0.15). This is small and mostly synthetic: few real
  second photos, no real shoot, no real lookalikes, no changes of make up or hair.
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
- A shot with a model still takes a product, so hidden products can make the first visible one "product 3". A face
  sorted is unsure of never opens a model on its own: type the name. Faces out of focus in front of the subject can be the
  largest and speak for the frame.
- Clips in the watched folder are ignored. Categories do not run on Finish shoot. Re-clustering products is not run.
- On the Scan tab, product folders appear once Find products has run. Live draws them as shots land.
- Only the last shoot in a folder can be carried on. The one before it is kept in the index (`*_prev` tables), with no
  button to bring it back.
- The folder finder is limited to four levels under each place it looks, one second a look, and a frame written in the
  last minute. Looking in Documents or Desktop can raise the Mac's own permission prompt the first time.
