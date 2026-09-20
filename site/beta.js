/* Join the beta. One button: the tap is recorded, confetti, a line, then an email field.
   Storage is a Supabase table reached through two insert-only functions with the publishable key
   (nothing is readable from the browser). No sign-in, no cookies. */
(function () {
  var URL = 'https://woasffpwdbwavwcrtllz.supabase.co/rest/v1/rpc/';
  var KEY = 'sb_publishable_aAR90Pzf1gza49CP2t7uKQ_xyDhkTZI';
  var wrap = document.getElementById('beta-cta');
  var join = document.getElementById('beta-join');
  if (!wrap || !join) return;
  var note = wrap.parentNode.querySelector('.beta-note');
  var source = (location.hash || '').slice(1, 61) || 'site';

  function rpc(fn, args) {
    return fetch(URL + fn, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'apikey': KEY, 'authorization': 'Bearer ' + KEY },
      body: JSON.stringify(args)
    }).then(function (r) { return r.ok ? r.json().catch(function () { return true; }) : r.json().then(function (e) { throw new Error(e.message || 'failed'); }); });
  }
  function ua() { return navigator.userAgent.slice(0, 300); }
  function reduced() { return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; }

  /* confetti: a short burst from the button in the page's own pastels, drawn on one canvas */
  var COLORS = ['#f3c8d0', '#bcd8f0', '#bfe5cf', '#f2e3a2', '#d6cbee', '#26282d'];
  function confetti(el, count) {
    if (reduced()) return;
    var c = document.createElement('canvas'); c.className = 'confetti';
    c.width = innerWidth * devicePixelRatio; c.height = innerHeight * devicePixelRatio;
    c.style.width = innerWidth + 'px'; c.style.height = innerHeight + 'px';
    document.body.appendChild(c);
    var g = c.getContext('2d'); g.scale(devicePixelRatio, devicePixelRatio);
    var r = el.getBoundingClientRect(), ox = r.left + r.width / 2, oy = r.top + r.height / 2, bits = [];
    for (var i = 0; i < count; i++) {
      var a = -Math.PI / 2 + (Math.random() - 0.5) * 1.6, v = 7 + Math.random() * 9;
      bits.push({ x: ox, y: oy, vx: Math.cos(a) * v, vy: Math.sin(a) * v, w: 6 + Math.random() * 6, h: 4 + Math.random() * 4,
        rot: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.3, col: COLORS[i % COLORS.length], round: Math.random() < 0.3 });
    }
    var t0 = performance.now();
    (function frame(t) {
      var k = (t - t0) / 1000;
      g.clearRect(0, 0, innerWidth, innerHeight);
      var alive = 0;
      bits.forEach(function (b) {
        b.vy += 0.35; b.vx *= 0.985; b.x += b.vx; b.y += b.vy; b.rot += b.vr;
        if (b.y < innerHeight + 20) alive++;
        g.save(); g.translate(b.x, b.y); g.rotate(b.rot); g.globalAlpha = Math.max(0, 1 - Math.max(0, k - 1.4) / 0.6);
        g.fillStyle = b.col;
        if (b.round) { g.beginPath(); g.arc(0, 0, b.w / 2, 0, Math.PI * 2); g.fill(); } else { g.fillRect(-b.w / 2, -b.h / 2, b.w, b.h); }
        g.restore();
      });
      if (alive && k < 2.2) requestAnimationFrame(frame); else c.remove();
    })(t0);
  }

  function step2() {
    join.disabled = true; join.classList.add('pressed');
    confetti(join, 140);
    rpc('beta_tap', { p_source: source, p_user_agent: ua() }).catch(function () {});
    var box = document.createElement('form'); box.className = 'beta-mail'; box.noValidate = true;
    box.innerHTML =
      '<p class="beta-line">Nice. Where do we send the build?</p>' +
      '<div class="beta-mail-row"><input name="email" type="email" inputmode="email" autocomplete="email" placeholder="you@studio.in" required maxlength="200" aria-label="Email">' +
      '<button type="submit" class="cta beta-submit">Notify me</button></div>' +
      '<p class="beta-msg" aria-live="polite"></p>';
    wrap.insertAdjacentElement('afterend', box);
    if (note) note.hidden = true;
    requestAnimationFrame(function () { box.classList.add('in'); box.email.focus({ preventScroll: true }); });
    var msg = box.querySelector('.beta-msg'), btn = box.querySelector('.beta-submit');
    box.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = box.email.value.trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) { msg.textContent = 'That email does not look right.'; msg.classList.add('bad'); return; }
      btn.disabled = true; msg.classList.remove('bad'); msg.textContent = 'One second';
      rpc('beta_join', { p_email: email, p_source: source, p_user_agent: ua() }).then(function () {
        box.innerHTML = '<p class="beta-line big">You are in. We will notify you shortly with the build.</p><p class="beta-fine">One email, from purohit.krick@gmail.com. Nothing else.</p>';
        confetti(box.querySelector('.beta-line'), 60);
      }).catch(function (err) {
        btn.disabled = false; msg.classList.add('bad'); msg.textContent = err.message === 'failed' ? 'Could not save that. Try again in a moment.' : err.message;
      });
    });
  }
  join.addEventListener('click', step2, { once: true });
})();
