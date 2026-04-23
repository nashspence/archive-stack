# Domain model

## Core nouns

Use these six nouns consistently:

- `collection` — the logical namespace the user thinks in
- `candidate` — one provisional planner proposal that may be re-allocated
- `image` — one finalized ISO artifact
- `copy` — one physical burned disc of an image
- `pin` — a declared requirement to keep a target materialized in hot storage
- `fetch` — the pin-scoped recovery manifest for one exact selector

## Core terms

### Collection

A logical namespace closed from a staged directory. A collection has a stable id and contains many files at stable
relative paths.

Collection-id rules:

- the id is the canonical relative path beneath the staging root for the closed directory
- the id may contain `/`, for example `photos/2024`
- no collection id may be an ancestor or descendant of another collection id

### File

A logical file identified by `(collection_id, path)`.

### Hot storage

The server-side materialized cache of file bytes currently available without optical recovery.

Selectors operate over the projected hot namespace, not over literal hot-store paths on disk.

### Candidate

A provisional planner proposal addressed by `candidate_id`.

Candidate lifecycle rules:

- while a candidate appears in `GET /v1/plan`, it is provisional and its represented collections may be
  re-allocated by the planner
- `POST /v1/plan/candidates/{candidate_id}/finalize` explicitly finalizes that candidate allocation
- finalized candidates do not appear in `GET /v1/plan`
- repeated finalization of the same `candidate_id` is idempotent and returns the same finalized image

### Image

A finalized optical artifact addressed by finalized API `image.id`.

Image lifecycle rules:

- finalized images are created only by explicit candidate finalization
- finalized images are not returned by `GET /v1/plan`
- `GET /v1/images/{image_id}` addresses finalized images only
- finalized `image.id` uses compact UTC basic form `YYYYMMDDTHHMMSSZ`
- finalized `image.id` is the same media-facing identifier carried on the ISO and disc manifest

### Target

A selector over the projected hot namespace naming either:

- a projected directory that may span multiple collections
- a projected file

### Copy

A physical burned disc identified by `(volume_id, copy_id)`.

Copy rules:

- `copy_id` is operator-supplied and unique within one finalized image/`volume_id`
- `location` is mutable operational metadata
- `location` is never part of copy identity

## Summary models

### Collection summary

A collection summary exposes at least:

- `id`
- `files`
- `bytes`
- `hot_bytes`
- `archived_bytes`
- `pending_bytes`

Definitions:

- `bytes` — total bytes of all logical files in the collection
- `hot_bytes` — total bytes currently materialized in hot storage for files in the collection
- `archived_bytes` — total bytes stored on at least one registered copy
- `pending_bytes` — `bytes - archived_bytes`

### Candidate summary

A candidate summary exposes at least:

- `candidate_id`
- `bytes`
- `fill`
- `files`
- `collections`
- `collection_ids`
- `iso_ready`

Candidate-summary rules:

- `collections` is the count of contained collection ids
- `collection_ids` is the lexically sorted list of contained collection ids
- candidate summaries remain provisional and never expose finalized-image ids or finalized-image-only fields

### Image summary

An image summary exposes at least:

- `id`
- `filename`
- `finalized_at`
- `bytes`
- `fill`
- `files`
- `collections`
- `collection_ids`
- `iso_ready`
- `copy_count`

Finalized-image summary rules:

- `collections` is the count of contained collection ids
- `collection_ids` is the lexically sorted list of contained collection ids
- `finalized_at` is the UTC timestamp encoded by finalized `image.id`
- finalized images always report `iso_ready = true`
- `copy_count` is the number of registered physical copies for that finalized image

### Copy summary

A copy summary exposes at least:

- `id`
- `volume_id`
- `location`
- `created_at`

### Fetch summary

A fetch summary exposes at least:

- `id`
- `target`
- `state`
- `files`
- `bytes`
- `entries_total`
- `entries_pending`
- `entries_partial`
- `entries_uploaded`
- `uploaded_bytes`
- `missing_bytes`
- `copies`
- `upload_state_expires_at`

Definitions:

- `bytes` — total logical-file bytes selected by the exact pin
- `uploaded_bytes` — accepted bytes in the fetch's ordered recovery-byte upload streams
- `missing_bytes` — remaining bytes in those ordered recovery-byte upload streams

### Pin summary

A pin summary exposes at least:

- `target`
- `fetch`
