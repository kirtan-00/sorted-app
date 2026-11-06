/* sorted: the live shoot page (live.html). The camera writes into a folder, photosort/live.py checks each file and
   copies it, sorted, to the drive, and this page shows the result.

   GET  /api/live/state              the whole session, drawn settled: what happened before you looked does not replay
   GET  /api/live/events?after=seq   long poll: shot, shot_update, product_created, product_renamed, status
   GET  /api/live/volumes, /api/live/original/{id}
   POST /api/live/start /stop /active /rename-product /export-store /pick /hide
   GET  /api/export/destination      the app's export folder

   The latest shot holds the stage. When the next one is ready the shot on stage flies into its pile (its product, its
   model, or Soft), and only then does the pile count. A burst or a bracket is one group: one picture on stage, one
   flight. All of this is display: the copy to the drive happens on the server and never waits for it.
   ?client=1 is the client's window: the frame just taken, nothing else. Each window keeps its own seq on the long poll.
   ?demo=1 opens on the start screen and plays a scripted shoot with no camera and no server (&step=1 waits for
   __live.next(), &skip=N opens that far in, as a page opened mid-shoot does; __live.demo.drive(state) and .hide(id, on)
   reach the states a script cannot). */
"use strict";
(function () {
  const Q = new URLSearchParams(location.search);
  const CLIENT = Q.get("client") === "1";
  const DEMO = Q.get("demo") === "1";
  const $ = (id) => document.getElementById(id);
  const reduceQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const reduced = () => reduceQuery.matches;

  const FLIGHT_MS = 420;       // stage to pile
  const DWELL_MS = 500;        // the least a shot holds the stage while others wait
  const VERDICT_WAIT_MS = 2500; // a shot with no verdict gives up the stage after this, and lands when its verdict does
  const CATCH_UP = 6;          // this many groups behind: stop flying and jump to the newest
  const CROSSFADE_MS = 250;    // the client's two layers trade places in this long (live.css: 180 ms in, 60 ms out)
  const NO_PRODUCT = "none";
  const UNSORTED = "Unsorted"; // live.py's one pile when Find products is off
  const SOFT = "soft";
  const MODEL = "model:";      // a model's pile is keyed by name; products are keyed by id
  const PANEL_KEY = "sorted.live.panel", TAB_KEY = "sorted.live.tab", FOLDER_KEY = "sorted.live.folder", SHOOT_KEY = "sorted.live.shoot", FIND_KEY = "sorted.live.find";
  /* per viewer conveniences: storage can throw or come back empty, and the page works the same without it */
  const remember = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* nothing kept */ } };
  const recall = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };

  /* ---------- the session ---------- */
  const S = {
    seq: 0, phase: "stopped", running: false, folder: "", active: { product: "", model: "" }, behind: 0,
    resync: false, exporting: null, shoot: recall(SHOOT_KEY) || "", drive: null, driveWarned: "", setup: false, prev: null,
    holds: { pause: false, zoom: false, compare: false },
    find: readFind(), tab: recall(TAB_KEY) === "models" ? "models" : "products", panel: recall(PANEL_KEY) !== "hidden",
    counts: { shots: 0, sharp: 0, soft: 0, flash: 0 },
    shots: new Map(), groups: new Map(), piles: new Map(),
    current: null, queue: [], paused: false, online: false, connected: false, view: "picture",
  };
  const flights = [];          // every flight, for the headless checks
  let pumpTimer = 0;

  function readFind() {
    try { const f = JSON.parse(recall(FIND_KEY)); if (f) return { faces: f.faces !== false, products: f.products !== false }; }
    catch (e) { /* nothing usable kept */ }
    return { faces: true, products: true };
  }

  /* The server's field names, read once here so nothing below cares how they were spelled. A later event for the same
     shot may carry only what changed, so raw is the shot as known so far. */
  function readShot(raw) {
    const has = (v) => v !== undefined && v !== null;
    const b = raw.burst, k = raw.bracket;
    const burst = !has(b) || b === false ? null : typeof b === "object" ? b : { id: b };
    const bracket = !has(k) || k === false ? null : typeof k === "object" ? k : { id: k };
    const pid = has(raw.product_id) ? raw.product_id : has(raw.product) && raw.product !== "" ? raw.product : NO_PRODUCT;
    return {
      raw: raw, id: raw.id, file: String(raw.file || raw.name || "").split("/").pop(), thumb: raw.preview || raw.thumb || "", small: raw.thumb || raw.preview || "",
      verdict: raw.verdict ? (raw.verdict === "sharp" ? "sharp" : "soft") : null, reason: raw.reason || "", kind: raw.kind || "",
      product: pid, productName: typeof raw.product === "string" ? raw.product : null, model: raw.model || "",
      burst: burst, bracket: bracket, flash: raw.flash === true || raw.flash === "fired",
      best: !!raw.best, sharp: [raw.sharp, raw.sharp_eye, raw.sharp_tile].find((v) => typeof v === "number") ?? null, ev: has(raw.ev) ? raw.ev : undefined, order: has(raw.n) ? raw.n : 0,
      alerts: Array.isArray(raw.alerts) ? raw.alerts : [], copy: raw.copy || "", hidden: !!raw.hidden,
      category: raw.category || (raw.kind === "model" ? "model" : "product"),
      landed: false,
    };
  }
  function groupKey(s) {
    if (s.burst) return "burst:" + (s.burst.id !== undefined ? s.burst.id : s.id);
    if (s.bracket) return "bracket:" + (s.bracket.id !== undefined ? s.bracket.id : s.id);
    return "shot:" + s.id;
  }
  function groupOf(s) {
    const key = groupKey(s);
    let g = S.groups.get(key);
    if (!g) { g = { key: key, frames: [], shownAt: 0, flown: false }; S.groups.set(key, g); }
    if (!g.frames.includes(s)) g.frames.push(s);
    return g;
  }
  /* A burst or a bracket is only known from its second or third frame, and the server says so once, on the newest frame,
     with the ids of the set. The frames before it are pulled in here; one still on stage takes the group with it. */
  function absorb(g, s) {
    const set = s.burst || s.bracket, field = s.burst ? "burst" : "bracket";
    let joined = false;
    ((set && set.ids) || []).forEach((id) => {
      const o = S.shots.get(id);
      if (!o || g.frames.includes(o)) return;
      const from = S.groups.get(groupKey(o));
      o.burst = null; o.bracket = null; o[field] = set;
      if (from) {
        from.frames = from.frames.filter((f) => f !== o);
        if (from === S.current) { S.current = g; g.shownAt = from.shownAt; joined = true; }
        if (!from.frames.length) { S.groups.delete(from.key); S.queue = S.queue.filter((q) => q !== from); }
      }
      g.frames.push(o);
    });
    g.frames.sort((a, b) => a.order - b.order);
    return joined;
  }
  function sharpest(g) {
    const told = g.frames.find((s) => s.best);
    if (told) return told;
    const scored = g.frames.filter((s) => s.sharp !== null);
    return scored.length ? scored.reduce((a, b) => (b.sharp > a.sharp ? b : a)) : null;
  }
  const lead = (g) => sharpest(g) || g.frames.find((s) => s.ev === 0) || g.frames.find((s) => s.verdict === "sharp") || g.frames[0];
  const groupReady = (g) => g.frames.every((s) => s.verdict);
  function groupVerdict(g) {
    if (g.frames.some((s) => s.verdict === "sharp")) return "sharp";
    return groupReady(g) ? "soft" : null;
  }
  /* soft goes to Soft; a sharp shot of a named model to that model; everything else to its product */
  const pileIdOf = (s) => (s.verdict !== "sharp" ? SOFT : s.category === "model" && s.model ? MODEL + s.model : s.product);
  const isModelPile = (id) => typeof id === "string" && id.startsWith(MODEL);
  const pileNameOf = (s) => (s.verdict === "sharp" && !isModelPile(pileIdOf(s)) ? s.productName : null);

  /* ---------- talking to the Mac ---------- */
  async function json(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (e) { /* no body to read */ }
      const err = new Error(detail || "The Mac returned an error (" + r.status + ")");
      err.status = r.status;
      throw err;
    }
    return r.status === 204 ? {} : r.json();
  }
  const http = {
    state: () => json("/api/live/state", { cache: "no-store" }),
    events: (after) => {
      const stop = new AbortController(), timer = setTimeout(() => stop.abort(), 45000);
      return json("/api/live/events?after=" + after, { signal: stop.signal, cache: "no-store" }).finally(() => clearTimeout(timer));
    },
    post: (path, body) => json("/api/live/" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }),
    pick: (purpose) => http.post("pick", { purpose: purpose }),
    volumes: () => json("/api/live/volumes", { cache: "no-store" }),
    original: async (id) => "/api/live/original/" + id,
    exportDest: () => json("/api/export/destination", { cache: "no-store" }),
  };
  const api = DEMO ? demoServer() : http;
  const sleep = (ms) => new Promise((go) => setTimeout(go, ms));

  async function connect() {
    let wait = 500;
    for (;;) {
      try {
        applySnapshot(await api.state());
        setOnline(true);
        wait = 500;
        for (;;) {
          const r = await api.events(S.seq);
          if (r.reset || (typeof r.seq === "number" && r.seq < S.seq)) break;   // the server restarted, or the log moved past us: reload
          (r.events || []).forEach((e) => {
            if (typeof e.seq === "number" && e.seq <= S.seq) return;  // a retried poll: already drawn
            onEvent(e);
            if (typeof e.seq === "number") S.seq = e.seq;
          });
          if (typeof r.seq === "number") S.seq = Math.max(S.seq, r.seq);
          if (S.resync) { S.resync = false; break; }
        }
      } catch (e) {
        if (e.name === "AbortError") continue;                         // our own timeout on a quiet poll, not a lost Mac
        setOnline(false);
        await sleep(wait);
        wait = Math.min(5000, wait * 2);
      }
    }
  }

  /* Draw the session as it stands, with no flights. Called on open and on every reconnect, so a shot seen twice is
     simply the same shot. A restarted server that has lost the session must not wipe a shoot off the TV. */
  function applySnapshot(st) {
    if (Z.open) zoomOut();
    compareShow(false);
    S.seq = st.seq || 0;
    readStatus(st);
    const rows = (st.shots || []).map(readShot);
    if (!rows.length && S.shots.size && !S.running) { paint(); return; }
    const keep = S.current && rows.length && lead(S.current).id === rows[rows.length - 1].id ? S.current : null;
    S.shots = new Map(); S.groups = new Map(); S.queue = []; clearTimeout(pumpTimer);
    rows.forEach((s) => { s.landed = !!s.verdict; S.shots.set(s.id, s); });
    rows.forEach((s) => { groupOf(s); });
    S.groups.forEach((g) => g.frames.sort((a, b) => a.order - b.order));
    S.groups.forEach((g) => { g.flown = true; });
    const c = st.counts || {}, verdictOf = (v) => rows.filter((s) => s.verdict === v).length;
    S.counts = { shots: c.shots !== undefined ? c.shots : rows.length, sharp: c.sharp !== undefined ? c.sharp : verdictOf("sharp"),
      soft: c.soft !== undefined ? c.soft : verdictOf("soft"), flash: c.flash !== undefined ? c.flash : rows.filter((s) => s.flash).length };
    rebuildPiles(st, rows);
    const last = rows.length ? S.groups.get(groupKey(rows[rows.length - 1])) : null;
    if (CLIENT) { clientSnapshot(); paint(); return; }
    if (last && !(keep && keep.key === last.key)) { S.current = last; last.shownAt = performance.now(); showHero(last, false); }
    else if (!last) { S.current = null; clearHero(); }
    else { S.current = last; last.shownAt = performance.now(); }
    paintStrip(); paint(); feedScene(true);
  }
  function rebuildPiles(st, rows) {
    const named = new Map((st.products || []).map((p) => [p.id, p]));
    const seen = new Set([SOFT]);
    (st.products || []).forEach((p) => { ensurePile(p.id, p.name, false); seen.add(p.id); });
    rows.forEach((s) => { if (s.verdict) { ensurePile(pileIdOf(s), pileNameOf(s), false); seen.add(pileIdOf(s)); } });
    (st.models || []).forEach((m) => { ensurePile(MODEL + m.name, m.name, false); seen.add(MODEL + m.name); });
    if (CLIENT) return;
    if (!S.find.products) { ensurePile(NO_PRODUCT, null, false); seen.add(NO_PRODUCT); }
    S.piles.forEach((p, id) => { if (!seen.has(id)) { p.el.remove(); S.piles.delete(id); } });
    S.piles.forEach((p, id) => {
      const mine = rows.filter((s) => s.verdict && pileIdOf(s) === id);
      const given = id === SOFT ? S.counts.soft : named.has(id) ? named.get(id).count : undefined;
      p.count = given !== undefined ? given : mine.length;
      p.thumbs = mine.slice(-3).map((s) => s.small).filter(Boolean);
      paintPile(p, false);
    });
  }
  function readStatus(st) {
    if (st.state !== undefined) { S.phase = st.state; S.running = st.state === "live" || st.state === "starting"; }
    else if (st.running !== undefined) { S.running = !!st.running; S.phase = S.running ? "live" : "stopped"; }
    if (st.folder !== undefined && st.folder !== null) S.folder = st.folder;
    if (st.shoot !== undefined && st.shoot !== null) S.shoot = st.shoot;
    if (st.drive !== undefined) S.drive = st.drive;
    if (st.faces !== undefined) S.find.faces = st.faces !== false;
    if (st.products_on !== undefined) S.find.products = st.products_on !== false;
    if (st.active) S.active = { product: st.active.product || "", model: st.active.model || "" };
    const behind = st.queue !== undefined ? st.queue : st.behind;
    if (behind !== undefined) S.behind = Math.max(0, Number(behind) || 0);
    if (st.export) readExport(st.export);
  }
  function readExport(x) {
    const was = S.exporting;
    S.exporting = x.running ? x : null;
    if (!was || x.running) return;
    if (x.error) say(x.error, true);
    else say("Exported " + shotWord(x.done || 0) + " to " + x.path + (x.failed ? ". " + shotWord(x.failed) + " could not be written" : ""), !!x.failed);
  }
  const shotWord = (n) => n + (n === 1 ? " shot" : " shots");

  function onEvent(e) {
    const type = e.type || e.event, body = e;
    if (type === "shot") {
      if (e.queue !== undefined) S.behind = Math.max(0, Number(e.queue) || 0);
      onShot(e);
    }
    else if (type === "shot_update") { const u = e.shot || e; if (S.shots.has(u.id)) onShot(u); }
    else if (type === "product_created") { if (!CLIENT) { ensurePile(body.id, body.name, true); feedScene(); } }
    else if (type === "product_renamed") { renamePile(body.id, body.name); }
    else if (type === "status") {
      const was = S.running;
      readStatus(body);
      if (S.running && !was) S.resync = true;          // a session started somewhere else: draw it from the top
      if (body.message && !CLIENT) say(body.message, true);
      paint();
    }
  }

  /* One shot: new, or more news about one already on screen */
  function onShot(raw) {
    const old = S.shots.get(raw.id);
    let wasHidden = false;
    const s = readShot(old ? { ...old.raw, ...raw } : raw);
    if (old) {
      const hadVerdict = old.verdict, hadFlash = old.flash, landed = old.landed;
      wasHidden = old.hidden;
      const before = landed ? pileIdOf(old) : null;
      const setBefore = old.burst || old.bracket ? { burst: old.burst, bracket: old.bracket } : null;
      Object.assign(old, s, { landed: landed }, s.burst || s.bracket || !setBefore ? {} : setBefore);
      if (before !== null && before !== pileIdOf(old) && !CLIENT) movePile(old, before);
      if (old.verdict && !hadVerdict) tallyVerdict(old);
      if (old.flash && !hadFlash) S.counts.flash++;
    } else {
      S.shots.set(s.id, s);
      S.counts.shots++;
      if (s.flash) S.counts.flash++;
      if (s.verdict) tallyVerdict(s);
    }
    const shot = old || s, g = groupOf(shot), fresh = !old && g.frames.length === 1;
    const onStage = absorb(g, shot);
    if (CLIENT) { clientOnShot(shot, !old, wasHidden); return; }
    if (shot.verdict) ensurePile(pileIdOf(shot), pileNameOf(shot), true);
    if (fresh && !onStage && g !== S.current) S.queue.push(g);
    if (g === S.current) { showHero(g, false, true); paintStrip(); }
    if (g.flown && shot.verdict && !shot.landed) landShot(shot, true);   // its verdict came after the group left the stage
    pump(); paint(); feedScene();
  }
  /* a landed shot the server reassigned: it changes piles with no flight */
  function movePile(s, from) {
    const a = S.piles.get(from), b = ensurePile(pileIdOf(s), pileNameOf(s), false);
    if (a) { a.count = Math.max(0, a.count - 1); paintPile(a, false); }
    b.count++; paintPile(b, false); paintTabs();
  }
  function tallyVerdict(s) { if (s.verdict === "sharp") S.counts.sharp++; else S.counts.soft++; }

  /* ---------- the stage ---------- */
  function pump() {
    clearTimeout(pumpTimer);
    if (S.paused) return;
    while (S.queue.length) {
      const now = performance.now(), cur = S.current, rushing = S.queue.length >= CATCH_UP;
      if (cur && !rushing) {
        const held = now - cur.shownAt;
        const waitFor = !groupReady(cur) ? VERDICT_WAIT_MS - held : DWELL_MS - held;
        if (waitFor > 0) { pumpTimer = setTimeout(pump, waitFor); break; }
      }
      const next = S.queue.shift(), still = S.queue.length >= CATCH_UP - 1;
      if (cur) { S.prev = cur; flyOut(cur, still); }
      S.current = next; next.shownAt = now;
      showHero(next, !still);
      paintStrip();
    }
    paint();
  }

  /* Let go of a hold. Up to five held groups fly in order; six or more land at once and the newest takes the stage */
  function release() {
    if (S.queue.length >= CATCH_UP) {
      const newest = S.queue.pop();
      if (S.current) landGroup(S.current);
      S.queue.forEach((g) => landGroup(g));
      S.prev = S.queue.length ? S.queue[S.queue.length - 1] : S.current;
      S.queue = [];
      S.current = newest; newest.shownAt = performance.now();
      showHero(newest, false);
    }
    pump();
  }
  /* Pause, zoom and compare each hold the stage, all through S.paused; the stage moves again when the last lets go */
  function hold(kind, on) {
    const was = S.paused;
    S.holds[kind] = on;
    S.paused = S.holds.pause || S.holds.zoom || S.holds.compare;
    if (was && !S.paused) release();
    paintStrip(); paint();
  }

  let heroImg = null, heroShot = null;
  /* the hero is swapped under the zoom and compare layers, never through them */
  function setHeroNode(node) {
    const f = $("frame");
    f.querySelectorAll(".hero, .hero-missing").forEach((n) => n.remove());
    if (node) f.prepend(node);
  }
  function clearHero() { setHeroNode(null); heroImg = null; heroShot = null; paintStrip(); }
  function fitHero() {
    if (!heroImg || !heroImg.naturalWidth) return;
    const f = $("frame"), cs = getComputedStyle(f);
    const w = f.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight), h = f.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const k = Math.min(w / heroImg.naturalWidth, h / heroImg.naturalHeight);
    heroImg.style.width = Math.round(heroImg.naturalWidth * k) + "px";
    heroImg.style.height = Math.round(heroImg.naturalHeight * k) + "px";
  }
  /* reveal: a new shot arrives with a short fade. keep: the same group, a better frame, swapped without ceremony */
  function showHero(g, reveal, keep) {
    const s = lead(g);
    if (keep && heroShot === s) return;
    heroShot = s;
    const img = new Image();
    img.className = "hero"; img.alt = s.file; img.decoding = "async";
    img.onload = () => {
      if (heroShot !== s) return;
      setHeroNode(img); heroImg = img; fitHero();
      if (reveal && !reduced() && !document.hidden) {
        img.animate([{ opacity: 0, transform: "scale(0.985)" }, { opacity: 1, transform: "none" }], { duration: 220, easing: "cubic-bezier(0.2, 0, 0, 1)" });
      }
    };
    img.onerror = () => {
      if (heroShot !== s) return;
      const m = document.createElement("div");
      m.className = "hero-missing"; m.textContent = "No preview yet for " + (s.file || "this shot");
      setHeroNode(m); heroImg = null;
    };
    img.src = s.thumb;
  }

  function verdictChip(g) {
    const v = groupVerdict(g), box = $("verdict"), was = box.dataset.v || "";
    const id = g.key + ":" + (v || "");
    if (box.dataset.id === id) return;
    box.dataset.id = id; box.dataset.v = v || "";
    box.textContent = "";
    if (!v) { const p = document.createElement("span"); p.className = "checking"; p.textContent = "Checking focus"; box.append(p); return; }
    const chip = document.createElement("div");
    chip.className = "chip " + v;
    if (v === "sharp") chip.textContent = "Sharp";
    else {
      const r = g.frames.map((s) => s.reason).filter(Boolean).pop() || "";
      const b = document.createElement("b");
      b.textContent = r ? "Soft " + r : "Soft";
      const again = document.createElement("span"); again.textContent = "Shoot it again";
      chip.append(b, again);
    }
    box.append(chip);
    if (!reduced()) chip.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }], { duration: 180, delay: was ? 0 : 160, fill: "backwards", easing: "cubic-bezier(0.2, 0, 0, 1)" });
  }
  /* Everything to say about the shot on stage, beside it, in plain words. Soft is the chip; these are the rest */
  function alertsOf(g) {
    const s = lead(g), seen = new Set(), out = [];
    s.alerts.forEach((a) => {
      const text = typeof a === "string" ? a : a && a.text;
      if (!text || (a && a.kind === "soft") || seen.has(text)) return;
      seen.add(text); out.push({ kind: (a && a.kind) || "light", text: text });
    });
    if (s.copy === "failed") out.push({ kind: "copy", text: "Could not copy this one to the drive" });
    return out;
  }
  function paintAlerts(g) {
    const box = $("alerts"), list = alertsOf(g), id = g.key + "|" + list.map((a) => a.text).join("|");
    if (box.dataset.id === id) return;
    box.dataset.id = id;
    box.replaceChildren(...list.map((a) => {
      const li = document.createElement("li");
      li.className = "alert" + (a.kind === "copy" ? " copy" : "");
      li.textContent = a.text;
      return li;
    }));
  }
  function destOf(g) {
    const v = groupVerdict(g);
    if (!v) return "";
    if (v === "soft") return "Goes to Soft";
    const id = pileIdOf(g.frames.find((f) => f.verdict === "sharp")), pile = S.piles.get(id);
    return pile ? "Goes to " + (isModelPile(id) ? "Models, " : "Products, ") + pile.name : "";
  }
  function paintStrip() {
    const g = S.current, note = $("group-note"), box = $("verdict");
    $("side").hidden = !g || S.view !== "picture";
    if (!g) { box.textContent = ""; box.dataset.id = ""; box.dataset.v = ""; note.textContent = ""; $("caption").textContent = ""; return; }
    verdictChip(g);
    paintAlerts(g);
    const s = lead(g);
    note.textContent = "";
    if (g.frames[0].burst) {
      const n = Math.max(g.frames.length, Number(g.frames[0].burst.n) || 0);
      const at = g.frames.indexOf(sharpest(g));
      const bars = document.createElement("span"); bars.className = "frames"; bars.setAttribute("aria-hidden", "true");
      for (let i = 0; i < n; i++) { const b = document.createElement("i"); if (i === at) b.className = "best"; bars.append(b); }
      const t = document.createElement("span");
      t.textContent = "Burst of " + n + (at >= 0 ? ", frame " + (at + 1) + " is sharpest" : "");
      note.append(bars, t);
    } else if (g.frames[0].bracket) {
      const list = g.frames.map((f) => f.ev).filter((v) => v !== undefined);
      note.textContent = list.length ? "Bracket " + list.map(evText).join(", ") + " EV" : "Bracket of " + g.frames.length;
    }
    $("caption").textContent = s.file;
    $("caption-dest").textContent = destOf(g);
    const hidden = g.frames.every((f) => f.hidden);
    const hide = $("hide");
    hide.textContent = hidden ? "Show to client" : "Hide from client";
    hide.setAttribute("aria-pressed", String(hidden));
    hide.classList.toggle("on", hidden);
    const state = $("side-state");
    state.textContent = cmp.on ? "Showing the previous shot" : Z.loading ? "Loading the full size file" : hidden ? "Hidden from the client" : "";
    state.hidden = !state.textContent;
    const zoom = $("zoom");
    zoom.textContent = Z.open ? "Back to fit" : "Zoom to 100%";
    zoom.disabled = !Z.open && !heroImg;
    $("compare").disabled = !S.prev || Z.open;
  }
  function evText(v) { const n = Math.round(Number(v) * 10) / 10; return n > 0 ? "+" + n : n < 0 ? "−" + Math.abs(n) : "0"; }

  /* ---------- the flight: FLIP, transform and opacity only. Horizontal and vertical run on different curves, so the
     shot travels on a slight arc instead of a ruled line. ---------- */
  function flyOut(g, instant) {
    const s = lead(g), v = groupVerdict(g);
    const pile = S.piles.get(v === "sharp" ? pileIdOf(g.frames.find((f) => f.verdict === "sharp")) : SOFT);
    const img = heroImg, target = pile && flightTarget(pile);
    if (instant || reduced() || document.hidden || !img || !target || !v || heroShot !== s || S.view !== "picture") { landGroup(g); return; }
    if (target === pile.stack) { pile.row.hidden = false; pile.incoming = true; pile.row.scrollIntoView({ block: "nearest" }); }
    if (pile.enter) pile.enter.finish();
    const a = img.getBoundingClientRect(), b = target.getBoundingClientRect();
    if (!b.width || !b.height) { landGroup(g); return; }
    const k = Math.min(b.width / a.width, b.height / a.height);
    const dx = b.left + (b.width - a.width * k) / 2 - a.left, dy = b.top + (b.height - a.height * k) / 2 - a.top;

    const x = document.createElement("div"), y = document.createElement("div"), pic = new Image();
    x.className = "flyer"; y.className = "flyer-y";
    x.style.cssText = "width:" + a.width + "px;height:" + a.height + "px;transform:translate(" + a.left + "px," + a.top + "px)";
    y.style.cssText = "width:100%;height:100%";
    pic.src = img.src; pic.alt = "";
    y.append(pic); x.append(y); document.body.append(x);
    img.style.visibility = "hidden";                     // the flyer is the picture now; nothing is left behind it
    const opts = { duration: FLIGHT_MS, fill: "forwards" };
    const ax = x.animate([{ transform: "translate(" + a.left + "px," + a.top + "px)" }, { transform: "translate(" + (a.left + dx) + "px," + a.top + "px)" }], { ...opts, easing: "cubic-bezier(0.3, 0, 0.1, 1)" });
    const ay = y.animate([
      { transform: "none", opacity: 1 },
      { transform: "translateY(" + dy + "px) scale(" + k + ")", opacity: 1, offset: 0.9 },
      { transform: "translateY(" + dy + "px) scale(" + k + ")", opacity: 0 },
    ], { ...opts, easing: "cubic-bezier(0.5, 0, 0.2, 1)" });
    const log = { shot: s.id, pile: pile.id, target: rectOf(b), end: null };
    flights.push(log);
    let done = false;
    const arrive = () => {
      if (done) return;
      done = true;
      log.end = rectOf(pic.getBoundingClientRect());
      ax.cancel(); ay.cancel(); x.remove();
      landGroup(g, true);
    };
    Promise.all([ax.finished, ay.finished]).then(arrive, arrive);
    setTimeout(arrive, FLIGHT_MS + 150);                 // a tab in the background may never finish the animation
  }
  /* The pile when it is on screen, its tab when another tab is open, nothing when the panel is put away */
  function flightTarget(p) {
    if (!S.panel) return null;
    const tab = pileTab(p.id);
    if (!tab || tab === S.tab) return p.stack;
    const t = $("tab-" + tab);
    return t.hidden ? null : t.querySelector("b");
  }
  const rectOf = (r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });

  function landGroup(g, animate) {
    g.flown = true;
    g.frames.forEach((s) => { if (s.verdict) landShot(s, animate); });
  }
  function landShot(s, animate) {
    if (s.landed) return;
    s.landed = true;
    const p = ensurePile(pileIdOf(s), null, true);
    p.count++;
    if (s.small) { p.thumbs.push(s.small); if (p.thumbs.length > 3) p.thumbs.shift(); }
    p.incoming = false;
    paintPile(p, animate && !reduced());
    paintTabs();
  }

  /* ---------- the piles ---------- */
  function ensurePile(id, name, animate) {
    let p = S.piles.get(id);
    if (p) { if (name && name !== p.name) renamePile(id, name); return p; }
    const label = name || (id === SOFT ? "Soft" : id === NO_PRODUCT ? (S.find.products ? "No product" : UNSORTED) : isModelPile(id) ? id.slice(MODEL.length) : String(id));
    if (CLIENT) { p = { id: id, name: label, count: 0, thumbs: [] }; S.piles.set(id, p); return p; }
    const row = document.createElement("li");
    row.className = "pile";
    row.innerHTML = '<div class="stack"></div><span class="pile-label"></span><div class="pile-count"><span>0</span></div>';
    p = { id: id, name: label, count: 0, shown: 0, thumbs: [], row: row, el: row, stack: row.querySelector(".stack"), countEl: row.querySelector(".pile-count"), labelEl: row.querySelector(".pile-label"), enter: null };
    S.piles.set(id, p);
    paintLabel(p);
    if (id === SOFT) { const wrap = document.createElement("ol"); wrap.append(row); $("soft-slot").append(wrap); p.el = wrap; }
    else {
      $(isModelPile(id) ? "piles-models" : "piles").append(row);
      if (animate && !reduced() && !row.hidden && pileTab(id) === S.tab) {
        row.scrollIntoView({ block: "nearest" });
        p.enter = row.animate([{ opacity: 0, transform: "translateX(16px)" }, { opacity: 1, transform: "none" }], { duration: 260, easing: "cubic-bezier(0.2, 0, 0, 1)" });
        p.enter.finished.then(() => { p.enter = null; }, () => { p.enter = null; });
      }
    }
    return p;
  }
  function paintLabel(p) {
    const editable = p.id !== SOFT && p.id !== NO_PRODUCT && !isModelPile(p.id) && p.name !== UNSORTED;
    const n = document.createElement(editable ? "button" : "span");
    n.className = "pile-name"; n.textContent = p.name;
    if (editable) { n.type = "button"; n.title = "Rename this product"; n.addEventListener("click", () => editPile(p)); }
    p.labelEl.replaceChildren(n);
  }
  function editPile(p) {
    const box = document.createElement("input");
    box.type = "text"; box.className = "pile-edit"; box.value = p.name; box.setAttribute("aria-label", "Product name"); box.spellcheck = false;
    p.labelEl.replaceChildren(box);
    box.focus(); box.select();
    let closed = false;
    const close = async (save) => {
      if (closed) return;
      closed = true;
      const name = box.value.trim();
      paintLabel(p);
      if (!save || !name || name === p.name) return;
      const before = p.name;
      renamePile(p.id, name);
      try { await api.post("rename-product", { product: p.id, name: name }); }
      catch (e) { renamePile(p.id, before); say("Could not rename it. " + e.message, true); }
    };
    box.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") close(true);
      if (e.key === "Escape") close(false);
    });
    box.addEventListener("blur", () => close(true));
  }
  function renamePile(id, name) {
    const p = S.piles.get(id);
    if (!p || !name || p.name === name) return;
    p.name = name;
    if (CLIENT) return;
    const open = p.labelEl.querySelector("input");
    if (!open) paintLabel(p);
    paintStrip(); feedScene();
  }
  const pileTab = (id) => (id === SOFT ? null : isModelPile(id) ? "models" : "products");
  /* A pile with nothing in it only shows while it is the one being shot, so a product opened for a shot that went to a
     model does not sit at zero. With Find products off, Unsorted always shows: it is where shots land. */
  function paintTabs() {
    if (CLIENT) return;
    const sum = { products: 0, models: 0 }, rows = { products: 0, models: 0 };
    const named = [...S.piles.values()].some((p) => p.id !== NO_PRODUCT && p.name === UNSORTED);   // the server opens it on the first shot
    S.piles.forEach((p) => {
      const tab = pileTab(p.id);
      if (!tab) return;
      const active = isModelPile(p.id) ? p.name === S.active.model : p.id === S.active.product || p.name === S.active.product;
      p.row.classList.toggle("active", active);
      const landing = p.id === NO_PRODUCT && !S.find.products && !named;
      p.row.hidden = !p.count && !active && !p.incoming && !landing;
      sum[tab] += p.count;
      if (!p.row.hidden) rows[tab]++;
    });
    $("tab-products-n").textContent = sum.products; $("tab-models-n").textContent = sum.models;
    $("piles").classList.toggle("none", !rows.products); $("piles-models").classList.toggle("none", !rows.models);
    $("products-empty").hidden = rows.products > 0; $("models-empty").hidden = rows.models > 0;
    const models = S.find.faces || !!S.active.model || sum.models > 0;   // with Find people off, a typed name brings it back
    $("tab-models").hidden = !models;
    if (!models && S.tab === "models") setTab("products");
  }
  function setTab(tab, focus) {
    S.tab = tab;
    remember(TAB_KEY, tab);
    ["products", "models"].forEach((t) => {
      const on = t === tab, b = $("tab-" + t);
      b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
      $("panel-" + t).hidden = !on;
      if (on && focus) b.focus();
    });
  }
  function setPanel(on, animate) {
    S.panel = on;
    remember(PANEL_KEY, on ? "shown" : "hidden");
    document.body.classList.toggle("no-panel", !on);
    $("rail").hidden = !on;
    const b = $("panel-toggle");
    b.textContent = on ? "Hide panel" : "Show panel";
    b.setAttribute("aria-expanded", String(on));
    if (on && animate && !reduced()) $("rail").animate([{ opacity: 0, transform: "translateX(12px)" }, { opacity: 1, transform: "none" }], { duration: 180, easing: "cubic-bezier(0.2, 0, 0, 1)" });
  }
  function paintPile(p, animate) {
    if (CLIENT) return;
    const want = p.thumbs.map((u) => u).join("\n");
    if (p.stack.dataset.t !== want) {
      p.stack.dataset.t = want;
      p.stack.replaceChildren(...p.thumbs.map((u) => { const i = new Image(); i.src = u; i.alt = ""; return i; }));
      p.stack.classList.toggle("has", p.thumbs.length > 0);
    }
    if (p.shown === p.count) return;
    p.shown = p.count;
    const old = p.countEl.firstElementChild, next = document.createElement("span");
    next.textContent = p.count;
    if (!animate) { p.countEl.replaceChildren(next); return; }
    p.countEl.querySelectorAll(".leaving").forEach((e) => e.remove());
    old.classList.add("leaving");
    p.countEl.prepend(next);
    const o = { duration: 160, easing: "cubic-bezier(0.2, 0, 0, 1)" };
    next.animate([{ transform: "translateY(45%)", opacity: 0 }, { transform: "none", opacity: 1 }], o);
    old.animate([{ transform: "none", opacity: 1 }, { transform: "translateY(-45%)", opacity: 0 }], { ...o, fill: "forwards" }).finished.then(() => old.remove(), () => old.remove());
    p.stack.animate([{ transform: "scale(1)" }, { transform: "scale(1.05)" }, { transform: "scale(1)" }], { duration: 200, easing: "ease-out" });
  }

  /* ---------- the header, the controls ---------- */
  function paint() {
    if (CLIENT) return;
    const c = S.counts;
    $("n-shots").textContent = c.shots; $("w-shots").textContent = c.shots === 1 ? "shot" : "shots";
    $("n-sharp").textContent = c.sharp; $("n-soft").textContent = c.soft; $("n-flash").textContent = c.flash;

    let waiting = S.behind;
    S.shots.forEach((s) => { if (!s.verdict) waiting++; });
    const q = $("queue"), line = waiting ? shotWord(waiting) + " being checked" : "";
    if (q.textContent !== line) q.textContent = line;
    q.hidden = !line;

    const held = S.queue.reduce((n, g) => n + g.frames.length, 0), heldBox = $("held");
    heldBox.hidden = !(S.holds.zoom || S.holds.pause);
    if (!heldBox.hidden) {
      const b = document.createElement("b"), hint = document.createElement("span");
      b.style.fontWeight = "inherit"; b.textContent = held + " held";
      hint.textContent = S.holds.zoom ? "Click the picture to carry on. The client window keeps updating." : "Space carries on. The client window keeps updating.";
      heldBox.replaceChildren(b, hint);
    }

    const dot = $("state-dot"), text = $("state-text");
    dot.className = "dot" + (!S.online ? " lost" : S.running ? " on" : "");
    const saving = S.phase === "stopping" || S.phase === "saving";
    text.textContent = !S.online ? (S.connected ? "Lost the Mac, trying again" : "Connecting to the Mac") : saving ? "Saving the shoot" : S.phase === "starting" ? "Starting" : S.running ? "Watching the folder" : "Stopped";
    $("title").textContent = S.shoot || (S.folder ? folderName() : "Live shoot");

    $("stop").hidden = !S.running; $("stop").disabled = !S.online;
    $("new-shoot").hidden = S.running || saving || !S.online;
    const x = S.exporting, exp = $("export");
    exp.disabled = !S.counts.sharp || !S.online || !!x;
    exp.textContent = x ? "Exporting " + (x.done || 0) + " of " + (x.total || 0) : "Export for store";
    syncScan("product", S.active.product); syncScan("model", S.active.model);
    paintTabs(); paintDrive(); paintSetup(saving);

    const empty = !S.current && S.view === "picture";
    $("empty").hidden = !empty;
    if (empty) {
      $("empty-lead").textContent = !S.online ? "Connecting to the Mac" : S.running ? "Waiting for the first shot" : "No shots in this shoot";
      $("empty-sub").textContent = S.online && S.running ? "Each new file in the folder appears here and gets checked for focus." : "";
    }
  }
  /* The drive line, and the alarm when the drive stops taking files. Both only read what the server says */
  function paintDrive() {
    const d = S.drive, line = $("drive-line"), alarm = $("drive-alarm");
    if (!d) { line.hidden = true; alarm.hidden = true; return; }
    const name = d.name || "The drive", n = Number(d.pending) || 0, failed = Number(d.failed) || 0;
    const gone = d.state === "paused_gone", full = d.state === "paused_full";
    let text = "This Mac only";
    if (d.state === "ok") text = "Saving to " + name + (n ? ", " + n + " waiting" : "") + (failed ? ", " + failed + " failed" : "");
    if (gone) text = name + " is disconnected, " + n + " waiting";
    if (full) text = name + " is full, " + n + " waiting";
    line.textContent = text; line.hidden = false; line.title = d.path || "";
    line.classList.toggle("bad", gone || full);
    alarm.hidden = !gone && !full;
    if (gone) alarm.textContent = name + " is disconnected. Shooting continues on this Mac" + (n ? ", and " + shotWord(n) + " will copy when it is back." : ".");
    if (full) alarm.textContent = name + " is full. Shooting continues on this Mac" + (n ? ", with " + shotWord(n) + " waiting to copy. Free up space on it to carry on." : ". Free up space on it to carry on.");
    if (d.warning && d.warning !== S.driveWarned) { S.driveWarned = d.warning; say(d.warning); }
  }
  function syncScan(id, value) {
    const box = $(id);
    if (document.activeElement !== box && !box.classList.contains("dirty")) box.value = value;
  }
  function setOnline(on) { S.online = on; if (on) S.connected = true; paint(); }

  let noteTimer = 0;
  function say(text, bad) {
    const n = $("note");
    n.textContent = text; n.hidden = false; n.classList.toggle("bad", !!bad);
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { n.hidden = true; }, bad ? 9000 : 6000);
  }

  /* ---------- the start screen: where the camera saves, where the sorted photos go, what the shoot is called ---------- */
  const setup = { volumes: [], volumesKey: "", drive: null, custom: null, touched: false, flash: "unknown", timer: 0, shown: false };
  const MAC = "mac";
  const freeText = (n) => {
    if (n === null || n === undefined) return "";
    const v = Number(n);
    return v >= 1e12 ? (v / 1e12).toFixed(1) + " TB free" : Math.round(v / 1e9) + " GB free";
  };
  function setupError(text) { const e = $("setup-error"); e.textContent = text || ""; e.hidden = !text; }
  function driveRow(value, name, sub, free, onPick) {
    const b = document.createElement("button"), mark = document.createElement("i"), t = document.createElement("span"), l = document.createElement("span"), f = document.createElement("span");
    b.type = "button"; b.className = "drive"; b.setAttribute("role", "radio"); b.dataset.value = value;
    const on = setup.drive === value;
    b.setAttribute("aria-checked", String(on));
    l.className = "drive-name"; l.textContent = name; t.append(l);
    if (sub) { const s = document.createElement("span"); s.className = "drive-sub"; s.textContent = sub; t.append(s); }
    f.className = "drive-free"; f.textContent = free || "";
    b.append(mark, t, f);
    b.addEventListener("click", onPick);
    return b;
  }
  function renderDrives() {
    const box = $("drives"), focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.value : null;
    const rows = setup.volumes.map((v) => driveRow(v.path, v.name, fsText(v.fs), freeText(v.free), () => chooseDrive(v.path)));
    if (!rows.length) { const p = document.createElement("p"); p.className = "drive-none"; p.textContent = "No drive connected. Plug one in and it shows up here."; rows.push(p); }
    if (setup.custom) rows.push(driveRow(setup.custom, setup.custom.replace(/\/+$/, "").split("/").pop() || setup.custom, setup.custom, "", () => chooseDrive(setup.custom)));
    rows.push(driveRow("choose", "Choose folder", "", "", pickDrive));
    rows.push(driveRow(MAC, "This Mac only", "Plug in a drive to keep a second copy off this Mac", "", () => chooseDrive(MAC)));
    box.replaceChildren(...rows);
    const radios = [...box.querySelectorAll(".drive")];
    if (!radios.some((r) => r.getAttribute("aria-checked") === "true")) radios[0].tabIndex = 0;
    radios.forEach((r) => { r.tabIndex = r.getAttribute("aria-checked") === "true" ? 0 : -1; });
    if (focused) { const again = radios.find((r) => r.dataset.value === focused); if (again) again.focus(); }
    const v = setup.volumes.find((x) => x.path === setup.drive);
    const warn = $("drive-warn");
    warn.hidden = !(v && /exfat/i.test(v.fs || ""));
    warn.textContent = "exFAT can lose photos if unplugged while copying. Eject it before you pull the cable.";
  }
  const fsText = (fs) => (/apfs/i.test(fs || "") ? "APFS" : /exfat/i.test(fs || "") ? "exFAT" : /hfs/i.test(fs || "") ? "Mac OS Extended" : /ntfs/i.test(fs || "") ? "NTFS" : /msdos/i.test(fs || "") ? "FAT32" : fs || "");
  function chooseDrive(value) { setup.touched = true; setup.drive = value; renderDrives(); }
  async function pickDrive() {
    setupError("");
    try {
      const r = await api.pick("drive");
      if (r.path) { setup.custom = r.path; chooseDrive(r.path); }
    } catch (e) { setupError("Could not open the folder picker. " + e.message); }
  }
  async function loadVolumes() {
    let list;
    try { const r = await api.volumes(); list = Array.isArray(r) ? r : r.volumes || []; }
    catch (e) { list = setup.volumes; }
    const key = JSON.stringify(list.map((v) => [v.path, v.fs, Math.round(v.free / 1e9)]));
    const gone = setup.drive && setup.drive !== MAC && setup.drive !== setup.custom && !list.some((v) => v.path === setup.drive);
    if (key === setup.volumesKey && !gone && setup.drive) return;
    setup.volumes = list; setup.volumesKey = key;
    if (gone) { setup.drive = null; setup.touched = false; }
    if (!setup.touched) setup.drive = list.length ? list[0].path : MAC;
    renderDrives();
  }
  function setFlash(v) {
    setup.flash = v;
    [["flash-unknown", "unknown"], ["flash-strobe", "strobe"], ["flash-none", "none"]].forEach(([id, k]) => $(id).setAttribute("aria-pressed", String(k === v)));
  }
  function defaultShootName() {
    return "Shoot " + new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  /* The start screen shows until a session is running, unless there is a finished shoot on screen to look at */
  function paintSetup(saving) {
    const show = S.online && !S.running && !saving && (S.setup || !S.shots.size);
    $("setup").hidden = !show;
    if (show && !setup.shown) {
      setup.shown = true;
      if (!$("folder").value) $("folder").value = S.folder || recall(FOLDER_KEY) || "";
      if (!$("shoot").value) $("shoot").value = defaultShootName();
      setupError("");
      loadVolumes();
      setup.timer = setInterval(loadVolumes, 5000);   // a drive plugged in now just appears
    }
    if (!show && setup.shown) { setup.shown = false; clearInterval(setup.timer); }
    $("start").disabled = !$("folder").value.trim() || !$("shoot").value.trim();
    $("setup-back").hidden = !S.shots.size;
  }
  async function pickFolder() {
    setupError("");
    try {
      const r = await api.pick("watch");
      if (r.path) { $("folder").value = r.path; paint(); }
    } catch (e) {
      $("folder").focus();
      setupError("Could not open the folder picker. Type the path instead. " + e.message);
    }
  }
  async function start() {
    const folder = $("folder").value.trim(), shoot = $("shoot").value.trim();
    if (!folder || !shoot || S.running) return;
    const want = { product: $("product").value.trim(), model: $("model").value.trim() };
    const find = { faces: $("find-people").checked, products: $("find-products").checked };
    const drive = setup.drive && setup.drive !== MAC ? setup.drive : null;
    $("start").disabled = true;
    setupError("");
    try {
      await api.post("start", { folder: folder, strobe: setup.flash === "strobe", no_flash: setup.flash === "none", drive: drive, shoot: shoot, faces: find.faces, products: find.products });
      remember(FOLDER_KEY, folder); remember(SHOOT_KEY, shoot);
      remember(FIND_KEY, JSON.stringify(find));
      S.find = find; S.setup = false; S.shoot = shoot;
      resetSession();
      S.running = true; S.phase = "starting"; S.folder = folder;
      applySnapshot(await api.state());
      if (want.product || want.model) { await api.post("active", want); S.active = want; }
    } catch (e) { setupError("Could not start. " + e.message); }
    paint();
  }
  async function stop() {
    $("stop").disabled = true;
    try { readStatus(await api.post("stop")); }
    catch (e) { say("Could not stop. " + e.message, true); }
    paint();
  }
  function resetSession() {
    if (Z.open) zoomOut();
    S.shots = new Map(); S.groups = new Map(); S.queue = []; S.current = null; S.prev = null; S.seq = 0; S.behind = 0; S.drive = null;
    S.counts = { shots: 0, sharp: 0, soft: 0, flash: 0 };
    S.piles.forEach((p) => p.el.remove()); S.piles = new Map();
    clearHero(); ensurePile(SOFT, "Soft", false);
    if (!S.find.products) ensurePile(NO_PRODUCT, null, false);
    feedScene(true);
  }
  async function commitActive() {
    const product = $("product").value.trim(), model = $("model").value.trim();
    const before = S.active;
    if (product === before.product && model === before.model) { clean(); return; }
    S.active = { product: product, model: model };
    clean(); paint();
    if (!S.running) { say("Saved for when you press Start"); return; }
    try {
      await api.post("active", S.active);
      say("Next shots go to " + (product || "no product") + (model ? ", " + model : ""));
    } catch (e) { S.active = before; paint(); say("Could not set that. " + e.message, true); }
  }
  function clean() { $("product").classList.remove("dirty"); $("model").classList.remove("dirty"); }
  /* Sharp shots only, one folder per product, into the app's export folder. Progress comes back on the status events */
  async function exportStore() {
    $("export").disabled = true;
    try {
      const d = await api.exportDest();
      const base = String(d.path || "").replace(/\/+$/, "");
      if (!base) throw new Error("Set an export folder in sorted first.");
      const r = await api.post("export-store", { dest: base + "/" + folderName() + " for store" });
      if (!r.total) say("No sharp shots to export yet.");
      else if (!S.exporting) S.exporting = { running: true, done: 0, total: r.total, path: r.path };
    } catch (e) { say("Could not export. " + e.message, true); }
    paint();
  }
  function setPaused(on) {
    $("pause").textContent = on ? "Carry on" : "Pause";
    $("pause").classList.toggle("on", on);
    hold("pause", on);
  }

  /* ---------- 100%: one file pixel to one screen pixel, from the original. It holds the stage while it is open ---------- */
  const Z = { open: false, loading: false, box: null, pic: null, s: null, x: 0, y: 0, w: 0, h: 0, drag: null };
  function placeZoom() {
    const W = Z.box.clientWidth, H = Z.box.clientHeight;
    Z.x = Z.w <= W ? (W - Z.w) / 2 : Math.min(0, Math.max(W - Z.w, Z.x));
    Z.y = Z.h <= H ? (H - Z.h) / 2 : Math.min(0, Math.max(H - Z.h, Z.y));
    Z.pic.style.transform = "translate3d(" + Z.x + "px," + Z.y + "px,0)";
  }
  async function zoomIn(e) {
    if (Z.open || !heroImg || S.view !== "picture" || cmp.on) return;
    const s = heroShot, r = heroImg.getBoundingClientRect(), inside = (v) => Math.min(1, Math.max(0, v));
    const fx = e ? inside((e.clientX - r.left) / r.width) : 0.5, fy = e ? inside((e.clientY - r.top) / r.height) : 0.5;
    const box = document.createElement("div"), pic = new Image();
    box.className = "zoom"; pic.alt = s.file; pic.draggable = false;
    Object.assign(Z, { open: true, loading: true, box: box, pic: null, s: s, drag: null });
    box.addEventListener("pointerdown", zoomDown);
    box.addEventListener("pointermove", zoomMove);
    box.addEventListener("pointerup", zoomUp);
    box.addEventListener("pointercancel", () => { Z.drag = null; box.classList.remove("dragging"); });
    $("frame").append(box);
    hold("zoom", true);
    try {
      pic.src = await api.original(s.id);
      await pic.decode();
    } catch (err) {
      if (Z.box !== box) return;
      zoomOut();
      say("Could not open the full size file for " + s.file + ".", true);
      return;
    }
    if (Z.box !== box) return;                           // closed while the file was loading
    const dpr = window.devicePixelRatio || 1, f = box.getBoundingClientRect();
    Z.w = pic.naturalWidth / dpr; Z.h = pic.naturalHeight / dpr; Z.pic = pic; Z.loading = false;
    pic.style.width = Z.w + "px"; pic.style.height = Z.h + "px";
    Z.x = (e ? e.clientX - f.left : f.width / 2) - fx * Z.w;
    Z.y = (e ? e.clientY - f.top : f.height / 2) - fy * Z.h;
    box.style.background = "var(--g50)";
    box.append(pic);
    placeZoom();
    if (e && !reduced()) {                               // out of the picture on screen, around the point clicked
      pic.animate([{ transform: "translate3d(" + (r.left - f.left) + "px," + (r.top - f.top) + "px,0) scale(" + r.width / Z.w + ")" }, { transform: pic.style.transform }], { duration: 240, easing: "cubic-bezier(0.2, 0, 0, 1)" });
    }
    paintStrip();
  }
  function zoomDown(e) {
    Z.drag = { x: e.clientX, y: e.clientY, ox: Z.x, oy: Z.y, moved: false };
    Z.box.setPointerCapture(e.pointerId);
  }
  function zoomMove(e) {
    const d = Z.drag;
    if (!d || !Z.pic) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    d.moved = true;
    Z.box.classList.add("dragging");
    Z.x = d.ox + dx; Z.y = d.oy + dy;
    placeZoom();
  }
  function zoomUp() {
    const d = Z.drag;
    Z.drag = null;
    if (Z.box) Z.box.classList.remove("dragging");
    if (d && !d.moved) zoomOut();                        // a click, not a drag
  }
  function zoomOut() {
    if (!Z.open) return;
    Z.box.remove();
    Object.assign(Z, { open: false, loading: false, box: null, pic: null, s: null, drag: null });
    hold("zoom", false);
  }

  /* ---------- compare: the last shot, for as long as the key or the button is down ---------- */
  const cmp = { on: false, el: null };
  function compareShow(on) {
    if (on === cmp.on) return;
    if (on) {
      if (!S.prev || Z.open || S.view !== "picture") return;
      const f = $("frame"), cs = getComputedStyle(f), img = new Image();
      const l = parseFloat(cs.paddingLeft), t = parseFloat(cs.paddingTop);
      img.className = "compare-pic"; img.alt = ""; img.src = lead(S.prev).thumb;
      img.style.cssText = "left:" + l + "px;top:" + t + "px;width:" + (f.clientWidth - l - parseFloat(cs.paddingRight)) + "px;height:" + (f.clientHeight - t - parseFloat(cs.paddingBottom)) + "px";
      f.append(img);
      cmp.on = true; cmp.el = img;
      hold("compare", true);
    } else {
      cmp.el.remove();
      cmp.on = false; cmp.el = null;
      hold("compare", false);
    }
  }

  /* ---------- hide from client: every frame of what is on stage, shown at once here and sent to the Mac ---------- */
  async function toggleHide() {
    const g = S.current;
    if (!g) return;
    const on = !g.frames.every((f) => f.hidden), set = (v) => g.frames.forEach((f) => { f.hidden = v; f.raw.hidden = v; });
    set(on); paintStrip();
    try { await Promise.all(g.frames.map((f) => api.post("hide", { id: f.id, hidden: on }))); }
    catch (e) { set(!on); paintStrip(); say("Could not change that. " + e.message, true); }
  }

  /* ---------- the client window. getScreenDetails can put it on the second screen; only a key or click there can make it
     fullscreen. With no such API, or one screen, it opens here and gets dragged over. ---------- */
  let clientWin = null, clientOn = null, screens = null;
  const clientUrl = () => "/ui/live.html?client=1" + (DEMO ? "&demo=1" : "");
  async function askScreens() {
    if (screens || !("getScreenDetails" in window)) return screens;
    try {
      screens = await window.getScreenDetails();         // the one permission prompt
      screens.addEventListener("screenschange", onScreens);
    } catch (e) { screens = null; }                      // refused: drag it over by hand
    return screens;
  }
  function onScreens() {
    const open = clientWin && !clientWin.closed;
    const still = !clientOn || screens.screens.some((s) => s.left === clientOn.left && s.top === clientOn.top);
    $("client-back").hidden = !open || still;
  }
  async function openClient() {
    if (clientWin && !clientWin.closed) { clientWin.focus(); say("The client window is already open."); return; }
    const d = await askScreens();
    const other = d ? d.screens.find((s) => s !== d.currentScreen) : null;
    const place = other ? "left=" + other.availLeft + ",top=" + other.availTop + ",width=" + other.availWidth + ",height=" + other.availHeight : "width=1280,height=720";
    const w = window.open(clientUrl(), "sorted-client", "popup," + place);
    if (!w) { say("The browser blocked the window. Press Open client window again.", true); return; }
    clientWin = w;
    clientOn = other ? { left: other.left, top: other.top } : null;
    $("client-back").hidden = true;
    say(other ? "The client window is on " + (other.label || "the second screen") + ". Click it and press F for fullscreen." : "The client window is open. Drag it to the TV, click it and press F for fullscreen.");
  }
  function putClientBack() {
    $("client-back").hidden = true;
    if (!clientWin || clientWin.closed) { openClient(); return; }
    try { if (clientWin.document.fullscreenElement) clientWin.document.exitFullscreen(); } catch (e) { /* not fullscreen after all */ }
    const s = screens && screens.currentScreen;
    if (s) clientWin.moveTo(s.availLeft + 40, s.availTop + 40);
    clientWin.resizeTo(1280, 720);
    clientWin.focus();
    clientOn = null;
    say("The client window is back on this screen.");
  }

  function toggleFull() {
    const f = document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
    if (f && f.catch) f.catch(() => { /* a browser that refuses: the page still works windowed */ });
  }

  /* The camera bay's barcode scanner is a keyboard that types very fast and ends with Enter. With no field focused its
     letters must not fire F or Space, so a single key waits a moment to see whether another is right behind it. */
  const SCAN_GAP_MS = 45;
  let scanBuf = "", scanTimer = 0, scanLast = 0;
  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t.tagName === "INPUT") {
      if (e.key === "Enter" && (t.id === "product" || t.id === "model")) { e.preventDefault(); commitActive(); t.select(); }
      return;
    }
    if (!$("setup").hidden) return;
    if (e.repeat) return;
    if (Z.open && e.key === "Escape") { zoomOut(); return; }
    if (Z.pic && e.key.startsWith("Arrow")) {
      e.preventDefault();
      const step = 120;
      Z.x += e.key === "ArrowLeft" ? step : e.key === "ArrowRight" ? -step : 0;
      Z.y += e.key === "ArrowUp" ? step : e.key === "ArrowDown" ? -step : 0;
      placeZoom();
      return;
    }
    const now = performance.now(), fast = now - scanLast < SCAN_GAP_MS;
    if (e.key === "Enter" && fast && scanBuf.length >= 3 && !CLIENT) {
      e.preventDefault(); clearTimeout(scanTimer);
      $("product").value = scanBuf; scanBuf = ""; commitActive();
      return;
    }
    if (e.key.length !== 1) return;
    if (e.key === " " && t.tagName === "BUTTON") return;
    if (e.key === " ") e.preventDefault();
    scanBuf = fast ? scanBuf + e.key : e.key;
    scanLast = now;
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => {
      const k = scanBuf; scanBuf = "";
      if (k === "f" || k === "F") toggleFull();
      if (CLIENT) return;
      if (k === " ") setPaused(!S.holds.pause);
      else if (k === "z" || k === "Z") { if (Z.open) zoomOut(); else zoomIn(); }
      else if ((k === "c" || k === "C") && compareKey) compareShow(true);
    }, SCAN_GAP_MS + 15);
    if (e.key === "c" || e.key === "C") compareKey = true;
  }
  let compareKey = false;
  function onKeyUp(e) {
    if (e.key === "c" || e.key === "C") { compareKey = false; compareShow(false); }
  }

  /* ---------- the Desktop view: the same scene the Scan tab draws, fed from this session ---------- */
  let fedTo = new Set(), feedQueued = false;
  function feedScene(reset) {
    if (reset) fedTo = new Set();
    if (CLIENT || S.view !== "desktop" || !window.ScanScene || !ScanScene.feed || feedQueued) return;
    feedQueued = true;
    setTimeout(() => {
      feedQueued = false;
      if (S.view !== "desktop") return;
      const rows = [], updates = [];
      const row = (s) => ({ id: s.id, name: s.file, video: false, thumb: s.small, qhash: "live" + s.id, looked: true, error: false, category: null, product: s.verdict ? pileIdOf(s) : null });
      S.shots.forEach((s) => { (fedTo.has(s.id) ? updates : rows).push(row(s)); fedTo.add(s.id); });
      const products = [];
      const sorted = new Map();                          // the shot on stage counts here: its card is already flying
      S.shots.forEach((s) => { if (s.verdict) sorted.set(pileIdOf(s), (sorted.get(pileIdOf(s)) || 0) + 1); });
      S.piles.forEach((p) => { const n = Math.max(p.count, sorted.get(p.id) || 0); if (n || p.id !== SOFT) products.push({ id: p.id, name: p.name, count: n }); });
      ScanScene.feed({ key: "live:" + S.folder, shoot: S.shoot || folderName(), disk: null, total: S.counts.shots, photos: S.counts.shots, videos: 0,
        rows: rows, updates: updates, folders: [], products: products, bare: true,
        pill: [S.running ? "Watching the folder" : "Stopped", shotWord(S.counts.shots)] }, { stage: "live", running: S.running });
    }, 120);
  }
  const folderName = () => (S.folder || "").replace(/\/+$/, "").split("/").pop() || "Live shoot";
  function setView(v) {
    if (v === S.view) return;
    S.view = v;
    $("view-picture").setAttribute("aria-pressed", String(v === "picture"));
    $("view-desktop").setAttribute("aria-pressed", String(v === "desktop"));
    $("scene-view").hidden = v !== "desktop";
    if (Z.open) zoomOut();
    compareShow(false);
    paintStrip();
    feedScene(true);
    paint();
    if (v === "picture") fitHero();
  }

  /* ---------- the client's TV ---------- */
  const client = { up: null, down: null, want: null, busy: false, shown: null };
  const clientWorthy = (s) => !s.hidden;
  function clientLatest() {
    let pick = null;
    S.shots.forEach((s) => { if (clientWorthy(s)) pick = s; });
    return pick;
  }
  function clientShow(s, cut) { client.want = { s: s, cut: !!cut }; clientPump(); }
  /* The newest frame always wins. One still loading or fading when another lands is passed over, so a burst ends on its
     last frame without the TV stuttering through the rest. */
  async function clientPump() {
    if (client.busy || !client.want) return;
    const s = client.want.s, cut = client.want.cut || reduced() || document.hidden;
    client.want = null; client.busy = true;
    const layer = client.down, pic = new Image();
    pic.src = s.thumb;
    let ok = true;
    try { await pic.decode(); } catch (e) { ok = false; }
    if (ok && !client.want && !s.hidden) {
      $("client").classList.toggle("cut", cut);
      layer.src = s.thumb;
      try { await layer.decode(); } catch (e) { /* drawn on load all the same */ }
      void layer.offsetWidth;
      if (!s.hidden) {
        layer.classList.add("up"); client.up.classList.remove("up");
        client.down = client.up; client.up = layer; client.shown = s.id;
        $("client-mark").hidden = true;
        if (!cut) await sleep(CROSSFADE_MS);
      }
    }
    client.busy = false;
    clientPump();
  }
  /* Off the screen now, with no fade: this is what Hide from client is for */
  function clientDrop() {
    $("client").classList.add("cut");
    [client.up, client.down].forEach((l) => l.classList.remove("up"));
    client.shown = null;
  }
  function clientRefresh() {
    const pick = clientLatest();
    if (pick && client.shown !== pick.id) clientShow(pick, true);
    if (!pick) { client.want = null; clientDrop(); $("client-mark").hidden = false; }
  }
  function clientOnShot(s, fresh, wasHidden) {
    if (fresh) { if (clientWorthy(s)) clientShow(s); return; }
    if (s.hidden === wasHidden) return;
    if (s.hidden && client.shown === s.id) clientDrop();
    clientRefresh();
  }
  function clientSnapshot() { clientRefresh(); }

  /* ---------- demo: a scripted shoot and a stand-in for live.py, behind the same calls and with its event shapes ---------- */
  function demoServer() {
    const PIC = { tubes: "/site/img/product.webp", sari: "/site/img/sari.webp", face: "/site/img/face5.webp", bride: "/site/img/bride.webp" };
    const list = [];
    let id = 0, at = 400;
    const push = (e, gap) => { at += gap; list.push({ at: at, e: e }); };
    const names = {};
    function shot(pic, pid, model, verdict, reason, extra) {
      id++;
      const o = extra || {};
      push({ type: "shot", id: id, n: id, file: "DCIM/100CANON/IMG_" + (4100 + id) + ".CR3", thumb: PIC[pic], preview: PIC[pic], kind: model ? "model" : "product",
        category: model ? "model" : "product", alerts: o.alerts || [], copy: "queued", hidden: false,
        product: names[pid], product_id: pid, model: model || null, verdict: verdict, reason: reason || null, flash: o.flash ? "fired" : "unknown",
        burst: o.burst || null, bracket: o.bracket || null, ev: o.ev === undefined ? null : o.ev, sharp: o.sharp === undefined ? null : o.sharp, queue: o.queue || 0 }, o.gap === undefined ? 2800 : o.gap);
    }
    /* the first frame arrives alone: the server only knows it was a burst when the second one lands */
    function burst(pic, pid, model, n, bestAt, softAt) {
      const first = id + 1, ids = [];
      for (let i = 0; i < n; i++) {
        ids.push(first + i);
        const v = i === softAt ? "soft" : "sharp";
        shot(pic, pid, model, v, v === "soft" ? "on the eyes" : "", { sharp: i === bestAt ? 900 : 300 + i, gap: i ? 110 : 2800, queue: Math.max(0, n - i - 1),
          burst: i ? { id: first, n: i + 1, ids: ids.slice() } : null });
      }
    }
    function bracket(pic, pid, model, evs) {
      const first = id + 1, ids = [];
      evs.forEach((ev, i) => {
        ids.push(first + i);
        shot(pic, pid, model, "sharp", "", { ev: ev, gap: i ? 220 : 2800, bracket: { id: first, n: i + 1, ids: ids.slice() } });
      });
    }
    const product = (pid) => { names[pid] = "product " + pid; push({ type: "product_created", id: pid, name: names[pid], auto: true }, 200); };
    const DARK = { kind: "exposure", text: "A stop darker than the last shot" }, MISFIRE = { kind: "flash", text: "The flash did not fire" },
      BLOWN = { kind: "highlights", text: "Highlights are blown on the left" }, COLOUR = { kind: "white_balance", text: "Colour shifted from the last shot" };
    product(1);
    shot("tubes", 1, "", "sharp", "");
    shot("tubes", 1, "", "sharp", "", { flash: true, alerts: [BLOWN] });
    shot("tubes", 1, "", "soft", "on the product", { alerts: [MISFIRE, DARK] });
    bracket("tubes", 1, "", [-2, 0, 2]);
    product(2);
    shot("sari", 2, "Asha", "sharp", "", { flash: true });
    shot("sari", 2, "Asha", "soft", "on the eyes", { flash: true, alerts: [COLOUR] });
    burst("sari", 2, "Asha", 6, 3, 5);
    shot("sari", 2, "Asha", "sharp", "");
    product(3);
    shot("bride", 3, "Meera", "sharp", "", { flash: true });
    shot("face", 3, "Meera", "soft", "on the eyes");
    shot("face", 3, "Meera", "sharp", "", { alerts: [DARK] });
    burst("bride", 3, "Meera", 4, 1);
    shot("bride", 3, "Meera", "sharp", "", { flash: true });

    const STEP = Q.get("step") === "1", speed = Number(Q.get("speed")) || 1;
    const VOLUMES = [{ name: "T7 Shield", path: "/Volumes/T7 Shield", free: 812e9, fs: "apfs" }, { name: "SHOOT_B", path: "/Volumes/SHOOT_B", free: 1.6e12, fs: "exfat" }];
    /* the start screen first, as on a real shoot; a client window, or a test that asked for a place in the script, goes straight in */
    const straight = CLIENT || STEP || Q.has("skip");
    const folder = "/Users/you/Pictures/Tether";
    let pointer = 0, phase = straight ? "live" : "stopped", strobe = true, down = 0, active = { product: null, model: null };
    let shoot = straight ? "Summer lookbook" : "", find = { faces: true, products: true }, wake = null, log = [], timer = 0, seq = 0;
    let drive = straight ? { path: VOLUMES[0].path, name: VOLUMES[0].name, fs: "apfs", state: "ok", pending: 0, copied: 0, failed: 0, free_bytes: 812e9, warning: null } : null;
    const held = [], made = new Set();                   // shots with no copy yet; products opened with Find products off
    const ping = () => { if (wake) { const w = wake; wake = null; w(); } };
    const emit = (e) => { log.push(Object.assign({}, e, { seq: ++seq })); ping(); };
    const driveStatus = () => emit({ type: "status", drive: Object.assign({}, drive) });
    /* the copy runs on its own clock, whatever the page is drawing */
    function copyOne(sid) {
      if (!drive || drive.state === "off") return;
      if (drive.state !== "ok") { held.push(sid); return; }
      setTimeout(() => {
        if (!drive) return;
        if (drive.state !== "ok") { held.push(sid); return; }
        drive.pending = Math.max(0, drive.pending - 1); drive.copied++;
        emit({ type: "shot_update", id: sid, copy: "copied" });
        driveStatus();
      }, 900 / speed);
    }
    /* What the script did not know at writing time: with Find people or Find products off, nothing is guessed */
    function shaped(e) {
      if (e.type === "product_created") return find.products ? e : null;
      if (e.type !== "shot") return e;
      const o = Object.assign({}, e);
      if (!find.products) {                              // as live.py: the typed product, else the one Unsorted pile
        const name = active.product || "Unsorted";
        if (!made.has(name)) { made.add(name); emit({ type: "product_created", id: "p " + name, name: name }); }
        o.product = name; o.product_id = "p " + name;
      }
      if (!find.faces) { o.model = active.model || null; o.category = o.model ? "model" : "product"; }
      else if (active.model) { o.model = active.model; }
      return o;
    }
    const step = () => {
      if (pointer >= list.length) return;
      const e = shaped(list[pointer++].e);
      if (!e) return;
      emit(e);
      if (e.type === "shot" && drive && drive.state !== "off") { drive.pending++; driveStatus(); copyOne(e.id); }
    };
    for (let i = Math.min(list.length, Number(Q.get("skip")) || 0); i > 0; i--) step();
    function tick() {
      clearTimeout(timer);
      if (STEP || phase !== "live" || pointer >= list.length) return;
      const gap = (list[pointer].at - (pointer ? list[pointer - 1].at : 0)) / speed;
      timer = setTimeout(() => { step(); tick(); }, Math.max(10, gap));
    }
    const refuse = () => (down > performance.now() ? Promise.reject(new Error("offline")) : null);
    function snapshot() {
      const shots = new Map(), products = new Map();
      log.forEach((e) => {
        if (e.type === "shot") {
          shots.set(e.id, Object.assign({}, e));
          ["burst", "bracket"].forEach((k) => { if (e[k]) e[k].ids.forEach((i) => { if (shots.has(i)) shots.get(i)[k] = e[k]; }); });
        }
        if (e.type === "shot_update" && shots.has(e.id)) { const u = Object.assign({}, e); delete u.seq; delete u.type; Object.assign(shots.get(e.id), u); }
        if (e.type === "product_created") products.set(e.id, { id: e.id, name: e.name, n: 0 });
        if (e.type === "product_renamed" && products.has(e.id)) products.get(e.id).name = e.name;
      });
      const models = new Map();
      shots.forEach((s) => { if (s.category === "model" && s.model) models.set(s.model, (models.get(s.model) || 0) + 1); });
      return { seq: seq, state: phase, running: phase === "live", folder: folder, shoot: shoot, strobe: strobe, active: active, queue: 0, drive: drive && Object.assign({}, drive), faces: find.faces, products_on: find.products,
        products: [...products.values()], models: [...models].map(([name, n]) => ({ name: name, n: n })), shots: [...shots.values()] };
    }
    tick();
    let bigger = new Map();                              // the site's pictures are small: a stand-in original big enough to pan
    function original(sid) {
      const hit = log.find((e) => e.type === "shot" && e.id === sid);
      if (!hit) return Promise.reject(new Error("gone"));
      if (bigger.has(hit.thumb)) return Promise.resolve(bigger.get(hit.thumb));
      return new Promise((done, fail) => {
        const i = new Image();
        i.onerror = fail;
        i.onload = () => {
          const c = document.createElement("canvas"), k = 5;
          c.width = i.naturalWidth * k; c.height = i.naturalHeight * k;
          c.getContext("2d").drawImage(i, 0, 0, c.width, c.height);
          c.toBlob((b) => { const u = URL.createObjectURL(b); bigger.set(hit.thumb, u); done(u); }, "image/jpeg", 0.85);
        };
        i.src = hit.thumb;
      });
    }
    return {
      state() { return refuse() || Promise.resolve(snapshot()); },
      async events(after) {
        for (;;) {
          const no = refuse(); if (no) return no;
          if (seq > after) return { seq: seq, events: log.filter((e) => e.seq > after), reset: false };
          await new Promise((go) => { wake = go; setTimeout(go, 400); });
        }
      },
      async post(path, body) {
        const no = refuse(); if (no) return no;
        await sleep(120);
        if (path === "stop") { phase = "stopped"; clearTimeout(timer); emit({ type: "status", state: phase, queue: 0 }); return snapshot(); }
        if (path === "start") {
          phase = "live"; strobe = body.strobe; shoot = body.shoot; pointer = 0; log = []; held.length = 0;
          find = { faces: body.faces !== false, products: body.products !== false };
          const v = VOLUMES.find((x) => x.path === body.drive);
          drive = body.drive ? { path: body.drive, name: v ? v.name : body.drive.split("/").pop(), fs: v ? v.fs : "apfs", state: "ok", pending: 0, copied: 0, failed: 0, free_bytes: v ? v.free : 5e11,
            warning: v && v.fs === "exfat" ? "SHOOT_B is exFAT. Eject it before you pull the cable." : null } : { path: null, name: null, fs: null, state: "off", pending: 0, copied: 0, failed: 0, free_bytes: 0, warning: null };
          tick();
          return {};
        }
        if (path === "active") { active = Object.assign({}, active, body); return active; }
        if (path === "rename-product") { emit({ type: "product_renamed", id: body.product, name: body.name }); return {}; }
        if (path === "hide") { emit({ type: "shot_update", id: body.id, hidden: !!body.hidden }); return {}; }
        if (path === "pick") return { path: body.purpose === "drive" ? "/Volumes/Archive/Shoots" : folder };
        if (path === "export-store") {
          const total = snapshot().shots.filter((s) => s.verdict === "sharp").length, where = body.dest;
          [0.3, 0.7, 1].forEach((f, i) => setTimeout(() => emit({ type: "status", export: { running: f < 1, done: Math.round(total * f), failed: 0, total: total, path: where, error: null } }), 250 * (i + 1)));
          return { started: true, total: total, path: where };
        }
        return {};
      },
      pick: (purpose) => api.post("pick", { purpose: purpose }),
      volumes: async () => VOLUMES,
      original: original,
      exportDest: async () => ({ path: "/Users/you/Pictures/sorted exports" }),
      next(n) { for (let i = n || 1; i > 0; i--) step(); return pointer; },
      down(ms) { down = performance.now() + ms; },
      /* the drive going away, filling up and coming back; ok copies what waited */
      drive(state) {
        if (!drive) return;
        drive.state = state;
        driveStatus();
        if (state === "ok") held.splice(0).forEach(copyOne);
      },
      hide(sid, on) { emit({ type: "shot_update", id: sid, hidden: on !== false }); },
      failCopy(sid) { drive.failed++; drive.pending = Math.max(0, drive.pending - 1); emit({ type: "shot_update", id: sid, copy: "failed" }); driveStatus(); },
      length: list.length,
    };
  }

  /* ---------- start ---------- */
  function boot() {
    if (CLIENT) {
      document.body.classList.add("is-client");
      document.title = "Pictures";
      $("client").hidden = false;
      client.up = $("client-a"); client.down = $("client-b");
    } else {
      ensurePile(SOFT, "Soft", false);
      setPanel(S.panel, false); setTab(S.tab);
      $("pick").addEventListener("click", pickFolder);
      $("setup-form").addEventListener("submit", (e) => { e.preventDefault(); start(); });
      ["folder", "shoot"].forEach((id) => $(id).addEventListener("input", paint));
      [["flash-unknown", "unknown"], ["flash-strobe", "strobe"], ["flash-none", "none"]].forEach(([id, v]) => $(id).addEventListener("click", () => setFlash(v)));
      $("drives").addEventListener("keydown", (e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        const list = [...$("drives").querySelectorAll(".drive")], at = list.indexOf(document.activeElement);
        list[(at + (e.key === "ArrowDown" ? 1 : list.length - 1)) % list.length].focus();
      });
      $("new-shoot").addEventListener("click", () => { S.setup = true; paint(); });
      $("setup-back").addEventListener("click", () => { S.setup = false; paint(); });
      $("panel-toggle").addEventListener("click", () => { setPanel(!S.panel, true); });
      ["products", "models"].forEach((t) => $("tab-" + t).addEventListener("click", () => setTab(t)));
      document.querySelector(".tabs").addEventListener("keydown", (e) => {
        if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !$("tab-models").hidden) setTab(S.tab === "products" ? "models" : "products", true);
      });
      $("frame").addEventListener("click", (e) => { if (e.target === heroImg) zoomIn(e); });
      $("zoom").addEventListener("click", () => { if (Z.open) zoomOut(); else zoomIn(); });
      $("hide").addEventListener("click", toggleHide);
      const cb = $("compare");
      cb.addEventListener("pointerdown", (e) => { cb.setPointerCapture(e.pointerId); compareShow(true); });
      ["pointerup", "pointercancel"].forEach((n) => cb.addEventListener(n, () => compareShow(false)));
      cb.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.stopPropagation(); compareShow(true); } });
      cb.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") compareShow(false); });
      $("client-open").addEventListener("click", openClient);
      $("client-back-btn").addEventListener("click", putClientBack);
      $("stop").addEventListener("click", stop);
      $("export").addEventListener("click", exportStore);
      $("pause").addEventListener("click", () => setPaused(!S.holds.pause));
      $("full").addEventListener("click", toggleFull);
      $("view-picture").addEventListener("click", () => setView("picture"));
      $("view-desktop").addEventListener("click", () => setView("desktop"));
      ["product", "model"].forEach((id) => {
        $(id).addEventListener("input", () => $(id).classList.add("dirty"));
        $(id).addEventListener("blur", () => { if ($(id).classList.contains("dirty")) commitActive(); });
      });
      /* a pointer click must not leave focus on a button, or the next Space would press it instead of pausing */
      document.addEventListener("click", (e) => { const b = e.target.closest && e.target.closest("button"); if (b && e.detail > 0) b.blur(); });
      new ResizeObserver(() => { fitHero(); if (Z.pic) placeZoom(); }).observe($("frame"));
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", onKeyUp);
    paint();
    connect();
  }

  window.__live = {
    state: () => ({ seq: S.seq, counts: { ...S.counts }, online: S.online, paused: S.paused, queue: S.queue.length, current: S.current && lead(S.current).id,
      piles: [...S.piles.values()].map((p) => ({ id: p.id, name: p.name, count: p.count })), flights: flights.slice(),
      prev: S.prev && lead(S.prev).id, zoom: Z.open, tab: S.tab, panel: S.panel, shown: client.shown, find: { ...S.find } }),
    next: (n) => api.next && api.next(n),
    demo: api,
  };
  boot();
})();
