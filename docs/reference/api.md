# API reference

This document summarizes the MVP HTTP and CLI contract. The canonical machine-readable shape is
`contracts/openapi/arc.v1.yaml`.

## HTTP API

All JSON endpoints are under `/v1`. Requests and responses use JSON unless otherwise specified.
Resumable upload URLs returned by the JSON API are Riverhog-managed tus-compatible resources.

Unless this contract explicitly says otherwise, authoritative resources created through the API remain addressable across
service restarts, including collections, finalized images, registered copies, active pins, active fetch manifests, and
unexpired upload progress.

### Collections

#### `POST /v1/collection-uploads`

Creates or resumes one explicit collection upload session.

Request body:

```json
{
  "collection_id": "photos/2024",
  "ingest_source": "/operator/photos/2024",
  "files": [
    {
      "path": "albums/japan/day-01.txt",
      "bytes": 18,
      "sha256": "..."
    }
  ]
}
```

Required behavior:

- the collection id is explicit rather than derived from any server-local path
- collection ids may contain `/`, for example `photos/2024`
- rejects a collection id if it would be an ancestor or descendant of an existing collection id
- persists enough upload-session state to survive service restart and repeated CLI runs
- keeps the collection invisible until every required file has uploaded,
  verified successfully, archived to Glacier, and verified by object receipt
- exposes per-file resumable upload state and collection-level progress
- reports collection-upload state as `uploading`, `archiving`, `finalized`, or
  `failed`

#### `GET /v1/collection-uploads/{collection_id}`

Returns the current upload-session state for one explicit collection id.

Required behavior:

- collection ids may span multiple path segments
- the returned state includes pending, partial, and uploaded file counts plus `upload_state_expires_at`
- an `archiving` session remains readable while Riverhog builds and uploads the
  collection archive package
- once the collection finalizes, the upload session is deleted and later reads
  return `not_found`
- a `failed` session remains retryable and the collection remains invisible

#### `POST /v1/collection-uploads/{collection_id}/files/{path}/upload`

Creates or resumes the resumable upload resource for one logical collection file.

Required behavior:

- the returned `upload_url` is a Riverhog-managed tus-compatible upload resource for that logical collection file
- repeated calls while the file remains resumable return the current upload resource rather than creating duplicates
- the response includes tus-style status headers such as `Tus-Resumable`, `Upload-Offset`, `Upload-Length`, and `Location`
- offsets and checksums are measured against the logical file byte stream for that file
- the terminal successful upload chunk may move the session into `archiving`,
  but it must not report final success until the collection archive package has
  uploaded to Glacier and verified successfully
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL`; once the last resumable file state expires, Riverhog forgets the upload session entirely and later retries start a fresh session

#### `HEAD /v1/collection-uploads/{collection_id}/files/{path}/upload`

Reads the current tus-style state for one existing collection-file upload resource.

Required behavior:

- returns `204`
- exposes `Tus-Resumable`, `Upload-Offset`, `Upload-Length`, and `Location`
- exposes `Upload-Expires` while the file still has incomplete resumable state
- returns `not_found` after the upload resource has been canceled, expired, or finalized away with the collection session

#### `DELETE /v1/collection-uploads/{collection_id}/files/{path}/upload`

Cancels one existing collection-file upload resource.

Required behavior:

- returns `204`
- cancels the current upload resource for that logical file
- deletes any incomplete server-side bytes for that file
- resets that file back to `pending`

#### `OPTIONS /v1/collection-uploads/{collection_id}/files/{path}/upload`

Describes the Riverhog-managed collection-file upload resource capabilities.

Required behavior:

- returns `204`
- exposes `Tus-Version`
- exposes `Tus-Extension`
- exposes `Tus-Checksum-Algorithm`

### Search

#### `GET /v1/search?q=<query>&limit=<n>`

Returns collection and file selectors that can be used directly with `pin` and `release`.

Required behavior:

- search is case-insensitive substring match over collection ids and full logical file paths
- file results include current hot availability
- file results include available copies, if any
- returned selectors use projected-path syntax only
- `limit` is honored

### Collections summary

#### `GET /v1/collections`

Lists collection summaries.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `100`
- `q` — case-insensitive substring filter over collection ids
- `protection_state` — exact filter over `under_protected`, `cloud_only`,
  `physical_only`, or `fully_protected`

Required behavior:

- the response includes pagination metadata and a `collections` array
- returned collection summaries use the same shape as `GET /v1/collections/{collection_id}`
- collection summaries are returned only for collections whose Glacier archive
  package has uploaded and verified
- filtering by `protection_state=fully_protected` can be used to answer which
  collections have both verified Glacier and full physical coverage
- collection summaries include direct collection Glacier state, archive
  manifest/OTS proof state, physical disc coverage, and finalized-image coverage

#### `GET /v1/collections/{collection_id}`

Returns a collection summary with byte coverage values.

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collections/photos/2024`
- API and CLI collection lookup treat slash-bearing ids as first-class
- collection summaries expose `glacier`, `archive_manifest`, `archive_format`,
  `compression`, `disc_coverage`, `protection_state`, `protected_bytes`, and
  per-image physical coverage details
- `glacier` is direct collection archive state, not a value derived from image
  coverage
- `archive_manifest` exposes manifest object path, manifest SHA-256, OTS proof
  object path, and OTS proof state
- per-image coverage details expose `covered_paths`, `physical_copies_registered`,
  `physical_copies_verified`, copy labels and locations
- collection coverage explains which finalized images, paths, and registered
  copies physically cover that collection

### File introspection

#### `GET /v1/collection-files/{collection_id}`

Lists the logical files in one collection.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `100`

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collection-files/photos/2024`
- the response includes pagination metadata and a `files` array
- each returned file includes its projected path and current hot or archived state
- each returned file includes available copies, if any

#### `GET /v1/files?target=<target>`

Returns logical files selected by one canonical target selector.

Supported query parameters:

- `target` — canonical projected-path selector
- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `100`

Required behavior:

- the `target` query parameter carries one canonical selector over the projected hot namespace
- the response includes pagination metadata, the canonical `target`, and a `files` array
- returned files use the same projected-path syntax accepted by `pin` and `release`
- file results include current hot availability
- file results include available copies, if any
- missing or non-matching targets return an empty list rather than `not_found`
- invalid target syntax is rejected with `invalid_target`

#### `GET /v1/files/{target:path}/content`

Downloads the bytes for one hot logical file.

Required behavior:

- the path target must select exactly one logical file, not a directory or broader selector
- content download succeeds only when the selected file is hot
- archived-only files return `not_found` and continue to use the fetch/upload recovery flow
- the response returns the file bytes rather than JSON

### Planning

#### `GET /v1/plan`

Returns the best current provisional planner output and readiness status.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`
- `sort` — one of `fill`, `bytes`, `files`, `collections`, or `candidate_id`
- `order` — `asc` or `desc`
- `q` — case-insensitive substring filter over `candidate_id`, contained collection ids, and represented projected file
  paths
- `collection` — exact collection-id filter over contained collection ids
- `iso_ready` — filters provisional candidates by whether they are currently ready to finalize

Required behavior:

- every returned plan entry is a provisional candidate
- every returned plan entry represents only collections whose Glacier archive
  package has uploaded and verified
- a provisional candidate may be re-allocated by the planner
- finalized images are not returned by `GET /v1/plan`
- the response includes pagination metadata and a `candidates` array
- the default ordering is fullest candidates first using `sort=fill&order=desc`
- explicit sort and filter controls only change how the current provisional plan is listed; they do not change planner
  allocation behavior
- plan candidate objects expose `candidate_id`
- plan candidate objects expose `collection_ids`
- plan candidate objects do not expose finalized-image fields such as finalized `id`, `filename`, `finalized_at`, or
  physical-copy protection metadata
- plan-specific fields such as `ready`, `target_bytes`, `min_fill_bytes`, and `unplanned_bytes` remain part of the
  response alongside the paged candidate listing

### Images

#### `GET /v1/images`

Lists finalized images.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`
- `sort` — one of `finalized_at`, `bytes`, or `physical_copies_registered`
- `order` — `asc` or `desc`
- `q` — case-insensitive substring filter over finalized image id, ISO filename, and contained collection ids
- `collection` — exact collection-id filter over contained collection ids
- `has_copies` — filters finalized images by whether at least one burned copy has been registered

Required behavior:

- this endpoint returns finalized images only
- provisional plan candidates are never returned by `GET /v1/images`
- default ordering is latest finalized image first using `sort=finalized_at&order=desc`
- the response includes pagination metadata and finalized-image summaries
- finalized-image summaries expose `filename`, `finalized_at`, `collection_ids`, `physical_protection_state`,
  `physical_copies_required`, `physical_copies_registered`, `physical_copies_verified`,
  and `physical_copies_missing`
- finalized-image summaries always report `iso_ready = true`
- finalized-image summaries report physical-copy state

#### `GET /v1/images/{image_id}`

Returns one image summary.

Required behavior:

- this endpoint returns finalized images only
- `image.id` is the canonical finalized image id
- finalized image ids use compact UTC basic form `YYYYMMDDTHHMMSSZ`
- the response uses the same finalized-image summary shape returned by `GET /v1/images`
- finalized image summaries always report `iso_ready = true`
- provisional plan candidates are not addressable through `GET /v1/images/{image_id}`

#### `GET /v1/images/{image_id}/rebuild-session`

Returns the latest image-rebuild recovery session for one finalized image.

Required behavior:

- returns the latest durable `image_rebuild` recovery session for that finalized
  image, including expired or completed sessions
- returns `not_found` when no recovery session has been created for that image

#### `POST /v1/images/{image_id}/rebuild-session`

Creates or resumes one `image_rebuild` recovery session for one finalized image.

Required behavior:

- creates a new `pending_approval` session only when the finalized image has no
  remaining protected copies and the collections required to rebuild it have
  uploaded collection Glacier archives
- repeated calls while one active session exists return that same active session rather than creating duplicates
- an expired or completed session does not block creating a new session id for the same image

#### `GET /v1/collections/{collection_id}/restore-session`

Returns the latest `collection_restore` recovery session for one collection.

Required behavior:

- returns the latest durable restore session for that collection, including
  expired or completed sessions
- returns `not_found` when no restore session has been created for that
  collection

#### `POST /v1/collections/{collection_id}/restore-session`

Creates or resumes one `collection_restore` recovery session for one collection.

Required behavior:

- creates a new `pending_approval` session only when the collection has an
  uploaded and verified Glacier archive package
- repeated calls while one active session exists return that same active session
  rather than creating duplicates
- an expired or completed session does not block creating a new session id for
  the same collection

#### `GET /v1/recovery-sessions/{session_id}`

Returns one Glacier-backed collection restore or image rebuild session by durable
session id.

Required behavior:

- recovery sessions remain addressable across service restart
- the response includes recovery `type`, cost estimate, operator warnings,
  notification state, covered collections, and any finalized images involved in
  an image rebuild
- the response includes `progress.archive_verification`, `progress.extraction`,
  and `progress.materialization`, each one of `pending`, `in_progress`,
  `completed`, or `failed`

#### `POST /v1/recovery-sessions/{session_id}/approve`

Approves one pending recovery session cost estimate.

Required behavior:

- approval is required before Riverhog requests restore work
- approval transitions the session from `pending_approval` to `restore_requested`
- readiness is based on archive-store restore status or direct archive-object readability, not only on the configured
  latency estimate
- approving an expired session is rejected with `invalid_state` and explains that recovery must be re-initiated

#### `POST /v1/recovery-sessions/{session_id}/complete`

Completes one ready recovery session.

Required behavior:

- completion is only valid from `ready`
- completion transitions the session to `completed`
- completion records cleanup or lifecycle handoff for restored Standard-storage data instead of waiting for Riverhog's
  session expiry

#### `POST /v1/recovery-sessions/{session_id}/collections/{collection_id}/materialize`

Materializes selected logical files from a ready `collection_restore` session
into committed hot storage.

Required request body:

- `paths` — non-empty list of logical file paths within the collection archive

Required behavior:

- succeeds only when the recovery session is `ready`
- the collection must belong to that recovery session
- Riverhog verifies the restored manifest, proof, archive membership, selected
  member byte counts, and selected member SHA-256 values before writing hot bytes
- only requested paths are materialized to hot storage
- the response reports completed archive verification, extraction, and
  materialization progress

#### `GET /v1/recovery-sessions/{session_id}/images/{image_id}/iso`

Downloads one rebuilt ISO from a ready `image_rebuild` recovery session.

Required behavior:

- succeeds only when the recovery session is `ready`
- the image must belong to that recovery session
- the response streams bytes from the rebuilt image artifact produced from
  restored collection archives and persisted coverage metadata
- `arc-disc recover` uses this endpoint for replacement burns instead of
  `GET /v1/images/{image_id}/iso`

#### `GET /v1/glacier`

Returns Glacier usage totals, direct per-collection archive state, collection
manifest/OTS proof state, pricing assumptions, AWS-native billing
actuals/forecast when available, optional CUR or Data Exports drill-down,
optional invoice summaries, and overall usage snapshots.

Supported query parameters:

- `collection` — narrows image and collection reporting to one exact collection id

Required behavior:

- the response always exposes `scope`, `measured_at`, `pricing_basis`, `totals`, `images`, `collections`, `billing`,
  and `history`
- `pricing_basis` states the assumptions used for cost estimates, including whether Riverhog resolved the storage
  rates from the AWS price-list API or from manual fallback values
- `pricing_basis.currency_code`, `pricing_basis.region_code`, and `pricing_basis.effective_at` make the AWS lookup
  basis explicit when Riverhog resolves live pricing
- `totals.measured_storage_bytes` reports measured uploaded Glacier object bytes, not billing overhead
- `totals.estimated_billable_bytes` includes configured Glacier metadata overhead on top of measured uploaded bytes
- `totals.estimated_monthly_cost_usd` is derived from the emitted pricing basis rather than observed cloud bills
- `totals.collections` counts collection archive records and
  `totals.uploaded_collections` counts those uploaded and verified
- each returned collection exposes direct Glacier archive state, measured
  uploaded bytes, estimated billable bytes, estimated monthly cost, manifest
  state, and OTS proof state
- returned image entries, when present, explain physical coverage of collections
- `billing` is separate from `history`: `history` tracks Riverhog's own stored-usage snapshots, while `billing`
  reports AWS-native billing views when Riverhog can resolve them
- `billing.actuals` prefers bucket-scoped AWS Cost Explorer daily actuals for the archive bucket when resource-level
  cost data is enabled, then falls back to tag-scoped or service-scoped Cost Explorer attribution
- `billing.actuals.billing_view_arn` is emitted when Riverhog resolves an AWS billing view for resource-level actuals
- `billing.forecast` is separate from `billing.actuals` because AWS forecast support is broader-scope than bucket
  resource actuals; Riverhog makes that forecast scope explicit
- `billing.exports` exposes CUR or Data Exports-derived billing breakdowns from the selected manifest and makes the
  resolved export ARN, execution id, manifest key, billing period, and file count explicit
- `billing.invoices` exposes AWS invoice summaries as account-level totals rather than archive-specific attribution
- unfiltered `GET /v1/glacier` returns overall usage snapshots that reflect changes in total uploaded Glacier usage over
  time

#### `POST /v1/plan/candidates/{candidate_id}/finalize`

Explicitly finalizes one ready provisional candidate and creates one finalized image resource.

Required behavior:

- this is the only operation that may create a finalized image id
- finalization assigns a unique immutable finalized image id in UTC basic form `YYYYMMDDTHHMMSSZ`
- if more than one image would otherwise finalize in the same second, later assignments advance in whole seconds until
  an unused id is found
- after finalization, the planner must not re-allocate that finalized image's represented bytes
- finalized candidates are not returned by `GET /v1/plan`
- repeated finalization of the same `candidate_id` is idempotent and returns the same finalized summary
- the finalized image record remains addressable after service restart
- finalization creates a physical image artifact and generated copy slots only
- finalized-image summaries report physical-copy protection state

#### `GET /v1/images/{image_id}/iso`

Returns ISO bytes if the image is ready.

Required behavior:

- ISO download does not finalize the image
- ISO download requires the finalized image to already exist
- subsequent downloads for the same finalized `image.id` reuse the same represented bytes
- this endpoint is not used for recovery-session burns

#### `POST /v1/images/{image_id}/copies`

Registers a physical burned disc for an image.

Required behavior:

- copy registration is only valid for an already finalized image
- the path `image_id` is the finalized image id
- the physical copy identity is `(volume_id, copy_id)`
- finalized images create exactly two generated copy ids by default, such as `{image_id}-1` and `{image_id}-2`
- if no `copy_id` is supplied, registration claims the next generated copy slot still in state `needed` or `burning`
- duplicate registration of the same generated `copy_id` is rejected with `conflict`
- the generated `copy_id` is also the exact disc label text Riverhog expects the operator to write
- `location` is mutable operational metadata and is never part of copy identity
- successful registration persists across service restart

#### `GET /v1/images/{image_id}/copies`

Lists the generated copy slots for one finalized image.

Required behavior:

- finalizing an image creates exactly two required copy slots by default
- if a confirmed copy is later reported `lost` or `damaged`, Riverhog preserves that historical record and may create a
  fresh generated replacement slot with a new `copy_id`
- each copy summary exposes generated identity, exact label text, current location, lifecycle state, verification state,
  and history

#### `PATCH /v1/images/{image_id}/copies/{copy_id}`

Updates one generated copy record.

Required behavior:

- location updates never mutate copy identity
- copy lifecycle state and verification state persist across service restart
- every location or state change is appended to copy history
- reporting one confirmed copy `lost` or `damaged` never reuses that same `copy_id` for replacement burn work
- when another protected copy still exists, replacement burn work is represented as a new generated `copy_id`

### Pins

#### `POST /v1/pin`

Pins a target into hot storage.

Required behavior:

- the `target` field carries one canonical selector over the projected hot namespace
- successful pin keeps the target desired in hot until explicitly released
- every successful exact pin creates or reuses one fetch manifest for that same selector
- if all targeted bytes are already hot, the returned fetch manifest is already in state `done`
- if some targeted bytes are archived but not hot, the returned fetch manifest is created or reused in a non-`done`
  state
- repeated pin of the same canonical selector is idempotent
- a successful pin remains active across service restart until explicitly released

#### `POST /v1/release`

Releases exactly one canonical selector pin.

Required behavior:

- releasing a non-existent exact pin is a successful no-op
- releasing a broader pin must not remove narrower remaining pins
- releasing a narrower pin must not remove broader remaining pins
- releasing the last exact pin for one selector abandons and removes that selector's fetch manifest
- releasing one exact pin also removes any hot files that are no longer selected by a remaining pin

#### `GET /v1/pins`

Lists active pins.

Required behavior:

- every returned pin includes its associated fetch id and current fetch state

### Fetches

#### `GET /v1/fetches/{fetch_id}`

Returns one pin-scoped fetch summary.

#### `GET /v1/fetches/{fetch_id}/manifest`

Returns a stable manifest for the exact pin lifetime.

- the fetch manifest is the source of truth for automated multipart recovery
- multipart logical files include part-level recovery hints
- `entries[].parts[]` are ordered by zero-based `index`
- every manifest entry includes logical plaintext `bytes` / `sha256` plus `recovery_bytes` for the ordered upload
  stream
- every part hint includes logical plaintext `bytes`, logical plaintext `sha256`, `recovery_bytes`, and at least one
  candidate recovery copy
- every candidate recovery copy includes `disc_path`, `recovery_bytes`, and `recovery_sha256`
- `arc-disc` uploads the raw encrypted bytes stored at `disc_path`, not reconstructed logical plaintext
- logical plaintext hash and size fields remain server-side verification anchors after decryption and reconstruction
- each manifest entry exposes current upload state, uploaded bytes, and upload expiry if partial state exists
- fetch entry upload states distinguish `pending`, `partial`, `byte_complete`, and `uploaded`
- `byte_complete` means the full ordered recovery-byte stream has been accepted but `POST /complete` has not yet finished server-side verification and materialization
- those hints drive disc sequencing and resumable recovery in `arc-disc`
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk and the manifest returns to
  `waiting_media`
- fetch summaries expose an audit field such as `upload_state_expires_at`
- fetch summaries expose separate `entries_byte_complete` and `entries_uploaded` counts
- the fetch manifest and any unexpired upload progress survive service restart while the exact pin remains active

#### `POST /v1/fetches/{fetch_id}/entries/{entry_id}/upload`

Creates or resumes the resumable upload resource for one manifest entry.

Required behavior:

- the response returns one upload URL bound to exactly one logical file entry
- the returned upload URL is a Riverhog-managed tus-compatible upload resource for that manifest entry
- the response includes current offset, total length, transport checksum algorithm, and expiry time
- offset and length are measured in the entry's ordered recovery-byte stream
- repeated calls while the upload remains resumable return the current upload resource rather than creating duplicates
- the server owns any required decryption and final logical-file validation behind that upload resource

#### `HEAD /v1/fetches/{fetch_id}/entries/{entry_id}/upload`

Reads the current tus-style state for one existing fetch-entry upload resource.

Required behavior:

- returns `204`
- exposes `Tus-Resumable`, `Upload-Offset`, `Upload-Length`, and `Location`
- exposes `Upload-Expires` while the entry still has incomplete resumable state
- returns `not_found` after the upload resource has been canceled or expired away

#### `DELETE /v1/fetches/{fetch_id}/entries/{entry_id}/upload`

Cancels one existing fetch-entry upload resource.

Required behavior:

- returns `204`
- cancels the current upload resource for that manifest entry
- deletes any incomplete or byte-complete server-side bytes for that entry
- resets that entry back to `pending`

#### `OPTIONS /v1/fetches/{fetch_id}/entries/{entry_id}/upload`

Describes the Riverhog-managed fetch-entry upload resource capabilities.

Required behavior:

- returns `204`
- exposes `Tus-Version`
- exposes `Tus-Extension`
- exposes `Tus-Checksum-Algorithm`

#### `POST /v1/fetches/{fetch_id}/complete`

Marks the fetch manifest satisfied once all required entries have been uploaded, verified, and materialized. The
manifest remains readable while the exact pin remains active.

If verification fails, the fetch remains active and incomplete. Clients should delete the affected `byte_complete` entry
upload resource before retrying from another registered copy or from recovered media.

## Error model

All non-2xx responses return JSON with at least:

- `error.code`
- `error.message`

Suggested error codes:

- `invalid_target`
- `not_found`
- `conflict`
- `invalid_state`
- `hash_mismatch`
- `bad_request`

## CLI parity

### `arc`

The `arc` CLI is a thin API client and should provide at least:

- `arc upload COLLECTION_ID ROOT`
- `arc find QUERY`
- `arc show COLLECTION`
- `arc show COLLECTION --files`
- `arc status [TARGET]`
- `arc get TARGET [-o FILE]`
- `arc plan [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--iso-ready|--not-ready]`
- `arc images [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--has-copies|--no-copies]`
- `arc glacier [--collection ID]`
- `arc iso get IMAGE_ID [-o FILE]`
- `arc copy add IMAGE_ID --at LOCATION [--copy-id GENERATED_ID]`
- `arc copy list IMAGE_ID`
- `arc copy move IMAGE_ID GENERATED_ID --to LOCATION`
- `arc copy mark IMAGE_ID GENERATED_ID --state STATE [--verification-state STATE]`
- `arc pin TARGET`
- `arc release TARGET`
- `arc pins`
- `arc fetch FETCH_ID`

`arc show COLLECTION --files` should provide a concise human-readable listing of the collection's logical files, including current hot or archived state and available copies when applicable.

`arc show COLLECTION` should provide a concise human-readable recovery and coverage view for one collection, including:

- an explicit summary of whether the collection is currently recoverable from verified physical copies, Glacier, both,
  or neither
- finalized images currently covering the collection
- projected paths carried by each image
- generated disc ids, exact label text, locations, and verification state
- direct collection Glacier object paths, manifest/OTS proof state, measured
  storage footprint, and monthly cost estimate

`arc status [TARGET]` should show file availability for either the whole projected namespace or one target selector.

`arc get TARGET [-o FILE]` should download one hot file target and fail clearly when the target is archived-only or does not select exactly one file.

`arc fetch FETCH_ID` should provide a concise human-readable listing of:

- files still pending upload
- files currently partial and still resumable
- the expiry time for each partial upload

For finalized-image commands:

- `IMAGE_ID` means the finalized image id
- finalized image ids use compact UTC basic form `YYYYMMDDTHHMMSSZ`
- `arc images --json` mirrors the `GET /v1/images` response payload
- `arc glacier --json` mirrors the `GET /v1/glacier` response payload
- `arc plan --json` mirrors the `GET /v1/plan` response payload
- non-JSON `arc plan` output stays concise and line-oriented while surfacing candidate id, fill, readiness, and
  contained collections
- non-JSON `arc images` acts as the default physical-media status view and stays concise while surfacing:
  ready-to-finalize provisional images, backlog still waiting for inclusion in a future ISO, finalized-image burn and
  verification backlog, non-compliant collections, and which collections are
  already fully protected
- non-JSON `arc glacier` output stays line-oriented while surfacing measured usage totals, configured pricing basis,
  direct collection archive state, manifest/OTS proof state, and collection
  cost estimates

### `arc-disc`

The `arc-disc` CLI is a fetch-fulfillment client for a machine with an optical drive and should provide:

- `arc-disc fetch FETCH_ID [--device DEVICE]`

For multipart recovery, one invocation should continue across successive discs until every required
part has been recovered, streamed, and uploaded.

Required behavior:

- complete files stream straight from optical recovery into the upload resource rather than being materialized to disk
  first
- split files stream into the same logical-file upload resource in ascending part order
- the upload resource receives raw encrypted recovery bytes exactly as stored in the hinted payload object(s)
- `arc-disc` treats the upload resource as opaque and does not own decryption or final logical-file hash validation
- resumable offsets remain valid only for the exact recovery-byte stream accepted so far for the current span
- any temporary buffering used during recovery is an internal implementation detail
- progress output is precise and continuous, including current transfer rate, percent complete for the current file, and
  percent complete for the whole manifest

## Behavioral invariants

- pinning the same selector twice results in exactly one active pin
- pinning the same exact selector twice reuses the same fetch manifest while that exact pin remains present
- releasing a target not currently pinned is a successful no-op
- a file is logically required in hot if and only if at least one active pin selects it
- immediately after collection finalization, every file in the collection is hot
- every active pin has exactly one associated fetch manifest, even when the selected bytes are already hot
- a file restored by a completed fetch is hot
- hot file content is directly downloadable when the target selects exactly one file
- archived-only file content is recoverable through fetch/upload, not through hot-content download
- upload-state expiry for a manifest discards incomplete partial uploads and returns that manifest to `waiting_media`
- `INCOMPLETE_UPLOAD_TTL` defaults to `24h`
- fetch upload progress is tracked per logical file, not per disc fragment
- every entry returned by `GET /v1/plan` is provisional and exposes `candidate_id`
- explicit finalization is the only path that creates a finalized image id
- finalized candidates are not returned by `GET /v1/plan`
- ISO download requires an already finalized image and uses the same represented bytes on every later download
- registering a copy cannot reduce archived coverage
- a physical copy is identified by `(volume_id, copy_id)`, never by `location`
- generated `copy_id` values are stable and never mutated by location or state updates
- no collection id is an ancestor or descendant of another collection id
- collection ingest uses explicit resumable upload sessions and finalizes only
  after every required file verifies and the collection Glacier archive package
  uploads and verifies
- the same canonical selector string means the same projected file set everywhere in API and CLI
- file availability shown by search, file introspection, pins, and CLI status uses the same hot/archived meaning
