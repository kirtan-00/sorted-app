# Client pick

A product shoot cut down to what the client sees. Products view, second mode ("Client pick"). Branch
`product-cull`, off `shoot-modes` 186e663 (v0.7.0). Code: `photosort/cull.py`; generator with ground truth:
`scripts/make_cull_set.py`; tests: `tests/test_cull.py`.

The job it was built for: 3,500 frames of 30 products down to about 300 (ten a product), every colour and angle
covered, blurry, cut-off and badly exposed frames and retakes thrown out, by hand, which took a team a long time.

## What it does

1. **Measures on the product, not the frame.** The product is what differs from the backdrop: a plane fitted to the
   frame's border in Lab (so a sweep lit darker towards the floor is still backdrop), delta E above 14, on a 160 px
   copy. Everything else is read inside that region on the 1024 px thumb (this Mac only; the disk can be unplugged):
   - sharpness: the mean of the strongest 1% of colour gradients;
   - the same by edge direction (eight bins): motion blur smears the edges across the way the camera moved and
     keeps the others, defocus softens every direction alike;
   - which frame edges the product runs off;
   - the share of the product clipped white (brightest channel 250 or more) or black (every channel 8 or less).
2. **Judges a frame against its own angle.** Retakes are grouped by framing (`search.framing_similarity`, the
   brightness-blind compare the brackets use), at 0.78, inside one colour. Every frame is held against the sharpest
   unclipped frame of its retake group (or, alone in it, any frame framed at least 0.6 like it). So a matte mug is
   never held to a laced sneaker's detail, a glossy can's highlight that clips in every frame is the product, not a
   mistake, and a detail shot that runs off the frame in three or more frames of its group is how it was framed.
3. **Gives every reject a reason in words**, in this order: blown out on the product, crushed to black on the
   product, product cut off by the frame edge, motion blur, soft on the product, not sure which product this is,
   retake of this angle (a sharper one is kept).
4. **Picks** a target a product (default 10) or a total split across products by how many colours each has
   (largest remainder, a product that cannot give its share passes the rest on). Inside a product: every colour
   first, each at the angle the picks have least of; then every angle not yet picked; then round the colours again.
   Best score wins inside a step. One frame a retake group, always. Angles across colours are matched by framing at
   0.70 and numbered by how many colours were shot at them.
5. **Frames Find products left out.** Small clusters (under 5% of the biggest product) and clusters that are mostly
   clipped or cut are taken apart and moved, a cluster at a time, to the product their frames are nearest on
   average (grey cosine 0.80 or more), each frame into that product's nearest colour. They are judged there but never
   picked for you: on the 3,500 set 76% landed on the right product. Frames near nothing are listed last as "not
   matched to a product" with whatever is wrong with them.

UI: the count ("373 picked from 3,424, 57 products"), what was set aside by reason, the target (Each product / In all
and a number; Pick again re-picks from the stored reasons instantly). One row a product: picks with colour and angle,
Swap (the next frame of the same colour and angle, best first, then retakes and rejects, round and round) and Drop on
hover; the rejects folded under one line ("105 left out: 68 retakes, 1 soft, ..."), opened into groups by reason,
each tile captioned with its reason and a Pick button. Click a tile to see it big with the whole sentence. "Same
product as" folds a product Find products split off (detail shots, mostly) into the right one. Add to Picked puts the picks on the Picked list;
Export for the client: `<destination>/<shoot>/client pick/<product>/<colour>/` plus `client pick.csv` (product,
colour, angle, file, status, reason) for every frame, picked or left out.

Endpoints: `GET /api/cull`, `POST /api/cull/run {per_product | total}`, `GET /api/cull/progress`,
`POST /api/cull/target`, `POST /api/cull/swap {id}`, `POST /api/cull/pick {id, on}`, `POST /api/cull/merge
{product, into}`, `POST /api/cull/pile`, `POST /api/export/cull {mode, include_raw}`. A new Find products clears the
picks (the product numbers change).

## Calibration and proof

`scripts/make_cull_set.py` draws 30 products (the seven of make_product_set.py and 23 more), 2 to 5 colours each, 5
or 6 angles (front, turned 25 degrees, three-quarter, from above, a detail that fills the frame, wide), 2 to 7 clean
retakes of each angle (a few pixels and a fraction of a degree apart, up to half a pixel softer), and on average 1.6
bad frames an angle: soft on the product only (Gaussian 2.5, 4 or 7 px; half the products sit on a sharp linen
tabletop that stays sharp), motion blur (10, 18 or 30 px, any direction), cut off (20 to 45% of the product past one
edge), blown (the product's brightest channel pushed 0.6 or 1.2 stops past white), crushed (a dark product under a
curve that sends half or more of it to black). Every file's truth is in truth.csv.

Thresholds were read off `--products 8 --seed 1` (840 files) and then run, unchanged, on `--products 30 --seed 2`
(3,424 files). Two failure modes found on the 3,500 set were fixed by judging against the frame's own angle (glossy
highlights, detail shots running off the frame) without moving a threshold.

| reason | calibration, 840 | held-out, 3,424 |
|---|---|---|
| blown out | P 1.000, R 1.000 | P 0.890, R 0.929 |
| crushed to black | P 1.000, R 1.000 | P 1.000, R 0.946 |
| cut off | P 0.953, R 1.000 | P 0.844, R 0.880 |
| motion blur | P 0.753, R 0.902 | P 0.726, R 0.754 |
| soft on the product | P 0.868, R 0.719 | P 0.779, R 0.799 |
| soft or motion, either word | P 0.960, R 0.968 | P 0.897, R 0.926 |
| retake | P 0.990, R 0.975 | P 0.981, R 0.945 |

Retake precision and recall count, for every true angle of a colour with c clean frames, c - 1 expected retakes.

Picks on the 3,424 set at 10 a product: 371 picked, 5 of them a bad frame (1.3%), 4 a second frame of the same
colour and angle; every colour of every product covered but one (102 of 103), 159 of 165 angles.

The whole-frame focus check (`focus.py`) on the same frames labels 115 of the 229 soft-on-the-product frames "ok":
the linen is sharp.

**Time on the M1** (3,424 photos, other jobs running, load average 6 to 14): Find products 159 s, Client pick
60 s (measuring at about 40 ms a photo across six threads, 3.5 s to judge and pick), 4 s to run again from stored
measurements, 0.02 s to re-pick for a new target. The scan itself (thumbs and embeddings, needed anyway) was 244 s.

## Not done, and what it gets wrong

- **Blinks.** Not detected: YuNet gives eye centres, not eyelids (docs/shoot-modes.md).
- **Soft against motion.** The two are told apart well enough to say "blurred" (0.90 / 0.93) but swap words on about
  a fifth of them; a soft frame with one strong edge direction reads as motion.
- **A frame with nothing sharper at its angle** cannot be called soft: it is its own reference. 5 of 371 picks.
- **Find products splits products** (a detail shot is its own "product"; 132 clusters for 30 products before Client
  pick folds the fragments back, 57 after). The split ones each get their own ten picks until you fold them with
  "Same product as". Auto-folding was measured and rejected: a detail cluster is no closer to its own product (grey
  cosine 0.79 to 0.93) than to another product of the same palette (up to 0.90).
- **Synthetic only.** Drawn products, not a real client shoot. The relative design (every frame against its own
  angle) is what should carry over; the absolute numbers (FG_DE, CUT_SIDE, the clipping shares) want checking on
  the first real product day.
