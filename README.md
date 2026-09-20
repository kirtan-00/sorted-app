# sorted

sorted indexes a shoot (photos, RAW, videos) on your Mac and lets you search it in plain words,
find every photo of a person, split it into categories, flag the out-of-focus ones and export
the picks. Everything runs on the Mac, nothing is uploaded, and the shoot disk is never written to.

## Install

Once, about ten minutes, mostly downloading.

1. Get the folder: on the GitHub page click **Code**, then **Download ZIP**, and unzip it (it
   unpacks as `sorted-app-main`; leave it wherever you like, Desktop is fine). Or
   `git clone https://github.com/kirtan-00/sorted-app.git` if you have git.
2. Double-click **Install sorted.command** inside the folder. A Terminal window opens and shows
   what it is doing. Wait for the line `sorted is installed. There is a sorted icon on your
   Desktop; double-click it any time.`
3. Done. The installer opens sorted once for you at the end and leaves a **sorted** icon on your
   Desktop.

If macOS refuses to open the installer ("Apple could not verify", "from an unidentified
developer"): open **System Settings**, **Privacy & Security**, scroll down and click **Open
Anyway** next to the message about `Install sorted.command`, then double-click it again. That
happens once. If double-clicking opens the file in a text editor instead, right-click it,
**Open With**, **Terminal**.

The installer needs an Apple silicon Mac (M1 or newer) on macOS 14 or newer and an internet
connection. It fetches uv (a Python package manager) into `~/.local/bin`, Python 3.11 and the
libraries into `.venv` inside the folder, the search model into `~/.cache/huggingface`, and
ffmpeg if the Mac has none, and puts the `sorted` icon on your Desktop. It touches nothing else.
Run it again any time: a second run takes seconds and repairs whatever is missing.

## Start

Double-click the **sorted** icon on your Desktop. (Or `sorted.app` inside the folder; the Desktop
icon is a shortcut to it.) It starts a local server and opens Chrome at `http://127.0.0.1:7777`
(or the next free port). Close the browser tab when you are done; the server stops the next time
you start the app. Moved the folder? Run **Install sorted.command** again and the Desktop icon
points at the new place.

If the app cannot see your Desktop or an external disk when you pick a folder, macOS is blocking
it: **System Settings**, **Privacy & Security**, **Full Disk Access**, add the `sorted.app` you
double-click (drag the Desktop icon in), then start it again. Nothing here leaves the Mac; this
is only macOS asking whether the app may read the disk.

## First run

1. The app opens on a welcome screen with two buttons. **Scan a disk or folder**: pick the shoot
   (the disk or the folder with the day's cards) and the scan starts. **Load a scan file**: pick a
   `.photosort-index.zip` someone gave you and skip the scan. Recent shoots are listed under the
   buttons.
2. Leave **Detect faces** ticked unless you are in a hurry (untick it, scan, and run
   **Detect faces now** later). A few thousand photos take a while; the progress bar is honest.
3. Then:
   - **Search** in plain words ("sunset on the boat", "close-up of hands"). The **Sharp** slider
     hides the soft frames; the faces filter picks solo, two-person or group shots.
   - **People**: **Group faces** once, name the people you care about, or **Find a person from a
     photo** with any reference picture. Tick people and export them, one folder each.
   - **Categories**: **Categorise** once; every photo lands in a category (ocean, boat, people,
     interview, building, road, night, food, and so on) plus what the shoot itself suggests.
   - **Check focus** (top bar) marks the frames nothing is sharp in, so you can hide or skip them.
4. **Export** anything you have selected or ticked: copies, links (need the disk plugged in) or a
   CSV, full size or web size, to a folder on this Mac.
5. **Reorganise disk** (Scan tab) sorts the shoot folder itself into `sorted/` by category and
   person. It shows a **Preview** first, moves files on the same disk without copying, and
   **Undo** puts every file back where it was.
6. Exporting straight into a Google Drive folder works too, after a one-time sign-in:
   [docs/google-drive.md](docs/google-drive.md).

## Where things live

- The scan (index) and thumbnails: `~/Library/Application Support/photosort/<shoot>/`. Delete a shoot's
  folder there and it is gone from sorted; the photos are untouched.
- Exports: `~/Desktop/photosort-out/<shoot>/<selection>/`.
- The app log: `~/Library/Logs/photosort.log`. The installer's log: `install.log` in the folder.

## Privacy

- Fully offline. The models are on the Mac; no photo, name or search ever goes to a server.
- The shoot disk is read-only to sorted. The one exception is **Reorganise disk**, which moves
  files only after you confirm the preview and which keeps an undo record.
- No telemetry. The app keeps a usage log of what you clicked and how long things took, on this
  Mac only, so a bug report can say what happened. It goes nowhere unless you send it.

## Feedback

When something is wrong or slow: open **How it works** (top bar), scroll to the feedback panel
and click **Save report to Desktop**. That writes `sorted-report-<date>.zip` on your Desktop
with the usage log, the app log, a summary and your Mac's specs (no photos, no thumbnails), and
**Email Kirtan** next to it opens a mail to purohit.krick@gmail.com; attach the zip and add a
line about what you were doing. Screenshots help. If the app will not start at all, send
`install.log` from the folder and `~/Library/Logs/photosort.log` instead.

## Command line

The same engine is a CLI, for scripts and for when Terminal is faster:

```
.venv/bin/python -m photosort.cli index ~/Pictures/Shot_001
.venv/bin/python -m photosort.cli find ~/Pictures/Shot_001 "a person smiling outdoors" --sharp 85
.venv/bin/python -m photosort.cli people ~/Pictures/Shot_001 --export
.venv/bin/python -m photosort.cli classify ~/Pictures/Shot_001
.venv/bin/python -m photosort.cli reorganise ~/Pictures/Shot_001 --apply
.venv/bin/python -m photosort.cli serve --open
```

Add `-h` to any of them to list the flags. For development: `uv pip install -e ".[dev]"` adds
pytest; `.venv/bin/python -m pytest -q tests` runs the suite. Search is MobileCLIP-S1 zero-shot,
faces are YuNet + SFace with DBSCAN, categories are zero-shot plus k-means, all local, no LLM.
