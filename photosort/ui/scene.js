/* sorted: the Scan tab's desktop. Two Finder windows cascade on the left (Photos behind, Videos in front), the shoot
   sits on the right as a macOS Stack named after its folder, a few loose files line the right edge, and one Liquid
   Glass pill says what the scan is doing. Ported from growth/scan-moment/finder take 3 (kit.js), fed with real data:

   GET /api/progress        the stage and its counter, which only the pill reads
   GET /api/scan/sample     about 120 of the shoot's files (the cards) and the true per-folder counts

   Every motion is a pure function of the scene clock and a few per-card times, and each time is set once, when the
   data says it happened: a card's thumb arriving (the read pass flips it under the Stack), its embedding arriving
   (the look pass flips it again), its folder arriving (it flies there). A tab opened mid-scan, a paused scan and a
   resumed one all redraw from the endpoint: what had already happened before the first fetch is settled, not
   replayed. Reduced motion settles everything, always.

   The windows behave like Finder windows: drag them by the toolbar, click one to bring it forward, the yellow or red
   light minimizes it into a tile on the bottom edge (cards bound for it land on the tile), green makes it larger.
   Their places are remembered per viewer in localStorage.

   Products: when the sample carries products [{id, name, count}] and rows carry product, and two or more products have
   been found, each gets a Finder folder on the desktop and its cards fly there instead of waiting for the categories.
   A rename arrives as a new name in a later sample. With no products field none of this runs.

   ScanScene.feed(sample, progress) drives the same scene from another page (the live shoot) with nothing polled: the
   sample may add key (one per session), bare (no Finder windows, folders across the whole desktop), pill ([line 1,
   line 2]) and a thumb URL per row. */
"use strict";
(function () {
  var W = 1440, H = 900;
  var LS_KEY = "sorted.scanDesktop.v1";
  var REDUCE = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var NEG = -1e9;

  /* ---------- maths ---------- */
  function clamp(x, a, b) { a = a === undefined ? 0 : a; b = b === undefined ? 1 : b; return Math.min(b, Math.max(a, x)); }
  function lerp(a, b, p) { return a + (b - a) * p; }
  function smooth(a, b, x) { var p = clamp((x - a) / (b - a)); return p * p * (3 - 2 * p); }
  function rng(seed) { var s = seed >>> 0; return function () { s = (s + 0x6D2B79F5) >>> 0; var t = s; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  /* closed-form spring step response: 0 at rest, settles at 1. response in s, bounce 0..0.5 */
  function spring(t, response, bounce) {
    bounce = bounce || 0;
    if (t <= 0) return 0;
    var w = 2 * Math.PI / response, z = 1 - bounce;
    if (z < 1) { var wd = w * Math.sqrt(1 - z * z); return 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + (z * w / wd) * Math.sin(wd * t)); }
    return 1 - Math.exp(-w * t) * (1 + w * t);
  }
  /* impulse response of a damped spring: a quick swell that settles back to 0 */
  function kick(t, response, bounce) {
    if (t <= 0) return 0;
    var w = 2 * Math.PI / response, z = 1 - bounce, wd = w * Math.sqrt(1 - z * z);
    return Math.exp(-z * w * t) * Math.sin(wd * t);
  }
  function fmt(n) { return Math.round(n).toLocaleString("en-US"); }
  function items(n) { return fmt(n) + (Math.round(n) === 1 ? " item" : " items"); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  /* ---------- the layout, in stage pixels (take 3) ---------- */
  var S = 118;                                     // card size
  var PILE = { x: 1050, y: 360, spreadX: 10, spreadY: 5, rot: 5, rise: .22, lift: 22 };
  var LOOSE = [[1366, 96], [1366, 214], [1366, 332], [1366, 450], [1366, 568], [1366, 686], [1262, 568], [1262, 686], [1262, 804], [1366, 804]];
  var LOOSE_S = .56;
  var HUD = { x: 886, y: 560, w: 328, h: 58 };
  var WINS = {
    photos: { title: "Photos", x: 36, y: 34, w: 740, h: 396, cap: 15, wide: true, delay: 0 },
    videos: { title: "Videos", x: 184, y: 418, w: 640, h: 396, cap: 12, wide: false, delay: .1 },
  };
  var CELL = 104, PITCH = 108, GRID_TOP = 60, GRID_PAD = 14, SIDE = 166;
  /* product folders: the free desktop above and below the Stack and pill; with no windows, the whole left of it */
  var PSLOTS = [], PSLOTS_BARE = [];
  [78, 186, 650, 758].forEach(function (y) { [880, 990, 1100, 1190].forEach(function (x, i) { PSLOTS.push([y > 500 && i === 3 ? 1196 : x, y]); }); });
  [90, 220, 350, 480, 610, 740].forEach(function (y) { [120, 240, 360, 480, 600, 720].forEach(function (x) { PSLOTS_BARE.push([x, y]); }); });
  var TUCK = .62, FLY = { response: .66, bounce: .1 }, ARC = 70, END_S = .3;
  var T_ARR = 0; for (var x = 0; x < 3; x += .002) if (spring(x, FLY.response, FLY.bounce) >= .965) { T_ARR = x; break; }

  /* ---------- folder icon: our own drawing (Apple's licence covers theirs), proportions measured off Tahoe ---------- */
  var gid = 0;
  function folderSVG() {
    var id = "fsf" + (gid++);
    return '<svg viewBox="0 0 126 100" class="back"><defs><linearGradient id="' + id + 'b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#77bde9"/><stop offset="1" stop-color="#5a9fd4"/></linearGradient></defs>' +
      '<path d="M4 13a9 9 0 0 1 9-9h25.5c4 0 6.4 1.4 8.9 4.3l2.9 3.4H113a9 9 0 0 1 9 9V89a9 9 0 0 1-9 9H13a9 9 0 0 1-9-9z" fill="url(#' + id + 'b)"/>' +
      '<path d="M4.6 13a8.4 8.4 0 0 1 8.4-8.4h25.5" stroke="rgba(255,255,255,.28)" stroke-width="1" fill="none"/><rect x="10" y="17.5" width="106" height="40" rx="4.5" fill="#f3f6f9"/></svg>' +
      '<svg viewBox="0 0 126 100" class="front"><defs><linearGradient id="' + id + 'f" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8fd0f5"/><stop offset=".55" stop-color="#78bfec"/><stop offset="1" stop-color="#63acde"/></linearGradient></defs>' +
      '<rect x="4" y="23" width="118" height="75" rx="8.5" fill="url(#' + id + 'f)"/><rect x="6.5" y="23.4" width="113" height="1.2" rx=".6" fill="#fff" opacity=".55"/>' +
      '<rect x="4" y="96.6" width="118" height="1.4" rx=".7" fill="#3f86bd" opacity=".35"/></svg>';
  }
  var ICON = {
    desktop: '<svg viewBox="0 0 18 18"><rect x="2" y="3" width="14" height="9.5" rx="1.8"/><path d="M6.5 15.5h5M9 12.5v3"/></svg>',
    doc: '<svg viewBox="0 0 18 18"><path d="M4.5 2.5h6l3 3v10h-9z"/><path d="M10.5 2.5v3h3"/></svg>',
    folder: '<svg viewBox="0 0 18 18"><path d="M2.5 5a1.5 1.5 0 0 1 1.5-1.5h3.2l1.6 1.8H14a1.5 1.5 0 0 1 1.5 1.5v6.7A1.5 1.5 0 0 1 14 15H4a1.5 1.5 0 0 1-1.5-1.5z"/><path d="M2.5 7.3h13"/></svg>',
    drive: '<svg viewBox="0 0 18 18"><rect x="2.5" y="5" width="13" height="8" rx="2"/><circle cx="12.6" cy="9" r=".6" fill="#8f8f93"/></svg>',
    cloud: '<svg viewBox="0 0 18 18"><path d="M5.2 13.5a3 3 0 0 1-.3-6 4.3 4.3 0 0 1 8.3 1.1 2.5 2.5 0 0 1 0 4.9z"/></svg>',
  };
  var TB = {
    back: '<svg viewBox="0 0 16 16"><path d="M10 3 5 8l5 5"/></svg>',
    fwd: '<svg viewBox="0 0 16 16"><path d="m6 3 5 5-5 5"/></svg>',
    grid: '<svg viewBox="0 0 16 16"><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1.2"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1.2"/><rect x="9" y="9" width="4.5" height="4.5" rx="1.2"/></svg>',
    list: '<svg viewBox="0 0 16 16"><path d="M5.5 4h8M5.5 8h8M5.5 12h8"/><circle cx="2.6" cy="4" r=".4"/><circle cx="2.6" cy="8" r=".4"/><circle cx="2.6" cy="12" r=".4"/></svg>',
    cols: '<svg viewBox="0 0 16 16"><rect x="2" y="3" width="12" height="10" rx="1.6"/><path d="M6 3v10M10 3v10"/></svg>',
    search: '<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.3"/><path d="m10.2 10.2 3.3 3.3"/></svg>',
    more: '<svg viewBox="0 0 16 16"><circle cx="3.5" cy="8" r=".6"/><circle cx="8" cy="8" r=".6"/><circle cx="12.5" cy="8" r=".6"/></svg>',
  };
  var GLYPH = [
    '<svg viewBox="0 0 14 14"><path d="M4.6 4.6l4.8 4.8M9.4 4.6l-4.8 4.8"/></svg>',
    '<svg viewBox="0 0 14 14"><path d="M3.8 7h6.4"/></svg>',
    '<svg viewBox="0 0 14 14"><path d="M7 3.8v6.4M3.8 7h6.4"/></svg>',
  ];

  /* ---------- card sprites: each thumbnail is drawn once; the frame loop only blits ---------- */
  function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  function geom(kind) {
    if (kind === "video") return { w: S, h: Math.round(S * 9 / 16) };
    if (kind === "portrait") return { w: Math.round(S * 2 / 3), h: S };
    return { w: S, h: Math.round(S * 2 / 3) };
  }
  function shapeOf(c) {
    if (c.video) return "video";
    var w = c.img ? c.img.naturalWidth : c.w, h = c.img ? c.img.naturalHeight : c.h;
    return w && h && h > w * 1.05 ? "portrait" : "photo";
  }
  function durLabel(d) { if (!(d > 0)) return null; d = Math.round(d); return Math.floor(d / 60) + ":" + String(d % 60).padStart(2, "0"); }
  function imageSprite(c, k) {
    var g = geom(shapeOf(c)), cv = document.createElement("canvas");
    cv.width = Math.ceil(g.w * k); cv.height = Math.ceil(g.h * k);
    var x = cv.getContext("2d"); x.scale(k, k); x.imageSmoothingQuality = "high";
    roundRect(x, 0, 0, g.w, g.h, 3); x.save(); x.clip();
    var iw = c.img.naturalWidth, ih = c.img.naturalHeight, ar = g.w / g.h, sw = iw, sh = ih;
    if (iw / ih > ar) sw = ih * ar; else sh = iw / ar;
    x.drawImage(c.img, (iw - sw) / 2, (ih - sh) / 2, sw, sh, 0, 0, g.w, g.h);
    var dl = c.video && durLabel(c.duration);
    if (c.video) {
      var fs = Math.max(8, S * .082); x.font = "600 " + fs + "px -apple-system, \"SF Pro Text\", sans-serif";
      var label = dl || "", tw = label ? x.measureText(label).width : 0, gl = fs * .62, bw = (label ? tw + fs * .3 : 0) + fs * 1.0 + gl * .6, bh = fs * 1.5;
      var bx = g.w - bw - S * .04, by = g.h - bh - S * .04;
      x.fillStyle = "rgba(0,0,0,.55)"; roundRect(x, bx, by, bw, bh, bh / 2); x.fill();
      x.fillStyle = "#fff"; var px = bx + fs * .5, py = by + bh / 2;
      x.beginPath(); x.moveTo(px, py - gl * .55); x.lineTo(px + gl * .9, py); x.lineTo(px, py + gl * .55); x.closePath(); x.fill();
      if (label) { x.textBaseline = "middle"; x.fillText(label, px + gl + fs * .3, py + .5); }
    }
    x.restore();
    x.strokeStyle = "rgba(255,255,255,.2)"; x.lineWidth = .75; roundRect(x, .375, .375, g.w - .75, g.h - .75, 3); x.stroke();
    return { cv: cv, w: g.w, h: g.h };
  }
  /* a file not read yet: a blank print with a faint photo or clip glyph, nothing pretending to be its picture */
  function blankSprite(kind, k) {
    var g = geom(kind), cv = document.createElement("canvas");
    cv.width = Math.ceil(g.w * k); cv.height = Math.ceil(g.h * k);
    var x = cv.getContext("2d"); x.scale(k, k);
    var gr = x.createLinearGradient(0, 0, 0, g.h); gr.addColorStop(0, "#34373c"); gr.addColorStop(1, "#2a2c30");
    roundRect(x, 0, 0, g.w, g.h, 3); x.fillStyle = gr; x.fill();
    x.strokeStyle = "rgba(255,255,255,.16)"; x.lineWidth = .75; roundRect(x, .375, .375, g.w - .75, g.h - .75, 3); x.stroke();
    x.save(); x.translate(g.w / 2, g.h / 2); x.strokeStyle = "rgba(255,255,255,.2)"; x.fillStyle = "rgba(255,255,255,.2)"; x.lineWidth = 1.3; x.lineJoin = "round";
    if (kind === "video") { x.beginPath(); x.moveTo(-5, -7); x.lineTo(7, 0); x.lineTo(-5, 7); x.closePath(); x.fill(); }
    else { roundRect(x, -11, -8, 22, 16, 2.5); x.stroke(); x.beginPath(); x.moveTo(-8, 5); x.lineTo(-2, -1); x.lineTo(2, 3); x.lineTo(5, 0); x.lineTo(8, 5); x.stroke(); x.beginPath(); x.arc(4, -3.5, 1.6, 0, 7); x.fill(); }
    x.restore();
    return { cv: cv, w: g.w, h: g.h };
  }
  function shadowSprite(w, h, blur, k) {
    var pad = blur * 2.2, cv = document.createElement("canvas");
    cv.width = Math.ceil((w + pad * 2) * k); cv.height = Math.ceil((h + pad * 2) * k);
    var c = cv.getContext("2d"); c.scale(k, k);
    var off = 10000;
    c.shadowColor = "rgba(0,0,0,1)"; c.shadowBlur = blur * k; c.shadowOffsetX = off * k; c.shadowOffsetY = 0;
    c.fillStyle = "#000"; roundRect(c, pad - off, pad + 1, w, h - 2, 4); c.fill();
    return { cv: cv, pad: pad };
  }

  /* ---------- the scene ---------- */
  var root, stage, fx, ctx, fxLow, ctxLow, hud, hudL1, hudL2, pie, tick, ring, stackLabel, built = false;
  var scale = 1, ox = 0, oy = 0, vis = { x0: 0, y0: 0, x1: W, y1: H }, spriteK = 2, dpr = 1;
  var wins = {}, folders = {}, folderList = [];
  var desk = { id: "desk", u: 0 }, products = [], productIds = {}, pNext = NEG, pMin = 2, external = false, bare = false, pillOverride = null;
  var cards = [], pile = [], loose = [], byId = {}, looseLabels = [];
  var sample = null, prog = { stage: "idle", running: false }, shoot = "", disk = null, sceneKey = null;
  var maxId = 0, foldersAt = Infinity, foldersSeen = false, lastFlyT = NEG, lastTuck = NEG, lastPull = NEG;
  var opened = NEG, active = false, raf = 0, pollTimer = 0, inflight = false, wasRunning = null, frozen = null;
  var blanks = {}, shadows = {};
  var stats = { frames: [], render: [], last: 0 };
  window.__sceneStats = stats;
  var log = []; window.__sceneLog = log;

  function clock() { return frozen !== null ? frozen : performance.now() / 1000; }

  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html) e.innerHTML = html; return e; }

  function build() {
    root = document.getElementById("finder-scene");
    if (!root) return false;
    stage = el("div", "fs-stage"); root.appendChild(stage);
    // two canvases: the desktop (the Stack and the loose files) sits under the windows, cards in flight over them
    fxLow = el("canvas", "fs-fx low"); stage.appendChild(fxLow); ctxLow = fxLow.getContext("2d");
    fx = el("canvas", "fs-fx"); stage.appendChild(fx); ctx = fx.getContext("2d");
    for (var id in WINS) wins[id] = makeWindow(id, WINS[id]);
    loadPlaces();
    /* the pill */
    hud = el("div", "fs-hud");
    Object.assign(hud.style, { left: HUD.x + "px", top: HUD.y + "px", width: HUD.w + "px", height: HUD.h + "px" });
    var inner = '<div class="fs-hud-in"><svg viewBox="0 0 30 30"><circle cx="15" cy="15" r="11" fill="none" stroke="rgba(255,255,255,.18)" stroke-width="2.6"/>' +
      '<path class="ring" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="2.6" stroke-linecap="round" d="M15 4A11 11 0 0 1 26 15" opacity="0"/>' +
      '<path class="pie" fill="none" stroke="rgba(255,255,255,.92)" stroke-width="2.6" stroke-linecap="round" d=""/>' +
      '<path class="tick" d="M9.6 15.4l3.6 3.5 7.2-7.6" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" opacity="0"/></svg>' +
      '<div><div class="l1"></div><div class="l2"></div></div></div>';
    if (window.customElements && customElements.get("glass-element")) {
      hud.innerHTML = '<glass-element width="' + HUD.w + '" height="' + HUD.h + '" radius="' + (HUD.h / 2) + '" depth="12" strength="72" blur="2" chromatic-aberration="2" background-color="rgba(30,32,36,.30)">' + inner + "</glass-element>";
      requestAnimationFrame(function () { requestAnimationFrame(styleGlass); });
    } else {
      hud.innerHTML = '<div class="fs-plain">' + inner + "</div>";
      // the module may still be loading: swap the plain pill for glass once it is defined
      if (window.customElements) customElements.whenDefined("glass-element").then(function () { var l1 = hudL1.textContent, l2 = hudL2.textContent; hud.remove(); built = false; buildHud(l1, l2); });
    }
    stage.appendChild(hud);
    bindHud();
    stackLabel = el("div", "fs-dlabel"); stackLabel.style.left = PILE.x + "px"; stackLabel.style.top = (PILE.y + 86) + "px"; stage.appendChild(stackLabel);
    for (var i = 0; i < LOOSE.length; i++) {
      var l = el("div", "fs-dlabel"); l.style.left = LOOSE[i][0] + "px"; l.style.top = (LOOSE[i][1] + 34) + "px"; l.style.opacity = "0"; stage.appendChild(l); looseLabels.push(l);
    }
    for (id in wins) { var b = el("div", "fs-badge"); b.hidden = true; stage.appendChild(b); wins[id].badge = b; }
    new ResizeObserver(fit).observe(root);
    fit();
    built = true;
    return true;
  }
  function buildHud(l1, l2) {   // glass arrived after the plain pill: rebuild just the pill
    var inner = hud.querySelector(".fs-hud-in").outerHTML;
    hud = el("div", "fs-hud");
    Object.assign(hud.style, { left: HUD.x + "px", top: HUD.y + "px", width: HUD.w + "px", height: HUD.h + "px" });
    hud.innerHTML = '<glass-element width="' + HUD.w + '" height="' + HUD.h + '" radius="' + (HUD.h / 2) + '" depth="12" strength="72" blur="2" chromatic-aberration="2" background-color="rgba(30,32,36,.30)">' + inner + "</glass-element>";
    stage.appendChild(hud); bindHud(); hudL1.textContent = l1; hudL2.textContent = l2; lastL1 = l1; lastL2 = l2; lastPie = -2;
    requestAnimationFrame(function () { requestAnimationFrame(styleGlass); });
    built = true;
  }
  function bindHud() {
    hudL1 = hud.querySelector(".l1"); hudL2 = hud.querySelector(".l2"); pie = hud.querySelector(".pie"); tick = hud.querySelector(".tick"); ring = hud.querySelector(".ring");
  }
  function styleGlass() {
    var g = hud.querySelector("glass-element"), box = g && g.shadowRoot && g.shadowRoot.querySelector(".glass-box");
    if (box) { box.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,.30), inset 0 -1px 0 rgba(255,255,255,.08), inset 0 0 0 .5px rgba(255,255,255,.16), 0 10px 30px rgba(0,0,0,.35), 0 1px 3px rgba(0,0,0,.3)"; box.style.cursor = "default"; box.style.transition = "none"; }
  }

  /* the stage keeps its 1440 x 900 layout and is scaled to the panel; whatever the panel shows beyond it (a wide
     window letterboxes left and right) is still desktop, and the windows may be dragged into it */
  function fit() {
    var r = root.getBoundingClientRect(), w = r.width, h = r.height;
    if (!w || !h) return;
    scale = Math.min(w / W, h / H); ox = (w - W * scale) / 2; oy = (h - H * scale) / 2;
    stage.style.transform = "translate(" + ox + "px," + oy + "px) scale(" + scale + ")";
    vis = { x0: -ox / scale, y0: -oy / scale, x1: (w - ox) / scale, y1: (h - oy) / scale };
    dpr = Math.min(2, window.devicePixelRatio || 1);
    var k = dpr * scale;
    [fx, fxLow].forEach(function (cv) {
      cv.style.left = vis.x0 + "px"; cv.style.top = vis.y0 + "px";
      cv.style.width = (vis.x1 - vis.x0) + "px"; cv.style.height = (vis.y1 - vis.y0) + "px";
      cv.width = Math.round((vis.x1 - vis.x0) * k); cv.height = Math.round((vis.y1 - vis.y0) * k); cv._k = k;
    });
    var want = Math.max(1.5, dpr * scale * 1.35);
    if (Math.abs(want - spriteK) / spriteK > .2 || !blanks.photo) { spriteK = want; rebuildSprites(); }
    for (var id in wins) { clampWin(wins[id]); placeWin(wins[id], clock()); }
    wake(1);
  }
  function rebuildSprites() {
    blanks = { photo: blankSprite("photo", spriteK), portrait: blankSprite("portrait", spriteK), video: blankSprite("video", spriteK) };
    shadows = { photo: shadowSprite(geom("photo").w, geom("photo").h, 9, 2), portrait: shadowSprite(geom("portrait").w, geom("portrait").h, 9, 2), video: shadowSprite(geom("video").w, geom("video").h, 9, 2) };
    for (var i = 0; i < cards.length; i++) if (cards[i].imgOK) cards[i].sprite = imageSprite(cards[i], spriteK);
  }

  /* ---------- Finder windows ---------- */
  function makeWindow(id, cfg) {
    var e = el("div", "fs-win" + (id === "videos" ? "" : " inactive"));
    var lights = '<div class="fs-lights"><i>' + GLYPH[0] + "</i><i>" + GLYPH[1] + "</i><i>" + GLYPH[2] + "</i></div>";
    e.innerHTML = '<div class="fs-side">' + lights +
      '<div class="fs-grp first"><h6>Favorites</h6><div class="fs-row">' + ICON.desktop + 'Desktop</div><div class="fs-row">' + ICON.doc + "Documents</div>" +
      '<div class="fs-row' + (id === "photos" ? " sel" : "") + '">' + ICON.folder + 'Photos</div><div class="fs-row' + (id === "videos" ? " sel" : "") + '">' + ICON.folder + "Videos</div></div>" +
      '<div class="fs-grp"><h6>Locations</h6><div class="fs-row fs-disk">' + ICON.drive + 'Macintosh HD</div><div class="fs-row">' + ICON.cloud + "iCloud Drive</div></div></div>" +
      '<div class="fs-main"><div class="fs-tb"><div class="fs-cap"><b>' + TB.back + '</b><span class="sep"></span><b class="dis">' + TB.fwd + "</b></div>" +
      '<div class="fs-ttl">' + cfg.title + '</div><div class="fs-sp"></div>' +
      (cfg.wide ? '<div class="fs-cap"><b class="on">' + TB.grid + "</b><b>" + TB.list + "</b><b>" + TB.cols + '</b></div><div class="fs-cap round">' + TB.more + "</div>"
        : '<div class="fs-cap"><b class="on">' + TB.grid + "</b><b>" + TB.list + "</b></div>") +
      '<div class="fs-cap round">' + TB.search + '</div></div><div class="fs-grid"></div><div class="fs-empty" hidden></div></div>';
    stage.appendChild(e);
    var w = { id: id, el: e, cfg: cfg, grid: e.querySelector(".fs-grid"), empty: e.querySelector(".fs-empty"), diskRow: e.querySelector(".fs-disk"),
      x: cfg.x, y: cfg.y, w: cfg.w, h: cfg.h, w0: cfg.w, h0: cfg.h, big: false, sizeAt: NEG, sizeFrom: null,
      min: false, minAt: NEG, minFrom: 0, minOrder: 0, front: id === "videos", cols: 0, folders: [], _tf: "", _clip: "", _op: "" };
    layoutGrid(w);
    var lightsEl = e.querySelector(".fs-lights");
    lightsEl.children[0].addEventListener("click", function (ev) { ev.stopPropagation(); minimize(w, true); });
    lightsEl.children[1].addEventListener("click", function (ev) { ev.stopPropagation(); minimize(w, true); });
    lightsEl.children[2].addEventListener("click", function (ev) { ev.stopPropagation(); zoom(w); });
    lightsEl.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); toFront(w); });
    e.addEventListener("pointerdown", function (ev) { onDown(w, ev); });
    return w;
  }
  function layoutGrid(w) {
    var cols = Math.max(1, Math.floor((w.w - SIDE - GRID_PAD * 2) / CELL));
    if (cols !== w.cols) { w.cols = cols; w.grid.style.gridTemplateColumns = "repeat(" + cols + ", " + CELL + "px)"; }
    for (var i = 0; i < w.folders.length; i++) {
      var f = w.folders[i], col = i % cols, row = Math.floor(i / cols);
      f.ox = SIDE + GRID_PAD + col * CELL + CELL / 2; f.oy = GRID_TOP + row * PITCH + 8 + 28;
    }
  }
  function toFront(w) {
    for (var id in wins) { var o = wins[id]; o.front = o === w; o.el.classList.toggle("inactive", !o.front); o.el.style.zIndex = o.min ? 15 : (o.front ? 2 : 1); }
    savePlaces(); wake(.2);
  }
  /* toolbar and the strip above the sidebar drag the window; the lights and the folders do not */
  function onDown(w, ev) {
    if (ev.button !== 0) return;
    if (w.min) { ev.preventDefault(); restore(w); return; }
    toFront(w);
    var r = w.el.getBoundingClientRect(), ly = (ev.clientY - r.top) / scale, lx = (ev.clientX - r.left) / scale;
    var onBar = ly < 52 || (lx < SIDE && ly < 46);
    if (!onBar || ev.target.closest(".fs-lights")) return;
    ev.preventDefault();
    var p0 = toStage(ev), x0 = w.x, y0 = w.y, id = ev.pointerId;
    w.el.setPointerCapture(id);
    w.dragging = true;
    function move(e) {
      if (e.pointerId !== id) return;
      var p = toStage(e);
      w.x = x0 + p.x - p0.x; w.y = y0 + p.y - p0.y; clampWin(w);
      placeWin(w, clock());                         // straight away, not on the next frame: the window stays under the pointer
      wake(.3);
    }
    function up(e) {
      if (e.pointerId !== id) return;
      w.dragging = false;
      w.el.removeEventListener("pointermove", move); w.el.removeEventListener("pointerup", up); w.el.removeEventListener("pointercancel", up);
      savePlaces();
    }
    w.el.addEventListener("pointermove", move); w.el.addEventListener("pointerup", up); w.el.addEventListener("pointercancel", up);
  }
  function toStage(ev) { var r = root.getBoundingClientRect(); return { x: (ev.clientX - r.left - ox) / scale, y: (ev.clientY - r.top - oy) / scale }; }
  function clampWin(w) {
    var m = 4;
    w.x = Math.max(vis.x0 + m, Math.min(w.x, vis.x1 - w.w - m));
    w.y = Math.max(vis.y0 + m, Math.min(w.y, vis.y1 - w.h - m));
  }
  function minimize(w, user) {
    if (w.min) return;
    var t = clock();
    w.minFrom = minU(w, t); w.min = true; w.minAt = t; w.minOrder = t;
    w.el.classList.add("min"); w.el.style.zIndex = 15;
    if (user) {   // the next window down becomes the active one, as it does on a Mac
      for (var id in wins) if (wins[id] !== w && !wins[id].min) { toFront(wins[id]); break; }
      savePlaces();
    }
    wake(1.2);
  }
  function restore(w) {
    if (!w.min) return;
    var t = clock();
    w.minFrom = minU(w, t); w.min = false; w.minAt = t;
    w.el.classList.remove("min");
    toFront(w);
    wake(1.2);
  }
  function zoom(w) {
    var t = clock();
    w.sizeFrom = { w: w.w, h: w.h }; w.sizeAt = t; w.big = !w.big;
    var tw = w.big ? Math.round(w.w0 * 1.22) : w.w0, th = w.big ? Math.round(w.h0 * 1.22) : w.h0;
    w.sizeTo = { w: Math.min(tw, vis.x1 - vis.x0 - 8), h: Math.min(th, vis.y1 - vis.y0 - 8) };
    toFront(w); savePlaces(); wake(1);
  }
  /* minimize progress: 0 open, 1 in the tile; a spring from wherever it was when the light was pressed */
  function minU(w, t) {
    var target = w.min ? 1 : 0;
    if (REDUCE) return target;
    var p = spring(t - w.minAt, .45, 0);
    return p >= .999 ? target : lerp(w.minFrom, target, p);
  }
  function tileRect(w) {
    var th = 78, list = [], id;
    for (id in wins) if (wins[id].min) list.push(wins[id]);
    if (!w.min && list.indexOf(w) < 0) list.push(w);   // a window on its way out of its tile keeps the slot it had
    list.sort(function (a, b) { return a.minOrder - b.minOrder; });
    var total = 0, gap = 14;
    list.forEach(function (o) { total += th * o.w / o.h; }); total += gap * (list.length - 1);
    var x = (vis.x0 + vis.x1) / 2 - total / 2, out = null;
    list.forEach(function (o) { var tw = th * o.w / o.h; if (o === w) out = { x: x, y: vis.y1 - th - 14, w: tw, h: th }; x += tw + gap; });
    return out;
  }
  /* where the window is drawn at time t: dragged place, the entrance, the zoom and the genie, all as one transform */
  function placeWin(w, t) {
    if (w.sizeFrom) {
      var ps = REDUCE ? 1 : spring(t - w.sizeAt, .42, .05);
      var nw = Math.round(lerp(w.sizeFrom.w, w.sizeTo.w, ps)), nh = Math.round(lerp(w.sizeFrom.h, w.sizeTo.h, ps));
      if (ps >= .999) { nw = w.sizeTo.w; nh = w.sizeTo.h; w.sizeFrom = null; }
      if (nw !== w.w || nh !== w.h) { w.w = nw; w.h = nh; clampWin(w); layoutGrid(w); }
    }
    var sw = w.w + "px", sh = w.h + "px";
    if (w.el.style.width !== sw) w.el.style.width = sw;
    if (w.el.style.height !== sh) w.el.style.height = sh;
    var e = REDUCE ? 1 : spring(t - opened - w.cfg.delay, .6, 0);
    var u = minU(w, t), tf, clip = "", op = e >= .999 ? "" : String(clamp(e * 1.4));
    var tr = (u > 0 || w.min) ? tileRect(w) : null;
    if (REDUCE && tr && (t - w.minAt) < .3) {        // reduced motion: a quick fade out of one place and into the other
      var q = (t - w.minAt) / .3, showMin = q >= .5 ? w.min : !w.min;
      op = String(q < .5 ? 1 - q * 2 : q * 2 - 1);
      u = showMin ? 1 : 0;
    }
    if (u <= 0.0005) {
      tf = "translate(" + w.x.toFixed(2) + "px," + (w.y + (1 - e) * 14).toFixed(2) + "px)" + (e < .999 ? " scale(" + (.985 + .015 * e).toFixed(4) + ")" : "");
    } else {
      /* genie: the bottom of the window narrows toward the tile first (the clip), then the whole window scales down
         into it, height a little ahead of width, and the narrowing lets go as it lands */
      var st = tr.w / w.w, sty = tr.h / w.h;
      var my = smooth(.12, .92, u), mx = smooth(.3, 1, u);
      var sx = lerp(1, st, mx), sy = lerp(1, sty, my);
      var X = lerp(w.x, tr.x, mx), Y = lerp(w.y, tr.y, my);
      tf = "translate(" + X.toFixed(2) + "px," + Y.toFixed(2) + "px) scale(" + sx.toFixed(4) + "," + sy.toFixed(4) + ")";
      var pinch = smooth(0, .42, u) * (1 - smooth(.62, 1, u));
      if (pinch > .002) {
        var tcx = (tr.x + tr.w / 2 - X) / sx, hw = Math.max(w.w * .16, tr.w / 2 / sx);
        tcx = clamp(tcx, hw, w.w - hw);
        var L = [], R = [], n = 14;
        for (var i = 0; i <= n; i++) {
          var v = i / n, f = pinch * Math.pow(smooth(0, 1, v), 1.4);
          L.push(lerp(0, tcx - hw, f).toFixed(1) + "px " + (v * w.h).toFixed(1) + "px");
          R.unshift(lerp(w.w, tcx + hw, f).toFixed(1) + "px " + (v * w.h).toFixed(1) + "px");
        }
        clip = "polygon(" + L.concat(R).join(",") + ")";
      }
    }
    if (w._tf !== tf) { w.el.style.transform = tf; w._tf = tf; }
    if (w._clip !== clip) { w.el.style.clipPath = clip; w._clip = clip; }
    if (w._op !== op) { w.el.style.opacity = op; w._op = op; }
    /* where its folders are right now, in stage pixels: cards in flight steer by this every frame */
    w.u = u; w.tile = tr;
    for (var j = 0; j < w.folders.length; j++) {
      var fo = w.folders[j];
      if (u > .5 && tr) { fo.cx = tr.x + tr.w / 2; fo.cy = tr.y + tr.h / 2; }
      else { fo.cx = w.x + fo.ox; fo.cy = w.y + fo.oy; }
    }
  }
  function savePlaces() {
    try {
      var o = {};
      for (var id in wins) { var w = wins[id]; o[id] = { x: Math.round(w.x), y: Math.round(w.y), min: w.min, big: w.big, front: w.front, order: w.minOrder }; }
      localStorage.setItem(LS_KEY, JSON.stringify(o));
    } catch (e) { /* private window, blocked storage: the windows just start where take 3 put them */ }
  }
  function loadPlaces() {
    var o = null;
    try { o = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch (e) { o = null; }
    if (!o) return;
    var fronted = null;
    for (var id in wins) {
      var s = o[id], w = wins[id];
      if (!s) continue;
      if (typeof s.x === "number" && isFinite(s.x)) w.x = s.x;
      if (typeof s.y === "number" && isFinite(s.y)) w.y = s.y;
      if (s.big) { w.big = true; w.w = Math.round(w.w0 * 1.22); w.h = Math.round(w.h0 * 1.22); layoutGrid(w); }
      if (s.min) { w.min = true; w.minAt = NEG; w.minFrom = 1; w.minOrder = s.order || 0; w.el.classList.add("min"); w.el.style.zIndex = 15; }
      if (s.front) fronted = w;
    }
    if (fronted && !fronted.min) toFront(fronted);
  }

  /* ---------- data in ---------- */
  function newScene(d, t) {
    // a new listing (another shoot, or a scan that changed what is on the disk): the cards start over
    var R = rng(37);
    cards = []; pile = []; loose = []; byId = {};
    tucks = []; planned = []; n1 = n2 = 0; oc = null; lastTuck = NEG; lastPull = NEG; lastFlyT = NEG; foldersAt = Infinity; foldersSeen = false; maxId = 0;
    for (var id in wins) { wins[id].folders = []; wins[id].grid.innerHTML = ""; }
    folders = {}; folderList = [];
    products.forEach(function (fo) { fo.cell.remove(); }); products = []; productIds = {}; pNext = NEG;
    for (var i = 0; i < looseLabels.length; i++) { looseLabels[i].textContent = ""; looseLabels[i].style.opacity = "0"; looseLabels[i]._o = "0"; }
  }
  function addRows(rows, t, settle, drop) {
    var R = rng(37 + cards.length);
    rows.forEach(function (r) {
      if (byId[r.id]) return;
      var c = { id: r.id, name: r.name, video: !!r.video, w: r.width, h: r.height, duration: r.duration, thumb: r.thumb, product: null, qhash: null, error: false, looked: false,
        category: null, img: null, imgOK: false, imgDone: false, sprite: null, tRead: Infinity, tT1: Infinity, tT2: Infinity, tFly: Infinity, tArr: Infinity,
        tFound: NEG, tPull: Infinity, fkey: null, r: [R(), R(), R(), R(), R(), R()] };
      c.isNew = true; cards.push(c); byId[c.id] = c;
      maxId = Math.max(maxId, r.id);
    });
    // the loose files: up to ten, spread through the sample, so they are read at different moments of the scan
    var n = cards.length, nl = external ? 0 : n >= 24 ? Math.min(LOOSE.length, Math.round(n / 12)) : Math.min(LOOSE.length, Math.floor(n / 4));
    var looseIdx = {};
    for (var j = 0; j < nl; j++) looseIdx[Math.min(n - 1, Math.floor((j + .5) * n / nl))] = j;
    pile = []; loose = [];
    cards.forEach(function (c, i) {
      if (looseIdx[i] !== undefined) { c.loose = true; c.spot = looseIdx[i]; loose[c.spot] = c; }
      else { c.loose = false; c.pi = pile.length; pile.push(c); }
    });
    // rest poses on the Stack
    pile.forEach(function (c, i) {
      c.jx = (c.r[1] - .5) * 2 * PILE.spreadX; c.jy = (c.r[2] - .5) * 2 * PILE.spreadY; c.jr = (c.r[3] - .5) * 2 * PILE.rot;
      if (c.isNew) c.tFound = settle || !drop ? NEG : t + .15 + (pile.length - 1 - i) * Math.min(.03, 1.4 / pile.length) + c.r[4] * .05;
    });
    loose.forEach(function (c, j) {
      if (!c) return;
      c.jr = 0; if (c.isNew) c.tFound = settle || !drop ? NEG : t + .3 + j * .08;
      looseLabels[j].textContent = c.name;
    });
    var inPlan = new Set(planned);
    planned = planned.filter(function (c) { return !c.loose; }).concat(pile.filter(function (c) { return !inPlan.has(c); }));
    oc = null;
    cards.forEach(function (c) { c.isNew = false; });
  }
  function update(u, t, settle) {
    var c = byId[u.id];
    if (!c) return;
    if (u.qhash && !c.qhash) {
      c.qhash = u.qhash; c.tRead = settle ? NEG : t; c.w = u.width || c.w; c.h = u.height || c.h; c.duration = u.duration || c.duration;
      loadImage(c);
    }
    if (u.error && !c.error) { c.error = true; c.tRead = settle ? NEG : t; }
    if (u.looked && !c.looked) { c.looked = true; c.tLook = settle ? NEG : t; }
    if (u.category && !c.category) c.category = u.category;
    if (u.thumb) c.thumb = u.thumb;
    if (u.product !== undefined && u.product !== null) c.product = u.product;
  }
  function loadImage(c) {
    var im = new Image();
    im.decoding = "async";
    im.onload = function () { c.img = im; c.imgOK = true; c.imgDone = true; c.sprite = imageSprite(c, spriteK); wake(1); };
    im.onerror = function () { c.imgDone = true; };
    im.src = c.thumb || "/api/thumb/" + encodeURIComponent(c.qhash) + "?size=grid";
  }

  /* the folders, once categorise has written them: the largest per window up to what the window shows, the rest
     folded into "other", then named in Finder's order */
  function setFolders(list, t, settle) {
    if (!list || !list.length) return;
    var per = { photos: [], videos: [] };
    list.forEach(function (f) { (per[f.kind] || per.photos).push({ name: f.name, n: f.n }); });
    var map = {};
    for (var kind in per) {
      var w = wins[kind], all = per[kind].sort(function (a, b) { return b.n - a.n; }), cap = w.cfg.cap, keep = all;
      if (all.length > cap) {
        keep = all.slice(0, cap - 1);
        var rest = all.slice(cap - 1), other = keep.filter(function (f) { return f.name === "other"; })[0];
        if (!other) { other = { name: "other", n: 0 }; keep.push(other); }
        rest.forEach(function (f) { other.n += f.n; map[kind + "/" + f.name] = kind + "/other"; });
      }
      keep.sort(function (a, b) { return a.name.localeCompare(b.name); });
      if (!foldersSeen) {
        w.grid.innerHTML = ""; w.folders = [];
        keep.forEach(function (f, i) {
          var cell = el("div", "fs-cell", '<div class="fs-hl"></div><div class="fs-ficon">' + folderSVG() + '</div><div class="fs-nm"></div><div class="fs-ct"></div>');
          cell.querySelector(".fs-nm").textContent = f.name;
          w.grid.appendChild(cell);
          var fo = { key: kind + "/" + f.name, name: f.name, win: w, cell: cell, icon: cell.querySelector(".fs-ficon"), front: cell.querySelector(".front"), hl: cell.querySelector(".fs-hl"),
            ct: cell.querySelector(".fs-ct"), final: f.n, per: 0, arr: [], tA: settle ? NEG : t + .1 + i * .045, _ct: null, _tf: "", _f: "", _h: "", _c: "" };
          w.folders.push(fo); folders[fo.key] = fo; folderList.push(fo);
        });
        layoutGrid(w);
      } else {
        keep.forEach(function (f) { var fo = folders[kind + "/" + f.name]; if (fo) fo.final = f.n; });
      }
    }
    for (var k in map) folders[k] = folders[map[k]];
    if (foldersSeen) return;
    foldersSeen = true; foldersAt = t;
    // flights: Stack top first, then any loose file still on the desktop
    var order = stackOrder(t).concat(loose.filter(function (c) { return c && c.tPull === Infinity; }));
    var tNext = Math.max(t + .55, lastTuck + TUCK, pNext);
    var R = rng(911);
    order.forEach(function (c) {
      if (c.fkey) return;                          // already bound for a product folder
      var fo = c.category && folders[(c.video ? "videos/" : "photos/") + c.category];
      if (!fo) return;
      c.fkey = fo.key; fo.per++;
      if (settle || REDUCE) { c.tFly = NEG; c.tArr = NEG; }
      else {
        c.tFly = tNext; tNext += .045 + R() * .03;
        c.tArr = c.tFly + T_ARR;
        var q = poseRest(c, c.tFly - .001);      // where it leaves from, fixed now so it does not slide while waiting
        c.x0 = q.x; c.y0 = q.y; c.rot0 = q.rot; c.s0 = q.s;
      }
      fo.arr.push(c.tArr);
    });
    folderList.forEach(function (fo) { fo.arr.sort(function (a, b) { return a - b; }); });
    lastFlyT = tNext;
    wake(tNext - t + T_ARR + 1);
  }

  /* the products: a folder each on the desktop once there are enough of them, in the order they were found. More than
     the desktop has room for fold into one last folder, as the categories fold into "other". */
  function deskFolder(key, name, slot, t, settle) {
    var cell = el("div", "fs-cell fs-dcell", '<div class="fs-hl"></div><div class="fs-ficon">' + folderSVG() + '</div><div class="fs-nm"></div><div class="fs-ct"></div>');
    cell.style.left = (slot[0] - CELL / 2) + "px"; cell.style.top = (slot[1] - 35) + "px";
    cell.querySelector(".fs-nm").textContent = name;
    stage.appendChild(cell);
    var fo = { key: key, name: name, win: desk, cell: cell, icon: cell.querySelector(".fs-ficon"), front: cell.querySelector(".front"), hl: cell.querySelector(".fs-hl"), nm: cell.querySelector(".fs-nm"),
      ct: cell.querySelector(".fs-ct"), final: 0, per: 0, arr: [], cx: slot[0], cy: slot[1], tA: settle ? NEG : t + .1 + (products.length % 6) * .06, _ct: null, _tf: "", _f: "", _h: "", _c: "" };
    products.push(fo); folders[key] = fo; folderList.push(fo);
    return fo;
  }
  function setProducts(list, t, settle) {
    var slots = bare ? PSLOTS_BARE : PSLOTS, i, fo;
    if (list.length >= pMin || products.length) {
      list.forEach(function (p) {
        var key = "product/" + p.id;
        if (!folders[key]) {
          if (products.length < slots.length - 1 || (products.length === slots.length - 1 && list.length === slots.length)) folders[key] = deskFolder(key, p.name, slots[products.length], t, settle);
          else folders[key] = folders["product/more"] || deskFolder("product/more", "more products", slots[slots.length - 1], t, settle);
        }
      });
      for (i = 0; i < products.length; i++) products[i].final = 0;
      list.forEach(function (p) {
        fo = folders["product/" + p.id];
        fo.final += p.count || 0;
        if (fo.key !== "product/more" && fo.name !== p.name) { fo.name = p.name; fo.nm.textContent = p.name; }
      });
    }
    productIds = {}; list.forEach(function (p) { productIds[p.id] = true; });
    placeProductCards(t, settle);
  }
  /* a read card whose product has a folder leaves for it; the time is set once, here */
  function placeProductCards(t, settle) {
    if (!products.length) return;
    var R = rng(733 + cards.length), tNext = Math.max(t + .35, pNext), moved = false;
    stackOrder(t).concat(loose.filter(function (c) { return c && c.tPull === Infinity; })).forEach(function (c) {
      if (c.fkey || c.tFly < Infinity || c.product === null || !(c.qhash || c.error)) return;
      var fo = folders["product/" + c.product];
      if (!fo) return;
      c.fkey = fo.key; fo.per++; moved = true;
      if (!c.loose) { if (c.tT1 === Infinity) tuckAt(c, 1, NEG); if (c.tT2 === Infinity) tuckAt(c, 2, NEG); }   // the passes do not wait for a card that has left
      if (settle || REDUCE) { c.tFly = NEG; c.tArr = NEG; }
      else {
        c.tFly = tNext; tNext += .06 + R() * .04;
        c.tArr = c.tFly + T_ARR;
        var q = poseRest(c, c.tFly - .001);
        c.x0 = q.x; c.y0 = q.y; c.rot0 = q.rot; c.s0 = q.s;
      }
      fo.arr.push(c.tArr);
    });
    if (!moved) return;
    products.forEach(function (fo) { fo.arr.sort(function (a, b) { return a - b; }); });
    if (tNext > t + .35) { pNext = tNext; wake(tNext - t + T_ARR + 1); }
  }

  /* ---------- polling ---------- */
  function shown() { return !!(root && root.parentNode && root.parentNode.offsetParent); }
  function poll() {
    if (inflight || !active || external) return;
    if (!shown()) { active = false; return; }
    inflight = true;
    fetch("/api/progress").then(function (r) { return r.json(); }).then(function (p) {
      prog = p || prog;
      var restarted = wasRunning === false && prog.running;   // a scan just started: listen from the top
      if (restarted) sceneKey = null;
      wasRunning = !!prog.running;
      var waiting = [];
      if (sceneKey) for (var i = 0; i < cards.length && waiting.length < 240; i++) {
        var c = cards[i];
        if ((!c.qhash && !c.error) || !c.looked || (!c.category && !c.error) || (sample && sample.products && c.product === null && !c.error)) waiting.push(c.id);
      }
      var reading = prog.running && ["done", "save", "categorise", "products"].indexOf(prog.stage) < 0;
      var url = "/api/scan/sample?n=120&since=" + (sceneKey ? maxId : 0) + (waiting.length ? "&ids=" + waiting.join(",") : "") +
        "&folders=" + (foldersSeen || reading ? 0 : 1);
      return fetch(url).then(function (r) { return r.json(); }).then(function (d) { ingest(d, restarted); });
    }).catch(function () { /* the next tick tries again */ }).then(function () {
      inflight = false;
      schedulePoll(prog.running ? 800 : 2500);
    });
  }
  function schedulePoll(ms) { clearTimeout(pollTimer); if (active) pollTimer = setTimeout(poll, ms); }

  function ingest(d, restarted) {
    var t = clock();
    shoot = d.shoot || ""; disk = d.disk;
    var key = d.key !== undefined ? d.key : shoot + "|" + d.total;
    var fresh = key !== sceneKey;
    var running = !!prog.running;
    log.push({ t: +t.toFixed(2), key: key, fresh: fresh, running: running, stage: prog.stage, rows: (d.rows || []).length, up: (d.updates || []).length, folders: (d.folders || []).length });
    if (log.length > 400) log.shift();
    if (fresh) {
      newScene(d, t);
      sceneKey = key;
      // settle what had already happened before this first look; a scan that just started plays from the top
      var settle = REDUCE || !running;
      var early = running && (prog.stage === "scan" || prog.stage === "faces" || (prog.stage === "features" && (prog.done || 0) < (prog.total || 1) * .05));
      addRows(d.rows || [], t, settle, !settle && (early || restarted));
      (d.rows || []).forEach(function (u) { update(u, t, true); });
      settlePrefix();
      for (var id in wins) wins[id].diskRow.lastChild.textContent = disk || "Macintosh HD";
    } else {
      if (d.rows && d.rows.length) { addRows(d.rows, t, REDUCE, false); d.rows.forEach(function (u) { update(u, t, REDUCE); }); }
    }
    (d.updates || []).forEach(function (u) { update(u, t, REDUCE); });
    sample = d;
    if (d.folders && d.folders.length) setFolders(d.folders, t, REDUCE || (fresh && !running));
    if (d.products) setProducts(d.products, t, REDUCE || fresh);   // what was sorted before this first look is settled, not replayed
    root.hidden = !(d.total > 0 || running);
    for (var id2 in wins) {
      var w = wins[id2], none = (id2 === "videos" ? d.videos : d.photos) === 0 && d.total > 0;
      w.empty.hidden = !none || w.folders.length > 0;
      if (none) w.empty.textContent = id2 === "videos" ? "No clips in this shoot" : "No photos in this shoot";
    }
    wake(2);
  }
  /* cards read before we first looked are already under the Stack, in the order they sit */
  function settlePrefix() {
    pile.forEach(function (c) { if (c.qhash || c.error) { c.tRead = NEG; tuckAt(c, 1, NEG); } });
    if (n1 === pile.length) pile.forEach(function (c) { if (c.looked || c.error) tuckAt(c, 2, NEG); });
    loose.forEach(function (c) { if (c && (c.qhash || c.error)) { c.tPull = NEG; } });
  }

  /* ---------- the passes: a card comes off the Stack, is read (or looked at) and goes in at the bottom ----------
     Each tuck is an event (time, card); the Stack's order at any time is the listing order with the events up to
     then applied. A pass takes the topmost card that is ready, so a clip still waiting near the top (clips are read
     last) does not hold the photos under it back. */
  var tucks = [], planned = [], n1 = 0, n2 = 0, oc = null;
  function tuckAt(c, pass, t) {
    if (pass === 1) { c.tT1 = t; n1++; } else { c.tT2 = t; n2++; }
    tucks.push({ t: t, c: c });
    planned.splice(planned.indexOf(c), 1); planned.push(c);
    if (t > NEG) lastTuck = t;
    oc = null;
  }
  function advance(t) {
    if (foldersSeen) return;                    // the folders came: no more flipping, the cards go to them
    var N = pile.length, i, c, ready = [];
    if (n1 < N) {
      for (i = 0; i < N; i++) { c = planned[i]; if (c.tT1 === Infinity && (c.error || (c.qhash && (c.imgDone || t - c.tRead > 1.5)))) ready.push(c); }
      ready.forEach(function (c, j) {
        var gap = ready.length - j > 20 ? .08 : ready.length - j > 6 ? .14 : .24;
        tuckAt(c, 1, REDUCE ? NEG : Math.max(t + .45, lastTuck + gap));
      });
    }
    if (n1 === N && n2 < N) {
      ready = [];
      for (i = 0; i < N; i++) { c = planned[i]; if (c.tT2 === Infinity && (c.looked || c.error)) ready.push(c); }
      ready.forEach(function (c, j) {
        var gap = n2 === 0 ? TUCK : ready.length - j > 20 ? .08 : ready.length - j > 6 ? .14 : .24;
        tuckAt(c, 2, REDUCE ? NEG : Math.max(t + .45, lastTuck + gap));
      });
    }
    for (i = 0; i < loose.length; i++) {
      var l = loose[i];
      if (!l || l.tPull !== Infinity) continue;
      if (l.error || (l.qhash && (l.imgDone || t - l.tRead > 1.5))) {
        l.tPull = REDUCE ? NEG : Math.max(t + .5, lastPull + .5);
        if (l.tPull > NEG) lastPull = l.tPull;
        wake(l.tPull - t + 2);
      }
    }
    if (lastTuck > t - TUCK) wake(lastTuck - t + TUCK + .2);
  }
  /* the Stack top to bottom at time t, without the cards that have left for a folder; the card about to be read
     is lifted to the top for the half second before it goes */
  function stackOrder(t) {
    if (!pile.length) return [];
    if (!oc || t < oc.t) oc = { t: NEG, order: pile.slice(), k: 0 };
    while (oc.k < tucks.length && tucks[oc.k].t <= t) { var c = tucks[oc.k].c, o = oc.order; o.splice(o.indexOf(c), 1); o.push(c); oc.k++; }
    oc.t = t;
    var out = [], next = oc.k < tucks.length && tucks[oc.k].t < t + .5 ? tucks[oc.k].c : null;
    if (next && !(next.tFly <= t)) out.push(next);
    for (var i = 0; i < oc.order.length; i++) { var d = oc.order[i]; if (d !== next && !(d.tFly <= t)) out.push(d); }
    return out;
  }
  function lastTucking(t) {   // the tuck running at t, if one is
    if (!oc) stackOrder(t);
    var k = oc && oc.t === t ? oc.k - 1 : -1;
    if (k < 0) { for (k = tucks.length - 1; k >= 0 && tucks[k].t > t; k--); }
    var e = tucks[k];
    return e && e.t > NEG && t - e.t < TUCK ? { c: e.c, t0: e.t } : null;
  }
  function nextTuck(c, t) { return c.tT1 > t ? c.tT1 : c.tT2 > t ? c.tT2 : Infinity; }

  /* ---------- poses ---------- */
  var orderCache = null;
  function poseRest(c, t) {
    if (c.loose) return { x: LOOSE[c.spot][0], y: LOOSE[c.spot][1], rot: 0, s: LOOSE_S, a: 1, lift: 0 };
    var ord = orderCache && orderCache.t === t ? orderCache.list : stackOrder(t), n = ord.length, rank = ord.indexOf(c);
    if (rank < 0) rank = 0;
    var x = PILE.x + c.jx, y = PILE.y + c.jy - (n - rank) * PILE.rise, rot = c.jr, s = 1, lift = 0;
    var nt = nextTuck(c, t);
    if (rank === 0 && nt < Infinity) lift = smooth(nt - .5, nt - .15, t);   // the top card rises and straightens while it is read
    y -= lift * PILE.lift; rot *= 1 - .75 * lift; s *= 1 + .04 * lift;
    return { x: x, y: y, rot: rot, s: s, a: 1, lift: lift, rank: rank, n: n };
  }
  function pose(c, t) {
    if (t < c.tFound || c.tArr + .25 < t) return null;
    var born = t - c.tFound, sDrop = spring(born, .5, 0);
    var q = poseRest(c, t);
    q.y -= (1 - sDrop) * 26; q.rot += (1 - sDrop) * (c.r[4] - .5) * 10; q.s *= 1 + (1 - sDrop) * .05; q.a = clamp(born / .16);
    if (c.loose && t >= c.tPull && c.tPull > NEG) {        // pulled into the Stack
      var pp = spring(t - c.tPull, .7, .08), pc = clamp(pp, 0, 1.02);
      if (pc >= 1 && t - c.tPull > 1.2) return null;
      var top = stackOrder(t).length;
      var x1 = PILE.x, y1 = PILE.y - top * PILE.rise - 4;
      var cx = (q.x + x1) / 2, cy = Math.min(q.y, y1) - 60 - c.r[2] * 30, u = 1 - pc;
      return { x: u * u * q.x + 2 * u * pc * cx + pc * pc * x1, y: u * u * q.y + 2 * u * pc * cy + pc * pc * y1, rot: Math.sin(pc * Math.PI) * (c.r[4] - .5) * 9,
        s: lerp(LOOSE_S, .96, smooth(0, 1, pc)), a: 1 - smooth(.82, 1, pc), lift: 0, fly: pc, h: Math.sin(Math.min(1, pc) * Math.PI) };
    }
    if (c.loose && c.tPull <= NEG) return null;
    if (!(t >= c.tFly)) {
      var tk = !c.loose && lastTucking(t);
      if (tk && tk.c === c) {                             // flipped under the Stack: up and out to the right, then in at the bottom
        var p = smooth(0, 1, (t - tk.t0) / TUCK), arcp = Math.sin(Math.PI * p);
        var yBot = PILE.y + c.jy - PILE.rise, yTop = PILE.y + c.jy - (q.n || 1) * PILE.rise - PILE.lift;
        q.x += arcp * 96; q.y = lerp(yTop, yBot, smooth(.35, 1, p)) - arcp * 26; q.rot = c.jr * .25 * (1 - p) + arcp * 7; q.under = p >= .5; q.tuck = p;
      }
      return q;
    }
    /* in flight: an arc to the folder (or its window's tile), shrinking, fading only at the very end */
    var f = folders[c.fkey];
    var fp = spring(t - c.tFly, FLY.response, FLY.bounce), fc = clamp(fp, 0, 1.02);
    var x0 = c.x0, y0 = c.y0, x1f = f.cx, y1f = f.cy - 4, mini = f.win.u > .5;
    var cxf = (x0 + x1f) / 2 + (c.r[1] - .5) * 90, cyf = Math.min(y0, y1f) - ARC - c.r[2] * 50, uf = 1 - fc;
    var end = mini ? .22 : END_S;
    return { x: uf * uf * x0 + 2 * uf * fc * cxf + fc * fc * x1f, y: uf * uf * y0 + 2 * uf * fc * cyf + fc * fc * y1f,
      rot: c.rot0 * (1 - smooth(0, .7, fc)) + Math.sin(fc * Math.PI) * (c.r[4] - .5) * 9,
      s: c.s0 * lerp(1, end, smooth(.15, 1, fc)) * (1 + .06 * Math.sin(Math.min(1, fc) * Math.PI)),
      a: 1 - smooth(.7, .97, fc), lift: 0, fly: fc, h: Math.sin(Math.min(1, fc) * Math.PI) };
  }

  /* ---------- drawing ---------- */
  function drawCard(ctx, c, q, t) {
    var k = fx._k, img = c.sprite, blank = blanks[shapeOf(c)];
    var nt = c.loose ? Infinity : nextTuck(c, t);
    // which face it shows: the picture once the read pass has gone over it (a loose file once it is read)
    var readAt = c.loose ? c.tRead : c.tT1;
    var showImg = img && (readAt <= t || q.fly);
    var band = -1, glint = -1;
    if (!c.loose && img && c.tT1 === nt && t >= nt - .42) { band = clamp((t - (nt - .42)) / .4); showImg = false; }
    if (!c.loose && img && c.tT2 === nt && t >= nt - .42) glint = clamp((t - (nt - .42)) / .4);
    var sp = showImg ? img : blank;
    if (!sp) return;
    var g = { w: sp.w, h: sp.h };
    ctx.setTransform(k, 0, 0, k, -vis.x0 * k, -vis.y0 * k);
    ctx.translate(q.x, q.y); ctx.rotate(q.rot * Math.PI / 180); ctx.scale(q.s, q.s);
    var sh = shadows[shapeOf(c)], hgt = (q.h || 0) * 14 + q.lift * 3;
    var sa = q.fly ? .34 * (1 - smooth(.35, .8, q.fly)) : .55 + q.lift * .04;
    if (sh && sa > .01) { ctx.globalAlpha = q.a * sa; ctx.drawImage(sh.cv, -g.w / 2 - sh.pad, -g.h / 2 - sh.pad + 3 + hgt, g.w + sh.pad * 2, g.h + sh.pad * 2); }
    ctx.globalAlpha = q.a;
    ctx.drawImage(sp.cv, -g.w / 2, -g.h / 2, g.w, g.h);
    if (band >= 0) {   // the read edge sweeps down the top card and the picture comes up behind it
      var yb = img.h * band;
      if (yb > .5) { ctx.save(); ctx.beginPath(); ctx.rect(-img.w / 2 - 1, -img.h / 2 - 1, img.w + 2, yb + 1); ctx.clip(); ctx.drawImage(img.cv, -img.w / 2, -img.h / 2, img.w, img.h); ctx.restore(); }
      var gy = -img.h / 2 + yb, gr = ctx.createLinearGradient(0, gy - 12, 0, gy + 1);
      gr.addColorStop(0, "rgba(255,255,255,0)"); gr.addColorStop(.85, "rgba(255,255,255,.28)"); gr.addColorStop(1, "rgba(255,255,255,0)");
      ctx.save(); roundRect(ctx, -img.w / 2, -img.h / 2, img.w, img.h, 3); ctx.clip(); ctx.fillStyle = gr; ctx.fillRect(-img.w / 2, gy - 12, img.w, 13); ctx.restore();
    } else if (glint >= 0 && glint < 1) {   // the look pass: a soft light runs down it
      var gy2 = -g.h / 2 + (g.h + 30) * glint - 15, gr2 = ctx.createLinearGradient(0, gy2 - 18, 0, gy2 + 18);
      gr2.addColorStop(0, "rgba(255,255,255,0)"); gr2.addColorStop(.5, "rgba(255,255,255,.22)"); gr2.addColorStop(1, "rgba(255,255,255,0)");
      ctx.save(); roundRect(ctx, -g.w / 2, -g.h / 2, g.w, g.h, 3); ctx.clip(); ctx.fillStyle = gr2; ctx.fillRect(-g.w / 2, gy2 - 18, g.w, 36); ctx.restore();
    }
    ctx.globalAlpha = 1;
  }

  var lastL1 = "", lastL2 = "", lastPie = -2, lastStack = "", sleepAt = 0;
  var inLoop = false;
  function wake(sec) { sleepAt = Math.max(sleepAt, performance.now() / 1000 + (sec || 1)); if (active && !raf && !inLoop) raf = requestAnimationFrame(loop); }

  function render(t) {
    var t0 = performance.now();
    advance(t);
    for (var id in wins) placeWin(wins[id], t);
    orderCache = { t: t, list: stackOrder(t) };
    var k = fx._k;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, fx.width, fx.height);
    ctxLow.setTransform(1, 0, 0, 1, 0, 0); ctxLow.clearRect(0, 0, fxLow.width, fxLow.height);
    var under = [], resting = [], flying = [], over = [];
    var ord = orderCache.list;
    for (var i = ord.length - 1; i >= 0; i--) {
      var c = ord[i], q = pose(c, t);
      if (!q) continue;
      if (q.tuck !== undefined) (q.under ? under : over).push([c, q]); else resting.push([c, q]);
    }
    for (i = 0; i < pile.length; i++) {
      var fc = pile[i];
      if (fc.tFly <= t) { var fq = pose(fc, t); if (fq) flying.push([fc, fq]); }
      if (external && fc.sprite && fc.tArr + 1 < t) { fc.sprite = null; fc.img = null; fc.imgOK = false; }   // a long shoot: a card that has landed needs no picture
    }
    for (i = 0; i < loose.length; i++) { var lc = loose[i]; if (!lc) continue; var lq = pose(lc, t); if (!lq) continue; (lq.fly ? flying : resting).push([lc, lq]); }
    var lists = [under, resting, over];
    for (var li = 0; li < lists.length; li++) for (i = 0; i < lists[li].length; i++) if (lists[li][i][1].a > .002) drawCard(ctxLow, lists[li][i][0], lists[li][i][1], t);
    flying.sort(function (a, b) { return a[1].fly - b[1].fly; });
    for (i = 0; i < flying.length; i++) if (flying[i][1].a > .002) drawCard(ctx, flying[i][0], flying[i][1], t);

    /* folders: appear, then count up as their cards land, one damped kick per arrival */
    var near = {};
    for (i = 0; i < flying.length; i++) { var fcard = flying[i][0]; if (fcard.fkey && t < fcard.tArr) near[fcard.fkey] = Math.max(near[fcard.fkey] || 0, smooth(.55, .88, flying[i][1].fly)); }
    var winCount = { photos: 0, videos: 0, desk: 0 }, winKick = { photos: NEG, videos: NEG, desk: NEG };
    for (i = 0; i < folderList.length; i++) {
      var f = folderList[i], arr = f.arr, n = 0;
      while (n < arr.length && arr[n] <= t) n++;
      var app = REDUCE ? 1 : spring(t - f.tA, .5, .12);
      var last = n ? arr[n - 1] : NEG, kk = kick(t - last, .42, .32);
      var hot = near[f.key] || 0, held = Math.max(hot, smooth(0, .04, t - last) * (1 - smooth(.04, .22, t - last)));
      var tf = app < .999 ? "scale(" + (.6 + .4 * app).toFixed(4) + ")" : "";
      if (f._c !== tf) { f.cell.style.transform = tf; f.cell.style.opacity = app < .999 ? clamp(app * 1.6).toFixed(3) : ""; f._c = tf; }
      var itf = "scale(" + (1 + .085 * kk).toFixed(4) + ")";
      if (f._tf !== itf) { f.icon.style.transform = itf; f._tf = itf; }
      var open = Math.max(hot * 14, 0) + Math.max(0, kk) * 16;
      var ftf = open > .05 ? "perspective(160px) rotateX(" + (-open).toFixed(2) + "deg)" : "";
      if (f._f !== ftf) { f.front.style.transform = ftf; f._f = ftf; }
      var ho = (held * .9).toFixed(3); if (f._h !== ho) { f.hl.style.opacity = ho; f._h = ho; }
      var cnt = f.per ? (n >= f.per ? f.final : Math.round(f.final * n / f.per)) : (t >= f.tA + .35 ? f.final : 0);
      if (f._ct !== cnt) { f.ct.textContent = items(cnt); f._ct = cnt; }
      winCount[f.win.id] += cnt; if (last > winKick[f.win.id]) winKick[f.win.id] = last;
    }
    /* a minimized window's tile carries its count */
    for (id in wins) {
      var w = wins[id], b = w.badge, show = w.u > .9 && winCount[id] > 0;
      if (b.hidden === show) b.hidden = !show;
      if (show) {
        var txt = winCount[id] >= 10000 ? Math.round(winCount[id] / 1000) + "k" : fmt(winCount[id]);
        if (b.textContent !== txt) b.textContent = txt;
        var bk = REDUCE ? 0 : kick(t - winKick[id], .4, .3);
        var btf = "translate(" + (w.tile.x + w.tile.w - 14).toFixed(1) + "px," + (w.tile.y - 9).toFixed(1) + "px) scale(" + (1 + .14 * bk).toFixed(3) + ")";
        if (b._tf !== btf) { b.style.transform = btf; b._tf = btf; }
      }
    }

    /* the desktop labels: each loose file shows its name until it leaves, the Stack counts what is still on it */
    for (i = 0; i < looseLabels.length; i++) {
      var lc2 = loose[i], visL = lc2 && t >= lc2.tFound + .1 && !(lc2.tPull <= t + .05) && !(lc2.tFly <= t + .05) ? "1" : "0";
      if (looseLabels[i]._o !== visL) { looseLabels[i].style.opacity = visL; looseLabels[i]._o = visL; }
    }
    var total = sample ? sample.total : 0, onStack;
    if (!pile.length) onStack = prog.stage === "scan" ? (prog.total || 0) : total;
    else { var left = 0, flown = 0; for (i = 0; i < pile.length; i++) { if (pile[i].tArr <= t) flown++; } left = pile.length - flown; onStack = flown ? Math.round(total * left / pile.length) : total; }
    var dim = foldersSeen && onStack === 0;
    var st = esc(shoot || "Shoot") + "<small>" + (onStack > 0 ? items(onStack) : "&nbsp;") + "</small>";
    if (st !== lastStack) { stackLabel.innerHTML = st; lastStack = st; }
    var so = dim ? "0.55" : "1"; if (stackLabel.style.opacity !== so) stackLabel.style.opacity = so;

    /* the pill */
    var he = REDUCE ? 1 : spring(t - opened - .55, .55, 0), htf = he >= .999 ? "" : "translateY(" + ((1 - he) * 12).toFixed(2) + "px)";
    if (hud._tf !== htf) { hud.style.transform = htf; hud.style.opacity = he >= .999 ? "" : clamp(he * 1.5).toFixed(3); hud._tf = htf; }
    var copy = pillCopy(t);
    if (copy[0] !== lastL1) { hudL1.textContent = copy[0]; lastL1 = copy[0]; }
    if (copy[1] !== lastL2) { hudL2.textContent = copy[1]; lastL2 = copy[1]; }
    var pr = copy[2] < 0 ? -1 : Math.round(copy[2] * 400) / 400;
    if (pr !== lastPie) {
      lastPie = pr;
      if (pr < 0) pie.setAttribute("d", "");
      else { var ang = pr * Math.PI * 2 - Math.PI / 2, large = pr > .5 ? 1 : 0;
        pie.setAttribute("d", pr >= .999 ? "M15 4a11 11 0 1 1 0 22a11 11 0 1 1 0-22" : pr <= 0 ? "" : "M15 4A11 11 0 " + large + " 1 " + (15 + 11 * Math.cos(ang)).toFixed(2) + " " + (15 + 11 * Math.sin(ang)).toFixed(2)); }
    }
    var spin = copy[3];   // looking for files: no counter yet, a quarter ring turns
    ring.setAttribute("opacity", spin ? "1" : "0");
    if (spin) ring.setAttribute("transform", "rotate(" + (REDUCE ? 0 : ((t * 300) % 360)).toFixed(1) + " 15 15)");
    tick.setAttribute("opacity", copy[4] ? "1" : "0");
    if (copy[4]) pie.setAttribute("d", "M15 4a11 11 0 1 1 0 22a11 11 0 1 1 0-22");
    stats.last = performance.now() - t0;
    return spin;
  }

  /* what the pill says: the stage, unless the desk is still catching up with it (cards still landing on the Stack,
     cards still flying to their folders) */
  function pillCopy(t) {
    if (pillOverride) return [pillOverride[0], pillOverride[1], -1, false, false];
    var s = prog.stage, total = sample ? sample.total : 0, done = prog.done || 0, ptot = prog.total || 0;
    var dropping = false, flyingNow = false, i;
    for (i = 0; i < pile.length; i++) { if (pile[i].tFound > t - .5) dropping = true; if (pile[i].tArr > t && pile[i].tFly < Infinity && !(pile[i].fkey && !foldersSeen)) flyingNow = true; }
    var of = function (a, b) { return fmt(a) + " of " + fmt(b); };
    var frac = ptot ? clamp(done / ptot) : 0;
    if (flyingNow || (s === "categorise" || s === "products")) {
      var placed = 0; for (i = 0; i < folderList.length; i++) placed += folderList[i]._ct || 0;
      return ["Putting them in folders", foldersSeen ? of(Math.min(placed, total), total) : fmt(total) + " files", foldersSeen ? clamp(placed / Math.max(1, total)) : -1, !foldersSeen, false];
    }
    if (s === "scan" || dropping) return ["Finding photos and clips", total || ptot ? fmt(total || ptot) + " found" : "Looking through " + (disk || shoot || "the shoot"), -1, true, false];
    if (s === "faces" || s === "features") return ["Reading every frame", of(done, ptot), frac, false, false];
    if (s === "embed" || s === "clipfaces") return ["Looking at what is in each one", of(done, ptot), frac, false, false];
    if (s === "paused") return ["Paused, the disk went away", "Plug it in and press Continue scan", -1, false, false];
    if (s === "error") return ["The scan stopped", "Something went wrong reading the shoot", -1, false, false];
    var nread = 0, pending = 0;
    for (i = 0; i < cards.length; i++) { if (cards[i].qhash || cards[i].error) nread++; else pending++; }
    if (foldersSeen) {
      var nf = folderList.length, vids = sample && sample.videos > 0 && sample.photos > 0;
      return [fmt(total) + " files sorted", nf + (nf === 1 ? " folder" : " folders") + (vids ? ", photos and videos apart" : ""), 1, false, true];
    }
    if (products.length && !prog.running) return [fmt(total) + " files sorted", products.length + " products", 1, false, true];
    if (pending && !prog.running) return ["Paused part way", "Continue scan picks up where it stopped", -1, false, false];
    return [fmt(total) + " files read", "Not in folders yet", 1, false, false];
  }

  function loop() {
    raf = 0;
    if (!active) return;
    if (!shown()) { active = false; clearTimeout(pollTimer); return; }   // the tab went away
    var now = performance.now();
    if (stats.prev) { stats.frames.push(now - stats.prev); if (stats.frames.length > 4000) stats.frames.shift(); }
    stats.prev = now;
    inLoop = true;
    var spinning;
    try { spinning = render(clock()); } finally { inLoop = false; }
    stats.render.push(stats.last); if (stats.render.length > 4000) stats.render.shift();
    var dragging = wins.photos.dragging || wins.videos.dragging;
    if (spinning || dragging || now / 1000 < sleepAt || frozen !== null) raf = requestAnimationFrame(loop);
    else stats.prev = 0;                            // nothing moves: stop drawing until something does
  }

  var ScanScene = {
    /* another page has the data: draw it here, poll nothing */
    feed: function (d, p) {
      if (!built && !build()) return;
      external = true; pMin = 1; bare = !!d.bare; pillOverride = d.pill || null;
      root.classList.toggle("fs-bare", bare);
      prog = p || prog;
      if (opened === NEG) opened = clock();
      active = true;
      ingest(d, false);
      fit();
      wake(2);
    },
    /* the Scan tab opened (app.js showView): start listening and drawing */
    open: function () {
      if (!built && !build()) return;
      if (active) return;
      active = true;
      if (opened === NEG) opened = clock();
      fit();
      wake(2);
      clearTimeout(pollTimer); poll();
    },
    /* app.js hands over each /api/progress it reads, so a scan it starts is seen at once */
    progress: function (p) {
      if (!p) return;
      if (!active) { ScanScene.open(); return; }
      if (p.running && !wasRunning) { clearTimeout(pollTimer); schedulePoll(50); }
    },
  };
  window.ScanScene = ScanScene;
  /* for the headless checks: hold the clock still, or let it run again */
  window.__scene = {
    freeze: function (dt) { frozen = performance.now() / 1000 + (dt || 0); wake(1); },
    thaw: function () { frozen = null; wake(1); },
    state: function () { return { cards: cards.length, pile: pile.length, loose: loose.filter(Boolean).length, folders: folderList.length, foldersSeen: foldersSeen,
      read: n1, looked: n2, stage: prog.stage, products: products.map(function (f) { return { key: f.key, name: f.name, final: f.final, per: f.per, landed: f.arr.filter(function (a) { return a <= clock(); }).length, shown: f._ct, x: f.cx, y: f.cy }; }), wins: Object.keys(wins).map(function (id) { var w = wins[id]; return { id: id, x: w.x, y: w.y, w: w.w, h: w.h, min: w.min, front: w.front, u: w.u }; }) }; },
  };
})();
