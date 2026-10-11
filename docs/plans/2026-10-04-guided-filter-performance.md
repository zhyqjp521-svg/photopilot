# Shared Guided Filter Statistics Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Eliminate redundant guide statistics during RGB skin smoothing without changing a single output pixel or adding dependencies.

**Architecture:** Keep the public single-channel guided_filter intact in behavior. Factor its shared guide mean/variance into a private helper; smooth_skin computes these once and reuses them for all three channel filters. Do not cache photos, modify UI, change filter order, change float precision, or approximate/downsample filtering.

**Tech Stack:** Python, NumPy float32, OpenCV boxFilter, unittest.

## Task 1: Test and implement shared statistics

**Files:** Modify photopilot/polish.py; create tests/test_guided_filter_performance.py.

1. Write a standalone legacy reference implementing the current guided_filter and smooth_skin algorithm. Compare exact pixels for textured uint8 RGB, small/large and portrait/landscape images, masks (None/soft/zero), strength and retain boundaries. Test source immutability and strength=0 identity.
2. Add a RED performance contract: mock cv2.boxFilter with wraps, run smooth_skin and assert 14 calls instead of the current 18. Run `.venv/bin/python -m unittest discover -s tests -p 'test_guided_filter_performance.py'` and verify expected failure.
3. Factor `_guided_stats(guide, radius)` returning mean and variance. Factor `_guided_apply(guide, src, radius, eps, mean_i, var_i)` retaining original arithmetic ordering. Public guided_filter invokes both; smooth_skin computes stats once then applies to channels. Six guide-statistics calls become two; channel-specific twelve calls remain.
4. Run targeted tests, then `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` and `node tests/frontend_regressions.cjs`. No commits/pushes: preserve dirty worktree and user originals.
5. Spec review then fresh independent code quality review against stage baseline `/tmp/photopilot-preview-perf.BXiUNB/polish-before.py`, not unrelated git changes.

## Task 2: Measure and verify integration

**Files:** Create scripts/bench_skin.py and docs/qa-2026-10-04-performance/report.md.

1. Benchmark actual legacy and current smooth_skin on identical synthetic pixels, alternate ordering, warm-up, repeated median timings; assert pixel equality every comparison. Report measurements, not timing assertions in CI.
2. Run isolated existing browser import/edit/undo/mask/export QA sequentially; verify originals SHA unchanged and errors absent. Refresh native app state before restart; if safe, close verified old PhotoPilot, launch one new instance and verify editor loads.
3. Record exact test counts, performance samples and limitations. Mark completion only after checks pass.

## Checklist

- [x] Task 1: RED 18!=14, GREEN 5 tests; 163 Python + 97 frontend; spec PASS; independent quality PASS.
- [x] Task 2: reproducible alternating benchmark, actual UI/native checks, report and final review PASS. Final 164 Python + 97 frontend; traced peak unchanged; final measured smooth_skin time reduction 5.587%–9.660%.
