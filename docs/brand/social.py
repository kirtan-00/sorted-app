"""Social and banner set, all from the same mark and wordmark. SVG source plus PNG export."""
from build import *
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

SOC = OUT / 'social'
TAG = 'Sorts out post.'
LINE2 = 'Photos and video, on your Mac. Nothing leaves the room.'


DEFS = {}      # glyph outlines defined once per file and placed with <use>


def glyph_uses(s, weight):
    """each glyph of a line as a <use> of a shared outline, so repeated letters cost a few bytes"""
    path = str(FONT).replace('Semibold', weight)
    font = FONTS.get(path) or FONTS.setdefault(path, TTFont(path))
    cmap, gs, hmtx = font.getBestCmap(), font.getGlyphSet(), font['hmtx']
    kern = kern_lookup(font)
    names = [cmap[ord(c)] for c in s]
    x, uses = 0, []
    for i, g in enumerate(names):
        gid = f'{weight[0].lower()}{g}'
        if gid not in DEFS:
            pen = SVGPathPen(gs, ntos=lambda v: f'{v:.0f}')
            gs[g].draw(TransformPen(pen, (1, 0, 0, -1, 0, 0)))
            DEFS[gid] = pen.getCommands()
        if DEFS[gid]:
            uses.append(f'<use href="#{gid}" xlink:href="#{gid}" x="{x}"/>')
        x += hmtx[g][0] + (kern(g, names[i + 1]) if i + 1 < len(names) else 0)
    return ''.join(uses), x


def text(x, y, size, s, weight='Regular', fill=INK, anchor='start'):
    """a line of copy as glyph outlines; size is the em in px, y the baseline"""
    uses, adv = glyph_uses(s, weight)
    k = size / 1000
    if anchor == 'middle':
        x -= adv * k / 2
    elif anchor == 'end':
        x -= adv * k
    return f'<g transform="translate({x:.1f} {y:.1f}) scale({k:.5f})" fill="{fill}">{uses}</g>'


def lockup(x, y, h, ink=INK, lift=BLUSH, anchor='start'):
    """horizontal lockup, h px tall (pulled tile top to baseline), top left at x, y"""
    s = ASC / GRID
    k = h / ((ORG + GRID + 1) * s)
    tx = GRID * s + 190
    w = (tx + WORD_W) * k
    if anchor == 'middle':
        x -= w / 2
    top = -(ORG + GRID + 1) * s
    inner = mark_at(-ORG * s, -(ORG + GRID) * s, s, ink=ink, lift=lift) + f'<path transform="translate({tx:.0f} 0)" d="{WORD_PATH}" fill="{ink}"/>'
    return f'<g transform="translate({x:.1f} {y - top * k:.1f}) scale({k:.5f})">{inner}</g>', w


def stacked(cx, y, w, ink=INK, lift=BLUSH):
    """stacked lockup, w px wide, centred on cx, top at y"""
    grid_w = 1000
    s = grid_w / GRID
    gap = 260
    bottom = -ASC - gap
    top = bottom - GRID * s - s
    k = w / WORD_W
    inner = mark_at(WORD_W / 2 - (ORG + GRID / 2) * s, top - ORG * s + s, s, ink=ink, lift=lift) + f'<path d="{WORD_PATH}" fill="{ink}"/>'
    return f'<g transform="translate({cx - w / 2:.1f} {y - top * k:.1f}) scale({k:.5f})">{inner}</g>', -top * k


def slot(x, y, w, h, label='screenshot goes here'):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{PAPER}" fill-opacity="0.55" stroke="{INK}" stroke-opacity="0.45" stroke-width="3" stroke-dasharray="16 12"/>'
            + text(x + w / 2, y + h / 2 + 10, 28, label, 'Regular', '#6b6b6b', 'middle'))


def defs():
    d = ''.join(f'<path id="{k}" d="{v}"/>' for k, v in DEFS.items() if v)
    DEFS.clear()
    return f'<defs>{d}</defs>' if d else ''


def sheet_svg(w, h, ground, inner):
    return svg(f'{defs()}<rect width="{w}" height="{h}" fill="{ground}"/>{inner}', w, h, (0, 0, w, h), ' xmlns:xlink="http://www.w3.org/1999/xlink"')


def github(w=1280, h=640):
    lk, lw = lockup(112, 168, 150)
    inner = lk + text(112, 418, 64, TAG, 'Semibold') + text(112, 486, 34, LINE2, 'Regular', '#3a3d44')
    return sheet_svg(w, h, MINT, inner)


def og(w=1200, h=630):
    lk, lw = lockup(100, 140, 150)
    inner = lk + text(100, 390, 62, TAG, 'Semibold') + text(100, 458, 32, LINE2, 'Regular', '#3a3d44')
    return sheet_svg(w, h, SKY, inner)


def x_header(w=1500, h=500):
    # the profile picture covers the bottom left corner, so the copy sits centre right
    lk, lw = lockup(540, 120, 150)
    inner = lk + text(540, 360, 58, TAG, 'Semibold') + text(540, 420, 28, LINE2, 'Regular', '#3a3d44')
    return sheet_svg(w, h, BUTTER, inner)


def linkedin(w=1128, h=191):
    lk, lw = lockup(64, 52, 88)
    inner = lk + text(64 + lw + 56, 128, 40, TAG, 'Regular', '#3a3d44')
    return sheet_svg(w, h, MINT, inner)


def ig_profile(w=1080, h=1080):
    # IG crops to a circle; the mark stays well inside radius 540
    return sheet_svg(w, h, MINT, mark_at(260, 260, 560 / 64, ink=INK, lift=BLUSH))


def ig_post(w=1080, h=1080):
    lk, lw = lockup(80, 80, 80)
    inner = lk + text(80, 262, 54, TAG, 'Semibold') + slot(80, 320, 920, 600) + text(80, 1000, 28, LINE2, 'Regular', '#3a3d44')
    return sheet_svg(w, h, SKY, inner)


def ig_story(w=1080, h=1920):
    # 250 px top and bottom are covered by the IG chrome
    lk, lw = lockup(80, 290, 84)
    inner = lk + text(80, 480, 58, TAG, 'Semibold') + slot(80, 540, 920, 1040) + text(80, 1640, 28, LINE2, 'Regular', '#3a3d44')
    return sheet_svg(w, h, BUTTER, inner)


def youtube(w=2560, h=1440):
    sx, sy, sw, sh = (w - 1546) / 2, (h - 423) / 2, 1546, 423
    lk, lw = lockup(sx + 60, sy + 70, 130)
    inner = lk + text(sx + 60, sy + 300, 60, TAG, 'Semibold') + text(sx + 60, sy + 360, 30, LINE2, 'Regular', '#3a3d44')
    inner += f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="8" fill="none" stroke="{INK}" stroke-opacity="0.35" stroke-width="3" stroke-dasharray="18 12"/>'
    inner += text(sx + sw - 24, sy + sh - 20, 24, 'safe area 1546 x 423, remove before upload', 'Regular', '#6b6b6b', 'end')
    inner += mark_at(sx + sw + 60, (h - 380) / 2, 380 / 64, ink=INK, lift=BLUSH)
    return sheet_svg(w, h, MINT, inner)


def sticker(w=800, h=800):
    k = 480 / WORD_W
    inner = f'<rect width="{w}" height="{h}" rx="96" fill="{BLUSH}"/>'
    inner += f'<path transform="translate({(w - WORD_W * k) / 2:.1f} {h / 2 + X_HEIGHT * k / 2:.1f}) scale({k:.5f})" d="{WORD_PATH}" fill="{INK}"/>'
    return svg(inner, w, h, (0, 0, w, h))


ITEMS = [
    ('github-social-preview', github), ('og-image', og), ('x-header', x_header), ('linkedin-banner', linkedin),
    ('ig-profile', ig_profile), ('ig-post-template', ig_post), ('ig-story-template', ig_story),
    ('youtube-banner', youtube), ('sticker', sticker),
]


def build_social():
    SOC.mkdir(parents=True, exist_ok=True)
    cells = ''
    for name, fn in ITEMS:
        s = fn()
        write(SOC / f'{name}.svg', s)
        import re
        w, h = [int(v) for v in re.search(r'width="(\d+)" height="(\d+)"', s).groups()]
        hp = TMP / f'{name}.html'
        write(hp, f'<style>html,body{{margin:0;background:transparent}}</style>{s.split(chr(10), 1)[1]}')
        shot(hp, SOC / f'{name}.png', 1, w, transparent=True, height=h)
        dw = 440 if w >= h else 240
        cells += f'<div class="col"><img src="file://{SOC}/{name}.png" width="{dw}" style="border:1px solid rgba(0,0,0,.08)"><div class="lbl">{name} {w} x {h}</div></div>'
    import sheets
    html = sheets.CSS + f'<h2>sorted: social and banner set</h2><p class="sub">each rendered from its SVG at native size, shown scaled.</p><div class="row" style="flex-wrap:wrap;align-items:flex-start;gap:24px">{cells}</div>'
    write(TMP / 'social.html', html)
    shot(TMP / 'social.html', SOC / 'sheet.png', 1, 1500)


if __name__ == '__main__':
    TMP.mkdir(parents=True, exist_ok=True)
    build_social()
