/* sorted landing v2. Vanilla JS on GSAP 3 + ScrollTrigger (cdnjs).
   Modes: motion (GSAP present, motion allowed), reduce (reduced motion or no GSAP: static frames, fades only).
   With no JS at all the page reads top to bottom and the app replica sits in its resting state. */
(function () {
  'use strict';

  var html = document.documentElement;
  var body = document.body;
  var mq = function (q) { return window.matchMedia(q).matches; };
  var gsapOK = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';
  var motionOK = mq('(prefers-reduced-motion: no-preference)') && gsapOK;
  var fine = mq('(pointer: fine)') && mq('(hover: hover)') && window.innerWidth > 760;
  var MODE = motionOK ? 'motion' : 'reduce';
  html.classList.add(MODE);
  if (!(motionOK && fine)) html.classList.add('no-tilt');
  if (gsapOK) gsap.registerPlugin(ScrollTrigger);

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var sleep = function (ms) { return new Promise(function (res) { setTimeout(res, ms); }); };

  /* the product name comes from the one CSS custom property; the title and the mailto subject follow it */
  var PRODUCT = (getComputedStyle(html).getPropertyValue('--product') || '').trim().replace(/^["']|["']$/g, '') || 'sorted';
  document.title = PRODUCT;
  $$('.wordmark').forEach(function (el) { el.setAttribute('aria-label', PRODUCT); });
  $$('a[data-mail]').forEach(function (a) {
    a.href = 'mailto:purohit.krick@gmail.com?subject=' + encodeURIComponent(PRODUCT + ' ' + a.getAttribute('data-mail'));
  });

  /* the app replica is laid out at its own pixel size and scaled to fit its column */
  var frame = $('#app-frame');
  function fitApp() {
    if (!frame) return;
    var cs = getComputedStyle(frame);
    var appW = parseFloat(cs.getPropertyValue('--app-w')) || 1280;
    var maxS = window.innerWidth <= 760 ? 0.6 : 0.56;
    var avail = frame.parentNode.getBoundingClientRect().width;
    var s = Math.min(maxS, avail / appW);
    frame.style.setProperty('--s', s.toFixed(4));
  }
  fitApp();
  window.addEventListener('resize', fitApp);

  /* smooth scroll: the wheel is lerped onto the native scroll position, so pins, anchors and sticky things keep working */
  var smooth = null;
  if (MODE === 'motion' && fine) {
    smooth = (function () {
      var target = window.scrollY, current = target, applied = target, raf = null, last = 0;
      function maxScroll() { return Math.max(0, html.scrollHeight - window.innerHeight); }
      function frameFn(t) {
        var dt = last ? Math.min(64, t - last) : 16.7;
        last = t;
        if (Math.abs(window.scrollY - applied) > 1) { current = applied = target = window.scrollY; }
        var k = 1 - Math.pow(1 - 0.12, dt / 16.7);
        current += (target - current) * k;
        if (Math.abs(target - current) < 0.25) current = target;
        window.scrollTo(0, current);
        applied = window.scrollY;
        if (current !== target) raf = requestAnimationFrame(frameFn);
        else { raf = null; last = 0; }
      }
      function start() { if (!raf) raf = requestAnimationFrame(frameFn); }
      window.addEventListener('wheel', function (e) {
        if (e.ctrlKey) return;
        e.preventDefault();
        var d = e.deltaY;
        if (e.deltaMode === 1) d *= 16; else if (e.deltaMode === 2) d *= window.innerHeight;
        if (!raf) { target = current = applied = window.scrollY; }
        target = Math.max(0, Math.min(maxScroll(), target + d));
        start();
      }, { passive: false });
      return { to: function (y) { if (!raf) { current = applied = window.scrollY; } target = Math.max(0, Math.min(maxScroll(), y)); start(); } };
    })();
  }
  $$('a[href^="#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var id = a.getAttribute('href').slice(1);
      var el = id ? document.getElementById(id) : null;
      if (!el) return;
      e.preventDefault();
      var y = id === 'top' ? 0 : el.getBoundingClientRect().top + window.scrollY - 60;
      if (smooth) smooth.to(y); else window.scrollTo({ top: y, behavior: MODE === 'motion' ? 'smooth' : 'auto' });
    });
  });

  /* the trail: a soft pastel disc lerps after the native cursor, shrinks to a dot over links, disappears over the app */
  if (MODE === 'motion' && fine) {
    var trail = $('.trail'), leak = $('.leak');
    var mx = -100, my = -100, tx = -100, ty = -100, lx = -1000, ly = -1000, seen = false, tscale = 1, cur = 1;
    body.classList.add('has-trail');
    window.addEventListener('mousemove', function (e) {
      mx = e.clientX; my = e.clientY;
      if (!seen) { seen = true; tx = mx; ty = my; lx = mx; ly = my; }
    }, { passive: true });
    gsap.ticker.add(function () {
      tx += (mx - tx) * 0.22; ty += (my - ty) * 0.22;
      cur += (tscale - cur) * 0.25;
      trail.style.transform = 'translate(' + tx.toFixed(1) + 'px,' + ty.toFixed(1) + 'px) scale(' + cur.toFixed(3) + ')';
      lx += (mx - lx) * 0.06; ly += (my - ly) * 0.06;
      leak.style.setProperty('--lx', lx.toFixed(1) + 'px');
      leak.style.setProperty('--ly', ly.toFixed(1) + 'px');
    });
    document.addEventListener('mouseleave', function () { trail.classList.add('is-off'); });
    document.addEventListener('mouseenter', function () { trail.classList.remove('is-off'); });
    var setTrail = function (t) {
      var off = t && t.closest && t.closest('.app-frame');
      var link = t && t.closest && t.closest('a, button, .chip, .nav');
      trail.classList.toggle('is-off', !!off);
      trail.classList.toggle('is-link', !off && !!link);
      tscale = link ? 6 / 28 : 1;
    };
    document.addEventListener('mouseover', function (e) { setTrail(e.target); });
    document.addEventListener('mouseout', function (e) { if (!e.relatedTarget) setTrail(null); });
  }

  /* magnetic buttons */
  if (MODE === 'motion' && fine) {
    var mags = $$('.magnetic').map(function (el) { return { el: el, on: false }; });
    window.addEventListener('mousemove', function (e) {
      for (var i = 0; i < mags.length; i += 1) {
        var m = mags[i], r = m.el.getBoundingClientRect();
        var dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
        var inside = Math.abs(dx) < r.width / 2 + 26 && Math.abs(dy) < r.height / 2 + 26;
        if (inside) { m.on = true; gsap.to(m.el, { x: dx * 0.3, y: dy * 0.3, duration: 0.45, ease: 'power3.out', overwrite: 'auto' }); }
        else if (m.on) { m.on = false; gsap.to(m.el, { x: 0, y: 0, duration: 0.9, ease: 'elastic.out(1, 0.45)', overwrite: 'auto' }); }
      }
    }, { passive: true });
  }

  /* ===== the app replica: a tiny real search over tagged tiles, an inspector, a people tab ===== */
  var app = (function () {
    var grid = $('#app-grid'), input = $('#app-input'), form = $('#app-q'), selbar = $('#app-selbar'), selcount = $('#app-selcount');
    var aerial = $('#app-aerial'), status = $('#app-status'), insp = { info: $('#insp-info'), cats: $('#insp-cats'), faces: $('#insp-faces') };
    var views = { search: $('#v-search'), people: $('#v-people') };
    var navs = $$('#app .nav');
    if (!grid) return null;
    var tiles = $$('.tile', grid);
    var FIXED = ['ocean', 'boat', 'beach', 'people', 'interview', 'building', 'office', 'road', 'night', 'food', 'sky', 'birds-animals', 'other'];
    var STOP = { the: 1, of: 1, on: 1, in: 1, at: 1, a: 1, an: 1, and: 1, with: 1 };

    tiles.forEach(function (t, i) {
      t.dataset.i = i;
      if (t.dataset.a === '1') { var b = document.createElement('span'); b.className = 'badge-a'; b.textContent = 'drone'; t.appendChild(b); }
    });

    function tokens(q) {
      return q.toLowerCase().split(/[^a-z0-9]+/).filter(function (w) { return w.length > 1 && !STOP[w]; });
    }
    function score(t, toks) {
      var words = t.dataset.w.split(' '), n = 0;
      toks.forEach(function (k) {
        for (var i = 0; i < words.length; i += 1) { if (words[i].indexOf(k) === 0 || k.indexOf(words[i]) === 0) { n += 1; break; } }
      });
      return n;
    }
    function search(q, onlyAerial) {
      var toks = tokens(q || '');
      var scored = tiles.map(function (t) {
        var s = toks.length ? score(t, toks) : 0;
        if (onlyAerial && t.dataset.a !== '1') s = -1;
        else if (onlyAerial && !toks.length) s = 1;
        return { t: t, s: s };
      });
      var max = 0;
      scored.forEach(function (x) { if (x.s > max) max = x.s; });
      var hits = scored.filter(function (x) { return x.s > 0; });
      return { hits: hits, max: max, active: toks.length > 0 || !!onlyAerial };
    }

    var scaleOf = function () { return parseFloat(getComputedStyle(frame).getPropertyValue('--s')) || 1; };
    function reorder(order) {
      var before = {};
      tiles.forEach(function (t) { before[t.dataset.i] = t.getBoundingClientRect(); });
      order.forEach(function (t) { grid.appendChild(t); });
      if (MODE !== 'motion') return;
      var s = scaleOf();
      tiles.forEach(function (t) {
        var a = before[t.dataset.i], b = t.getBoundingClientRect();
        var dx = (a.left - b.left) / s, dy = (a.top - b.top) / s;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) { gsap.set(t, { x: 0, y: 0 }); return; }
        gsap.fromTo(t, { x: dx, y: dy }, { x: 0, y: 0, duration: 0.6, ease: 'power3.inOut', overwrite: 'auto' });
      });
    }

    var lead = null;
    function apply(q, onlyAerial) {
      var res = search(q, onlyAerial);
      var hitSet = {};
      res.hits.forEach(function (x) { hitSet[x.t.dataset.i] = x.s; });
      var sorted = res.hits.slice().sort(function (a, b) { return b.s - a.s || a.t.dataset.i - b.t.dataset.i; }).map(function (x) { return x.t; });
      var rest = tiles.filter(function (t) { return !(t.dataset.i in hitSet); }).sort(function (a, b) { return a.dataset.i - b.dataset.i; });
      reorder(res.active ? sorted.concat(rest) : tiles.slice().sort(function (a, b) { return a.dataset.i - b.dataset.i; }));
      var unsure = 0;
      tiles.forEach(function (t) {
        var hit = t.dataset.i in hitSet;
        var un = hit && res.max > 1 && hitSet[t.dataset.i] < res.max;
        if (un) unsure += 1;
        t.classList.toggle('selected', hit);
        t.classList.toggle('unsure', un);
        t.classList.toggle('dim', res.active && !hit);
      });
      selbar.classList.toggle('has-sel', res.hits.length > 0);
      if (!res.active) selcount.textContent = 'Hover a tile. Type a word.';
      else if (!res.hits.length) selcount.textContent = 'Nothing matched. Try another word, or clear a filter.';
      else selcount.textContent = res.hits.length + ' selected of ' + tiles.length + ' shown' + (unsure ? ', ' + unsure + ' less sure' : '') + (onlyAerial ? ', drone shots only' : '');
      lead = sorted[0] || null;
      if (!hovered) fill(lead);
      return res;
    }

    function row(k, v) { var r = document.createElement('div'); r.className = 'insp-row'; r.innerHTML = '<span class="k"></span><span class="v"></span>'; r.firstChild.textContent = k; r.lastChild.textContent = v; return r; }
    function fill(t) {
      insp.info.innerHTML = ''; insp.cats.innerHTML = ''; insp.faces.innerHTML = '';
      if (!t) { insp.info.innerHTML = '<p class="insp-empty">Select a photo or clip</p>'; return; }
      var d = t.dataset;
      var pv = document.createElement('div'); pv.className = 'insp-preview'; pv.style.setProperty('--u', 'url(' + d.u + ')');
      var name = document.createElement('div'); name.className = 'insp-name'; name.textContent = d.n;
      insp.info.appendChild(pv); insp.info.appendChild(name);
      insp.info.appendChild(row('Taken', d.taken));
      insp.info.appendChild(row('Camera', d.cam));
      insp.info.appendChild(row('Size', d.size));
      if (d.k === 'video') insp.info.appendChild(row('Duration', d.d));
      var sr = row('Sharpness', d.sharp + '%'); var bar = document.createElement('span'); bar.className = 'insp-bar'; bar.style.setProperty('--p', d.sharp / 100); sr.appendChild(bar);
      insp.info.appendChild(sr);
      var parts = d.cat.split(', '), cname = parts[0], pct = parts[1] || '', less = parts[2];
      var discovered = FIXED.indexOf(cname) < 0;
      insp.cats.appendChild(row('Category', discovered ? 'other' : cname + '  ' + pct));
      if (discovered) insp.cats.appendChild(row('Discovered', cname + '  ' + pct));
      if (less) insp.cats.appendChild(row('Confidence', pct + ', less sure'));
      insp.cats.appendChild(row('Aerial', d.a === '1' ? 'yes' : 'no'));
      var f = d.k === 'video' ? 'not scanned in clips' : (d.faces === '0' ? 'none' : d.faces === '1' ? 'one' : d.faces === '2' ? 'two' : 'group of ' + d.faces);
      insp.faces.appendChild(row('Faces', f));
    }

    /* the visitor: hover fills the inspector, typing runs the same search, any touch pauses the script */
    var hovered = null, lastVisitor = 0;
    var touch = function () { lastVisitor = Date.now(); };
    grid.addEventListener('mouseover', function (e) { var t = e.target.closest('.tile'); if (t && t !== hovered) { hovered = t; fill(t); } });
    grid.addEventListener('mouseleave', function () { hovered = null; fill(lead); });
    grid.addEventListener('focusin', function (e) { var t = e.target.closest('.tile'); if (t) { hovered = t; fill(t); } });
    var typingTimer = null;
    input.addEventListener('input', function () { touch(); clearTimeout(typingTimer); typingTimer = setTimeout(function () { apply(input.value, aerial.checked); }, 120); });
    input.addEventListener('focus', function () { touch(); scriptStop(); });
    form.addEventListener('submit', function (e) { e.preventDefault(); touch(); apply(input.value, aerial.checked); });
    aerial.addEventListener('change', function () { touch(); scriptStop(); apply(input.value, aerial.checked); });
    navs.forEach(function (n) { n.addEventListener('click', function () { touch(); scriptStop(); show(n.dataset.view); }); });

    function show(view) {
      navs.forEach(function (n) { n.classList.toggle('on', n.dataset.view === view); });
      Object.keys(views).forEach(function (k) { views[k].hidden = k !== view; });
      if (!views[view]) views.search.hidden = false;
      if (!views[view]) navs.forEach(function (n) { n.classList.toggle('on', n.dataset.view === 'search'); });
    }

    /* tilt: the image leans 2 to 3 degrees toward the pointer, on its own element so FLIP keeps the tile's transform */
    if (MODE === 'motion' && fine) {
      tiles.forEach(function (t) {
        var img = $('.img', t);
        t.addEventListener('mousemove', function (e) {
          var r = t.getBoundingClientRect();
          var px = (e.clientX - r.left) / r.width - 0.5, py = (e.clientY - r.top) / r.height - 0.5;
          gsap.to(img, { rotateY: px * 6, rotateX: -py * 6, transformPerspective: 500, duration: 0.5, ease: 'power2.out', overwrite: 'auto' });
        });
        t.addEventListener('mouseleave', function () { gsap.to(img, { rotateY: 0, rotateX: 0, duration: 0.8, ease: 'power3.out', overwrite: 'auto' }); });
      });
    }

    /* the script: types, searches, ticks, merges; yields to the visitor and resumes when they leave it alone */
    var running = false, stopped = false, visible = true;
    if ('IntersectionObserver' in window) { new IntersectionObserver(function (es) { visible = es[0].isIntersecting; }, { threshold: 0.15 }).observe(frame); }
    function scriptStop() { stopped = true; }
    async function idle() {
      while (!visible || Date.now() - lastVisitor < 6000 || document.activeElement === input) await sleep(300);
      stopped = false;
    }
    async function type(text) {
      input.classList.add('typing');
      input.value = '';
      for (var c = 0; c < text.length; c += 1) {
        if (stopped) return false;
        input.value += text[c];
        await sleep(46 + Math.random() * 54);
      }
      return true;
    }
    async function untype() {
      while (input.value.length && !stopped) { input.value = input.value.slice(0, -1); await sleep(18); }
      input.classList.remove('typing');
    }
    async function hold(ms) { var t0 = Date.now(); while (Date.now() - t0 < ms) { if (stopped) return false; await sleep(100); } return true; }

    var mergeRow = $('#app-merge'), same = $('#app-same'), left = $('#app-merge-left'), right = $('#app-merge-right'), rightSide = $('#app-merge-rightside');
    var mergeName = $('#app-merge-name'), meeraCount = $('#app-meera-count');
    async function merge(rowEl, sameBtn, leftEl, rightEl, rightSideEl, nameEl, capFn) {
      sameBtn.classList.add('pressed');
      await sleep(220);
      sameBtn.classList.remove('pressed');
      var faces = $$('.face', rightEl), s = rowEl.closest('.app') ? scaleOf() : 1;
      var lr = leftEl.getBoundingClientRect();
      var gap = 4;
      if (MODE === 'motion') {
        var moves = faces.map(function (f, i) {
          var fr = f.getBoundingClientRect();
          var tx = (lr.right + gap + i * (fr.width + gap) - fr.left) / s, ty = (lr.top - fr.top) / s;
          return gsap.to(f, { x: tx, y: ty, duration: 0.7, ease: 'power3.inOut', delay: i * 0.05 });
        });
        rowEl.classList.add('done');
        await sleep(900);
        moves.forEach(function (m) { m.kill(); });
      } else {
        rowEl.classList.add('done');
      }
      faces.forEach(function (f) { if (window.gsap) gsap.set(f, { clearProps: 'transform' }); leftEl.appendChild(f); });
      rightSideEl.hidden = true;
      if (capFn) capFn();
    }
    function mergeReset(rowEl, leftEl, rightEl, rightSideEl, n) {
      var faces = $$('.face', leftEl).slice(n);
      faces.forEach(function (f) { rightEl.appendChild(f); });
      rightSideEl.hidden = false;
      rowEl.classList.remove('done');
    }

    async function loop() {
      running = true;
      while (true) {
        await idle();
        show('search'); aerial.checked = false; apply('', false);
        if (!(await hold(1400))) continue;
        if (!(await type('woman in a sari'))) continue;
        await sleep(300); apply(input.value, false);
        if (!(await hold(3000))) continue;
        await untype(); apply('', false);
        if (!(await hold(500))) continue;
        if (!(await type('fishing boats'))) continue;
        await sleep(300); apply(input.value, false);
        if (!(await hold(3000))) continue;
        await untype(); apply('', false);
        if (!(await hold(600))) continue;
        aerial.checked = true; apply('', true);
        if (!(await hold(2800))) continue;
        aerial.checked = false; apply('', false);
        if (!(await hold(700))) continue;
        show('people');
        if (!(await hold(1300))) continue;
        await merge(mergeRow, same, left, right, rightSide, mergeName, function () { mergeName.textContent = 'Hari, 72 photos'; meeraCount.textContent = '72 photos'; });
        if (!(await hold(2400))) continue;
        mergeReset(mergeRow, left, right, rightSide, 2); mergeName.textContent = 'Hari'; meeraCount.textContent = '38 photos';
        show('search');
        if (!(await hold(400))) continue;
      }
    }

    return {
      start: function () { if (!running) setTimeout(loop, 900); },
      still: function () { input.value = 'woman in a sari'; input.classList.add('typing'); apply(input.value, false); },
      search: search,
      tiles: tiles,
      merge: merge,
      mergeReset: mergeReset
    };
  })();


  /* ===== the sorting scene: loose tiles float, then fly into the pastel folders as you scroll ===== */
  function sorter() {
    var pin = $('#sorter-pin'), tiles = $$('.p-tile'), folders = $$('.pfolder'), flaps = $$('.pflap'), a = $('#sorter-a'), b = $('#sorter-b');
    if (!pin || MODE !== 'motion') return;
    tiles.forEach(function (t, i) {
      gsap.to($('b', t), { y: 6 + (i % 3) * 3, duration: 2.2 + (i % 5) * 0.35, ease: 'sine.inOut', yoyo: true, repeat: -1, delay: -(i * 0.37) });
    });
    gsap.set(flaps, { rotateX: -70, transformPerspective: 500 });
    var tl = gsap.timeline({
      defaults: { ease: 'power2.inOut' },
      scrollTrigger: { trigger: pin, start: 'top top', end: '+=140%', pin: true, scrub: 0.8, anticipatePin: 1, invalidateOnRefresh: true }
    });
    tiles.forEach(function (t, i) {
      var k = Number(t.getAttribute('data-to'));
      tl.to(t, {
        x: function () { var tr = t.getBoundingClientRect(), dr = folders[k].getBoundingClientRect(); return dr.left + dr.width / 2 - (tr.left + tr.width / 2 - gsap.getProperty(t, 'x')); },
        y: function () { var tr = t.getBoundingClientRect(), dr = folders[k].getBoundingClientRect(); return dr.top + dr.height * 0.42 - (tr.top + tr.height / 2 - gsap.getProperty(t, 'y')); },
        rotation: 0, scale: 0.42, duration: 1
      }, 0.1 + (i % 8) * 0.06);
      tl.to(t, { opacity: 0, duration: 0.12 }, 1.0 + (i % 8) * 0.06);
    });
    tl.to(a, { opacity: 0, y: -12, duration: 0.2 }, 0.9);
    tl.to(b, { opacity: 1, y: 0, duration: 0.2 }, 1.05);
    tl.to(flaps, { rotateX: 0, duration: 0.25, stagger: 0.06, ease: 'power2.in' }, 1.35);
    tl.to({}, { duration: 0.25 }, 1.75);
  }

  /* ===== pastel blobs drift behind everything, transform only ===== */
  function blobs() {
    if (MODE !== 'motion') return;
    $$('.blob').forEach(function (bl, i) {
      gsap.to(bl, { x: (i % 2 ? -1 : 1) * (60 + i * 20), y: (i % 3 ? 1 : -1) * (40 + i * 14), duration: 18 + i * 5, ease: 'sine.inOut', yoyo: true, repeat: -1, delay: -i * 4 });
    });
  }

  /* ===== the pinned story: five stages, one frame ===== */
  function story() {
    var pin = $('#story-pin');
    if (!pin) return;
    var steps = $$('.step'), live = $('#step-live'), rail = $('#rail-fill');
    var layers = ['#l-plug', '#l-index', '#l-search', '#l-people', '#l-export'].map(function (s) { return $(s); });
    var navMap = ['s-nav-search', 's-nav-index', 's-nav-search', 's-nav-people', 's-nav-search'];
    var counts = { photos: $('#s-count-photos'), clips: $('#s-count-clips'), people: $('#s-count-people'), cats: $('#s-count-cats') };
    var disk = $('#disk'), dockLine = $('#dock-line'), folder = $('#s-folder'), sstatus = $('#s-status');
    var progFill = $('#prog-fill'), progText = $('#prog-text'), ticker = $('#ticker');
    var qText = $('#s-q-text'), sHits = $$('#s-grid .s-tile.hit'), sLine = $('#s-line');
    var links = $$('#links path'), cFaces = $$('.c-face'), cloudLine = $('#cloud-line');
    var fTiles = $$('.f-tile'), flaps = $$('.folder .flap'), folders = $$('.folder'), flyLine = $('#fly-line');

    if (MODE !== 'motion') { steps.forEach(function (s) { s.classList.add('is-on'); }); return; }

    /* starts. the CSS holds the finished frame of every stage; motion mode rewinds each one here */
    var c = { photos: 0, clips: 0, people: 0, cats: 0, items: 0 };
    gsap.set(layers.slice(1), { autoAlpha: 0, y: 24 });
    gsap.set(rail, { scaleY: 0 });
    var slot = $('.dock-slot');
    gsap.set(disk, { x: 320, opacity: 0 });
    gsap.set(dockLine, { opacity: 0 });
    folder.textContent = 'no folder open'; sstatus.textContent = 'Idle';
    Object.keys(counts).forEach(function (k) { counts[k].textContent = '0'; });
    gsap.set(progFill, { scaleX: 0 });
    progText.textContent = 'features  0/955';
    gsap.set(ticker, { y: 0 });
    var qW = qText.scrollWidth;
    gsap.set(qText, { width: 0 });
    gsap.set(sHits, { opacity: 0.28, borderColor: 'transparent' });
    gsap.set(sLine, { opacity: 0 });
    links.forEach(function (p) { var len = p.getTotalLength(); gsap.set(p, { strokeDasharray: len, strokeDashoffset: len }); });
    var cloud = $('#cloud');
    gsap.set(cloudLine, { opacity: 0 });
    fTiles.forEach(function (t, i) { t.style.setProperty('--fx', 8 + (i % 3) * 31); t.style.setProperty('--fy', 2 + Math.floor(i / 3) * 33); });
    gsap.set(flaps, { rotateX: -78, transformPerspective: 260 });
    gsap.set(flyLine, { opacity: 0 });

    var stageText = steps.map(function (s) { return $('p', s).textContent; });
    function setStep(i) {
      steps.forEach(function (s, k) { s.classList.toggle('is-on', k === i); });
      if (live) live.textContent = stageText[i];
      $$('.s-nav').forEach(function (n) { n.classList.toggle('on', n.id === navMap[i]); });
    }
    setStep(0);

    var tl = gsap.timeline({
      defaults: { ease: 'none' },
      scrollTrigger: {
        trigger: pin, start: 'top top', end: '+=420%', pin: true, scrub: 0.6, anticipatePin: 1, invalidateOnRefresh: true,
        onUpdate: function (self) { setStep(Math.min(4, Math.floor(self.progress * 5))); }
      }
    });
    tl.to(rail, { scaleY: 1, duration: 5 }, 0);

    /* 1 plug in: the disk docks, the counts run */
    tl.to(disk, { x: 0, opacity: 1, duration: 0.35, ease: 'power2.out' }, 0.05);
    tl.to(slot, { opacity: 0, duration: 0.08 }, 0.36);
    tl.to(dockLine, { opacity: 1, duration: 0.1 }, 0.42);
    tl.to(c, { photos: 630, clips: 325, people: 4, cats: 18, items: 955, duration: 0.4, onUpdate: function () {
      counts.photos.textContent = Math.round(c.photos); counts.clips.textContent = Math.round(c.clips);
      counts.people.textContent = Math.round(c.people); counts.cats.textContent = Math.round(c.cats);
      folder.textContent = c.items > 0 ? 'SHOOT_2026_09' : 'no folder open';
      sstatus.textContent = c.items > 0 ? Math.round(c.items) + ' items found' : 'Idle';
    } }, 0.45);
    tl.to(layers[0], { autoAlpha: 0, y: -24, duration: 0.12 }, 0.9);
    tl.to(layers[1], { autoAlpha: 1, y: 0, duration: 0.12 }, 0.9);

    /* 2 index: the bar fills, file names stream past */
    var tickH = ticker.scrollHeight - ticker.parentNode.clientHeight;
    var p = { n: 0 };
    tl.to(progFill, { scaleX: 1, duration: 0.7 }, 1.08);
    tl.to(p, { n: 955, duration: 0.7, onUpdate: function () {
      var n = Math.round(p.n), left = Math.ceil((1 - n / 955) * 11);
      progText.textContent = n >= 955 ? 'features  955/955  done' : 'features  ' + n + '/955  about ' + Math.max(1, left) + ' min left';
      sstatus.textContent = n >= 955 ? '955 items in the index' : 'indexing ' + n + ' of 955';
    } }, 1.08);
    tl.to(ticker, { y: -Math.max(0, tickH), duration: 0.72 }, 1.08);
    tl.to(layers[1], { autoAlpha: 0, y: -24, duration: 0.12 }, 1.9);
    tl.to(layers[2], { autoAlpha: 1, y: 0, duration: 0.12 }, 1.9);

    /* 3 search: the query reveals, the matches light */
    tl.to(qText, { width: qW, duration: 0.3 }, 2.08);
    tl.to(sHits, { opacity: function (i, el) { return el.classList.contains('unsure') ? 0.55 : 1; }, borderColor: '#2680eb', duration: 0.1, stagger: 0.07 }, 2.42);
    tl.to(sLine, { opacity: 1, duration: 0.1 }, 2.72);
    tl.to(layers[2], { autoAlpha: 0, y: -24, duration: 0.12 }, 2.9);
    tl.to(layers[3], { autoAlpha: 1, y: 0, duration: 0.12 }, 2.9);

    /* 4 people: lines draw between the faces, the faces gather into one row */
    tl.to(links, { strokeDashoffset: 0, duration: 0.25, stagger: 0.03 }, 3.08);
    cFaces.forEach(function (f, i) {
      var sx = parseFloat(f.style.getPropertyValue('--x')), sy = parseFloat(f.style.getPropertyValue('--y'));
      tl.to(f, {
        x: function () { var r = cloud.getBoundingClientRect(); return ((62 + i * 66) - sx) / 520 * r.width; },
        y: function () { var r = cloud.getBoundingClientRect(); return (150 - sy) / 260 * r.height; },
        duration: 0.3, ease: 'power2.inOut'
      }, 3.42 + i * 0.02);
    });
    tl.to(links, { opacity: 0, duration: 0.1 }, 3.45);
    tl.to(cloudLine, { opacity: 1, duration: 0.1 }, 3.72);
    tl.to(layers[3], { autoAlpha: 0, y: -24, duration: 0.12 }, 3.9);
    tl.to(layers[4], { autoAlpha: 1, y: 0, duration: 0.12 }, 3.9);

    /* 5 export: tiles fly into folders, the folders fold shut */
    fTiles.forEach(function (t, i) {
      var k = Number(t.getAttribute('data-to'));
      tl.to(t, { x: function () {
        var tr = t.getBoundingClientRect(), dr = folders[k].getBoundingClientRect();
        return dr.left + dr.width / 2 - (tr.left + tr.width / 2 - gsap.getProperty(t, 'x'));
      }, y: function () {
        var tr = t.getBoundingClientRect(), dr = folders[k].getBoundingClientRect();
        return (dr.top + dr.height / 2 - 6 - (tr.top + tr.height / 2 - gsap.getProperty(t, 'y')));
      }, scale: 0.3, opacity: 0, duration: 0.3, ease: 'power2.in' }, 4.1 + i * 0.04);
    });
    tl.to(flaps, { rotateX: 0, duration: 0.15, stagger: 0.05, ease: 'power2.in' }, 4.55);
    tl.to(flyLine, { opacity: 1, duration: 0.1 }, 4.78);
    tl.to({}, { duration: 0.2 }, 4.8);
  }



  /* ===== deliver: the link pastes, Upload presses, packets run the arrows, the client's folder fills, the count ticks ===== */
  function deliver() {
    var flow = $('#flow');
    if (!flow || MODE !== 'motion') return;
    var link = $('#fa-link'), caret = $('#fa-caret'), btn = $('#fa-btn'), status = $('#fa-status'), tiles = $$('#fd-grid i'), count = $('#fd-count');
    var arrows = $$('.flow-arrow', flow), pkts = $$('.pkt', flow);
    var linkW = link.scrollWidth, vertical = function () { return window.innerWidth <= 1100; };
    var n = { v: 0 }, busy = false;
    function reset() {
      gsap.set(link, { width: 0 }); gsap.set(caret, { opacity: 1 });
      gsap.set(tiles, { opacity: 0, scale: 0.6 }); gsap.set(pkts, { opacity: 0, x: 0, y: 0 });
      count.textContent = '0 of 372'; status.textContent = 'ready'; n.v = 0;
    }
    function play() {
      if (busy) return; busy = true;
      reset();
      var tl = gsap.timeline({ onComplete: function () { busy = false; } });
      tl.to(link, { width: linkW, duration: 0.9, ease: 'none' }, 0.2);
      tl.to(caret, { opacity: 0, duration: 0.1 }, 1.2);
      tl.add(function () { btn.classList.add('pressed'); status.textContent = 'uploading from the SSD, nothing staged'; }, 1.5);
      tl.add(function () { btn.classList.remove('pressed'); }, 1.75);
      arrows.forEach(function (ar, k) {
        var ps = $$('.pkt', ar), len = vertical() ? ar.clientHeight : ar.clientWidth;
        ps.forEach(function (pk, i) {
          var v = vertical() ? { y: len - 8 } : { x: len - 11 };
          tl.fromTo(pk, { opacity: 1 }, Object.assign({ duration: 0.7, ease: 'power1.inOut', repeat: 3, repeatDelay: 0.1 }, v), 1.8 + k * 0.5 + i * 0.28);
          tl.to(pk, { opacity: 0, duration: 0.1 }, 4.9 + k * 0.5);
        });
      });
      tl.to(tiles, { opacity: 1, scale: 1, duration: 0.35, ease: 'back.out(1.6)', stagger: 0.22 }, 2.4);
      tl.to(n, { v: 372, duration: 2.9, ease: 'power1.inOut', onUpdate: function () { count.textContent = Math.round(n.v) + ' of 372'; } }, 2.4);
      tl.add(function () { status.textContent = '372 of 372 uploaded, 0 skipped, manifest written'; }, 5.4);
    }
    reset();
    ScrollTrigger.create({ trigger: flow, start: 'top 72%', once: true, onEnter: function () { setTimeout(play, 300); } });
    btn.addEventListener('click', play);
  }

  /* ===== numbers: the bars draw themselves, the values tick, once, when the chart scrolls in ===== */
  function numbers() {
    var charts = $$('.chart');
    if (!charts.length) return;
    function fmt(kind, v) {
      if (kind === 'ms') { var m = Math.floor(v / 60), sec = Math.round(v - m * 60); return m + ' min ' + sec + ' s'; }
      if (kind === 's1') return v.toFixed(1) + ' s';
      return Math.round(v) + ' s';
    }
    var strip = $('#strip');
    if (MODE !== 'motion') { return; }
    if (strip) {
      var half = strip.scrollWidth / 2;
      gsap.to(strip, { x: -half, duration: 16, ease: 'none', repeat: -1,
        scrollTrigger: { trigger: strip, start: 'top bottom', end: 'bottom top', toggleActions: 'play pause resume pause' } });
    }
    charts.forEach(function (ch) {
      var bars = $$('.bar', ch), fills = $$('.bar-fill', ch), big = $('.big', ch), stack = $('.stack', ch);
      if (fills.length) gsap.set(fills, { scaleX: 0 });
      bars.forEach(function (b) { $('.bar-v', b).textContent = fmt(b.dataset.fmt, 0); });
      if (big) big.textContent = '0';
      var stackBars = stack ? $$('.stack-track b', stack) : [];
      var stackVals = stack ? $$('.stack-v', stack) : [];
      if (stack) { gsap.set(stackBars, { scaleX: 0 }); stackVals.forEach(function (v) { v.textContent = '0 ms'; }); }
      var tl = gsap.timeline({ scrollTrigger: { trigger: ch, start: 'top 78%', once: true } });
      bars.forEach(function (b, i) {
        var fill = $('.bar-fill', b), val = $('.bar-v', b), sec = parseFloat(b.dataset.sec), max = parseFloat(b.dataset.max), o = { v: 0 };
        var d = 0.5 + 1.0 * (sec / max);
        tl.to(fill, { scaleX: sec / max, duration: d, ease: 'power3.out' }, i * 0.12);
        tl.to(o, { v: sec, duration: d, ease: 'power3.out', onUpdate: function () { val.textContent = fmt(b.dataset.fmt, o.v); } }, i * 0.12);
      });
      if (big) { var n = { v: 0 }, target = parseFloat(big.dataset.n); tl.to(n, { v: target, duration: 1.6, ease: 'power3.out', onUpdate: function () { big.textContent = Math.round(n.v).toLocaleString('en-IN'); } }, 0); }
      if (stack) {
        tl.to(stackBars, { scaleX: 1, duration: 0.9, ease: 'power3.out', stagger: 0.15 }, 0.3);
        stackVals.forEach(function (v, i) { var o = { v: 0 }, t = parseFloat(v.dataset.ms); tl.to(o, { v: t, duration: 0.9, ease: 'power3.out', onUpdate: function () { v.textContent = Math.round(o.v) + ' ms'; } }, 0.3 + i * 0.15); });
      }
    });
  }

  /* ===== the clip strip: hover scrubs the playhead, the frame follows the segment ===== */
  (function () {
    var clip = $('#clip'), track = $('#clip-track'), head = $('#playhead'), time = $('#clip-time');
    if (!clip) return;
    var segs = $$('.segment', track), layers = $$('.clip-layer', clip), TOTAL = 168;
    function at(frac) {
      frac = Math.max(0, Math.min(1, frac));
      var r = track.getBoundingClientRect();
      head.style.setProperty('--x', (12 + frac * (r.width - 24)) + 'px');
      var sec = Math.round(frac * TOTAL), m = Math.floor(sec / 60), s = sec % 60;
      time.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
      var acc = 0, idx = 0, total = segs.reduce(function (a, sg) { return a + parseFloat(sg.style.getPropertyValue('--w')); }, 0);
      for (var i = 0; i < segs.length; i += 1) { acc += parseFloat(segs[i].style.getPropertyValue('--w')) / total; if (frac <= acc + 0.0001) { idx = i; break; } idx = i; }
      segs.forEach(function (sg, i) { sg.classList.toggle('on', i === idx); });
      layers.forEach(function (l, i) { l.classList.toggle('on', i === idx); });
    }
    at(0);
    var crawl = null, hovering = false;
    if (MODE === 'motion') {
      var pos = { f: 0 };
      crawl = gsap.to(pos, { f: 1, duration: 14, ease: 'none', repeat: -1, onUpdate: function () { if (!hovering) at(pos.f); },
        scrollTrigger: { trigger: clip, start: 'top bottom', end: 'bottom top', toggleActions: 'play pause resume pause' } });
    }
    track.addEventListener('mousemove', function (e) { hovering = true; var r = track.getBoundingClientRect(); at((e.clientX - r.left - 12) / (r.width - 24)); });
    track.addEventListener('mouseleave', function () { hovering = false; });
    track.addEventListener('touchmove', function (e) { var r = track.getBoundingClientRect(); at((e.touches[0].clientX - r.left - 12) / (r.width - 24)); }, { passive: true });
  })();

  /* ===== every face, one person: the big merge row plays once in view, replays on Same ===== */
  (function () {
    var row = $('#big-merge'), same = $('#big-same'), diff = $('#big-diff'), left = $('#big-left'), right = $('#big-right'), rightSide = $('#big-rightside'), name = $('#big-name'), cap = $('#merge-cap');
    if (!row || !app) return;
    var busy = false;
    async function play() {
      if (busy) return; busy = true;
      await app.merge(row, same, left, right, rightSide, name, function () { name.textContent = 'Hari, 72 photos'; cap.textContent = 'Joined. The answer is kept on every regrouping.'; });
      await sleep(2600);
      app.mergeReset(row, left, right, rightSide, 2);
      name.textContent = 'Hari'; cap.textContent = 'Press Same. The groups join and the answer is kept.';
      busy = false;
    }
    same.addEventListener('click', play);
    diff.addEventListener('click', function () { if (busy) return; cap.textContent = 'Kept apart. Also remembered.'; setTimeout(function () { cap.textContent = 'Press Same. The groups join and the answer is kept.'; }, 1800); });
    if (MODE === 'motion' && 'IntersectionObserver' in window) {
      var io = new IntersectionObserver(function (es) { if (es[0].isIntersecting) { io.disconnect(); setTimeout(play, 900); } }, { threshold: 0.6 });
      io.observe(row);
    }
  })();

  /* ===== ten ways in: each chip runs the same search over the same tiles ===== */
  (function () {
    var chips = $$('.chip'), grid = $('#uc-grid'), q = $('#uc-q'), n = $('#uc-n');
    if (!chips.length || !app) return;
    var current = null;
    function preview(chip) {
      if (chip === current) return;
      current = chip;
      chips.forEach(function (c) { c.classList.toggle('on', c === chip); });
      var query = chip.getAttribute('data-q');
      var res = query === 'aerial' ? app.search('', true) : app.search(query, false);
      q.textContent = query === 'aerial' ? 'drone shots' : query;
      var score = {};
      res.hits.forEach(function (h) { score[h.t.dataset.i] = h.s; });
      var hits = res.hits.length;
      if (!grid.children.length) {
        app.tiles.slice().sort(function (a, b) { return a.dataset.i - b.dataset.i; }).forEach(function (src) {
          var t = document.createElement('i'); t.className = 'uc-tile'; t.dataset.i = src.dataset.i;
          t.style.setProperty('--u', 'url(' + src.dataset.u + ')');
          grid.appendChild(t);
        });
      }
      var k = 0;
      $$('.uc-tile', grid).forEach(function (t) {
        var hit = t.dataset.i in score, un = hit && res.max > 1 && score[t.dataset.i] < res.max;
        t.classList.toggle('hit', hit); t.classList.toggle('unsure', un);
        if (hit && MODE === 'motion') { gsap.fromTo(t, { scale: 0.92 }, { scale: 1, duration: 0.4, delay: k * 0.05, ease: 'back.out(2)' }); k += 1; }
      });
      n.textContent = hits ? hits + ' of ' + app.tiles.length + (query === 'aerial' ? ', from the metadata' : '') : 'nothing matched';
    }
    chips.forEach(function (c) {
      c.addEventListener('mouseenter', function () { preview(c); });
      c.addEventListener('focus', function () { preview(c); });
      c.addEventListener('click', function () { preview(c); });
    });
    preview(chips[0]);
  })();

  /* ===== reveals ===== */
  function splitLines(el) {
    if (!el.dataset.text) {
      el.dataset.text = el.innerHTML.split(/<br\s*\/?>/i).map(function (seg) {
        var d = document.createElement('div'); d.innerHTML = seg;
        $$('.wordmark', d).forEach(function (w) { w.textContent = PRODUCT; w.classList.remove('wordmark'); w.classList.add('wordmark-in'); });
        return d.innerHTML.replace(/\s+/g, ' ').trim();
      }).join('\n');
    }
    var segs = el.dataset.text.split('\n');
    el.innerHTML = segs.map(function (seg, si) {
      var d = document.createElement('div'); d.innerHTML = seg;
      return d.textContent.split(' ').map(function (w) { return '<span class="w" data-seg="' + si + '">' + w + '</span>'; }).join(' ');
    }).join('<br>');
    var spans = $$('.w', el), lines = [], cur = [], lastTop = null, lastSeg = null;
    spans.forEach(function (s) {
      var t = s.offsetTop, seg = s.getAttribute('data-seg');
      if (cur.length && (seg !== lastSeg || Math.abs(t - lastTop) > 2)) { lines.push(cur); cur = []; }
      cur.push(s.textContent); lastTop = t; lastSeg = seg;
    });
    if (cur.length) lines.push(cur);
    el.innerHTML = lines.map(function (l) { return '<span class="line-mask"><span>' + l.join(' ') + '</span></span>'; }).join('');
    return $$('.line-mask > span', el);
  }

  if (MODE === 'reduce') {
    if (app) app.still();
    story();
    var els = $$('[data-reveal]');
    if ('IntersectionObserver' in window) {
      var io2 = new IntersectionObserver(function (es) { es.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add('is-in'); io2.unobserve(en.target); } }); }, { rootMargin: '0px 0px -8% 0px' });
      els.forEach(function (el) { io2.observe(el); });
    }
    return;
  }

  var lineEntries = [];
  function revealLines(el, immediate, delay) {
    var spans = splitLines(el);
    gsap.set(spans, { y: 0, yPercent: 105 });
    el.classList.add('is-ready');
    var entry = { el: el, done: false, tween: null };
    var vars = { yPercent: 0, duration: 1.1, ease: 'power4.out', stagger: 0.08, delay: delay || 0, onComplete: function () { entry.done = true; } };
    if (!immediate) vars.scrollTrigger = { trigger: el, start: 'top 88%', once: true };
    entry.tween = gsap.to(spans, vars);
    lineEntries.push(entry);
  }
  function revealFade(el, immediate, delay) {
    el.classList.add('is-ready');
    gsap.set(el, { opacity: 0, y: 12 });
    var vars = { opacity: 1, y: 0, duration: 0.9, ease: 'power3.out', delay: delay || 0 };
    if (!immediate) vars.scrollTrigger = { trigger: el, start: 'top 90%', once: true };
    gsap.to(el, vars);
  }
  function revealUnmask(el, immediate, delay) {
    el.classList.add('is-ready');
    gsap.set(el, { clipPath: 'inset(0 0 100% 0)' });
    var vars = { clipPath: 'inset(0 0 0% 0)', duration: 1.3, ease: 'power4.inOut', delay: delay || 0, onComplete: function () { el.style.clipPath = 'none'; } };
    if (!immediate) vars.scrollTrigger = { trigger: el, start: 'top 85%', once: true };
    gsap.to(el, vars);
  }
  function revealRows(el, immediate, delay) {
    el.classList.add('is-ready');
    var kids = Array.prototype.slice.call(el.children);
    gsap.set(kids, { opacity: 0, y: 12 });
    var vars = { opacity: 1, y: 0, duration: 0.8, ease: 'power3.out', stagger: 0.08, delay: delay || 0 };
    if (!immediate) vars.scrollTrigger = { trigger: el, start: 'top 85%', once: true };
    gsap.to(kids, vars);
  }

  function init() {
    var hero = $('.hero');
    $$('[data-reveal]').forEach(function (el) {
      var kind = el.getAttribute('data-reveal');
      var inHero = hero.contains(el);
      var delay = 0;
      if (inHero) {
        if (el.classList.contains('badge')) delay = 0.1;
        else if (el.tagName === 'H1') delay = 0.2;
        else if (el.classList.contains('lede')) delay = 0.5;
        else if (kind === 'fade' && el.classList.contains('cta-row')) delay = 0.7;
        else if (kind === 'unmask') delay = 0.55;
        else delay = 1.4;
      }
      if (kind === 'lines') revealLines(el, inHero, delay);
      else if (kind === 'fade') revealFade(el, inHero, delay);
      else if (kind === 'unmask') revealUnmask(el, inHero, delay);
      else if (kind === 'rows') revealRows(el, inHero, delay);
    });
    if (app) app.start();
    blobs();
    sorter();
    story();
    deliver();
    numbers();

    var lastW = window.innerWidth, rt = null;
    window.addEventListener('resize', function () {
      clearTimeout(rt);
      rt = setTimeout(function () {
        if (Math.abs(window.innerWidth - lastW) < 60) return;
        lastW = window.innerWidth;
        lineEntries.forEach(function (en) {
          var spans = splitLines(en.el);
          if (en.done) { gsap.set(spans, { y: 0, yPercent: 0 }); }
          else {
            if (en.tween && en.tween.scrollTrigger) en.tween.scrollTrigger.kill();
            if (en.tween) en.tween.kill();
            gsap.set(spans, { y: 0, yPercent: 105 });
            en.tween = gsap.to(spans, { yPercent: 0, duration: 1.1, ease: 'power4.out', stagger: 0.08, onComplete: function () { en.done = true; }, scrollTrigger: { trigger: en.el, start: 'top 88%', once: true } });
          }
        });
        ScrollTrigger.refresh();
      }, 200);
    });
  }

  var ready = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
  Promise.race([ready, sleep(2500)]).then(function () { requestAnimationFrame(init); });
})();
