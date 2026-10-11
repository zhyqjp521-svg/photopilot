# ADR-0001: Local atomic project checkpoint

## Status

Accepted for this scoped stage under the user's request to implement the prioritized workflow.

## Context

The library, manual selection and single-photo tone controls currently exist only in page memory. The application is local and has no existing database. Originals and manual XMP ratings must remain untouched. Failure modes include abrupt restart, disk failure, missing external drives, corrupt JSON, late restore requests and overlapping autosaves.

## Decision

Use one versioned, bounded JSON checkpoint outside the photo directory and cache. Save through a server lock, unique same-directory temporary file, fsync and atomic replacement. Serialize frontend writes; invalidate late restore after a new import. Verify original file fingerprints and pause automatic overwrite when recovery is incomplete. Save recipes and bounded tone history, never runtime handles or processing session IDs.

## Consequences

- Positive: no extra dependency, portable inspection, small implementation surface, old checkpoint survives failed replacement.
- Negative: saving rewrites the full manifest; only last-project recovery, not multi-project collaboration or durable export scheduling; very large libraries may later justify SQLite.
- Neutral: external photo paths remain references, not backed-up photo bytes. Closing immediately before autosave completes cannot be guaranteed; explicit Save confirms durable completion.

## Alternatives Considered

- localStorage: tied to browser/port, quota and origin; insufficient for reliable native restart recovery.
- SQLite: useful for incremental manifests and multiple projects, but unnecessary migration/complexity for this first stage.
- Pickle/runtime snapshots: unsuitable for safe interoperable validation and cannot restore active decoder threads.

## References

- `docs/plans/2026-10-01-reliable-editing.md`
- `docs/AUDIT_2026-09-30.md`
