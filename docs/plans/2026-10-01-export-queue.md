# Reliable batch export Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Batch exports survive page reload, retry with their original recipe, and offer explicit resume after application restart without overwriting originals or existing outputs.

**Architecture:** A bounded atomic JSON queue owns per-item state and a single worker. The server freezes prepared color references, white-balance gains and polish/export settings as data, independent of ephemeral editor sessions. Restart pauses unfinished work until the user resumes it. The existing single-photo editor stays unchanged.

**Tech Stack:** Python stdlib threads/JSON/atomic files, NumPy (safe data serialization), existing HTTP server and vanilla JavaScript. No new dependencies, downloaded assets, commits or pushes.

## Task 1: Durable queue core

Files: `photopilot/export_queue.py`, `tests/test_export_queue.py`.

Test first: submit/idempotency, worker completion and item failures, pause/resume, retry only failures with immutable recipe, restart interruption, atomic write failure, bounded validation and source-change refusal. Observe failing tests before implementation.

Implement bounded versioned JSON state with atomic replace and private permissions. A single worker processes items with an injected renderer. Persist running before render and result before moving on; no lock held while rendering. New queues run; restored queues require explicit resume. Use an explicit injected storage path for tests and `PHOTOPILOT_EXPORT_PATH` for production override. Successful items are never rerendered. Preserve previous durable state on save failure and surface errors. Provide shutdown for isolated tests.

Verify focused tests and full Python baseline. Spec review must pass before independent quality review.

## Task 2: Server integration and safe output publication

Files: `photopilot/export_recipe.py`, `tests/test_export_recipe.py`, then `photopilot/server.py`, `tests/test_export_server.py`.

The data-only recipe codec is an independent subtask and can be developed in parallel with the queue core, in separate files. Both must pass spec and quality review before the server adapter depends on them.

Test first: HTTP submit/status/action, JSON-safe prepared-session roundtrip including reference pixels/stats/WB, original recipe after restart, failed retry, bounded request bodies, path/name collision safety and output conflict refusal.

Add queue APIs; freeze only known session fields, never pickle/cache/runtime handles. Queue rendering must not rely on `_SESSIONS` eviction. Stage each image in its destination directory and publish without clobbering existing files. Retain a stable output directory per job; validate source fingerprint before and after processing. Do not expose internal recipes in status responses. Resume must not recompute batch reference/WB from a failed subset.

Verify actual generated PNG exports and full baseline. Spec then independent quality review.

### Queue read-path performance subtask

Files: `photopilot/export_queue.py`, `tests/test_export_queue.py`, followed by a small server call-site adaptation.

Profiling and adapter inspection found that summaries copied all items before discarding them, and paginated details copied an entire batch before slicing. Test first, then add opt-in `list(summaries=True)` and bounded `get(job_id, offset=0, limit=50)` that clone only requested items while preserving accurate full-job counts. Keep default APIs and all persistence behavior unchanged. Verify unrequested records are not copied, pagination bounds, returned-object isolation and a fixture-based timing comparison. Spec and quality review before server adoption.

Add a locked internal recipe-leaf accessor so result validation can read the frozen output folder and a requested output name without copying reference pixels or the full batch mapping. Validate complete canonical output paths against that folder, not merely their suffix. Reject color-only exports without a reference before preparation or image decoding.

## Task 3: Batch queue interface

Files: `photopilot/server.py` embedded UI, `tests/frontend_regressions.cjs`.

Test first: batch submit uses queue, single editor unchanged, polling restores on reload without reopening dialogs, pause/resume/retry original job, escaped failures, bounded result DOM, transient polling failure recovers and stale responses cannot replace newer state.

Keep incumbent UI identity. Show a compact export queue with counts, explicit interrupted/failed messages and pause/resume/retry controls. Distinguish window reload from application exit: exports continue on reload, application exit requires resume. Never silently retry failed files or restart a finished job.

Verify 57 existing frontend regressions plus new tests, real browser reload/restart/failure-retry flow using only generated fixtures, responsive view and native app smoke test where available. Spec then quality then final integration review. Record evidence and unresolved limitations in an audit.

## Acceptance and safeguards

- Prior baseline: 51 Python tests and 57 frontend regressions pass on 2026-10-01.
- Queue/store failures must be visible, not reported as success.
- Original files and completed outputs remain byte-identical through resume/retry.
- Crash after publication before acknowledgement may leave an output conflict: report it; never overwrite it.
- Rendering stays serial to bound image/face-analysis memory. Pause takes effect after the current photo, not mid-decode.
- Actual fixture-based QA is required before claiming this stage complete; this does not imply product-wide parity or zero bugs.

## Execution status (2026-10-03)

- Task 1 core, Task 2 recipe/server and read-path subtask: implemented, independent spec and quality gates passed.
- Task 3 interface: implemented, spec fixes verified and independent quality gate passed; 79 frontend regressions preserve the original 57.
- Live QA: reload/pause/restart/resume, source-missing frozen-recipe retry, full download, single editor and queue layout passed. Installed native APP imported and exported all three generated fixtures successfully.
- Final integration review: passed, independently rerunning 137 Python and 79 frontend tests; audit contains actual evidence and remaining design boundaries. No commit or push performed.
