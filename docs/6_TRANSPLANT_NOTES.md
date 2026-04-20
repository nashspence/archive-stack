# Transplanted portions from the previous version (see git history for more information)

These parts were intentionally adapted into the clean MVP skeleton:

## Added modules

- `src/arc_core/fs_paths.py`
  - relative-path normalization
  - root-node-name normalization
  - parent path enumeration
  - safe unlink / safe tree removal

- `src/arc_core/hashing.py`
  - file SHA-256
  - canonical tree hash

- `src/arc_core/crypto_age.py`
  - age-size budgeting helpers
  - stream-oriented encrypt/decrypt helpers
  - logical hash/size helpers

- `src/arc_core/archive_artifacts.py`
  - collection hash manifest generation
  - collection artifact relpath generation
  - proof generation hook

- `src/arc_core/proofs.py`
  - stub proof stamper
  - external command proof stamper

- `src/arc_core/webhooks.py`
  - clean image-ready webhook payloads
  - reminder delivery service
  - generic store protocol for later persistence wiring

- `src/arc_api/auth.py`
  - optional bearer auth via `ARC_API_TOKEN`

- `src/arc_core/sqlite_db.py`
  - SQLite WAL/foreign-key engine helper
  - session factory / session scope helpers

## Design notes

The old codebase mixed these helpers into API orchestration and ORM-heavy flows.
Here they have been deliberately extracted as small, reusable modules so the clean MVP can still start fresh.

## Collection hash / proof capability

Use `generate_collection_hash_artifacts(...)` after you have a finalized collection source tree and an artifact root.
The default stamper writes a deterministic stub proof. Later you can swap in `CommandProofStamper([...])`.

## Image-ready webhook reminders

The new reminder module is intentionally image-oriented instead of container-oriented. It supports batches of one or more images that are ready for ISO download.
Back it with a repository implementing `ImageReadyReminderStore` when real image readiness exists.
