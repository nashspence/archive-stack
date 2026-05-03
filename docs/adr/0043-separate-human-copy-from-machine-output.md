# ADR-0043: Separate Human Copy from Machine Output

## Decision

Normal human-facing copy uses the established operator terms: collection, files, hot storage, disc, blank disc, replacement disc, label, storage location, cloud backup, recovery, disc restore, safe, needs attention, and fully protected.

`recovery` is reserved for cloud-backup restore approval, readiness, expiry, and replacement-disc work after protected physical copies are lost.

`disc restore` is the operator-facing name for bringing pinned or requested files back from optical media into hot storage. Internal statechart and code identifiers may keep `hot_recovery` where they name the implementation path, but normal copy must not ask operators to distinguish "Recovery" from "Hot Recovery".

Normal human-facing copy does not require the operator to understand candidates, finalized images, copy slots, Glacier object paths, fetch manifests, recovery-byte streams, hot-recovery internals, or protection-state enums.

Riverhog contracts normal human-facing text in `contracts/operator/copy.py` and shared human formatting in `contracts/operator/format.py`.

JSON output, API schemas, logs, and explicit debug or detail output may remain machine-shaped.

## Reason

The operator experience should stay calm and task-centered without weakening the precise machine contracts used by scripts, tests, and integrations.
