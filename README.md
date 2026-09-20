# PhotoSort

Local offline tool to index, search, and organize photos with face clustering and sharpness scoring.

## Setup

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
scripts/fetch_models.sh
```

## Launching

Double-click `PhotoSort.app` in Finder. It starts the local server and opens the browser at http://localhost:7777 with no folder open yet; use the "Open folder" button in the app to pick one (native macOS folder picker) or choose from your last 8 folders in the recent dropdown. You can switch to a different folder at any time without restarting the app.

## Commands

**Index a folder and extract face embeddings:**
```
python -m photosort.cli index ~/Pictures/Shot_001
python -m photosort.cli index ~/Pictures/Shot_001 --no-faces  # skip face detection (a later run with faces picks them up)
python -m photosort.cli index ~/Pictures/Shot_001 --retry-errors  # re-try photos that failed to decode last time
```

**Search photos by caption (MobileCLIP zero-shot):**
```
python -m photosort.cli find ~/Pictures/Shot_001 "a person smiling outdoors"
python -m photosort.cli find ~/Pictures/Shot_001 "close-up" --sharp 85 --limit 20
```

**Cluster faces and organize into person/group/solo folders:**
```
python -m photosort.cli people ~/Pictures/Shot_001
python -m photosort.cli people ~/Pictures/Shot_001 --eps 0.3  # stricter clustering
```

**Start the local web UI:**
```
python -m photosort.cli serve ~/Pictures/Shot_001 --open
python -m photosort.cli serve --open   # no folder yet; pick one from the app
```

**Run benchmarks:**
```
python -m photosort.cli bench ~/Pictures/Shot_001 --n 200
```

## Categories

The Categories tab (in the web UI) runs zero-shot scene classification over an indexed folder: ocean, boat, beach, people, interview, building, office, road, night, food, sky, birds-animals, or other. Click "Categorise" to run it, then click a category tile to jump to Search filtered on that category, or "Export links" to symlink every photo in that category into the export folder (no copying, safe for a big read-only shoot). This never writes to the source folder unless you separately opt into `photosort.cli classify --apply-on-disk`.

The same pass also fills a second row, "Discovered in this shoot": k-means over the shoot's CLIP embeddings (photos and videos alike), each cluster named by the closest of a few hundred plain labels in `photosort/vocab.py` ("excavator 138", "havan fire 61"). Local and deterministic, no LLM; it needs at least 16 embedded photos. Discovered tiles filter, tick and export like the fixed ones, landing under `categories/discovered/<name>/`.

Every category view puts the confident matches first and, below a "less sure" divider, the ones the model is under 50% sure about, sorted by confidence (a photo filed under "other" whose best guess was building shows up under building there). Exports take only the sure ones unless "include less sure" is ticked. Face find does the same with a band just below the match slider.

## Storage & Exports

The index and thumbnails live in `~/Library/Application Support/photosort/<shoot-slug>/`. Your source folder is never modified.

Exports (by default) copy files to `~/Desktop/photosort-out/<shoot>/<selection-name>/`. Use `--mode symlink` to create symlinks instead, or `--mode csv` to write a manifest.

## Notes

Sharpness is measured on the subject: if a face exists, it's scored on the eye region; otherwise, on the sharpest tiles in the frame. Shallow depth-of-field portraits won't be flagged as blurry.

Face clustering uses YuNet (detection) + SFace (embeddings) with DBSCAN. Tune `--eps` on your own shoot: lower values mean stricter clustering (fewer false matches). Start at 0.3-0.5 and adjust.

Search uses MobileCLIP-S1 (zero-shot, no LLM, fully local).

## If PhotoSort.app does not open

macOS blocks apps launched from Finder from reading the Desktop (and external disks) until you allow it.
Either: System Settings > Privacy & Security > Full Disk Access > add PhotoSort.app (drag it in), then relaunch.
Or run it from Terminal, which already has that access:

    bash ~/Desktop/photosort/PhotoSort.app/Contents/MacOS/PhotoSort

The launcher logs to ~/Library/Logs/photosort.log.
