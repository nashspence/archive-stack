# Domain model

## Core nouns

Use these five nouns consistently:

- `collection` — the logical namespace the user thinks in
- `image` — one provisional or finalized ISO artifact
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

### Image

A planned optical artifact addressed by stable API `image.id`.

Image lifecycle rules:

- before the first ISO download request, an image is provisional and its represented collections may still be
  re-allocated by the planner
- the first ISO download request finalizes the image allocation
- finalization assigns and stores immutable `volume_id` for that `image.id`
- `volume_id` is the media-facing identifier carried in the ISO and disc manifest

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

### Image summary

An image summary exposes at least:

- `id`
- `volume_id`
- `bytes`
- `fill`
- `files`
- `collections`
- `iso_ready`

### Copy summary

A copy summary exposes at least:

- `id`
- `image`
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

### Pin summary

A pin summary exposes at least:

- `target`
- `fetch`
