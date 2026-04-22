# API reference

This document summarizes the MVP HTTP and CLI contract. The canonical machine-readable shape is
`openapi/arc.v1.yaml`.

## HTTP API

All endpoints are under `/v1`. Requests and responses use JSON unless otherwise specified.

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
- makes the collection eligible for planning

### Search

#### `GET /v1/search?q=<query>&limit=<n>`

Returns collection and file targets that can be used directly with `pin` and `release`.

Required behavior:

- search is case-insensitive substring match over collection ids and full logical file paths
- file results include current hot availability
- file results include available copies, if any
- `limit` is honored

### Collections summary

#### `GET /v1/collections/{collection_id}`

Returns a collection summary with byte coverage values.

Required behavior:

- collection ids may span multiple path segments, for example `GET /v1/collections/photos/2024`
- API and CLI collection lookup treat slash-bearing ids as first-class

### Planning

#### `GET /v1/plan`

Returns the best current planner output and readiness status.

Required behavior:

- each image in the plan is provisional until its first ISO download request
- before first download, a planned image may be re-allocated by the planner
- plan responses expose `volume_id`, which is `null` until the image is finalized by first download

### Images

#### `GET /v1/images/{image_id}`

Returns one image summary.

Required behavior:

- `image.id` is the stable API handle for the planned image
- `volume_id` is `null` before the first ISO download request for that image
- after the first ISO download request, the stored `volume_id` is returned for that same `image.id`

#### `GET /v1/images/{image_id}/iso`

Returns ISO bytes if the image is ready.

Required behavior:

- the first successful request finalizes that image's represented bytes for burning
- that first successful request assigns a unique immutable `volume_id` in UTC basic form `YYYYMMDDTHHMMSSZ`
- if more than one image would otherwise finalize in the same second, later assignments advance in whole seconds until
  an unused `volume_id` is found
- after finalization, subsequent downloads for the same `image.id` reuse the same `volume_id` and the same represented
  bytes
- after finalization, the planner must not re-allocate those represented collections away from that image

#### `POST /v1/images/{image_id}/copies`

Registers a physical burned disc for an image.

Required behavior:

- copy registration is only valid for a finalized image that already has a stored `volume_id`
- the physical copy identity is `(volume_id, copy_id)`
- the user-supplied `copy_id` must be unique within that finalized image/`volume_id`; duplicates are rejected with
  `conflict`
- `location` is mutable operational metadata and is never part of copy identity

### Pins

#### `POST /v1/pin`

Pins a target into hot storage.

Required behavior:

- successful pin keeps the target desired in hot until explicitly released
- if all targeted bytes are already hot, no fetch is created
- if some targeted bytes are archived but not hot, a fetch is created or reused
- repeated pin of the same canonical target is idempotent

#### `POST /v1/release`

Releases exactly one canonical target pin.

Required behavior:

- releasing a non-existent exact pin is a successful no-op
- releasing a broader pin must not remove narrower remaining pins
- releasing a narrower pin must not remove broader remaining pins

#### `GET /v1/pins`

Lists active pins.

### Fetches

#### `GET /v1/fetches/{fetch_id}`

Returns one fetch summary.

#### `GET /v1/fetches/{fetch_id}/manifest`

Returns a stable manifest for the fetch lifetime.

- the fetch manifest is the source of truth for automated multipart recovery
- multipart logical files include part-level recovery hints
- `entries[].parts[]` are ordered by zero-based `index`
- every part hint includes exact plaintext `bytes`, plaintext `sha256`, and at least one candidate recovery copy
- those hints drive disc sequencing and local resumable recovery state in `arc-disc`
- the API still accepts one final plaintext upload per logical file

#### `PUT /v1/fetches/{fetch_id}/files/{entry_id}`

Uploads one recovered plaintext file and verifies it against the expected hash.

#### `POST /v1/fetches/{fetch_id}/complete`

Finalizes a fetch once all required entries have been uploaded and verified.

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
- `arc plan`
- `arc iso get IMAGE_ID [-o FILE]`
- `arc copy add IMAGE_ID COPY_ID --at LOCATION`
- `arc pin TARGET`
- `arc release TARGET`
- `arc pins`
- `arc fetch FETCH_ID`

### `arc-disc`

The `arc-disc` CLI is a fetch-fulfillment client for a machine with an optical drive and should provide:

- `arc-disc fetch FETCH_ID --state-dir PATH [--device DEVICE]`

For multipart recovery, one invocation should continue across successive discs until every required
part has been staged, reconstructed, verified, and uploaded.

## Behavioral invariants

- pinning the same target twice results in exactly one active pin
- releasing a target not currently pinned is a successful no-op
- a file is logically required in hot if and only if at least one active pin selects it
- immediately after collection close, every file in the collection is hot
- a file restored by a completed fetch is hot
- before the first ISO download request, a planned image may still be re-allocated and has `volume_id = null`
- the first successful ISO download finalizes the image allocation and stores immutable `volume_id` for that `image.id`
- subsequent ISO downloads for the same `image.id` use the same `volume_id` and represented bytes
- registering a copy cannot reduce archived coverage
- a physical copy is identified by `(volume_id, copy_id)`, never by `location`
- duplicate `copy_id` values are rejected within one finalized image/`volume_id`
- no collection id is an ancestor or descendant of another collection id
- the same canonical target string means the same file set everywhere in API and CLI
