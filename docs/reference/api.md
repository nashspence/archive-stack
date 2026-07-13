# API reference

This document summarizes the MVP HTTP and CLI contract. The canonical machine-readable shape is
`contracts/openapi/riverhog.v1.yaml`.

## HTTP API

All JSON endpoints are under `/v1`. Requests and responses use JSON unless otherwise specified.
Resumable upload URLs returned by the JSON API are Riverhog-managed tus-compatible resources.

Unless this contract explicitly says otherwise, authoritative resources created through the API remain addressable across
service restarts, including collections, finalized images, registered copies, active fetch manifests, and unexpired upload
progress.

### Collections

#### `POST /v1/collection-uploads`

Creates or resumes one determinate collection upload from a human-readable slug
and a complete file manifest. Riverhog normalizes the slug and mints the
canonical collection id.

Request body:

```json
{
  "slug": "mom iphone photos",
  "upload_timestamp": "20250712T213200Z",
  "ingest_source": "/operator/photos/2024",
  "notify": {
    "enabled": true,
    "recipients": ["operator"]
  },
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

- the client does not provide a collection id
- the server normalizes the slug and creates collection ids like
  `2026/20260524T190233Z__mom-iphone-photos`
- the timestamp is minted in UTC by the server unless the request provides
  `upload_timestamp` in UTC basic form `YYYYMMDDTHHMMSSZ`
- `upload_timestamp` is optional and exists for migration of archival sets whose
  original timestamp should be preserved; `slug` remains required
- optional `notify` targets are persisted with the collection upload and reused
  for Riverhog-owned collection lifecycle events such as `collections.finalized`
  and archival failures
- retries with the same normalized slug and file manifest resume the existing
  upload or return the already-finalized collection
- persists enough upload-session state to survive service restart and repeated CLI runs
- keeps the collection invisible until every required file has uploaded,
  verified successfully, archived to Glacier, and verified by object receipt
- persists the completed archive, collection manifest, and OTS proof receipts
  before hot-file promotion, so restart retries do not re-upload completed
  archive-store objects
- commits the finalized collection/archive records before deleting staged bytes,
  preserving restart-safe retries during archive and promotion failures
- exposes per-file resumable upload state and collection-level progress
- reports collection-upload state as `uploading`, `archiving`, `finalized`, or
  `failed`

#### `POST /v1/collection-upload-sessions`

Creates or resumes one incremental collection upload session from a
human-readable slug without requiring a complete file manifest up front.

Request body:

```json
{
  "slug": "mom iphone photos",
  "upload_timestamp": "20250712T213200Z",
  "ingest_source": "/operator/photos/2024",
  "notify": {
    "enabled": true,
    "recipients": ["operator"]
  }
}
```

Required behavior:

- the client does not provide a collection id
- the server normalizes the slug and creates collection ids like
  `2026/20260524T190233Z__mom-iphone-photos`
- repeated calls with the same normalized slug resume an open session until it
  is completed, canceled, or expired
- if `upload_timestamp` is provided, repeated calls target that exact canonical
  collection id
- optional `notify` targets are persisted with the collection upload and reused
  for Riverhog-owned collection lifecycle events such as `collections.finalized`
  and archival failures
- the returned session state is `open`
- open sessions remain mutable until explicitly completed or canceled
- open sessions expire after `RIVERHOG_UPLOAD_SESSION_IDLE_TTL` without
  activity and transition to `expired`

#### `POST /v1/collection-upload-sessions/{collection_id}/files`

Registers one logical file in an open incremental collection upload session.

Request body:

```json
{
  "path": "albums/japan/day-01.txt",
  "bytes": 18,
  "sha256": "..."
}
```

Required behavior:

- collection ids may span multiple path segments
- file paths are normalized relative paths inside the collection
- re-registering the same path with identical `bytes` and `sha256` is
  idempotent
- re-registering the same path with different metadata is rejected
- file bytes are uploaded to the direct tusd URL returned by
  `POST /v1/collection-uploads/{collection_id}/files/{path}/upload`
- each successful registration refreshes the open-session idle TTL

#### `POST /v1/collection-upload-sessions/{collection_id}/complete`

Freezes and completes one open incremental collection upload session.

Required behavior:

- collection ids may span multiple path segments
- completion requires at least one registered file
- completion requires every registered file to have uploaded and verified
- once accepted, the session transitions to `archiving` and uses the same
  Glacier archive finalization path as determinate uploads
- repeated completion after archival handoff is idempotent while the upload
  session remains visible
- a finalized collection response is returned if finalization has already
  completed

#### `POST /v1/collection-upload-sessions/{collection_id}/cancel`

Cancels one open incremental collection upload session.

Required behavior:

- collection ids may span multiple path segments
- canceling deletes staged upload bytes and cancels outstanding tus resources
- the session transitions to `canceled` and remains as a small audit record
- sessions cannot be canceled after explicit completion hands the collection to
  archiving

#### `GET /v1/collection-uploads/{collection_id}`

Returns the current upload-session state for one server-minted collection id.

Required behavior:

- collection ids may span multiple path segments
- the returned state includes pending, partial, and uploaded file counts plus `upload_state_expires_at`
- an `archiving` session remains readable while Riverhog builds and uploads the
  collection archive package
- once the collection finalizes, the upload session is deleted and later reads
  return `not_found`
- retryable archival failures keep the session in `archiving` with retry status,
  and the collection remains invisible until finalization succeeds

#### `POST /v1/collection-uploads/{collection_id}/files/{path}/upload`

Creates or resumes the resumable upload resource for one logical collection file.

Required behavior:

- the returned `upload_url` is a direct tusd URL for that logical collection file
- clients send TUS `HEAD`, `PATCH`, and `DELETE` requests directly to the
  returned `upload_url`; Riverhog does not proxy collection file bytes
- repeated calls while the file remains resumable return the current upload
  resource rather than creating duplicates
- the response includes tus-style status headers such as `Tus-Resumable`, `Upload-Offset`, `Upload-Length`, and `Location`
- offsets and checksums are measured against the logical file byte stream for that file
- completing all file uploads leaves an incremental session `open`; the client
  must call `/v1/collection-upload-sessions/{collection_id}/complete`
- incomplete file upload state expires after `INCOMPLETE_UPLOAD_TTL`; once the
  last resumable file state expires, Riverhog forgets a determinate upload
  session entirely and later retries start a fresh session

### Search

#### `GET /v1/search`

Returns paged collection-file targets for operator lookup and hot-storage selection.

Supported query parameters:

- `q` — case-insensitive substring search over collection ids and full logical file paths
- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `100`
- `sort` — one of `target`, `collection`, `path`, `bytes`, `hot`, or `archived`
- `order` — `asc` or `desc`
- `collection` — exact collection id filter
- `hot` — optional boolean hot-storage filter
- `archived` — optional boolean deep-archive filter

Required behavior:

- search is case-insensitive substring match over collection ids and full logical file paths
- the response includes pagination metadata and a `files` array
- file results include projected target, collection id, relative path, byte count,
  and current hot/deep-archive state

### Jeb

Riverhog exposes Jeb operator endpoints under `/v1/jeb`. These endpoints proxy
the service-local Jeb collector API; clients should use these Riverhog API paths
instead of running the collector CLI remotely.

#### `GET /v1/jeb/status`

Returns collector status, source readiness, active attempts, recent failures,
and routing preflight failures.

Supported query parameters:

- `include_backlog` — boolean, default `true`; when false, skips source
  directory eligible-file scans

#### `GET /v1/jeb/batches`

Lists Jeb batch attempts with paged, server-side sort and filter support.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `500`
- `sort` — indexed Jeb attempt or batch summary field, default `updated_at`
- `order` — `asc` or `desc`, default `desc`
- `terminal` — `active`, `terminal`, or `all`, default `active`
- `state` — exact batch attempt state filter
- `account` — exact Jeb account/source slug filter
- `collection` — exact Jeb collection id filter
- `target` — exact target filter
- `q` — case-insensitive search over attempt, batch, job, collection, target,
  state, timestamp, or error text

#### `GET /v1/jeb/config/check`

Validates the deployed Jeb service configuration and initializes its state store
if needed.

#### `POST /v1/jeb/once`

Requests one scheduler pass on the deployed Jeb service.

#### `POST /v1/jeb/archive-now`

Requests one immediate archive attempt for a Jeb account.

Request body:

```json
{
  "account": "example-camera",
  "process": true
}
```

Required behavior:

- `account` is the Jeb account/source slug
- `process` defaults to `true`; when false, Jeb creates the eligible batch but
  does not process it immediately
- Jeb sends complete eligible account batches to Munchy; Munchy owns routing,
  profile selection, archive/leave/cull behavior, and Riverhog archive contents

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
- returned collection summaries are compact list items; use
  `GET /v1/collections/{collection_id}` for bounded per-image coverage previews
  and copy detail
- collection summaries are returned only for collections whose Glacier archive
  package has uploaded and verified
- filtering by `protection_state=fully_protected` can be used to answer which
  collections have both verified Glacier and full physical coverage
- collection summaries include direct collection Glacier state, archive
  manifest/OTS proof state, and physical disc coverage

#### `GET /v1/collections/{collection_id}`

Returns a collection summary with byte coverage values.

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collections/photos/2024`
- API and CLI collection lookup treat slash-bearing ids as first-class
- collection summaries expose `glacier`, `collection_manifest`, `archive_format`,
  `compression`, `disc_coverage`, `protection_state`, `protected_bytes`, and
  per-image physical coverage details
- `glacier` is direct collection archive state, not a value derived from image
  coverage
- `collection_manifest` exposes manifest object path, manifest SHA-256, OTS proof
  object path, and OTS proof state
- per-image coverage details expose `covered_paths`, `physical_copies_registered`,
  `physical_copies_verified`, copy labels and locations
- `coverage_path_limit` controls the returned `covered_paths` preview per
  image and defaults to a bounded operator view; `covered_paths_total` reports
  the total path count before truncation

### Files

#### `GET /v1/files?target=<target>`

Returns logical files selected by one canonical target selector.

Supported query parameters:

- `target` — canonical projected-path selector
- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`, max `100`

Required behavior:

- the `target` query parameter carries one canonical selector over the projected hot namespace
- the response includes pagination metadata, the canonical `target`, and a `files` array
- returned files use the same projected-path syntax accepted by fetch and hot-eviction commands
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
- allocation may reorder collection pieces across candidate images to improve packing
- files are never voluntarily split; file parts only exist when a single file cannot fit on one candidate image
- collections that require multiple candidate images are split only as required unless saturation splitting is needed
- collections that could fit on one candidate image may be split once, by whole files, to make an underfilled candidate
  ready without increasing total waiting candidate bytes even before saturation
- whether a collection could fit on one candidate image is evaluated against the complete collection, not only its
  currently unburned remainder
- each candidate image may contain at most one optionally split collection
- every candidate image that contains any part of a collection budgets that collection's encrypted manifest and encrypted
  OpenTimestamps proof
- underfilled tail candidates are held out of the returned ready plan until future collections push them over the
  configured minimum fill threshold
- if waiting candidate bytes exceed `RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES`, the planner may add fair beneficial
  whole-file voluntary splits, including for collections that already required splitting, until enough candidate images
  meet the minimum fill threshold to bring waiting bytes under the saturation threshold
- required splits and voluntary splits are counted separately; saturation splitting chooses a feasible collection with
  the lowest existing voluntary split count, so required-split collections start as natural saturation targets
- the response includes pagination metadata and a `candidates` array
- the default ordering is fullest candidates first using `sort=fill&order=desc`
- explicit sort and filter controls only change how the current provisional plan is listed; they do not change planner
  allocation behavior
- plan candidate objects expose `candidate_id`
- plan candidate objects expose their own `target_bytes`, preserving the media capacity that the candidate was planned
  for even if current planner settings later change
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
- finalized-image summaries expose `filename`, `finalized_at`, `target_bytes`, `collection_ids`,
  `physical_protection_state`, `physical_copies_required`, `physical_copies_registered`, `physical_copies_verified`,
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

Image rebuild sessions are not manually started. Riverhog creates them when copy
state changes leave a finalized image with no protected copies and the required
collection Glacier archives are uploaded. The recovery processor requests and
polls Glacier restore work automatically.

#### `POST /v1/fetches/{fetch_id}/cancel`

Cancels an active hot-storage fetch.

Required behavior:

- cancels a fetch by fetch id, regardless of whether it was queued for `djdan`
  or cloud materialization
- for `djdan` fetches, cancels any in-flight resumable upload resources,
  discards staged recovery bytes, deletes existing fetch entries, clears queued
  notification state, and returns the fetch to `draft`
- for cloud fetches, cancels active collection-native recovery sessions that
  intersect the fetch and returns the fetch to `draft` when possible
- returns the same fetch status payload as `GET /v1/fetches/{fetch_id}/status`

#### `GET /v1/recovery-sessions`

Returns a paged inventory of Glacier-backed cloud-fetch and image rebuild
sessions.

Supported query parameters:

- `page`, `per_page`
- `sort`: `created_at`, `id`, `type`, `state`, `restore_ready_at`, or
  `restore_expires_at`
- `order`: `asc` or `desc`
- `type`: `collection_restore` or `image_rebuild`
- `state`: `restore_requested`, `ready`, `expired`, or `completed`
- `collection`: restricts results to sessions attached to one collection id
- `image`: restricts results to sessions attached to one finalized image id

Required behavior:

- list views are bounded database lookups and do not scan archive objects or hot
  storage
- cloud-fetch operator views filter with `type=collection_restore`
- image rebuild operator views filter with `type=image_rebuild`

#### `GET /v1/recovery-sessions/{session_id}`

Returns one Glacier-backed cloud-fetch or image rebuild session by durable
session id.

Required behavior:

- recovery sessions remain addressable across service restart
- the response includes recovery `type`, operator warnings,
  notification state, covered collections, and any finalized images involved in
  an image rebuild
- cloud-fetch recovery responses include `restore_paths`; `null` means the
  whole collection is in scope
- the response includes `progress.archive_verification`, `progress.extraction`,
  and `progress.materialization`, each one of `pending`, `in_progress`,
  `completed`, or `failed`

#### `POST /v1/recovery-sessions/{session_id}/complete`

Completes one ready recovery session.

Required behavior:

- completion is only valid from `ready` or `expired`
- completion transitions the session to `completed`
- completion records cleanup or lifecycle handoff for restored Standard-storage data instead of waiting for Riverhog's
  session expiry
- cloud-fetch recovery sessions complete automatically after requested files are
  materialized; `djdan burn` uses this endpoint after rebuilding replacement
  image copies from a ready image rebuild session

#### `GET /v1/recovery-sessions/{session_id}/images/{image_id}/iso`

Downloads one rebuilt ISO from a ready `image_rebuild` recovery session.

Required behavior:

- succeeds only when the recovery session is `ready`
- the image must belong to that recovery session
- the response streams bytes from the rebuilt image artifact produced from
  restored collection archives and persisted coverage metadata
- `djdan burn` uses this endpoint for replacement burns from active image
  rebuild sessions instead of `GET /v1/images/{image_id}/iso`

#### `GET /v1/glacier`

Returns Glacier usage totals, direct per-collection archive state, collection
manifest/OTS proof state, image-to-collection contribution metadata, and
overall usage snapshots.

Supported query parameters:

- `collection` — narrows image and collection reporting to one exact collection id

Required behavior:

- the response always exposes `scope`, `measured_at`, `totals`, `images`,
  `collections`, and `history`
- `totals.measured_storage_bytes` reports measured uploaded archive-store bytes
- `totals.collections` counts collection archive records and
  `totals.uploaded_collections` counts those uploaded and verified
- each returned collection exposes direct archive-store state, measured
  uploaded bytes, manifest state, and OTS proof state
- returned image entries, when present, explain physical coverage of collections
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
- finalized-image ISO responses are generated streams and do not promise `Content-Length`; callers should use the
  finalized-image `bytes` field as an estimate for operator progress only
- finalized-image ISO responses include `X-Accel-Buffering: no` so nginx-compatible proxies can forward headers and
  streaming bytes without waiting for the rebuilt body to accumulate
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
- successful registration also records the per-file recovery index for every
  file or file part physically present on that image, so the response means the
  copy is usable for later fetch planning

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
- location or protection changes synchronously reconcile affected per-file
  recovery-index rows
- reporting one confirmed copy `lost` or `damaged` never reuses that same `copy_id` for replacement burn work
- when another protected copy still exists, replacement burn work is represented as a new generated `copy_id`

#### `POST /v1/images/{image_id}/copies/{copy_id}/label-needed`

Sends the best-effort operator notification that `copy_id` has been burned and
verified and now needs its physical label applied.

Required behavior:

- this endpoint is used by `djdan burn` after burned-media verification and before label confirmation
- it does not register the copy, assign a storage location, or count the copy toward physical protection
- it returns the current generated copy summary
- if `RIVERHOG_OPERATOR_WEBHOOK_URL` is configured, Riverhog emits `images.copy_label_needed`
- webhook delivery failures are logged and do not block the burn workflow

### Hot Eviction

#### `POST /v1/hot/evict`

Removes selected compliant files from committed hot storage.

Required behavior:

- the `targets` field carries one or more canonical selectors over the projected hot namespace
- every selected file must already have the required verified disc protection
- under-protected selections fail with `conflict` and do not evict partial data
- a selector that matches no files fails with `not_found`
- eviction is synchronous and returns selected file/byte counts plus evicted file/byte counts
- eviction does not create, start, or cancel a fetch

### Fetches

#### `GET /v1/fetches`

Lists named fetch manifests. Supports `page`, `per_page`, `state`, `q`, `sort`, and `order`.

Required behavior:

- responses include `page`, `per_page`, `total`, and `pages`
- every fetch includes id, name, targets, state, file count, byte count, missing hot-storage bytes, upload progress, and
  copy hints when useful for the current state
- list/show output is served from fetch-keyed summary projection data and must not scan unbounded collection-file rows
  for routine operator views

#### `POST /v1/fetches`

Creates one draft named fetch.

Required behavior:

- `name` is required and carries the operator's human-readable purpose
- optional `targets` use canonical projected-path selector syntax
- a draft fetch can have no targets so the operator can build it incrementally
- adding targets immediately refreshes the fetch summary projection
- duplicate target selectors are ignored

#### `POST /v1/fetches/{fetch_id}/targets`

Adds target selectors to a draft fetch.

#### `DELETE /v1/fetches/{fetch_id}/targets`

Removes target selectors from a draft fetch.

Required behavior for target editing:

- only `draft` fetches are editable
- editing fails once a fetch is queued to djdan, uploading, verifying, queued to cloud, cloud-fetching, done, or failed
- editing is a frequent operator path and should use set-based catalog work with bounded response time

#### `POST /v1/fetches/{fetch_id}/start`

Freezes a draft fetch and chooses its fulfillment path.

Required behavior:

- `cloud=false` queues the fetch for the prompt-based `djdan fetch` workflow and moves it to `queued_djdan`
- `cloud=true` starts cloud-fetch materialization and moves it through `queued_cloud`/`cloud_fetching`
- a fetch with no targets cannot start
- starting an already-started fetch fails with `invalid_state`
- if a fetch is queued to djdan and `RIVERHOG_OPERATOR_WEBHOOK_URL` is configured, Riverhog emits
  `fetches.queued_djdan` and then `fetches.queued_djdan.reminder` on the configured reminder interval while it remains
  queued

#### `GET /v1/fetches/{fetch_id}`

Returns one named fetch summary.

#### `GET /v1/fetches/{fetch_id}/status`

Returns a bounded operator preflight/status view for one named fetch.

- includes the same summary fields as `GET /v1/fetches/{fetch_id}`
- includes derived hot/archive/disc coverage counts from the fetch file projection
- includes per-target summaries, a bounded selected-file preview, and the recommended next operator action
- includes a bounded `cloud_fetch` recovery-session list for cloud materialization progress and cancellation audit
- includes a bounded list of pending, partial, or byte-complete entry statuses
- does not include recovery copy hints, part metadata, or recovery-byte digests
- does not backfill finalized-image recovery metadata
- intended for human `riverhog hot fetch show` output
- routine status reads must be fetch-keyed projection lookups, not selector scans over unbounded collection files

#### `GET /v1/fetches/{fetch_id}/files`

Returns the selected logical files for one named fetch. Supports `page`,
`per_page`, `q`, `sort`, `order`, `hot`, `archived`, and `disc_coverage`.

Required behavior:

- responses include `page`, `per_page`, `total`, `pages`, `sort`, `order`, and `files`
- each file includes target, collection id, path, bytes, hot availability, archive coverage, and registered disc coverage
- list reads are served from the fetch-keyed file projection so operator paging, sorting, and filtering stay responsive
- intended for human and JSON `riverhog hot fetch files FETCH_ID` output

#### `GET /v1/fetches/{fetch_id}/manifest`

Returns a stable manifest for the named fetch.

- the fetch manifest is the source of truth for automated multipart recovery
- multipart logical files include part-level recovery hints
- `entries[].parts[]` are ordered by zero-based `index`
- every manifest entry includes `collection_id` and `path` for the target logical file
- every manifest entry includes logical plaintext `bytes` / `sha256` plus `recovery_bytes` for the ordered upload
  stream
- every part hint includes logical plaintext `bytes`, logical plaintext `sha256`, `recovery_bytes`, and at least one
  candidate recovery copy
- every candidate recovery copy includes `disc_path`, `recovery_bytes`, and `recovery_sha256`
- Riverhog captures candidate recovery-byte lengths with the registered physical copy, and lazily backfills missing
  recovery-byte digests from the finalized image root before returning a fetch manifest; fetch publication and
  completion must not require the logical file to already be present in hot storage
- `djdan` uploads the raw encrypted bytes stored at `disc_path`, not reconstructed logical plaintext
- logical plaintext hash and size fields remain server-side verification anchors after decryption and reconstruction
- each manifest entry exposes current upload state, uploaded bytes, and upload expiry if partial state exists
- fetch entry upload states distinguish `pending`, `partial`, `byte_complete`, and `uploaded`
- `byte_complete` means the full ordered recovery-byte stream has been accepted but `POST /complete` has not yet finished server-side verification and materialization
- those hints drive disc sequencing and resumable recovery in `djdan`
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk and the manifest returns to
  `queued_djdan`
- fetch summaries expose an audit field such as `upload_state_expires_at`
- fetch summaries expose separate `entries_byte_complete` and `entries_uploaded` counts
- the fetch manifest and any unexpired upload progress survive service restart while the fetch remains active

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
manifest remains readable after completion.

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

### `riverhog`

The `riverhog` CLI is collection-first and should provide:

- `riverhog collection list [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--protection STATE]`
- `riverhog collection show COLLECTION`
- `riverhog find [QUERY] [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--collection ID] [--hot|--not-hot] [--archived|--not-archived]`
- `riverhog collection upload SLUG ROOT [--timestamp YYYYMMDDTHHMMSSZ] [--wait finalized|staged]`
- `riverhog collection watch COLLECTION_UPLOAD_ID`
- `riverhog collection cancel COLLECTION_UPLOAD_ID`
- `riverhog hot evict TARGET...`
- `riverhog hot fetch list [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--state STATE] [--query TEXT]`
- `riverhog hot fetch create --name NAME [TARGET...]`
- `riverhog hot fetch add FETCH_ID TARGET...`
- `riverhog hot fetch remove FETCH_ID TARGET...`
- `riverhog hot fetch show FETCH_ID`
- `riverhog hot fetch files FETCH_ID [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--hot|--not-hot] [--archived|--not-archived] [--disc|--no-disc]`
- `riverhog hot fetch start FETCH_ID [--cloud]`
- `riverhog hot fetch cancel FETCH_ID`

`riverhog collection upload` streams files in bounded tus-compatible chunks. The default
chunk size is 8 MiB; operators may set `RIVERHOG_UPLOAD_CHUNK_BYTES` to a
positive byte count when a specific deployment path needs smaller or larger
request bodies. HTTPS API clients prefer HTTP/2 by default; set
`RIVERHOG_HTTP2=false` to force HTTP/1.1 for deployments that cannot negotiate
HTTP/2. The current CLI sends each resumable request chunk as one bounded PATCH
request body through the HTTP client. Upload throughput and retry cost are
tuned with the request chunk size, file concurrency, HTTP version, proxy body
limits, and per-chunk timeout.
The CLI uploads one file at a time by default. Operators may set
`RIVERHOG_UPLOAD_FILE_CONCURRENCY` to upload multiple logical files in parallel
from one process, which is primarily useful for collections with thousands of
small files. Each worker keeps the normal per-file resumable upload contract.
Per-file start/completion logs are emitted only for files at or above
`RIVERHOG_UPLOAD_FILE_LOG_BYTES`; smaller files still appear in total progress
and in retry/error messages.
See [Upload Transport Reference](upload-transport.md) for the operational
findings, proxy guidance, and tuning procedure behind these defaults.
`RIVERHOG_UPLOAD_BASE_URL` may
override the scheme and host of absolute upload URLs while preserving the
API-provided path, which is useful when bulk upload traffic is sent through a
local tunnel. `RIVERHOG_HOST_HEADER` and `RIVERHOG_TLS_VERIFY=false` let
operators bind the connect address to a LAN IP while still routing through a
name-based reverse proxy during DNS outages or hairpin edge cases. During
uploads the CLI
prints manifest, resume, per-file, and throttled total progress messages to
stderr; `--json` output remains reserved for the final machine-readable payload
on stdout. `RIVERHOG_UPLOAD_TIMEOUT_SECONDS` controls the per-chunk PATCH
timeout, defaulting to 300 seconds. If a chunk response is lost after the server
accepted the data, the CLI re-queries the resumable file offset and continues
from the server-confirmed position. A single CLI invocation reuses one HTTP
connection pool for API and upload requests, avoiding per-chunk TCP/TLS setup.
`RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS` bounds how long the API will wait on the
internal tusd forward for one chunk before returning a transient 503; the CLI
treats that response the same way as a dropped connection and resumes from the
authoritative offset.
Server-side upload expiry sweeps do not poll tusd offsets for live,
non-expired uploads because tusd interrupts an active PATCH when another
request, including HEAD, targets the same upload resource.

`riverhog collection upload` defaults to `--wait finalized`: it exits successfully after
every source file is verified and staged on the server and the background
archive finalization has completed. Use `riverhog collection upload --wait staged`
when a terminal session should detach after server custody begins while archive
upload, hot-file promotion, and planner refresh continue in the background.
Operators should track those longer phases with the configured operator webhook
or `riverhog collection show`. `RIVERHOG_UPLOAD_WAIT` may be set to `staged`
or `finalized` to change the default.

Glacier recovery notifications are deliberately explicit because bulk restores
are rare and slow. `glacier_recovery.started` confirms Riverhog has requested a
Glacier restore, `glacier_recovery.ready` confirms the temporary restored
archive data is available, optional `glacier_recovery.ready.reminder` events
repeat while action is still outstanding, and `glacier_recovery.completed`
confirms Riverhog has finished verification/materialization and cleanup. The
complete machine-readable operator webhook contract lives at
[`contracts/webhooks/operator-notifications.v1.json`](../../contracts/webhooks/operator-notifications.v1.json).
Every operator webhook includes `notification.title` and `notification.body`
rendered from that contract so receivers can use Riverhog's canonical quiet
phone text directly.

`riverhog find` should provide a concise, paged human-readable listing of
logical files across the projected namespace. It supports substring search plus
collection, hot-storage, and deep-archive filters; JSON output mirrors the
`GET /v1/files` response payload.

`riverhog collection show COLLECTION` should provide a concise human-readable recovery and coverage view for one collection, including:

- an explicit summary of whether the collection is currently recoverable from verified physical copies, Glacier, both,
  or neither
- finalized images currently covering the collection
- a bounded preview of projected paths carried by each image, plus total path
  counts
- generated disc ids, exact label text, locations, and verification state
- direct collection archive object paths and manifest/OTS proof state

`riverhog hot fetch show FETCH_ID` should provide a concise human-readable listing of:

- the fetch purpose, selectors, state, coverage counts, and next recommended action
- per-target selected-file summaries
- active or completed cloud-fetch recovery sessions when present
- a bounded preview of selected files
- files still pending upload
- files currently partial and still resumable
- the expiry time for each partial upload
- completed fetches should show summary state without fetching the full recovery manifest

`riverhog hot fetch files FETCH_ID` should provide a paged, searchable,
sortable multiline list of selected file targets without truncating the target
path.

### `djdan`

The `djdan` CLI is an optical-media client for a machine with an optical drive and should provide:

- `djdan burn [--device DEVICE] [--staging-dir DIR] [--simulate]`
- `djdan fetch [FETCH_ID] [--device DEVICE]`
- `djdan image plan [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--iso-ready|--not-ready]`
- `djdan image list [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--has-discs|--no-discs]`
- `djdan image show IMAGE_ID`
- `djdan image download IMAGE_ID [-o FILE]`
- `djdan disc list [IMAGE_ID] [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT]`
- `djdan disc show COPY_ID`
- `djdan disc location COPY_ID --to LOCATION`
- `djdan disc rebuild start COPY_ID --reason lost|damaged`
- `djdan disc rebuild list|show|pause|resume`

For finalized-image and disc commands:

- `CANDIDATE_ID` means a ready provisional candidate id returned by `djdan image plan --iso-ready`
- `IMAGE_ID` means the finalized image id
- finalized image ids use compact UTC basic form `YYYYMMDDTHHMMSSZ`
- `djdan image list --json` mirrors the `GET /v1/images` response payload
- `djdan image plan --json` mirrors the `GET /v1/plan` response payload
- `djdan disc list IMAGE_ID --json` mirrors the `GET /v1/images/{image_id}/copies` response payload
- standalone manual candidate finalization is intentionally not exposed; `djdan burn` selects and
  finalizes ready candidates as part of the guided burn workflow
- standalone manual disc registration is intentionally not exposed; `djdan burn` registers verified physical copies as
  part of the guided burn workflow
- non-JSON `djdan image plan` output stays concise and line-oriented while surfacing candidate id, fill, readiness, and
  contained collections
- non-JSON `djdan image list` is a literal paginated finalized-image listing

For multipart recovery, one invocation should continue across successive discs until every required
part has been recovered, streamed, and uploaded.

Required behavior:

- complete files stream straight from optical recovery into the upload resource rather than being materialized to disk
  first
- split files stream into the same logical-file upload resource in ascending part order
- manifest entries identify the logical file with both `collection_id` and `path`, so multidisc fetches that span
  collections remain unambiguous even when relative paths repeat
- the upload resource receives raw encrypted recovery bytes exactly as stored in the hinted payload object(s)
- `djdan` treats the upload resource as opaque and does not own decryption or final logical-file hash validation
- `djdan fetch` is an intentionally prompt-based multidisc flow: it names the exact required copy before reading from a
  new disc, avoids repeating the prompt while consecutive manifest work stays on the same disc, and prompts again when a
  later part or manifest entry needs a different copy
- resumable offsets remain valid only for the exact recovery-byte stream accepted so far for the current span
- any temporary buffering used during recovery is an internal implementation detail
- progress output is precise and continuous, including current transfer rate, percent complete for the current file, and
  percent complete for the whole manifest

## Behavioral invariants

- creating a fetch requires a human-readable name
- draft fetches can be edited by adding or removing selectors
- started fetches are frozen until they complete, fail, or fetch cancellation returns them to draft
- starting a fetch without `--cloud` queues it for `djdan fetch`
- `djdan fetch` with no id clears all queued djdan fetches in one guided session
- starting a fetch with `--cloud` creates or resumes cloud materialization for the selected files
- evicting hot files is allowed only after every selected file is fully compliant
- eviction removes only the selected hot files and never creates recovery intent
- a file restored by a completed fetch is hot
- hot file content is directly downloadable when the target selects exactly one file
- archived-only file content is recoverable through fetch/upload, not through hot-content download
- upload-state expiry for a manifest discards incomplete partial uploads and returns that manifest to `queued_djdan`
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
- collection ingest starts from a slug-only upload creation request, then uses
  the returned server-minted collection id for resumable file uploads
- collection ingest finalizes only after every required file verifies and the
  collection Glacier archive package uploads and verifies
- the same canonical selector string means the same projected file set everywhere in API and CLI
- file availability shown by search, file introspection, fetches, and CLI status uses the same hot/archived meaning
