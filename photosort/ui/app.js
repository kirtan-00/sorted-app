(function () {
  "use strict";

  var state = {
    view: "search",
    results: [],
    total: 0,
    offset: 0,
    lastParams: {},
    selected: new Set(),
    people: [],
    progressTimer: null,
    folder: { root: null, name: null, indexed: false },
    recent: [],
    categories: { fixed: {}, discovered: {}, drone: 0 },
    classifyTimer: null,
    findPath: null,
    exportDest: null,
    savedPeople: [],
    peopleUnticked: new Set(),
    catTicked: new Set(),       // tile keys ticked for "Export ticked categories": a fixed name, or "discovered:" + name
    catSeen: new Set(),         // tile keys already given their default tick (all but "unclassified")
    // ===== server capabilities and pending work: set from /api/stats =====
    caps: { focus: false },     // true once /api/stats carries "focus": the server has the focus and reorganise endpoints
    facesPending: 0,            // ok rows with n_faces NULL (indexed with faces off)
    // ===== end server capabilities =====
    // ===== navigation: where the Search filter came from, and each list's scroll position =====
    ctx: null,                  // null, or {from: "people"|"categories", kind: "person"|"saved"|"category"|"cluster"|"drone", key}
    scroll: {},                 // view name -> main.scrollTop when the user left it
    // ===== end navigation =====
  };

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // The status line holds the text span plus the "name this person" form, so only the span is written.
  var statusEl = $("#status-text");
  var statusTimer = null;
  function setStatus(msg, hold) {
    statusEl.textContent = msg || "";
    // ===== status colour: an error message gets .err (danger) on the status bar =====
    $("#status").classList.toggle("err", /^(could not|failed|error)|\bfailed:/i.test(msg || ""));
    // ===== end status colour =====
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    if (msg && !hold) {
      statusTimer = setTimeout(function () { statusEl.textContent = ""; }, 6000);
    }
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          var err = new Error((body && body.detail) || (r.status + " " + r.statusText));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    });
  }

  // ===== usage log: what the tester did, kept on this Mac (POST /api/usage, see docs/usage-log.md) =====
  // One hook, no per-button wiring: document-level click, change and keydown listeners plus window errors.
  // Events queue up and go out every 2 s in one request (keepalive, so a tab close still delivers; sendBeacon
  // on pagehide). Facts only: ids, classes, tabs, key names, filter values; never a query, a name or a path.
  var usageQueue = [];
  var usageTab = { name: null, since: Date.now() };
  function track(ev, fields) {
    var e = fields || {};
    e.ev = ev;
    if (e.view === undefined) e.view = state.view;
    usageQueue.push(e);
    if (usageQueue.length >= 200) flushUsage();
  }
  function flushUsage(unloading) {
    if (!usageQueue.length) return;
    var body = JSON.stringify({ events: usageQueue.splice(0, 200) });
    try {
      if (unloading && navigator.sendBeacon) {
        navigator.sendBeacon("/api/usage", new Blob([body], { type: "application/json" }));
        return;
      }
      fetch("/api/usage", { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true })
        .catch(function () { /* the log is best effort */ });
    } catch (err) { /* the log is best effort */ }
  }
  setInterval(function () { flushUsage(); }, 2000);
  function trackTab(name) {                            // called from showView: the tab switch plus time on the one just left
    if (usageTab.name === name) return;
    var now = Date.now();
    if (usageTab.name) track("tab_time", { tab: usageTab.name, seconds: Math.round((now - usageTab.since) / 100) / 10, view: usageTab.name });
    usageTab = { name: name, since: now };
    track("tab", { tab: name, view: name });
  }
  function usageTarget(t) {
    var el = t && t.closest ? t.closest("button, a, .card, .cat-tile, label.check, .seg label, .panel-head, [data-view]") : null;
    if (!el) return null;
    var tile = el.classList.contains("cat-tile") ? el : (el.classList.contains("cat-tile-main") ? el.closest(".cat-tile") : null);
    if (tile) {
      return { ev: "tile", kind: tile.classList.contains("drone") ? "drone" : (tile.querySelector(".disc-tick") ? "discovered" : "category") };
    }
    if (el.classList.contains("card")) {
      if (el.classList.contains("person")) return { ev: "tile", kind: "person" };
      return { ev: "card", kind: el.querySelector(".badge:not(.drone)") ? "video" : "photo", unsure: el.classList.contains("unsure") };
    }
    if (el.classList.contains("panel-head")) {
      var panel = el.closest(".panel");
      return { ev: "panel", id: panel ? panel.dataset.panel : "", open: el.getAttribute("aria-expanded") !== "true" };
    }
    if (el.dataset && el.dataset.view) return null;   // the tab itself: showView logs it
    var input = el.tagName === "LABEL" ? el.querySelector("input") : null;
    if (input) return null;                            // its change event is the fact, logged below
    var out = { ev: "click", id: el.id || "" };
    if (!el.id) out.cls = (el.className && typeof el.className === "string" ? el.className.split(" ")[0] : el.tagName.toLowerCase());
    if (!el.id && el.form && el.form.id) out.form = el.form.id;   // the Find button: a submit in form#q
    if (el.tagName === "A" && el.href) out.href = el.href.split(":")[0];   // the scheme only (mailto)
    if (el.closest("#help")) out.where = "help";
    else if (el.closest("#lightbox")) out.where = "lightbox";
    return out;
  }
  document.addEventListener("click", function (e) {
    var f = usageTarget(e.target);
    if (!f) return;
    var ev = f.ev; delete f.ev;
    if (e.shiftKey) f.shift = true;
    if (e.metaKey || e.ctrlKey) f.cmd = true;
    track(ev, f);
  }, true);
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.name && !t.id) return;
    var f = { id: t.id || "", name: t.name || "" };
    if (t.type === "checkbox" || t.type === "radio") f.value = t.type === "radio" ? t.value : t.checked;
    else if (t.tagName === "SELECT") f.value = t.name === "person" || t.id === "person-select" || t.id === "recent-folders" ? (t.value ? "set" : "cleared") : t.value;
    else if (t.type === "range" || t.type === "number") f.value = Number(t.value);
    else f.chars = (t.value || "").length;             // a text field: only how much was typed
    track("filter", f);
  }, true);
  document.addEventListener("keydown", function (e) {
    var t = e.target; var tag = t && t.tagName;
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    var cmd = e.metaKey || e.ctrlKey;
    var special = /^(Escape|Enter|ArrowLeft|ArrowRight|ArrowUp|ArrowDown| |\/)$/.test(e.key);
    if (!cmd && !special) return;                      // plain typing is never logged
    if (!cmd && typing && !/^(Escape|Enter)$/.test(e.key)) return;   // in a field only Esc and Enter are shortcuts
    if (cmd && !/^[a-z0-9\[\]]$/i.test(e.key) && !special) return;
    track("key", { key: e.key === " " ? "Space" : e.key, cmd: cmd, shift: e.shiftKey, typing: typing });
  }, true);
  window.addEventListener("error", function (e) {
    var src = (e.filename || "").split("/").pop();
    track("error", { message: String(e.message || "").slice(0, 200), source: src, line: e.lineno || 0, col: e.colno || 0 });
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    track("error", { message: String(r && r.message ? r.message : r).slice(0, 200), source: "promise" });
  });
  window.addEventListener("pagehide", function () {
    if (usageTab.name) track("tab_time", { tab: usageTab.name, seconds: Math.round((Date.now() - usageTab.since) / 100) / 10, view: usageTab.name });
    track("unload");
    flushUsage(true);
  });
  // ===== end usage log =====

  // ---------- tabs ----------
  // ===== scroll memory: main is the one scroller and the sections swap inside it, so each list's position is kept per view =====
  var mainEl = $("main");
  var pendingScroll = null;                            // {view, top}: a restore that a later render (people, categories) reapplies once
  function restoreScroll(name) {
    var top = state.scroll[name] || 0;
    mainEl.scrollTop = top;
    pendingScroll = top ? { view: name, top: top } : null;
  }
  function settleScroll(name) {                        // called by the list renders: the tab's async reload rebuilt the list, put the scroll back once
    if (pendingScroll && pendingScroll.view === name && state.view === name) { mainEl.scrollTop = pendingScroll.top; }
    if (pendingScroll && pendingScroll.view === name) pendingScroll = null;
  }
  // The user scrolling on their own ends the pending restore, so a later re-render (a "Same" answer, Group faces) never yanks the list back.
  mainEl.addEventListener("scroll", function () { if (pendingScroll && mainEl.scrollTop !== pendingScroll.top) pendingScroll = null; });
  // ===== end scroll memory =====
  function showView(name) {
    // ===== scroll memory: leaving a view keeps its position =====
    var switching = name !== state.view;
    if (switching) state.scroll[state.view] = mainEl.scrollTop;
    // ===== end scroll memory =====
    state.view = name;
    // ===== nav selector: only the four view buttons; the "Check focus" link also lives in <nav> =====
    $$("header nav button[data-view]").forEach(function (b) {
      b.classList.toggle("on", b.dataset.view === name);
    });
    // ===== end nav selector =====
    var hasFolder = !!(state.folder && state.folder.root);
    $$("main > section").forEach(function (s) {
      if (s.id === "view-nofolder") { s.hidden = hasFolder; return; }
      s.hidden = !hasFolder || s.id !== "view-" + name;
    });
    if (!hasFolder) return;
    trackTab(name);                                    // ===== usage log: the tab and the time on the one before =====
    // ===== scroll memory: the returned-to list sits where it was =====
    if (switching) restoreScroll(name);
    // ===== end scroll memory =====
    // ===== navigation: a saved-person find owed from a history restore runs once Search shows =====
    if (name === "search" && pendingFind) { var pf = pendingFind; pendingFind = null; findSaved(pf); }
    // ===== end navigation =====
    if (name === "people" && state.people.length === 0) loadPeople();
    if (name === "people") { loadReferences(); loadSuggestions(); }   // loadSuggestions: the "same person?" block below
    if (name === "categories") loadCategories();
    mountDriveStrip();                                 // ===== Google Drive: the strip sits under whichever export row is showing =====
  }
  $$("header nav button[data-view]").forEach(function (b) {   // ===== nav selector (see showView) =====
    b.addEventListener("click", function () { goView(b.dataset.view); });   // goView: showView plus a history entry (navigation block below)
  });

  // ===== navigation: context bar, breadcrumb, browser history, back =====
  // A jump from a People or Categories row sets state.ctx and lands on Search with the grid filtered; the
  // context bar under the sub-toolbar shows the way back. Every tab switch and every jump is a history entry
  // ({view, ctx, root} plus a hash such as #people or #search?person=126), so command-[ and the trackpad
  // swipe walk back through them and a reload keeps the view. The no-folder screen and the lightbox never push.
  var ctxBar = $("#ctxbar");
  var ctxBackBtn = $("#ctx-back");
  var ctxBackLabel = $("#ctx-back-label");
  var ctxCrumb = $("#ctx-crumb");
  var ctxClearBtn = $("#ctx-clear");
  var viewSearch = $("#view-search");
  var VIEWS = ["search", "people", "categories", "index"];
  var TAB_NAMES = { people: "People", categories: "Categories" };
  try { history.scrollRestoration = "manual"; } catch (e) { /* not supported */ }

  function ctxKey(ctx) { return JSON.stringify(ctx || null); }
  function personById(id) {
    for (var i = 0; i < state.people.length; i++) if (state.people[i].id === id) return state.people[i];
    return null;
  }
  function personLabel(p, id) { return (p && p.name) || "person_" + String(id).padStart(2, "0"); }
  // The filter params a context stands for; passed to runSearch explicitly at boot, when the person select has no options yet.
  function ctxParams(ctx) {
    if (!ctx) return {};
    if (ctx.kind === "person") return { person: ctx.key };
    if (ctx.kind === "category") return { category: ctx.key };
    if (ctx.kind === "cluster") return { cluster: ctx.key };
    if (ctx.kind === "drone") return { aerial: 1 };
    return {};
  }
  // Write the context into the form: the person select, the two hidden category fields, the drone box and the chip.
  function applyCtxFilters(ctx) {
    var kind = ctx ? ctx.kind : null;
    personSelect.value = kind === "person" ? String(ctx.key) : "";
    $("#category-filter").value = kind === "category" ? ctx.key : "";
    $("#cluster-filter").value = kind === "cluster" ? ctx.key : "";
    aerialOnly.checked = kind === "drone";
    if (kind === "category") showCategoryChip("category: " + ctx.key);
    else if (kind === "cluster") showCategoryChip("discovered: " + ctx.key);
    else if (kind === "drone") showCategoryChip("drone");
    else showCategoryChip(null);
  }
  function setCtx(ctx) {
    state.ctx = ctx || null;
    applyCtxFilters(state.ctx);
    renderCtxBar();
  }
  function crumbSeg(text, last) {
    var s = document.createElement("span");
    s.className = "crumb-seg" + (last ? " last" : "");
    s.textContent = text;
    return s;
  }
  // "People / Dhaval owner / 760 photos", "Categories / discovered / fishing boats / 42". A text query ranks the
  // whole filtered set rather than narrowing it, so with a query the tail reads from the search's own numbers:
  // "beach · 'sunset' · 200 of 807 shown".
  function renderCtxBar() {
    var ctx = state.ctx;
    ctxBar.hidden = !ctx;
    viewSearch.classList.toggle("has-ctx", !!ctx);
    $$("header nav button[data-view]").forEach(function (b) { b.classList.toggle("from", !!ctx && b.dataset.view === ctx.from); });
    if (!ctx) return;
    var tab = TAB_NAMES[ctx.from] || ctx.from;
    ctxBackLabel.textContent = tab;
    ctxBackBtn.title = "back to " + tab + " (Esc)";
    ctxClearBtn.title = "drop the " + tab + " filter and stay on Search";
    var segs = [tab];
    var name, count = null;
    if (ctx.kind === "person") {
      var p = personById(ctx.key);
      name = personLabel(p, ctx.key);
      if (p) count = p.n + " photo" + (p.n === 1 ? "" : "s");
    } else if (ctx.kind === "saved") {
      name = ctx.key;
      count = state.total + " photo" + (state.total === 1 ? "" : "s");
    } else if (ctx.kind === "cluster") {
      segs.push("discovered");
      name = /^group \d+$/.test(ctx.key) ? "Unnamed " + ctx.key : ctx.key;
      if (state.categories.discovered[ctx.key] != null) count = String(state.categories.discovered[ctx.key]);
    } else if (ctx.kind === "drone") {
      name = "drone";
      if (state.categories.drone) count = String(state.categories.drone);
    } else {
      name = ctx.key;
      if (state.categories.fixed[ctx.key] != null) count = String(state.categories.fixed[ctx.key]);
    }
    var q = state.lastParams.q, ranked = !!(q || state.lastParams.image_id);
    if (ranked && ctx.kind !== "saved") {
      segs.push(name + " · " + (q ? "'" + q + "'" : "similar shots") + " · " + state.results.length + " of " + state.total + " shown");
    } else {
      segs.push(name);
      if (count != null) segs.push(count);
    }
    ctxCrumb.innerHTML = "";
    segs.forEach(function (t, i) {
      if (i) { var sep = document.createElement("span"); sep.className = "crumb-sep"; sep.textContent = "/"; ctxCrumb.appendChild(sep); }
      ctxCrumb.appendChild(crumbSeg(t, i === segs.length - 1));
    });
  }

  // history
  var navRestoring = false;
  function hashFor(view, ctx) {
    if (view !== "search" || !ctx) return "#" + view;
    var key = { person: "person", saved: "saved", category: "category", cluster: "cluster", drone: "aerial" }[ctx.kind];
    return "#search?" + key + "=" + encodeURIComponent(ctx.kind === "drone" ? 1 : ctx.key);
  }
  function parseHash(h) {
    var m = /^#(search|people|categories|index)(?:\?(.*))?$/.exec(h || "");
    if (!m) return null;
    var entry = { view: m[1], ctx: null };
    if (m[1] === "search" && m[2]) {
      var p = new URLSearchParams(m[2]);
      if (p.get("person") && /^\d+$/.test(p.get("person"))) entry.ctx = { from: "people", kind: "person", key: Number(p.get("person")) };
      else if (p.get("saved")) entry.ctx = { from: "people", kind: "saved", key: p.get("saved") };
      else if (p.get("category")) entry.ctx = { from: "categories", kind: "category", key: p.get("category") };
      else if (p.get("cluster")) entry.ctx = { from: "categories", kind: "cluster", key: p.get("cluster") };
      else if (p.get("aerial")) entry.ctx = { from: "categories", kind: "drone", key: "drone" };
    }
    return entry;
  }
  // backable: the entry before this one is the origin tab, so the Back button can use history.back() and keep Forward alive.
  function navPush(replace, backable) {
    if (navRestoring || !(state.folder && state.folder.root)) return;
    var entry = { view: state.view, ctx: state.ctx, root: state.folder.root, backable: !!backable };
    var cur = history.state;
    var same = cur && cur.view === entry.view && ctxKey(cur.ctx) === ctxKey(entry.ctx) && cur.root === entry.root;
    var url = hashFor(entry.view, entry.ctx);
    try {
      if (replace || !cur || same) history.replaceState(same && !replace ? cur : entry, "", url);
      else history.pushState(entry, "", url);
    } catch (e) { /* file: origins refuse */ }
  }
  function goView(name) {
    showView(name);
    navPush();
  }
  // Land on Search filtered by a row of another tab. Called by the People rows, the saved-people rows and the category rows.
  function jumpTo(ctx, run) {
    var backable = state.view === ctx.from;
    setCtx(ctx);
    showView("search");
    mainEl.scrollTop = 0;
    run();
    navPush(false, backable);
  }
  function navBack() {
    var from = state.ctx && state.ctx.from;
    if (!from) return;
    if (history.state && history.state.backable && history.state.root === state.folder.root) { history.back(); return; }
    setCtx(null);
    runSearch();
    goView(from);
  }
  function clearCtx() {
    if (!state.ctx) return;
    setCtx(null);
    runSearch();
    navPush();
  }
  // Put an entry's view and filter back; the grid is re-run whenever the filter changed, even on another tab, so it is never stale.
  function restoreEntry(entry) {
    var ctx = entry.root && entry.root !== state.folder.root ? null : entry.ctx;
    var changed = ctxKey(ctx) !== ctxKey(state.ctx);
    navRestoring = true;
    setCtx(ctx);
    showView(VIEWS.indexOf(entry.view) >= 0 ? entry.view : "search");
    if (changed) {
      if (ctx && ctx.kind === "saved") { if (state.view === "search") findSaved(ctx.key); else pendingFind = ctx.key; }
      else runSearch(ctxParams(ctx));
    }
    navRestoring = false;
  }
  var pendingFind = null;                              // a saved-person find owed to the Search tab, run when it next shows
  window.addEventListener("popstate", function (e) {
    if (!(state.folder && state.folder.root)) return;
    var entry = (e.state && e.state.view) ? e.state : parseHash(location.hash);
    if (entry) restoreEntry(entry);
  });
  function navBootEntry() {                            // history.state survives a reload; the hash is the fallback (a typed or pasted URL)
    var s = history.state;
    if (s && s.view && VIEWS.indexOf(s.view) >= 0) return { view: s.view, ctx: s.root === state.folder.root ? s.ctx : null };
    return parseHash(location.hash);
  }
  ctxBackBtn.addEventListener("click", navBack);
  ctxClearBtn.addEventListener("click", clearCtx);
  // ===== end navigation =====

  // ---------- stats ----------
  var lastErrorCount = null;
  function loadStats() {
    return api("/api/stats").then(function (s) {
      // ===== title block: "630 photos · 325 videos · 606 faces", the index time in the tooltip =====
      var bits = [s.photos + " photos"];
      if (s.videos) bits.push(s.videos + " videos");
      bits.push(s.faces + " faces");
      if (s.people) bits.push(s.people + " people");
      if (s.indexing) bits.push("indexing…");
      if (s.errors) bits.push(s.errors + " failed");
      if (s.faces_pending) bits.push(s.faces_pending + " need faces");
      $("#stats").textContent = bits.join(" · ");
      $("#stats").title = s.last_index ? "indexed " + s.last_index : "";
      // ===== end title block =====
      // ===== after stats: capabilities, the faces-pending controls, the focus line and the reorganise status =====
      state.caps.focus = !!(s && typeof s.focus === "object");   // an older server has no focus or reorganise endpoints
      renderFacesPending(s);
      loadFocusStatus();
      loadReorganiseStatus();
      // ===== end after stats =====
      if (s.errors !== lastErrorCount) {
        lastErrorCount = s.errors;
        loadErrors();
      }
      return s;
    });
  }

  function loadErrors() {
    return api("/api/errors").then(function (data) {
      var list = (data && data.errors) || [];
      var box = $("#index-errors");
      box.hidden = list.length === 0;
      $("#index-errors-title").textContent = list.length + " file(s) could not be read. They are skipped; tick retry and Index again once fixed.";
      $("#index-errors-list").textContent = list.slice(0, 200).map(function (e) { return e.rel; }).join("\n") + (list.length > 200 ? "\n… " + (list.length - 200) + " more" : "");
    }).catch(function () { /* non-fatal */ });
  }

  // ---------- folder ----------
  var folderNameEl = $("#folder-name");
  var recentSelect = $("#recent-folders");

  function applyFolderInfo(info) {
    state.folder = info || { root: null, name: null, indexed: false };
    folderNameEl.textContent = state.folder.name || "no folder open";
    folderNameEl.title = state.folder.root || "";
  }

  function loadFolder() {
    return api("/api/folder").then(function (info) {
      applyFolderInfo(info);
      return info;
    });
  }

  function loadRecent() {
    return api("/api/folder/recent").then(function (data) {
      state.recent = (data && data.recent) || [];
      renderRecent();
    }).catch(function () { /* non-fatal */ });
  }

  function renderRecent() {
    recentSelect.innerHTML = '<option value="">recent&hellip;</option>';
    state.recent.forEach(function (r) {
      var opt = document.createElement("option");
      opt.value = r.path;
      opt.textContent = r.name || r.path;
      recentSelect.appendChild(opt);
    });
    recentSelect.value = "";
  }

  // Reached after any folder switch: reset per-folder UI state, then either land
  // on the Index tab (fresh folder, nothing indexed yet) or refresh the current view.
  function settleFolder(info) {
    state.people = [];
    state.results = [];
    state.selected = new Set();
    state.categories = { fixed: {}, discovered: {}, drone: 0 };
    state.catTicked = new Set(); state.catSeen = new Set();
    state.findPath = null; syncSaveForm();            // a reference from the previous shoot must not be saved into this one
    state.savedPeople = []; state.peopleUnticked = new Set(); renderSavedPeople();
    renderGrid();
    peopleEl.innerHTML = "";
    personSelect.innerHTML = '<option value="">anyone</option>';
    progressEl.textContent = "";
    $("#index-errors").hidden = true;
    lastErrorCount = null;
    $("#category-filter").value = ""; $("#cluster-filter").value = "";
    showCategoryChip(null);
    // ===== per-folder reset: focus boxes and line, faces pending, the reorganise section =====
    $("#hide-bad").checked = false; $("#hide-soft").checked = false;
    renderFocusLine(null);
    renderFacesPending(null);
    resetReorganise();
    // ===== end per-folder reset =====
    // ===== navigation: a filter from the previous shoot means nothing here; the lists start at the top =====
    setCtx(null);
    state.scroll = {};
    // ===== end navigation =====
    loadRecent();
    if (!info.root) {
      showView(state.view);
      return;
    }
    if (!info.indexed) {
      showView("index");
      navPush(true);                                   // ===== navigation: the new shoot replaces the entry, never a push =====
      var btn = $("#start-index");
      if (btn) btn.focus();
      loadStats().then(function (s) { if (s.indexing) pollProgress(); });   // first index of a fresh folder, page reloaded mid-run
      return;
    }
    loadStats().then(function (s) { loadErrors(); if (s.indexing) pollProgress(); });
    loadPeople();
    loadCategories();                                  // fills the count beside the Categories nav row
    runSearch();
    showView(state.view === "index" ? "search" : state.view);
    navPush(true);                                     // ===== navigation: the new shoot replaces the entry, never a push =====
  }

  function openFolderPicker() {
    setStatus("waiting for the folder picker…", true);
    return fetch("/api/folder/choose", { method: "POST" }).then(function (r) {
      if (r.status === 204) { setStatus("folder pick cancelled"); return null; }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || (r.status + " " + r.statusText));
        });
      }
      return r.json();
    }).then(function (info) {
      if (!info) return;
      applyFolderInfo(info);
      setStatus("opened " + (info.name || info.root));
      settleFolder(info);
    }).catch(function (err) {
      setStatus("could not open folder: " + err.message);
    });
  }

  function switchFolder(path) {
    setStatus("opening " + path + "…", true);
    return api("/api/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path }),
    }).then(function (info) {
      applyFolderInfo(info);
      setStatus("opened " + (info.name || info.root));
      settleFolder(info);
    }).catch(function (err) {
      setStatus("could not open folder: " + err.message);
    });
  }

  // export destination (another disk)
  var exportDestEl = $("#export-dest");
  var exportDestReset = $("#export-dest-reset");

  function renderExportDest() {
    var d = state.exportDest;
    if (!d) { exportDestEl.textContent = ""; exportDestEl.title = ""; exportDestReset.hidden = true; return; }
    // ===== destination label: the path only (truncated from the start by CSS), the sentence in the tooltip =====
    exportDestEl.textContent = d.path + (d.mounted ? " (" + d.free_gb + " GB free)" : " (not mounted)");
    exportDestEl.title = "exports go to " + d.path;
    // ===== end destination label =====
    exportDestReset.hidden = !!d.default;
  }

  function loadExportDest() {
    return api("/api/export/destination").then(function (d) {
      state.exportDest = d;
      renderExportDest();
      return d;
    }).catch(function () { /* non-fatal */ });
  }

  $("#export-dest-change").addEventListener("click", function () {
    setStatus("waiting for the folder picker…", true);
    fetch("/api/export/destination/choose", { method: "POST" }).then(function (r) {
      if (r.status === 204) { setStatus("destination unchanged"); return null; }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || (r.status + " " + r.statusText));
        });
      }
      return r.json();
    }).then(function (d) {
      if (!d) return;
      state.exportDest = d;
      renderExportDest();
      setStatus("exports now go to " + d.path);
    }).catch(function (err) {
      setStatus("could not change the destination: " + err.message);
    });
  });

  exportDestReset.addEventListener("click", function () {
    api("/api/export/destination", { method: "DELETE" }).then(function (d) {
      state.exportDest = d;
      renderExportDest();
      setStatus("exports go back to " + d.path);
    }).catch(function (err) {
      setStatus("could not reset the destination: " + err.message);
    });
  });

  $("#open-folder").addEventListener("click", openFolderPicker);
  $("#open-folder-main").addEventListener("click", openFolderPicker);
  recentSelect.addEventListener("change", function () {
    var path = recentSelect.value;
    if (path) switchFolder(path);
  });

  // ---------- search ----------
  var form = $("#q");
  var sharpInput = form.querySelector('[name="sharp"]');
  var sharpOut = $("#sharp-out");
  // ===== slider value fields: the number next to a slider is a text field; typing moves the slider =====
  function bindSliderField(range, field, onCommit) {
    function paint() { range.style.setProperty("--p", (range.value - range.min) / (range.max - range.min)); }
    range.addEventListener("input", function () { field.value = range.value; paint(); });
    field.addEventListener("change", function () {
      var v = parseFloat(field.value);
      if (isNaN(v)) { field.value = range.value; return; }
      v = Math.min(Number(range.max), Math.max(Number(range.min), v));
      range.value = v; field.value = range.value; paint();
      onCommit();
    });
    paint();
  }
  bindSliderField(sharpInput, sharpOut, function () { runSearch(); });
  sharpInput.addEventListener("change", function () { runSearch(); });
  // ===== end slider value fields =====

  function currentFilters() {
    var fd = new FormData(form);
    var params = {};
    var q = (fd.get("q") || "").trim();
    if (q) params.q = q;
    var sharp = fd.get("sharp");
    if (sharp && Number(sharp) > 0) params.sharp = sharp;
    var faces = fd.get("faces");
    if (faces) params.faces = faces;
    var person = fd.get("person");
    if (person) params.person = person;
    var category = fd.get("category");
    if (category) params.category = category;
    var kind = fd.get("kind");
    if (kind) params.kind = kind;
    var cluster = fd.get("cluster");
    if (cluster) params.cluster = cluster;
    if (fd.get("aerial")) params.aerial = 1;
    // ===== focus filters: hide_bad drops the bad rows, hide_soft drops soft and bad; unchecked rows are never hidden =====
    if (fd.get("hide_bad")) params.hide_bad = 1;
    if (fd.get("hide_soft")) params.hide_soft = 1;
    // ===== end focus filters =====
    return params;
  }

  var PAGE = 200;
  function runSearch(extra, append) {
    // ===== navigation: a saved-person find is not a form filter, so any plain search replaces it and drops that crumb =====
    if (state.ctx && state.ctx.kind === "saved") { state.ctx = null; renderCtxBar(); }
    // ===== end navigation =====
    var params = currentFilters();
    Object.assign(params, extra || {});
    if (!append) { state.offset = 0; state.results = []; }
    params.limit = PAGE; params.offset = state.offset;
    state.lastParams = params;
    var qs = new URLSearchParams(params).toString();
    return api("/api/search?" + qs).then(function (data) {
      state.results = append ? state.results.concat(data.results || []) : (data.results || []);
      state.total = data.total || 0;
      state.offset = state.results.length;
      renderGrid();
    }).catch(function (err) {
      if (err.status === 404) {
        state.results = []; state.total = 0;
        renderGrid();
        setStatus("that photo has no embedding to compare against");
      } else {
        setStatus("search failed: " + err.message);
      }
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    runSearch();
  });
  Array.prototype.filter.call(form.elements, function (el) { return el.tagName === "SELECT" || el.type === "radio"; }).forEach(function (sel) {
    sel.addEventListener("change", function () { runSearch(); });   // form.elements also sees the kind segments in the sidebar (form="q")
  });
  // ===== focus filters: a change on either box re-runs the search =====
  [$("#hide-bad"), $("#hide-soft")].forEach(function (box) { box.addEventListener("change", function () { runSearch(); }); });
  // ===== end focus filters =====
  var aerialOnly = $("#aerial-only");
  aerialOnly.addEventListener("change", function () {
    // Unticking the box by hand is the same as clearing the "drone" chip the tile put up.
    if (!aerialOnly.checked && $("#category-chip-name").textContent === "drone") showCategoryChip(null);
    // ===== navigation: unticking by hand also ends a drone context =====
    if (!aerialOnly.checked && state.ctx && state.ctx.kind === "drone") { state.ctx = null; renderCtxBar(); navPush(); }
    // ===== end navigation =====
    runSearch();
  });
  // ===== navigation: the "anyone" picker moves a People context to the picked person, or ends it on "anyone" =====
  $("#person-select").addEventListener("change", function () {   // personSelect itself is assigned further down
    if (!state.ctx || state.ctx.from !== "people") return;
    state.ctx = personSelect.value ? { from: "people", kind: "person", key: Number(personSelect.value) } : null;
    renderCtxBar();
    navPush();
  });
  // ===== end navigation =====
  $("#show-more").addEventListener("click", function () { runSearch(state.lastParams, true); });
  $("#select-matching").addEventListener("click", function () {
    var p = Object.assign({}, state.lastParams); delete p.limit; delete p.offset;
    api("/api/search/ids?" + new URLSearchParams(p).toString()).then(function (data) {
      (data.ids || []).forEach(function (id) { state.selected.add(id); });
      $$(".card", gridEl).forEach(function (c) { if (state.selected.has(Number(c.dataset.id))) c.classList.add("selected"); });
      updateSelbar();
      setStatus("selected all " + data.total + " matching photo(s)");
    }).catch(function (err) { setStatus("could not select: " + err.message); });
  });

  // n_faces is null when the photo was indexed with faces off: not "0", just unknown
  function facesLabel(n) { return n == null ? "?" : String(n); }

  function mmss(seconds) {
    var t = Math.max(0, Math.round(seconds || 0));
    var m = Math.floor(t / 60), s = t % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  var gridEl = $("#grid");
  // ===== grid selection model: id -> result map for the inspector, shift-click ranges, a roving tabindex =====
  var resultById = new Map();
  var lastClickedId = null;
  var hoveredId = null;
  function cardEl(id) { return gridEl.querySelector('.card[data-id="' + id + '"]'); }
  function indexOfId(id) {
    for (var i = 0; i < state.results.length; i++) if (state.results[i].id === id) return i;
    return -1;
  }
  function clickCard(e, r, card) {
    if (e.shiftKey && lastClickedId != null) {
      var a = indexOfId(lastClickedId), b = indexOfId(r.id);
      if (a >= 0 && b >= 0) {
        for (var i = Math.min(a, b); i <= Math.max(a, b); i++) state.selected.add(state.results[i].id);
        $$(".card", gridEl).forEach(function (c) { if (state.selected.has(Number(c.dataset.id))) c.classList.add("selected"); });
        updateSelbar();
        return;
      }
    }
    lastClickedId = r.id;
    toggleSelect(r.id, card);                          // a plain click and a command-click both toggle
  }
  gridEl.addEventListener("mouseover", function (e) {
    var c = e.target.closest ? e.target.closest(".card") : null;
    if (c && gridEl.contains(c)) { hoveredId = Number(c.dataset.id); renderInspector(); }
  });
  gridEl.addEventListener("mouseleave", function () { hoveredId = null; renderInspector(); });
  gridEl.addEventListener("focusin", function (e) {
    var c = e.target.closest ? e.target.closest(".card") : null;
    if (!c) return;
    $$(".card", gridEl).forEach(function (x) { x.tabIndex = x === c ? 0 : -1; });
  });
  function moveFocus(key) {
    var cards = $$(".card", gridEl);
    var i = cards.indexOf(document.activeElement);
    if (i < 0) { if (cards[0]) cards[0].focus(); return; }
    var cols = getComputedStyle(gridEl).gridTemplateColumns.split(" ").length || 1;
    var j = key === "ArrowRight" ? i + 1 : key === "ArrowLeft" ? i - 1 : key === "ArrowDown" ? i + cols : i - cols;
    if (j >= 0 && j < cards.length) { cards[j].focus(); cards[j].scrollIntoView({ block: "nearest" }); }
  }
  // ===== end grid selection model =====
  function renderGrid() {
    gridEl.innerHTML = "";
    resultById = new Map();
    hoveredId = null;
    var divided = false;
    state.results.forEach(function (r, idx) {
      resultById.set(r.id, r);
      // Results arrive sure first, then the "less sure" band by confidence: one divider before the first
      // unsure one. The whole list is rebuilt from state.results, so "Show more" keeps a single divider.
      var unsure = r.sure === false;
      if (unsure && !divided) {
        divided = true;
        var div = document.createElement("div");
        div.className = "grid-divider mono";
        div.textContent = "less sure, sorted by confidence";
        gridEl.appendChild(div);
      }
      var card = document.createElement("div");
      card.className = "card";
      if (unsure) card.classList.add("unsure");
      if (state.selected.has(r.id)) card.classList.add("selected");
      card.dataset.id = r.id;

      var img = document.createElement("img");
      img.loading = "lazy";
      img.alt = r.rel;
      img.src = "/api/thumb/" + r.qhash + "?size=grid";
      card.appendChild(img);

      // no caption on the card: sharpness, faces and confidence live in the inspector
      card.tabIndex = idx === 0 ? 0 : -1;
      if (r.kind === "video") {
        var badge = document.createElement("div");
        badge.className = "badge mono";
        badge.textContent = mmss(r.duration);          // the play triangle is a CSS glyph on .badge
        card.appendChild(badge);
      }
      if (r.aerial) {
        var drone = document.createElement("div");
        drone.className = "badge drone mono";
        drone.textContent = "drone";
        card.appendChild(drone);
      }

      card.addEventListener("click", function (e) { clickCard(e, r, card); });
      card.addEventListener("dblclick", function () {
        openLightbox(r);
      });
      gridEl.appendChild(card);
    });
    var more = $("#more-row");
    more.hidden = state.results.length === 0;
    // A text or image query ranks the whole shoot, so "total" is the shoot size and
    // "select all matching" would select everything: only filter-only searches get it.
    var ranked = !!(state.lastParams.q || state.lastParams.image_id);
    $("#shown-count").textContent = ranked ? state.results.length + " shown" : state.results.length + " of " + state.total + " shown";
    $("#show-more").hidden = state.results.length >= state.total;
    $("#select-matching").hidden = ranked;
    updateSelbar();
    renderCtxBar();                                    // ===== navigation: the crumb's query tail follows the results =====
  }

  function toggleSelect(id, card) {
    if (state.selected.has(id)) {
      state.selected.delete(id);
      card.classList.remove("selected");
    } else {
      state.selected.add(id);
      card.classList.add("selected");
    }
    updateSelbar();
  }

  var selbar = $("#selbar");
  var selcount = $("#selcount");
  var selectAllTop = $("#selectall-top");
  function updateSelbar() {
    var n = state.selected.size;
    var shown = state.results.length;
    selbar.hidden = shown === 0 && n === 0;
    selbar.classList.toggle("has-sel", n > 0);
    selectAllTop.hidden = shown === 0;
    selcount.textContent = n + " selected" + (shown ? " of " + shown + " shown" : "");
    renderInspector();                                 // the inspector follows the selection
  }

  function selectAllShown() {
    state.results.forEach(function (r) { state.selected.add(r.id); });
    $$(".card", gridEl).forEach(function (c) { c.classList.add("selected"); });
    updateSelbar();
    setStatus("selected " + state.results.length + " shown photo(s)");
  }
  $("#selectall").addEventListener("click", selectAllShown);
  selectAllTop.addEventListener("click", selectAllShown);

  $("#clearsel").addEventListener("click", function () {
    state.selected.clear();
    $$(".card.selected", gridEl).forEach(function (c) { c.classList.remove("selected"); });
    updateSelbar();
  });

  var exportTimer = null;
  var EXPORT_POLL_MAX_FAILS = 5;
  function pollExportProgress(label) {
    if (exportTimer) clearInterval(exportTimer);
    var fails = 0;
    exportTimer = setInterval(function () {
      api("/api/export/progress").then(function (p) {
        fails = 0;
        // ===== Google Drive: an upload reports files in flight and bytes; the strip shows the same plus the folder link at the end =====
        if (p.what === "drive") {
          if (p.running) { renderDriveProgress(p); return; }
          clearInterval(exportTimer); exportTimer = null;
          renderDriveDone(p);
          return;
        }
        // ===== end Google Drive =====
        if (p.running) {
          setStatus(label + " " + p.done + "/" + p.total + (p.failed ? ", " + p.failed + " failed" : "") + (p.skipped ? ", " + p.skipped + " already there" : ""), true);
          return;
        }
        clearInterval(exportTimer); exportTimer = null;
        if (p.error) { setStatus("export failed: " + p.error, true); return; }
        var written = p.done - p.failed - (p.skipped || 0);
        var msg = "exported " + written + " of " + p.total + " to " + p.path;
        if (p.skipped) msg += ", " + p.skipped + " already there";
        if (p.failed) msg += " (" + p.failed + " failed, see failed.txt)";
        setStatus(msg, true);
      }).catch(function () {
        fails += 1;
        if (fails < EXPORT_POLL_MAX_FAILS) return;     // one dropped poll is not a lost server
        clearInterval(exportTimer); exportTimer = null;
        setStatus("lost contact with the server; check the terminal", true);
      });
    }, 800);
  }
  function startExport(ids, name, mode, label) {
    return api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: ids, name: name, mode: mode }),
    }).then(function () { pollExportProgress(label); })
      .catch(function (err) { setStatus("export failed: " + err.message, true); });
  }

  $("#export").addEventListener("click", function () {
    var ids = Array.from(state.selected);
    if (!ids.length) { setStatus("select some photos first"); return; }
    if (exportDest() === "drive") {                    // ===== Google Drive: the selection goes up as <name>/ =====
      startDriveExport({ what: "selection", ids: ids, name: $("#exportname").value.trim() || "selection", include_raw: $("#drive-include-raw").checked },
                       "uploading " + ids.length + " photo(s)");
      return;
    }
    var name = $("#exportname").value.trim() || "export";
    var mode = $("#exportmode").value;
    startExport(ids, name, mode, "exporting " + ids.length + " photo(s)");
  });

  // ---------- export destination: this Mac, or a Google Drive folder ----------
  // ===== Google Drive =====
  // One choice for the three export rows (selection, saved people, ticked categories), remembered in
  // localStorage. In Drive mode the row's copy/link picker hides (an upload is always a copy) and one
  // strip node moves under the row that is showing: the folder link, what the server found behind it,
  // the account, the web-size choice. Export then posts /api/drive/export and the shared progress
  // poller reads the job as an upload. Nothing here runs a sign-in on its own.
  var DEST_KEY = "sorted.export.dest";
  var driveStrip = $("#drive-strip");
  var driveLink = $("#drive-link");
  var driveFolderEl = $("#drive-folder");
  var driveAccount = $("#drive-account");
  var driveWebSize = $("#drive-web-size");
  var driveRawLabel = $("#drive-raw-label");
  var driveMsg = $("#drive-msg");
  var driveStatus = null;                              // the last /api/drive/status
  var driveStatusTimer = null;                         // the 1.5 s poll while a sign-in is open
  var driveInspected = null;                           // the link the folder line describes
  var driveInspectSeq = 0;
  var drivePrefilled = false;
  var driveBars = { search: $("#selbar"), people: $("#people-export-row"), categories: $("#cat-export-row") };

  function exportDest() {
    var v = null;
    try { v = localStorage.getItem(DEST_KEY); } catch (e) { /* private mode */ }
    return v === "drive" ? "drive" : "mac";
  }
  function setExportDest(v) {
    try { localStorage.setItem(DEST_KEY, v); } catch (e) { /* private mode */ }
    renderDest();
  }
  function renderDest() {
    var dest = exportDest();
    var drive = dest === "drive";
    $$(".seg.dest input").forEach(function (r) { r.checked = r.value === dest; });
    // the copy/link picker has no meaning for an upload; trimmed segments cannot go to Drive
    [$("#exportmode"), $("#people-export-mode"), $("#cat-export-mode")].forEach(function (sel) { sel.hidden = drive; });
    var segOpt = $("#cat-videos option[value=segments]");
    segOpt.disabled = drive;
    if (drive && $("#cat-videos").value === "segments") $("#cat-videos").value = "clips";
    $("#export").title = drive ? "upload the selection to the Drive folder" : "export the selection";
    $("#people-export-refs").title = drive ? "upload everyone ticked above to the Drive folder" : "export everyone ticked above";
    $("#cat-export-all").title = drive ? "upload every category ticked above to the Drive folder" : "export every category ticked above";
    mountDriveStrip();
    if (drive && !driveStatus) loadDriveStatus();
  }
  $$(".seg.dest input").forEach(function (r) {
    r.addEventListener("change", function () { if (r.checked) setExportDest(r.value); });
  });

  function mountDriveStrip() {
    var bar = driveBars[state.view];
    if (exportDest() !== "drive" || !bar) { driveStrip.hidden = true; return; }
    if (driveStrip.parentNode !== bar) {
      bar.appendChild(driveStrip);
      // a finished upload or a refusal belonged to the row it came from; a running one follows the strip
      if (driveMsg.dataset.kind === "done" || driveMsg.dataset.kind === "error") { driveMsg.dataset.kind = ""; setDriveMsg(""); }
    }
    driveRawLabel.hidden = state.view !== "search";   // the people and categories rows carry their own RAW tick
    driveStrip.hidden = false;
  }

  function gb(bytes) { return (bytes / 1e9).toFixed(1) + " GB"; }

  // ----- the account line: not configured, signed out, signing in, signed in -----
  function setDriveMsg(text, err) {
    driveMsg.textContent = text || "";
    driveMsg.classList.toggle("err", !!err);
    driveMsg.hidden = !text;
  }
  function renderDriveAccount() {
    var st = driveStatus;
    driveAccount.innerHTML = "";
    if (!st) return;
    var setupMsg = driveMsg.dataset.kind === "setup";
    if (!st.configured && !st.signed_in) {
      driveMsg.innerHTML = "";
      driveMsg.dataset.kind = "setup";
      driveMsg.classList.remove("err");
      driveMsg.appendChild(document.createTextNode("Put the Google client file at "));
      var code = document.createElement("code");
      code.className = "drive-path";
      code.textContent = st.client_path;
      driveMsg.appendChild(code);
      driveMsg.appendChild(document.createTextNode(" first. How: docs/google-drive.md"));
      driveMsg.hidden = false;
      return;
    }
    if (setupMsg) { driveMsg.dataset.kind = ""; setDriveMsg(""); }
    if (st.signing_in) {
      var wait = document.createElement("span");
      wait.className = "drive-wait";
      wait.textContent = "Finish the sign-in in your browser…";
      driveAccount.appendChild(wait);
      return;
    }
    if (st.signed_in) {
      var who = document.createElement("span");
      who.textContent = st.email || "signed in";
      who.title = "the Google account the files go up from";
      driveAccount.appendChild(who);
      var out = document.createElement("button");
      out.type = "button"; out.className = "link"; out.textContent = "Sign out";
      out.title = "forget the token on this Mac";
      out.addEventListener("click", driveSignOut);
      driveAccount.appendChild(out);
      return;
    }
    if (st.error) {
      var er = document.createElement("span");
      er.className = "err"; er.textContent = st.error;
      driveAccount.appendChild(er);
    }
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "btn"; btn.textContent = "Sign in to Google";
    btn.title = "opens Google's consent page in your browser";
    btn.addEventListener("click", driveSignIn);
    driveAccount.appendChild(btn);
  }
  function applyDriveStatus(st) {
    var wasSigningIn = driveStatus && driveStatus.signing_in;
    driveStatus = st;
    if (!drivePrefilled) {                             // the last link and web size, once; the fields are the user's after that
      drivePrefilled = true;
      if (st.link && !driveLink.value) driveLink.value = st.link;
      if (st.web_size != null) {
        var v = String(st.web_size);
        if (!driveWebSize.querySelector('option[value="' + v + '"]')) {
          var o = document.createElement("option"); o.value = v; o.textContent = v + " px";
          driveWebSize.appendChild(o);
        }
        driveWebSize.value = v;
      }
    }
    renderDriveAccount();
    if (st.signing_in) {
      if (!driveStatusTimer) driveStatusTimer = setTimeout(function () { driveStatusTimer = null; loadDriveStatus(); }, 1500);
    } else if (wasSigningIn && st.signed_in) {
      setStatus("signed in to Google as " + (st.email || "you"));
      driveInspected = null;                           // a link checked while signed out only got "sign in first"
    }
    if (st.signed_in && driveLink.value.trim() && driveInspected !== driveLink.value.trim()) inspectDriveLink(true);
  }
  function loadDriveStatus() {
    return api("/api/drive/status").then(applyDriveStatus).catch(function (err) {
      driveAccount.innerHTML = "";
      var er = document.createElement("span"); er.className = "err"; er.textContent = "could not read the Drive status: " + err.message;
      driveAccount.appendChild(er);
    });
  }
  function driveSignIn() {
    setDriveMsg("");
    api("/api/drive/signin", { method: "POST" }).then(function () {
      applyDriveStatus(Object.assign({}, driveStatus || {}, { signing_in: true, error: null }));
    }).catch(function (err) { setDriveMsg(err.message, true); });
  }
  function driveSignOut() {
    api("/api/drive/signout", { method: "POST" }).then(function () {
      driveInspected = null; driveFolderEl.textContent = ""; driveFolderEl.classList.remove("err");
      setDriveMsg("");
      loadDriveStatus();
    }).catch(function (err) { setDriveMsg(err.message, true); });
  }

  // ----- the folder behind the link: name, owner, whether our space is what counts -----
  function inspectDriveLink(quiet) {
    var link = driveLink.value.trim();
    if (!link) { driveInspected = null; driveFolderEl.textContent = ""; driveFolderEl.classList.remove("err"); return; }
    if (link === driveInspected) return;
    driveInspected = link;
    var seq = ++driveInspectSeq;
    driveFolderEl.classList.remove("err");
    driveFolderEl.textContent = "checking…"; driveFolderEl.title = "";
    api("/api/drive/inspect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ link: link }),
    }).then(function (info) {
      if (seq !== driveInspectSeq) return;
      driveFolderEl.innerHTML = "";
      var nm = document.createElement("span"); nm.className = "drive-name"; nm.textContent = info.name;
      driveFolderEl.appendChild(nm);
      var parts = [];
      if (info.owner_email) parts.push(info.owner_email);
      if (info.shared_drive) parts.push("Shared Drive");
      else if (info.free_bytes != null) parts.push(gb(info.free_bytes) + " free in your Drive");
      if (parts.length) driveFolderEl.appendChild(document.createTextNode(" · " + parts.join(" · ")));
      driveFolderEl.title = info.name + (info.owner_email ? ", owned by " + info.owner_email : "");
      if (info.link && info.link !== link) { driveLink.value = info.link; driveInspected = info.link; }
    }).catch(function (err) {
      if (seq !== driveInspectSeq) return;
      driveInspected = null;                           // Enter tries again
      driveFolderEl.classList.add("err");
      driveFolderEl.textContent = err.message; driveFolderEl.title = err.message;
      if (err.status === 401 && !quiet) loadDriveStatus();
    });
  }
  driveLink.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); inspectDriveLink(); driveLink.blur(); } });
  driveLink.addEventListener("blur", function () { inspectDriveLink(); });
  driveLink.addEventListener("input", function () { if (driveMsg.dataset.kind === "error") setDriveMsg(""); });

  // ----- the upload: the shared export job, read as an upload by the poller -----
  function startDriveExport(extra, label) {
    var link = driveLink.value.trim();
    if (!link) { setDriveMsg("paste a Google Drive folder link first", true); driveMsg.dataset.kind = "error"; driveLink.focus(); return; }
    var body = Object.assign({ link: link, web_size: driveWebSize.value ? Number(driveWebSize.value) : null,
                               skip_videos: $("#drive-skip-videos").checked }, extra);
    setDriveMsg(""); driveMsg.dataset.kind = "";
    setStatus(label + "…", true);
    api("/api/drive/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) {
      renderDriveProgress({ done: 0, total: res.total || 0, bytes: 0, current: null });
      pollExportProgress(label);
    }).catch(function (err) {
      setStatus("upload failed: " + err.message, true);
      setDriveMsg(err.message, true); driveMsg.dataset.kind = "error";
      if (err.status === 401) loadDriveStatus();        // a dead token reads as signed out: offer the sign-in again
    });
  }
  function renderDriveProgress(p) {
    var line = "uploading " + p.done + " of " + p.total + (p.current ? ", " + p.current : "") + ", " + gb(p.bytes || 0)
             + (p.failed ? ", " + p.failed + " failed" : "") + (p.skipped ? ", " + p.skipped + " already there" : "");
    setStatus(line, true);
    driveMsg.innerHTML = "";
    driveMsg.dataset.kind = "progress";
    driveMsg.classList.remove("err");
    driveMsg.appendChild(document.createTextNode("Uploading " + p.done + " of " + p.total + (p.current ? ", " + p.current : "") + " · " + gb(p.bytes || 0)));
    var bar = document.createElement("span");
    bar.className = "drive-bar";
    bar.style.setProperty("--p", p.total ? p.done / p.total : 0);
    driveMsg.appendChild(bar);
    driveMsg.hidden = false;
  }
  function renderDriveDone(p) {
    driveMsg.innerHTML = "";
    driveMsg.dataset.kind = "done";
    driveMsg.classList.remove("err");
    driveMsg.hidden = false;
    if (p.error) {
      setStatus("upload failed: " + p.error, true);
      driveMsg.classList.add("err");
      driveMsg.textContent = p.error;
      if (/sign in/i.test(p.error)) loadDriveStatus();
      return;
    }
    var sent = p.done - (p.failed || 0) - (p.skipped || 0);
    var text = "Uploaded " + sent + " of " + p.total + ", " + gb(p.bytes || 0);
    if (p.skipped) text += ", " + p.skipped + " already there";
    text += ". ";
    driveMsg.appendChild(document.createTextNode(text));
    if (p.path) {
      var a = document.createElement("a");
      a.href = p.path; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open in Drive";
      driveMsg.appendChild(a);
    }
    var status = "uploaded " + sent + " of " + p.total + " to Google Drive, " + gb(p.bytes || 0);
    if (p.skipped) status += ", " + p.skipped + " already there";
    if (p.failed) {
      status += " (" + p.failed + " failed)";
      var f = document.createElement("p");
      f.className = "drive-failed";
      f.textContent = p.failed + " file" + (p.failed === 1 ? "" : "s") + " did not go up:";
      driveMsg.appendChild(f);
      var list = document.createElement("pre");
      list.className = "drive-failures";
      list.textContent = (p.failures || []).map(function (line) {
        var i = line.indexOf("\t");
        return i < 0 ? line : line.slice(0, i) + "  " + line.slice(i + 1);
      }).join("\n");
      driveMsg.appendChild(list);
    }
    setStatus(status, true);
  }
  renderDest();
  // ===== end Google Drive =====

  // ---------- lightbox ----------
  var lightbox = $("#lightbox");
  var lbImg = $("#lb-img");
  var lbVideo = $("#lb-video");
  var lbMeta = $("#lb-meta");
  var lbSegments = $("#lb-segments");
  var lbLike = $("#lb-like");
  var currentLb = null;

  // A video plays from the original file; under it, one frame per scene, click to seek there.
  function seekVideo(t) {
    var go = function () { try { lbVideo.currentTime = t; } catch (e) { /* not seekable yet */ } };
    if (lbVideo.readyState >= 1) go();
    else lbVideo.addEventListener("loadedmetadata", go, { once: true });
  }

  function renderSegments(segs) {
    lbSegments.innerHTML = "";
    lbSegments.hidden = !segs.length;
    segs.forEach(function (s) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "segment mono";
      var img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "scene " + (s.idx + 1);
      img.src = s.frame_url;
      b.appendChild(img);
      var label = document.createElement("span");
      label.className = "seg-label";
      label.textContent = mmss(s.start) + " to " + mmss(s.end) + ", " + (s.category || "unclassified");
      b.appendChild(label);
      b.addEventListener("click", function () {
        $$(".segment", lbSegments).forEach(function (x) { x.classList.toggle("on", x === b); });
        seekVideo(s.start);
      });
      lbSegments.appendChild(b);
    });
  }

  function stopVideo() {
    try { lbVideo.pause(); } catch (e) { /* nothing playing */ }
    lbVideo.removeAttribute("src");
    lbVideo.load();
    lbVideo.hidden = true;
    lbSegments.hidden = true;
    lbSegments.innerHTML = "";
  }

  function openLightbox(r) {
    currentLb = r;
    var isVideo = r.kind === "video";
    stopVideo();
    lbImg.hidden = isVideo;
    if (isVideo) {
      lbVideo.hidden = false;
      lbVideo.src = "/api/media/" + r.id;
      lbImg.removeAttribute("src");
    } else {
      lbImg.src = "/api/thumb/" + r.qhash + "?size=full";
    }
    var sharpPct = r.sharp_pct != null ? Math.round(r.sharp_pct) : 0;
    var bits = [r.rel, r.width + "×" + r.height];
    if (isVideo) bits.push(mmss(r.duration));
    bits.push("sharp " + sharpPct + "%");
    if (!isVideo) bits.push(facesLabel(r.n_faces) + " faces");
    if (r.camera) bits.push(r.camera);
    if (r.taken_at) bits.push(r.taken_at);
    // ===== lightbox strip: the file name in mono, the rest in the UI face =====
    lbMeta.textContent = "";
    var fn = document.createElement("span");
    fn.className = "filename";
    fn.textContent = bits[0];
    lbMeta.appendChild(fn);
    lbMeta.appendChild(document.createTextNode("  ·  " + bits.slice(1).join("  ·  ")));
    // ===== end lightbox strip =====
    lightbox.hidden = false;
    if (isVideo) {
      api("/api/segments/" + r.id).then(function (data) {
        if (currentLb !== r) return;                       // another item opened meanwhile
        renderSegments((data && data.segments) || []);
      }).catch(function () { /* the player still works without the strip */ });
    }
  }
  function closeLightbox() {
    lightbox.hidden = true;
    currentLb = null;
    stopVideo();
  }
  $("#lb-close").addEventListener("click", closeLightbox);
  lightbox.addEventListener("click", function (e) {
    if (e.target === lightbox) closeLightbox();
  });
  // ===== keyboard: Esc, arrows in the lightbox, / or command-F to search, command-A, space, grid arrows =====
  function stepLightbox(d) {
    if (!currentLb) return;
    var i = indexOfId(currentLb.id);
    var next = i >= 0 ? state.results[i + d] : null;
    if (next) openLightbox(next);
  }
  document.addEventListener("keydown", function (e) {
    var t = e.target;
    var tag = t && t.tagName;
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    var qField = form.querySelector('[name="q"]');
    var cmd = e.metaKey || e.ctrlKey;
    if (!lightbox.hidden) {
      if (e.key === "Escape") { e.preventDefault(); closeLightbox(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); stepLightbox(1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); stepLightbox(-1); }
      return;
    }
    // ===== help panel: Esc closes it before anything else reads the key =====
    if (e.key === "Escape" && !helpEl.hidden) { e.preventDefault(); setHelp(false); return; }
    // ===== end help panel =====
    if (e.key === "Escape" && t === qField) {
      if (qField.value) { qField.value = ""; runSearch(); } else qField.blur();
      return;
    }
    // ===== navigation keys: command-1 to command-4 switch tabs from anywhere; Esc on a filtered Search goes back =====
    if (cmd && !e.shiftKey && !e.altKey && /^[1-4]$/.test(e.key) && state.folder && state.folder.root) {
      e.preventDefault();
      goView(VIEWS[Number(e.key) - 1]);
      return;
    }
    if (e.key === "Escape" && !typing && state.view === "search" && state.ctx) { e.preventDefault(); navBack(); return; }
    // ===== end navigation keys =====
    if (typing && !(cmd && e.key.toLowerCase() === "f")) return;
    if (e.key === "/" || (cmd && e.key.toLowerCase() === "f")) {
      e.preventDefault();
      if (state.folder && state.folder.root) goView("search");   // ===== navigation: a tab switch, so a history entry =====
      qField.focus(); qField.select();
      return;
    }
    if (state.view !== "search") return;
    if (cmd && e.key.toLowerCase() === "a") { e.preventDefault(); selectAllShown(); return; }
    if (e.key === " ") {
      var id = hoveredId != null ? hoveredId : (document.activeElement && document.activeElement.classList.contains("card") ? Number(document.activeElement.dataset.id) : null);
      var c = id != null ? cardEl(id) : null;
      if (c) { e.preventDefault(); toggleSelect(id, c); }
      return;
    }
    if (e.key === "Enter" && document.activeElement && document.activeElement.classList.contains("card")) {
      var rr = resultById.get(Number(document.activeElement.dataset.id));
      if (rr) { e.preventDefault(); openLightbox(rr); }
      return;
    }
    if (/^Arrow(Left|Right|Up|Down)$/.test(e.key) && gridEl.contains(document.activeElement)) { e.preventDefault(); moveFocus(e.key); }
  });
  // ===== end keyboard =====
  lbLike.addEventListener("click", function () {
    if (!currentLb) return;
    var id = currentLb.id;
    closeLightbox();
    showView("search");
    form.querySelector('[name="q"]').value = "";
    runSearch({ image_id: id });
    setStatus("showing photos similar to that one");
  });

  // ===== panels: a header click collapses the section, remembered per panel in localStorage =====
  function panelKey(p) { return "sorted.panel." + (p.dataset.panel || ""); }
  function applyPanelState(p) {
    var saved = null;
    try { saved = localStorage.getItem(panelKey(p)); } catch (e) { /* private mode */ }
    // ===== panel default: a panel marked "collapsed" in the HTML starts closed until the user opens it =====
    var open = saved == null ? !p.classList.contains("collapsed") : saved !== "0";
    // ===== end panel default =====
    p.classList.toggle("collapsed", !open);
    var h = p.querySelector(".panel-head");
    if (h) h.setAttribute("aria-expanded", open ? "true" : "false");
  }
  $$(".panel[data-panel]").forEach(applyPanelState);
  document.addEventListener("click", function (e) {
    var h = e.target.closest ? e.target.closest(".panel-head") : null;
    if (!h) return;
    var p = h.closest(".panel");
    var open = p.classList.toggle("collapsed") === false;
    h.setAttribute("aria-expanded", open ? "true" : "false");
    try { localStorage.setItem(panelKey(p), open ? "1" : "0"); } catch (e2) { /* private mode */ }
  });
  function makePanel(key, title) {
    var p = document.createElement("div");
    p.className = "panel";
    p.dataset.panel = key;
    var h = document.createElement("button");
    h.type = "button"; h.className = "panel-head";
    h.innerHTML = '<svg class="disclosure" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1.5L6.5 5 3 8.5"/></svg>';
    h.appendChild(document.createTextNode(title));
    var b = document.createElement("div");
    b.className = "panel-body";
    p.appendChild(h); p.appendChild(b);
    applyPanelState(p);
    return { el: p, body: b };
  }
  // ===== end panels =====

  // ===== inspector: the hovered item, else the single selection, else the multi-selection summary =====
  var inspBody = $("#insp-body");
  var inspToggle = $("#inspector-toggle");
  var INSP_KEY = "sorted.inspector";
  function inspRow(k, v, cls) {
    var row = document.createElement("div"); row.className = "insp-row";
    var kk = document.createElement("span"); kk.className = "k"; kk.textContent = k;
    var vv = document.createElement("span"); vv.className = "v" + (cls ? " " + cls : ""); vv.textContent = v;
    row.appendChild(kk); row.appendChild(vv);
    return row;
  }
  function renderInspector() {
    if (!inspBody || !document.body.classList.contains("insp")) return;
    inspBody.innerHTML = "";
    var n = state.selected.size;
    var r = hoveredId != null ? resultById.get(hoveredId) : null;
    if (!r && n === 1) r = resultById.get(state.selected.values().next().value);
    var info = makePanel("insp-info", "Info");
    var cats = makePanel("insp-cats", "Categories");
    var faces = makePanel("insp-faces", "Faces");
    inspBody.appendChild(info.el); inspBody.appendChild(cats.el); inspBody.appendChild(faces.el);
    if (!r && n > 1) {
      var head = document.createElement("div"); head.className = "insp-title"; head.textContent = n + " items selected";
      info.body.appendChild(head);
      var byCat = {}, photos = 0, videos = 0, withFaces = 0;
      state.selected.forEach(function (id) {
        var x = resultById.get(id);
        if (!x) return;
        if (x.kind === "video") videos += 1; else photos += 1;
        if (x.n_faces) withFaces += 1;
        var c = x.category || "unclassified";
        byCat[c] = (byCat[c] || 0) + 1;
      });
      if (photos || videos) info.body.appendChild(inspRow("Kind", (photos ? photos + " photos" : "") + (photos && videos ? ", " : "") + (videos ? videos + " clips" : "")));
      Object.keys(byCat).sort(function (a, b) { return byCat[b] - byCat[a]; }).forEach(function (c) { cats.body.appendChild(inspRow(c, String(byCat[c]))); });
      faces.body.appendChild(inspRow("With faces", String(withFaces)));
      return;
    }
    if (!r) {
      var p = document.createElement("p"); p.className = "insp-empty"; p.textContent = "Select a photo or clip";
      info.body.appendChild(p);
      cats.el.hidden = true; faces.el.hidden = true;
      return;
    }
    var img = document.createElement("img");
    img.className = "insp-preview";
    img.alt = "";
    img.src = "/api/thumb/" + r.qhash + "?size=full";
    info.body.appendChild(img);
    var name = document.createElement("div"); name.className = "insp-name"; name.textContent = r.rel; name.title = r.rel;
    info.body.appendChild(name);
    if (r.taken_at) info.body.appendChild(inspRow("Taken", r.taken_at.replace("T", " ")));
    if (r.camera) info.body.appendChild(inspRow("Camera", r.camera));
    if (r.width && r.height) info.body.appendChild(inspRow("Size", r.width + " × " + r.height));
    if (r.kind === "video") info.body.appendChild(inspRow("Duration", mmss(r.duration)));
    var sharpPct = r.sharp_pct != null ? Math.round(r.sharp_pct) : 0;
    var sharpRow = inspRow("Sharpness", sharpPct + "%");
    var bar = document.createElement("span"); bar.className = "insp-bar"; bar.style.setProperty("--p", sharpPct / 100);
    sharpRow.appendChild(bar);
    info.body.appendChild(sharpRow);
    // ===== inspector focus label: ok / soft / bad once the focus check has scored this row =====
    if (r.focus) info.body.appendChild(inspRow("Focus", r.focus === "bad" ? "bad, out of focus" : r.focus));
    // ===== end inspector focus label =====
    cats.body.appendChild(inspRow("Category", r.category ? r.category + (r.category_score != null ? "  " + Math.round(r.category_score * 100) + "%" : "") : "unclassified"));
    if (r.cluster) cats.body.appendChild(inspRow("Discovered", /^group \d+$/.test(r.cluster) ? "Unnamed " + r.cluster : r.cluster));
    if (r.sure === false) cats.body.appendChild(inspRow("Confidence", Math.round((r.confidence || 0) * 100) + "%, less sure"));
    cats.body.appendChild(inspRow("Aerial", r.aerial ? "yes" : "no"));
    faces.body.appendChild(inspRow("Faces", r.kind === "video" ? "not scanned in clips" : facesLabel(r.n_faces)));
  }
  function setInspector(open) {
    if (open !== document.body.classList.contains("insp")) track("inspector", { open: open });   // ===== usage log =====
    document.body.classList.toggle("insp", open);
    if (inspToggle) inspToggle.setAttribute("aria-pressed", open ? "true" : "false");
    try { localStorage.setItem(INSP_KEY, open ? "1" : "0"); } catch (e) { /* private mode */ }
    renderInspector();
  }
  if (inspToggle) {
    var inspSaved = null;
    try { inspSaved = localStorage.getItem(INSP_KEY); } catch (e) { /* private mode */ }
    setInspector(inspSaved == null ? window.innerWidth >= 1400 : inspSaved === "1");
    inspToggle.addEventListener("click", function () { setInspector(!document.body.classList.contains("insp")); });
  }
  // ===== end inspector =====

  // ---------- people ----------
  var peopleEl = $("#people");
  var personSelect = $("#person-select");

  function loadPeople() {
    return api("/api/people").then(function (list) {
      state.people = list;
      renderPeople();
      fillPersonSelect();
      return list;
    });
  }

  function renderPeople() {
    peopleEl.innerHTML = "";
    state.people.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "card person";

      if (p.cover_qhash) {
        var img = document.createElement("img");
        img.loading = "lazy";
        img.alt = p.name || ("person " + p.id);
        img.src = "/api/thumb/" + p.cover_qhash + "?size=full";
        img.addEventListener("load", function () { cropToFace(img, p.cover_box); }, { once: true });
        card.appendChild(img);
      }

      var tag = document.createElement("div");
      tag.className = "tag mono";
      tag.textContent = p.n + " photo" + (p.n === 1 ? "" : "s");
      card.appendChild(tag);

      var nameWrap = document.createElement("div");
      nameWrap.className = "pname";
      var nameInput = document.createElement("input");
      nameInput.value = p.name || "";
      nameInput.placeholder = "person_" + String(p.id).padStart(2, "0");
      nameInput.className = "mono";
      nameInput.title = "name this person";           // ===== navigation: the field keeps its own tooltip, the row's says it navigates =====
      nameInput.addEventListener("click", function (e) { e.stopPropagation(); });
      nameInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); nameInput.blur(); }   // blur does the save, once
      });
      nameInput.addEventListener("blur", function () {
        var val = nameInput.value.trim();
        if (val === (p.name || "")) return;
        api("/api/people/" + p.id + "/name", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: val }),
        }).then(function () {
          p.name = val;
          fillPersonSelect();
          setStatus("named person " + p.id + ": " + (val || "(cleared)"));
        }).catch(function (err) {
          setStatus("could not save name: " + err.message);
        });
      });
      nameWrap.appendChild(nameInput);
      card.appendChild(nameWrap);

      // ===== navigation: the row is the way in; the name field keeps its own click (stopPropagation above) =====
      card.title = "show every photo of " + personLabel(p, p.id);
      card.addEventListener("click", function () {
        jumpTo({ from: "people", kind: "person", key: p.id }, function () { runSearch({ person: p.id }); });
      });
      // ===== end navigation =====

      peopleEl.appendChild(card);
    });
    settleScroll("people");                            // ===== navigation: a return to the tab lands where it was =====
  }

  // Draw the face box (padded 1.6x, clamped to the image) into a square canvas with
  // object-fit: cover semantics, then swap the img to that crop. cover_box is in the
  // same pixel space as the full-size thumb the img loaded.
  var COVER_EDGE = 320;
  function cropToFace(img, box) {
    box = box || [0, 0, 0, 0];
    var W = img.naturalWidth, H = img.naturalHeight;
    if (!W || !H || !box[2] || !box[3]) return;
    var pad = 1.6;
    var cx = box[0] + box[2] / 2, cy = box[1] + box[3] / 2;
    var side = Math.max(box[2], box[3]) * pad;
    side = Math.min(side, W, H);
    var sx = Math.min(Math.max(cx - side / 2, 0), W - side);
    var sy = Math.min(Math.max(cy - side / 2, 0), H - side);
    try {
      var canvas = document.createElement("canvas");
      canvas.width = COVER_EDGE; canvas.height = COVER_EDGE;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(img, sx, sy, side, side, 0, 0, COVER_EDGE, COVER_EDGE);
      img.src = canvas.toDataURL("image/jpeg", 0.85);
    } catch (e) {
      /* canvas unavailable or tainted: keep the plain thumb */
    }
  }

  function fillPersonSelect() {
    var current = personSelect.value;
    personSelect.innerHTML = '<option value="">anyone</option>';
    state.people.forEach(function (p) {
      var opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = (p.name || "person_" + String(p.id).padStart(2, "0")) + " (" + p.n + ")";
      personSelect.appendChild(opt);
    });
    personSelect.value = current || "";
    // ===== navigation: a person context set before the options existed (reload, history) takes the select now; names reach the crumb =====
    if (state.ctx && state.ctx.kind === "person" && !personSelect.value) personSelect.value = String(state.ctx.key);
    renderCtxBar();
    // ===== end navigation =====
  }

  $("#cluster").addEventListener("click", function () {
    // An untouched control sends no eps, so the server's own default (config.FACE_CLUSTER_EPS) applies.
    var epsEl = $("#eps");
    var body = {};
    if (epsEl.value !== epsEl.defaultValue && parseFloat(epsEl.value) > 0) body.eps = parseFloat(epsEl.value);
    setStatus("grouping faces…", true);
    api("/api/people/cluster", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (list) {
      state.people = list;
      renderPeople();
      fillPersonSelect();
      setStatus("found " + list.length + " group(s)");
      loadStats();
      loadSuggestions();                               // the lookalike pairs change with every re-grouping
    }).catch(function (err) {
      setStatus("grouping failed: " + err.message);
    });
  });

  $("#export-people").addEventListener("click", function () {
    setStatus("exporting people, groups and solo shots…", true);
    api("/api/export/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "symlink" }),
    }).then(function (res) {
      setStatus("exported links to " + res.path);
    }).catch(function (err) {
      setStatus("export failed: " + err.message);
    });
  });

  // ===== same person? (merge suggestions) =====
  // GET /api/people/suggestions lists pairs of face groups that look alike; "Same" merges them
  // (POST /api/people/merge, keeping the named or larger side), "Different" hides the pair for good
  // (POST /api/people/reject). Both answers are remembered by face id across re-groupings. An older
  // server without the endpoint 404s: the block simply stays hidden. Twenty rows at a time.
  var mergeBlock = $("#merge-block");
  var mergeList = $("#merge-list");
  var mergeEmpty = $("#merge-empty");
  var mergeMore = $("#merge-more");
  var MERGE_PAGE = 20;
  var mergeQueue = [];
  var mergeShown = 0;

  function faceImg(f, alt) {
    var img = document.createElement("img");
    img.loading = "lazy";
    img.alt = alt;
    img.src = "/api/thumb/" + f.qhash + "?size=full";
    img.addEventListener("load", function () { cropToFace(img, f.box); }, { once: true });
    return img;
  }

  function mergeSide(cover, faces, name, n) {
    var side = document.createElement("div");
    side.className = "merge-side";
    var row = document.createElement("div");
    row.className = "merge-faces";
    [cover].concat(faces || []).slice(0, 4).forEach(function (f) {
      if (f && f.qhash) row.appendChild(faceImg(f, name || "face"));
    });
    side.appendChild(row);
    var label = document.createElement("div");
    label.className = "merge-name mono";
    label.textContent = name || ("group of " + n);
    side.appendChild(label);
    return side;
  }

  function renderSuggestion(s) {
    var row = document.createElement("div");
    row.className = "merge-row";
    row.appendChild(mergeSide(s.a_cover, s.a_faces, s.a_name, s.a_n));
    var sep = document.createElement("span");
    sep.className = "merge-sep";
    row.appendChild(sep);
    row.appendChild(mergeSide(s.b_cover, s.b_faces, s.b_name, s.b_n));
    var sim = document.createElement("span");
    sim.className = "merge-sim mono";
    sim.textContent = Math.round((s.sim || 0) * 100) + "% alike";
    row.appendChild(sim);
    var same = document.createElement("button");
    same.type = "button"; same.className = "btn"; same.textContent = "Same"; same.title = "merge these two groups into one person";
    var diff = document.createElement("button");
    diff.type = "button"; diff.className = "btn"; diff.textContent = "Different"; diff.title = "keep them apart and stop asking";
    function answer(path, body, msg) {
      same.disabled = true; diff.disabled = true;
      api(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function () {
        row.remove();
        syncMergeEmpty();
        setStatus(msg);
        loadPeople();                                  // groups and the anyone select follow the answer
      }).catch(function (err) {
        same.disabled = false; diff.disabled = false;
        setStatus("could not save that answer: " + err.message);
      });
    }
    same.addEventListener("click", function () {
      // keep the named side; with no name on either, the larger group
      var keepA = s.a_name ? true : (s.b_name ? false : (s.a_n || 0) >= (s.b_n || 0));
      answer("/api/people/merge", { keep: keepA ? s.a : s.b, drop: keepA ? s.b : s.a }, "merged into one person");
    });
    diff.addEventListener("click", function () {
      answer("/api/people/reject", { a: s.a, b: s.b }, "kept as two people");
    });
    row.appendChild(same);
    row.appendChild(diff);
    return row;
  }

  function syncMergeEmpty() {
    var left = mergeQueue.length - mergeShown;
    mergeEmpty.hidden = mergeList.children.length > 0 || left > 0;
    mergeMore.hidden = left <= 0;
    mergeMore.textContent = "Show " + Math.min(left, MERGE_PAGE) + " more";
  }

  function showMoreSuggestions() {
    mergeQueue.slice(mergeShown, mergeShown + MERGE_PAGE).forEach(function (s) { mergeList.appendChild(renderSuggestion(s)); });
    mergeShown = Math.min(mergeQueue.length, mergeShown + MERGE_PAGE);
    syncMergeEmpty();
  }
  mergeMore.addEventListener("click", showMoreSuggestions);

  function loadSuggestions() {
    if (!(state.folder && state.folder.root)) { mergeBlock.hidden = true; return Promise.resolve(); }
    return api("/api/people/suggestions").then(function (data) {
      mergeQueue = (data && data.suggestions) || [];
      mergeShown = 0;
      mergeList.innerHTML = "";
      mergeBlock.hidden = false;
      showMoreSuggestions();
    }).catch(function () {
      mergeBlock.hidden = true;                        // no endpoint on this server, or indexing: nothing to answer yet
    });
  }
  // ===== end same person? =====

  // find a person from a reference photo
  var findSim = $("#find-sim");
  var findSimOut = $("#find-sim-out");
  var FIND_SIM_DEFAULT = findSim.value;

  function showFindResults(data, who) {
    // Not a paged search: every match is already here, so the "show more" and
    // "select all matching" affordances are hidden after renderGrid() re-shows them.
    // who: "that person" for a picked photo, or a saved name (that payload has no reference keys).
    state.results = data.results || [];
    state.total = data.total || 0;
    state.offset = state.results.length;
    state.lastParams = {};
    showView("search");
    renderGrid();
    $("#show-more").hidden = true;
    $("#select-matching").hidden = true;
    $("#shown-count").textContent = state.results.length + " shown";
    if ("faces_in_reference" in data && !data.faces_in_reference) {
      setStatus("no face found in that photo");
      return;
    }
    if (data.reference_face_too_small) {
      setStatus("the face in that photo is too small to match, pick a closer shot");
      return;
    }
    setStatus("found " + state.total + " photo(s) of " + (who || "that person") + (data.person_id ? ", person " + data.person_id : ""));
  }

  $("#find-person").addEventListener("click", function () {
    setStatus("waiting for the photo picker…", true);
    fetch("/api/people/find/choose", { method: "POST" }).then(function (r) {
      if (r.status === 204) { setStatus("photo pick cancelled"); return null; }
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          throw new Error((body && body.detail) || (r.status + " " + r.statusText));
        });
      }
      return r.json();
    }).then(function (data) {
      if (!data) return;
      // Only a usable face can be re-matched or saved under a name.
      var usable = !!(data.path && data.faces_in_reference && !data.reference_face_too_small);
      state.findPath = usable ? data.path : null;
      syncSaveForm();
      // A fresh pick matches at the default threshold, so the slider shows that too.
      findSim.value = FIND_SIM_DEFAULT; findSimOut.value = FIND_SIM_DEFAULT; findSim.dispatchEvent(new Event("input"));
      showFindResults(data);
    }).catch(function (err) {
      setStatus("could not find that person: " + err.message);
    });
  });

  bindSliderField(findSim, findSimOut, function () { findSim.dispatchEvent(new Event("change")); });
  findSim.addEventListener("change", function () {
    if (state.savedPeople.length) loadReferences();    // counts follow the slider
    if (!state.findPath) return;
    setStatus("matching at " + findSim.value + "…", true);
    api("/api/people/find", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.findPath, min_sim: parseFloat(findSim.value) }),
    }).then(function (data) { showFindResults(data); }).catch(function (err) {
      setStatus("could not find that person: " + err.message);
    });
  });

  // ---------- named people (saved reference photos) ----------
  var savePersonForm = $("#save-person-form");
  var savePersonName = $("#save-person-name");
  var savedPeopleEl = $("#saved-people");
  var peopleExportRow = $("#people-export-row");

  function syncSaveForm() {
    savePersonForm.hidden = !state.findPath;
  }

  function savePerson() {
    var name = savePersonName.value.trim();
    if (!state.findPath) { setStatus("find a person from a photo first"); return; }
    if (!name) { setStatus("type a name for this person"); savePersonName.focus(); return; }
    setStatus("saving " + name + "…", true);
    api("/api/people/references", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, path: state.findPath }),
    }).then(function (res) {
      savePersonName.value = "";
      state.peopleUnticked.delete(res.name);
      setStatus("saved " + res.name + "; the People tab lists everyone saved");
      return loadReferences();
    }).catch(function (err) {
      setStatus("could not save that person: " + err.message);
    });
  }
  $("#save-person").addEventListener("click", savePerson);
  savePersonName.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); savePerson(); }
  });

  function loadReferences() {
    if (!(state.folder && state.folder.root)) return Promise.resolve([]);
    var qs = new URLSearchParams({ min_sim: findSim.value }).toString();
    return api("/api/people/references?" + qs).then(function (data) {
      state.savedPeople = (data && data.people) || [];
      renderSavedPeople();
      return state.savedPeople;
    }).catch(function (err) {
      setStatus("could not load saved people: " + err.message);
    });
  }

  function findSaved(name) {
    setStatus("matching " + name + " at " + findSim.value + "…", true);
    api("/api/people/references/" + encodeURIComponent(name) + "/find", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_sim: parseFloat(findSim.value) }),
    }).then(function (data) {
      showFindResults(data, name);
      // ===== navigation: the crumb reads "People / name / N photos" once the matches are in =====
      if (state.ctx && state.ctx.kind === "saved" && state.ctx.key === name) renderCtxBar();
      // ===== end navigation =====
    }).catch(function (err) {
      setStatus("could not show " + name + ": " + err.message);
    });
  }
  // ===== navigation: a saved row lands on Search as "People / name" =====
  function jumpToSaved(name) {
    jumpTo({ from: "people", kind: "saved", key: name }, function () { findSaved(name); });
  }
  // ===== end navigation =====

  function renameSaved(p, newName) {
    return api("/api/people/references/" + encodeURIComponent(p.name) + "/rename", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName }),
    }).then(function () {
      if (state.peopleUnticked.has(p.name)) { state.peopleUnticked.delete(p.name); state.peopleUnticked.add(newName); }
      setStatus("renamed " + p.name + " to " + newName);
      return loadReferences();
    }).catch(function (err) {
      setStatus("could not rename: " + err.message);
      return loadReferences();
    });
  }

  function removeSaved(p) {
    setStatus("removing " + p.name + "…", true);
    return Promise.all(p.reference_ids.map(function (id) {
      return api("/api/people/references/" + id, { method: "DELETE" });
    })).then(function () {
      state.peopleUnticked.delete(p.name);
      setStatus("removed " + p.name);
      return loadReferences();
    }).catch(function (err) {
      setStatus("could not remove " + p.name + ": " + err.message);
      return loadReferences();
    });
  }

  function renderSavedPeople() {
    savedPeopleEl.innerHTML = "";
    peopleExportRow.hidden = !state.savedPeople.length;
    if (!state.savedPeople.length) {
      var empty = document.createElement("p");
      empty.className = "mono";
      empty.textContent = "no saved people yet: Find a person from a photo, then name them in the status line";
      savedPeopleEl.appendChild(empty);
      return;
    }
    state.savedPeople.forEach(function (p) {
      var row = document.createElement("div");
      row.className = "ref-row";

      var tickLabel = document.createElement("label");
      tickLabel.title = "include in Export ticked people";
      var tick = document.createElement("input");
      tick.type = "checkbox";
      tick.className = "ref-tick";
      tick.value = p.name;
      tick.checked = !state.peopleUnticked.has(p.name);
      tick.addEventListener("change", function () {
        if (tick.checked) state.peopleUnticked.delete(p.name); else state.peopleUnticked.add(p.name);
      });
      tickLabel.appendChild(tick);
      row.appendChild(tickLabel);

      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "mono ref-name";
      nameInput.value = p.name;
      nameInput.title = p.reference_ids.length + " reference photo" + (p.reference_ids.length === 1 ? "" : "s") + ": " + p.sources.join(", ");
      nameInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); nameInput.blur(); }   // blur does the save, once
      });
      nameInput.addEventListener("blur", function () {
        var val = nameInput.value.trim();
        if (!val) { nameInput.value = p.name; return; }
        if (val === p.name) return;
        renameSaved(p, val);
      });
      row.appendChild(nameInput);

      var count = document.createElement("span");
      count.className = "mono ref-count";
      count.textContent = p.count + " photo" + (p.count === 1 ? "" : "s");
      row.appendChild(count);

      // ===== navigation: Show is the row chevron at the right edge; the whole row navigates except its controls =====
      var show = document.createElement("button");
      show.type = "button";
      show.className = "ref-show";
      show.textContent = "Show";
      show.title = "show every photo of " + p.name;
      show.addEventListener("click", function () { jumpToSaved(p.name); });
      row.title = "show every photo of " + p.name;
      row.addEventListener("click", function (e) {
        if (e.target.closest("input, label, button")) return;   // the tick, the name field, the remove button keep their own clicks
        jumpToSaved(p.name);
      });
      // ===== end navigation =====

      // Two clicks to remove, no browser dialog: the first arms the button, the second deletes.
      // A second click that lands within 400ms of the arming one is a double-click, not a
      // deliberate confirm, so it is ignored rather than treated as the delete.
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ref-remove";                 // ===== navigation: styled by class now that Show sits after it =====
      remove.textContent = "x";
      remove.title = "remove " + p.name + " (two clicks)";
      var armTimer = null;
      var armedAt = 0;
      function disarm() {
        if (armTimer) { clearTimeout(armTimer); armTimer = null; }
        remove.classList.remove("armed");
        remove.textContent = "x";
      }
      remove.addEventListener("click", function () {
        if (!remove.classList.contains("armed")) {
          remove.classList.add("armed");
          remove.textContent = "really remove?";
          armedAt = Date.now();
          armTimer = setTimeout(disarm, 5000);
          return;
        }
        if (Date.now() - armedAt <= 400) return;   // a double-click landed as the confirm, not a real one
        disarm();
        removeSaved(p);
      });
      remove.addEventListener("blur", disarm);
      row.appendChild(remove);
      row.appendChild(show);                           // ===== navigation: the chevron is the last thing on the row =====

      savedPeopleEl.appendChild(row);
    });
  }

  $("#people-export-refs").addEventListener("click", function () {
    var names = $$(".ref-tick", savedPeopleEl).filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
    if (!names.length) { setStatus("tick at least one person"); return; }
    var mode = $("#people-export-mode").value;
    var includeRaw = $("#people-include-raw").checked;
    if (exportDest() === "drive") {                    // ===== Google Drive: saved people go up as people/<name>/ =====
      startDriveExport({ what: "people", names: names, include_raw: includeRaw, min_sim: parseFloat(findSim.value) },
                       "uploading " + names.length + " " + (names.length === 1 ? "person" : "people"));
      return;
    }
    setStatus("exporting " + names.length + " " + (names.length === 1 ? "person" : "people") + "…", true);
    api("/api/export/references", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: names, mode: mode, include_raw: includeRaw, min_sim: parseFloat(findSim.value) }),
    }).then(function () { pollExportProgress("exporting people"); })
      .catch(function (err) { setStatus("export failed: " + err.message, true); });
  });

  // ---------- categories ----------
  // Two rows: the fixed CATEGORIES (a filter on photos.category) and the ones discovered in this shoot
  // (k-means clusters named from the vocabulary, a filter on photos.cluster). A tile key is the fixed
  // name, or "discovered:" + name, so the tick state of the two rows never collides. The fixed row ends
  // with a "drone" tile when the shoot has any: a flag across categories (photos.aerial), key "drone".
  var catTilesEl = $("#cat-tiles");
  var discTilesEl = $("#disc-tiles");
  var catProgressEl = $("#cat-progress");
  var categoryChip = $("#category-chip");

  function showCategoryChip(text) {
    if (!text) { categoryChip.hidden = true; return; }
    $("#category-chip-name").textContent = text;
    categoryChip.hidden = false;
  }
  $("#category-chip-clear").addEventListener("click", function () {
    $("#category-filter").value = ""; $("#cluster-filter").value = ""; aerialOnly.checked = false;
    showCategoryChip(null);
    // ===== navigation: the chip is hidden while a context shows, but clearing it ends a Categories context all the same =====
    if (state.ctx && state.ctx.from === "categories") { state.ctx = null; renderCtxBar(); navPush(); }
    // ===== end navigation =====
    runSearch();
  });

  // One of the filters at a time from a tile: a fixed category, a discovered one (cluster), or the drone flag.
  // ===== navigation: each lands on Search through jumpTo, which writes the fields, the chip, the crumb and a history entry =====
  function filterByCategory(cat, cluster) {
    jumpTo({ from: "categories", kind: cluster ? "cluster" : "category", key: cat }, function () { runSearch(); });
  }

  function filterByDrone() {
    jumpTo({ from: "categories", kind: "drone", key: "drone" }, function () { runSearch(); });
  }
  // ===== end navigation =====

  function loadCategories() {
    return api("/api/categories").then(function (counts) {
      state.categories = { fixed: (counts && counts.fixed) || {}, discovered: (counts && counts.discovered) || {},
                           drone: (counts && counts.drone) || 0 };
      renderCategoryTiles();
    }).catch(function (err) {
      setStatus("could not load categories: " + err.message);
    });
  }

  var catExportRow = $("#cat-export-row");

  // drone: the one tile that is not a category; its tick box has its own class so the export reads it as
  // the drone flag rather than a category name.
  function makeTile(cat, count, cluster, drone, max) {
    var key = drone ? "drone" : (cluster ? "discovered:" + cat : cat);
    var tile = document.createElement("div");
    tile.className = "cat-tile" + (drone ? " drone" : "");
    tile.style.setProperty("--p", max ? Math.min(1, count / max) : 0);   // the thin bar under the name

    var tick = document.createElement("label");
    tick.className = "cat-tile-tick";
    tick.title = "include in Export ticked categories";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = drone ? "drone-tick" : (cluster ? "disc-tick" : "cat-tick");
    box.value = cat;
    if (!state.catSeen.has(key)) {                 // first sight: every category but "unclassified" starts ticked;
      state.catSeen.add(key);                      // the drone tile does not, it would double every aerial row
      if (cat !== "unclassified" && !drone) state.catTicked.add(key);
    }
    box.checked = state.catTicked.has(key);
    box.addEventListener("change", function () {
      if (box.checked) state.catTicked.add(key); else state.catTicked.delete(key);
    });
    tick.appendChild(box);
    tile.appendChild(tick);

    var label = document.createElement("button");
    label.type = "button";
    label.className = "cat-tile-main mono";
    // ===== table row: name, count, a 60 px bar; an unnamed discovered group gets a rename pencil =====
    var unnamed = !!cluster && /^group \d+$/.test(cat);
    var nameEl = document.createElement("span"); nameEl.className = "cat-name" + (unnamed ? " unnamed" : "");
    nameEl.textContent = unnamed ? "Unnamed " + cat : cat;
    var countEl = document.createElement("span"); countEl.className = "cat-count"; countEl.textContent = count;
    var barEl = document.createElement("span"); barEl.className = "cat-bar";
    label.appendChild(nameEl); label.appendChild(countEl); label.appendChild(barEl);
    label.title = "show " + cat + " in the grid";
    label.addEventListener("click", function () { if (drone) filterByDrone(); else filterByCategory(cat, cluster); });
    tile.appendChild(label);

    var tools = document.createElement("span");         // a fixed slot so rows with and without a pencil line up
    tools.className = "cat-tools";
    if (unnamed) {
      var pen = document.createElement("button");
      pen.type = "button";
      pen.className = "cat-rename-btn";
      pen.title = "name this group";
      pen.setAttribute("aria-label", "name this group");
      pen.addEventListener("click", function () { startRename(tile, cat, label); });
      tools.appendChild(pen);
    }
    tile.appendChild(tools);
    var exportBtn = document.createElement("button");
    exportBtn.type = "button";
    exportBtn.className = "cat-export";
    exportBtn.textContent = "Export links";
    exportBtn.title = "export links to every " + cat + " file";
    exportBtn.addEventListener("click", function () { exportCategory(cat, cluster, drone); });
    tile.appendChild(exportBtn);
    // ===== navigation: the row chevron (a CSS pseudo-element) and any bare part of the row open the category too =====
    tile.addEventListener("click", function (e) {
      if (e.target.closest("input, label, button")) return;   // the tick, the pencil, a rename field and Export links keep their own clicks
      if (drone) filterByDrone(); else filterByCategory(cat, cluster);
    });
    // ===== end navigation =====
    return tile;
  }

  // The pencil swaps the row's label for a text field; Enter or blur saves, Escape cancels.
  function startRename(tile, cat, label) {
    if (tile.classList.contains("renaming")) return;
    tile.classList.add("renaming");
    var input = document.createElement("input");
    input.type = "text";
    input.className = "cat-rename";
    input.value = cat;
    input.title = "a name for this group";
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var val = input.value.trim();
      input.remove();
      tile.classList.remove("renaming");
      if (!save || !val || val === cat) return;
      setStatus("renaming " + cat + "…", true);
      api("/api/categories/discovered/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old: cat, new: val }),
      }).then(function () {
        setStatus("renamed " + cat + " to " + val + "; names given here are lost when you Categorise again", true);
        loadCategories();
      }).catch(function (err) {
        setStatus("could not rename " + cat + ": " + err.message, true);
      });
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    label.insertAdjacentElement("afterend", input);
    input.focus(); input.select();
  }
  // ===== end table row =====

  function renderTileRow(el, counts, cluster, emptyText) {
    el.innerHTML = "";
    var names = Object.keys(counts);
    if (!names.length) {
      var p = document.createElement("p");
      p.className = "mono";
      p.textContent = emptyText;
      el.appendChild(p);
      return 0;
    }
    var max = Math.max.apply(null, names.map(function (n) { return counts[n] || 0; }));
    names.forEach(function (cat) { el.appendChild(makeTile(cat, counts[cat], cluster, false, max)); });
    return names.length;
  }

  function renderCategoryTiles() {
    var nFixed = renderTileRow(catTilesEl, state.categories.fixed, false, "no categories yet, run Categorise");
    var nDrone = state.categories.drone || 0;
    if (nDrone) {                                   // last tile of the fixed row, hidden when the shoot has none;
      catTilesEl.appendChild(makeTile("drone", nDrone, false, true, nDrone));   // the "run Categorise" hint stays next to it
    }
    var nDisc = renderTileRow(discTilesEl, state.categories.discovered, true, "no discovered categories yet, run Categorise");
    catExportRow.hidden = !(nFixed || nDisc || nDrone);
    $("#nav-cat-count").textContent = (nFixed + nDisc) || "";                 // the count beside the Categories nav row
    // ===== navigation: counts reach the crumb; a return to the tab lands where it was =====
    renderCtxBar();
    settleScroll("categories");
    // ===== end navigation =====
  }

  function exportCategory(cat, cluster, drone) {
    setStatus("gathering " + cat + " photos…", true);
    var params = drone ? { aerial: 1 } : (cluster ? { cluster: cat } : { category: cat });
    if (!drone && !$("#cat-include-unsure").checked) params.sure_only = 1;
    var qs = new URLSearchParams(params).toString();
    return api("/api/search/ids?" + qs).then(function (data) {
      var ids = data.ids || [];
      if (!ids.length) { setStatus("no photos in " + cat); return null; }
      setStatus("exporting " + ids.length + " " + cat + " photo(s)…", true);
      return startExport(ids, "categories/" + (cluster ? "discovered/" : "") + cat, "symlink", "exporting " + cat);
    }).catch(function (err) {
      setStatus("export failed: " + err.message);
    });
  }

  $("#cat-export-all").addEventListener("click", function () {
    var ticked = function (sel) { return $$(sel).filter(function (b) { return b.checked; }).map(function (b) { return b.value; }); };
    var cats = ticked(".cat-tick");
    var disc = ticked(".disc-tick");
    var drone = ticked(".drone-tick").length > 0;
    var n = cats.length + disc.length + (drone ? 1 : 0);
    if (!n) { setStatus("tick at least one category"); return; }
    var mode = $("#cat-export-mode").value;
    var includeRaw = $("#cat-include-raw").checked;
    var includeUnsure = $("#cat-include-unsure").checked;
    var videos = $("#cat-videos").value;
    if (exportDest() === "drive") {                    // ===== Google Drive: ticked categories go up as categories/<name>/ =====
      startDriveExport({ what: "categories", categories: cats, discovered: disc, include_raw: includeRaw,
                         include_unsure: includeUnsure, videos: videos, drone: drone },
                       "uploading " + n + " categor" + (n === 1 ? "y" : "ies"));
      return;
    }
    setStatus("exporting " + n + " categor" + (n === 1 ? "y" : "ies") + "…", true);
    api("/api/export/categories", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categories: cats, discovered: disc, mode: mode, include_raw: includeRaw,
                             include_unsure: includeUnsure, videos: videos, drone: drone }),
    }).then(function () { pollExportProgress("exporting categories"); })
      .catch(function (err) { setStatus("export failed: " + err.message, true); });
  });

  function stopClassifyPoll() {
    if (state.classifyTimer) {
      clearInterval(state.classifyTimer);
      state.classifyTimer = null;
    }
  }

  function pollClassifyProgress() {
    stopClassifyPoll();
    state.classifyTimer = setInterval(function () {
      api("/api/classify/progress").then(function (p) {
        catProgressEl.textContent = p.running ? "categorising…" : "";
        if (!p.running) {
          stopClassifyPoll();
          loadCategories();           // both rows, fixed and discovered, from the same endpoint the tab opens with
          setStatus(p.error ? ("categorising failed: " + p.error) : "categorising finished");
        }
      }).catch(function () { stopClassifyPoll(); });
    }, 800);
  }

  $("#categorise").addEventListener("click", function () {
    api("/api/classify", { method: "POST" }).then(function () {
      setStatus("categorising…", true);
      catProgressEl.textContent = "categorising…";
      pollClassifyProgress();
    }).catch(function (err) {
      if (err.status === 409) {
        setStatus("already categorising");
        pollClassifyProgress();
      } else if (err.status === 501) {
        setStatus("categorisation isn't available yet");
      } else {
        setStatus("could not start categorising: " + err.message);
      }
    });
  });

  // ---------- index ----------
  var progressEl = $("#progress");

  function stopProgressPoll() {
    if (state.progressTimer) {
      clearInterval(state.progressTimer);
      state.progressTimer = null;
    }
  }

  function formatProgress(p) {
    if (p.stage === "error") return "indexing failed: " + (p.error || "unknown error");
    var done = p.done || 0, total = p.total || 0;
    var line = p.stage + "  " + done + "/" + total;
    if (p.running && p.stage_started && done > 0 && total > done) {
      var elapsed = Date.now() / 1000 - p.stage_started;
      var rate = done / Math.max(elapsed, 0.001);
      var eta = (total - done) / rate;
      line += "  " + rate.toFixed(1) + "/s, about " + (eta < 90 ? Math.round(eta) + " s" : Math.round(eta / 60) + " min") + " left";
    } else if (p.running) {
      line += "  (running)";
    }
    return line;
  }

  function pollProgress() {
    stopProgressPoll();
    state.progressTimer = setInterval(function () {
      api("/api/progress").then(function (p) {
        progressEl.textContent = formatProgress(p);
        progressEl.style.setProperty("--p", p.total ? (p.done || 0) / p.total : 0);   // the 4 px bar under the readout
        if (!p.running) {
          stopProgressPoll();
          if (p.stage === "error") {
            setStatus("indexing failed: " + (p.error || "unknown error"), true);
          } else {
            setStatus("indexing finished");
          }
          // the index changed under us: refresh everything that shows it
          loadStats();
          loadPeople();
          runSearch();
        }
      }).catch(function () { stopProgressPoll(); });
    }, 800);
  }

  // ===== index job starter: shared by "Index this folder" and the two "detect faces now" controls =====
  function startIndexJob(body, startMsg) {
    return api("/api/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function () {
      setStatus(startMsg, true);
      progressEl.textContent = "starting…";
      pollProgress();
      loadStats();
    }).catch(function (err) {
      if (err.status === 409) {
        setStatus(err.message || "already indexing");
        pollProgress();
      } else {
        setStatus("could not start indexing: " + err.message);
      }
    });
  }
  $("#start-index").addEventListener("click", function () {
    startIndexJob({ faces: $("#faces").checked, retry_errors: $("#retry-errors").checked }, "indexing started…");
  });
  // ===== end index job starter =====

  // index bundles: pack the index into one zip, or install one picked from disk
  // A POST to a picker endpoint: null on 204 (cancelled), the JSON body otherwise, an Error on failure.
  function pickerPost(path, body) {
    var opts = { method: "POST" };
    if (body) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      if (r.status === 204) return null;
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var err = new Error((b && b.detail) || (r.status + " " + r.statusText));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    });
  }

  $("#bundle-export").addEventListener("click", function () {
    setStatus("packing the index…", true);
    api("/api/bundle/export", { method: "POST" }).then(function () { pollExportProgress("packing index"); })
      .catch(function (err) { setStatus("could not pack the index: " + err.message, true); });
  });

  function importBundle() {
    setStatus("waiting for the bundle picker…", true);
    pickerPost("/api/bundle/import/choose").then(function (res) {
      if (!res) { setStatus("import cancelled"); return null; }
      if (!res.needs_root) return res;
      // Made on a Mac where the disk sat under another path: ask for the folder, then install under it.
      setStatus("that bundle was made for " + res.bundle.root + ", which is not here; pick the photo folder", true);
      return pickerPost("/api/bundle/import/choose-root", { zip: res.zip }).then(function (r2) {
        if (!r2) { setStatus("import cancelled, nothing was installed"); return null; }
        return r2;
      });
    }).then(function (info) {
      if (!info) return;
      applyFolderInfo(info);
      setStatus("imported " + (info.name || info.root) + ": " + info.photos + " photos, ready", true);
      settleFolder(info);
    }).catch(function (err) {
      setStatus("could not import the bundle: " + err.message, true);
    });
  }
  $("#bundle-import").addEventListener("click", importBundle);
  $("#bundle-import-main").addEventListener("click", importBundle);

  // ===== help: the "How it works" slide-over (420 px from the right, over the inspector) =====
  // Opens from the toolbar button; Esc (keyboard block above), the close button and a click outside close it.
  // Its sections are ordinary .panel blocks, so the panel click handler collapses them and remembers each.
  var helpEl = $("#help");
  var helpToggle = $("#help-toggle");
  var helpClose = $("#help-close");
  function setHelp(open) {
    if (open && helpEl.hidden) track("help_open");     // ===== usage log =====
    helpEl.hidden = !open;
    helpToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) helpClose.focus();
    else if (document.activeElement && helpEl.contains(document.activeElement)) helpToggle.focus();
  }
  helpToggle.addEventListener("click", function () { setHelp(helpEl.hidden); });
  helpClose.addEventListener("click", function () { setHelp(false); });
  document.addEventListener("click", function (e) {
    if (helpEl.hidden) return;
    if (helpEl.contains(e.target) || helpToggle.contains(e.target)) return;
    setHelp(false);
  });
  // ===== end help =====

  // ===== feedback: the usage log's report and summary, at the foot of the slide-over (docs/usage-log.md) =====
  // POST /api/usage/report writes ~/Desktop/sorted-report-<date>.zip and replies with its path; GET
  // /api/usage/summary carries the human-readable text the Copy button puts on the clipboard.
  var fbSave = $("#fb-save");
  var fbCopy = $("#fb-copy");
  var fbOut = $("#fb-out");
  function fbSay(msg, isErr) {
    fbOut.textContent = msg || ""; fbOut.hidden = !msg; fbOut.classList.toggle("err", !!isErr);
    if (msg && fbOut.scrollIntoView) fbOut.scrollIntoView({ block: "nearest" });
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.top = "-1000px";
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error("copy blocked"));
    });
  }
  if (fbSave) fbSave.addEventListener("click", function () {
    fbSave.disabled = true;
    flushUsage();
    api("/api/usage/report", { method: "POST" }).then(function (res) {
      fbSay("Saved " + res.path);
    }).catch(function (err) {
      fbSay("could not save the report: " + err.message, true);
    }).then(function () { fbSave.disabled = false; });
  });
  if (fbCopy) fbCopy.addEventListener("click", function () {
    fbCopy.disabled = true;
    flushUsage();
    api("/api/usage/summary").then(function (s) {
      return copyText(s.text || JSON.stringify(s, null, 2)).then(function () { fbSay("Summary copied, paste it into the email"); });
    }).catch(function (err) {
      fbSay("could not copy the summary: " + err.message, true);
    }).then(function () { fbCopy.disabled = false; });
  });
  // ===== end feedback =====

  // ===== focus: the status line under the filters, "Check focus" and its poll =====
  // GET /api/focus/status gives {checked, unchecked, bad, soft}; POST /api/focus scores every unchecked row
  // once (photos from the index row, clips from their stored frames) and GET /api/focus/progress follows it.
  // Both reads are skipped on a server without the endpoints (state.caps.focus, set from /api/stats).
  var focusStatusEl = $("#focus-status");
  var checkFocusBtn = $("#check-focus");
  var focusTimer = null;
  function renderFocusLine(st) {
    var checked = (st && st.checked) || 0;
    var total = checked + ((st && st.unchecked) || 0);
    focusStatusEl.textContent = checked ? checked + " of " + total + " checked" : "focus not checked";
    focusStatusEl.title = checked ? (st.bad || 0) + " out of focus, " + (st.soft || 0) + " soft" : "how much of the shoot has been through the focus check";
    checkFocusBtn.hidden = !!(checked && total && checked >= total);   // nothing left to check
  }
  function loadFocusStatus() {
    if (!state.caps.focus || !(state.folder && state.folder.root)) return Promise.resolve();
    return api("/api/focus/status").then(renderFocusLine).catch(function () { /* the line keeps its last text */ });
  }
  function stopFocusPoll() { if (focusTimer) { clearInterval(focusTimer); focusTimer = null; } }
  function pollFocusProgress() {
    stopFocusPoll();
    focusTimer = setInterval(function () {
      api("/api/focus/progress").then(function (p) {
        if (p.running) { setStatus("checking focus " + (p.done || 0) + " of " + (p.total || 0), true); return; }
        stopFocusPoll();
        checkFocusBtn.disabled = false;
        if (p.error) { setStatus("focus check failed: " + p.error, true); return; }
        var c = p.counts || {};
        setStatus("checked focus on " + (c.checked || 0) + ": " + (c.bad || 0) + " out of focus, " + (c.soft || 0) + " soft", true);
        loadFocusStatus();
        loadStats();
        runSearch();                                   // the hide boxes may now drop rows
      }).catch(function () { stopFocusPoll(); checkFocusBtn.disabled = false; });
    }, 800);
  }
  checkFocusBtn.addEventListener("click", function () {
    if (!(state.folder && state.folder.root)) { setStatus("open a shoot first"); return; }
    checkFocusBtn.disabled = true;
    api("/api/focus", { method: "POST" }).then(function (res) {
      setStatus("checking focus 0 of " + (res.total || 0), true);
      pollFocusProgress();
    }).catch(function (err) {
      if (err.status === 409 && /already/.test(err.message)) { setStatus("already checking focus"); pollFocusProgress(); return; }
      checkFocusBtn.disabled = false;
      setStatus("could not check focus: " + err.message);
    });
  });
  // ===== end focus =====

  // ===== optional faces: "N need faces" in the title, "Detect faces now" on Index, the People tab link =====
  // /api/stats.faces_pending counts rows indexed with faces off. POST /api/index {faces: true} runs faces
  // only on those rows (the server skips everything else), with the usual index progress.
  var detectFacesNow = $("#detect-faces-now");
  var facesPendingRow = $("#faces-pending-row");
  function renderFacesPending(s) {
    var n = (s && s.faces_pending) || 0;
    state.facesPending = n;
    detectFacesNow.hidden = !(n > 0 && !(s && s.indexing));
    facesPendingRow.hidden = !(n > 0);
    $("#faces-pending-text").textContent = n + (n === 1 ? " photo" : " photos") + " not scanned for faces yet,";
  }
  function scanFacesNow() {
    startIndexJob({ faces: true }, "scanning " + state.facesPending + " photo(s) for faces…");
    goView("index");                                   // ===== navigation: a tab switch, so a history entry =====
  }
  detectFacesNow.addEventListener("click", scanFacesNow);
  $("#faces-pending-scan").addEventListener("click", scanFacesNow);
  // ===== end optional faces =====

  // ===== reorganise disk: plan, confirm by typing the folder name, apply, poll, undo =====
  // The one flow that writes to the shoot disk. POST /api/reorganise/plan lists the moves (a 400 carries a
  // guard message, shown verbatim); POST /api/reorganise/apply {plan_id, confirm} runs as the export job, so
  // /api/export/progress is polled with "moving N of M"; GET /api/reorganise/status says whether sorted/
  // exists and POST /api/reorganise/undo puts everything back ("restoring N of M").
  var reorgSetup = $("#reorg-setup");
  var reorgByPeople = $("#reorg-by-people");
  var reorgAck = $("#reorg-ack");
  var reorgPreview = $("#reorg-preview");
  var reorgGuard = $("#reorg-guard");
  var reorgPlan = $("#reorg-plan");
  var reorgSummary = $("#reorg-summary");
  var reorgSample = $("#reorg-sample tbody");
  var reorgConfirm = $("#reorg-confirm");
  var reorgApply = $("#reorg-apply");
  var reorgStatus = $("#reorg-status");
  var reorgStatusText = $("#reorg-status-text");
  var reorgUndo = $("#reorg-undo");
  var reorgPlanId = null;

  function showReorgGuard(msg) { reorgGuard.textContent = msg || ""; reorgGuard.hidden = !msg; }
  function syncReorgApply() { reorgApply.disabled = !(reorgPlanId && reorgConfirm.value === (state.folder.name || "")); }
  function resetReorganise() {
    reorgPlanId = null;
    reorgPlan.hidden = true; reorgSample.innerHTML = ""; reorgSummary.textContent = "";
    reorgConfirm.value = ""; reorgConfirm.placeholder = ""; reorgApply.disabled = true;
    reorgAck.checked = false; reorgByPeople.checked = false; reorgPreview.disabled = true;
    showReorgGuard(null);
    reorgStatus.hidden = true; reorgSetup.hidden = false; reorgUndo.disabled = false;
  }
  function fmtWhen(iso) {
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return iso || "";
    try { return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return d.toLocaleString(); }
  }
  function renderReorganiseStatus(st) {
    var done = !!(st && st.reorganised);
    reorgSetup.hidden = done;
    reorgStatus.hidden = !done;
    if (done) {
      reorgPlanId = null; reorgPlan.hidden = true;
      reorgStatusText.textContent = "reorganised on " + fmtWhen(st.created) + ", " + st.moves + " file" + (st.moves === 1 ? "" : "s");
    }
  }
  function loadReorganiseStatus() {
    if (!state.caps.focus || !(state.folder && state.folder.root)) return Promise.resolve();
    return api("/api/reorganise/status").then(renderReorganiseStatus).catch(function () { /* section keeps its state */ });
  }
  function renderReorganisePlan(plan) {
    reorgPlanId = plan.plan_id;
    var people = Object.keys(plan.people || {}).map(function (n) { return n + " " + plan.people[n]; });
    reorgSummary.textContent = "Will move " + plan.moves + " file" + (plan.moves === 1 ? "" : "s") + " into " + plan.folders + " folder" + (plan.folders === 1 ? "" : "s") +
      ": " + plan.photos + " photos, " + plan.videos + " videos, " + plan.drone + " drone" +
      (people.length ? "; people: " + people.join(", ") : "") +
      "; " + plan.collisions + " name collision" + (plan.collisions === 1 ? "" : "s");
    reorgSample.innerHTML = "";
    (plan.sample || []).forEach(function (m) {
      var tr = document.createElement("tr");
      var a = document.createElement("td"); a.className = "filename"; a.textContent = m.src_rel; a.title = m.reason || "";
      var arrow = document.createElement("td"); arrow.className = "arrow"; arrow.textContent = "→";
      var b = document.createElement("td"); b.className = "filename"; b.textContent = m.dst_rel + (m.also && m.also.length ? "  (also " + m.also.join(", ") + ")" : "");
      tr.appendChild(a); tr.appendChild(arrow); tr.appendChild(b);
      reorgSample.appendChild(tr);
    });
    reorgConfirm.value = "";
    reorgConfirm.placeholder = state.folder.name || "";
    reorgPlan.hidden = false;
    syncReorgApply();
  }
  reorgAck.addEventListener("change", function () { reorgPreview.disabled = !reorgAck.checked; });
  reorgByPeople.addEventListener("change", function () { reorgPlanId = null; reorgPlan.hidden = true; syncReorgApply(); });   // a plan is for one setting
  reorgConfirm.addEventListener("input", syncReorgApply);
  reorgPreview.addEventListener("click", function () {
    showReorgGuard(null); reorgPlan.hidden = true; reorgPlanId = null;
    reorgPreview.disabled = true; reorgPreview.textContent = "Planning…";
    api("/api/reorganise/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ by_people: reorgByPeople.checked }),
    }).then(renderReorganisePlan).catch(function (err) {
      showReorgGuard(err.message);
    }).then(function () {
      reorgPreview.textContent = "Preview";
      reorgPreview.disabled = !reorgAck.checked;
    });
  });
  // Apply and undo ride the export job, so the export poller's timer is shared: two pollers never overlap.
  function pollDiskJob(verb, onDone) {
    if (exportTimer) clearInterval(exportTimer);
    var fails = 0;
    exportTimer = setInterval(function () {
      api("/api/export/progress").then(function (p) {
        fails = 0;
        if (p.running) { setStatus(verb + " " + (p.done || 0) + " of " + (p.total || 0) + (p.failed ? ", " + p.failed + " failed" : ""), true); return; }
        clearInterval(exportTimer); exportTimer = null;
        if (p.error) setStatus("could not " + (verb === "moving" ? "reorganise" : "undo") + ": " + p.error, true);
        else if (verb === "moving") setStatus("moved " + ((p.done || 0) - (p.failed || 0)) + " of " + (p.total || 0) + " files into " + (p.path || "sorted/") + (p.failed ? " (" + p.failed + " failed, left in place)" : ""), true);
        else setStatus("restored " + ((p.done || 0) - (p.failed || 0)) + " of " + (p.total || 0) + " files" + (p.failed ? " (" + p.failed + " could not go back)" : ""), true);
        onDone();
      }).catch(function () {
        fails += 1;
        if (fails < EXPORT_POLL_MAX_FAILS) return;
        clearInterval(exportTimer); exportTimer = null;
        setStatus("lost contact with the server; check the terminal", true);
      });
    }, 800);
  }
  function afterDiskJob() {
    loadFolder();                                      // the folder info, then everything that shows paths
    loadStats();
    loadCategories();
    loadReorganiseStatus();
    runSearch();
  }
  reorgApply.addEventListener("click", function () {
    if (!reorgPlanId || reorgConfirm.value !== (state.folder.name || "")) return;
    reorgApply.disabled = true;
    api("/api/reorganise/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: reorgPlanId, confirm: reorgConfirm.value }),
    }).then(function (res) {
      setStatus("moving 0 of " + (res.total || 0), true);
      reorgPlanId = null;                              // the server pops the plan it ran
      pollDiskJob("moving", afterDiskJob);
    }).catch(function (err) {
      showReorgGuard(err.message);
      syncReorgApply();
    });
  });
  reorgUndo.addEventListener("click", function () {
    reorgUndo.disabled = true;
    api("/api/reorganise/undo", { method: "POST" }).then(function (res) {
      setStatus("restoring 0 of " + (res.total || 0), true);
      pollDiskJob("restoring", function () {
        reorgUndo.disabled = false;
        reorgAck.checked = false; reorgPreview.disabled = true;   // a second run earns Preview again
        afterDiskJob();
      });
    }).catch(function (err) {
      reorgUndo.disabled = false;
      setStatus("could not undo: " + err.message, true);
    });
  });
  // ===== end reorganise disk =====

  // ---------- boot ----------
  loadExportDest();
  loadFolder().then(function (info) {
    loadRecent();
    if (!info.root) {
      showView(state.view);
      return;
    }
    // ===== navigation boot: the view and filter come back from history.state (survives a reload) or the hash =====
    var entry = navBootEntry();
    if (entry) state.view = entry.view;
    var ctx = entry ? entry.ctx : null;
    // ===== end navigation boot =====
    if (!info.indexed) {
      showView("index");
      navPush(true);                                   // ===== navigation: the boot entry is a replace, never a push =====
      var btn = $("#start-index");
      if (btn) btn.focus();
      loadStats().then(function (s) { if (s.indexing) pollProgress(); });   // first index of a fresh folder, page reloaded mid-run
      return;
    }
    loadStats().then(function (s) { if (s.indexing) pollProgress(); });
    loadPeople();
    loadCategories();                                  // fills the count beside the Categories nav row
    // ===== navigation boot: a person filter goes out explicitly, the select has no options yet; a saved name is a find =====
    setCtx(ctx);
    if (ctx && ctx.kind === "saved") { if (state.view === "search") findSaved(ctx.key); else pendingFind = ctx.key; }
    else runSearch(ctxParams(ctx));
    showView(state.view);
    navPush(true);
    // ===== end navigation boot =====
  });
})();
