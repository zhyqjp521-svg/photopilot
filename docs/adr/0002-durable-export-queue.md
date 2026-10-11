# ADR-0002: Durable serial export queue

## Status

Accepted for the user-authorized batch-reliability stage.

## Context

The page currently drives exports one request at a time. Reload drops progress; retry prepares a failed subset with current controls, changing batch reference statistics and settings. Runtime sessions disappear on restart and are evicted after sixteen preparations.

## Decision

Use a bounded, versioned atomic JSON queue and a serial server worker, with fully frozen, data-only prepared recipes. Persist item transitions and source fingerprints. Pause unfinished jobs on application restart; resume only through an explicit user action. Publish outputs using same-directory staging and no-clobber publication. Keep single-photo editor behavior independent.

## Consequences

### Positive

- No new database dependency or service; private local storage and inspectable state.
- Retry/resume does not change color references or previously completed files.
- Serial work bounds face-analysis concurrency and render memory.

### Negative

- Atomic whole-file JSON writes impose a job/item/byte cap (20 retained jobs, 20,000 photos per job, 16 MB recipe, 64 MB total state); this is not an unlimited archival job database. Each transition rewrites the snapshot, including frozen reference pixels, so high-volume delivery should eventually migrate to transactional storage. Completed/failed records can be removed explicitly without deleting outputs.
- Pause waits for the current image to finish. Hard interruption during publication can require manual resolution of an output conflict.
- The server process must remain alive; closing the application is not background-service execution.

### Alternatives considered

- Browser-local queue: rejected because rendering and session state disappear with the page.
- Reprepare on retry: rejected because automatic reference/WB statistics may change.
- SQLite: valid future option for unbounded history, but extra schema/migration complexity is unnecessary for a capped desktop queue at this stage.
- Pickled sessions: rejected; persistent state must be bounded, validated data, not executable object serialization.

## Verification

Test durable transitions, fail-closed persistence, frozen recipe roundtrip, output collisions, source changes and restart. Use generated images only for live QA and confirm original/output hashes.
