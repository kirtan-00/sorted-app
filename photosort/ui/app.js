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
    // ===== resume: how far the scan got, from /api/stats.scan or /api/folder.scan (null before the first read) =====
    scan: null,                 // {items, scanned, embedded, pending, unembedded, complete, faces, interrupted: {kind, stage, done, total} | null}
    // ===== end resume =====
    // ===== navigation: where the Search filter came from, and each list's scroll position =====
    // ===== combined filters: every active filter is a chip; they AND together (a saved-person find stays on its own) =====
    chips: [],                  // ordered [{kind, key, from}]: kind person|saved|category|cluster|drone|kind|hide_bad|hide_soft, from "people"|"categories"|null
    shootTotal: 0,              // photos + videos from /api/stats, the M in "31 of 777 match"
    // ===== end combined filters =====
    searches: { recent: [], saved: [] },   // ===== search history: /api/searches, per shoot =====
    showCopies: false,          // ===== duplicates and bursts: false folds each set into one tile (fold=1), true shows every copy and frame =====
    folded: 0,                  // how many rows the folded tiles are hiding, from the last search
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
    var el = t && t.closest ? t.closest("button, a, .card, .cat-tile, label.check, .seg label, .panel-head, .ctx-chip, [data-view]") : null;
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
    var hasFolder = folderOpen(state.folder);
    $$("main > section").forEach(function (s) {
      if (s.id === "view-nofolder") { s.hidden = hasFolder; return; }
      s.hidden = !hasFolder || s.id !== "view-" + name;
    });
    setWelcomeFrame(hasFolder);                        // ===== welcome: the sidebar and toolbar groups dim and go inert =====
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
    if (name === "index") loadScanHealth();            // ===== resume: the health line walks the disk when the tab opens =====
    mountDriveStrip();                                 // ===== Google Drive: the strip sits under whichever export row is showing =====
  }
  $$("header nav button[data-view]").forEach(function (b) {   // ===== nav selector (see showView) =====
    b.addEventListener("click", function () { goView(b.dataset.view); });   // goView: showView plus a history entry (navigation block below)
  });

  // ===== navigation: context bar, filter chips, browser history, back =====
  // Every active filter is a chip in the bar under the sub-toolbar: a person, a category, a discovered one, drone,
  // photos or videos, the two focus boxes. A row on People or Categories ADDS its chip to the ones already there
  // (one per kind: a second person replaces the first); the form controls in the sub-toolbar and the sidebar are
  // the same filters, so a change there is the same as a chip. Every tab switch and every chip change is a history
  // entry ({view, chips, root} plus a hash such as #people or #search?person=126&category=beach), so command-[ and
  // the trackpad swipe walk back through them and a reload keeps the view. Back removes the last chip. A saved-person
  // find is not a form filter (its matches come from one POST), so it is the one chip that stands alone.
  var ctxBar = $("#ctxbar");
  var ctxBackBtn = $("#ctx-back");
  var ctxBackLabel = $("#ctx-back-label");
  var ctxChips = $("#ctx-chips");
  var ctxCount = $("#ctx-count");
  var ctxClearBtn = $("#ctx-clear");
  var viewSearch = $("#view-search");
  var VIEWS = ["search", "people", "categories", "index"];
  var TAB_NAMES = { people: "People", categories: "Categories" };
  var CHIP_KINDS = ["person", "saved", "category", "cluster", "drone", "kind", "hide_bad", "hide_soft"];
  try { history.scrollRestoration = "manual"; } catch (e) { /* not supported */ }

  function chipsKey(chips) { return JSON.stringify((chips || []).map(function (c) { return [c.kind, c.key]; })); }
  function chipOf(kind, chips) {
    var list = chips || state.chips;
    for (var i = 0; i < list.length; i++) if (list[i].kind === kind) return list[i];
    return null;
  }
  function personById(id) {
    for (var i = 0; i < state.people.length; i++) if (state.people[i].id === id) return state.people[i];
    return null;
  }
  function personLabel(p, id) { return (p && p.name) || "person_" + String(id).padStart(2, "0"); }
  // The filter params the chips stand for; passed to runSearch explicitly at boot, when the person select has no options yet.
  function ctxParams(chips) {
    var out = {};
    (chips || []).forEach(function (c) {
      if (c.kind === "person") out.person = c.key;
      else if (c.kind === "category") out.category = c.key;
      else if (c.kind === "cluster") out.cluster = c.key;
      else if (c.kind === "drone") out.aerial = 1;
      else if (c.kind === "kind") out.kind = c.key;
      else if (c.kind === "hide_bad") out.hide_bad = 1;
      else if (c.kind === "hide_soft") out.hide_soft = 1;
    });
    return out;
  }
  // The chips the form holds right now, in a fixed kind order (the person select, the two hidden category fields, the
  // drone box, the kind segments, the two focus boxes). A saved find lives only in state.chips, never in the form.
  function chipsFromForm() {
    var fd = new FormData(form), out = [];
    if (fd.get("person")) out.push({ kind: "person", key: Number(fd.get("person")), from: null });
    if (fd.get("category")) out.push({ kind: "category", key: fd.get("category"), from: null });
    if (fd.get("cluster")) out.push({ kind: "cluster", key: fd.get("cluster"), from: null });
    if (fd.get("aerial")) out.push({ kind: "drone", key: "drone", from: null });
    if (fd.get("kind")) out.push({ kind: "kind", key: fd.get("kind"), from: null });
    if (fd.get("hide_bad")) out.push({ kind: "hide_bad", key: "hide_bad", from: null });
    if (fd.get("hide_soft")) out.push({ kind: "hide_soft", key: "hide_soft", from: null });
    return out;
  }
  // After a form control changed by hand: chips still on the form keep their place and origin, new ones go last, gone ones drop.
  function syncChipsFromForm() {
    var now = chipsFromForm();
    var kept = state.chips.filter(function (c) {
      if (c.kind === "person" && !state.people.length) return true;   // the select has no options yet (boot): the chip stands
      return now.some(function (n) { return n.kind === c.kind && n.key === c.key; });
    });
    now.forEach(function (n) { if (!chipOf(n.kind, kept)) kept.push(n); });
    var changed = chipsKey(kept) !== chipsKey(state.chips);
    state.chips = kept;
    renderCtxBar();
    return changed;
  }
  // Write the chips into the form: the person select, the two hidden category fields, the drone box, the kind segments, the focus boxes.
  function applyChips(chips) {
    state.chips = (chips || []).slice();
    writeChipsToForm(state.chips);
    renderCtxBar();
  }
  // The form alone (no state change): a search run from the history list writes the form this way, so runSearch sees the change.
  function writeChipsToForm(chips) {
    var c = function (kind) { return chipOf(kind, chips); };
    personSelect.value = c("person") ? String(c("person").key) : "";
    $("#category-filter").value = c("category") ? c("category").key : "";
    $("#cluster-filter").value = c("cluster") ? c("cluster").key : "";
    aerialOnly.checked = !!c("drone");
    var k = c("kind") ? c("kind").key : "";
    $$('#kind-select input[name="kind"]').forEach(function (r) { r.checked = r.value === k; });
    $("#hide-bad").checked = !!c("hide_bad");
    $("#hide-soft").checked = !!c("hide_soft");
  }
  // Add one chip to the set: one per kind, so a second person replaces the first; a saved find stands alone.
  function addChip(chip, base) {
    var list = (base || state.chips).filter(function (c) { return c.kind !== chip.kind && c.kind !== "saved"; });
    if (chip.kind === "saved") list = [];
    list.push(chip);
    return list;
  }
  function chipLabel(c) {
    if (c.kind === "person") return personLabel(personById(c.key), c.key);
    if (c.kind === "cluster") return "discovered: " + (/^group \d+$/.test(c.key) ? "Unnamed " + c.key : c.key);
    if (c.kind === "kind") return c.key;
    if (c.kind === "hide_bad") return "no out of focus";
    if (c.kind === "hide_soft") return "no soft";
    return c.key;
  }
  function chipTitle(c) {
    if (c.kind === "person" || c.kind === "saved") return "only photos of " + chipLabel(c);
    if (c.kind === "category") return "only the " + c.key + " category";
    if (c.kind === "cluster") return "only the discovered category " + c.key;
    if (c.kind === "drone") return "only drone shots";
    if (c.kind === "kind") return "only " + c.key;
    if (c.kind === "hide_bad") return "the focus check's out-of-focus rows are hidden";
    return "the focus check's soft rows are hidden too";
  }
  // The origin tab of the last chip that came from one: the Back button's label and the sidebar dot.
  function lastOrigin() {
    for (var i = state.chips.length - 1; i >= 0; i--) if (state.chips[i].from) return state.chips[i].from;
    return null;
  }
  // The bar: Back, one chip per filter with its own x, "31 of 777 match", Clear filters.
  function renderCtxBar() {
    var chips = state.chips;
    ctxBar.hidden = !chips.length;
    viewSearch.classList.toggle("has-ctx", chips.length > 0);
    $$("header nav button[data-view]").forEach(function (b) {
      b.classList.toggle("from", chips.some(function (c) { return c.from === b.dataset.view; }));
    });
    ctxChips.innerHTML = "";                          // an emptied bar keeps no stale chips behind its hidden attribute
    if (!chips.length) { ctxCount.textContent = ""; ctxCount.title = ""; return; }
    var origin = lastOrigin();
    var tab = origin ? TAB_NAMES[origin] || origin : null;
    ctxBackLabel.textContent = tab || "Back";
    ctxBackBtn.title = tab ? "remove the last filter and go back to " + tab + " (Esc)" : "remove the last filter (Esc)";
    chips.forEach(function (c) {
      var chip = document.createElement("span");
      chip.className = "ctx-chip ctx-chip-" + c.kind;
      chip.title = chipTitle(c);
      var label = document.createElement("span");
      label.className = "ctx-chip-label";
      label.textContent = chipLabel(c);
      chip.appendChild(label);
      var x = document.createElement("button");
      x.type = "button"; x.className = "ctx-chip-x";
      x.title = "remove this filter";
      x.setAttribute("aria-label", "remove the " + chipLabel(c) + " filter");
      x.innerHTML = '<svg viewBox="0 0 8 8" aria-hidden="true"><path d="M1 1l6 6M7 1L1 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
      x.addEventListener("click", function () { removeChip(c); });
      chip.appendChild(x);
      ctxChips.appendChild(chip);
    });
    var n = state.total, m = state.shootTotal;
    ctxCount.textContent = m ? n + " of " + m + " match" : n + " match";
    ctxCount.title = m ? n + " of the shoot's " + m + " photos and clips pass every filter" : "";
  }

  // history
  var navRestoring = false;
  var HASH_KEYS = { person: "person", saved: "saved", category: "category", cluster: "cluster", drone: "aerial", kind: "kind", hide_bad: "hide_bad", hide_soft: "hide_soft" };
  function hashFor(view, chips) {
    if (view !== "search" || !chips || !chips.length) return "#" + view;
    var p = new URLSearchParams();
    chips.forEach(function (c) { p.set(HASH_KEYS[c.kind], c.kind === "drone" || c.kind === "hide_bad" || c.kind === "hide_soft" ? "1" : String(c.key)); });
    return "#search?" + p.toString();
  }
  // A pasted or typed URL: the chips in the order the hash lists them; a person, category, cluster or drone chip remembers its tab.
  function parseHash(h) {
    var m = /^#(search|people|categories|index)(?:\?(.*))?$/.exec(h || "");
    if (!m) return null;
    var entry = { view: m[1], chips: [] };
    if (m[1] === "search" && m[2]) {
      new URLSearchParams(m[2]).forEach(function (v, k) {
        if (k === "person" && /^\d+$/.test(v)) entry.chips = addChip({ kind: "person", key: Number(v), from: "people" }, entry.chips);
        else if (k === "saved" && v) entry.chips = [{ kind: "saved", key: v, from: "people" }];
        else if (k === "category" && v) entry.chips.push({ kind: "category", key: v, from: "categories" });
        else if (k === "cluster" && v) entry.chips.push({ kind: "cluster", key: v, from: "categories" });
        else if (k === "aerial" && v) entry.chips.push({ kind: "drone", key: "drone", from: "categories" });
        else if (k === "kind" && (v === "photos" || v === "videos")) entry.chips.push({ kind: "kind", key: v, from: null });
        else if (k === "hide_bad" && v) entry.chips.push({ kind: "hide_bad", key: "hide_bad", from: null });
        else if (k === "hide_soft" && v) entry.chips.push({ kind: "hide_soft", key: "hide_soft", from: null });
      });
      if (chipOf("saved", entry.chips)) entry.chips = [chipOf("saved", entry.chips)];
    }
    return entry;
  }
  // backable: the entry before this one is the origin tab, so the Back button can use history.back() and keep Forward alive.
  function navPush(replace, backable) {
    if (navRestoring || !(state.folder && state.folder.root)) return;
    var entry = { view: state.view, chips: state.chips, root: state.folder.root, backable: !!backable };
    var cur = history.state;
    var same = cur && cur.view === entry.view && chipsKey(cur.chips) === chipsKey(entry.chips) && cur.root === entry.root;
    var url = hashFor(entry.view, entry.chips);
    try {
      if (replace || !cur || same) history.replaceState(same && !replace ? cur : entry, "", url);
      else history.pushState(entry, "", url);
    } catch (e) { /* file: origins refuse */ }
  }
  function goView(name) {
    showView(name);
    navPush();
  }
  // Land on Search with one more chip. Called by the People rows, the saved-people rows and the category rows.
  function jumpTo(chip, run) {
    var backable = state.view === chip.from;
    applyChips(addChip(chip));
    showView("search");
    mainEl.scrollTop = 0;
    run();
    navPush(false, backable);
  }
  // Back removes the last chip: through the browser when the entry before is the origin tab (Forward then brings the chip
  // back), else by hand, landing on the chip's own tab when nothing is left.
  function navBack() {
    if (!state.chips.length) return;
    if (history.state && history.state.backable && history.state.root === state.folder.root) { history.back(); return; }
    var last = state.chips[state.chips.length - 1];
    applyChips(state.chips.slice(0, -1));
    runSearch(ctxParams(state.chips));
    if (last.from && !state.chips.length) goView(last.from);
    else navPush();
  }
  function removeChip(chip) {
    applyChips(state.chips.filter(function (c) { return c !== chip; }));
    runSearch(ctxParams(state.chips));
    navPush();
  }
  function clearCtx() {
    if (!state.chips.length) return;
    applyChips([]);
    runSearch();
    navPush();
  }
  // Put an entry's view and chips back; the grid is re-run whenever the chips changed, even on another tab, so it is never stale.
  function restoreEntry(entry) {
    var chips = entry.root && entry.root !== state.folder.root ? [] : (entry.chips || []);
    var changed = chipsKey(chips) !== chipsKey(state.chips);
    navRestoring = true;
    applyChips(chips);
    showView(VIEWS.indexOf(entry.view) >= 0 ? entry.view : "search");
    if (changed) {
      var saved = chipOf("saved", chips);
      if (saved) { if (state.view === "search") findSaved(saved.key); else pendingFind = saved.key; }
      else runSearch(ctxParams(chips));
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
    if (s && s.view && VIEWS.indexOf(s.view) >= 0) return { view: s.view, chips: s.root === state.folder.root ? (s.chips || []) : [] };
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
      if (s.indexing) bits.push("scanning…");
      if (s.errors) bits.push(s.errors + " failed");
      if (s.faces_pending) bits.push(s.faces_pending + " need faces");
      $("#stats").textContent = bits.join(" · ");
      $("#stats").title = s.last_index ? "scanned " + s.last_index : "";
      // ===== end title block =====
      // ===== combined filters: "31 of 777 match" needs the shoot's size =====
      state.shootTotal = (s.photos || 0) + (s.videos || 0);
      renderCtxBar();
      // ===== end combined filters =====
      // ===== welcome: the shoot disk went away since the folder opened; the welcome comes back with its line =====
      if (state.folder && state.folder.root && s.mounted === false && state.folder.mounted !== false) {
        state.folder.mounted = false;
        showView(state.view);
      }
      // ===== end welcome =====
      // ===== after stats: capabilities, the faces-pending controls, the focus line and the reorganise status =====
      state.caps.focus = !!(s && typeof s.focus === "object");   // an older server has no focus or reorganise endpoints
      if (s && s.scan) { state.scan = s.scan; renderScanBar(); }   // ===== resume =====
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
      $("#index-errors-title").textContent = list.length + " file(s) could not be read. They are skipped; tick Retry failed files and Scan again once fixed.";
      $("#index-errors-list").textContent = list.slice(0, 200).map(function (e) { return e.rel; }).join("\n") + (list.length > 200 ? "\n… " + (list.length - 200) + " more" : "");
    }).catch(function () { /* non-fatal */ });
  }

  // ---------- folder ----------
  var folderNameEl = $("#folder-name");
  var recentSelect = $("#recent-folders");

  function applyFolderInfo(info) {
    state.folder = info || { root: null, name: null, indexed: false };
    if (info && info.scan) state.scan = info.scan;   // ===== resume =====
    else if (!info || !info.root) state.scan = null;
    folderNameEl.textContent = state.folder.name || "no folder open";
    folderNameEl.title = state.folder.root || "";
  }
  // ===== welcome: a folder counts as open only while its disk is there (/api/folder and /api/stats carry mounted) =====
  function folderOpen(f) { return !!(f && f.root && f.mounted !== false); }
  var welcomeUnmounted = $("#welcome-unmounted");
  var welcomeDisk = $("#welcome-disk");
  function setWelcomeFrame(hasFolder) {
    document.body.classList.toggle("nofolder", !hasFolder);
    ["header nav", "#folderbar", "#exportbar", "#inspector-toggle"].forEach(function (sel) {
      var el = $(sel);
      if (el) el.inert = !hasFolder;
    });
    var out = !hasFolder && !!(state.folder && state.folder.root);   // a shoot is open but its disk is unplugged
    welcomeUnmounted.hidden = !out;
    $(".welcome-ask").hidden = out;
    if (out) welcomeDisk.textContent = state.folder.disk || state.folder.name || "the disk";
    // ===== resume: a shoot whose scan was cut short says so under the connect line =====
    var left = out && state.scan && !state.scan.complete;
    $("#welcome-scanleft").hidden = !left;
    if (left) $("#welcome-scanleft-text").textContent = scanSummary(state.scan) + ".";
    // ===== end resume =====
  }
  // ===== end welcome =====

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
    // ===== welcome: the same folders as small links under the two buttons =====
    var box = $("#welcome-recent");
    $$(".link", box).forEach(function (b) { b.remove(); });
    state.recent.forEach(function (r) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "link small";
      b.textContent = r.name || r.path; b.title = r.path;
      b.addEventListener("click", function () { switchFolder(r.path); });
      box.appendChild(b);
    });
    box.hidden = state.recent.length === 0;
    // ===== end welcome =====
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
    // ===== per-folder reset: focus boxes and line, faces pending, the reorganise section =====
    renderFocusLine(null);
    renderFacesPending(null);
    resetReorganise();
    renderScanBar(); renderScanHealth(null);          // ===== resume: state.scan came with the folder reply =====
    loadProject();                                     // ===== project file: the card for this shoot =====
    // ===== end per-folder reset =====
    // ===== navigation: a filter from the previous shoot means nothing here; the lists start at the top =====
    applyChips([]);
    state.shootTotal = 0;
    state.scroll = {};
    // ===== end navigation =====
    closeHistory(); loadSearches();                   // ===== search history: the new shoot's list =====
    state.showCopies = false;                          // ===== duplicates and bursts: folded again on a new shoot =====
    if ($("#show-copies")) $("#show-copies").checked = false;
    loadPrefs(); loadExports();                        // ===== export presets and history: this shoot's =====
    loadRecent();
    if (!folderOpen(info)) {
      showView(state.view);
      return;
    }
    if (!info.indexed) {
      showView("index");
      navPush(true);                                   // ===== navigation: the new shoot replaces the entry, never a push =====
      var btn = $("#start-index");
      if (btn) btn.focus();
      loadStats().then(function (s) { if (s.indexing) pollProgress(); });   // first scan of a fresh folder, page reloaded mid-run
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
      if (!info) return null;
      applyFolderInfo(info);
      setStatus("opened " + (info.name || info.root));
      settleFolder(info);
      return info;
    }).catch(function (err) {
      setStatus("could not open folder: " + err.message);
      return null;
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

  function num(x) { return Number(x).toLocaleString("en-US"); }

  var PAGE = 200;
  // Two searches can be in flight at once (a radio change fires one, the chip sync another), and the slower
  // reply used to land last and leave the counts describing a filter that is no longer on. Every search takes
  // a ticket; only the newest one is allowed to paint. "Show more" (append) keeps its own ticket.
  var searchSeq = 0;
  function runSearch(extra, append) {
    // ===== combined filters: a plain search (no explicit params) reads the chips off the form, so a control changed by hand
    // is a chip like any other; a saved-person find is not a form filter, so any plain search drops it. A change is a history entry. =====
    var pushAfter = false;
    if (!extra && !append) {
      if (chipOf("saved")) state.chips = state.chips.filter(function (c) { return c.kind !== "saved"; });
      pushAfter = syncChipsFromForm();
    }
    // ===== end combined filters =====
    var params = currentFilters();
    Object.assign(params, extra || {});
    params.fold = state.showCopies ? 0 : 1;            // ===== duplicates and bursts: one tile per set unless "Show every copy" is ticked =====
    if (!append) { state.offset = 0; state.results = []; }
    params.limit = PAGE; params.offset = state.offset;
    state.lastParams = params;
    var qs = new URLSearchParams(params).toString();
    var ticket = ++searchSeq;
    return api("/api/search?" + qs).then(function (data) {
      if (ticket !== searchSeq) return;                // a newer search is on its way; this reply is history
      state.results = append ? state.results.concat(data.results || []) : (data.results || []);
      state.total = data.total || 0;
      state.folded = data.folded || 0;                 // ===== duplicates and bursts: rows inside the tiles =====
      state.offset = state.results.length;
      renderGrid();
      if (pushAfter) navPush();                        // ===== combined filters: the hash and history follow the chips =====
    }).catch(function (err) {
      if (ticket !== searchSeq) return;
      if (err.status === 404) {
        state.results = []; state.total = 0; state.folded = 0;
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
  aerialOnly.addEventListener("change", function () { runSearch(); });   // ===== combined filters: the box is the drone chip =====

  // ===== search history and saved searches: arrow down in the field lists the last 20 queries on this shoot, each with the
  // filters it ran with; a star keeps one as a chip in the sidebar under Filters; a row or a chip puts the text and every
  // filter back and runs it (a history entry like any chip change). The list is per shoot, in the index (table searches). =====
  var qField = form.querySelector('[name="q"]');
  var qHist = $("#q-history");
  var savedGroup = $("#saved-group");
  var savedSearchesEl = $("#saved-searches");
  var qhRows = [];                                     // the rows in the dropdown, in order
  var qhIndex = -1;                                    // the highlighted row
  var FACES_WORDS = { none: "no people", one: "one person", two: "two people", group: "group of 3+" };
  function loadSearches() {
    if (!(state.folder && state.folder.root)) { state.searches = { recent: [], saved: [] }; renderSavedSearches(); return Promise.resolve(); }
    return api("/api/searches").then(function (d) {
      state.searches = { recent: d.recent || [], saved: d.saved || [] };
      renderSavedSearches();
      if (!qHist.hidden) renderHistory();
    }).catch(function () { /* the list is a convenience; a failed load leaves the old one */ });
  }
  // The filters a query ran with, in a few words: "person_01 · beach · drone · photos · sharp 40"
  function filterSummary(f) {
    f = f || {};
    var parts = [];
    if (f.person) parts.push(personLabel(personById(Number(f.person)), Number(f.person)));
    if (f.category) parts.push(f.category);
    if (f.cluster) parts.push(/^group \d+$/.test(f.cluster) ? "Unnamed " + f.cluster : f.cluster);
    if (f.aerial) parts.push("drone");
    if (f.kind) parts.push(f.kind);
    if (f.faces) parts.push(FACES_WORDS[f.faces] || f.faces);
    if (f.sharp) parts.push("sharp " + Math.round(Number(f.sharp)));
    if (f.hide_soft) parts.push("no soft"); else if (f.hide_bad) parts.push("no out of focus");
    return parts.join(" · ");
  }
  function chipsFromFilters(f) {
    f = f || {};
    var chips = [];
    if (f.person && /^\d+$/.test(String(f.person))) chips.push({ kind: "person", key: Number(f.person), from: null });
    if (f.category) chips.push({ kind: "category", key: String(f.category), from: null });
    if (f.cluster) chips.push({ kind: "cluster", key: String(f.cluster), from: null });
    if (f.aerial) chips.push({ kind: "drone", key: "drone", from: null });
    if (f.kind === "photos" || f.kind === "videos") chips.push({ kind: "kind", key: f.kind, from: null });
    if (f.hide_bad) chips.push({ kind: "hide_bad", key: "hide_bad", from: null });
    if (f.hide_soft) chips.push({ kind: "hide_soft", key: "hide_soft", from: null });
    return chips;
  }
  // Run a recent or saved search: the text, the toolbar controls (Sharp, faces) and the chips it ran with.
  function applySearchRow(row) {
    var f = row.filters || {};
    closeHistory();
    if (state.view !== "search") goView("search");
    qField.value = row.q;
    sharpInput.value = f.sharp ? Number(f.sharp) : 0;
    sharpInput.dispatchEvent(new Event("input"));    // the number field and the fill follow the slider
    form.querySelector('[name="faces"]').value = f.faces && FACES_WORDS[f.faces] ? f.faces : "";
    writeChipsToForm(chipsFromFilters(f));            // the form only: runSearch reads the chips off it and pushes a history entry
    track("search_again", { saved: !!row.saved, filters: Object.keys(f).length });
    runSearch();
    mainEl.scrollTop = 0;
  }
  function setSaved(row, saved) {
    return api("/api/searches/" + row.id + "/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ saved: saved }) })
      .then(function () { setStatus(saved ? "saved: " + row.q : "forgot the saved search " + row.q); return loadSearches(); })
      .catch(function (err) { setStatus("could not save the search: " + err.message); });
  }
  function forgetSearch(row) {
    return api("/api/searches/" + row.id, { method: "DELETE" }).then(function () { return loadSearches(); })
      .catch(function (err) { setStatus("could not remove the search: " + err.message); });
  }
  var X_SVG = '<svg viewBox="0 0 8 8" aria-hidden="true"><path d="M1 1l6 6M7 1L1 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
  var STAR_SVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.2l1.5 3.1 3.4.5-2.45 2.4.6 3.4L6 9l-3.05 1.6.6-3.4L1.1 4.8l3.4-.5z"/></svg>';
  function renderHistory() {
    var keep = qhIndex;                                // a re-render after a fresh load keeps the highlight
    qHist.innerHTML = ""; qhRows = []; qhIndex = -1;
    var rows = state.searches.recent;
    if (!rows.length) {
      var e = document.createElement("div"); e.className = "qh-empty"; e.textContent = "No searches yet on this shoot"; qHist.appendChild(e);
      return;
    }
    rows.forEach(function (row) {
      var r = document.createElement("div"); r.className = "qh-row"; r.setAttribute("role", "option"); r.dataset.id = row.id;
      var t = document.createElement("span"); t.className = "qh-text"; t.textContent = row.q; t.title = row.q;
      var f = document.createElement("span"); f.className = "qh-filters"; f.textContent = filterSummary(row.filters); f.title = f.textContent;
      var star = document.createElement("button");
      star.type = "button"; star.className = "qh-star"; star.innerHTML = STAR_SVG;
      star.setAttribute("aria-pressed", row.saved ? "true" : "false");
      star.title = row.saved ? "forget this saved search" : "save this search as a chip in the sidebar";
      star.setAttribute("aria-label", (row.saved ? "forget the saved search " : "save the search ") + row.q);
      star.addEventListener("click", function (ev) { ev.stopPropagation(); setSaved(row, !row.saved); });
      var del = document.createElement("button");
      del.type = "button"; del.className = "qh-del"; del.innerHTML = X_SVG;
      del.title = "remove from the list"; del.setAttribute("aria-label", "remove " + row.q + " from the list");
      del.addEventListener("click", function (ev) { ev.stopPropagation(); forgetSearch(row); });
      r.appendChild(t); r.appendChild(f); r.appendChild(star); r.appendChild(del);
      r.addEventListener("mousedown", function (ev) { ev.preventDefault(); });   // the field keeps its focus
      r.addEventListener("click", function () { applySearchRow(row); });
      qHist.appendChild(r); qhRows.push(r);
    });
    if (keep >= 0) highlightRow(Math.min(keep, qhRows.length - 1));
  }
  function openHistory() {
    renderHistory();
    qHist.hidden = false; qField.setAttribute("aria-expanded", "true");
    loadSearches().then(function () {                  // fresh: the last search may have added a row
      if (!qHist.hidden && qhIndex < 0 && qhRows.length) highlightRow(0);
    });
  }
  function closeHistory() {
    if (qHist.hidden) return;
    qHist.hidden = true; qhIndex = -1; qField.setAttribute("aria-expanded", "false");
  }
  function highlightRow(i) {
    qhIndex = i;
    qhRows.forEach(function (r, k) { r.classList.toggle("on", k === i); });
    if (qhRows[i]) qhRows[i].scrollIntoView({ block: "nearest" });
  }
  qField.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" && !e.altKey && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      if (qHist.hidden) { openHistory(); highlightRow(qhRows.length ? 0 : -1); }
      else highlightRow(Math.min(qhIndex + 1, qhRows.length - 1));
      return;
    }
    if (qHist.hidden) return;
    if (e.key === "ArrowUp") { e.preventDefault(); if (qhIndex <= 0) closeHistory(); else highlightRow(qhIndex - 1); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeHistory(); return; }
    if (e.key === "Enter" && qhIndex >= 0 && state.searches.recent[qhIndex]) { e.preventDefault(); applySearchRow(state.searches.recent[qhIndex]); return; }
    if (e.key === "Tab") closeHistory();
  });
  qField.addEventListener("input", closeHistory);
  qField.addEventListener("blur", closeHistory);
  form.addEventListener("submit", closeHistory);
  function renderSavedSearches() {
    var rows = state.searches.saved;
    savedGroup.hidden = savedSearchesEl.hidden = !rows.length;
    savedSearchesEl.innerHTML = "";
    rows.forEach(function (row) {
      var chip = document.createElement("span"); chip.className = "ss-chip";
      var run = document.createElement("button");
      run.type = "button"; run.className = "ss-run"; run.textContent = row.q;
      var sum = filterSummary(row.filters);
      run.title = "run this saved search" + (sum ? ": " + row.q + " · " + sum : "");
      run.addEventListener("click", function () { applySearchRow(row); });
      var x = document.createElement("button");
      x.type = "button"; x.className = "ss-x"; x.innerHTML = X_SVG;
      x.title = "forget this saved search"; x.setAttribute("aria-label", "forget the saved search " + row.q);
      x.addEventListener("click", function () { setSaved(row, false); });
      chip.appendChild(run); chip.appendChild(x);
      savedSearchesEl.appendChild(chip);
    });
  }
  // ===== end search history =====
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
      // ===== duplicates and bursts: "x2" for a file that is on the disk twice, "burst of 12" for a burst =====
      if (r.group) {
        var grp = document.createElement("div");
        grp.className = "badge group mono";
        grp.textContent = r.group.kind === "burst" ? "burst of " + r.group.n : "x" + r.group.n;
        grp.title = r.group.kind === "burst" ? "one tile for " + r.group.n + " frames shot as a burst" : "the same file is on the disk " + r.group.n + " times";
        card.appendChild(grp);
      }
      // ===== end duplicates and bursts =====

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
    // ===== duplicates and bursts: say how many rows the tiles are hiding, so a filtered count is never a surprise =====
    var folded = state.folded ? ", " + num(state.folded) + " " + (state.folded === 1 ? "copy" : "copies") + " folded" : "";
    $("#shown-count").textContent = (ranked ? state.results.length + " shown" : state.results.length + " of " + state.total + " shown") + folded;
    $("#shown-count").title = state.folded ? "duplicates and burst frames are one tile each; tick Show every copy in the sidebar to see them all" : "";
    $("#show-more").hidden = state.results.length >= state.total;
    $("#select-matching").hidden = ranked;
    updateSelbar();
    renderCtxBar();                                    // ===== navigation: the count line follows the results =====
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
          loadExports();                               // ===== export history: the Drive row =====
          return;
        }
        // ===== end Google Drive =====
        if (p.running) {
          setStatus(label + " " + p.done + "/" + p.total + (p.failed ? ", " + p.failed + " failed" : "") + (p.skipped ? ", " + p.skipped + " already there" : ""), true);
          return;
        }
        clearInterval(exportTimer); exportTimer = null;
        // ===== project file: its own end line, and the card shows the new place and time =====
        if (p.what === "bundle") {
          loadProject();
          if (p.error) { setStatus("could not save the project: " + p.error, true); return; }
          setStatus("project saved: " + p.path + (p.failed ? " (" + p.failed + " thumbnails could not be read)" : ""), true);
          return;
        }
        // ===== end project file =====
        loadExports();                                 // ===== export history: the row for this export =====
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
    var prefs = exportPrefs();                         // ===== export presets: the size and the RAW box ride along =====
    return api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: ids, name: name, mode: mode, web_size: mode === "copy" ? prefs.web_size : null, include_raw: prefs.include_raw }),
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

  // ===== export presets: one select sets copy or links, the size and the RAW box for the selection export; any change to those
  // makes it Custom; the choice is remembered per shoot (/api/export/prefs, in the index). With Google Drive as the destination
  // the preset writes the strip's size and RAW controls instead, since the strip owns them there. =====
  var PRESETS = { web: { mode: "copy", web_size: 2048, include_raw: false }, links: { mode: "symlink", web_size: null, include_raw: false },
                  full: { mode: "copy", web_size: null, include_raw: true } };
  var exportPreset = $("#export-preset");
  var exportSize = $("#export-size");
  var exportRaw = $("#export-raw");
  var exportRawLabel = $("#export-raw-label");
  var prefsQuiet = false;                              // writing the controls from a preset must not flip it to Custom
  function exportPrefs() {                             // the controls as they stand
    var mode = $("#exportmode").value;
    return { preset: exportPreset.value, mode: mode, web_size: mode === "copy" && exportSize.value ? Number(exportSize.value) : null, include_raw: exportRaw.checked };
  }
  function syncExportControls() {                      // links and the CSV point at the originals: no size for them
    var copy = $("#exportmode").value === "copy";
    exportSize.disabled = !copy;
    exportSize.title = copy ? "the long edge for photos in a copy; RAW files and clips are never resized" : "links and the CSV point at the originals, so there is no size";
  }
  function applyPreset(name, save) {
    var p = PRESETS[name];
    prefsQuiet = true;
    exportPreset.value = name;
    if (p) {
      $("#exportmode").value = p.mode;
      exportSize.value = p.web_size ? String(p.web_size) : "";
      exportRaw.checked = p.include_raw;
      $("#drive-web-size").value = p.web_size ? String(p.web_size) : "";
      $("#drive-include-raw").checked = p.include_raw;
    }
    syncExportControls();
    prefsQuiet = false;
    if (save) savePrefs();
  }
  function savePrefs() {
    if (!(state.folder && state.folder.root)) return;
    api("/api/export/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(exportPrefs()) })
      .catch(function () { /* the preset is a convenience; the export itself carries the controls */ });
  }
  function loadPrefs() {
    if (!(state.folder && state.folder.root)) return Promise.resolve();
    return api("/api/export/prefs").then(function (p) {
      prefsQuiet = true;
      exportPreset.value = p.preset || "web";
      $("#exportmode").value = p.mode || "copy";
      exportSize.value = p.web_size ? String(p.web_size) : "";
      exportRaw.checked = !!p.include_raw;
      if (p.preset !== "custom") { $("#drive-web-size").value = exportSize.value; $("#drive-include-raw").checked = exportRaw.checked; }
      syncExportControls();
      prefsQuiet = false;
    }).catch(function () { prefsQuiet = false; });
  }
  function customised() {                              // a control changed by hand: the preset is Custom now
    if (prefsQuiet) return;
    exportPreset.value = "custom";
    syncExportControls();
    track("export_custom", {});
    savePrefs();
  }
  exportPreset.addEventListener("change", function () { track("export_preset", { preset: exportPreset.value }); applyPreset(exportPreset.value, true); });
  $("#exportmode").addEventListener("change", customised);
  exportSize.addEventListener("change", customised);
  exportRaw.addEventListener("change", customised);
  $("#drive-web-size").addEventListener("change", function () { if (!prefsQuiet && state.view === "search") { exportSize.value = $("#drive-web-size").value; customised(); } });
  $("#drive-include-raw").addEventListener("change", function () { if (!prefsQuiet && state.view === "search") { exportRaw.checked = $("#drive-include-raw").checked; customised(); } });
  syncExportControls();
  // ===== end export presets =====

  // ===== export history: the last 10 exports of this shoot in the Scan tab, with Show in Finder and Run again =====
  var exportsList = $("#exports-list");
  var exportsEmpty = $("#exports-empty");
  var WHAT_WORDS = { selection: "Selection", categories: "Categories", people: "People" };
  function loadExports() {
    if (!(state.folder && state.folder.root)) { renderExports([]); return Promise.resolve(); }
    return api("/api/exports").then(function (d) { renderExports(d.exports || []); }).catch(function () { /* the list is a convenience */ });
  }
  function renderExports(rows) {
    exportsList.innerHTML = "";
    exportsEmpty.hidden = rows.length > 0;
    rows.forEach(function (r) {
      var row = document.createElement("div"); row.className = "exp-row" + (r.error ? " failed" : "");
      var what = document.createElement("div"); what.className = "exp-what";
      var n = r.count || 0;
      what.textContent = (WHAT_WORDS[r.what] || r.what) + ", " + (r.options || "") + " ";
      var cnt = document.createElement("span"); cnt.className = "exp-count";
      cnt.textContent = r.error ? "failed: " + r.error : n + (n === 1 ? " file" : " files") + (r.skipped ? ", " + r.skipped + " already there" : "") + (r.failed ? ", " + r.failed + " failed" : "");
      what.appendChild(cnt);
      var where = document.createElement("div"); where.className = "exp-where"; where.textContent = r.path || ""; where.title = r.path || "";
      var when = document.createElement("div"); when.className = "exp-when"; when.textContent = fmtWhen(r.at); when.title = r.at || "";
      var acts = document.createElement("div"); acts.className = "exp-acts";
      if (r.exists) {
        var show = document.createElement("button"); show.type = "button"; show.className = "link small"; show.textContent = "Show in Finder";
        show.title = "open the export folder in the Finder";
        show.addEventListener("click", function () { revealPath(r.path); });
        acts.appendChild(show);
      } else if (r.dest === "drive" && r.path) {
        var open = document.createElement("a"); open.className = "link small"; open.textContent = "Open in Drive"; open.href = r.path; open.target = "_blank"; open.rel = "noopener";
        acts.appendChild(open);
      }
      var again = document.createElement("button"); again.type = "button"; again.className = "link small"; again.textContent = "Run again";
      again.title = "the same export, into the destination as it is now";
      again.addEventListener("click", function () { runExportAgain(r); });
      acts.appendChild(again);
      row.appendChild(what); row.appendChild(where); row.appendChild(when); row.appendChild(acts);
      exportsList.appendChild(row);
    });
  }
  function runExportAgain(r) {
    api("/api/exports/" + r.id + "/run", { method: "POST" }).then(function (res) {
      var label = (r.dest === "drive" ? "uploading " : "exporting ") + (WHAT_WORDS[r.what] || r.what).toLowerCase();
      setStatus(label + " again…", true);
      pollExportProgress(label);
    }).catch(function (err) { setStatus("could not run it again: " + err.message, true); });
  }
  // ===== end export history =====

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
    $("#export-size").hidden = drive; $("#export-raw-label").hidden = drive;   // ===== export presets: the Drive strip carries size and RAW =====
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
    if (e.key === "Escape" && !typing && state.view === "search" && state.chips.length) { e.preventDefault(); navBack(); return; }
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
    if (r.group) info.body.appendChild(groupRow(r));   // ===== duplicates and bursts: the copies toggle, pick the sharpest =====
    cats.body.appendChild(inspRow("Category", r.category ? r.category + (r.category_score != null ? "  " + Math.round(r.category_score * 100) + "%" : "") : "unclassified"));
    if (r.cluster) cats.body.appendChild(inspRow("Discovered", /^group \d+$/.test(r.cluster) ? "Unnamed " + r.cluster : r.cluster));
    if (r.sure === false) cats.body.appendChild(inspRow("Confidence", Math.round((r.confidence || 0) * 100) + "%, less sure"));
    cats.body.appendChild(inspRow("Aerial", r.aerial ? "yes" : "no"));
    faces.body.appendChild(inspRow("Faces", r.kind === "video" ? "not scanned in clips" : facesLabel(r.n_faces)));
  }
  // ===== duplicates and bursts: the inspector row for a tile that stands for a set. Copies: "2 on disk" and a link that
  // unfolds them into their own tiles (and folds them back). Bursts: "12 frames", "pick the sharpest" selects the frame
  // with the best sharpness score (the tile swaps to it while folded), and the same unfold link. Export follows the
  // selection, so a folded set exports the shown one and an unfolded one exports every tile picked. =====
  function groupRow(r) {
    var g = r.group, burst = g.kind === "burst";
    var row = inspRow(burst ? "Burst" : "Copies", burst ? g.n + " frames" : g.n + " on disk");
    var acts = document.createElement("span"); acts.className = "insp-acts";
    if (burst) {
      var pick = document.createElement("button");
      pick.type = "button"; pick.className = "link small"; pick.textContent = "pick the sharpest";
      pick.title = "select the frame with the best sharpness score" + (g.sharpest === r.id ? " (this one)" : "");
      pick.addEventListener("click", function () { pickSharpest(r); });
      acts.appendChild(pick);
    }
    var tog = document.createElement("button");
    tog.type = "button"; tog.className = "link small";
    tog.textContent = state.showCopies ? (burst ? "fold the burst" : "fold copies") : (burst ? "show every frame" : "show copies");
    tog.title = state.showCopies ? "one tile per set again" : "every copy and frame as its own tile, selectable and exportable one by one";
    tog.addEventListener("click", function () { setShowCopies(!state.showCopies); });
    acts.appendChild(tog);
    row.appendChild(acts);
    return row;
  }
  function setShowCopies(v) {
    var box = $("#show-copies");
    if (v === state.showCopies) { if (box) box.checked = v; return; }
    if (chipOf("saved")) { setStatus("a saved-person find shows every frame already"); if (box) box.checked = false; return; }
    state.showCopies = v;
    if (box) box.checked = v;
    track("copies", { shown: v });
    var p = Object.assign({}, state.lastParams); delete p.limit; delete p.offset;
    runSearch(p).then(function () { setStatus(v ? "every copy and frame is its own tile" : "copies and bursts folded, one tile each"); });
  }
  var showCopiesBox = $("#show-copies");
  if (showCopiesBox) showCopiesBox.addEventListener("change", function () { setShowCopies(showCopiesBox.checked); });

  function pickSharpest(r) {
    var g = r.group;
    var best = null;
    for (var i = 0; i < g.members.length; i++) if (g.members[i].id === g.sharpest) best = g.members[i];
    if (!best) return;
    g.members.forEach(function (m) { state.selected.delete(m.id); });
    if (state.showCopies || best.id === r.id) {
      state.selected.add(best.id);
      $$(".card", gridEl).forEach(function (c) {
        var id = Number(c.dataset.id);
        if (g.members.some(function (m) { return m.id === id; })) c.classList.toggle("selected", id === best.id);
      });
    } else {                                           // folded: the tile becomes the sharpest frame, selected
      var idx = indexOfId(r.id);
      if (idx < 0) return;
      state.results[idx] = Object.assign({}, r, { id: best.id, rel: best.rel, qhash: best.qhash, sharp_pct: best.sharp_pct, group: g });
      state.selected.add(best.id);
      var top = mainEl.scrollTop;
      renderGrid();
      mainEl.scrollTop = top;
      hoveredId = best.id;
    }
    lastClickedId = best.id;
    updateSelbar();
    setStatus("picked " + best.rel.split("/").pop() + ", the sharpest of " + g.n);
  }
  // ===== end duplicates and bursts =====
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
    // ===== navigation: a person chip set before the options existed (reload, history) takes the select now; names reach the chip =====
    var pc = chipOf("person");
    if (pc && !personSelect.value) personSelect.value = String(pc.key);
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
    state.folded = data.folded || 0;
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
      // ===== navigation: the count line follows the matches once they are in =====
      var sc = chipOf("saved");
      if (sc && sc.key === name) renderCtxBar();
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

  // A tile adds its chip (a fixed category, a discovered one, or the drone flag) to whatever is already filtering the grid.
  // ===== navigation: each lands on Search through jumpTo, which writes the fields, the chips and a history entry =====
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

  // ===== scan wording: "Scanning 1,204 of 3,677  reading photos and clips  4.1/s, about 3 min left" =====
  var STAGE_TEXT = { scan: "finding files", features: "reading photos and clips", faces: "finding faces", embed: "learning what is in each shot" };
  function formatProgress(p) {
    if (p.stage === "error") return "scan failed: " + (p.error || "unknown error");
    var done = p.done || 0, total = p.total || 0;
    var n = function (x) { return Number(x).toLocaleString("en-US"); };
    // ===== resume: the disk went away mid-scan; what was read is kept and Continue does the rest =====
    if (p.stage === "paused") return "Scan paused at " + n(done) + " of " + n(total) + ": the disk went away. Plug it in and press Continue scan; nothing already scanned is read again.";
    // ===== end resume =====
    if (p.stage === "done") return "Scanned " + n(total) + " of " + n(total) + (p.running ? "" : "  finished");
    var line = (total || p.stage !== "scan") ? "Scanning " + n(done) + " of " + n(total) : "Scanning";
    line += "  " + (STAGE_TEXT[p.stage] || p.stage);
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
        renderAwake(p);                                // ===== resume: "keeping the Mac awake" while caffeinate holds =====
        renderScanBar(p);                              // ===== resume: the search head shows the same readout =====
        if (!p.running) {
          stopProgressPoll();
          if (p.stage === "error") {
            setStatus("scan failed: " + (p.error || "unknown error"), true);
          } else if (p.stage === "paused") {
            setStatus("scan paused: the disk went away. Plug it in and press Continue scan.", true);   // ===== resume =====
          } else {
            setStatus("scan finished. Save the project (command S) so it travels with the disk.", true);
          }
          // the index changed under us: refresh everything that shows it
          loadStats().then(function () { if (state.view === "index") loadScanHealth(); });   // ===== resume: the health line after a scan =====
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
      if (err.status === 409 && /already scanning/.test(err.message || "")) {
        setStatus(err.message);
        pollProgress();
      } else {
        // a 409 with another reason (the disk is not connected, a reorganise or focus check is running) is
        // shown as it is; polling here would overwrite it with "scan finished" on the first idle tick
        setStatus("could not start the scan: " + err.message, true);
      }
    });
  }
  $("#start-index").addEventListener("click", function () {
    startIndexJob({ faces: $("#faces").checked, retry_errors: $("#retry-errors").checked }, "scan started…");
  });
  // ===== end index job starter =====

  // ===== resume: the scan bar, the welcome line, the Scan tab's resume / awake / health lines, Continue =====
  // state.scan (from /api/stats and /api/folder) says how far the scan got from the index alone: items listed,
  // rows read (scanned), rows searchable (embedded), rows listed but never read (pending), and the job cut short
  // (interrupted: {kind scan|focus, stage, done, total}). The bar under the search head shows it while the shoot
  // is not fully scanned, and the same readout as the Scan tab while a scan runs; the welcome shows it while the
  // disk is away. Continue is POST /api/index {resume: true}: the server reuses the interrupted scan's own faces
  // choice and only reads what is missing. The health line (GET /api/scan/health, when the Scan tab opens) adds
  // what is on the disk right now and the one fix that closes the gap.
  var scanBar = $("#scanbar");
  var scanBarText = $("#scanbar-text");
  var scanBarAwake = $("#scanbar-awake");
  var scanBarContinue = $("#scanbar-continue");
  var scanResume = $("#scan-resume");
  var scanResumeText = $("#scan-resume-text");
  var scanAwake = $("#scan-awake");
  var scanHealthText = $("#scan-health-text");
  var scanFix = $("#scan-fix");
  var fmtN = function (x) { return Number(x || 0).toLocaleString("en-US"); };
  var STAGE_AT = { scan: "listing the files", features: "reading photos and clips", faces: "finding faces", embed: "learning what is in each shot", focus: "checking focus" };
  var lastProgress = null;                             // the last /api/progress reply while a scan runs

  // "2,080 of 4,315 scanned" / "4,315 read, 2,235 not searchable yet": the one-line state of an unfinished scan.
  function scanSummary(sc) {
    if (!sc) return "";
    if (sc.pending) return fmtN(sc.scanned) + " of " + fmtN(sc.items) + " scanned";
    if (sc.unembedded) return fmtN(sc.items) + " read, " + fmtN(sc.unembedded) + " not searchable yet";
    return fmtN(sc.items) + " scanned";
  }
  // Where the last scan stopped, in words, as the lead-in to the summary: "The last scan stopped while reading
  // photos and clips: 2,080 of 4,315 scanned." A focus pass cut short is its own sentence.
  function interruptedLine(sc) {
    var j = sc && sc.interrupted;
    if (!j) return "";
    if (j.kind === "focus") return "The last focus check stopped at " + fmtN(j.done) + " of " + fmtN(j.total) + ". ";
    return "The last scan stopped while " + (STAGE_AT[j.stage] || j.stage || "scanning") + ": ";
  }

  function continueScan() {
    startIndexJob({ resume: true }, "continuing the scan…");
    track("scan_continue");
  }
  scanBarContinue.addEventListener("click", continueScan);
  $("#scan-continue").addEventListener("click", continueScan);
  $("#scanbar-tab").addEventListener("click", function () { goView("index"); });

  // The bar under the search head. Running: the progress readout (p) and the awake note. Not running and the
  // scan short: the summary with Continue. Complete: hidden.
  function renderScanBar(p) {
    if (p && p.running) lastProgress = p;
    else if (p && !p.running) lastProgress = null;
    var sc = state.scan;
    var running = !!(lastProgress && lastProgress.running);
    var open = !!(state.folder && state.folder.root);
    // the Scan tab's own resume block mirrors the bar's idle state
    var showResume = open && !running && !!sc && !sc.complete;
    scanResume.hidden = !showResume;
    if (showResume) {
      scanResumeText.textContent = interruptedLine(sc) + scanSummary(sc) + (sc.pending ? ", " + fmtN(sc.pending) + " still to read" : "") + ". Continue reads only what is missing.";
    }
    if (showResume && healthFix === "continue") scanFix.hidden = true;   // the block above already carries Continue
    if (!open || (!running && (!sc || sc.complete))) { scanBar.hidden = true; scanBar.classList.remove("running"); return; }
    scanBar.hidden = false;
    scanBar.classList.toggle("running", running);
    if (running) {
      scanBarText.textContent = formatProgress(lastProgress);
      scanBarAwake.hidden = !lastProgress.awake;
      scanBarContinue.hidden = true;
    } else {
      scanBarText.textContent = interruptedLine(sc) + scanSummary(sc) + ". Search covers only what is scanned so far.";
      scanBarAwake.hidden = true;
      scanBarContinue.hidden = !!(sc.interrupted && sc.interrupted.kind === "focus" && !sc.pending && !sc.unembedded);
    }
  }
  function renderAwake(p) {
    scanAwake.hidden = !(p && p.running && p.awake);
  }

  // The health line: items on disk vs scanned vs searchable vs checked for faces, and one button for the gap.
  var FIX_LABEL = { continue: "Continue scan", rescan: "Scan the new files", faces: "Detect faces now", focus: "Finish the focus check" };
  var healthFix = null;
  function renderScanHealth(h) {
    if (!h) { scanHealthText.textContent = state.folder && state.folder.root ? "Checking the folder…" : ""; scanFix.hidden = true; healthFix = null; return; }
    if (h.running) { scanHealthText.textContent = ""; scanFix.hidden = true; healthFix = null; return; }   // the readout above says it all
    var bits = [];
    if (h.on_disk !== null && h.on_disk !== undefined) bits.push(fmtN(h.on_disk) + " on the disk");
    else if (!h.mounted) bits.push("disk not connected");
    bits.push(fmtN(h.scanned) + " scanned");
    bits.push(fmtN(h.embedded) + " searchable");
    if (h.faces || h.faced) bits.push(fmtN(h.faced) + " checked for faces");
    else if (h.faces_pending) bits.push(fmtN(h.faces_pending) + " not checked for faces");
    var text = bits.join(" · ");
    if (h.new_on_disk) text += ". " + fmtN(h.new_on_disk) + " new " + (h.new_on_disk === 1 ? "file" : "files") + " on the disk the scan has not seen";
    else if (h.errors) text += ". " + fmtN(h.errors) + " could not be read";
    if (!h.fix) text += ". Everything on the disk is scanned and searchable";
    scanHealthText.textContent = text + ".";
    healthFix = h.fix;
    scanFix.hidden = !healthFix || (healthFix === "continue" && !scanResume.hidden)    // the resume block already carries Continue
      || (healthFix === "rescan" && !h.scanned);                                       // a never-scanned folder has Scan this folder right above
    scanFix.textContent = FIX_LABEL[healthFix] || "";
    scanFix.classList.toggle("primary", healthFix === "continue");
  }
  function loadScanHealth() {
    if (!(state.folder && state.folder.root)) { renderScanHealth(null); return Promise.resolve(); }
    renderScanHealth(null);
    return api("/api/scan/health").then(function (h) {
      if (h && typeof h.complete === "boolean") { state.scan = h; renderScanBar(); }
      renderScanHealth(h);
    }).catch(function () { scanHealthText.textContent = ""; scanFix.hidden = true; });
  }
  scanFix.addEventListener("click", function () {
    if (healthFix === "continue") return continueScan();
    if (healthFix === "rescan") return startIndexJob({ faces: $("#faces").checked, retry_errors: false }, "scanning the new files…");
    if (healthFix === "faces") return scanFacesNow();
    if (healthFix === "focus") return checkFocusBtn.click();
  });
  // ===== end resume =====

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

  // ===== the project file: sorted_<shoot>.sorted. The card on the Scan tab shows the name, where Save
  // writes (a sorted folder beside the shoot, or the folder picked last time) and when it was last saved.
  // Save project (command S) needs no picker; Save to runs the folder picker; Open project runs the file picker.
  // A double-click on a project file in the Finder reaches the page through GET /api/launch at boot. =====
  var projectName = $("#project-name"), projectDir = $("#project-dir"), projectState = $("#project-state");
  var projectReveal = $("#bundle-reveal");
  state.project = null;
  function revealPath(path) {
    return api("/api/reveal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path }) })
      .catch(function (err) { setStatus("could not show it in the Finder: " + err.message, true); });
  }
  function shortDir(path) {
    // /Volumes/Prod_02/sorted reads as Prod_02 › sorted; /Users/me/Documents/sorted as Documents › sorted
    var parts = (path || "").split("/").filter(Boolean);
    if (parts[0] === "Volumes") parts = parts.slice(1);
    else if (parts[0] === "Users" && parts.length > 2) parts = parts.slice(2);
    if (parts.length > 3) parts = ["…"].concat(parts.slice(-3));   // the folder name is the part that matters
    return parts.length ? parts.join(" › ") : path;
  }
  function renderProject(info) {
    state.project = info || null;
    if (!info) { projectName.textContent = ""; projectDir.textContent = ""; projectState.textContent = ""; projectReveal.hidden = true; return; }
    projectName.textContent = info.name;
    projectDir.textContent = shortDir(info.dir); projectDir.title = info.dir;
    projectReveal.hidden = !info.exists;
    projectState.className = "project-state";
    if (info.exists && info.saved_at) {
      projectState.textContent = "Saved " + info.saved_at.slice(0, 16);
      projectState.classList.add("saved");
    } else if (info.exists) {
      projectState.textContent = "Opened from this file";
      projectState.classList.add("saved");
    } else if (info.path) {
      projectState.textContent = "The saved file is not there any more (was " + info.path + "). Save again.";
      projectState.classList.add("stale");
    } else {
      projectState.textContent = "Not saved yet";
    }
  }
  function loadProject() {
    if (!(state.folder && state.folder.root)) { renderProject(null); return Promise.resolve(null); }
    return api("/api/project").then(renderProject).catch(function () { renderProject(null); });
  }
  function saveProject() {
    if (!(state.folder && state.folder.root)) { setStatus("open a shoot first"); return; }
    setStatus("saving the project…", true);
    api("/api/project/save", { method: "POST" }).then(function () {
      pollExportProgress("saving the project");
    }).catch(function (err) { setStatus("could not save the project: " + err.message, true); });
  }
  $("#project-save").addEventListener("click", saveProject);
  projectReveal.addEventListener("click", function () { if (state.project && state.project.path) revealPath(state.project.path); });
  $("#bundle-export").addEventListener("click", function () {
    setStatus("waiting for the folder picker…", true);
    pickerPost("/api/bundle/export/choose").then(function (res) {
      if (!res) { setStatus("save cancelled"); return; }
      setStatus("saving the project…", true);
      pollExportProgress("saving the project");
    }).catch(function (err) { setStatus("could not save the project: " + err.message, true); });
  });
  // command S saves the project from anywhere in the app (the browser's own Save page is never what anyone wants here)
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (state.folder && state.folder.root) saveProject();
    }
  });

  function openedProject(info) {
    applyFolderInfo(info);
    // ===== resume: a project file of a half-scanned shoot says so, and the bar under the search offers Continue =====
    if (info.scan && !info.scan.complete) {
      setStatus("opened " + (info.name || info.root) + ": " + scanSummary(info.scan) + ". This project is incomplete; press Continue scan with the disk connected.", true);
    } else {
      setStatus("opened " + (info.name || info.root) + ": " + info.photos + " photos, ready", true);
    }
    // ===== end resume =====
    settleFolder(info);
  }
  function importBundle() {
    setStatus("waiting for the file picker…", true);
    pickerPost("/api/bundle/import/choose").then(function (res) {
      if (!res) { setStatus("open cancelled"); return null; }
      if (!res.needs_root) return res;
      // Made on a Mac where the disk sat under another path: ask for the folder, then install under it.
      setStatus("that project was made for " + res.bundle.root + ", which is not here; pick the photo folder", true);
      return pickerPost("/api/bundle/import/choose-root", { zip: res.zip }).then(function (r2) {
        if (!r2) { setStatus("open cancelled, nothing was changed"); return null; }
        return r2;
      });
    }).then(function (info) {
      if (info) openedProject(info);
    }).catch(function (err) {
      setStatus("could not open the project: " + err.message, true);
    });
  }
  $("#bundle-import").addEventListener("click", importBundle);
  // The file a double-click handed the app: the same load as Open project, with the folder picker only when
  // the shoot sits somewhere else on this Mac.
  function openProjectFile(zip) {
    setStatus("opening " + zip.split("/").pop() + "…", true);
    return pickerPost("/api/bundle/import", { zip: zip }).then(function (info) {
      if (info) openedProject(info);
    }).catch(function (err) {
      if (err.status === 400 && /not here/.test(err.message || "")) {
        setStatus(err.message, true);
        return pickerPost("/api/bundle/import/choose-root", { zip: zip }).then(function (r2) {
          if (!r2) { setStatus("open cancelled, nothing was changed"); return; }
          openedProject(r2);
        }).catch(function (e2) { setStatus("could not open the project: " + e2.message, true); });
      }
      setStatus("could not open the project: " + err.message, true);
      return null;
    });
  }
  // ===== end project file =====

  // ===== welcome: the two buttons, the faces tick, the How it works link =====
  // Scan a disk or folder: the folder picker; a folder with nothing scanned yet starts the scan at once with the
  // welcome's faces choice (mirrored into the Scan tab's box); a scanned one just opens. Open a project file: importBundle.
  var welcomeFaces = $("#welcome-faces");
  $("#welcome-scan").addEventListener("click", function () {
    openFolderPicker().then(function (info) {
      if (!info || !folderOpen(info) || info.indexed) return;
      $("#faces").checked = welcomeFaces.checked;
      startIndexJob({ faces: welcomeFaces.checked, retry_errors: false }, "scan started…");
    });
  });
  $("#welcome-load").addEventListener("click", importBundle);
  $("#welcome-help").addEventListener("click", function () { setHelp(true); });
  // ===== end welcome =====

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
    if (open) loadVersion();                           // ===== version: the number, the build, what the daily check found =====
  }

  // ===== version and the opt-in update check: /api/version says what runs here and, when the box is ticked, what the site
  // says is out; the box posts /api/version/check (on: one check now, and one a day from then; off: forgotten). =====
  var appVersionEl = $("#app-version");
  var updateBox = $("#update-check");
  var updateLine = $("#update-line");
  var versionQuiet = false;
  function renderVersion(st) {
    appVersionEl.textContent = st.version + (st.build ? " (" + st.build + ")" : "");
    appVersionEl.title = st.build ? "version " + st.version + ", build " + st.build : "version " + st.version;
    versionQuiet = true; updateBox.checked = !!st.enabled; versionQuiet = false;
    updateLine.className = "help-out";
    updateLine.innerHTML = "";
    if (!st.enabled) { updateLine.hidden = true; return; }
    updateLine.hidden = false;
    if (st.newer) {
      updateLine.classList.add("update-new");
      var strong = document.createElement("strong"); strong.textContent = st.latest + " is out";
      updateLine.appendChild(strong);
      if (st.notes) updateLine.appendChild(document.createTextNode(": " + st.notes + " "));
      else updateLine.appendChild(document.createTextNode(". "));
      var a = document.createElement("a"); a.href = st.download_url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Get it from the download page";
      updateLine.appendChild(a);
    } else if (st.checking && !st.checked_at) {
      updateLine.textContent = "checking…";
    } else if (st.error && !st.latest) {
      updateLine.textContent = "could not reach the site (" + st.error + "); it tries again tomorrow";
      updateLine.classList.add("err");
    } else if (st.latest) {
      updateLine.textContent = "this is the newest version, checked " + fmtWhen(st.checked_at);
    } else {
      updateLine.textContent = "checking…";
    }
  }
  function loadVersion() {
    return api("/api/version").then(function (st) {
      renderVersion(st);
      if (st.checking && !helpEl.hidden) setTimeout(function () { if (!helpEl.hidden) loadVersion(); }, 2500);
    }).catch(function () { appVersionEl.textContent = "?"; });
  }
  updateBox.addEventListener("change", function () {
    if (versionQuiet) return;
    var on = updateBox.checked;
    updateLine.hidden = !on; updateLine.className = "help-out"; updateLine.textContent = on ? "checking…" : "";
    api("/api/version/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) })
      .then(renderVersion)
      .catch(function (err) { setStatus("could not change the version check: " + err.message, true); loadVersion(); });
  });
  loadVersion();                                       // the line under the wordmark is right from the start
  // ===== end version =====
  helpToggle.addEventListener("click", function () { setHelp(helpEl.hidden); });
  helpClose.addEventListener("click", function () { setHelp(false); });
  document.addEventListener("click", function (e) {
    if (helpEl.hidden) return;
    if (helpEl.contains(e.target) || helpToggle.contains(e.target)) return;
    if (e.target.closest && e.target.closest("#welcome-help")) return;   // ===== welcome: its link opens the panel =====
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
  // Safari drops the click's activation across an await, so where ClipboardItem takes a promise the write
  // starts inside the gesture and the text arrives later; elsewhere the text is fetched first.
  function copySummary() {
    var textP = api("/api/usage/summary").then(function (s) { return s.text || JSON.stringify(s, null, 2); });
    if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
      try {
        var item = new ClipboardItem({ "text/plain": textP.then(function (t) { return new Blob([t], { type: "text/plain" }); }) });
        return navigator.clipboard.write([item]);
      } catch (e) { /* a ClipboardItem that takes no promise: fall through */ }
    }
    return textP.then(copyText);
  }
  if (fbCopy) fbCopy.addEventListener("click", function () {
    fbCopy.disabled = true;
    flushUsage();
    copySummary().then(function () {
      fbSay("Summary copied, paste it into the email");
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

  // ===== optional faces: "N need faces" in the title, the Scan tab's health line ("Detect faces now"), the People tab link =====
  // /api/stats.faces_pending counts rows indexed with faces off. POST /api/index {faces: true} runs faces
  // only on those rows (the server skips everything else), with the usual index progress.
  var facesPendingRow = $("#faces-pending-row");
  function renderFacesPending(s) {
    var n = (s && s.faces_pending) || 0;
    state.facesPending = n;
    facesPendingRow.hidden = !(n > 0);
    $("#faces-pending-text").textContent = n + (n === 1 ? " photo" : " photos") + " not scanned for faces yet,";
  }
  function scanFacesNow() {
    startIndexJob({ faces: true }, "finding faces in " + state.facesPending + " photo(s)…");
    goView("index");                                   // ===== navigation: a tab switch, so a history entry =====
  }
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
  // ===== project file: a double-click in the Finder started the app with a file; open it first =====
  api("/api/launch").then(function (l) { if (l && l.open) openProjectFile(l.open); }).catch(function () { /* older server */ });
  // ===== end project file =====
  loadFolder().then(function (info) {
    loadRecent();
    if (!folderOpen(info)) {
      showView(state.view);
      return;
    }
    loadProject();                                     // ===== project file: the card, whatever tab opens =====
    // ===== navigation boot: the view and filter come back from history.state (survives a reload) or the hash =====
    var entry = navBootEntry();
    if (entry) state.view = entry.view;
    var chips = entry ? entry.chips || [] : [];
    // ===== end navigation boot =====
    if (!info.indexed) {
      showView("index");
      navPush(true);                                   // ===== navigation: the boot entry is a replace, never a push =====
      var btn = $("#start-index");
      if (btn) btn.focus();
      loadStats().then(function (s) { if (s.indexing) pollProgress(); });   // first scan of a fresh folder, page reloaded mid-run
      return;
    }
    loadStats().then(function (s) { if (s.indexing) pollProgress(); });
    loadPeople();
    loadCategories();                                  // fills the count beside the Categories nav row
    loadSearches();                                    // ===== search history: the sidebar chips =====
    loadPrefs(); loadExports();                        // ===== export presets and history =====
    // ===== navigation boot: a person filter goes out explicitly, the select has no options yet; a saved name is a find =====
    applyChips(chips);
    var savedChip = chipOf("saved", chips);
    if (savedChip) { if (state.view === "search") findSaved(savedChip.key); else pendingFind = savedChip.key; }
    else runSearch(ctxParams(chips));
    showView(state.view);
    navPush(true);
    // ===== end navigation boot =====
  });
})();
