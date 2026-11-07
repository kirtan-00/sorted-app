/* The full-size view's picture: fit, 100%, drag to pan, and two shots side by side at the same zoom. app.js owns which shot
   is shown and the keys; this owns the panes, the preloading and the pixels. An item is {key, thumb, full, edge}: thumb the
   1024 px picture from the index, there at once, full the file's own pixels, which replace it when decoded, edge the long
   edge the file really has. 100% is one picture pixel on one screen pixel, so on a Retina screen it is drawn at half its CSS
   size. The zoom and the point looked at are one state shared by every pane and kept from shot to shot, as fractions of the
   picture, so stepping through a burst stays on the same eye. */
window.sortedLoupe = function (stage, opts) {
  "use strict";
  var KEEP = 6;                                        // decoded pictures held: both panes and the shots either side, about 100 MB each at 24 MP
  var DRAG = 4;                                        // px a press may wander and still be a click
  var view = { zoom: false, cx: 0.5, cy: 0.5 };
  var panes = [];
  var cache = new Map();                               // key -> {img, ready}, oldest first
  var pointer = null;                                  // where the mouse last was over the stage, for Z
  var press = null;

  function makePane() {
    var el = document.createElement("div");
    el.className = "lb-pane";
    var thumb = document.createElement("img");
    thumb.alt = ""; thumb.draggable = false; thumb.className = "lb-thumb";
    var cap = document.createElement("div");
    cap.className = "lb-cap"; cap.hidden = true;
    el.appendChild(thumb); el.appendChild(cap);
    var p = { el: el, thumb: thumb, cap: cap, img: null, item: null, w: 0, h: 0 };
    thumb.addEventListener("load", function () {
      if (p.img || !p.item || !thumb.naturalWidth) return;
      var k = (p.item.edge || Math.max(thumb.naturalWidth, thumb.naturalHeight)) / Math.max(thumb.naturalWidth, thumb.naturalHeight);
      p.w = thumb.naturalWidth * k; p.h = thumb.naturalHeight * k;
      place(p);
    });
    stage.appendChild(el);
    return p;
  }
  function dpr() { return window.devicePixelRatio || 1; }
  function fitScale(p, W, H) { return Math.min(W / p.w, H / p.h, 1); }
  // Where the picture sits in its pane right now: its top left, its drawn size.
  function geometry(p) {
    var W = p.el.clientWidth, H = p.el.clientHeight;
    var s = view.zoom ? 1 / dpr() : fitScale(p, W, H);
    var w = p.w * s, h = p.h * s, round = function (v) { return Math.round(v * dpr()) / dpr(); };
    var x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, W / 2 - view.cx * w));
    var y = h <= H ? (H - h) / 2 : Math.min(0, Math.max(H - h, H / 2 - view.cy * h));
    return { W: W, H: H, s: s, w: round(w), h: round(h), x: round(x), y: round(y) };
  }
  function place(p) {
    if (!p.w) return;
    var g = geometry(p);
    [p.thumb, p.img].forEach(function (im) {
      if (!im) return;
      im.style.width = g.w + "px"; im.style.height = g.h + "px";
      im.style.transform = "translate(" + g.x + "px," + g.y + "px)";
    });
    p.el.classList.toggle("can-pan", view.zoom && (g.w > g.W || g.h > g.H));
  }
  function placeAll() { panes.forEach(place); }
  function say() {
    var p = active();
    if (!p || !p.item) return;
    opts.onChange({ zoom: view.zoom, compare: panes.length > 1, loaded: !!p.img, failed: !!p.failed, w: p.w, h: p.h });
  }
  function active() { return panes[panes.length - 1]; }

  function load(item) {
    var c = cache.get(item.key);
    if (c) { cache.delete(item.key); cache.set(item.key, c); return c; }   // the newest end
    var img = new Image();
    img.alt = ""; img.draggable = false; img.className = "lb-full";
    img.decoding = "async";
    img.src = item.full;
    c = { img: img, ready: img.decode().then(function () { return img; }) };
    c.ready.catch(function () { cache.delete(item.key); });
    cache.set(item.key, c);
    while (cache.size > KEEP) cache.delete(cache.keys().next().value);
    return c;
  }
  function setItem(p, item) {
    if (p.img) { p.img.remove(); p.img = null; }
    p.item = item; p.failed = false; p.w = 0; p.h = 0;
    p.el.removeAttribute("data-full");
    p.thumb.hidden = false;
    p.thumb.src = item.thumb;
    if (p.thumb.complete && p.thumb.naturalWidth) p.thumb.dispatchEvent(new Event("load"));
    say();
    if (!item.full) return Promise.resolve();
    return load(item).ready.then(function (img) {
      if (p.item !== item) return;                     // the next shot came first
      if (img.parentNode && img.parentNode !== p.el) img = img.cloneNode();
      p.img = img; p.w = img.naturalWidth; p.h = img.naturalHeight;
      p.el.insertBefore(img, p.cap);
      place(p);
      p.thumb.hidden = true;
      p.el.setAttribute("data-full", "1");
      say();
    }).catch(function () {
      if (p.item !== item) return;
      p.failed = true;
      say();
    });
  }
  function toOne() { while (panes.length > 1) { var p = panes.shift(); p.el.remove(); if (p.img) p.img.remove(); } active().cap.hidden = true; active().el.classList.remove("on"); }

  // Fractions of the picture under a point in the pane, and the centre that keeps that point where it is at 100%.
  function zoomAt(p, px, py) {
    var g = geometry(p), u = (px - g.x) / g.w, v = (py - g.y) / g.h, s = 1 / dpr();
    view.cx = u + (g.W / 2 - px) / (p.w * s);
    view.cy = v + (g.H / 2 - py) / (p.h * s);
    clampView(p, s);
  }
  function clampView(p, s) {
    var hw = p.el.clientWidth / (2 * p.w * s), hh = p.el.clientHeight / (2 * p.h * s);
    view.cx = hw >= 0.5 ? 0.5 : Math.min(1 - hw, Math.max(hw, view.cx));
    view.cy = hh >= 0.5 ? 0.5 : Math.min(1 - hh, Math.max(hh, view.cy));
  }
  function setZoom(on, p, px, py) {
    if (on && p && p.w) { if (px != null) zoomAt(p, px, py); }
    view.zoom = on;
    placeAll(); say();
  }
  function paneAt(t) { for (var i = 0; i < panes.length; i++) if (panes[i].el.contains(t)) return panes[i]; return null; }
  function panBy(p, dx, dy) {
    var s = 1 / dpr();
    view.cx -= dx / (p.w * s); view.cy -= dy / (p.h * s);
    clampView(p, s);
    placeAll();
  }

  stage.addEventListener("pointerdown", function (e) {
    var p = paneAt(e.target);
    if (!p || e.button !== 0) return;
    press = { p: p, id: e.pointerId, x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY, moved: false, on: e.target.tagName === "IMG" };
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", function (e) {
    var r = stage.getBoundingClientRect();
    pointer = { t: document.elementFromPoint(e.clientX, e.clientY), x: e.clientX, y: e.clientY, r: r };
    if (!press || press.id !== e.pointerId) return;
    if (!press.moved && Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG) return;
    press.moved = true;
    if (view.zoom) { panBy(press.p, e.clientX - press.lx, e.clientY - press.ly); press.p.el.classList.add("panning"); }
    press.lx = e.clientX; press.ly = e.clientY;
  });
  function release(e) {
    if (!press || press.id !== e.pointerId) return;
    var q = press; press = null;
    q.p.el.classList.remove("panning");
    if (e.type !== "pointerup" || q.moved) return;
    if (!q.on && !view.zoom) { opts.onBackdrop(); return; }
    var b = q.p.el.getBoundingClientRect();
    setZoom(!view.zoom, q.p, e.clientX - b.left, e.clientY - b.top);
  }
  stage.addEventListener("pointerup", release);
  stage.addEventListener("pointercancel", release);
  stage.addEventListener("wheel", function (e) {       // two fingers on a trackpad pan, as a drag does
    var p = paneAt(e.target);
    if (!view.zoom || !p) return;
    e.preventDefault();
    panBy(p, -e.deltaX, -e.deltaY);
  }, { passive: false });
  if (window.ResizeObserver) new ResizeObserver(placeAll).observe(stage);

  return {
    // The shot in the active pane. fresh: opened from the grid, so back to fit.
    show: function (item, fresh) {
      if (fresh) { view.zoom = false; view.cx = 0.5; view.cy = 0.5; pointer = null; }
      if (!panes.length) panes.push(makePane());
      stage.classList.add("has-picture");
      return setItem(active(), item);
    },
    clear: function () {
      panes.forEach(function (p) { p.el.remove(); if (p.img) p.img.remove(); });
      panes = [];
      stage.classList.remove("has-picture");
    },
    // Warm the shots around this one once it is on screen, and let go of the rest.
    preload: function (items) {
      items.forEach(function (it) { if (it.full) load(it); });
    },
    // Z: at the pointer when it is on the picture, else at the point last looked at.
    toggleZoom: function () {
      var p = active();
      if (!p) return;
      var over = !view.zoom && pointer ? paneAt(pointer.t) : null;
      if (over && pointer.t.tagName === "IMG") {
        var b = over.el.getBoundingClientRect();
        setZoom(true, over, pointer.x - b.left, pointer.y - b.top);
      } else setZoom(!view.zoom, p);
    },
    fit: function () { setZoom(false); },
    zoomed: function () { return view.zoom; },
    // Two up: the shot shown stays on the left and holds still; item comes in on the right, which the arrows then move.
    compare: function (leftCaption, item, caption) {
      var p = makePane();
      panes.push(p);
      panes[0].cap.textContent = leftCaption; panes[0].cap.hidden = false;
      p.cap.textContent = caption; p.cap.hidden = false;
      p.el.classList.add("on");
      placeAll();                                      // the left pane just got narrower
      return setItem(p, item);
    },
    caption: function (text) { active().cap.textContent = text; },
    compareOff: function () { toOne(); placeAll(); say(); },
    comparing: function () { return panes.length > 1; }
  };
};
