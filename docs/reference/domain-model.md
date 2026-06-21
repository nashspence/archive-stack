# Domain model

## Core nouns

Use these core nouns consistently:

- `collection` — the logical namespace the user thinks in
- `candidate` — one provisional planner proposal that may be re-allocated
- `image` — one finalized ISO artifact
- `copy` — one physical burned disc of an image
- `fetch` — a named, operator-created manifest of target selectors that can be
  fulfilled from optical media or cloud archive data
- `hot eviction` — explicit removal of compliant hot bytes from the fast cache
- `recovery_session` — an automatic Glacier restore or image rebuild workflow

## Core terms

### Collection

A logical namespace uploaded from a human-readable slug. A collection has a
server-minted stable id and contains many files at stable relative paths.

Riverhog accepts a collection only after every uploaded file verifies and the
whole-collection Glacier archive package has uploaded and verified.

Collection-id rules:

- clients provide a slug, not a collection id
- Riverhog normalizes the slug and mints ids like
  `2026/20260524T190233Z__mom-iphone-photos`
- the timestamp defaults to UTC collection-upload creation time
- migration uploads may provide an explicit UTC basic timestamp like
  `20250712T213200Z`; the slug is still required
- no collection id may be an ancestor or descendant of another collection id
- accepted collections are immediately Glacier-backed and eligible for hot
  visibility and disc planning

### File

A logical file identified by `(collection_id, path)`.

### Hot storage

The server-side materialized cache of file bytes currently available without optical recovery.

Selectors operate over the projected hot namespace, not over literal hot-store paths on disk.

Immediately after collection finalization, files are hot cache entries.
Under-protected files are not evictable until enough verified physical copies
exist. Operators use `riverhog hot evict` to synchronously remove compliant hot
bytes from the cache.

### Durable authoritative state

The authoritative archive state survives service restarts.

This includes at least:

- collections and their coverage summaries
- collection Glacier archive package state
- finalized images and registered copies
- named fetch manifests and their selector targets
- hot-residency state and any unexpired resumable-upload progress

Implementations may rebuild derived projections during restart while keeping the same authoritative state.

### Candidate

A provisional planner proposal addressed by `candidate_id`.

Candidate lifecycle rules:

- while a candidate appears in `GET /v1/plan`, it is provisional and its represented collections may be
  re-allocated by the planner
- `POST /v1/plan/candidates/{candidate_id}/finalize` explicitly finalizes that candidate allocation; this is an internal
  API operation used by `djdan burn`, not a standalone operator CLI action
- finalized candidates do not appear in `GET /v1/plan`
- repeated finalization of the same `candidate_id` is idempotent and returns the same finalized image

### Image

A finalized optical artifact addressed by finalized API `image.id`.

Image lifecycle rules:

- finalized images are created only when `djdan burn` finalizes a ready candidate or when internal/test tooling seeds a
  finalized image
- finalized images are not returned by `GET /v1/plan`
- `GET /v1/images/{image_id}` addresses finalized images only
- finalized `image.id` uses compact UTC basic form `YYYYMMDDTHHMMSSZ`
- finalized `image.id` is the same media-facing identifier carried on the ISO and disc manifest
- finalized images are physical recovery artifacts; Glacier archive state belongs
  to collections

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
- copy registration records the generated id, location, and lifecycle state
  synchronously with the per-file recovery-index rows
- each covered file-copy row records the on-disc payload path plus exact encrypted recovery-byte length; missing
  recovery-byte digests are backfilled before fetch manifests expose cold-recovery copy hints

## Summary models

### Collection summary

A collection summary exposes at least:

- `id`
- `files`
- `bytes`
- `hot_bytes`
- `archived_bytes`
- `pending_bytes`
- `glacier`
- `collection_manifest`
- `archive_format`
- `compression`
- `disc_coverage`
- `protection_state`
- `protected_bytes`
- `image_coverage`

Definitions:

- `bytes` — total bytes of all logical files in the collection
- `hot_bytes` — total bytes currently materialized in hot storage for files in the collection
- `archived_bytes` — total bytes stored on at least one registered copy
- `pending_bytes` — `bytes - archived_bytes`
- `protected_bytes` — total logical-file bytes currently covered by enough
  verified physical copies while the collection archive remains uploaded and
  verified
- `glacier` — direct collection archive state and object metadata
- `collection_manifest` — manifest object path, manifest SHA-256, OTS proof object
  path, and OTS proof state for the collection archive package
- OTS proof state records proof object presence and integrity. Restore/recovery
  verification separately validates the `.ots` proof against the exact
  collection manifest with the configured OpenTimestamps verification command.
- `disc_coverage` — physical media coverage state and verified physical bytes
- `protection_state` — one of `under_protected`, `cloud_only`,
  `physical_only`, or `fully_protected`
- `image_coverage` — finalized-image physical coverage details for this
  collection, including registered copies

### Fetch operator projections

Fetch operator views are maintained as fetch-keyed projections:

- `fetch_operator_summaries` stores the bounded list/show summary fields for
  each fetch
- `fetch_operator_files` stores the resolved logical files selected by each
  fetch target, including current hot, archive, and registered disc coverage

`riverhog hot fetch list`, `riverhog hot fetch show`, and
`riverhog hot fetch files` read these projections for routine operator display.
They must not resolve selectors by scanning unbounded collection-file rows at
read time.

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
- `physical_protection_state`
- `physical_copies_required`
- `physical_copies_registered`
- `physical_copies_verified`
- `physical_copies_missing`

Finalized-image summary rules:

- `collections` is the count of contained collection ids
- `collection_ids` is the lexically sorted list of contained collection ids
- `finalized_at` is the UTC timestamp encoded by finalized `image.id`
- finalized images always report `iso_ready = true`
- `physical_protection_state` is one of `unprotected`,
  `partially_protected`, or `protected`
- `physical_copies_required` defaults to `2`
- `physical_copies_registered` counts currently registered or verified physical copies
- `physical_copies_verified` counts registered copies whose verification state is
  `verified`
- `physical_copies_missing` is the remaining shortfall to the required physical-copy count
- finalized-image protection is physical-copy state

### Glacier usage report

A Glacier-usage report exposes at least:

- `scope`
- `measured_at`
- `totals`
- `collections`
- `images`
- `history`

Glacier-usage-report rules:

- `totals.measured_storage_bytes` sums measured uploaded archive-store bytes, including
  Standard S3 collection manifest and OTS proof objects
- `totals.collections` counts collection archive records
- `totals.uploaded_collections` counts collection archives in `uploaded` state
- `collections` expose direct measured usage for whole-collection archive object
  sets, including manifest and OTS proof state
- `images` may explain which finalized images physically cover reported
  collections
- `history` stores overall Glacier-usage snapshots rather than collection-scoped rows

### Recovery session

A recovery session exposes at least:

- `id`
- `type`
- `state`
- `collections`
- `images`
- `notification`

Recovery-session rules:

- `type` is `collection_restore` or `image_rebuild`
- `collection_restore` is the internal collection-native session type behind
  fetch-scoped cloud-fetch recovery; it restores collection content from the
  collection archive, manifest, and OTS proof
- `image_rebuild` restores the collection archives needed to rebuild a lost
  finalized image from persisted coverage metadata
- session cost estimates count the required collection archive restores
- AWS S3 Glacier restores use the configured retrieval tier and ready TTL as a
  temporary-copy window; the archive object remains in its archive storage class
  until copied elsewhere
- collection manifests and OTS proofs are Standard S3 sibling objects, so
  recovery reads them directly and only restores archived `archive.tar` payloads
- recovered image rebuilds stream restored archive tars into a temporary image
  tree and stream the rebuilt ISO from `xorriso`; Riverhog does not keep whole
  collection archives or replacement ISOs in process memory

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

- `bytes` — total logical-file bytes selected by the fetch targets
- `uploaded_bytes` — accepted bytes in the fetch's ordered recovery-byte upload streams
- `missing_bytes` — remaining bytes in those ordered recovery-byte upload streams

### Fetch summary

A fetch summary exposes at least:

- `id`
- `name`
- `targets`
- `state`
- `files`
- `bytes`
- `missing_bytes`
