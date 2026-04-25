# API reference

This document summarizes the MVP HTTP and CLI contract. The canonical machine-readable shape is
`contracts/openapi/arc.v1.yaml`.

## HTTP API

All JSON endpoints are under `/v1`. Requests and responses use JSON unless otherwise specified.
Resumable fetch-entry upload URLs are returned by the JSON API and use tus-compatible transport semantics.

Unless this contract explicitly says otherwise, authoritative resources created through the API remain addressable across
service restarts, including collections, finalized images, registered copies, active pins, active fetch manifests, and
unexpired upload progress.

### Collections

#### `POST /v1/collections/close`

Closes a staged directory into a new collection.

Request body:

```json
{
  "path": "/srv/archive/staging/photos/2024"
}
```

Required behavior:

- scans and freezes the staged directory
- derives the collection id from the canonical relative path beneath the staging root
- allows slash-bearing collection ids such as `/staging/photos/2024 -> photos/2024`
- rejects a collection id if it would be an ancestor or descendant of an existing collection id
- creates one new collection
- materializes all files into hot storage immediately
- persists the collection so it remains addressable after service restart
- makes the collection eligible for planning

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

#### `GET /v1/collections/{collection_id}`

Returns a collection summary with byte coverage values.

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collections/photos/2024`
- API and CLI collection lookup treat slash-bearing ids as first-class

### File introspection

#### `GET /v1/collections/{collection_id}/files`

Lists the logical files in one collection.

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collections/photos/2024/files`
- each returned file includes its projected path and current hot or archived state
- each returned file includes available copies, if any
- collection ids whose final path segment is literally `files` are reserved by this endpoint shape

#### `GET /v1/files?target=<target>`

Returns logical files selected by one canonical target selector.

Required behavior:

- the `target` query parameter carries one canonical selector over the projected hot namespace
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
- a provisional candidate may be re-allocated by the planner
- finalized images are not returned by `GET /v1/plan`
- the response includes pagination metadata and a `candidates` array
- the default ordering is fullest candidates first using `sort=fill&order=desc`
- explicit sort and filter controls only change how the current provisional plan is listed; they do not change planner
  allocation behavior
- plan candidate objects expose `candidate_id`
- plan candidate objects expose `collection_ids`
- plan candidate objects do not expose finalized-image fields such as finalized `id`, `filename`, `finalized_at`, or
  `copy_count`
- plan-specific fields such as `ready`, `target_bytes`, `min_fill_bytes`, and `unplanned_bytes` remain part of the
  response alongside the paged candidate listing

### Images

#### `GET /v1/images`

Lists finalized images.

Supported query parameters:

- `page` — 1-based page number, default `1`
- `per_page` — page size, default `25`
- `sort` — one of `finalized_at`, `bytes`, or `copy_count`
- `order` — `asc` or `desc`
- `q` — case-insensitive substring filter over finalized image id, ISO filename, and contained collection ids
- `collection` — exact collection-id filter over contained collection ids
- `has_copies` — filters finalized images by whether at least one burned copy has been registered

Required behavior:

- this endpoint returns finalized images only
- provisional plan candidates are never returned by `GET /v1/images`
- default ordering is latest finalized image first using `sort=finalized_at&order=desc`
- the response includes pagination metadata and finalized-image summaries
- finalized-image summaries expose `filename`, `finalized_at`, `collection_ids`, and `copy_count`
- finalized-image summaries always report `iso_ready = true`

#### `GET /v1/images/{image_id}`

Returns one image summary.

Required behavior:

- this endpoint returns finalized images only
- `image.id` is the canonical finalized image id
- finalized image ids use compact UTC basic form `YYYYMMDDTHHMMSSZ`
- the response uses the same finalized-image summary shape returned by `GET /v1/images`
- finalized image summaries always report `iso_ready = true`
- provisional plan candidates are not addressable through `GET /v1/images/{image_id}`

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

#### `GET /v1/images/{image_id}/iso`

Returns ISO bytes if the image is ready.

Required behavior:

- ISO download does not finalize the image
- ISO download requires the finalized image to already exist
- subsequent downloads for the same finalized `image.id` reuse the same represented bytes

#### `POST /v1/images/{image_id}/copies`

Registers a physical burned disc for an image.

Required behavior:

- copy registration is only valid for an already finalized image
- the path `image_id` is the finalized image id
- the physical copy identity is `(volume_id, copy_id)`
- the user-supplied `copy_id` must be unique within that finalized image/`volume_id`; duplicates are rejected with
  `conflict`
- `location` is mutable operational metadata and is never part of copy identity
- successful registration persists across service restart

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
- those hints drive disc sequencing and resumable recovery in `arc-disc`
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk and the manifest returns to
  `waiting_media`
- fetch summaries expose an audit field such as `upload_state_expires_at`
- the fetch manifest and any unexpired upload progress survive service restart while the exact pin remains active

#### `POST /v1/fetches/{fetch_id}/entries/{entry_id}/upload`

Creates or resumes the resumable upload resource for one manifest entry.

Required behavior:

- the response returns one upload URL bound to exactly one logical file entry
- the returned upload URL uses tus-compatible resumable upload semantics
- the response includes current offset, total length, transport checksum algorithm, and expiry time
- offset and length are measured in the entry's ordered recovery-byte stream
- repeated calls while the upload remains resumable return the current upload resource rather than creating duplicates
- the server owns any required decryption and final logical-file validation behind that upload resource

#### `POST /v1/fetches/{fetch_id}/complete`

Marks the fetch manifest satisfied once all required entries have been uploaded, verified, and materialized. The
manifest remains readable while the exact pin remains active.

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

- `arc close PATH`
- `arc find QUERY`
- `arc show COLLECTION`
- `arc show COLLECTION --files`
- `arc status [TARGET]`
- `arc get TARGET [-o FILE]`
- `arc plan [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--iso-ready|--not-ready]`
- `arc images [--page N] [--per-page N] [--sort FIELD] [--order asc|desc] [--query TEXT] [--collection ID] [--has-copies|--no-copies]`
- `arc iso get IMAGE_ID [-o FILE]`
- `arc copy add IMAGE_ID COPY_ID --at LOCATION`
- `arc pin TARGET`
- `arc release TARGET`
- `arc pins`
- `arc fetch FETCH_ID`

`arc show COLLECTION --files` should provide a concise human-readable listing of the collection's logical files, including current hot or archived state and available copies when applicable.

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
- `arc plan --json` mirrors the `GET /v1/plan` response payload
- non-JSON `arc plan` output stays concise and line-oriented while surfacing candidate id, fill, readiness, and
  contained collections
- non-JSON `arc images` output stays concise and line-oriented while surfacing finalized id, filename, copy count,
  and contained collections

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
- immediately after collection close, every file in the collection is hot
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
- duplicate `copy_id` values are rejected within one finalized image/`volume_id`
- no collection id is an ancestor or descendant of another collection id
- the same canonical selector string means the same projected file set everywhere in API and CLI
- file availability shown by search, file introspection, pins, and CLI status uses the same hot/archived meaning
