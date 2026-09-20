#!/usr/bin/env python3
"""Writes the eight frame sub-compositions in compositions/frames/ from one set of shared parts
(fonts, blobs, headline masks) so every frame carries the same landing-page look.
Run from docs/reel: python3 tools/build-frames.py"""
import os, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "compositions", "frames")
os.makedirs(OUT, exist_ok=True)

W, H = 1080, 1920

FONTS = """
@font-face{font-family:"Archivo";font-weight:700;font-stretch:112.5%;font-style:normal;font-display:block;src:url("assets/fonts/Archivo-SemiExpanded-Bold-latin.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:400;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Regular.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:500;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Medium.woff2") format("woff2");}
@font-face{font-family:"Source Sans 3";font-weight:600;font-style:normal;font-display:block;src:url("assets/fonts/SourceSans3-Semibold.woff2") format("woff2");}
"""

BASE_CSS = """
#root { position: absolute; inset: 0; width: 1080px; height: 1920px; overflow: hidden;
  font-family: "Source Sans 3", -apple-system, "Helvetica Neue", Arial, sans-serif; color: #26282d; }
.clip { position: absolute; inset: 0; }
.ground { position: absolute; inset: 0; background: #f7f4ee; }
.blob { position: absolute; border-radius: 50%; opacity: 0.85; }
.b1 { width: 720px; height: 720px; left: -220px; top: -160px; background: radial-gradient(circle, #cfe4fb, rgba(207,228,251,0) 68%); }
.b2 { width: 820px; height: 820px; right: -300px; top: 240px; background: radial-gradient(circle, #ddd6f5, rgba(221,214,245,0) 68%); }
.b3 { width: 640px; height: 640px; left: 260px; top: 1000px; background: radial-gradient(circle, #cfeee0, rgba(207,238,224,0) 68%); }
.b4 { width: 760px; height: 760px; right: -120px; bottom: -300px; background: radial-gradient(circle, #fbefb8, rgba(251,239,184,0) 68%); }
.b5 { width: 600px; height: 600px; left: -200px; bottom: -180px; background: radial-gradient(circle, #cfeee0, rgba(207,238,224,0) 68%); }
.hl { position: absolute; left: 70px; width: 940px; font-family: "Archivo", "Source Sans 3", sans-serif; font-weight: 700;
  font-stretch: 112.5%; letter-spacing: -0.015em; line-height: 1.04; color: #26282d; }
.hl.c { text-align: center; }
.lm { display: block; overflow: hidden; padding-bottom: 0.06em; margin-bottom: -0.06em; }
.li { display: block; }
.sub { position: absolute; left: 70px; width: 940px; font-size: 34px; line-height: 1.4; color: #4f545d; }
.sub.c { text-align: center; }
.win { position: absolute; overflow: hidden; border-radius: 12px; background: #1d1d1d;
  box-shadow: 0 0 0 1px rgba(38,40,45,0.18), 0 40px 90px rgba(38,40,45,0.22); }
.win img { position: absolute; display: block; }
.block { position: absolute; left: 40px; width: 1000px; border-radius: 28px; }
"""

BLOBS = """
      <div class="ground"></div>
      <div class="blob b1" data-layout-allow-overflow></div><div class="blob b2" data-layout-allow-overflow></div><div class="blob b3" data-layout-allow-overflow></div><div class="blob b4" data-layout-allow-overflow></div><div class="blob b5" data-layout-allow-overflow></div>
"""

BLOB_TL = """
  tl.fromTo(q('.b1'), { x: 0, y: 0 }, { x: 70, y: 40, duration: D, ease: 'sine.inOut' }, 0);
  tl.fromTo(q('.b2'), { x: 0, y: 0 }, { x: -60, y: 50, duration: D, ease: 'sine.inOut' }, 0);
  tl.fromTo(q('.b3'), { x: 0, y: 0 }, { x: 50, y: -40, duration: D, ease: 'sine.inOut' }, 0);
  tl.fromTo(q('.b4'), { x: 0, y: 0 }, { x: -40, y: -30, duration: D, ease: 'sine.inOut' }, 0);
  tl.fromTo(q('.b5'), { x: 0, y: 0 }, { x: 40, y: -20, duration: D, ease: 'sine.inOut' }, 0);
"""

def lines(cls, texts):
    return "".join(f'<span class="lm"><span class="li {cls}">{t}</span></span>' for t in texts)

def frame(fid, dur, css, html, js):
    doc = f"""<template>
  <style>{FONTS}{BASE_CSS}{css}
  </style>
  <div id="root" data-composition-id="{fid}" data-width="{W}" data-height="{H}">
    <div id="f{fid}-ground" class="clip" data-start="0" data-duration="{dur}" data-track-index="1">{BLOBS if '__NOBLOBS__' not in html else ''}
    </div>
    <div id="f{fid}-content" class="clip" data-start="0" data-duration="{dur}" data-track-index="2">
{html.replace('__NOBLOBS__', '')}
    </div>
  </div>
  <script>
  (function () {{
    var D = {dur};
    var R = document.querySelector('[data-composition-id="{fid}"]');
    var q = function (s) {{ return R.querySelectorAll(s); }};
    var one = function (s) {{ return R.querySelector(s); }};
    window.__timelines = window.__timelines || {{}};
    var tl = gsap.timeline({{ paused: true }});
{textwrap.indent(js, '    ')}
    window.__timelines["{fid}"] = tl;
  }})();
  </script>
</template>
"""
    with open(os.path.join(OUT, fid + ".html"), "w") as f:
        f.write(doc)
    print("wrote", fid)

def reveal(sel, at, stagger=0.12, dur=0.8):
    return f"tl.fromTo(q('{sel} .li'), {{ yPercent: 112 }}, {{ yPercent: 0, duration: {dur}, ease: 'power3.out', stagger: {stagger} }}, {at});\n"

def fade(sel, at, dur=0.6, y=16):
    return f"tl.fromTo(q('{sel}'), {{ opacity: 0, y: {y} }}, {{ opacity: 1, y: 0, duration: {dur}, ease: 'power2.out' }}, {at});\n"

# ---------------------------------------------------------------- 01 pile
PHOTOS = ["boats1", "hari1", "coast", "sari", "harbour", "hari2", "site", "boatman", "road", "hari3", "sea", "face1", "golden", "boats2"]
# left, top, rotation (deg), for 250x171 tiles, inside x[70..930] y[600..1500]
POS = [(80, 620, -8), (390, 600, 5), (700, 640, -4), (150, 820, 6), (470, 800, -7), (760, 860, 3),
       (90, 1030, 4), (400, 1010, -5), (690, 1060, 7), (170, 1240, -6), (480, 1220, 4), (760, 1260, -3),
       (300, 1400, 5), (620, 1420, -6)]
tiles = "".join(
    f'<div class="tile t{i}" style="left:{x}px;top:{y}px;--r:{r}deg"><b style="background-image:url(assets/photos/{p}.webp)"></b></div>'
    for i, ((x, y, r), p) in enumerate(zip(POS, PHOTOS)))
frame("01-pile", 3.5, """
.hl { top: 290px; font-size: 92px; }
.sub { top: 500px; }
.tile { position: absolute; width: 250px; height: 171px; padding: 4px; border-radius: 6px; background: #fffdf8;
  box-shadow: 0 10px 30px rgba(38,40,45,0.12); }
.tile b { display: block; width: 100%; height: 100%; border-radius: 3px; background-size: cover; background-position: center; }
""", f"""
      <div class="hl f1-hl">{lines('', ['A shoot arrives', 'as a pile.'])}</div>
      <div class="sub f1-sub">955 items. 800 GB. All named DSC05455.</div>
      <div class="pile">{tiles}</div>
""", """
var tiles = q('.tile');
tiles.forEach(function (t, i) {
  var r = parseFloat(t.style.getPropertyValue('--r'));
  tl.fromTo(t, { y: -900, rotation: r + (i % 2 ? 22 : -22), opacity: 0 },
    { y: 0, rotation: r, opacity: 1, duration: 0.85, ease: 'power3.out' }, i * 0.055);
  tl.to(t, { y: 6 + (i % 3) * 3, duration: 1.6, ease: 'sine.inOut' }, 1.5 + (i % 5) * 0.1);
});
""" + reveal('.f1-hl', 1.2) + fade('.f1-sub', 2.4) + BLOB_TL)

# ---------------------------------------------------------------- 02 search
# capture 1520x1920 shown 820 wide -> scale 0.5395 -> 1036 tall; window shows y 520..1556 (1036)
caps = ["app-empty", "app-boat-t01", "app-boat-t02", "app-boat-t03", "app-boat-t04", "app-boat", "app-sadhu", "app-aerial", "app-woman-in-a-sari"]
imgs = "".join(f'<img class="cap c{i}" src="assets/captures/{c}.jpg" alt="">' for i, c in enumerate(caps))
frame("02-search", 6.5, """
.hl { top: 270px; font-size: 84px; }
.sub { top: 555px; font-size: 30px; }
.win { left: 70px; top: 620px; width: 860px; height: 936px; }
.win img { left: -307px; top: -68px; width: 1167px; height: 1474px; opacity: 0; }
.win img.c0 { opacity: 1; }
""", f"""
      <div class="hl f2-hl">{lines('', ['Plug in the disk.', 'Search the', 'whole shoot.'])}</div>
      <div class="sub f2-sub">Plain words. Photos and 4K clips.</div>
      <div class="win f2-win">{imgs}</div>
""", reveal('.f2-hl', 0.05) + fade('.f2-win', 0.5, 0.7, 24) + fade('.f2-sub', 2.6) + """
var caps = q('.cap');
var times = [1.3, 1.5, 1.65, 1.85, 2.4, 4.0, 4.9, 5.7];
times.forEach(function (t, i) {
  tl.set(caps[i + 1], { opacity: 1 }, t);
  tl.set(caps[i], { opacity: 0 }, t + 0.001);
});
tl.fromTo(one('.f2-win'), { scale: 1 }, { scale: 1.04, duration: D, ease: 'none', transformOrigin: '50% 30%' }, 0);
""" + BLOB_TL)

# ---------------------------------------------------------------- 03 people
# replica capture 2560x1522 (replica 1280x760 at 2x). Merge panel region in replica px: x 140..720, y 52..220 -> crop at 2x: x 280..1440, y 104..440
# window 860 wide; region 1160 wide -> scale 0.7414; height 336*0.7414 = 249 -> too short; show y 40..240 (2x: 80..480) -> 400*0.741 = 297
merge = ["replica-merge-034", "replica-merge-038", "replica-merge-041", "replica-merge-045", "replica-merge-050"]
mimgs = "".join(f'<div class="mcap m{i}" style="background-image:url(assets/captures/{c}.jpg)"></div>' for i, c in enumerate(merge))
frame("03-people", 4.5, """
.hl { top: 280px; font-size: 84px; }
.sub { top: 950px; font-size: 30px; }
.w1 { left: 70px; top: 500px; width: 860px; height: 200px; }
.cl, .cr { position: absolute; top: 0; height: 200px; overflow: hidden; }
.cl { left: 0; width: 480px; }
.cr { left: 480px; width: 380px; }
.mcap { position: absolute; width: 2202px; height: 1309px; top: -206px; opacity: 0; background-size: 2202px 1309px; background-repeat: no-repeat; }
.cl .mcap { left: -353px; }
.cr .mcap { left: -1345px; }
.mcap.m0 { opacity: 1; }
.sub { top: 760px; }
.w2 { left: 70px; top: 880px; width: 860px; height: 614px; }
.w2 img { width: 1167px; height: 1474px; left: -307px; top: -307px; }
""", f"""
      <div class="hl f3-hl">{lines('', ['Every face,', 'one person.'])}</div>
      <div class="win w1 f3-w1"><div class="cl">{mimgs}</div><div class="cr">{mimgs}</div></div>
      <div class="sub f3-sub">One photo of someone finds every frame of them.</div>
      <div class="win w2 f3-w2"><img src="assets/captures/app-people.jpg" alt=""></div>
""", reveal('.f3-hl', 0.05) + fade('.f3-w1', 0.6, 0.7, 24) + fade('.f3-sub', 3.0) + fade('.f3-w2', 2.6, 0.7, 24) + """
['.cl', '.cr'].forEach(function (side) {
  var m = q(side + ' .mcap');
  [1.6, 1.85, 2.05, 2.3].forEach(function (t, i) {
    tl.set(m[i + 1], { opacity: 1 }, t);
    tl.set(m[i], { opacity: 0 }, t + 0.001);
  });
});
""" + BLOB_TL)

# ---------------------------------------------------------------- 04 video
# replica-drone 2560x1522; show grid + inspector: replica px x 140..1800, y 90..760 -> 2x 280..3600? no: replica is 1280 wide so 2x = 2560.
# region x 140..1280 (2x 280..2560, 2280 wide) -> at 860 wide scale 0.377 -> too small. Take x 140..900 (grid + start of inspector) 2x 280..1800 = 1520 wide -> scale 0.566; y 40..600 (2x 80..1200) -> 634 tall
frame("04-video", 3.0, """
.hl { top: 280px; font-size: 84px; }
.sub { top: 1040px; font-size: 30px; }
.w1 { left: 70px; top: 480px; width: 860px; height: 530px; }
.w1 img { width: 1167px; height: 1474px; left: -307px; top: -69px; }
.w2 { left: 70px; top: 1150px; width: 860px; height: 406px; }
.w2 img { width: 1930px; height: 1148px; left: -226px; top: -100px; }
""", f"""
      <div class="hl f4-hl">{lines('', ['Photos', 'and video.'])}</div>
      <div class="win w1 f4-w1"><img src="assets/captures/app-videos.jpg" alt=""></div>
      <div class="sub f4-sub">A clip is cut into scenes. Every scene is searchable.</div>
      <div class="win w2 f4-w2"><img src="assets/captures/replica-drone.jpg" alt=""></div>
""", reveal('.f4-hl', 0.05) + fade('.f4-w1', 0.3, 0.7, 24) + fade('.f4-sub', 1.4) + fade('.f4-w2', 1.2, 0.7, 24) + """
tl.fromTo(one('.f4-w1 img'), { scale: 1 }, { scale: 1.05, duration: D, ease: 'none', transformOrigin: '30% 20%' }, 0);
""" + BLOB_TL)

# ---------------------------------------------------------------- 05 numbers
rows = [("3,677 photos, 147 GB", 0.24, "10 min 42 s"),
        ("955 items, 800 GB, photos and 4K clips", 1.0, "44 min 36 s"),
        ("One 4K drone clip, 23 s", 0.03, "1.0 s")]
rows_html = "".join(f"""
        <div class="row r{i}"><div class="k">{k}</div>
          <div class="tr"><i class="track"><b class="fill" style="--p:{p}"></b></i><span class="v">{v}</span></div></div>""" for i, (k, p, v) in enumerate(rows))
frame("05-numbers", 6.0, """
.block { top: 240px; height: 1340px; background: #fbefb8; }
.hl { left: 100px; width: 880px; top: 320px; font-size: 80px; }
.sub { left: 100px; width: 880px; top: 520px; font-size: 32px; color: #4f545d; }
.rows { position: absolute; left: 100px; width: 830px; top: 680px; }
.row { position: relative; height: 230px; opacity: 0; }
.k { font-size: 32px; color: #26282d; margin-bottom: 26px; }
.tr { display: flex; align-items: center; gap: 28px; }
.track { display: block; flex: 1; height: 16px; border-radius: 8px; background: rgba(38,40,45,0.10); overflow: hidden; }
.fill { display: block; width: 100%; height: 100%; border-radius: 8px; background: #26282d; transform-origin: left center; transform: scaleX(0.001); }
.v { display: block; width: 250px; text-align: right; font-size: 40px; font-weight: 600; font-variant-numeric: tabular-nums; color: #26282d; }
.close { position: absolute; left: 100px; width: 880px; top: 1400px; font-size: 30px; line-height: 1.4; color: #4f545d; }
""", f"""__NOBLOBS__
      <div class="ground"></div>
      <div class="blob b1" data-layout-allow-overflow></div><div class="blob b2" data-layout-allow-overflow></div><div class="blob b4" data-layout-allow-overflow></div>
      <div class="block f5-block"></div>
      <div class="hl f5-hl">{lines('', ['A five-year-old', 'Mac is enough.'])}</div>
      <div class="sub f5-sub">Every number measured on a 2020 MacBook Air, M1, 8 GB, the base model.</div>
      <div class="rows f5-rows">{rows_html}</div>
      <div class="close f5-close">No GPU farm. No upload. No subscription for compute.</div>
""", """
tl.fromTo(one('.f5-block'), { opacity: 0, scale: 0.98 }, { opacity: 1, scale: 1, duration: 0.7, ease: 'power3.out', transformOrigin: '50% 50%' }, 0);
""" + reveal('.f5-hl', 0.25) + fade('.f5-sub', 0.9) + """
var rows = q('.row');
var seconds = [642, 2676, 1.0];
var fmt = [function (s) { return Math.floor(s / 60) + ' min ' + String(Math.round(s % 60)).padStart(2, '0') + ' s'; },
           function (s) { return Math.floor(s / 60) + ' min ' + String(Math.round(s % 60)).padStart(2, '0') + ' s'; },
           function (s) { return s.toFixed(1) + ' s'; }];
rows.forEach(function (row, i) {
  var at = 1.3 + i * 1.3;
  var fill = row.querySelector('.fill'), v = row.querySelector('.v');
  var p = parseFloat(fill.style.getPropertyValue('--p'));
  tl.fromTo(row, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out' }, at);
  tl.fromTo(fill, { scaleX: 0.001 }, { scaleX: p, duration: 1.1, ease: 'power3.out' }, at + 0.15);
  var st = { s: 0 };
  tl.to(st, { s: seconds[i], duration: 1.1, ease: 'power3.out', onUpdate: function () { v.textContent = fmt[i](st.s); } }, at + 0.15);
});
""" + fade('.f5-close', 5.1) + """
tl.fromTo(q('.b1'), { x: 0, y: 0 }, { x: 70, y: 40, duration: D, ease: 'sine.inOut' }, 0);
tl.fromTo(q('.b2'), { x: 0, y: 0 }, { x: -60, y: 50, duration: D, ease: 'sine.inOut' }, 0);
tl.fromTo(q('.b4'), { x: 0, y: 0 }, { x: -40, y: -30, duration: D, ease: 'sine.inOut' }, 0);
""")

# ---------------------------------------------------------------- 06 deliver
flows = ["flow-1", "flow-3", "flow-5", "flow-8"]
fimgs = "".join(f'<img class="fcap f{i}" src="assets/captures/{c}.jpg" alt="">' for i, c in enumerate(flows))
frame("06-deliver", 4.5, """
.block { top: 240px; height: 1340px; background: #cfe4fb; }
.hl { left: 100px; width: 880px; top: 310px; font-size: 76px; }
.flow { position: absolute; left: 140px; top: 520px; width: 800px; height: 967px; }
.flow img { position: absolute; left: 0; top: 0; width: 800px; height: 967px; opacity: 0; }
.flow img.f0 { opacity: 1; }
.sub { left: 100px; width: 880px; top: 1496px; font-size: 30px; }
""", f"""__NOBLOBS__
      <div class="ground"></div>
      <div class="blob b3" data-layout-allow-overflow></div><div class="blob b5" data-layout-allow-overflow></div>
      <div class="block f6-block"></div>
      <div class="hl f6-hl">{lines('', ['Send the client', 'their frames tonight.'])}</div>
      <div class="flow f6-flow">{fimgs}</div>
      <div class="sub f6-sub">A folder on any disk, or straight into their Drive.</div>
""", """
tl.fromTo(one('.f6-block'), { opacity: 0, scale: 0.98 }, { opacity: 1, scale: 1, duration: 0.7, ease: 'power3.out', transformOrigin: '50% 50%' }, 0);
""" + reveal('.f6-hl', 0.25) + fade('.f6-flow', 0.7, 0.7, 24) + fade('.f6-sub', 3.2) + """
var f = q('.fcap');
[1.6, 2.5, 3.4].forEach(function (t, i) {
  tl.set(f[i + 1], { opacity: 1 }, t);
  tl.set(f[i], { opacity: 0 }, t + 0.001);
});
tl.fromTo(q('.b3'), { x: 0, y: 0 }, { x: 50, y: -40, duration: D, ease: 'sine.inOut' }, 0);
tl.fromTo(q('.b5'), { x: 0, y: 0 }, { x: 40, y: -20, duration: D, ease: 'sine.inOut' }, 0);
""")

# ---------------------------------------------------------------- 07 private
frame("07-private", 2.5, """
.mark { position: absolute; left: 480px; top: 620px; width: 120px; height: 120px; }
.hl { top: 790px; font-size: 92px; }
.sub { top: 1010px; }
""", f"""
      <img class="mark f7-mark" src="assets/brand/mark.svg" alt="">
      <div class="hl c f7-hl">{lines('', ['Nothing leaves', 'the Mac.'])}</div>
      <div class="sub c f7-sub">Offline. No upload. No account needed to sort.</div>
""", fade('.f7-mark', 0.05, 0.7, 12) + reveal('.f7-hl', 0.3) + fade('.f7-sub', 1.3) + BLOB_TL)

# ---------------------------------------------------------------- 08 end
frame("08-end", 3.5, """
.lockup { position: absolute; left: 0; width: 1080px; top: 560px; text-align: center; }
.lockup .mk { display: block; width: 200px; height: 200px; margin: 0 auto 36px; }
.lockup .wm { display: block; width: 420px; height: 117px; margin: 0 auto; }
.badge { position: absolute; left: 50%; top: 1000px; margin-left: -230px; width: 460px; height: 56px; display: flex; align-items: center; justify-content: center; gap: 14px;
  font-size: 27px; font-weight: 600; color: #26282d; background: #cfeee0; border-radius: 28px; white-space: nowrap; }
.badge i { width: 12px; height: 12px; border-radius: 50%; background: #2f8f6a; box-shadow: 0 0 0 5px rgba(47,143,106,0.18); }
.url { position: absolute; left: 70px; width: 940px; top: 1120px; text-align: center; font-size: 46px; font-weight: 600; color: #26282d; }
.sub { top: 1215px; }
""", """
      <div class="lockup f8-lockup"><img class="mk" src="assets/brand/mark.svg" alt=""><img class="wm" src="assets/brand/wordmark.svg" alt=""></div>
      <div class="badge f8-badge"><i></i>public beta, testing right now</div>
      <div class="url f8-url">kirtan-00.github.io/sorted</div>
      <div class="sub c f8-sub">Runs on a Mac. Be first to run it.</div>
""", """
tl.fromTo(one('.f8-lockup'), { opacity: 0, scale: 0.96 }, { opacity: 1, scale: 1, duration: 0.9, ease: 'power3.out', transformOrigin: '50% 50%' }, 0.05);
tl.fromTo(one('.f8-badge'), { opacity: 0, scale: 0.9 }, { opacity: 1, scale: 1, duration: 0.6, ease: 'back.out(1.4)', transformOrigin: '50% 50%' }, 0.9);
""" + fade('.f8-url', 1.5) + fade('.f8-sub', 2.0) + BLOB_TL)
