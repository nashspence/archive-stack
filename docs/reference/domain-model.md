# Domain model

## Core nouns

Use these five nouns consistently:

- `collection` — the logical namespace the user thinks in
- `image` — one planned ISO artifact
- `copy` — one physical burned disc of an image
- `pin` — a declared requirement to keep a target materialized in hot storage
- `fetch` — an operational recovery job created only when a pin needs archived bytes that are not currently hot

## Core terms

### Collection

A logical namespace closed from a staged directory. A collection has a stable id and contains many files at stable
relative paths.

### File

A logical file identified by `(collection_id, path)`.

### Hot storage

The server-side materialized cache of file bytes currently available without optical recovery.

### Target

A selector naming either a whole collection, a directory prefix within a collection, or a single file within a collection.

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
- `bytes`
- `fill`
- `files`
- `collections`
- `iso_ready`

### Copy summary

A copy summary exposes at least:

- `id`
- `image`
- `location`
- `created_at`

### Fetch summary

A fetch summary exposes at least:

- `id`
- `target`
- `state`
- `files`
- `bytes`
- `copies`

### Pin summary

A pin summary exposes at least:

- `target`
