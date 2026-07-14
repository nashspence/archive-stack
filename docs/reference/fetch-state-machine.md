# Fetch state machine

## States

```text
draft -> done
draft -> queued_archive -> restoring_archive -> done
queued_archive -> failed
restoring_archive -> failed
queued_archive -> draft
restoring_archive -> draft
```

## Behavior

A draft fetch has editable selectors. Starting it resolves the selectors against the current catalog and freezes them. An all-hot selection transitions directly to `done`.

A selection with missing hot files transitions to `queued_archive`. Riverhog creates or resumes collection archive restores, then marks the fetch `restoring_archive` while verified files are materialized. The fetch reaches `done` only when every selected file is hot.

Canceling archive work returns the fetch to `draft`. A failed fetch records the retrieval failure and can be inspected before the operator chooses a new attempt.
