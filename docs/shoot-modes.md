# Shoot modes (v0.7.0)

Camera-aware features for stills shoots: exposure brackets, bursts done properly, product shoots split by
product and colour, and the camera's own settings as filters. No language model anywhere; the only model is the
SigLIP image encoder every scan already runs.

Branch `shoot-modes`, off `siglip` 37c90c4.

## 1. Camera settings (the foundation)

What the camera wrote, read at scan time from the standard EXIF sub-IFD: shutter (`exposure_s`), aperture
(`fnumber`), `iso`, EV compensation (`ev_bias`), focal length (`focal_mm`, the 35 mm equivalent when the camera
wrote one), `lens`, `exp_mode` (ExposureMode) and `subsec` (SubSecTimeOriginal). PIL first, exifread as the
fallback for ARW and DNG, the same split as the date and camera reads.

- Cost: 2 ms cold, 0.3 to 0.7 ms warm per file (nine 61 MP ILCE-7RM5 JPGs, two ARW and two DNG fixtures).
- `lens` is NULL when never read and `''` when read and the file names no lens, so "not read yet" and "unknown"
  stay apart. A shoot scanned before v0.7 gets "N photos without camera settings, Read camera settings" in the
  rail (`index.read_camera_info`, `/api/camera/read`): header only, needs the disk, writes nothing under the
  shoot root (tested), counts files that are not on the disk as missing and leaves them NULL.
- Facets (rail, "Camera settings"): lens exact; focal, shutter, aperture and ISO in buckets
  (`config.CAMERA_BUCKETS`). "unknown" is a value with its own count. Clips are in none of them.
- Inspector: Lens, Focal length, Shutter, Aperture, ISO, EV comp, and "Drive: auto bracket" when the camera said so.

**MakerNote is not read.** exifread with `details=True` costs 220 to 415 ms per 61 MP Sony JPG, against about
83 ms for everything else a photo costs at scan time, and Sony keeps drive mode in enciphered tags exifread does
not decode anyway. ExposureMode 2 ("auto bracket") is the one drive hint in standard EXIF and it is used.

## 2. Exposure brackets

A bracket is a run of stills in one folder off one body with consecutive frame numbers, each within 2 s (plus the
previous frame's own shutter time, so a night bracket with a 4 s frame still chains) of the last, each neighbour's
exposure at least a third of a stop from the one before, and the same framing.

- **Exposure** is the full triangle, `log2(N^2/t) - log2(ISO/100)`, so a manual-mode bracket, where the shutter
  moves and EV compensation stays 0 on every frame (what the real Sony frames show), is found. EV compensation is
  the fallback when the triangle is missing. The EV list is relative to the median frame, to the nearest third:
  `0, -2, +2`.
- **Framing** is brightness-blind: the correlation of log-luminance gradients on a 32 px copy of the grid thumb,
  best over a ±2 cell shift (handheld drift). A stop up or down adds a constant to log luminance, which a gradient
  drops. Threshold 0.6. Calibrated on the 50 landing-page photos pushed to -2 and +2 stops and cropped up to 4%:
  same frame 0.62 worst, 0.82 median; two different photos 0.19 at the 95th percentile, 0.78 worst (two frames of
  one scene). SigLIP cosine on the same pairs overlapped badly (same frame 0.63 worst, different photos 0.91 at the
  95th), which is why the embedding is not the framing test. A new frame has to match any frame already in the
  run, not just the previous one: a Sony brackets 0, -2, +2, and the -2 and +2 are four stops apart.
- **Not a bracket:** a run spanning less than one stop from darkest to brightest, unless the camera wrote
  ExposureMode 2 (auto exposure drifts by thirds: the nine real ILCE-7RM5 frames went ISO 8000, 12800, 10000, and
  third-stop AEB is caught by the camera's own flag); two frames (a photographer adjusting) unless the camera wrote ExposureMode 2; an exposure
  coming round again splits the run, so 0 -2 +2 0 -2 +2 is two brackets; a frame of a bracket is never a burst
  frame, and two frames a whole stop or more apart are not a burst (auto exposure drifting by thirds inside a burst stays a burst).
- **UI:** group kind `bracket` with `n`, `ev`, `base` (the middle exposure). Folded, the tile is the middle
  exposure, never the darkest frame that came first. Badge "bracket -2 0 +2" (up to three frames) or "bracket of 5";
  inspector "3 frames, -2 0 +2 stops", "pick the middle exposure", "show every frame".
- **Measured** on the scratch shoot (214 stills: three handheld 0/-2/+2 brackets, one 5-frame aperture-priority
  bracket at thirds, bursts, singles, nine real Sony event frames where ISO changes by a third of a stop between
  frames, 180 product shots): 4 of 4 brackets found with the right EV lists, no false bracket.

## 3. Bursts

The existing rule (consecutive frame numbers, ≤1 s apart, cosine 0.9) is unchanged except for the exposure guard
above (a stop or more between neighbours breaks a burst). Added:

- `best_by`: "eyes" when every frame has a face, where `sharp` is already the eye-crop sharpness, so "sharpest" is
  the sharpest eyes; "detail" otherwise. The inspector says "pick the sharpest eyes" or "pick the sharpest".
- **Keep the best of each burst** (rail, under Sequences): every burst the current filters show, its sharpest
  frame into the pile, one call (`POST /api/groups/best?kind=burst`). Also "Keep the middle of each bracket".
- **Sequences facet**: Bursts / Brackets / Copies with live counts, "11 in 3" (frames in sets). Ticking one
  shows every frame of each set (`group=` filter on search, ids and facets). A kind the shoot has none of has no row.
- Fold still folds bursts and copies exactly as before; brackets fold too.

**Eyes open is not detected.** YuNet gives five landmarks, not eyelids, so "sharpest eyes" is the measurable
thing. A closed eye can still be a sharp eye.

## 4. Product shoots

`photosort/products.py`, the Products tab. Find products groups stills into products, splits each product into
colour variants, and counts angles inside a variant. Reads the grid thumbs on this Mac; the disk can be unplugged.
Scope: the whole shoot or "What Search shows" (the ids of the current filters).

- **Products**: average-linkage clustering of SigLIP embeddings of the **grey** grid thumbs, cosine 0.89. Colour is
  taken out on purpose: the stored colour embedding puts a red bottle nearer a red flask (0.94, two SKUs) than the
  same bottle from another side (0.83). Cost: one extra encode, 47 ms a photo on the M1 (47 s per 1,000).
- **Variants**: the dominant colour of the subject. The backdrop is the median colour of the frame's border;
  pixels are weighted by distance from it and by how central they are; binned into twelve hues plus three neutral
  lightness bands; the heaviest bin's mean Lab is the colour. A coloured bin with a quarter of the heaviest bin's
  weight wins over a neutral one (a watch is its strap, not its black face). Variants split at ΔE 21 (CIE76).
- **Angles** stay inside their variant: views are counted (cosine 0.97) and never make a new variant.
- **Names**: a 59-word product vocabulary scored like the discovered categories, best word must beat the runner-up
  by 0.01; otherwise "product N". Rename inline. Swatch names from a fixed 24-colour table.
- **Click** a swatch: Search filtered to that product and colour (chip "backpack green", in the URL hash).
  "every colour" filters the product. **Export one folder per colour**: `<dest>/<shoot>/products/<product> <colour>/`.

**Calibration set**: `scripts/make_product_set.py`, 180 files: seven drawn products (bottle, flask, mug, sneaker,
headphones, backpack, watch) in six colours (orange next to red and navy next to blue on purpose; the flask is a
near-twin SKU of the bottle) and four views (as shot, turned 20°, mirrored and cropped, squashed to three-quarter),
plus the landing page's one real product photo hue-shifted three ways.

| | colour embeddings, cut 0.80 | grey embeddings, cut 0.89 |
|---|---|---|
| products found (truth 8) | 7, bottle and flask merged | 8 |
| product ARI | 0.854 | 1.000 |
| product purity | 0.867 | 1.000 |
| variants (truth 45) | | 45, purity 1.000 |
| same colour, other angle kept in its variant | | 270 of 270 pairs |
| other colour split off | | 1,728 of 1,728 pairs |

Grey embeddings are perfect anywhere from 0.88 to 0.90; 0.89 is the middle. ΔE between views of one colour: 0.7
median, 16.8 worst; between colours: 24.8 worst (red against orange).

**On the mixed scratch shoot** (214 stills, the whole shoot as scope): every synthetic product was pure and whole,
the real photo split into two products (9 + 3; the mirrored crops), and nine more "products" came from repeated
non-product content (bracket sets, bursts, the event frames). That is correct behaviour (they are the same thing
shot several times) but it is noise on a mixed shoot: filter Search to the product shots and use "What Search
shows". Names: backpack, bottle, bottle 2 (the flask), headphones, watch, cosmetic tube; mug and sneaker stayed
"product 1" and "product 2".

These are synthetic numbers. Nobody has run Products on a real client product shoot yet.

## Not done

- **Focus stacks**: no solid evidence. Sony writes no focus distance outside the MakerNote, and an image-only signal
  (the sharp region moving across the frame) cannot tell a focus stack from a burst with a moving subject without
  a real stacked set to calibrate on. A focus stack today shows as a burst when it is shot fast, else as singles.
- **Panoramas**: not done. Overlap detection needs feature matching between neighbours, not cheap at 20k.
- **MakerNote drive mode and bracket step** (Sony, Canon, Nikon, Fuji): see section 1 for the cost.
- **Eyes open**: see section 3.
- Facet counts are rows; with fold on the context bar counts tiles (as before for every facet).
- Products ride in the `.sorted` project file with the rest of index.db; they are not re-run when the shoot is rescanned, so Find products again after adding photos.
- Drive export of products: local destinations only.
- `/api/facets` now also runs `Index.facet_counts` over every row in Python for the sequence and camera axes. Fine at
  214 rows; not timed at 20k (the older facet counts went to SQL indexes for speed).
