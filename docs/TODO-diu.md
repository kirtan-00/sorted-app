# Diu dataset: next build (captured 2026-09-18, before laptop restart)

Test on "Dhaval lifestyle reels" (46 JPEGs) passed: index, faces, categories, tiles all work in the app.

## Required before the real Diu run
1. Discovered categories. The seven fixed categories miss subjects nobody predicted. Add: cluster the CLIP
   embeddings (k-means or HDBSCAN on the unit vectors), auto-name each cluster by zero-shot against a large
   label vocabulary (a few hundred scene/object nouns: boat, temple, market, food, sunset, dog, scooter,
   fort, palm tree, kids, drone shot, night, ...), show them as extra tiles ("discovered: boats 38").
2. Blurry and out-of-focus as categories. Two extra tiles driven by the existing sharpness score:
   "blurry" (bottom N% overall) and "out of focus" (faces present but eye sharpness low). Selectable like
   any other category.
3. Final export. One button: pick any set of categories (fixed + discovered + blurry/out of focus), it
   builds ONE folder `~/Desktop/photosort-out/<shoot>/final/` containing one subfolder per category.
   Mode selectable: links (default, disk is too big) or copies. RAW siblings follow their JPEG.
4. Keep the hard rule: the disk is read-only. `--apply-on-disk` exists but is never run unless asked.

## How to start the app (Terminal, not Finder, because of the Desktop permission)
    bash ~/Desktop/photosort/PhotoSort.app/Contents/MacOS/PhotoSort
If the page says "cannot be reached": `pkill -f "photosort.cli serve"` then run the line above again.
