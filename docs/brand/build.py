"""Builds the sorted brand kit: SVG marks, wordmark (Source Sans 3 Semibold converted to paths),
lockups, favicon, macOS app icons and the two proof sheets.

Run with the system python3 (needs fontTools + brotli to open the woff2); it shells out to the
project venv for Playwright screenshots. Everything is drawn on a 64 unit grid; the wordmark
lives in font units (1000 per em) and is scaled at the end.
"""
import json, math, os, subprocess, sys
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SITE = ROOT / 'site' / 'brand'
FONT = ROOT / 'photosort' / 'ui' / 'SourceSans3-Semibold.woff2'
VENV_PY = ROOT / '.venv' / 'bin' / 'python'
import tempfile
TMP = Path(os.environ.get('BRAND_TMP', Path(tempfile.gettempdir()) / 'sorted-brand'))

# palette
INK = '#15171b'
PAPER = '#f7f5ef'
SKY = '#bcd8f0'
MINT = '#bfe5cf'
BUTTER = '#f2e3a2'
BLUSH = '#f3c8d0'
LILAC = '#d6cbee'
ACCENT = '#2b5ce0'
SPECTRUM = {'g50': '#1d1d1d', 'g100': '#323232', 'g300': '#545454', 'g600': '#b3b3b3', 'g800': '#e6e6e6'}

XML = '<?xml version="1.0" encoding="UTF-8"?>\n'
NS = 'xmlns="http://www.w3.org/2000/svg"'


# wordmark: glyph outlines with GPOS pair kerning, so it matches what the browser shapes
def kern_lookup(font):
    gpos = font['GPOS'].table
    idx = set()
    for fr in gpos.FeatureList.FeatureRecord:
        if fr.FeatureTag == 'kern':
            idx.update(fr.Feature.LookupListIndex)
    subs = []
    for li in sorted(idx):
        lk = gpos.LookupList.Lookup[li]
        for st in lk.SubTable:
            if lk.LookupType == 9:
                st = st.ExtSubTable
            if st.__class__.__name__ == 'PairPos':
                subs.append(st)

    def kern(g1, g2):
        for st in subs:
            if g1 not in st.Coverage.glyphs:
                continue
            if st.Format == 1:
                for pv in st.PairSet[st.Coverage.glyphs.index(g1)].PairValueRecord:
                    if pv.SecondGlyph == g2 and pv.Value1 is not None:
                        return getattr(pv.Value1, 'XAdvance', 0)
            else:
                c1 = st.ClassDef1.classDefs.get(g1, 0)
                c2 = st.ClassDef2.classDefs.get(g2, 0)
                v = st.Class1Record[c1].Class2Record[c2].Value1
                if v is not None and getattr(v, 'XAdvance', 0):
                    return v.XAdvance
        return 0
    return kern


FONTS = {}


def word_glyphs(text, weight='Semibold'):
    path = str(FONT).replace('Semibold', weight)
    font = FONTS.get(path) or FONTS.setdefault(path, TTFont(path))
    cmap, gs, hmtx = font.getBestCmap(), font.getGlyphSet(), font['hmtx']
    kern = kern_lookup(font)
    names = [cmap[ord(c)] for c in text]
    x, out = 0, []
    for i, g in enumerate(names):
        pen = SVGPathPen(gs, ntos=lambda v: f'{v:.0f}')
        gs[g].draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        if pen.getCommands():
            out.append(pen.getCommands())
        x += hmtx[g][0] + (kern(g, names[i + 1]) if i + 1 < len(names) else 0)
    return out, x


GLYPHS, WORD_W = word_glyphs('sorted')
WORD_PATH = 'M' + 'M'.join(p[1:] for p in GLYPHS)   # one path, six subpaths


def text_path(text, weight='Regular'):
    """any line of copy as one path in font units, baseline at y 0; returns (d, advance width)"""
    glyphs, w = word_glyphs(text, weight)
    return 'M' + 'M'.join(p[1:] for p in glyphs), w
X_HEIGHT = 491
ASC = 730     # top of the d, with a little air
WORD_VB = (0, -ASC, WORD_W, ASC + 40)


# primitives
def rrect(x, y, w, h, r, fill, extra=''):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{extra}/>'


def svg(inner, w, h, vb, extra=''):
    return f'{XML}<svg {NS} width="{w}" height="{h}" viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}"{extra}>\n{inner}\n</svg>\n'


# the mark: a 2 by 2 contact sheet with one frame pulled. Tiles are 24 on an 8 gap from 4,
# so at 16 px the three settled tiles land on whole pixels (1..7 and 9..15).
TILE, GAP, ORG, RAD = 24, 8, 4, 5
LIFT_C = (48.6, 15.4)  # centre of the pulled tile, its top corner stays 2 units inside the box
LIFT_DEG = 12


def mark(ink=INK, lift=BLUSH, mono=False):
    lift = ink if mono else lift
    o2 = ORG + TILE + GAP
    tiles = (rrect(ORG, ORG, TILE, TILE, RAD, ink) + rrect(ORG, o2, TILE, TILE, RAD, ink) +
             rrect(o2, o2, TILE, TILE, RAD, ink))
    cx, cy = LIFT_C
    pulled = (f'<rect x="{cx - TILE/2}" y="{cy - TILE/2}" width="{TILE}" height="{TILE}" rx="{RAD}" '
              f'fill="{lift}" transform="rotate({LIFT_DEG} {cx} {cy})"/>')
    return tiles + pulled


def mark_at(x, y, s, **kw):
    return f'<g transform="translate({x:.1f} {y:.1f}) scale({s:.4f})">{mark(**kw)}</g>'


# alternates, shown on the directions sheet only
LID = 'M5.3 32C17.3 14 46.7 14 58.7 32C46.7 50 17.3 50 5.3 32Z'


def alt_eye(lid=INK, iris=SKY, pupil=INK, sw=5.5, with_iris=True):
    parts = [f'<circle cx="32" cy="32" r="14" fill="{iris}"/>'] if with_iris else []
    parts.append(f'<path d="{LID}" fill="none" stroke="{lid}" stroke-width="{sw}" stroke-linejoin="miter" stroke-miterlimit="8"/>')
    parts.append(rrect(23, 27, 18, 10, 2.2, pupil))
    return ''.join(parts)


def alt_disc(disc=BUTTER, pupil=INK):
    return f'<circle cx="32" cy="32" r="28" fill="{disc}"/>' + rrect(17, 24, 30, 16, 3.5, pupil)


# continuous corner squircle, the macOS icon silhouette (figma style smoothing 0.6, radius 22.37 percent)
def squircle_path(w, r=None, smoothing=0.6):
    r = r or w * 0.2237
    p = min((1 + smoothing) * r, w / 2)
    arc = 90 * (1 - smoothing)
    arc_len = math.sin(math.radians(arc / 2)) * r * math.sqrt(2)
    alpha = (90 - arc) / 2
    p34 = r * math.tan(math.radians(alpha / 2))
    beta = 45 * smoothing
    c = p34 * math.cos(math.radians(beta))
    d = c * math.tan(math.radians(beta))
    b = (p - arc_len - c - d) / 3
    a = 2 * b
    f = lambda v: f'{v:.2f}'
    return (f'M{f(w - p)} 0'
            f'c{f(a)} 0 {f(a + b)} 0 {f(a + b + c)} {f(d)}a{f(r)} {f(r)} 0 0 1 {f(arc_len)} {f(arc_len)}c{f(d)} {f(c)} {f(d)} {f(b + c)} {f(d)} {f(a + b + c)}'
            f'L{f(w)} {f(w - p)}'
            f'c0 {f(a)} 0 {f(a + b)} {f(-d)} {f(a + b + c)}a{f(r)} {f(r)} 0 0 1 {f(-arc_len)} {f(arc_len)}c{f(-c)} {f(d)} {f(-(b + c))} {f(d)} {f(-(a + b + c))} {f(d)}'
            f'L{f(p)} {f(w)}'
            f'c{f(-a)} 0 {f(-(a + b))} 0 {f(-(a + b + c))} {f(-d)}a{f(r)} {f(r)} 0 0 1 {f(-arc_len)} {f(-arc_len)}c{f(-d)} {f(-c)} {f(-d)} {f(-(b + c))} {f(-d)} {f(-(a + b + c))}'
            f'L0 {f(p)}'
            f'c0 {f(-a)} 0 {f(-(a + b))} {f(d)} {f(-(a + b + c))}a{f(r)} {f(r)} 0 0 1 {f(arc_len)} {f(-arc_len)}c{f(c)} {f(-d)} {f(b + c)} {f(-d)} {f(a + b + c)} {f(-d)}Z')


def app_icon(size, ground=MINT, ink=INK, lift=BLUSH, full_bleed=False, gid='lit'):
    """macOS convention: the shape is 824/1024 of the canvas, centred, transparent around it."""
    shape = size if full_bleed else size * 824 / 1024
    off = (size - shape) / 2
    path = squircle_path(shape)
    s = shape / 64 * 0.72
    ms = 64 * s
    mo = (shape - ms) / 2
    grad = (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#ffffff" stop-opacity="0.28"/>'
            f'<stop offset="0.55" stop-color="#ffffff" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="#000000" stop-opacity="0.07"/></linearGradient>')
    inner = (f'<defs>{grad}</defs><g transform="translate({off:.2f} {off:.2f})">'
             f'<path d="{path}" fill="{ground}"/><path d="{path}" fill="url(#{gid})"/>'
             f'{mark_at(mo, mo + shape * 0.01, s, ink=ink, lift=lift)}</g>')
    return svg(inner, size, size, (0, 0, size, size))


# lockups: the settled grid (4..60 of the box) spans baseline to ascender; the pulled tile floats above
GRID = ORG + 2 * TILE + GAP - ORG   # 56 units of settled grid


def lockup_h(ink=INK, lift=BLUSH):
    s = ASC / GRID
    gap = 190
    m = mark_at(-ORG * s, -(ORG + GRID) * s, s, ink=ink, lift=lift)
    tx = GRID * s + gap
    w = tx + WORD_W
    inner = m + f'<path transform="translate({tx:.0f} 0)" d="{WORD_PATH}" fill="{ink}"/>'
    top = -(ORG + GRID) * s - 1 * s      # the pulled tile reaches about y 0.5 of the box
    vb = (-40, top - 40, w + 80, -top + 80)
    return svg(inner, round(vb[2] / 10), round(vb[3] / 10), vb)


def lockup_stacked(ink=INK, lift=BLUSH):
    grid_w = 1000
    s = grid_w / GRID
    cx = WORD_W / 2
    gap = 260
    bottom = -ASC - gap                       # settled grid sits here
    top = bottom - GRID * s
    m = mark_at(cx - (ORG + GRID / 2) * s, top - ORG * s, s, ink=ink, lift=lift)
    inner = m + f'<path d="{WORD_PATH}" fill="{ink}"/>'
    t0 = top - 1 * s - 60
    vb = (-60, t0, WORD_W + 120, -t0 + 100)
    return svg(inner, round(vb[2] / 10), round(vb[3] / 10), vb)


def wordmark_svg(ink=INK):
    vb = WORD_VB
    return svg(f'<path d="{WORD_PATH}" fill="{ink}"/>', round(vb[2] / 10), round(vb[3] / 10), vb)


def mark_svg(size=64, **kw):
    return svg(mark(**kw), size, size, (0, 0, 64, 64))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def shot(html_path, png_path, scale=1, width=1400, transparent=False, height=None):
    code = f'''
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={{'width': {width}, 'height': {height or 600}}}, device_scale_factor={scale})
    pg.goto('file://{html_path}')
    pg.wait_for_timeout(250)
    pg.screenshot(path='{png_path}', full_page={height is None}, omit_background={transparent})
    b.close()
'''
    subprocess.run([str(VENV_PY), '-c', code], check=True)


def icon_png(size):
    html = f'<style>html,body{{margin:0;background:transparent}}</style>' + app_icon(size).split('\n', 1)[1]
    hp = TMP / f'icon-{size}.html'
    write(hp, html)
    shot(hp, OUT / f'app-icon-{size}.png', 1, size, transparent=True, height=size)


def favicon_png(size, ground=None):
    """32: transparent mark. 180 (apple touch): full bleed pastel, iOS rounds it"""
    if ground:
        body = f'<svg {NS} width="{size}" height="{size}" viewBox="0 0 64 64"><rect width="64" height="64" fill="{ground}"/>{mark_at(7, 7, 0.78)}</svg>'
    else:
        body = f'<svg {NS} width="{size}" height="{size}" viewBox="0 0 64 64">{mark()}</svg>'
    hp = TMP / f'favicon-{size}.html'
    write(hp, f'<style>html,body{{margin:0;background:transparent}}</style>{body}')
    shot(hp, OUT / f'favicon-{size}.png', 1, size, transparent=not ground, height=size)


def build_kit():
    files = {
        'mark.svg': mark_svg(),
        'mark-dark.svg': mark_svg(ink=PAPER),
        'mark-mono.svg': mark_svg(mono=True),
        'wordmark.svg': wordmark_svg(),
        'wordmark-dark.svg': wordmark_svg(PAPER),
        'lockup-horizontal.svg': lockup_h(),
        'lockup-horizontal-dark.svg': lockup_h(PAPER),
        'lockup-stacked.svg': lockup_stacked(),
        'lockup-stacked-dark.svg': lockup_stacked(PAPER),
        'favicon.svg': svg(mark(), 32, 32, (0, 0, 64, 64)),
        'app-icon.svg': app_icon(1024),
    }
    for name, text in files.items():
        write(OUT / name, text)
    for name in ('mark.svg', 'wordmark.svg', 'favicon.svg'):
        write(SITE / name, files[name])
    for size in (1024, 512, 128):
        icon_png(size)
    favicon_png(32)
    favicon_png(180, MINT)


if __name__ == '__main__':
    TMP.mkdir(parents=True, exist_ok=True)
    build_kit()
    import sheets, social
    sheets.directions()
    sheets.proof()
    social.build_social()
    print('built', OUT)
