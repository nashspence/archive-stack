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

A logical namespace uploaded through an explicit collection-upload session. A collection has a stable id and contains many files at stable
relative paths.

Collection-id rules:

- the id is explicit and canonical
- the id may contain `/`, for example `photos/2024`
- no collection id may be an ancestor or descendant of another collection id

### File

A logical file identified by `(collection_id, path)`.

### Hot storage

The server-side materialized cache of file bytes currently available without optical recovery.

Selectors operate over the projected hot namespace, not over literal hot-store paths on disk.

### Durable authoritative state

The authoritative archive state survives service restarts.

This includes at least:

- collections and their coverage summaries
- finalized images and registered copies
- exact pins and their fetch manifests
- hot-residency state and any unexpired resumable-upload progress

Implementations may rebuild derived projections during restart while keeping the same authoritative state.

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

- finalized images create two generated copy ids by default using `{image_id}-N`
- the generated `copy_id` is the exact disc label text to write on media
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
- `protection_state`
- `protected_bytes`
- `image_coverage`

Definitions:

- `bytes` — total bytes of all logical files in the collection
- `hot_bytes` — total bytes currently materialized in hot storage for files in the collection
- `archived_bytes` — total bytes stored on at least one registered copy
- `pending_bytes` — `bytes - archived_bytes`
- `protected_bytes` — total logical-file bytes currently covered by protected finalized images
- `protection_state` — one of `unprotected`, `partially_protected`, or `protected`
- `image_coverage` — finalized-image coverage details for this collection, including registered copies and Glacier state

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
- `protection_state`
- `physical_copies_required`
- `physical_copies_registered`
- `physical_copies_missing`
- `glacier`

Finalized-image summary rules:

- `collections` is the count of contained collection ids
- `collection_ids` is the lexically sorted list of contained collection ids
- `finalized_at` is the UTC timestamp encoded by finalized `image.id`
- finalized images always report `iso_ready = true`
- `protection_state` is one of `unprotected`, `partially_protected`, or `protected`
- `physical_copies_required` defaults to `2`
- `physical_copies_registered` counts currently registered or verified physical copies
- `physical_copies_missing` is the remaining shortfall to the required physical-copy count
- `glacier` summarizes current Glacier archive state and metadata for that finalized image
- `glacier.state` progresses through `pending`, `uploading`, `uploaded`, `retrying`, or `failed`
- `glacier.object_path` uses a privacy-safe finalized-image key and never embeds collection ids or logical file paths
- after restart, an `uploading` job either reconciles an already completed object at that privacy-safe key or starts a
  fresh upload attempt; Riverhog does not resume one in-flight remote multipart stream

### Copy summary

A copy summary exposes at least:

- `id`
- `volume_id`
- `label_text`
- `location`
- `created_at`
- `state`
- `verification_state`

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
- `entries_byte_complete`
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
