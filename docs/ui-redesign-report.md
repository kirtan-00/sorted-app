# lightproof UI: Spectrum on a macOS frame

Branch `diu-scale`. Files: `photosort/ui/index.html`, `photosort/ui/style.css` (rewritten), `photosort/ui/app.js` (edits in commented `=====` blocks, listed below), `photosort/ui/SourceSans3-{Regular,Medium,Semibold}.woff2` plus `SourceSans3-LICENSE.md` (OFL), this report. `docs/ui-screens/` is git-ignored; the PNGs are written there by the harness and not committed.
Verified by rendering the live app (NSG_26_Part 2: 630 photos, 325 clips) with Playwright at 1100, 1280 and 1800. Zero page errors and zero console errors from the UI; the only 4xx logged are `/api/media/804` and `/api/media/806`, clips the live server cannot serve. `documentElement.scrollWidth` equals the viewport at every width, inspector open or closed.

## Skin: Adobe Spectrum dark on the macOS window frame
- Type: Source Sans 3 (bundled, `font-display: swap`, system fallback), 14 px body, 12 px small, 11 px detail, 16 px section titles at 600, 18 px shoot name, letter-spacing 0, tabular numerals on body. Mono only for file names (`.filename`, `.insp-name`) and the error log (`#index-errors-list`). `.mono`, which app.js stamps widely, now only sets tabular numerals.
- Colours: gray-50 #1d1d1d window, gray-75 #262626 sidebar and inspector, gray-100 #323232 toolbars, fields, sub-toolbars and action bars, gray-200 #3f3f3f control ground, gray-300 #545454 borders and section rules, gray-500 #909090 tertiary, gray-600 #b3b3b3 secondary, gray-800 #e6e6e6 primary, white only on the CTA. Accent blue-500 #2680eb, hover #378ef0, focus ring #4b9cf5 at 2 px. Red #e34850 for errors and the armed remove, green reserved.
- Controls: action button 28 px (compact, in toolbars) with a gray-300 border and transparent ground, hover gray-200; the CTA (Find, Export while N > 0) filled blue; picker = action button with a chevron; text field on gray-100 with a gray-300 border, focus border blue plus the 2 px ring; search field with magnifier and a circle-x clear; custom 14 px checkbox with a white tick on blue; Spectrum slider (2 px track, blue fill, 14 px handle with a gray-800 border) and its value in a 44 px number field that also drives the slider; the kind filter is an action group of three joined segments (radios named `kind` with `form="q"`).
- Frame: 44 px unified toolbar (wordmark, shoot name 18/600 over the counts line, Recent picker, Open folder, destination path with a blue Change link, inspector toggle), 200 px source-list sidebar (32 px rows, 18 px stroked SVG glyphs, selected on gray-200, FILTERS group label), 260 px inspector on the right (open by default above 1400 px, remembered in localStorage, slides 180 ms), 24 px status bar. No-folder state collapses to the centred prompt.
- Grid: Lightroom Library cells, 6 px gap, gray-75 cell with the thumb inset 4 px, hover gray-100, selected gray-300 with a 2 px blue border and a 20 px blue tick top-right, duration badge bottom-right, less-sure at 60 %, no captions.
- Inspector: Lightroom panels (Info, Categories, Faces) with 32 px disclosure headers; preview, file name, taken, camera, size, duration, sharpness bar, category with score, discovered group, aerial, faces. Hover wins, then a single selection, then "N items selected" with kind and category breakdown. Empty: "Select a photo or clip".
- Categories: two panels, table rows 28 px alternating, checkbox / name / count / 60 px bar / Export links on hover. `group N` clusters read "Unnamed group N" in secondary with a pencil that renames inline through `POST /api/categories/discovered/rename {old, new}`; errors land in the status bar in red, success warns that names are lost on re-Categorise.
- People: panels for Same person? (44 px crops, names 12/500), Find a person, Saved people, Face groups (52 px rows, name fields look like labels until hovered or focused).
- Index: Progress panel (readout, 4 px bar, error log in mono on gray-100) and the bundle panel.
- Lightbox: opaque black, 32 px close, 44 px info strip (file name mono, rest 12 px secondary), 56 px scene frames, arrows step through the results.
- Motion: 130 ms ease-out on ground, border and colour; the inspector column 180 ms; nothing else.

## Functional polish
Keyboard: `/` or command-F focuses the search field from anywhere (switches to Search); Enter runs it; Esc clears the field when it is focused (re-running the search), closes the lightbox otherwise; left and right arrows step the lightbox; space toggles the hovered or focused card; command-A selects all shown; arrow keys move a focus ring across the grid (roving tabindex, column count read from the grid), Enter opens the focused card. Clicks: plain click toggles (unchanged), shift-click selects the range from the last clicked card, command-click toggles. Every button and field carries a `title`. Panels collapse on header click, remembered per panel. Status bar messages that start with "could not", "failed", "error" or contain "failed:" turn red.

## Hooks preserved
Every id and class app.js reads or writes from the previous round, plus the new ones: ids `inspector`, `insp-body`, `inspector-toggle`, `kind-select` (now the fieldset), `nav-cat-count`, `merge-*`; classes `panel`, `panel-head`, `panel-body`, `collapsed`, `cat-tools`, `cat-rename-btn`, `cat-rename`, `cat-export`, `cat-bar`, `cat-name.unnamed`, `insp-*`, `filename`, `err` (on `#status`), `has-sel` (on `#selbar`), `renaming` (on `.cat-tile`). Structural: `header nav button` (only the four view buttons are buttons inside `<nav>`), `main > section`, `#q input[name=q]`, `#q [name=sharp]`, `#q button[type=submit]`, `.badge:not(.drone)` for video cards.

## app.js blocks touched (line numbers at commit)
- 33 to 35 status colour (`.err` on `#status`).
- 79 to 88 title block: "630 photos · 325 videos · 606 faces", index time in the tooltip.
- 221 to 224 destination label: path only, sentence in the tooltip.
- 277 to 292 slider value fields (`bindSliderField`, the `--p` fill); 343 the select listener also binds radios; 1120 and 1127 the find-sim field uses the same binding.
- 373 to 413 grid selection model: `resultById`, `clickCard` (shift range, command toggle), hover tracking for the inspector, roving tabindex, `moveFocus`; 443 to 444 no caption node, card tabindex; 458 click handler takes the event; 496 `updateSelbar` renders the inspector.
- 625 to 632 lightbox strip: file name span.
- 650 to 695 keyboard (replaces the old Escape-only handler); `stepLightbox`.
- 706 to 739 panels: collapse state per `data-panel` in localStorage, `makePanel` for the inspector.
- 741 to 819 inspector: `renderInspector` into Info / Categories / Faces panels, `setInspector` and the toolbar toggle.
- 964 to 1072 same person? strip (previous round); 1015 and 1017 tooltips.
- 1274 tooltip on Show.
- 1404 to 1473 table row: name, count, bar, the `.cat-tools` slot, the rename pencil and `startRename`.
- Previous round, unchanged: `loadCategories()` at boot and after a folder switch, `has-sel`, `--p` on `#progress`, `#nav-cat-count`.

## Screens (`docs/ui-screens/`, local only, each at 1100, 1280 and 1800)
01 search on load, 02 results, 02b zero results, 03 less-sure divider, 04 three selected with the inspector open, 04b hover, 04c field focus, 04d one selected with the inspector, 04e grid focus ring after an arrow key, 05 lightbox photo, 05b after a right arrow, 06 lightbox video with scenes, 07 categories, 07b rename field open (DOM mock), 08 category chip, 09 people, 09b people rows, saved people, face groups, save popover (DOM mock), 09c same-person strip (DOM mock), 10 index, 10b progress and errors (DOM mock), 11 no-folder.
Harness: `/private/tmp/claude-501/-Users-purohit/07e13950-ce1f-4811-bd28-ade4666e2d06/scratchpad/psui/ps_shoot2.py` (scratch). Allowed interactions only; the inspector toggle is clicked at 1100 and 1280 for the inspector shots and restored.

## Known gaps
- The woff2 files sit flat in `photosort/ui/` because `server.py` serves `/ui/{name}` as one segment; moving them into `photosort/ui/fonts/` needs `/ui/{path:path}` in server.py (not my file).
- At 1100 with the inspector forced open the main area is 640 px and the search sub-toolbar and selection bar wrap to two rows rather than clipping; the default there is closed.
- The lightbox video element fills the stage, so a clip the server cannot serve shows an empty player (the live 404s on `/api/media/804` and `806`).
- Rename and merge were exercised only through DOM mocks; the endpoints are live and the request bodies follow the server models.
- `tests/test_server.py::test_api` still passes through the `application-name` meta; the assertion should say "lightproof".
- `#stats` keeps app.js's "N people" segment when people exist and drops the index timestamp into the tooltip.

## Round 3: How it works, focus filters, optional faces, Reorganise disk (commit 8409295)
Same skin, same files. Verified against the live app at 1280, plus 1100 and 1800 for the help panel and the Index tab; zero page errors, zero console errors, no 4xx at any width, `scrollWidth` equals the viewport. The live server on 7777 predates the focus and reorganise endpoints (`/api/focus/status` and `/api/reorganise/status` 404 there, `/api/stats` has no `focus` or `faces_pending`), so every new state in the screens is a DOM mock and the flows were exercised end to end with Playwright routes standing in for the server (`psui/functest3.py` in the scratchpad: plan 400 and 200, confirm gating, apply, the moving poll, status with Undo, the restoring poll, Check focus and its poll, hide boxes on the query string, Detect faces now). No POST reached the wire.

### How it works
`#help-toggle` (a `.tb-icon.tb-help` with a question-in-circle glyph and the label, `aria-expanded`, `aria-controls`) sits left of the inspector toggle. `#help` is a body-level `aside[role=dialog]` fixed under the toolbar, 420 px, gray-75, 1 px gray-300 left rule, 180 ms slide-in, z-index between the save-person popover and the lightbox. Six ordinary `.panel[data-panel=help-*]` sections (open by default, collapse state remembered like every other panel), Source Sans 14 px in gray-700, copy in the owner's voice. Close × top-right, Esc, and any click outside close it; focus lands on the close button on open and returns to the toggle on close.

### Focus filters
Two `label.check.nav-row` boxes under the kind control, `name=hide_bad` and `name=hide_soft` with `form="q"`, so `currentFilters()` picks them up like `aerial`; a change re-runs the search. Under them `.focus-line`: `#focus-status` (12 px gray-500, "412 of 955 checked" or "focus not checked", the bad and soft counts in its tooltip) and `#check-focus`, a `.link.small`. Check focus posts `/api/focus`, polls `/api/focus/progress` every 800 ms into the status bar as "checking focus 120 of 955" (the total counts photos and clips; photos are instant), then refreshes the line, the stats and the grid. The link hides once every row is checked. The inspector Info panel shows a Focus row (ok, soft, or "bad, out of focus") when the result carries the label.

### Optional faces
`#faces` is now "Detect faces (people tab)" and still goes out as `{faces}`. `/api/stats.faces_pending` feeds "606 need faces" in the title subtitle, `#detect-faces-now` beside the Index button (hidden while indexing) and `#faces-pending-row` at the top of the People tab ("606 photos not scanned for faces yet, scan now"). Both post `/api/index {faces: true}` through the same `startIndexJob` the Index button uses and switch to the Index tab for the progress.

### Reorganise disk
`#reorg-block`, a `.panel.collapsed` at the foot of the Index tab with a red (`.panel-head.danger`) header. Body: the two-line warning in gray-600, `#reorg-by-people`, `#reorg-ack` (enables `#reorg-preview`), `#reorg-guard` in red-500 next to Preview for a 400 or 409 message verbatim. A plan renders `#reorg-summary` ("Will move 955 files into 14 folders: 630 photos, 325 videos, 0 drone; people: Meera 212, Arjun 48; 0 name collisions", the people segment only when the plan has any), the 20-row sample as a table (from → to, file names in the mono face, the reason in the tooltip, an "(also …)" suffix for a photo of several named people), `#reorg-confirm` with the shoot name as placeholder and `#reorg-apply`, a filled red `.btn.danger` enabled only while the field equals the shoot name exactly (Enter in the field does nothing). Changing the by-people box drops the plan. Apply posts `{plan_id, confirm}` and polls `/api/export/progress` through `pollDiskJob`, which shares the export poller's timer so two pollers never overlap: "moving 240 of 955", then "moved 955 of 955 files into <root>/sorted"; on completion the folder info, stats, categories, reorganise status and the search are reloaded. `GET /api/reorganise/status` with `reorganised: true` swaps the setup for "reorganised on <date>, 955 files" plus `#reorg-undo`, which posts `/api/reorganise/undo` and polls the same way as "restoring 240 of 955". A folder switch resets the section.

### Capability gate
`loadStats` sets `state.caps.focus` when `/api/stats` carries `focus`; the focus line and the reorganise status are only fetched when it does, so an older server gets no 404s and the line keeps "focus not checked". After a restart of the live server both reads go out on every stats refresh (boot, folder switch, after an index or a focus pass).

### Hooks
Unchanged except: `header nav button` narrowed to `header nav button[data-view]` in both places (the sidebar now holds a `.link` button that must not switch views); `applyPanelState` honours a `collapsed` class in the HTML as the default (existing panels carry none, so nothing changes for them); the Index button's request goes through `startIndexJob(body, msg)`. New ids: `help`, `help-toggle`, `help-close`, `hide-bad`, `hide-soft`, `focus-status`, `check-focus`, `detect-faces-now`, `faces-pending-row`, `faces-pending-text`, `faces-pending-scan`, `reorg-block`, `reorg-setup`, `reorg-by-people`, `reorg-ack`, `reorg-preview`, `reorg-guard`, `reorg-plan`, `reorg-summary`, `reorg-sample`, `reorg-confirm`, `reorg-apply`, `reorg-status`, `reorg-status-text`, `reorg-undo`.

### app.js blocks touched (line numbers at 8409295)
- 23 to 26 `state.caps` and `state.facesPending`.
- 62 to 66 and 77 nav selector `button[data-view]`.
- 90 title block: "N need faces"; 96 to 101 after stats: capability, faces controls, focus line, reorganise status.
- 173 to 178 per-folder reset in `settleFolder`.
- 331 to 334 `currentFilters()` hide_bad / hide_soft; 369 to 371 change listeners.
- 695 to 697 keyboard: Esc closes the help panel.
- 740 to 742 panel default from the `collapsed` class.
- 832 to 834 inspector Focus row.
- 1655 to 1677 `startIndexJob`, the Index button through it.
- 1727 to 1746 help panel; 1748 to 1795 focus; 1797 to 1815 optional faces; 1817 to 1955 reorganise disk (`pollDiskJob`, `afterDiskJob`).

### Screens (`docs/ui-screens/`, local only)
12 help panel over results (1100, 1280, 1800), 12b scrolled to the last section, 12c one section collapsed; 13 focus boxes ticked with "412 of 955 checked", 13b "checking focus 120 of 955", 13c inspector Focus row (the search response was given labels on the way in, so the real render path drew it); 14 Reorganise disk opened, 14b guard message, 14c plan with the sample table, 14d confirm typed and the red button armed, 14e "moving 240 of 955", 14f reorganised status with Undo, 14g "restoring 240 of 955" (14 to 14f also at 1100 and 1800); 15 Index tab with Detect faces now and "606 need faces" in the title, 15b People tab with the scan link. Harness: `psui/ps_shoot3.py` next to `ps_shoot2.py` in the scratchpad; live interactions were Find, tab clicks, the help toggle, panel headers, one card click, the inspector toggle.

### Known gaps
- A guard 400 from Preview logs Chromium's own "Failed to load resource: 400" line in the console; the message itself is shown in the section. Same for any 4xx the browser sees.
- The "checking focus" total counts every unchecked row, photos included, so it reads "120 of 955" rather than the brief's "of 325 clips"; photos finish in the first tick.
- The faces-only index pass ends with the generic "indexing finished" from `pollProgress`.
- The plan sample shows 20 rows of up to 955; per-file failure reasons after an apply are not on the job (the report offered `state["export"]["failures"]`; the status bar shows the count).
- Apply and undo share the export poller; an export started while a reorganise runs is refused by the server (409) anyway.

## Round 4: navigation, "there is no back"
Same skin, same files plus a section in the People and Categories rows. Verified against the live app (01 Photos: 3,677 photos, 446 face groups, 36 categories) at 1280 and 1100 with Playwright; 106 of 106 checks passed, zero page errors, zero console errors, no 4xx, `scrollWidth` equals the viewport with the context bar up. Live interactions: tab clicks, a face-group row (its count, never the name field, whose blur saves), a category row, a discovered row, the back button, Clear filter, a Find, the "anyone" picker, Esc, command-1 to command-4, browser back and forward, a reload on `#search?person=126` and on `#categories`. The saved-people rows and the "Meera" find were served by Playwright routes (`/api/people/references` and its `/find`), so no POST reached the wire.

### Context bar
`#ctxbar` sits under the search sub-toolbar inside `.search-head`, a sticky wrapper that holds both (the sub-toolbar itself is `position: static` there), 36 px on gray-100 with the hairline below; the sub-toolbar's own hairline separates the two rows. Left: `#ctx-back`, a chevron-left glyph and the origin tab's name, tooltip "back to People (Esc)". Then `#ctx-crumb`, 13 px gray-600 segments with "/" separators in gray-500 and the last segment gray-800 at 500: "People / Dhaval owner / 760 photos", "Categories / beach / 777", "Categories / discovered / fishing boats / 194", "People / Meera / 212 photos" for a saved person. Right: `#ctx-clear`, a `.link` "Clear filter" that drops the filter and stays on Search. While the bar is up `#view-search.has-ctx` hides `#category-chip`, since the crumb says the same thing with one clear affordance; `showCategoryChip` still sets the chip's text (the drone box handler reads it). The bar shows only for a filter that came from another tab: a person picked straight from the "anyone" picker gets no bar and no history entry.

With a text query the server ranks the whole filtered set rather than narrowing it (`category=beach` and `category=beach&q=sunset` both return total 807), so the crumb's tail reads from the search's own numbers: "Dhaval owner · 'sunset' · 200 of 760 shown". "More like this" reads "· similar shots ·". The row count in the crumb is the origin list's (777 on the beach tile, where the search total is 807: the tile counts the rows filed under beach plus the people photos whose scene guess is beach, and the search adds the "other" rows whose best guess was beach, as less sure).

### History
`state.ctx` is `null` or `{from, kind, key}` (`person`, `saved`, `category`, `cluster`, `drone`). `setCtx` writes the form (person select, the two hidden category fields, the drone box, the chip) and renders the bar; `jumpTo(ctx, run)` is the one entry point for the rows and pushes `{view, ctx, root, backable}` with a hash: `#people`, `#categories`, `#index`, `#search`, `#search?person=126`, `#search?category=beach`, `#search?cluster=fishing%20boats`, `#search?aerial=1`, `#search?saved=Meera`. Tab clicks, command-1 to command-4, `/` and "scan now" go through `goView` (showView plus a push); a push is skipped when the entry equals `history.state`, so clicking the active tab twice adds nothing. `popstate` restores the entry (`restoreEntry`): the context, the view, and a re-run of the grid whenever the filter changed even when the landing view is not Search, so a hidden grid is never stale; a saved-person find owed to a hidden Search runs when that tab next shows. An entry from another shoot (`root` differs) restores its view only. Boot prefers `history.state` (it survives a reload) over the hash; a person filter at boot goes out as an explicit `runSearch({person})` because the select has no options yet, and `fillPersonSelect` sets the select once they exist. `settleFolder` clears the context and replaces the entry. The no-folder screen and the lightbox never push.

The Back button and Esc call `navBack`: when the current entry was pushed from its origin tab (`backable`) it is `history.back()`, so Forward brings the filtered grid back; otherwise (a reload, a pasted URL) the filter is cleared and the origin tab is pushed. The "anyone" picker moves a People context to the picked person or ends it on "anyone"; unticking Drone shots by hand ends a drone context; a plain search drops a saved-person context, since a saved find is not a form filter.

### Keyboard, sidebar, rows, scroll
Esc order: lightbox, help panel, the focused query field (clear, then blur), then a filtered Search goes back; nothing while typing elsewhere. Command-1 to command-4 switch tabs from anywhere, fields included. The origin tab's sidebar row carries `.from`: a 6 px blue-500 dot at the right edge (the Categories count loses its auto margin so the dot stays outermost); the dot is dropped while that tab is the active one, since there is nothing to point back to from there. Face-group rows and category rows get a chevron-right `::after` (a masked data-URI glyph in gray-500, gray-800 on hover) and a hover ground on gray-100; the row's name field keeps its own click. Saved-people rows: the whole row navigates except the tick, the name field and the remove button; Show is now the chevron (`.ref-show`, last on the row) and the remove is `.ref-remove`. Category rows: the chevron and any bare part of the row open the category too, the label button as before. `.ref-row .ref-name` now outranks `input[type="text"]`, so that field reads as a label until hovered, as the round-2 note intended. `main` is the one scroller; `showView` saves `main.scrollTop` per view on leaving and restores it on return, with a one-shot reapply in `renderPeople` and `renderCategoryTiles` because the tab's own reload rebuilds those lists.

### Hooks
Unchanged except: `#q` and the new `#ctxbar` sit inside `div.search-head`; the saved-row buttons carry `.ref-show` and `.ref-remove` (the CSS that keyed on `:last-child` now keys on `.ref-remove`). New ids: `ctxbar`, `ctx-back`, `ctx-back-label`, `ctx-crumb`, `ctx-clear`. New classes: `search-head`, `ctxbar`, `ctx-back`, `ctx-crumb`, `crumb-seg`, `crumb-sep`, `has-ctx` (on `#view-search`), `from` (on a nav button), `row-chev`, `ref-show`, `ref-remove`.

### app.js blocks touched (line numbers at 03b74cc)
- 27 to 30 `state.ctx`, `state.scroll`.
- 64 to 78 scroll memory (`mainEl`, `restoreScroll`, `settleScroll`, the hand-scroll listener that ends a pending restore); 80 to 83 and 96 to 98 in `showView`; 99 to 101 the owed saved find; 107 nav buttons through `goView`.
- 110 to 296 navigation: `ctxParams`, `applyCtxFilters`, `setCtx`, `renderCtxBar`, `hashFor`, `parseHash`, `navPush`, `goView`, `jumpTo`, `navBack`, `clearCtx`, `restoreEntry`, `popstate`, `navBootEntry`, the two bar buttons.
- 396 to 399, 407 and 418 `settleFolder`: context reset, replace entry.
- 563 to 565 `runSearch` drops a saved context; 602 to 604 the drone box; 607 to 614 the person picker.
- 736 `renderGrid` re-renders the crumb.
- 940 to 947 keyboard: command-digits, Esc back; 951 `/` through `goView`.
- 1140 the name field's tooltip; 1163 to 1172 `renderPeople`: row click through `jumpTo`, scroll settle; 1210 to 1213 `fillPersonSelect`.
- 1479 to 1490 `findSaved` and `jumpToSaved`; 1568 to 1611 `renderSavedPeople` rows.
- 1648 to 1663 chip clear, `filterByCategory`, `filterByDrone` through `jumpTo`; 1736 to 1741 `makeTile` row click; 1807 to 1810 `renderCategoryTiles`.
- 2089 `scanFacesNow` through `goView`; 2247 to 2269 boot.

### Screens (`docs/ui-screens/`, local only, 1280 and 1100)
16 People tab, 16b after a face-group row (the context bar, the dot on People), 16c the crumb with a query, 16d after back (the list where it was, the rows with chevrons), 16e a reload on `#search?person=126`, 16f a hovered row; 17 Categories rows, 17b beach (the bar, the chip hidden, the dot on Categories), 17c after back, 17d a discovered row, 17e after Clear filter, 17f a hovered row; 18 saved-people rows (routed), 18b the "People / Meera / 212 photos" crumb, 18c a hovered saved row. Harness: `psui/ps_shoot4.py` and `psui/func4.py` in the scratchpad.

### Known gaps
- Command-1 to command-4 call `preventDefault`; a headless run cannot tell whether a real browser hands those keys to the page (Chrome's tab switching, Safari's bookmarks). If the browser keeps them, the sidebar still works; press one once to know.
- One context at a time: a People row now clears a category, cluster or drone filter and a Categories row clears the person picker. Before this round a person click stacked on a category filter and left the drone box alone.
- Pre-existing, not from this round (reproduced at 2794371): a Same or Different answer scrolls the People list to the top. Removing the row alone shifts it by 72 px through scroll anchoring; the jump to 0 comes later in that flow and was not chased here.
- The "N of M" narrowing count in the brief does not exist server-side: a text query ranks within the filter, so the crumb shows shown-of-total instead.
- History entries from before a folder switch stay in the browser's list; walking back into one restores its view only.
- A person picked from the "anyone" picker with no context, then a tab switch and a browser back, comes back to Search with the picker cleared: the entry describes the filter state, and that pick was never one.
