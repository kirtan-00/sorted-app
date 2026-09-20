# Report: one person, one group (face merge suggestions)

Branch diu-scale. Commits `0338fb3` (clustering + links + suggestions, tests) and the commit that carries this report (endpoints + CLI, tests). Suite after the fix round: 219 passed, 1 skipped.

## What was built

**config.py**: `FACE_CLUSTER_EPS = 0.32` (was 0.5), `FACE_MERGE_SUGGEST_SIM = 0.55`, `FACE_MERGE_AUTO_SIM = 1.0` (automatic merge off; see calibration for why).

**db.py**: table `face_links(a, b, decision, created_at, PRIMARY KEY(a, b))`, a < b are face ids. `add_face_link(conn, a, b, decision)` orders the pair and uses INSERT OR REPLACE, so a later answer replaces an earlier one (a yes after a no wins, and the reverse). `face_links(conn)` returns `[(a, b, decision)]`. `load_face_people(conn)` returns face ids and their person ids in the same order as `load_face_embeds` (no more second query with an assumed order).

**people.py**:
- `cluster_faces(root, eps=None, ...)`: DBSCAN at eps (None means FACE_CLUSTER_EPS), then `_apply_links`:
  1. must-links: every "same" pair whose faces sit in two groups joins the groups (union-find); a noise face with a "same" link joins its partner's group; two noise faces with a "same" link become a group of their own.
  2. auto-merge (only when FACE_MERGE_AUTO_SIM < 1.0): repeatedly take the pair of groups with the highest centroid cosine at or above the threshold that has no "different" link between any face of one and any face of the other, join them, recompute centroids. Best pair first, one join per pass, so a chain cannot re-form the blob.
  3. labels compacted; names survive by majority vote over the merged members, so a named plus an unnamed group keeps the name.
  A "same" chain that joins two groups which also carry a "different" link between them: same wins (the yes was explicit; DBSCAN never splits a group on a no). Documented in the docstring.
- `suggest_merges(root, limit=50)`: centroid (mean of unit vectors, re-normalised) per group, every pair with cosine >= FACE_MERGE_SUGGEST_SIM, at least one side with GROUP_MIN_FACES (3) faces or more (a pair of two tiny groups has centroids too noisy to trust; a tiny group against a big one is fine and is exactly the burst-of-34 case below), minus pairs with a "different" link, sorted by sim desc. `a_n`/`b_n` are FACE counts (the centroid's support), not `people.n` photo counts. Each side carries `cover: {qhash, box}` plus up to 3 more `{qhash, box}` faces (best score, cover excluded), box in preview coordinates like `/api/people` covers.
- `merge_people(root, keep, drop)`: faces of drop move to keep, `n` recomputed as distinct photos, name = keep's or drop's, drop row deleted, keep's cover kept (healed to its best face if missing), "same" link written between the two covers (captured before the move). Returns the updated person dict in the `/api/people` shape. ValueError on self-merge or unknown id.
- `reject_merge(root, a, b)`: "different" link between the two covers. ValueError on same id twice, unknown id, or a group with no faces.

**server.py** (one contiguous block after `/api/people/{pid}/name`):
- `GET /api/people/suggestions` -> `{"suggestions": [...]}` (`[]` with no folder open); each item `{a, b, sim, a_name, b_name, a_n, b_n, a_cover: {qhash, box}, b_cover: {qhash, box}, a_faces: [{qhash, box} x up to 3], b_faces: [...]}`.
- `POST /api/people/merge` `{keep, drop}` -> the updated person (same shape as a `/api/people` row); marks the index stale.
- `POST /api/people/reject` `{a, b}` -> `{"ok": true}`.
- All three: 400 on bad ids or no folder, 409 while indexing.
- `ClusterReq.eps` is now `float | None = None` (None = FACE_CLUSTER_EPS). The shape is unchanged.

**cli.py**: `python -m photosort.cli people <folder>` prints `N groups, M same-person suggestions (answer them in the People tab)` after the listing; `--eps` defaults to the config value instead of 0.5.

**Tests**: tests/test_people.py (+8: tight/loose eps, same link across re-cluster, same link pulls a noise face in, different link blocks an auto-merge, suggestions near/far/rejected with the full shape, merge keeps either name and heals the cover, guards, reject then same overrides), tests/test_server.py (+2: the three endpoints, 200 shapes, 400s, 409s, no-folder cases), tests/test_cli.py (+1). Every test that touches a shoot dir asserts the listing is unchanged. Synthetic embeddings only.

## Calibration (copy of `01 Photos-faeec8a5/index.db`, 6,569 faces, 3,677 photos; thumbs read from the live dir, nothing written there)

Sheets: `.superpowers/sdd/face-merge-sheets/` (untracked copies; originals in the session scratchpad `faces/sheets/`). Group sheets sample 48 faces evenly across the group's score order, so the low-score tail is visible. An objective impurity floor was added: a person id owning two faces in one photo has at least one wrong face, so `sum(faces - distinct photos)` over groups is a lower bound on wrong faces.

| eps | groups | noise | largest groups (faces) | impurity floor | what the sheets showed |
|---|---|---|---|---|---|
| 0.50 | 105 | 98 | 5388, 606, 52 | (not run) | one blob holds 82% of all faces; the brief's "1,900 blob" is 5,388 on this copy |
| 0.42 | 329 | 268 | 3365, 594, 244 | 1853 | 3,365-face blob |
| 0.38 | 407 | 361 | 956, 770, 556, 369 | 588 | 956 group: at least five people (clean-shaven man, bearded man in glasses, a woman in sunglasses, a bald man, blurred kids); 770 group: the client, 0/48 wrong |
| 0.35 | 467 | 425 | 729, 553, 369, 199 | 306 | 729 client 0/48; 553 bald man in glasses 0/48; 369 group is 369 faces in 213 photos, two moustached men mixed; 199 group: 6/48 wrong, all in the bottom row (kids, backs of heads, two orange-fabric non-faces at score 0.71 to 0.82) |
| **0.32** | **518** | **487** | **726, 553, 198, 176, 174, 171** | **116** | all six largest viewed, 0/48 wrong on each. The 369 mixed group split into 198 (one moustached man, pure, `track_eps0.32_person173_198faces.png`) + 105 (a wide-faced smiling man, pure, `track_eps0.32_person174_105faces.png`) + 63 (a third moustached man, frontal) + 3; the 199 group split into 162 (the clean-shaven man, pure) + 29 (backs of heads and a kid, junk but pure of him). 76 of the 116 floor is one "blur blob" of tiny out-of-focus background faces (person 182, 115 faces in 39 photos), which no eps fixes; the rest is five small groups of 2 to 12 excess faces |
| 0.30 | 554 | 548 | 724, 480, 186 | 85 | the 553 bald man splits 480 + rest; more questions for the user with no purity gain on the big groups |

Chosen: **eps 0.32**. The two people who matter most on this shoot stay whole (726 and 553 faces) and every large group I looked at is pure. Cost: the client's 770 faces at 0.38 become 726 + a burst of 34 near-identical low-res frames (sim 0.62 to the main group, rank 62 in the suggestion queue, sheet `client_split_eps0.32_person291_34faces.png`) + 6 faces at sim 0.53 (below the suggest threshold, reachable by find-by-photo) + 4 noise. Noise is 487 of 6,569 faces (7.4%).

Suggestions at eps 0.32: 408 pairs at sim >= 0.50, 174 >= 0.55, 89 >= 0.60, 38 >= 0.65, 6 >= 0.70. Top 30 (`sug_eps0.32_top01-15.png`, `sug_eps0.32_top16-30.png`, four faces per side): 24 clearly the same person, 5 blur-against-blur that look the same but cannot be verified at 96 px, 1 pair (rank 2, sim 0.74) is the blur blob against another blur group, meaningless either way. No clearly wrong pair in the top 30. Band 0.50 to 0.55 (`sug_eps0.32_band50-55.png`, first 15): about 5 same, 2 likely different (a yellow-kurta bearded man against a wider-faced man in profile; a man against a fabric non-face), the rest blur or hair-only junk. Band 0.45 to 0.50: mostly different. So `FACE_MERGE_SUGGEST_SIM = 0.55`, the same line as the single-face threshold, not the brief's 0.50: below it the queue is mostly clicks on junk.

`FACE_MERGE_AUTO_SIM` stays 1.0. Above 0.70 there are six pairs and two of them are blur-against-blur that I cannot call, so "zero wrong auto-merges" cannot be shown. Everything above 0.65 that was verifiable was correct, so 0.70 is the value to try once the merge UI has been used on a shoot and the user's answers can be checked against it.

## Fix round (quality floors + size-aware rank)

**Signal for the blur blob: focus, not size.** On the copy the 115-face blob (person 182 at eps 0.32) has a median short edge of 32 px, while three of the six biggest groups (198, 176, 174 faces) contain faces down to 13 px, so no edge floor removes the blob without gutting real groups (edge < 32 removes 57 of 115 blob faces and 543 faces from the six biggest). `eye_sharp` (Laplacian variance of the eye strip on the preview) separates them cleanly: blob 5th/50th/95th percentile = 13 / 25 / 73, the six biggest groups' minimums are 60, 97, 116, 391, 423, 799.

| eye_sharp floor | faces dropped by focus | blob faces left in a group | faces lost from the six biggest |
|---|---|---|---|
| 30 | 187 | 37 | 0 |
| **40** | **265** | **9 (two groups of 5 and 4; 97 unassigned)** | **0** |
| 50 | 332 | 6 | 0 |
| 60 | 366 | 3 | 0 |
| 70 | 407 | 7 | 6 |

Chosen `FACE_CLUSTER_MIN_EYE_SHARP = 40.0` (the smallest floor at which the blob is gone) and `FACE_CLUSTER_MIN_EDGE = 10` (138 sharp-looking 7 to 9 px background heads: upscaled to 96 px they are texture, not identity; 91 of them were sitting in groups). Excluded faces keep `person_id` NULL; `find_by_reference` and the named references are untouched and still see them. Sheets: `excluded_eye60_edge10.png` (an even sample of everything under the earlier 60 floor: rows 1 to 2 are the 8 to 9 px heads, the rest is blur, with about 5 soft but recognisable faces in 48), `excluded_eye40_sharpest48.png` (the band just under 40: blur, with a handful of soft faces: three women in a row at 36, a smiling man at 38; they stay findable by reference).

Numbers at eps 0.32, before -> after the floors: groups 518 -> 467, faces without a group 487 -> 818 (403 of them excluded by the floors: 265 focus, 138 size), largest groups unchanged (726, 553, 198, 176, 174, 171), impurity floor 116 -> 15 (worst now 20 faces in 12 photos). One caveat seen on the sheets: an eye strip covered by dark sunglasses or a missed landmark can score low on a sharp face (one 73 px sharp man at eye_sharp 15), so a few sharp faces are excluded too; the client's sunglasses shots are safe (his group's minimum is 116).

**Rank.** `rank = sim * (1 + log10(min(a_n, b_n)))`, sorted desc; `sim` unchanged in the payload and the 0.55 threshold still applies to `sim`. The client's 34-face burst against his 726-face group (sim 0.62) moves from rank 62 to **rank 9**. Top 15 by rank (`sug_ranked_top01-15.png`): 13 the same person, 1 likely different (a stocky man in black sunglasses against the slim bearded man in glasses, sim 0.58, rank 10), 1 uncertain. 148 pairs at or above 0.55 after the floors.

## Concerns

0. **The live server on 7777 runs the old module.** The three endpoints and the 0.32 default exist only after a restart, which I did not do (per the brief). Testing against the live port before a restart will 404 on `/api/people/suggestions`.
1. **UI still sends eps 0.5.** `photosort/ui/app.js` line 701 posts `parseFloat($("#eps").value) || 0.5`; the People tab will keep clustering at 0.5 (one 5,388-face blob) until the UI agent changes the input's default to 0.32 or stops sending eps. The server default is already 0.32. Not touched here per the brief.
2. **Links die with a re-index of their photo.** `replace_faces` deletes and re-inserts a photo's faces with new ids, so a link whose cover face sits in a re-indexed photo vanishes silently. Survives re-cluster, as promised; does not survive re-index.
3. **A "different" link does not split a DBSCAN group.** If DBSCAN itself puts two faces the user separated into one group, the no is not enforced there (the brief scoped cannot-links to the auto-merge). Rare at 0.32 but possible.
4. **Blur blob and rank: fixed in the fix round above.** Remaining edge: `eye_sharp` is a Laplacian variance on the preview, so its absolute scale depends on the shoot (sharp lens, ISO noise); 40 is calibrated on one shoot. A noisy high-ISO indoor shoot may pass more blur, a very clean one may exclude soft-but-real faces; they stay findable by reference either way.
6. `suggest_merges` re-reads the whole face matrix on every GET (about 0.3 s on 6,569 faces). Fine for a click; not for polling.
7. The `.superpowers/sdd/face-merge-sheets/` PNGs are untracked on purpose (only the listed paths were committed). Delete or commit as you like.
