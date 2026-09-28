# Smart Sort, Compare, Filters, and Presets Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make PhotoPilot group near-identical/burst photos together, provide a focused 2–4 image comparison view with synchronized zoom, add composable filters and manual stars, and ship original commercial-friendly style presets.

**Architecture:** Keep the existing local Python scan pipeline and XMP sidecars. Extend `PhotoScore` with group metadata, expose a stable smart ordering, and let the browser maintain a full scanned library plus a filtered view. Add a lightweight comparison overlay that reuses the existing preview endpoint and applies one zoom control to all panes. Presets remain original OKLab parameter sets; no third-party LUT or asset is copied.

**Tech Stack:** Python 3.11, OpenCV/NumPy, stdlib HTTP server, inline HTML/CSS/JavaScript, XMP sidecars.

---

### Task 1: Lock down grouping, ordering, ratings, and preset contracts

**Objective:** Add failing tests for group metadata, smart ordering, XMP rating persistence, UI controls, and the preset catalog.

**Files:**
- Modify: `tests/run_all.py`
- Modify: `photopilot/cull.py` (only after RED)
- Modify: `photopilot/xmp.py` (only after RED)
- Modify: `photopilot/color.py` (only after RED)

**Step 1: Write failing tests**

Add checks that a burst group reports `group_size >= 2`, marks exactly one best frame, `smart_rank()` keeps group members contiguous, an XMP-written manual rating can be read back, and the new preset names are accepted and produce uint8 output. Add static UI checks for `比较 2–4 张`, `星级`, `智能分组`, and a zoom control.

**Step 2: Run tests to verify failure**

Run: `./venv/bin/python tests/run_all.py`

Expected: FAIL because the new metadata, reader, presets, and UI strings do not exist yet.

**Step 3: Implement the minimal contracts**

Add only the fields/functions needed by the tests, then run the focused suite again.

**Step 4: Run tests to verify pass**

Run: `./venv/bin/python tests/run_all.py`

Expected: PASS for the new checks and all existing checks.

---

### Task 2: Make burst grouping first-class

**Objective:** Preserve duplicate-group identity and expose a deterministic smart order to the UI/API.

**Files:**
- Modify: `photopilot/cull.py`
- Modify: `photopilot/server.py`

**Steps:**
1. Add `group_size`, `group_rank`, and `group_best` to `PhotoScore` with safe defaults.
2. In `_group_bursts`, populate group size, rank by score, and best-frame metadata while retaining the existing duplicate flag semantics.
3. Add `smart_rank()` that sorts groups by their best score and each group by best-first score, keeping every group contiguous.
4. Use `smart_rank()` for scan terminal results and keep score/name alternatives available to the browser.
5. Serialize the new fields and manual XMP rating without breaking old clients.
6. Run the full test suite.

---

### Task 3: Add composable smart sorting, filtering, and stars

**Objective:** Let users switch between smart grouping, score, file name, and star order, then filter without losing selection state.

**Files:**
- Modify: `photopilot/server.py`
- Modify: `tests/run_all.py`

**Steps:**
1. Add a sort select and filter select to the toolbar, with explicit labels and counts.
2. Keep `library` as the complete scan result and derive `ranked` as the current sorted/filtered view.
3. Add filters for all, recommended, groups, starred, and needs-review; make them work with the existing AI recommendation and selection set.
4. Add a card star button that writes XMP through `/api/rate` and updates the visible count immediately.
5. Ensure filtering never silently clears selected paths or the追色 target.
6. Run Python/JS tests and verify keyboard navigation remains usable.

---

### Task 4: Add 2–4 image group comparison with synchronized zoom

**Objective:** Let users open a burst group and compare two to four images side by side, with one zoom slider controlling all images.

**Files:**
- Modify: `photopilot/server.py`
- Modify: `tests/run_all.py`

**Steps:**
1. Add a “比较 N 张” card action only for groups of two or more.
2. Add a modal with 2–4 image panes, keyboard Escape close, and responsive stacking at narrow widths.
3. Add a 1×–4× range control and apply the same transform to every pane; keep each pane’s filename, score, and star action visible.
4. Load previews through the existing safe `/api/raw_img` endpoint and show a clear empty/error state.
5. Run static checks and exercise the modal through the local browser.

---

### Task 5: Ship original, commercial-friendly presets

**Objective:** Add useful style presets without copying protected LUTs or proprietary assets.

**Files:**
- Modify: `photopilot/color.py`
- Modify: `photopilot/server.py`
- Modify: `README.md`
- Modify: `tests/run_all.py`

**Steps:**
1. Add original OKLab parameter presets: Natural, Portrait Soft, Travel Vibrant, Mono Contrast, and Film Warm/Clean Cool/Teal already present.
2. Expose `/api/presets` with display labels and short descriptions while retaining machine names.
3. Add a preset selector and “套用预设” action; reuse the existing non-destructive output pipeline.
4. Document that these are original algorithmic looks, and list permissive color-science references used for concepts only (BSD-3-Clause projects; no code/assets copied).
5. Run all tests and verify generated output is valid RGB uint8.

---

### Task 6: Final visual QA, build, and runtime verification

**Objective:** Verify UI states, package the app, and leave one fresh PhotoPilot instance running.

**Files:**
- Modify: `scripts/build_app.py` only if packaging requires it.

**Steps:**
1. Run `compileall`, `node --check`, and `tests/run_all.py`.
2. Run the Impeccable detector once against `photopilot/server.py` after all UI edits.
3. Rebuild the macOS app, stop only the exact existing PhotoPilot PID, and launch one new instance.
4. Smoke-test scan, filter, group comparison, zoom, star write, and preset output against the local 78-photo folder.
5. Report the implemented behavior, test counts, runtime PID, and research/license sources.
