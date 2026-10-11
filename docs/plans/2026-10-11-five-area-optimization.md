# PhotoPilot five-area optimization implementation plan

> Use subagent-driven-development for independent tasks, followed by spec and quality reviews. Preserve existing uncommitted work; do not commit or push unless requested.

**Goal:** Improve large-library navigation, reversible culling, configurable similarity, detailed comparisons, and failure recovery without modifying user originals during QA.

**Architecture:** Keep the local Python + embedded browser architecture. Bounded per-group rendering replaces prefix-only navigation for large grouped libraries. Session culling history records explicit changes, while ratings continue to use acknowledged XMP writes. Similarity remains deterministic local code rather than external licensed models.

**Tech stack:** Python, NumPy/OpenCV, vanilla JavaScript, unittest, VM regression tests, isolated Playwright/installed Chrome.

**Completed:** All five scoped tasks implemented and spec/quality reviews approved. Final validation: 192 Python tests, 147 frontend tests, 9 comparison checks, and isolated installed-Chrome browser QA including 6000-photo grouped/flat/giant-group navigation. No original photos modified or native app restarted; see `docs/qa-2026-10-11-five-area.md` for limits and measurements.

## 1. Bounded large-library navigation

Files: `photopilot/server.py`, `tests/grouped_view_browser.cjs`, `tests/frontend_regressions.cjs`.

1. Add a failing 6000-photo browser test that jumps to a distant group and asserts bounded card count, correct destination, retained selections.
2. Run the browser test and record baseline card count/time.
3. Build lightweight section shells and lazily materialize only a bounded window of group cards; use estimated heights for unloaded sections and preserve the scroll anchor when evicting.
4. Test next/previous, collapse/unfold, filtering, three densities, stale rAF work, and flat-grid compatibility.

## 2. Reversible culling

Files: `photopilot/server.py`, `tests/frontend_regressions.cjs`, new isolated browser tests.

1. Add failing tests for selection undo/redo and project-removal restoration.
2. Keep a bounded session history. Record successful changes only, with paths and before/after data; clear on library replacement.
3. Add UI undo/redo and guarded Cmd/Ctrl+Z shortcuts. Rating undo reuses `setRating` and retains failed entries for retry. Local trash is not silently reversed or replayed.
4. Verify input/editor/comparison conflicts, failed XMP writes, scan busy state, saved project consistency, and hidden selection.

## 3. Similarity accuracy and manual correction

Files: `photopilot/cull.py`, `photopilot/server.py`, `photopilot/project.py` if settings change, `tests/test_similarity.py`, frontend tests.

1. Add failing tests for chain drift, cross-folder grouping, aspect-ratio mismatch, strict/balanced/loose thresholds, missing hashes.
2. Compare each member against the group anchor as well as its neighbor; keep incompatible directories/aspect ratios separate.
3. Expose strictness for scans and manual split/merge of selected photos. Refresh rank/best/duplicate metadata, retain ratings/selection, persist project.
4. Verify invalid settings and no accidental changes to unrelated groups.

## 4. Detailed comparison

Files: `photopilot/server.py`, standalone proposed comparison snippet and tests (integrated by parent to avoid shared-file edits).

1. Add failing tests for true source-pixel 100% scale, active-pane exact ratings, keep/reject, synchronized pan/zoom, late image callbacks.
2. Use independent pane image dimensions with shared normalized pan/zoom; fit and 100% controls distinguish native pixels from fit multipliers.
3. Add explicit active pane and 0–5 rating/keep/reject actions; compare shortcuts must not rate unrelated grid selections.
4. Test 2/3/4 panes, different aspect ratios, loading failures, pointer cancellation, modal closure and reopen.

## 5. Editing and recovery reliability

Files: `photopilot/polish.py`, `photopilot/export_queue.py`, new focused tests (backend only delegated independently).

1. Inspect existing exposure and export/input identity safeguards; write reproductions before fixes.
2. Test clipped highlights, dark/flat input, neutral color preservation, preview/export use of identical tone recipe.
3. Test missing/changed input, restart recovery, ambiguous exports and retry; implement only demonstrated gaps, preserve originals and completed exports.
4. Run all Python tests and isolated browser write-failure tests.

## Review and handoff

Run `.venv/bin/python -m unittest discover -s tests -q`, `node tests/frontend_regressions.cjs`, and `NODE_PATH=/Users/fanda./.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/grouped_view_browser.cjs` plus new browser tests. Run `git diff --check` and compile checks. Spec review must precede quality review; address findings and rerun. Report implemented scope and limits honestly, including that synthetic throughput does not establish full RAW decode speed.
