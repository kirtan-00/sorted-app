# The usage log

sorted keeps a log of what you did with it, on this Mac, so that when you test it we can see what you
tried, where you got stuck, what failed and how long things took. Nothing is sent anywhere. There is no
network code in the logger, the app has no analytics endpoint to talk to, and the report is a zip you
email us yourself. The promise on the front page (offline, no data leak) holds for the log too.

## Where it lives

    ~/Library/Application Support/photosort/usage/events.jsonl

One JSON object per line, one line per event:

    {"ts": "2026-09-20T13:06:53+05:30", "session": "5ae5f2931086", "ev": "search",
     "q_len": 21, "q": "fishing boats at dusk", "filters": {"category": "ocean"}, "total": 7, "ms": 65}

`ts` is local time, `session` is one id per app start, `ev` is the event name and the rest are that
event's facts. When the file passes 20 MB it is renamed `events.1.jsonl` (and the previous one
`events.2.jsonl`); anything older is dropped. Set `PHOTOSORT_HOME` to move the whole folder.

## What is logged

Facts, never content: durations, counts, sizes, which button, which tab, category names, error strings.

From the server:

- `server_start`: app version (the git short sha, or the package version in the .app), macOS version,
  chip, RAM, python and ffmpeg versions.
- `folder_open`: the shoot folder's name, how many items its index holds, whether it was indexed, whether
  it came from an imported bundle.
- `index_start`, `index_done`: items, photos, videos, faces on or off, failures, seconds, the error if
  the run died.
- `classify_done`: seconds, the count per fixed category, how many discovered groups, the error if any.
- `search`: the query's length and its first 60 characters (queries are how you describe what you want,
  and that is the point of the test), which filters were on, the result count, milliseconds. The grid's
  own reloads (an empty search) and "Show more" pages are not logged.
- `people_group` (groups, tightness, ms), `person_find_by_photo` (matches, faces in the reference),
  `person_find_saved` (matches), `person_save` (faces in the reference, name length), `people_merge`,
  `people_reject`, `rename` (what was renamed and the new name's length, never the name).
- `focus_run`: checked, bad, soft, seconds, error.
- `export_start`, `export_done`: what (selection, categories, people, bundle, or a Drive upload),
  destination (local or drive), files, failed, bytes, seconds, error. Category names ticked for an
  export are logged; people are counted, not named.
- `reorganise_plan`, `reorganise_apply`, `reorganise_undo`: moves, folders, collisions, seconds, a
  refused plan's guard message.
- `drive_signin`: ok, or the error string.
- `api_error`: every 4xx and 5xx the server returns, with the route template (for example
  `/api/people/references/{name}/find`, never the URL itself), the status and the message. A missing
  thumbnail is not logged.
- `exception`: an uncaught error in a request, as its last traceback line.
- `report_saved`: the size of a report zip you saved.

From the app window (prefixed `ui_`):

- `ui_tab` and `ui_tab_time` (which tab, seconds spent on the one just left), `ui_click` (the button's
  id, or its class when it has none), `ui_key` (keyboard shortcuts: command combinations, Esc, Enter,
  arrows, space and slash; ordinary typing is never logged), `ui_tile` (a category, discovered, drone
  or person tile), `ui_card` (a photo or clip card), `ui_filter` (a filter's new value; for a text
  field only how many characters, for the person and recent-folder pickers only set or cleared),
  `ui_help_open`, `ui_inspector`, `ui_panel` (a section folded or unfolded), `ui_unload`.
- `ui_error`: an error the page threw, with its message, file name and line.

## What is never logged

- Photo or video paths. The shoot folder's name is the only path segment that appears.
- People's names. Renames log the new name's length; the People tab's rows are logged by kind.
- Search text beyond its first 60 characters.
- Photos, thumbnails, frames, embeddings, or the index database.
- Anything typed into a text field other than its length (Esc and Enter in a field are logged as keys).

The logger scrubs every string it is handed: an absolute or home path becomes `<path>` and a quoted
span (the server quotes names and folder names in its error messages) becomes `'?'`. Error messages
from the server pass through that filter, so a "not a directory: /Volumes/SSD/shoot" reaches the log
as "not a directory: <path>". `tests/test_usage.py` drives a shoot with a distinctive photo name and a
saved person through the server and asserts neither reaches the log or the report.

## The summary

`GET /api/usage/summary` (and the Copy summary button) gives: sessions, first and last timestamp,
counts per event, the top 20 queries, the last 50 errors, total items indexed, seconds by feature (each
tab, indexing, categorising, focus check, exporting, reorganising, searching, grouping faces), and the
system line. `text` in the reply is the same as `summary.txt` in the report.

## How to send it

Open How it works (the button at the top right) and scroll to Feedback:

1. **Save report to Desktop** writes `~/Desktop/sorted-report-<date>.zip` and shows the path. The zip
   holds `usage/events*.jsonl`, `summary.json`, `summary.txt`, `system.json` and, when the app was
   started from the launcher, `~/Library/Logs/photosort.log` (the launcher's own log: start lines and
   any crash on startup; it can carry the path the app was launched from). No photos, no thumbnails, no
   index.db. Set `PHOTOSORT_REPORT_DIR` to write it somewhere else.
2. **Copy summary** puts the human-readable summary on the clipboard, for the body of the email.
3. **Email Kirtan** opens your mail app with the subject "sorted beta report". Attach the zip, paste the
   summary, send.

Or from a terminal: `curl -X POST http://127.0.0.1:7777/api/usage/report`.

To read it yourself: `tail -50 ~/Library/Application\ Support/photosort/usage/events.jsonl` (each line is
plain JSON), or `unzip -p ~/Desktop/sorted-report-*.zip summary.txt`.
