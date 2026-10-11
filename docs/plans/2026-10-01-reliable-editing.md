# Reliable Project Recovery and Editing Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Preserve the last working library and choices across restarts, then make single-photo tone editing reversible without modifying originals.

**Architecture:** A versioned JSON checkpoint is atomically stored by the existing loopback server. The frontend serializes debounced saves and restores only after preset controls are ready; startup restore may never overwrite a newer user import. Tone history is bounded and per photo; rendering session IDs are never persisted.

**Tech Stack:** Python standard library, existing ThreadingHTTPServer, vanilla JavaScript, unittest and Node VM behavioral tests. No new dependencies, models or third-party assets.

## Scope and gates

- Preserve all existing dirty changes. Baseline: 30 Python and 4 frontend regressions pass.
- Follow RED → GREEN → regression for every task. Review spec first, then quality, before proceeding.
- No commit, push, model download or claims that all roadmap stages are complete.
- This stage restores completed/partial library state, not a paused decoder or in-flight export queue. Brushes, advanced RAW, scene consistency and delivery templates remain next stages.

## Task 1: Atomic checkpoint API

Create `photopilot/project.py`, `tests/test_project.py`; add only backend routes/import in `photopilot/server.py`.

1. Write real-file tests for roundtrip through a new store instance, corrupt/unsupported JSON, missing/changed originals, atomic replacement failure, invalid/oversized/nonfinite data and concurrent saves. Verify failure before implementation.
2. Implement `ProjectStore(path)`, `save(state)`, `load()` with a lock and same-directory unique tempfile + fsync + os.replace. Keep the last checkpoint on write failure. Default location: user application state, not photos, cache or repository.
3. Schema: `{version:1,folder,photos,selected,target,selection_touched,settings,drafts,scan_incomplete}`. Bound JSON size/depth/items, validate score records, finite numbers and path strings. Fingerprint originals with size/mtime_ns; regenerate thumbnail URLs and reload ratings from XMP. Never write original images/XMP in restore.
4. GET `/api/project` → `{state:null}` or `{state,missing:[],changed:[]}`. POST `/api/project` receives state and returns `{ok:true,saved_at}`. Existing local-origin checks remain enforced; persistence request size must be bounded before reading body.
5. Run `.venv/bin/python -m unittest discover -s tests -v`, `node tests/frontend_regressions.cjs`, spec review, then quality review.

## Task 2: Serialized autosave and safe restore

Modify frontend in `photopilot/server.py`; extend `tests/frontend_regressions.cjs` or new behavioral test module.

1. Write failing Node VM tests: old restore cannot overwrite new scan/import; saves serialized and latest snapshot wins; failed save is visible/retryable; restoring explicit empty selection keeps it empty; preset initialization precedes restoration; missing originals cannot silently overwrite old checkpoint.
2. Integrate GET/POST API after initialization. Debounce ordinary changes, serialize POSTs, checkpoint partial/terminal scans and selection/target/ratings/recipe/view controls. Use pagehide/visibility flush as best effort, expose explicit Save and Retry controls/status. Don't save an empty failed import over an existing project.
3. Display restored count, missing/changed originals and interrupted scan notice. Pause autosave if originals are unavailable/changed until deliberate successful rescan/import. Explicit INIT_FOLDER wins over restore; compare SCAN_REQ_GEN to avoid late restore races.
4. Verify tests, spec review, quality review. Apply incumbent compact control styles and accessible horizontal labels (Impeccable harden).

## Task 3: Tone undo/redo and per-photo drafts

Modify frontend `photopilot/server.py`, behavioral Node tests.

1. Tests first for bounded history, duplicate/no-op suppression, redo branch truncation, reset undo, flush latest slider gesture before undo/export/close, per-photo separation, and stale preview races.
2. Provide Undo/Redo buttons and Cmd/Ctrl+Z, Shift+Z/Y scoped to editor, without overriding text input native undo. One slider gesture = one history entry, not hundreds. Keep at most 50 tone states per photo. Undo/redo updates main batch controls and schedules fresh preview; disable in color-only mode.
3. Save per-photo tone draft/history via Task 2 checkpoint. Reopen the same photo with its saved tone; never resurrect an old sid. Snapshot values for export and prevent changing controls during export so output and displayed settings match.
4. Verify tests, spec review, quality review.

## Integration acceptance

- Full Python/frontend suites, inline JS syntax, compileall and diff whitespace checks.
- Isolated legacy run_all with temporary fixture root (do not rewrite user demo).
- Real browser/native engine: import test images, select/rate/target, change tone, undo/redo/reset, save; restart engine, restore without rescanning, reopen draft, export and verify resolution + original SHA-256.
- Probe missing original, invalid checkpoint and failed save UI; fix regressions using another failing test.
- Record actual checks and remaining limitations in `docs/AUDIT_2026-10-01.md`. Keep only one desktop PhotoPilot process after verification.

## Execution record

- Task 1: complete; independent spec and quality reviews passed. 51 Python regressions passed, including atomic failure, HTTP size limits, absolute paths and mixed-source ordering.
- Task 2: complete; independent spec and quality reviews passed after startup/partial-scan, overlapping-retry and benign keyboard/click restoration races were fixed. 31 frontend regressions passed.
- Task 2 real browser checks: three original PNG files imported without byte changes; empty selection, star, target and parameters saved and restored after engine restart. Save failure/retry, missing originals, corrupt checkpoint/retry and empty failed scan behaved correctly. These checks will be repeated against the final source.
- Task 3: complete; independent spec and quality reviews passed, including re-review of recipe/export locking and the post-export pending-preview fix. 57 frontend regressions passed. History persists per photo, is limited to 50 compatible six-tone states and does not persist rendering sessions.
- Integration: independent final review passed with no blocking security or logic findings; 51 Python / 57 frontend tests, inline script syntax, compileall, diff whitespace and isolated legacy suites passed. Legacy optional Hopper, large-image draft and NIMA tests were skipped and are not counted as verified.
- Final-source real browser checks repeated: exact-byte three-PNG upload; empty selection, star, target and settings restored after service restart; save failure/retry, missing originals, corrupt checkpoint/retry, failed scan and delayed startup navigation checks passed. Single-photo history restored after a separate service restart, exported a real 1800×1200 PNG and remained separate between photos. Original SHA-256 stayed unchanged. Late preview/export races and the formerly lost post-export adjustment passed.
- UI hardening: bounded safe missing-path warnings, visible save/retry feedback and compact horizontal undo/redo controls. Impeccable final-source detector returned no findings.
- Evidence and remaining scope: `docs/AUDIT_2026-10-01.md` and `docs/qa-2026-10-01/`. The existing old desktop PhotoPilot process was not restarted; only the latest source engine and Chrome were exercised. Native WKWebView, installation and later roadmap stages are not complete. No commit or push was performed.
- Follow-up native acceptance: after user approval and moving the window to the current Space, closed the old process and used the installed launcher. Real NSOpenPanel photo selection, WKWebView large-image tone adjustment/undo/redo, export, post-export preview adjustment and explicit save passed. Restarted the native process again; library, selection and tone history restored without rescanning, undo/redo and a second 1800×1200 PNG export passed. Original hash unchanged. One PhotoPilot main window remains. This supersedes the earlier native-interaction gap, but not installation/icon, broad RAW/person-retouch or later roadmap limitations.
