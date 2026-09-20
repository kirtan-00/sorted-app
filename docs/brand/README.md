# sorted brand kit

The mark is a 2 by 2 contact sheet with one frame pulled. Three settled tiles in ink, one tile lifting out in blush, turned twelve degrees. That is the select: the shot you were looking for, found and picked.

## Why this one (8 lines)

1. It is the product in one shape: a grid of shots, and the one you wanted lifting out. No eye, no lens, no folder, nothing another culling app already owns.
2. It is built on a 16 pixel grid (tiles 24 on an 8 gap, from 4) so the three settled tiles land on whole pixels at 16 px. The pixel test on proof.png shows it: hard edges in the menu bar, the favicon and the toolbar, where the mark lives all day.
3. The pulled tile is the only colour, so the mark carries the pastel programme on any ground and still reads in one colour when the tile goes ink (mark-mono).
4. Never use it with: a drop shadow, an outline, a gradient inside the tiles, more than one tile lifted, a second tile in a second colour, an orange or terracotta tile, or the pulled tile animated back into place (it becomes a loader).
5. Never set the wordmark in any face but Source Sans 3 Semibold (converted to paths here), never capitalise it, never add "app" or ".io" after it, never letter-space it.
6. Minimum sizes: mark 16 px; horizontal lockup 24 px tall; stacked lockup 64 px tall; wordmark alone 12 px tall.
7. Clear space: one gap width times three (24 of 64 units, one tile) on every side of the mark; one x-height of the wordmark around any lockup. Nothing else inside that box.
8. The tiles are not a mascot and not a chart. They never fill with photos, never become a progress meter, never rearrange on hover. One tile is out, and it stays out.

## Files

| File | Use |
| - | - |
| mark.svg, mark-dark.svg, mark-mono.svg | the mark alone, ink on light, light on dark, and single colour for template images (menu bar, toolbar) |
| wordmark.svg, wordmark-dark.svg | "sorted" as paths, Source Sans 3 Semibold with the font's own kerning |
| lockup-horizontal.svg, lockup-horizontal-dark.svg | mark then wordmark; the settled grid spans baseline to ascender, the pulled tile floats above |
| lockup-stacked.svg, lockup-stacked-dark.svg | mark over the wordmark, for the site hero and the about box |
| favicon.svg, favicon-32.png, favicon-180.png | favicon (Safari ignores SVG favicons, hence the 32 PNG) and the apple touch icon on mint, full bleed, iOS rounds it |
| app-icon.svg, app-icon-1024.png, app-icon-512.png, app-icon-128.png | macOS icon: continuous corner squircle, 824 of 1024 with transparent margin, mint ground, top lit, no shadow (macOS adds its own) |
| social/ | GitHub preview, OG image, X header, LinkedIn banner, Instagram profile, post and story templates, YouTube banner with the safe area marked, and a sticker; SVG source and PNG export, contact sheet in social/sheet.png |
| directions.png, proof.png | the three directions explored (A gate eye, B the o as the eye, C the chosen tile), and the kit in use |
| build.py, sheets.py, social.py | regenerate everything: `python3 docs/brand/build.py` (system python with fontTools; screenshots run through the venv) |

The unsuffixed files are the ink on light canonical set; `site/brand/` carries copies of mark.svg, wordmark.svg and favicon.svg for the landing page.

## Palette

| Name | Hex | Role |
| - | - | - |
| ink | #15171b | settled tiles, wordmark, body text on light |
| ground | #f7f5ef | paper, the light ground; the tiles on dark |
| blush | #f3c8d0 | the pulled tile, always |
| mint | #bfe5cf | app icon ground, GitHub and LinkedIn, the pastel the tile reads best against on light and on dark |
| sky | #bcd8f0 | OG image, Instagram post |
| butter | #f2e3a2 | X header, Instagram story |
| lilac | #d6cbee | spare pastel, use sparingly; the pink tile washes out on it, so never behind the mark |
| accent | #2b5ce0 | links, primary button, the one saturated colour; never as a gradient, never inside the mark |

On dark ground (the app is Spectrum dark, #1d1d1d) the settled tiles go to ground (#f7f5ef) and the pulled tile stays blush. Inside the app toolbar the settled tiles take Spectrum g600 (#b3b3b3) to match the title text; the blush tile stays and still reads as pink on g100 (proof.png, toolbar row).

## Typography

Wordmark: Source Sans 3 Semibold, lower case, converted to outlines so the SVG never depends on a font. Chosen over a geometric alternative because the app UI is already set in Source Sans 3 at 14 px 600, and the lockup sits in that toolbar; a second face would fight it. Copy on the social set is Source Sans 3 Regular and Semibold, also as outlines, one shared outline per glyph.
