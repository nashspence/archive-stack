# ADR-0043: Separate Human Copy from Machine Output

## Decision

Normal human-facing copy uses the established operator terms: collection, files, hot storage, disc, blank disc, replacement disc, label, storage location, cloud backup, recovery, safe, needs attention, and fully protected.

Normal human-facing copy does not require the operator to understand candidates, finalized images, copy slots, Glacier object paths, fetch manifests, recovery-byte streams, or protection-state enums.

Riverhog contracts normal human-facing text in `contracts/operator/copy.py` and shared human formatting in `contracts/operator/format.py`.

JSON output, API schemas, logs, and explicit debug or detail output may remain machine-shaped.

## Reason

The operator experience should stay calm and task-centered without weakening the precise machine contracts used by scripts, tests, and integrations.
