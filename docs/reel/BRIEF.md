---
workflow: product-launch-video
flow: automation
storyboard: no
message: "Plug in the disk. Search the whole shoot."
destination: instagram-reel
aspect: 1080x1920
language: en
audience: editors, wedding photographers, agencies and filmmakers with a pile of shoot data
length: 30s
angle: proof
style_preset: none
music: none
---

## Intent

The launch reel for sorted, a Mac app that indexes a whole shoot disk (photos and 4K video) on
the Mac and makes it searchable in plain words, with faces grouped into people and export straight
to a folder or Google Drive. Nothing leaves the Mac. The reel is the landing page in motion: the
same pastel blocks, the same dark app, the same type (Archivo semi-expanded 700 for the big lines,
Source Sans 3 for the small). Calm, confident, a little dry. On-screen text only, no voiceover.
Public beta, testing right now.

Story (the brag): a shoot arrives as a pile; type words, it finds; one face, every frame of that
person; photos AND video; the real M1 numbers (3,677 photos in 10 min 42 s; 955 items across
800 GB in 44 min 36 s; a 4K drone clip in 1.0 s); export or straight to Drive; nothing leaves the
Mac; public beta, testing right now; the URL kirtan-00.github.io/sorted. End card: mark +
wordmark + URL.

## Assets

- ../brand/mark.svg, ../brand/mark-dark.svg, ../brand/wordmark.svg, ../brand/wordmark-dark.svg — the brand kit; the end card and the corner lockups use these.
- ../../site/img/*.webp — the 51 Pexels photos the landing page uses (credits in site/img/credits.json). The only photos allowed in the reel.
- ../../site/fonts/SourceSans3-*.woff2 — Source Sans 3 files.
- capture/ — Playwright captures of the landing page (http://127.0.0.1:8123/) and of the real app running on the Pexels photos (scratch server on port 7792).

## Customizations

- Instagram reel safe area: every word and the mark inside x[70..930] y[240..1560] of the 1080x1920 frame.
- Fixed 30 fps.
- Count-ups and bar fills on the numbers beat, in the style of the landing page's butter block.
- Real captures only, no mockups; the landing page's pile animation and the app replica are the footage.

## Notes

- Voice rules: concrete first, second person, short, dry. No em dashes, no "--", no "AI-powered". Lines come from docs/sorted-concept.md "Lines already in use" and the real numbers.
- No client photos, ever. Pexels only.
- Brand: never animate the pulled tile back into place, tiles never fill with photos, the pink tile never on pink or peach, wordmark only from the SVG paths.
- Palette: ink #15171b, ground #f7f5ef, sky #bcd8f0, mint #bfe5cf, butter #f2e3a2, blush #f3c8d0, lilac #d6cbee, accent #2b5ce0. App dark #1d1d1d.
- Music: none. Not signed in to HeyGen, so the catalog is unavailable; a track can be added in the Instagram editor.
