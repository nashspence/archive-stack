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
  "path": "/srv/archive/staging/photos-2024"
}
```

Required behavior:

- scans and freezes the staged directory
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

### Planning

#### `GET /v1/plan`

Returns the best current planner output and readiness status.

### Images

#### `GET /v1/images/{image_id}`

Returns one image summary.

#### `GET /v1/images/{image_id}/iso`

Returns ISO bytes if the image is ready.

#### `POST /v1/images/{image_id}/copies`

Registers a physical burned disc for an image.

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

- `arc-disc fetch FETCH_ID [--device DEVICE]`

## Behavioral invariants

- pinning the same target twice results in exactly one active pin
- releasing a target not currently pinned is a successful no-op
- a file is logically required in hot if and only if at least one active pin selects it
- immediately after collection close, every file in the collection is hot
- a file restored by a completed fetch is hot
- registering a copy cannot reduce archived coverage
- the same canonical target string means the same file set everywhere in API and CLI
