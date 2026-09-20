"""The two proof sheets: directions.png (three directions side by side) and proof.png (the kit in use)."""
from build import *

CSS = f'''<style>
body{{margin:0;padding:28px 32px;background:#e8e5de;font:13px/1.35 "Source Sans 3",-apple-system,sans-serif;color:#2a2a2a}}
@font-face{{font-family:"Source Sans 3";font-weight:600;src:url(file://{FONT})}}
@font-face{{font-family:"Source Sans 3";font-weight:400;src:url(file://{str(FONT).replace('Semibold','Regular')})}}
h2{{margin:0 0 4px;font-size:15px;font-weight:600}} h3{{margin:0;font-weight:600;font-size:13px;width:160px;flex:none}}
.sub{{color:#666;margin:0 0 18px}}
.row{{display:flex;align-items:center;gap:14px;margin-bottom:18px}}
.cell{{display:flex;align-items:center;justify-content:center;gap:12px;padding:12px;border-radius:6px;flex:none}}
.light{{background:{PAPER}}} .dark{{background:#262626}} .pastel{{background:{MINT}}}
.lbl{{font-size:10px;color:#777;text-align:center;margin-top:4px}}
.col{{display:flex;flex-direction:column;align-items:center}}
svg{{display:block}}
.px{{image-rendering:pixelated;display:block}}
</style>'''


def inline(s):
    return s.split('\n', 1)[1] if s.startswith('<?xml') else s


def box(inner, size, vb='0 0 64 64'):
    return f'<svg {NS} width="{size}" height="{size}" viewBox="{vb}">{inner}</svg>'


def word_with_disc(fill=INK, disc=BUTTER, h=36):
    parts = []
    x = 0
    font_glyphs = GLYPHS
    for ch, d in zip('sorted', font_glyphs):
        if ch == 'o':
            import re
            xs = [float(t) for t in re.findall(r'-?\d+(?:\.\d+)?', d)][0::2]
            cx = (min(xs) + max(xs)) / 2
            cy = -X_HEIGHT / 2 - 10
            r = X_HEIGHT / 2 + 18
            parts.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="{disc}"/>')
            parts.append(rrect(round(cx - 140), round(cy - 75), 280, 150, 32, fill))
        else:
            parts.append(f'<path d="{d}" fill="{fill}"/>')
    vb = WORD_VB
    return f'<svg {NS} width="{vb[2]*h/vb[3]:.0f}" height="{h}" viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}">{"".join(parts)}</svg>'


def word(fill=INK, h=36):
    vb = WORD_VB
    return f'<svg {NS} width="{vb[2]*h/vb[3]:.0f}" height="{h}" viewBox="{vb[0]} {vb[1]} {vb[2]} {vb[3]}"><path d="{WORD_PATH}" fill="{fill}"/></svg>'


def sq(inner_mark, size, ground, gid):
    """squircle cell for the directions sheet: any mark, full bleed, top lit"""
    path = squircle_path(64)
    grad = (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0.28"/>'
            f'<stop offset="0.55" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity="0.07"/></linearGradient>')
    return (f'<svg {NS} width="{size}" height="{size}" viewBox="0 0 64 64"><defs>{grad}</defs>'
            f'<path d="{path}" fill="{ground}"/><path d="{path}" fill="url(#{gid})"/>'
            f'<g transform="translate(8.3 8.3) scale(0.74)">{inner_mark}</g></svg>')


def raster(inner, size, bg, name):
    """rasterise a mark at 1x and hand back an img tag blown up with pixelated scaling"""
    hp = TMP / f'{name}.html'
    write(hp, f'<style>html,body{{margin:0;background:{bg}}}</style>{box(inner, size)}')
    png = TMP / f'{name}.png'
    shot(hp, png, 1, size, height=size)
    return f'<img class="px" src="file://{png}" width="{size*6}" height="{size*6}">'


def directions():
    rows = []
    # A: gate eye (the pick)
    a_l, a_d = alt_eye(), alt_eye(lid=PAPER, iris=SKY, pupil=INK)
    rows.append(('A. gate eye (alternate)', 'an eye whose pupil is a film gate. The app looks at every shot; the gate says footage, not surveillance.',
                 a_l, a_d, box(a_l, 36) + word(), box(a_d, 36) + word(PAPER),
                 sq(alt_eye(iris=PAPER), 128, SKY, 'ga'), sq(alt_eye(lid=PAPER, iris=SKY, pupil=INK), 128, '#2c2c2e', 'gb')))
    # B: disc eye living in the o
    b_l, b_d = alt_disc(), alt_disc(BUTTER, INK)
    rows.append(('B. the o is the eye', 'a pastel disc with a sharp ink gate, and the wordmark carries it as its o.',
                 b_l, b_d, word_with_disc(), word_with_disc(PAPER),
                 sq(alt_disc(PAPER, INK), 128, BUTTER, 'gc'), sq(alt_disc(BUTTER, INK), 128, '#2c2c2e', 'gd')))
    # C: grid, one tile lifts
    c_l, c_d = mark(), mark(ink=PAPER)
    rows.append(('C. one tile lifts out (chosen)', 'a 2 by 2 contact sheet with one frame pulled: the select.',
                 c_l, c_d, box(c_l, 36) + word(), box(c_d, 36) + word(PAPER),
                 sq(mark(), 128, MINT, 'ge'), sq(mark(ink=PAPER), 128, '#2c2c2e', 'gf')))
    html = [CSS, '<h2>sorted: three directions</h2><p class="sub">mark at 64, lockup on light and dark, app icon at 128 on light and dark, 16 px menu bar size, 1:1 and as real 1x pixels zoomed 6x.</p>']
    for name, note, ml, md, lock_l, lock_d, ic_l, ic_d in rows:
        html.append(f'''<div class="row"><div><h3>{name}</h3><div style="width:160px;color:#666;font-size:11px">{note}</div></div>
<div class="cell light col">{box(ml, 64)}<div class="lbl">64</div></div>
<div class="cell light">{lock_l}</div>
<div class="cell dark">{lock_d}</div>
<div class="cell light">{ic_l}</div>
<div class="cell dark">{ic_d}</div>
<div class="cell light col" style="padding:8px">{box(ml, 16)}<div class="lbl">16</div></div>
<div class="cell dark col" style="padding:8px">{box(md, 16)}<div class="lbl">16</div></div>
<div class="cell light col" style="padding:8px">{raster(ml, 16, PAPER, 'z' + name[0] + 'l')}<div class="lbl">16 at 1x, 6x zoom</div></div>
<div class="cell dark col" style="padding:8px">{raster(md, 16, '#262626', 'z' + name[0] + 'd')}<div class="lbl">16 at 1x, 6x zoom</div></div>
</div>''')
    hp = TMP / 'directions.html'
    write(hp, ''.join(html))
    shot(hp, OUT / 'directions.png', 1, 1500)


def toolbar(with_mark=True):
    """the app toolbar: 44 px Spectrum strip, Source Sans 3 600 at 14 px, the h1 replaced by the lockup"""
    lock = (box(mark(ink=SPECTRUM['g600']), 18) +
            f'<span style="font:600 14px/20px \'Source Sans 3\';color:{SPECTRUM["g600"]};margin-left:7px">sorted</span>') if with_mark else \
        f'<span style="font:600 14px/20px \'Source Sans 3\';color:{SPECTRUM["g600"]}">sorted</span>'
    btn = lambda t: f'<span style="display:inline-flex;align-items:center;height:28px;padding:0 10px;border:1px solid {SPECTRUM["g300"]};border-radius:4px;font:500 14px \'Source Sans 3\';color:{SPECTRUM["g800"]}">{t}</span>'
    return f'''<div style="width:760px;height:44px;background:{SPECTRUM['g100']};border-bottom:1px solid {SPECTRUM['g50']};display:flex;align-items:center;gap:12px;padding:0 12px;box-sizing:border-box;border-radius:6px 6px 0 0">
<div style="display:flex;align-items:center;padding-right:12px;border-right:1px solid {SPECTRUM['g300']};height:20px">{lock}</div>
<div style="display:flex;flex-direction:column;line-height:1.15"><span style="font:600 18px 'Source Sans 3';color:{SPECTRUM['g800']}">Diu wedding, day 2</span><span style="font:400 12px 'Source Sans 3';color:{SPECTRUM['g600']}">3,677 photos, 214 clips</span></div>
<span style="flex:1"></span>{btn('Open folder')}{btn('Export')}</div>
<div style="width:760px;height:52px;background:{SPECTRUM['g50']};border-radius:0 0 6px 6px;display:flex;align-items:center;padding:0 12px;box-sizing:border-box;gap:8px">
<span style="display:inline-block;width:320px;height:28px;border:1px solid {SPECTRUM['g300']};border-radius:4px;background:#262626;font:400 14px/28px 'Source Sans 3';color:{SPECTRUM['g600']};padding:0 10px;box-sizing:border-box">bride on the boat, golden hour</span></div>'''


def proof():
    files = {n: inline((OUT / n).read_text()) for n in ('mark.svg', 'mark-dark.svg', 'mark-mono.svg', 'wordmark.svg', 'wordmark-dark.svg',
                                                        'lockup-horizontal.svg', 'lockup-horizontal-dark.svg', 'lockup-stacked.svg', 'lockup-stacked-dark.svg', 'favicon.svg')}
    def sized(name, h):
        return files[name].replace('<svg ', f'<svg style="height:{h}px;width:auto" ', 1)
    def strip(name):
        return files[name].split('>', 1)[1].rsplit('<', 1)[0]
    sizes = ''.join(f'<div class="col">{box(mark(), s)}<div class="lbl">{s}</div></div>' for s in (16, 32, 64, 256))
    sizes_d = ''.join(f'<div class="col">{box(mark(ink=PAPER), s)}<div class="lbl">{s}</div></div>' for s in (16, 32, 64, 256))
    mono = ''.join(f'<div class="col">{box(mark(mono=True), s)}<div class="lbl">{s} mono</div></div>' for s in (16, 22))
    mono += f'<div class="col">{raster(mark(mono=True), 16, PAPER, "zmono")}<div class="lbl">16 mono, real pixels</div></div>'
    mono += f'<div class="col">{raster(mark(), 16, PAPER, "z16")}<div class="lbl">16, real pixels</div></div>'
    mono += f'<div class="col">{raster(mark(ink=PAPER), 16, "#262626", "z16d")}<div class="lbl">16 on dark, real pixels</div></div>'
    pal = ''.join(f'<div class="col"><div style="width:64px;height:48px;border-radius:6px;background:{c};border:1px solid rgba(0,0,0,.06)"></div><div class="lbl">{n}<br>{c}</div></div>'
                  for n, c in (('ink', INK), ('ground', PAPER), ('sky', SKY), ('mint', MINT), ('butter', BUTTER), ('blush', BLUSH), ('lilac', LILAC), ('accent', ACCENT)))
    icons = ''.join(f'<div class="col"><img src="file://{OUT}/app-icon-{s}.png" width="{d}" height="{d}"><div class="lbl">icon {s} shown at {d}</div></div>' for s, d in ((1024, 160), (512, 96), (128, 64)))
    favs = (f'<div class="col"><img src="file://{OUT}/favicon-32.png" width="32" height="32"><div class="lbl">favicon 32 png</div></div>'
            f'<div class="col"><img src="file://{OUT}/favicon-180.png" width="60" height="60" style="border-radius:13px"><div class="lbl">touch icon 180</div></div>'
            f'<div class="col">{files["favicon.svg"]}<div class="lbl">favicon.svg</div></div>')
    html = f'''{CSS}<h2>sorted: proof sheet</h2><p class="sub">the kit on light, dark and pastel; the mark at 16, 32, 64, 256 with real 1x pixels; menu bar mono; app icons and favicons from vector; the lockup inside the app's 44 px Spectrum toolbar.</p>
<div class="row"><h3>light</h3><div class="cell light" style="gap:28px">{sized('lockup-horizontal.svg', 44)}{sized('lockup-stacked.svg', 120)}{sized('wordmark.svg', 40)}{box(strip('mark.svg'), 64)}</div></div>
<div class="row"><h3>dark</h3><div class="cell dark" style="gap:28px">{sized('lockup-horizontal-dark.svg', 44)}{sized('lockup-stacked-dark.svg', 120)}{sized('wordmark-dark.svg', 40)}{box(strip('mark-dark.svg'), 64)}</div></div>
<div class="row"><h3>pastel block</h3><div class="cell pastel" style="gap:28px">{sized('lockup-horizontal.svg', 44)}{box(mark(), 64)}{box(mark(lift=BUTTER), 64)}{box(mark(lift=PAPER), 64)}</div>
<div class="cell" style="background:{BUTTER};gap:28px">{sized('lockup-stacked.svg', 100)}</div><div class="cell" style="background:{SKY}">{box(mark(), 64)}</div><div class="cell" style="background:{BLUSH}">{box(mark(lift=PAPER), 64)}</div></div>
<div class="row"><h3>sizes</h3><div class="cell light" style="gap:20px;align-items:flex-end">{sizes}{mono}</div><div class="cell dark" style="gap:20px;align-items:flex-end">{sizes_d}</div></div>
<div class="row"><h3>app icon</h3><div class="cell light" style="gap:24px;align-items:flex-end">{icons}</div><div class="cell dark" style="gap:24px;align-items:flex-end">{icons}</div><div class="cell light" style="gap:18px;align-items:flex-end">{favs}</div></div>
<div class="row"><h3>in the toolbar</h3><div>{toolbar()}</div></div>
<div class="row"><h3>palette</h3><div class="cell light" style="gap:14px">{pal}</div></div>'''
    hp = TMP / 'proof.html'
    write(hp, html)
    shot(hp, OUT / 'proof.png', 1, 1500)


def icon_grounds():
    """scratch test: which pastel ground carries the pink tile on light and on dark"""
    cells = ''
    for g, n in ((MINT, 'mint'), (SKY, 'sky'), (BUTTER, 'butter'), (PAPER, 'paper'), (LILAC, 'lilac')):
        icon = inline(app_icon(128, ground=g, gid='g' + n))
        cells += f'<div class="cell light col">{icon}<div class="lbl">{n}</div></div><div class="cell dark col">{icon}<div class="lbl">{n}</div></div>'
    write(TMP / 'grounds.html', CSS + f'<div class="row" style="flex-wrap:wrap">{cells}</div>')
    shot(TMP / 'grounds.html', TMP / 'grounds.png', 1, 1500)
