# lightproof landing: report

Date: 2026-09-19. Branch diu-scale. Deliverable in /Users/purohit/Desktop/photosort/site/ (index.html, style.css, main.js). No build step, no framework. GSAP 3.13 + ScrollTrigger from cdnjs; smooth scroll is hand rolled because Lenis is not on cdnjs (404, library not found). Fonts from fonts.googleapis.com: JetBrains Mono (wordmark, UI, data) and Newsreader (headlines, body, italic tagline), with system fallbacks.

## What was built

A darkroom. Warm black ground (#0c0a09), fibre paper type (#ece6da), one safelight red (#ff4a3a, 5.8:1 on the ground, used for marks, the cursor, the vows and small mono file names). The wordmark is the masthead: "light" solid, "proof" at 70 % ink, in JetBrains Mono at up to 12.4 rem. Every illustration is the product rebuilt in CSS and SVG: the app window, abstract sepia tiles with a tiled SVG grain, a category bar with the less sure band, a person finder, a clip timeline with segment labels, a folder tree, a disk and a laptop. No photographs, no client imagery. Copy uses only the claims in the brief; numbers are verbatim. No pricing, logos, testimonials, counts or dates. No em dashes, no double hyphen anywhere in site/ or in the two .superpowers files (grep verified, including CSS custom properties, HTML comments and decrements, none used).

## Sections, top to bottom

1. Hero: fixed header (wordmark fades in after the masthead scrolls off, drawn underline nav, magnetic early access button); masthead wordmark; italic tagline on three forced lines; lede; two CTAs (mailto with subject "lightproof early access", GitHub); the mock app that types "woman in a sari", "fishing boats", "balcony", "dining table" in a loop and filters 24 tiles in place (matches develop with a red hairline and a match percentage, less sure matches at half ink, the rest go unexposed; the chip row reports "showing 6 of 955, 1 less sure").
2. One real run: a three row ledger (3,677 photos of 33 MB in about 11 minutes; 955 items, 630 photos and 325 clips, 750 GB, zero errors; 0 bytes written) and a note on formats and the model.
3. Four things happen (pinned, scrubbed, the only numbered sequence): plug in (cable draws into a laptop, lock appears, mounted read-only line), index (progress bar, live counter 0 to 3,677 with an ETA derived from the real 11 minutes, log lines), search ("fishing boats" types out, four of twelve tiles light), export (folder tree draws, free space check line). A red rail fills alongside the four steps and the active step goes to full ink.
4. Ask for it the way you would ask the runner: copy on the less sure band, six example query chips, the categories mock (fixed set, discovered in this shoot, less sure under 50 %).
5. Four more ways in: person from one photo, scene breakdown timeline (cursor says "play"), drone tick, sharpness on the subject. Asymmetric 7/5 and 5/7 grid, cards tilt toward the pointer and reveal one detail line.
6. Hand the editor a folder: two paragraphs (per category folders, links or copies, free space check; the index bundle) and an export tree mock.
7. The word: "A changing bag is lightproof so that nothing gets in. This is lightproof the other way round." then No cloud. No account. No upload. in safelight red, then the byte for byte line and the privacy paragraph.
8. The small print: a mono datasheet (reads, disk, model, categories, Sony cards, export, network).
9. Early access CTA and footer.

## Motion inventory

- Smooth scroll: wheel hijacked, lerped onto the native scroll position (no wrapper transform, so pin, anchors and fixed elements keep working); resyncs when anything else scrolls the page (keyboard, scrollbar, Playwright). Off on coarse pointers and reduced motion. Verified: one wheel of 600 px lands 311, 477, 600 over the following frames.
- Load moment: wordmark halves rise out of masks, tagline lines stagger, lede lines, CTAs fade, the app window unmasks from the top.
- Scroll reveals: headlines and paragraphs split into real lines at runtime (forced breaks honoured, re-split on resize) and rise out of line masks; mocks unmask with a clip-path; ledger rows and datasheet rows stagger.
- Pinned scrub: #flow-pin pinned for 320 % of the viewport, scrub 0.7, four stages crossfade with sub animations (stroke draw, width, counter text, staggered lines). Four frames recorded at 8 %, 35 %, 62 % and 90 % on both viewports.
- Hover: magnetic buttons (pull inside a 28 px halo, elastic release; verified transform matrix(1,0,0,1,26.3,-6.5) near the edge, back to 0 on release), drawn underline nav links (scale in from the left, out to the right), tilt cards with a detail line, chips lift.
- Cursor: dot plus lagging ring, pointer-events none, grows red over links, becomes a paper disc with a label over [data-cursor] targets ("search" on the demo, "play" on the clip card). Enabled only for pointer: fine plus hover: hover above 720 px and with motion allowed; hidden by CSS on pointer: coarse and reduced motion.
- Video card playhead crawls while the card is on screen.
- Reduced motion: html.reduce, native scroll, no cursor, no tilt, no smooth scroll, reveals are opacity fades via IntersectionObserver, the demo sits in its searched state, the flow section shows all four stages stacked in their finished state.
- JS off: everything visible top to bottom, flow stages stacked, demo shows the empty query with the placeholder.

## Screenshots (docs/site-screens/)

Desktop 1440x900: desk-00-load (mid intro), desk-01-top (cursor labelled "search" over the demo), desk-02-run, desk-pin-01 to desk-pin-04 (scrub proof), desk-10-ask, desk-11-ways (video card tilted, "play" cursor), desk-12-export, desk-13-word, desk-14-sheet, desk-15-end, desk-20-bottom, desk-js-off-top, desk-js-off-full (full page, JS disabled), desk-reduced-motion-top, desk-reduced-motion-flow.
Phone 390x844: phone-00-load, phone-01-top, phone-02-run, phone-pin-01 to phone-pin-04, phone-10-ask, phone-11-ways, phone-12-export, phone-13-word, phone-14-sheet, phone-15-end, phone-20-bottom.

## Console

page.on('console') and page.on('pageerror') registered on every run: clean at 1440, at 390, with reduced motion, and with JS off. document.documentElement.scrollWidth is 1440 at 1440 and 390 at 390 (no horizontal scroll).

## Fixed while looking

- Line reveals never landed: GSAP read the CSS translateY(105%) as a pixel offset and kept it. Now y is zeroed before yPercent animates.
- Section gaps were 360 px; halved.
- Hero did not fit the 900 px fold; demo tiles went 3:2, deck margin tightened. Whole window now sits above the fold at 1440x900.
- Tagline widowed "room." onto a fourth line; the splitter now honours br.
- Phone pinned section collapsed to 0 px wide (align-items: center inherited from the desktop grid into the column flex); stretched.
- Timeline segment labels overlapped at phone width; segments clip.
- Plug stage: the tiny connector became a laptop outline.
- Wrap and hairlines now align with the header at 64 px.

## Known gaps

- Lenis is not on cdnjs, so the smooth scroll is hand rolled (wheel only; trackpad momentum comes through as wheel deltas, touch is native).
- The mock category counts, folder counts, match percentages and the index log lines are illustrative UI values, not claims; the four headline numbers (3,677 / 33 MB / 11 min / M1 8 GB and 955 / 630 + 325 / 750 GB / zero errors) are the real run. No search latency is claimed anywhere.
- .superpowers/ is excluded in .git/info/exclude on this checkout; the two files were added with git add -f as the brief asked.
- Hover and cursor states cannot be seen in a static screenshot beyond the two captured; the magnetic pull and wheel lerp were verified numerically instead.
- Not tested in Safari (Playwright chromium only). clip-path inset and aspect-ratio are fine in Safari 15+.
