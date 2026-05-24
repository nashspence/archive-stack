# Domain model

## Core nouns

Use these core nouns consistently:

- `collection` — the logical namespace the user thinks in
- `candidate` — one provisional planner proposal that may be re-allocated
- `image` — one finalized ISO artifact
- `copy` — one physical burned disc of an image
- `pin` — a declared requirement to keep a target materialized in hot storage
- `fetch` — the pin-scoped recovery manifest for one exact selector
- `recovery_session` — an approved Glacier restore or image rebuild workflow

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
- the timestamp is UTC collection-upload creation time
- no collection id may be an ancestor or descendant of another collection id
- accepted collections are immediately Glacier-backed and eligible for hot
  visibility and disc planning

### File

A logical file identified by `(collection_id, path)`.

### Hot storage

The server-side materialized cache of file bytes currently available without optical recovery.

Selectors operate over the projected hot namespace, not over literal hot-store paths on disk.

### Durable authoritative state

The authoritative archive state survives service restarts.

This includes at least:

- collections and their coverage summaries
- collection Glacier archive package state
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
- `archive_manifest`
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
- `archive_manifest` — manifest object path, manifest SHA-256, OTS proof object
  path, and OTS proof state for the collection archive package
- OTS proof state records proof object presence and integrity. Restore/recovery
  verification separately validates the `.ots` proof against the exact
  collection manifest with the configured OpenTimestamps verification command.
- `disc_coverage` — physical media coverage state and verified physical bytes
- `protection_state` — one of `under_protected`, `cloud_only`,
  `physical_only`, or `fully_protected`
- `image_coverage` — finalized-image physical coverage details for this
  collection, including registered copies

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
- `pricing_basis`
- `totals`
- `collections`
- `images`
- `billing`
- `history`

Glacier-usage-report rules:

- `totals.measured_storage_bytes` sums measured uploaded Glacier object bytes only
- `totals.estimated_billable_bytes` adds configured Glacier metadata overhead to that measured storage
- `totals.estimated_monthly_cost_usd` is a derived estimate from the emitted pricing basis
- `totals.collections` counts collection archive records
- `totals.uploaded_collections` counts collection archives in `uploaded` state
- `collections` expose direct measured usage for whole-collection Glacier
  archives, including manifest and OTS proof state
- `images` may explain which finalized images physically cover reported
  collections
- `billing.actuals` reports AWS-native actual cost periods separately from Riverhog's own storage snapshots
- `billing.actuals.scope` records whether actuals are `bucket`-scoped, `tag`-scoped, `service`-scoped, or unavailable
- `billing.actuals.billing_view_arn` records the AWS billing view Riverhog used for resource-level bucket actuals
- `billing.forecast` reports AWS-native forecast periods and keeps forecast scope separate from actual scope
- `billing.exports` exposes CUR or Data Exports-derived cost breakdowns when Riverhog can inspect a configured export
- `billing.exports` records resolved export, execution, manifest, billing-period, and file-count metadata separately
  from the aggregated breakdown rows
- `billing.invoices` exposes AWS invoice summaries as account-level totals rather than archive-specific attribution
- `history` stores overall Glacier-usage snapshots rather than collection-scoped estimates
- `pricing_basis.source` distinguishes AWS-resolved storage rates from manual fallback
- `pricing_basis.currency_code`, `pricing_basis.region_code`, and `pricing_basis.effective_at` identify the AWS
  lookup basis when Riverhog resolves live pricing
- `pricing_basis.archived_metadata_bytes_per_object`, `pricing_basis.standard_metadata_bytes_per_object`, and
  `pricing_basis.minimum_storage_duration_days` remain explicit Glacier storage-class constants

### Recovery session

A recovery session exposes at least:

- `id`
- `type`
- `state`
- `collections`
- `images`
- `cost_estimate`
- `notification`

Recovery-session rules:

- `type` is `collection_restore` or `image_rebuild`
- `collection_restore` restores collection content from the collection archive,
  manifest, and OTS proof
- `image_rebuild` restores the collection archives needed to rebuild a lost
  finalized image from persisted coverage metadata
- session cost estimates count the required collection archive restores

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
