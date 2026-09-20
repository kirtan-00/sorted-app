---
version: 1
name: sorted — the landing page in motion (frame layer, 1080×1920)
description: >
  The design system of kirtan-00.github.io/sorted at reel scale. Off-white paper with soft pastel
  blobs, generous pastel blocks with a 28px radius, Archivo semi-expanded 700 for the big lines,
  Source Sans 3 for everything else, and the dark Spectrum-style app as the one serious surface.
  Calm, confident, a little dry. Chill pastel page, serious dark tool.
unit: the frame — 1080×1920 (Instagram reel). Safe area x[70..930] y[240..1560].
principle: it has to look like the landing page, not like a template

colors:
  paper: "#f7f4ee"
  paper-2: "#fffdf8"
  ink: "#26282d"
  ink-2: "#4f545d"
  ink-3: "#7b818b"
  line: "rgba(38, 40, 45, 0.12)"
  mint: "#cfeee0"
  mint-d: "#2f8f6a"
  peach: "#ffd9c7"
  lav: "#ddd6f5"
  butter: "#fbefb8"
  sky: "#cfe4fb"
  blush: "#f3c8d0"
  blue: "#2680eb"
  app-g50: "#1d1d1d"
  app-g75: "#262626"
  app-g100: "#323232"
  app-g300: "#545454"
  app-g600: "#b3b3b3"
  app-g800: "#e6e6e6"

radii:
  block: "28px"
  app-frame: "12px"
  tile: "6px"
  pill: "999px"

typography:
  display:   { fontFamily: "Archivo", weight: 700, stretch: "112.5%", tracking: "-0.015em", lineHeight: 1.02, color: "ink", px: "88 to 112 on 1080" }
  display-sm:{ fontFamily: "Archivo", weight: 700, stretch: "112.5%", tracking: "-0.015em", lineHeight: 1.05, color: "ink", px: "64 to 76" }
  big-number:{ fontFamily: "Archivo", weight: 700, stretch: "112.5%", tracking: "-0.02em", lineHeight: 1, color: "ink", tabular: true, px: "120 to 160" }
  lede:      { fontFamily: "Source Sans 3", weight: 400, lineHeight: 1.4, color: "ink-2", px: "34 to 40" }
  body:      { fontFamily: "Source Sans 3", weight: 400, lineHeight: 1.45, color: "ink-2", px: "28 to 32" }
  label:     { fontFamily: "Source Sans 3", weight: 600, lineHeight: 1.2, color: "ink", px: "28 to 32" }
  small:     { fontFamily: "Source Sans 3", weight: 400, lineHeight: 1.3, color: "ink-3", px: "24" }
  mono:      { fontFamily: "SF Mono, Menlo, monospace", weight: 400, color: "ink-2", px: "24" }
  badge:     { fontFamily: "Source Sans 3", weight: 600, color: "ink", px: "26", upper: false }

spacing:
  gutter: "70px (the reel safe edge)"
  block-pad: "56px"
  stack: "24 / 40 / 64"

components:
  blobs:
    description: "Five soft radial pastel circles (sky, lav, mint, butter, mint) at ~0.85 opacity drifting slowly behind everything on the paper ground. Transform only. They are the page's atmosphere; every paper frame carries them."
  block:
    backgroundColor: "one of mint / peach / lav / butter / sky"
    rounded: "{radii.block}"
    padding: "{spacing.block-pad}"
    shadow: "none"
    description: "The landing page's pastel section card. One block per beat at most; the pink tile of the mark never sits on peach or blush."
  badge:
    backgroundColor: "{colors.mint}"
    textColor: "{colors.ink}"
    rounded: "{radii.pill}"
    description: "'public beta, testing right now' with a 7px mint-d dot and a 3px translucent ring before the text."
  cta:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper-2}"
    rounded: "{radii.pill}"
    description: "The one solid button (Join the beta / Download for Mac). Ghost variant: transparent with a 28% ink border."
  app-frame:
    backgroundColor: "{colors.app-g50}"
    rounded: "{radii.app-frame}"
    shadow: "0 0 0 1px rgba(38,40,45,0.18), 0 40px 90px rgba(38,40,45,0.22)"
    description: "A real capture of the app (or of the landing page's replica) in a rounded dark window with the page's soft shadow. Captures only, never a redrawn UI."
  tile:
    backgroundColor: "{colors.paper-2}"
    rounded: "{radii.tile}"
    padding: "4px"
    shadow: "0 10px 30px rgba(38,40,45,0.12)"
    description: "A loose photo in the pile: 3:2 photo on a 4px white mount, rotated a few degrees."
  folder:
    description: "Pastel folder (peach / mint / sky / lav): a rounded body with a tab, a flap that closes, a mono label below (people/Hari/, fishing boats/, drone/, interviews/)."
  bar:
    track: "rgba(38,40,45,0.10), 12px tall, pill"
    fill: "{colors.ink}, display:block so scaleX resolves"
    description: "The numbers block's bar: a label above, the value right-aligned beside the track, bold Source Sans 3."
  mark:
    description: "docs/brand/mark.svg on light, mark-dark.svg on the dark app. Wordmark only from wordmark.svg paths. The pulled pink tile stays out; it never settles back, never fills with a photo."
---

# sorted at reel scale

## The look

The reel is the landing page in motion. Paper ground with the five drifting pastel blobs; big
Archivo lines set tight; Source Sans 3 for anything that explains; pastel blocks with a 28px radius
for the sections that carry a colour (mint for post and the badge, sky for delivery, butter for the
numbers, peach and lav for folders); and the dark app, always a real capture, in a rounded window
with the page's soft shadow. Nothing glows, nothing is neon, nothing is a gradient except the
blobs.

## Type

- Big lines: Archivo, 700, semi-expanded (font-stretch 112.5%), letter-spacing -0.015em, line
  height 1.02. 88 to 112px on the 1080 frame. Sentence case, full stops.
- Everything else: Source Sans 3. 400 for copy, 600 for labels and buttons. Numbers tabular.
- Mono (SF Mono / Menlo) only for folder names and file names.
- No uppercase tracking games, no italics, no drop shadows on type.

## Colour

Paper #f7f4ee is the ground of every light frame; ink #26282d for type. Pastels are generous on
the page and rare inside the app. Blue #2680eb only inside the app (focus ring, Find, primary
buttons); it is never a headline colour. The mark's pink tile #f3c8d0 never sits on peach or blush.

## Motion

Calm and physical, like the page: tiles drift and settle, text reveals by line from a mask,
bars fill with power3.out, counts tick up. Nothing bounces. 0.5 to 0.8s moves, 1.2 to 2s holds.
Captures push in slowly (Ken Burns 1.0 to 1.06) rather than sit still.

## Safe area

Every word and the mark inside x[70..930] y[240..1560]. Blobs and blocks may bleed past it;
text never does.

## Font loading

```html
<style>
@font-face{font-family:"Archivo";font-weight:700;font-stretch:112.5%;font-style:normal;font-display:block;src:url("assets/fonts/Archivo-SemiExpanded-Bold-latin.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:400;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Regular.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:500;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Medium.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:600;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Semibold.woff2") format("woff2");}
</style>
```
