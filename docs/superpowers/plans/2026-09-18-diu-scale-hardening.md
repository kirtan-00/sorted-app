# Diu Scale Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing photosort pipeline survive a 20,000-photo shoot on an unpluggable disk without ever costing Kirtan a re-run, and prove it with a synthetic scale gate before the real Diu disk is touched.

**Architecture:** No new subsystems. Each task hardens one existing seam (index run, export, folder open, search paging, UI) in the file that already owns it, keeps the SQLite index under `~/Library/Application Support/photosort/<slug>/` as the single durable store, and adds a `slow`-marked scale test driven by a generator script. Server work stays in `server.py` endpoints + `app.js` handlers, mirroring the existing index/classify background-job pattern (thread + `state[...]` + `/api/.../progress` polled at 800 ms).

**Tech Stack:** Python 3.11, FastAPI, SQLite (WAL), Pillow, numpy, scikit-learn DBSCAN, vanilla JS UI, pytest. macOS `caffeinate`.

**Spec:** `docs/superpowers/specs/2026-09-18-diu-scale-hardening-design.md`

## Scope ruling (Kirtan, 2026-09-18)

Execute Tasks 1, 3, 4, 5 only. Task 2 (sleep guard) is dropped for now. Task 7 (videos) is dropped: videos are not counted or shown anywhere. Tasks 6, 8, 9, 10 wait for a later go. Task 3 therefore edits `index_folder` directly (no `_index_folder` rename) and adds no `videos` field; Task 5 has no `sleep_guard` in the export thread.

## Global Constraints

- The shoot root is READ-ONLY. Nothing is created, moved, renamed or deleted under it. Every test that touches a source folder asserts its listing is unchanged afterwards.
- Index + thumbs live under `app_home()` (`PHOTOSORT_HOME` in tests). Exports go under `export_root()` (`PHOTOSORT_EXPORT_DIR` in tests). `tests/conftest.py` isolates both automatically.
- Never run `python -m photosort.cli serve` from the assistant's shell while Kirtan has the app open (the launcher `pkill`s by pattern). Tests use `TestClient` only.
- No em dashes and no `--` in UI text, README, docs, commit messages, or CLI help strings. Use commas, colons, or "to".
- Do not touch `classify.apply_on_disk`, the classify thresholds, or the `models/` directory.
- Match existing style: one file per concern, short docstrings, `from conftest import make_image` inside tests, `index_folder(tmp_path, faces=False, workers=1, embed=False)` for fast fixtures.
- Run the full suite with `cd ~/Desktop/photosort && .venv/bin/python -m pytest -q` before every commit. Baseline: 60 passed, 1 skipped.
- Commit after every task with a conventional prefix (`fix:`, `feat:`, `test:`, `docs:`) and the trailer `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- The manual 3k x 24 MP run in Task 10 runs only on mains power (`pmset -g batt` must say AC Power).

---

### Task 1: Unmounted root must not wipe the index

**Files:**
- Modify: `photosort/index.py:49-63` (`index_folder` scan section)
- Modify: `photosort/db.py:98-109` (`known_files`, add `missing_files`, `restore_missing`)
- Modify: `tests/test_index.py:26-37` (`test_missing_then_restored` rewrite)
- Modify: `tests/test_export.py:50-57` (`test_export_skips_missing_photos` keeps a second photo)
- Test: `tests/test_index.py`

**Interfaces:**
- Produces: `class SourceUnavailable(RuntimeError)` in `photosort/index.py`. Raised by `index_folder` before any DB write when the root is not a directory, or when the walk finds zero images while the DB holds at least one `status='ok'` photo.
- Produces: `db.missing_files(conn) -> dict[str, tuple[int, float, str]]` mapping rel to `(size, mtime, qhash)` for `status='missing'` rows.
- Produces: `db.restore_missing(conn, rels: list[str]) -> None` flips those rows back to `status='ok'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_index.py`:

```python
def test_unmounted_root_refuses_and_keeps_index(tmp_path):
    """The disk got unplugged: the root vanishes. Indexing must refuse, not mark everything missing."""
    import shutil, pytest
    from conftest import make_image
    from photosort.index import SourceUnavailable
    shoot = tmp_path / "shoot"; shoot.mkdir()
    make_image(shoot, "a.jpg", seed=1); make_image(shoot, "b.jpg", seed=2)
    index_folder(shoot, faces=False, workers=1, embed=False)
    parked = tmp_path / "parked"; shutil.move(str(shoot), str(parked))
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False)
    conn = db.connect(shoot)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 2
    # mounted again but empty (wrong disk, or a bad eject left an empty mount point): still refuse
    shoot.mkdir()
    with pytest.raises(SourceUnavailable):
        index_folder(shoot, faces=False, workers=1, embed=False)
    assert conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0] == 2

def test_empty_new_folder_indexes_to_zero(tmp_path):
    """A brand-new empty folder is not an error; there is nothing to protect."""
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["total"] == 0 and s["indexed"] == 0
```

Rewrite the existing `test_missing_then_restored`: it must keep a second photo in the folder (with the guard, deleting the only photo now refuses instead of marking it missing), and the restored file (same size + mtime, thumb still on the Mac) is NOT re-processed:

```python
def test_missing_then_restored(tmp_path):
    from conftest import make_image
    import os, shutil
    p = make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=2)   # b.jpg stays, so the folder is never empty
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    st = p.stat(); backup = tmp_path.parent / "a_backup.jpg"; shutil.copy2(p, backup); p.unlink()
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "missing"
    shutil.copy2(backup, p); os.utime(p, (st.st_atime, st.st_mtime))
    s = index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert s["indexed"] == 0 and s["skipped"] == 2          # restored from the saved index, no re-decode
    assert conn.execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "ok"
```

`tests/test_export.py::test_export_skips_missing_photos` also empties its folder; give it a second photo that stays:

```python
def test_export_skips_missing_photos(tmp_path):
    from photosort import db
    from conftest import make_image
    ids = _one_photo(tmp_path)
    make_image(tmp_path, "b.jpg", seed=2); index_folder(tmp_path, faces=False, workers=1, embed=False)
    (tmp_path / "a.jpg").unlink()
    index_folder(tmp_path, faces=False, workers=1, embed=False)   # marks a.jpg missing, b.jpg keeps the folder non-empty
    assert db.connect(tmp_path).execute("SELECT status FROM photos WHERE rel='a.jpg'").fetchone()[0] == "missing"
    out = export_ids(tmp_path, ids, "culled")                    # must not raise
    assert out.is_dir() and list(out.iterdir()) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_index.py tests/test_export.py -q`
Expected: `test_unmounted_root_refuses_and_keeps_index` fails with `ImportError: cannot import name 'SourceUnavailable'`; `test_missing_then_restored` fails on `s["indexed"] == 0`.

- [ ] **Step 3: Add the db helpers**

In `photosort/db.py`, replace `known_files` and add two functions:

```python
def known_files(conn, retry_errors: bool = False) -> dict[str, tuple[int, float]]:
    """rel -> (size, mtime) for rows that count as already indexed. Missing rows are excluded here
    and handled by missing_files() so a returning file can be restored without a re-decode."""
    q = "SELECT rel, size, mtime FROM photos WHERE status != 'missing'"
    if retry_errors:
        q += " AND status != 'error'"
    return {r[0]: (r[1], r[2]) for r in conn.execute(q)}

def missing_files(conn) -> dict[str, tuple[int, float, str]]:
    """rel -> (size, mtime, qhash) for rows the last scan could not find."""
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT rel, size, mtime, qhash FROM photos WHERE status='missing'")}

def restore_missing(conn, rels: list[str]) -> None:
    conn.executemany("UPDATE photos SET status='ok' WHERE rel=? AND status='missing'", [(r,) for r in rels])
    conn.commit()
```

- [ ] **Step 4: Guard the scan in index_folder**

In `photosort/index.py`, add after the imports:

```python
class SourceUnavailable(RuntimeError):
    """The shoot root is not there (disk unplugged, wrong mount) while the index already holds photos.
    Raised before any write so the saved index is left exactly as it was."""
```

Replace the block from `conn = db.connect(root)` through `db.mark_missing(...)` with:

```python
    conn = db.connect(root)
    notify({"stage": "scan", "done": 0, "total": 0})
    n_ok = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
    if not root.is_dir():
        raise SourceUnavailable(f"{root} is not there. Plug the disk in; the saved index ({n_ok} photos) was left untouched.")
    files = find_images(root)
    if not files and n_ok > 0:
        raise SourceUnavailable(f"{root} has no photos right now. Is the disk mounted? The saved index ({n_ok} photos) was left untouched.")
    known = db.known_files(conn, retry_errors=retry_errors)
    missing = db.missing_files(conn)
    idx = db.index_dir(root)
    # A file that went missing and came back unchanged, with its thumb still on the Mac, needs no re-decode.
    restore = [f.rel for f in files if f.rel in missing and missing[f.rel][:2] == (f.size, f.mtime)
               and (idx / "thumbs" / f"{missing[f.rel][2]}.jpg").is_file()]
    if restore:
        db.restore_missing(conn, restore)
        known.update({r: missing[r][:2] for r in restore})
    need_faces = db.photos_without_faces(conn) if faces else set()
    todo = [f for f in files if known.get(f.rel) != (f.size, f.mtime) or f.rel in need_faces]
    stats = dict(total=len(files), skipped=len(files) - len(todo), indexed=0, errors=0, embedded=0)
    db.mark_missing(conn, {f.rel for f in files})
```

Note `idx = db.index_dir(root)` already exists further down in the embed block; delete that later duplicate line.

- [ ] **Step 5: Surface the message cleanly in the server**

In `photosort/server.py` `_run`, change the except:

```python
        except Exception as e:
            from .index import SourceUnavailable
            msg = str(e) if isinstance(e, SourceUnavailable) else f"{type(e).__name__}: {e}"
            state["progress"] = {"stage": "error", "error": msg, "done": 0, "total": 0}
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (62 passed, 1 skipped).

- [ ] **Step 7: Commit**

```bash
git add photosort/index.py photosort/db.py photosort/server.py tests/test_index.py tests/test_export.py
git commit -m "fix: refuse to index an unmounted or empty root, restore returning files without a re-decode

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Sleep guard for the length of an index run

**Files:**
- Modify: `photosort/index.py` (`index_folder`)
- Modify: `photosort/cli.py` (`cmd_index`)
- Modify: `README.md` (the "How to start" section)
- Modify: `photosort/ui/index.html:93-98` (Index tab)
- Test: `tests/test_index.py`

**Interfaces:**
- Produces: `index.sleep_guard() -> subprocess.Popen | None` context helper; spawns `caffeinate -i -w <pid>` when the binary exists, else `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_index.py`:

```python
def test_index_spawns_caffeinate_when_available(tmp_path, monkeypatch):
    import subprocess, photosort.index as ix
    from conftest import make_image
    calls = []
    class FakeProc:
        def terminate(self): calls.append("terminate")
        def wait(self, timeout=None): pass
    monkeypatch.setattr(ix.shutil, "which", lambda name: "/usr/bin/caffeinate" if name == "caffeinate" else None)
    monkeypatch.setattr(ix.subprocess, "Popen", lambda args, **kw: (calls.append(args), FakeProc())[1])
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    assert calls[0][:2] == ["/usr/bin/caffeinate", "-i"] and calls[0][2] == "-w"
    assert calls[-1] == "terminate"

def test_index_without_caffeinate_still_runs(tmp_path, monkeypatch):
    import photosort.index as ix
    from conftest import make_image
    monkeypatch.setattr(ix.shutil, "which", lambda name: None)
    make_image(tmp_path, "a.jpg")
    assert index_folder(tmp_path, faces=False, workers=1, embed=False)["indexed"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_index.py -q -k caffeinate`
Expected: `AttributeError: module 'photosort.index' has no attribute 'shutil'`.

- [ ] **Step 3: Implement**

In `photosort/index.py` add `import os, shutil, subprocess` to the first import line and add:

```python
def sleep_guard():
    """Keep the Mac from idle-sleeping while we run. caffeinate -w exits by itself when this pid does.
    Lid-close on battery still sleeps the machine; nothing in software prevents that."""
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        return subprocess.Popen([exe, "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
```

Wrap the body of `index_folder` after `t0 = time.time(); root = Path(root)`:

```python
    guard = sleep_guard()
    try:
        return _index_folder(root, faces, workers, progress, embed, retry_errors, t0)
    finally:
        if guard is not None:
            guard.terminate()
            try: guard.wait(timeout=2)
            except Exception: pass
```

and rename the existing body to `def _index_folder(root, faces, workers, progress, embed, retry_errors, t0) -> dict:` (drop its own `t0 = ...; root = Path(root)` line).

- [ ] **Step 4: CLI message and copy**

The launcher is NOT wrapped in caffeinate: that would block idle sleep for hours of idle curation on battery. The guard lives inside the runs (index here, export in Task 5).

`photosort/cli.py` `cmd_index`: catch the guard so the CLI prints a sentence, not a traceback:

```python
def cmd_index(a):
    from .index import index_folder, SourceUnavailable
    try:
        s = index_folder(Path(a.folder), faces=not a.no_faces, workers=a.workers, progress=_progress, retry_errors=a.retry_errors)
    except SourceUnavailable as e:
        sys.exit(str(e))
    print(f"indexed {s['indexed']}  skipped {s['skipped']}  errors {s['errors']}  embedded {s['embedded']}  in {s['seconds']}s")
```

`photosort/ui/index.html`, inside `#view-index` after the `.row` div add:

```html
    <p class="mono hint">A big folder takes a while. Plug the Mac in and keep the lid open: the app stops idle sleep, but closing the lid on battery still puts the Mac to sleep and kills the run. If that happens, just press Index again; finished photos are skipped.</p>
```

`photosort/ui/style.css` append:

```css
.hint { color: #666; max-width: 60ch; font-size: 12px; }
```

`README.md`: under the start instructions add one line: `Indexing a big folder: plug in, lid open. Idle sleep is blocked while it runs; lid-close on battery is not.`

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add photosort/index.py photosort/cli.py README.md photosort/ui/index.html photosort/ui/style.css tests/test_index.py
git commit -m "feat: hold off idle sleep during an index run, say plainly what caffeinate cannot do

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Per-file errors and ETA

**Files:**
- Modify: `photosort/index.py` (progress payload)
- Modify: `photosort/server.py` (`stats`, new `/api/errors`, `IndexReq.retry_errors`)
- Modify: `photosort/ui/app.js` (`loadStats`, `formatProgress`, Index tab)
- Modify: `photosort/ui/index.html` (Index tab)
- Test: `tests/test_index.py`, `tests/test_server.py`

**Interfaces:**
- Produces: every progress dict from `index_folder` carries `stage_started` (epoch seconds when that stage began).
- Produces: `GET /api/stats` gains `errors: int`.
- Produces: `GET /api/errors -> {"errors": [{"rel": str, "indexed_at": str}]}` ordered by rel.
- Produces: `POST /api/index` body `{"faces": bool, "retry_errors": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_index.py`:

```python
def test_progress_carries_stage_start(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg")
    seen = []
    index_folder(tmp_path, faces=False, workers=1, embed=False, progress=seen.append)
    assert all("stage_started" in d for d in seen)
    feat = [d for d in seen if d["stage"] == "features"]
    assert feat and feat[0]["stage_started"] <= feat[-1]["stage_started"]
```

Append to `tests/test_server.py`:

```python
def _wait_idle(c, n=100):
    import time
    for _ in range(n):
        if not c.get("/api/progress").json()["running"]: return
        time.sleep(0.1)

def test_errors_listed_and_retryable(tmp_path):
    """A transient read failure on an unchanged file: a plain re-index leaves it alone (same size+mtime),
    retry_errors re-processes it."""
    from conftest import make_image
    from photosort import db
    make_image(tmp_path, "a.jpg"); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path); conn.execute("UPDATE photos SET status='error' WHERE rel='a.jpg'"); conn.commit()
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/stats").json()["errors"] == 1
    listed = c.get("/api/errors").json()["errors"]
    assert [e["rel"] for e in listed] == ["a.jpg"] and listed[0]["indexed_at"]
    assert c.post("/api/index", json={"faces": False}).json()["started"]; _wait_idle(c)
    assert c.get("/api/stats").json()["errors"] == 1          # unchanged file, not retried by default
    assert c.post("/api/index", json={"faces": False, "retry_errors": True}).json()["started"]; _wait_idle(c)
    assert c.get("/api/stats").json()["errors"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_index.py tests/test_server.py -q -k "stage_start or errors_listed"`
Expected: KeyError `stage_started`; KeyError `errors`.

- [ ] **Step 3: Progress payload**

In `index_folder`, replace `notify = progress or (lambda d: None)` with (`t0` is the existing `t0 = time.time()` at the top of the function):

```python
    _raw = progress or (lambda d: None)
    stage = {"name": None, "t": t0}
    def notify(d: dict) -> None:
        if d["stage"] != stage["name"]:
            stage["name"], stage["t"] = d["stage"], time.time()
        _raw(dict(d, stage_started=stage["t"]))
```

- [ ] **Step 4: Server**

`photosort/server.py`:

```python
class IndexReq(BaseModel):
    faces: bool = True
    retry_errors: bool = False
```

`_run(root_at_start, faces, retry_errors)` passes `retry_errors=retry_errors` to `index_folder`; `start_index` passes `req.retry_errors` in the thread args.

In `stats()` add to the dict: `errors=n("SELECT count(*) FROM photos WHERE status='error'"),` and to the no-root dict `errors=0`.

New endpoint after `/api/stats`:

```python
    @app.get("/api/errors")
    def errors():
        if state["root"] is None:
            return {"errors": []}
        conn = db.connect(state["root"])
        rows = conn.execute("SELECT rel, indexed_at FROM photos WHERE status='error' ORDER BY rel").fetchall()
        return {"errors": [{"rel": r[0], "indexed_at": r[1]} for r in rows]}
```

- [ ] **Step 5: UI**

`index.html`, Index tab `.row` gains a checkbox and the section gains an error list:

```html
      <label><input type="checkbox" id="retry-errors"> retry files that failed last time</label>
```
```html
    <div id="index-errors" hidden>
      <p class="mono" id="index-errors-title"></p>
      <pre id="index-errors-list" class="mono"></pre>
    </div>
```

`app.js`:

In `loadStats`, after the `indexing…` push: `if (s.errors) bits.push(s.errors + " failed");` and call `loadErrors()` when `s.errors` changed:

```javascript
  function loadErrors() {
    return api("/api/errors").then(function (data) {
      var list = (data && data.errors) || [];
      var box = $("#index-errors");
      box.hidden = list.length === 0;
      $("#index-errors-title").textContent = list.length + " file(s) could not be read. They are skipped; tick retry and Index again once fixed.";
      $("#index-errors-list").textContent = list.slice(0, 200).map(function (e) { return e.rel; }).join("\n") + (list.length > 200 ? "\n… " + (list.length - 200) + " more" : "");
    }).catch(function () { /* non-fatal */ });
  }
```

Call `loadErrors()` from `loadStats().then` and from `settleFolder`. Replace `formatProgress`:

```javascript
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
```

In the start-index click handler, body becomes `JSON.stringify({ faces: faces, retry_errors: $("#retry-errors").checked })`.

- [ ] **Step 6: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add photosort/index.py photosort/server.py photosort/ui/app.js photosort/ui/index.html tests/test_index.py tests/test_server.py
git commit -m "feat: list files that failed to index, retry them from the app, show rate and ETA

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Search paging and an ids-only endpoint

**Files:**
- Modify: `photosort/search.py:58-77` (`search`, add `query`)
- Modify: `photosort/server.py` (`/api/search`, new `/api/search/ids`)
- Modify: `photosort/ui/app.js` (`runSearch`, `renderGrid`, selection bar, `exportCategory`)
- Modify: `photosort/ui/index.html` (grid footer)
- Test: `tests/test_search.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `Index.query(text=None, image_id=None, filters=Filters()) -> list[dict]` (every match, sorted, no limit).
- Produces: `Index.search(..., limit=200, offset=0)` = `query(...)[offset:offset+limit]`.
- Produces: `GET /api/search` response `{"results": [...], "total": int, "offset": int, "limit": int}`.
- Produces: `GET /api/search/ids` (same filter params) response `{"ids": [int, ...], "total": int}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_search.py`:

```python
def test_search_offset_pages_through_query(tmp_path):
    from conftest import make_image
    from photosort.index import index_folder
    from photosort.search import Index
    for i in range(5): make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ix = Index(tmp_path)
    everything = ix.query()
    assert [r["rel"] for r in everything] == [f"p{i}.jpg" for i in range(5)]
    assert [r["rel"] for r in ix.search(limit=2, offset=0)] == ["p0.jpg", "p1.jpg"]
    assert [r["rel"] for r in ix.search(limit=2, offset=4)] == ["p4.jpg"]
    assert ix.search(limit=2, offset=99) == []
```

Append to `tests/test_server.py`:

```python
def test_search_total_and_ids_endpoint(tmp_path):
    from conftest import make_image
    for i in range(5): make_image(tmp_path, f"p{i}.jpg", seed=i)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    page = c.get("/api/search", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5 and page["offset"] == 2 and [r["rel"] for r in page["results"]] == ["p2.jpg", "p3.jpg"]
    ids = c.get("/api/search/ids").json()
    assert ids["total"] == 5 and len(ids["ids"]) == 5 and all(isinstance(i, int) for i in ids["ids"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_search.py tests/test_server.py -q -k "offset or total_and_ids"`
Expected: `AttributeError: 'Index' object has no attribute 'query'`; KeyError `total`.

- [ ] **Step 3: search.py**

Replace the `search` method with:

```python
    def query(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters()) -> list[dict]:
        """Every photo that passes the filters, sorted by similarity (text or image query) or by capture time."""
        person_ids = self._person_photo_ids(filters.person_id) if filters.person_id is not None else None
        cands = [p for p in self.photos.values() if self._passes(p, filters, person_ids)]
        if text or image_id is not None:
            if image_id is not None:
                i = self.pos.get(image_id)
                if i is None:
                    raise LookupError(f"no embedding for photo {image_id}")
                q = self.M[i]
            else:
                from .embed import get_embedder
                q = get_embedder().encode_text([text])[0]
            scores = self.M @ q
            for p in cands:
                i = self.pos.get(p["id"]); p["score"] = float(scores[i]) if i is not None else -1.0
            cands.sort(key=lambda p: -p["score"])
        else:
            for p in cands: p["score"] = 0.0
            cands.sort(key=lambda p: ((p["taken_at"] or "~"), p["rel"]))
        return cands

    def search(self, text: str | None = None, image_id: int | None = None, filters: Filters = Filters(),
               limit: int = 200, offset: int = 0) -> list[dict]:
        return [dict(p) for p in self.query(text, image_id, filters)[offset:offset + limit]]
```

- [ ] **Step 4: server.py**

Replace the `/api/search` endpoint and add `/api/search/ids`:

```python
    def _filters(sharp, faces, person, taken_from, taken_to, category) -> Filters:
        return Filters(sharp_min_pct=sharp, faces=faces or None, person_id=person, taken_from=taken_from,
                       taken_to=taken_to, category=category or None)

    @app.get("/api/search")
    def search(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
               person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
               category: str | None = None, limit: int = 200, offset: int = 0):
        if state["root"] is None:
            return {"results": [], "total": 0, "offset": 0, "limit": limit}
        limit = max(1, min(limit, 1000)); offset = max(0, offset)
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_filters(sharp, faces, person, taken_from, taken_to, category))
        except LookupError as e:
            raise HTTPException(404, str(e))
        return {"results": [dict(p) for p in rows[offset:offset + limit]], "total": len(rows), "offset": offset, "limit": limit}

    @app.get("/api/search/ids")
    def search_ids(q: str | None = None, image_id: int | None = None, sharp: float | None = None, faces: str | None = None,
                   person: int | None = None, taken_from: str | None = None, taken_to: str | None = None,
                   category: str | None = None):
        if state["root"] is None:
            return {"ids": [], "total": 0}
        try:
            rows = ix().query(text=q or None, image_id=image_id, filters=_filters(sharp, faces, person, taken_from, taken_to, category))
        except LookupError as e:
            raise HTTPException(404, str(e))
        return {"ids": [p["id"] for p in rows], "total": len(rows)}
```

Drop the old `cat_supported` dance; `Filters` has `category`.

- [ ] **Step 5: UI**

`index.html`: after `<div id="grid" class="grid"></div>` add:

```html
    <div id="more-row" class="row" hidden>
      <span id="shown-count" class="mono"></span>
      <button type="button" id="show-more">Show more</button>
      <button type="button" id="select-matching">Select all matching</button>
    </div>
```

`app.js` state gains `total: 0, offset: 0, lastParams: {}`. Replace `runSearch`:

```javascript
  var PAGE = 200;
  function runSearch(extra, append) {
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
```

In `renderGrid`, at the end before `updateSelbar()`:

```javascript
    var more = $("#more-row");
    more.hidden = state.results.length === 0;
    $("#shown-count").textContent = state.results.length + " of " + state.total + " shown";
    $("#show-more").hidden = state.results.length >= state.total;
```

Note: `runSearch` is called with `extra` from `lbLike` (`{image_id}`), person clicks and category clicks; those still work because `extra` is merged into `lastParams` and re-sent by Show more. In `updateSelbar`, `shown` now reads `state.results.length` (unchanged).

Replace the fetch in `exportCategory`:

```javascript
    var qs = new URLSearchParams({ category: cat }).toString();
    return api("/api/search/ids?" + qs).then(function (data) {
      var ids = data.ids || [];
```

- [ ] **Step 6: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. `test_api` still passes because `res[0]` reads `results`.

- [ ] **Step 7: Commit**

```bash
git add photosort/search.py photosort/server.py photosort/ui/app.js photosort/ui/index.html tests/test_search.py tests/test_server.py
git commit -m "feat: page search results, select every match without fetching every row

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Export as a background job with a free-space check

**Files:**
- Modify: `photosort/export.py` (`export_ids` gains `progress`, add `export_bytes`)
- Modify: `photosort/server.py` (`/api/export` starts a job, new `/api/export/progress`)
- Modify: `photosort/ui/app.js` (export click, `exportCategory`, new `pollExportProgress`)
- Modify: `photosort/ui/index.html` (export mode option text)
- Modify: `tests/test_server.py:7-18` (`test_api` polls)
- Test: `tests/test_export.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `export.export_bytes(root, ids) -> int` total source bytes for those ids.
- Produces: `export.export_ids(root, ids, name, mode="copy", progress=None) -> Path`; `progress(dict(done, total, failed))` after each file; a per-file `OSError` is counted in `failed`, logged into `<out>/failed.txt`, and does not stop the run.
- Produces: `POST /api/export` -> `{"started": true, "total": n}` (400 when copy needs more than free space minus 1 GiB, 409 when a job is running).
- Produces: `GET /api/export/progress` -> `{"running": bool, "done": int, "total": int, "failed": int, "path": str|None, "error": str|None}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_export.py`:

```python
def test_export_reports_progress_and_survives_a_bad_file(tmp_path):
    from conftest import make_image
    from photosort.export import export_bytes
    from photosort import db
    make_image(tmp_path, "a.jpg", seed=1); make_image(tmp_path, "b.jpg", seed=2)
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    ids = [r["id"] for r in Index(tmp_path).search()]
    assert export_bytes(tmp_path, ids) == (tmp_path / "a.jpg").stat().st_size + (tmp_path / "b.jpg").stat().st_size
    # b.jpg vanishes from the disk after indexing: copy must finish a.jpg and report one failure
    (tmp_path / "b.jpg").unlink()
    seen = []
    out = export_ids(tmp_path, ids, "partial", "copy", progress=seen.append)
    assert (out / "a.jpg").is_file() and not (out / "b.jpg").exists()
    assert seen[-1] == {"done": 2, "total": 2, "failed": 1}
    assert "b.jpg" in (out / "failed.txt").read_text()
```

Replace the export lines in `tests/test_server.py::test_api`:

```python
    assert c.post("/api/export", json={"ids": [res[0]["id"]], "name": "t"}).json()["started"]
    for _ in range(100):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.05)
    assert p["error"] is None and p["done"] == 1
    ex = Path(p["path"])
    assert ex.is_dir() and (ex / "a.jpg").is_file() and not str(ex).startswith(str(tmp_path))
```

(add `import time` at the top of the file). Append:

```python
def test_export_refuses_when_disk_is_short(tmp_path, monkeypatch):
    from conftest import make_image
    import photosort.server as srv
    make_image(tmp_path, "a.jpg")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    pid = c.get("/api/search").json()["results"][0]["id"]
    class Usage: free = 10
    monkeypatch.setattr(srv.shutil, "disk_usage", lambda p: Usage)
    r = c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "copy"})
    assert r.status_code == 400 and "free" in r.json()["detail"]
    assert c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "symlink"}).json()["started"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_export.py tests/test_server.py -q`
Expected: ImportError `export_bytes`; KeyError `started`; AttributeError `shutil` on server.

- [ ] **Step 3: export.py**

```python
def export_bytes(root: Path, ids: list[int]) -> int:
    conn = db.connect(Path(root)); total = 0
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        total += conn.execute(f"SELECT COALESCE(SUM(size), 0) FROM photos WHERE id IN ({q}) AND status='ok'", chunk).fetchone()[0]
    return int(total)

def export_ids(root: Path, ids: list[int], name: str, mode: str = "copy", progress=None) -> Path:
    root = Path(root); out = export_dir(root, name)
    notify = progress or (lambda d: None)
    conn = db.connect(root)
    rows = []
    for i in range(0, len(ids), 900):            # chunk: SQLite caps bound variables
        chunk = ids[i:i + 900]; q = ",".join("?" * len(chunk))
        rows += conn.execute(f"SELECT id, rel, sharp, n_faces, taken_at FROM photos WHERE id IN ({q}) AND status='ok' ORDER BY id", chunk).fetchall()
    out.mkdir(parents=True, exist_ok=True)
    if mode == "csv":
        with open(out / "photos.csv", "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["id", "path", "sharp", "n_faces", "taken_at"])
            for r in rows: w.writerow([r["id"], str(root / r["rel"]), r["sharp"], r["n_faces"], r["taken_at"]])
        notify({"done": len(rows), "total": len(rows), "failed": 0})
        return out
    failed: list[str] = []
    for n, r in enumerate(rows, 1):
        src = root / r["rel"]; dst = out / Path(r["rel"]).name
        if dst.exists() or dst.is_symlink():
            dst = out / f"{r['id']}_{Path(r['rel']).name}"
        try:
            if mode == "copy": shutil.copy2(src, dst)
            else: os.symlink(src.resolve(), dst)
        except OSError as e:
            failed.append(f"{r['rel']}\t{e}")
        notify({"done": n, "total": len(rows), "failed": len(failed)})
    if failed:
        (out / "failed.txt").write_text("\n".join(failed) + "\n")
    return out
```

- [ ] **Step 4: server.py**

Add `import shutil` at the top and `from .export import export_ids, export_bytes`. Add to `state`: `"export": {"running": False, "done": 0, "total": 0, "failed": 0, "path": None, "error": None}`. Replace `/api/export`:

```python
    EXPORT_HEADROOM = 1 << 30   # keep 1 GiB free on the Mac after a copy

    @app.post("/api/export")
    def export(req: ExportReq):
        if state["root"] is None:
            raise HTTPException(400, "no folder open")
        if state["export"]["running"]:
            raise HTTPException(409, "an export is already running")
        if req.mode == "copy":
            from .config import export_root
            need = export_bytes(state["root"], req.ids)
            base = export_root(); base.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(base).free
            if need + EXPORT_HEADROOM > free:
                raise HTTPException(400, f"copy needs {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free on this Mac. Use links, or export fewer photos.")
        root_at_start = state["root"]
        state["export"] = {"running": True, "done": 0, "total": len(req.ids), "failed": 0, "path": None, "error": None}

        def prog(d):
            state["export"].update(d)

        def _run_export():
            try:
                state["export"]["path"] = str(export_ids(root_at_start, req.ids, req.name, req.mode, progress=prog))
            except Exception as e:
                state["export"]["error"] = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            finally:
                state["export"]["running"] = False

        try:
            from .export import export_dir
            export_dir(root_at_start, req.name)      # validate the name now so a bad one is a 400, not a background error
        except ValueError as e:
            state["export"]["running"] = False
            raise HTTPException(400, str(e))
        threading.Thread(target=_run_export, daemon=True).start()
        return {"started": True, "total": len(req.ids)}

    @app.get("/api/export/progress")
    def export_progress():
        return state["export"]
```

`_switch_root` also resets `state["export"]` to the idle dict.

`/api/export/people` stays synchronous in this pass: it is the people/groups/solo bundle, not the final Diu export, and Plan B's final export replaces it. Add one line to its docstring/comment saying so, and the UI button label becomes "Export people / groups / solo (small shoots)".

- [ ] **Step 5: UI**

`index.html` export mode options:

```html
        <option value="copy">copy to Desktop</option>
        <option value="symlink">links (need the disk plugged in to open)</option>
        <option value="csv">csv</option>
```

`app.js`: add a poller and use it from both export paths:

```javascript
  var exportTimer = null;
  function pollExportProgress(label) {
    if (exportTimer) clearInterval(exportTimer);
    exportTimer = setInterval(function () {
      api("/api/export/progress").then(function (p) {
        if (p.running) { setStatus(label + " " + p.done + "/" + p.total + (p.failed ? ", " + p.failed + " failed" : ""), true); return; }
        clearInterval(exportTimer); exportTimer = null;
        if (p.error) setStatus("export failed: " + p.error, true);
        else setStatus("exported " + (p.done - p.failed) + " of " + p.total + " to " + p.path + (p.failed ? " (" + p.failed + " failed, see failed.txt)" : ""), true);
      }).catch(function () { clearInterval(exportTimer); exportTimer = null; });
    }, 800);
  }
  function startExport(ids, name, mode, label) {
    return api("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: ids, name: name, mode: mode }),
    }).then(function () { pollExportProgress(label); })
      .catch(function (err) { setStatus("export failed: " + err.message, true); });
  }
```

The `#export` click handler body becomes `startExport(ids, name, mode, "exporting " + ids.length + " photo(s)");`. In `exportCategory`, the inner `api("/api/export", ...)` becomes `startExport(ids, "categories/" + cat, "symlink", "exporting " + cat)` and the trailing `.then(res => ...)` is removed.

- [ ] **Step 6: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add photosort/export.py photosort/server.py photosort/ui/app.js photosort/ui/index.html tests/test_export.py tests/test_server.py
git commit -m "feat: export runs in the background with progress, a free-space check and a failed list

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Saved shoots, open a shoot with the disk unplugged

**Files:**
- Modify: `photosort/db.py` (`get_meta`, `set_meta`, `list_shoots`)
- Modify: `photosort/index.py` (store `meta.root`)
- Modify: `photosort/server.py` (`_folder_info`, `set_folder`, `/api/shoots`, guards)
- Modify: `photosort/ui/app.js` (shoots select, offline banner)
- Modify: `photosort/ui/index.html`, `photosort/ui/style.css`
- Test: `tests/test_db.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `db.get_meta(conn, key) -> str | None`, `db.set_meta(conn, key, value)`.
- Produces: `db.list_shoots(recent: list[str] = ()) -> list[dict]` with keys `slug, root, name, photos, last_index, mounted`. Root comes from `meta.root`, else from a `recent` path whose slug matches, else `None`.
- Produces: `GET /api/folder` and `POST /api/folder` responses gain `mounted: bool`.
- Produces: `GET /api/shoots -> {"shoots": [...]}` sorted by `last_index` desc.
- `POST /api/index` returns 400 "disk not mounted" when `mounted` is false. `POST /api/export` returns 400 for `copy`/`symlink` when not mounted (`csv` allowed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_list_shoots_reads_root_from_meta_or_recent(tmp_path):
    from conftest import make_image
    from photosort.index import index_folder
    from photosort import db
    from photosort.config import shoot_slug
    a = tmp_path / "A"; a.mkdir(); make_image(a, "a.jpg")
    index_folder(a, faces=False, workers=1, embed=False)
    b = tmp_path / "B"; b.mkdir(); make_image(b, "b.jpg")
    index_folder(b, faces=False, workers=1, embed=False)
    conn = db.connect(b); conn.execute("DELETE FROM meta WHERE key='root'"); conn.commit()   # an index from before meta.root existed
    shoots = {s["slug"]: s for s in db.list_shoots(recent=[str(b)])}
    assert shoots[shoot_slug(a)]["root"] == str(a.resolve()) and shoots[shoot_slug(a)]["mounted"] is True and shoots[shoot_slug(a)]["photos"] == 1
    assert shoots[shoot_slug(b)]["root"] == str(b) and shoots[shoot_slug(b)]["name"] == "B"
    import shutil; shutil.rmtree(a)
    assert {s["slug"]: s for s in db.list_shoots()}[shoot_slug(a)]["mounted"] is False
```

Append to `tests/test_server.py`:

```python
def test_open_saved_shoot_with_disk_unplugged(tmp_path):
    from conftest import make_image
    import shutil
    shoot = tmp_path / "shoot"; shoot.mkdir(); make_image(shoot, "a.jpg")
    index_folder(shoot, faces=False, workers=1, embed=False)
    c = TestClient(create_app(shoot))
    pid = c.get("/api/search").json()["results"][0]["id"]
    shutil.rmtree(shoot)                                   # disk unplugged
    info = c.post("/api/folder", json={"path": str(shoot)}).json()
    assert info["mounted"] is False and info["indexed"] is True
    assert c.get("/api/search").json()["total"] == 1     # browsing still works from the saved index
    assert c.get("/api/thumb/" + c.get("/api/search").json()["results"][0]["qhash"]).status_code == 200
    assert c.post("/api/index", json={"faces": False}).status_code == 400
    assert c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "copy"}).status_code == 400
    assert c.post("/api/export", json={"ids": [pid], "name": "t", "mode": "csv"}).json()["started"]
    shoots = c.get("/api/shoots").json()["shoots"]
    assert shoots[0]["root"] == str(shoot.resolve()) and shoots[0]["mounted"] is False
    assert c.post("/api/folder", json={"path": str(tmp_path / "never-indexed")}).status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_db.py tests/test_server.py -q -k "shoots or unplugged"`
Expected: AttributeError `list_shoots`; 400 on the unmounted `/api/folder` POST.

- [ ] **Step 3: db.py**

```python
def get_meta(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None

def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value)); conn.commit()

def list_shoots(recent: list[str] = ()) -> list[dict]:
    """Every index on this Mac. root comes from meta.root (written at index time), else from a
    recent-folders path whose slug matches (indexes made before meta.root existed)."""
    home = app_home()
    if not home.is_dir():
        return []
    by_slug = {shoot_slug(Path(p)): p for p in recent}
    out = []
    for d in home.iterdir():
        if not (d / DB_NAME).is_file():
            continue
        conn = sqlite3.connect(d / DB_NAME, timeout=30)
        try:
            root = get_meta(conn, "root") or by_slug.get(d.name)
            photos = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
            last = get_meta(conn, "last_index")
        finally:
            conn.close()
        name = Path(root).name if root else d.name.rsplit("-", 1)[0]
        out.append(dict(slug=d.name, root=root, name=name, photos=photos, last_index=last,
                        mounted=bool(root) and Path(root).is_dir()))
    out.sort(key=lambda s: s["last_index"] or "", reverse=True)
    return out
```

(`get_meta` on a fresh connection needs the `meta` table to exist; it does for any dir with `index.db` written by `connect`.)

- [ ] **Step 4: index.py**

In `_index_folder`, right after `conn = db.connect(root)`: `db.set_meta(conn, "root", str(root.resolve()))`. Note the guard in Task 1 raises before this line only for the `not root.is_dir()` case; move `set_meta` to after the `SourceUnavailable` checks so an unmounted path never overwrites a good root.

- [ ] **Step 5: server.py**

```python
    def _folder_info() -> dict:
        r = state["root"]
        if r is None:
            return {"root": None, "name": None, "indexed": False, "mounted": False}
        conn = db.connect(r)
        n = conn.execute("SELECT count(*) FROM photos WHERE status='ok'").fetchone()[0]
        return {"root": str(r), "name": r.name or str(r), "indexed": n > 0, "mounted": r.is_dir()}

    def _has_index(p: Path) -> bool:
        from .config import shoot_slug, DB_NAME
        return (app_home() / shoot_slug(p) / DB_NAME).is_file()
```

`set_folder`:

```python
    @app.post("/api/folder")
    def set_folder(req: FolderReq):
        p = Path(req.path).expanduser()
        if not p.is_dir() and not _has_index(p):
            raise HTTPException(400, f"not a directory: {req.path}")
        return _switch_root(p.resolve())
```

`start_index`: after the `running` check add `if not state["root"].is_dir(): raise HTTPException(400, "disk not mounted; plug it in to index")`.
`export`: after the `running` check add `if req.mode != "csv" and not state["root"].is_dir(): raise HTTPException(400, "disk not mounted; plug it in to copy or link photos")`.

New endpoint:

```python
    @app.get("/api/shoots")
    def shoots():
        return {"shoots": db.list_shoots(recent=_load_recent())}
```

- [ ] **Step 6: UI**

`index.html`: replace the `#recent-folders` select with `<select id="shoots" class="mono"><option value="">saved shoots&hellip;</option></select>`, and add under `<header>` (before `<main>`):

```html
<div id="offline-banner" class="mono" hidden>Disk not connected. Browsing the saved index; plug the disk in to index more or to copy and link photos.</div>
```

`style.css`: rename `#recent-folders` rules to `#shoots`; append `#offline-banner { background: #fff3cd; color: #5c4400; padding: 6px 16px; font-size: 12px; }`.

`app.js`: add `shoots: []` to the `state` object, then replace `loadRecent`/`renderRecent`/`recentSelect` with:

```javascript
  var shootsSelect = $("#shoots");
  function loadShoots() {
    return api("/api/shoots").then(function (data) {
      state.shoots = (data && data.shoots) || [];
      shootsSelect.innerHTML = '<option value="">saved shoots&hellip;</option>';
      state.shoots.forEach(function (s) {
        if (!s.root) return;
        var opt = document.createElement("option");
        opt.value = s.root;
        opt.textContent = s.name + "  (" + s.photos + (s.mounted ? ")" : ", disk not connected)");
        shootsSelect.appendChild(opt);
      });
      shootsSelect.value = "";
    }).catch(function () { /* non-fatal */ });
  }
  shootsSelect.addEventListener("change", function () { if (shootsSelect.value) switchFolder(shootsSelect.value); });
```

Every former `loadRecent()` call becomes `loadShoots()`. In `applyFolderInfo` add:

```javascript
    var offline = !!(state.folder.root && state.folder.mounted === false);
    $("#offline-banner").hidden = !offline;
    $("#start-index").disabled = offline;
    $$("#exportmode option").forEach(function (o) { o.disabled = offline && o.value !== "csv"; });
    if (offline) $("#exportmode").value = "csv";
```

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add photosort/db.py photosort/index.py photosort/server.py photosort/ui/app.js photosort/ui/index.html photosort/ui/style.css tests/test_db.py tests/test_server.py
git commit -m "feat: list saved shoots, open one and browse it with the disk unplugged

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Count the videos the walk skips

**Files:**
- Modify: `photosort/config.py` (`VIDEO_EXTS`)
- Modify: `photosort/walk.py` (`count_videos`)
- Modify: `photosort/index.py` (store `meta.videos`)
- Modify: `photosort/server.py` (`stats.videos`)
- Modify: `photosort/ui/app.js` (`loadStats`)
- Test: `tests/test_walk.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `config.VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mts", ".avi"}`.
- Produces: `walk.count_videos(root) -> int` (same skip rules as `find_images`: hidden entries and `photosort-out` ignored).
- `GET /api/stats` `videos` now reads `meta.videos` (0 when absent).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_walk.py`:

```python
def test_count_videos_ignores_hidden_and_output(tmp_path):
    from photosort.walk import count_videos
    (tmp_path / "day1").mkdir(); (tmp_path / "day1" / "c1.MOV").write_bytes(b"x")
    (tmp_path / "c2.mp4").write_bytes(b"x"); (tmp_path / ".hidden.mov").write_bytes(b"x")
    (tmp_path / "photosort-out").mkdir(); (tmp_path / "photosort-out" / "c3.mov").write_bytes(b"x")
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert count_videos(tmp_path) == 2
```

Append to `tests/test_server.py`:

```python
def test_stats_reports_videos_not_indexed(tmp_path):
    from conftest import make_image
    make_image(tmp_path, "a.jpg"); (tmp_path / "clip.mov").write_bytes(b"x")
    index_folder(tmp_path, faces=False, workers=1, embed=False)
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/stats").json()["videos"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_walk.py tests/test_server.py -q -k videos`
Expected: ImportError `count_videos`; `videos == 0`.

- [ ] **Step 3: Implement**

`config.py` after `RAW_EXTS`: `VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mts", ".avi"}`.

`walk.py`:

```python
def count_videos(root: Path) -> int:
    """Videos are not indexed; this only tells the user how many the shoot holds."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "photosort-out"]
        n += sum(1 for fn in filenames if not fn.startswith(".") and Path(fn).suffix.lower() in VIDEO_EXTS)
    return n
```

(import `VIDEO_EXTS` from config.)

`index.py` `_index_folder`, right after `files = find_images(root)` and the empty guard: `db.set_meta(conn, "videos", str(count_videos(root)))` (import `count_videos`).

`server.py` `stats()`: `videos=int(db.get_meta(conn, "videos") or 0)`.

`app.js` `loadStats`: `if (s.videos) bits.push(s.videos + " videos (not indexed)");`.

- [ ] **Step 4: Run the suite, commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add photosort/config.py photosort/walk.py photosort/index.py photosort/server.py photosort/ui/app.js tests/test_walk.py tests/test_server.py
git commit -m "feat: count the videos a shoot holds and say they are not indexed

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: People tab hides tiny clusters by default

**Files:**
- Modify: `photosort/config.py` (`PERSON_MIN_PHOTOS = 3`)
- Modify: `photosort/people.py` (`list_people(root, min_photos=1)`)
- Modify: `photosort/server.py` (`/api/people?min_photos=`)
- Modify: `photosort/ui/app.js`, `photosort/ui/index.html`
- Test: `tests/test_people.py`

**Interfaces:**
- Produces: `people.list_people(root, min_photos: int = 1) -> list[dict]`; rows with `n < min_photos` are omitted. `cluster_faces` and `export_people` keep calling it with the default (nothing hidden there).
- Produces: `GET /api/people?min_photos=<int>` (default 1, the UI sends 3 unless "show small groups" is ticked).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_people.py`:

```python
def test_list_people_min_photos(tmp_path):
    from photosort import db
    from photosort.people import list_people
    from conftest import make_image
    from photosort.index import index_folder
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    conn.execute("INSERT INTO people(name, n) VALUES('big', 5)"); conn.execute("INSERT INTO people(name, n) VALUES('small', 1)"); conn.commit()
    assert [p["name"] for p in list_people(tmp_path)] == ["big", "small"]
    assert [p["name"] for p in list_people(tmp_path, min_photos=3)] == ["big"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_people.py -q -k min_photos`
Expected: TypeError unexpected keyword `min_photos`.

- [ ] **Step 3: Implement**

`config.py`: `PERSON_MIN_PHOTOS = 3   # People tab hides smaller clusters unless asked`.

`people.py` `list_people` signature `def list_people(root: Path, min_photos: int = 1) -> list[dict]:` and the query gains `WHERE pe.n >= ?` before `ORDER BY`, with `(min_photos,)` bound.

`server.py`:

```python
    @app.get("/api/people")
    def people(min_photos: int = 1):
        if state["root"] is None:
            return []
        from .people import list_people
        return list_people(state["root"], min_photos=max(1, min_photos))
```

`index.html` People `.row` gains `<label><input type="checkbox" id="small-groups"> show groups under 3 photos</label>`.

`app.js` `loadPeople`: `var min = $("#small-groups").checked ? 1 : 3; return api("/api/people?min_photos=" + min)...`; add `$("#small-groups").addEventListener("change", loadPeople);`. The cluster button response already returns the full list; call `loadPeople()` instead of using the returned list so the filter applies.

- [ ] **Step 4: Run the suite, commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add photosort/config.py photosort/people.py photosort/server.py photosort/ui/app.js photosort/ui/index.html tests/test_people.py
git commit -m "feat: People tab hides clusters under 3 photos unless asked

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Synthetic shoot generator

**Files:**
- Create: `scripts/make_scale_set.py`
- Test: `tests/test_scale_set.py`

**Interfaces:**
- Produces: `make_scale_set.build(out: Path, n: int, edge: int = 1600, seed: int = 0, days: int = 4, burst_every: int = 25, corrupt_every: int = 400, videos: int = 5) -> dict(photos=int, bursts=int, corrupt=int, videos=int)`.
  Layout: `out/day<k>/IMG_<i:05d>.jpg`, EXIF `DateTimeOriginal` walking forward 7 s per photo from `2026:09:10 08:00:00`, camera `SCALECAM`. Every `burst_every`-th photo is followed by 3 near-copies (same texture, 2 px shift, 3 s apart). Every `corrupt_every`-th file is 12 random bytes. `videos` empty `.MOV` stubs in `day1/`.
- CLI: `python scripts/make_scale_set.py OUT --n 2000 [--edge 1600] [--seed 0]`. Refuses an `OUT` under `/Volumes/`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scale_set.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

def test_generator_layout(tmp_path):
    from make_scale_set import build
    from photosort.walk import find_images, count_videos
    from photosort.features import exif_info
    s = build(tmp_path / "set", n=60, edge=256, burst_every=20, corrupt_every=30, videos=2)
    assert s == {"photos": 60, "bursts": 2, "corrupt": 1, "videos": 2}   # bursts at i=20,40; corrupt at i=30
    files = find_images(tmp_path / "set")
    assert len(files) == 60 and len({f.path.parent.name for f in files}) == 4
    assert count_videos(tmp_path / "set") == 2
    ok = [f for f in files if f.size > 100]
    info = exif_info(ok[0].path)
    assert info["taken_at"].startswith("2026-09-10T") and info["camera"] == "SCALECAM"

def test_generator_refuses_volumes(tmp_path):
    import pytest
    from make_scale_set import build
    with pytest.raises(ValueError):
        build(Path("/Volumes/One Touch/anything"), n=1)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_scale_set.py -q`
Expected: ModuleNotFoundError `make_scale_set`.

- [ ] **Step 3: Write the script**

`scripts/make_scale_set.py`:

```python
"""Build a fake shoot for scale tests: nested day folders, EXIF capture times, bursts of near-copies,
a few corrupt files, some video stubs. Never writes under /Volumes (that is where real shoots live)."""
from __future__ import annotations
import argparse, datetime as dt
from pathlib import Path
import numpy as np
from PIL import Image

START = dt.datetime(2026, 9, 10, 8, 0, 0)

def _texture(rng: np.random.Generator, edge: int) -> np.ndarray:
    small = (rng.random((edge // 16, edge // 16 * 3 // 2, 3)) * 255).astype("uint8")
    return np.asarray(Image.fromarray(small).resize((edge * 3 // 2, edge), Image.BILINEAR))

def _save(arr: np.ndarray, path: Path, when: dt.datetime) -> None:
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    ex[0x0110] = "SCALECAM"
    Image.fromarray(arr).save(path, quality=88, exif=ex)

def build(out: Path, n: int, edge: int = 1600, seed: int = 0, days: int = 4, burst_every: int = 25,
          corrupt_every: int = 400, videos: int = 5) -> dict:
    out = Path(out)
    if str(out.resolve()).startswith("/Volumes/"):
        raise ValueError("refusing to write a synthetic set under /Volumes; real shoots live there")
    rng = np.random.default_rng(seed)
    for k in range(1, days + 1):
        (out / f"day{k}").mkdir(parents=True, exist_ok=True)
    stats = dict(photos=0, bursts=0, corrupt=0, videos=0)
    when = START; i = 0; pending_burst = 0; base = None
    while stats["photos"] < n:
        day = out / f"day{i * days // max(n, 1) + 1}"
        path = day / f"IMG_{i:05d}.jpg"
        if pending_burst:
            shift = int(rng.integers(1, 4))
            arr = np.roll(base, shift, axis=1); pending_burst -= 1; when += dt.timedelta(seconds=3)
        else:
            base = _texture(rng, edge); arr = base; when += dt.timedelta(seconds=7)
            if burst_every and i > 0 and i % burst_every == 0:
                pending_burst = 3; stats["bursts"] += 1
        if corrupt_every and i > 0 and i % corrupt_every == 0:
            path.write_bytes(rng.bytes(12)); stats["corrupt"] += 1
        else:
            _save(arr, path, when)
        stats["photos"] += 1; i += 1
    for v in range(videos):
        (out / "day1" / f"CLIP_{v:03d}.MOV").write_bytes(b"")
        stats["videos"] += 1
    return stats

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="build a synthetic shoot for scale tests")
    p.add_argument("out"); p.add_argument("--n", type=int, default=2000); p.add_argument("--edge", type=int, default=1600)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    print(build(Path(a.out), a.n, a.edge, a.seed))
```

Note the day assignment `i * days // n + 1` spreads photos evenly across the day folders; the test asserts 4 distinct parents. Corrupt files count toward `photos` (they are files the walk finds).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_scale_set.py -q`
Expected: 2 passed (bursts start at i=20 and 40; i=60 is past n; the one corrupt file is i=30).

- [ ] **Step 5: Commit**

```bash
git add scripts/make_scale_set.py tests/test_scale_set.py
git commit -m "test: synthetic shoot generator for scale runs

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: The scale gate

**Files:**
- Create: `tests/test_scale.py`
- Modify: `pyproject.toml` (pytest `slow` marker, skipped by default)
- Create: `docs/SCALE.md`
- Modify: `docs/TODO-diu.md` (step zero)

**Interfaces:**
- `pytest -m slow` runs the gate; `pytest` alone skips it (`addopts = "-m 'not slow'"`).
- Consumes: `make_scale_set.build`, `index_folder`, `cluster_faces` internals, `/api/search`, `/api/search/ids`, `/api/export`.

- [ ] **Step 1: Marker config**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = ["slow: multi-minute scale runs, opt in with -m slow"]
addopts = "-m 'not slow'"
```

Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as before (nothing is marked yet).

- [ ] **Step 2: Write the gate**

Create `tests/test_scale.py`:

```python
"""Scale gate. Opt in: .venv/bin/python -m pytest -m slow tests/test_scale.py -q -s
Proves the pipeline at a few thousand files before the real disk is touched."""
import os, sys, time
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
pytestmark = pytest.mark.slow

N = int(os.environ.get("PHOTOSORT_SCALE_N", "2000"))

def _listing(root: Path) -> list[tuple[str, int, float]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*") if p.is_file())

def test_index_search_export_at_scale(tmp_path):
    from make_scale_set import build
    from photosort.index import index_folder
    from photosort.server import create_app
    from fastapi.testclient import TestClient
    shoot = tmp_path / "shoot"
    s = build(shoot, n=N, edge=1024)
    before = _listing(shoot)
    t = time.time()
    st = index_folder(shoot, faces=False, workers=4, embed=False)
    print(f"\nindex {N}: {time.time() - t:.1f}s, indexed {st['indexed']} errors {st['errors']}")
    assert st["indexed"] + st["errors"] == N and st["errors"] == s["corrupt"]
    c = TestClient(create_app(shoot))
    stats = c.get("/api/stats").json()
    assert stats["photos"] == N - s["corrupt"] and stats["errors"] == s["corrupt"] and stats["videos"] == s["videos"]
    t = time.time()
    page = c.get("/api/search", params={"limit": 200, "offset": 1000}).json()
    assert page["total"] == N - s["corrupt"] and len(page["results"]) == 200
    ids = c.get("/api/search/ids").json()["ids"]
    assert len(ids) == N - s["corrupt"]
    print(f"search page + ids: {time.time() - t:.2f}s")
    assert c.post("/api/export", json={"ids": ids, "name": "all", "mode": "symlink"}).json()["started"]
    for _ in range(600):
        p = c.get("/api/export/progress").json()
        if not p["running"]: break
        time.sleep(0.1)
    assert p["error"] is None and p["done"] == len(ids) and p["failed"] == 0
    assert sum(1 for _ in Path(p["path"]).iterdir()) == len(ids)
    # incremental re-run is near-instant and touches nothing
    t = time.time()
    st2 = index_folder(shoot, faces=False, workers=4, embed=False)
    assert st2["indexed"] == 0 and st2["skipped"] == N     # unchanged corrupt files are known too, so they count as skipped
    print(f"re-index: {time.time() - t:.1f}s")
    assert _listing(shoot) == before

def test_face_clustering_20k_vectors(tmp_path):
    """DBSCAN cosine over 20k SFace-sized vectors must finish in bounded time and memory."""
    from photosort import db
    from photosort.people import cluster_faces
    from conftest import make_image
    from photosort.index import index_folder
    make_image(tmp_path, "a.jpg"); index_folder(tmp_path, faces=False, workers=1, embed=False)
    conn = db.connect(tmp_path)
    pid = conn.execute("SELECT id FROM photos").fetchone()[0]
    rng = np.random.default_rng(0)
    centers = rng.standard_normal((200, 128)).astype(np.float32)
    rows = []
    for k in range(20000):
        c = centers[k % 200]; v = c + 0.15 * rng.standard_normal(128).astype(np.float32); v /= np.linalg.norm(v)
        rows.append((pid, 0, 0, 10, 10, 0.9, "[]", 1.0, v.tobytes()))
    conn.executemany("INSERT INTO faces(photo_id,x,y,w,h,score,landmarks,eye_sharp,embed) VALUES(?,?,?,?,?,?,?,?,?)", rows); conn.commit()
    t = time.time()
    people = cluster_faces(tmp_path, eps=0.5)
    dt = time.time() - t
    print(f"\ncluster 20k faces: {dt:.1f}s, {len(people)} groups")
    assert 150 <= len(people) <= 250 and dt < 120
```

- [ ] **Step 3: Run the gate**

Check power first: `pmset -g batt | head -1` must say `AC Power`. Then:

Run: `.venv/bin/python -m pytest -m slow tests/test_scale.py -q -s 2>&1 | tail -15`
Expected: 2 passed; printed timings. Copy the printed numbers into `docs/SCALE.md` (Step 5). If `test_face_clustering_20k_vectors` blows the 120 s budget, do not raise the budget: report the number and stop; that is a finding for Kirtan.

- [ ] **Step 4: Manual 24 MP run (mains power only)**

```bash
mkdir -p ~/Desktop/photosort-scale && .venv/bin/python scripts/make_scale_set.py ~/Desktop/photosort-scale/set3k --n 3000 --edge 4000
/usr/bin/time -l .venv/bin/python -m photosort.cli index ~/Desktop/photosort-scale/set3k 2>&1 | tail -20
.venv/bin/python -m photosort.cli people ~/Desktop/photosort-scale/set3k
.venv/bin/python -m photosort.cli classify ~/Desktop/photosort-scale/set3k
du -sh ~/Library/Application\ Support/photosort/set3k-*
```

Record: wall time, `maximum resident set size` from `time -l`, ms/photo, thumbs size on disk. Faces ON here (the M1 with 4 face workers plus MobileCLIP in the parent is the unknown the earlier bench never measured). When done, delete `~/Desktop/photosort-scale` and its index dir under Application Support (they are synthetic; this is not client data).

- [ ] **Step 5: Write docs/SCALE.md**

```markdown
# Scale numbers (measured, not projected)

Machine: M1, 8 GB. Date: 2026-09-18.

| Run | N | Faces | Wall | Peak RSS | Per photo | Thumbs on disk |
|-----|---|-------|------|----------|-----------|----------------|
| pytest gate, 1024 px | 2000 | off | <fill> | n/a | <fill> | n/a |
| CLI, 4000 px (24 MP-class) | 3000 | on | <fill> | <fill> | <fill> | <fill> |
| DBSCAN, synthetic vectors | 20000 faces | | <fill> | | | |

Search page + ids over 2000: <fill>. Symlink export of 2000: <fill>. Incremental re-index of 2000: <fill>.

Caveats on the 3000 x 4000 px run: generating the set is 20-30 min of CPU before indexing starts (not part of the timing); the textures hold no faces, so YuNet runs on every frame but SFace embedding cost is NOT measured here. Real faces cost more.

Not yet measured: RAW files (needs the real disk), HEIC, SFace on real faces, the real Diu count.

How to re-run: `pytest -m slow tests/test_scale.py -s` and the CLI lines in
docs/superpowers/plans/2026-09-18-diu-scale-hardening.md, Task 10.
```

Fill every `<fill>` with the measured value. No projections in this file.

- [ ] **Step 6: Step zero in TODO-diu.md**

Prepend to `docs/TODO-diu.md` under the title:

```markdown
## Step zero, the moment the disk is plugged in
1. Open `diu Photos` in the app and index it (it has 0 rows today; the earlier test ran on the reels folder).
2. `python -m photosort.cli bench "/Volumes/One Touch/other data/diu Photos" --n 200` and add the line to docs/SCALE.md.
3. Only then start Plan B (bursts, discovered categories, blurry tiles, override, final export).
```

- [ ] **Step 7: Full suite, commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, scale tests reported as deselected.

```bash
git add tests/test_scale.py pyproject.toml docs/SCALE.md docs/TODO-diu.md
git commit -m "test: scale gate at 2k photos and 20k faces, measured numbers in docs/SCALE.md

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:** hazards 1 (Task 1), 2 (Task 5), 3 (Task 6), 4 (Task 3), 5 (Task 2), 6 (Task 7), 7 (Task 4), 8 (Task 8), gate (Tasks 9, 10). Step zero (Task 10 Step 6).

**Type consistency:** `SourceUnavailable` defined in Task 1, imported in Task 1 Step 5 and used nowhere else. `export_bytes(root, ids) -> int` defined in Task 5 and used only there. `db.get_meta/set_meta` defined in Task 6, used in Tasks 6 and 7 (Task 7 depends on Task 6; execute in order). `Index.query` defined in Task 4, used by both endpoints there. `list_people(root, min_photos=1)` in Task 8 matches the server call. Progress key `stage_started` in Task 3 matches `formatProgress`. `/api/export/progress` keys `running, done, total, failed, path, error` match the UI poller and the gate test.

**Ordering:** 3 after 2 (`_index_folder` rename), 5 after 2 (`sleep_guard`), 6 after 5 (`state["export"]` reset in `_switch_root`), 7 after 6 (meta helpers). Otherwise independent. Task 10 needs everything.
