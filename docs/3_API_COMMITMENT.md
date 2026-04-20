Below is a precise MVP contract, written so it can be converted directly into acceptance tests.

# Scope

This specification defines the MVP behavior of:

* the HTTP API
* the `arc` CLI
* the `arc-disc` CLI

This specification does not define:

* any web UI
* background scheduler behavior beyond required externally visible effects
* internal database schema
* optical disc on-media format except where required by fetch manifest behavior

# Core model

## Terms

`collection`
: A logical namespace closed from a staged directory. A collection has a stable id and contains many files at stable relative paths.

`file`
: A logical file within a collection, identified by `(collection_id, path)`.

`image`
: One planned ISO artifact produced by the planner.

`copy`
: One physical burned disc corresponding to one image.

`pin`
: A user-declared requirement that a target must be materialized in hot storage.

`fetch`
: A recovery job created when a pin targets data not currently present in hot storage.

`hot storage`
: The server-side materialized cache of file bytes currently available without optical recovery.

`target`
: A selector naming either a whole collection, a directory prefix within a collection, or a single file within a collection.

## Normative rules

1. The system of record for archive membership and hot residency is the API state, not filesystem mutation by the user.
2. Hot storage is a materialization layer. Releasing a target removes the requirement to keep it hot; it does not delete archived data.
3. Pins are explicit and idempotent.
4. Releasing a target removes exactly that pin and nothing else.
5. Effective hot residency is the union of all active pins whose targeted bytes are currently materialized.
6. A fetch exists only to satisfy a pin for bytes not currently hot.
7. The API must support pin and release at whole-collection, directory-prefix, and single-file granularity.
8. The API must not require restoring an entire collection to restore a single file.

# Selector grammar

## Canonical target syntax

```text
<collection>
<collection>:/dir/
<collection>:/dir/file.ext
```

## Semantics

* `<collection>` targets the entire collection.
* `<collection>:/dir/` targets every file whose logical path begins with `/dir/`.
* `<collection>:/dir/file.ext` targets exactly one file.

## Normalization rules

1. The collection id is case-sensitive.
2. Paths are absolute within the collection and must begin with `/` when present.
3. Directory targets must end with `/`.
4. File targets must not end with `/`.
5. Empty path after `:` is invalid.
6. `.` and `..` path segments are invalid.
7. Repeated `/` separators must be normalized or rejected consistently; the implementation must choose one behavior and apply it everywhere. For MVP, rejection is preferred.
8. The API and both CLIs must preserve and echo targets in canonical form.

## Examples

Valid:

```text
photos-2024
photos-2024:/raw/
photos-2024:/albums/japan/img_0042.cr3
```

Invalid:

```text
photos-2024:
photos-2024:raw/
photos-2024:/raw
photos-2024:/a/../b
```

# Externally visible state

## Collection summary

A collection summary must expose at least:

* `id`
* `files`
* `bytes`
* `hot_bytes`
* `archived_bytes`
* `pending_bytes`

Definitions:

`bytes`
: Total bytes of all logical files in the collection.

`hot_bytes`
: Total bytes currently materialized in hot storage for files in the collection.

`archived_bytes`
: Total bytes stored on at least one registered copy.

`pending_bytes`
: `bytes - archived_bytes`

## Image summary

An image summary must expose at least:

* `id`
* `bytes`
* `fill`
* `files`
* `collections`
* `iso_ready`

## Copy summary

A copy summary must expose at least:

* `id`
* `image`
* `location`
* `created_at`

## Fetch summary

A fetch summary must expose at least:

* `id`
* `target`
* `state`
* `files`
* `bytes`
* `copies`

## Pin summary

A pin summary must expose at least:

* `target`

## Fetch states

The MVP fetch state machine is:

```text
waiting_media -> uploading -> verifying -> done
waiting_media -> uploading -> verifying -> failed
waiting_media -> failed
uploading -> failed
verifying -> failed
```

State meanings:

`waiting_media`
: The fetch exists and requires optical recovery input.

`uploading`
: One or more recovered files are being uploaded.

`verifying`
: All required files have been uploaded and are being verified and materialized.

`done`
: All required files are verified and materialized in hot.

`failed`
: The fetch cannot currently complete.

# API contract

## Versioning

All endpoints below are under `/v1`.

## Media types

* Requests and responses use JSON unless otherwise specified.
* ISO download returns binary content.
* Fetch file upload uses `application/octet-stream`.

## Idempotency

The following operations are required to be idempotent:

* `POST /collections/close` only if the implementation chooses to treat repeated close of the same staged path as safe; otherwise it must fail consistently. For MVP, failure on repeated close is preferred.
* `POST /pin`
* `POST /release`
* `POST /fetches/{fetch_id}/complete` after completion may either return the completed state or fail consistently; returning completed state is preferred.

## Error model

All non-2xx responses must be JSON and include at least:

* `error.code`
* `error.message`

Suggested codes:

* `invalid_target`
* `not_found`
* `conflict`
* `invalid_state`
* `hash_mismatch`
* `bad_request`

# Endpoints

1. Close collection

---

`POST /v1/collections/close`

Request:

```json
{
  "path": "/srv/archive/staging/photos-2024"
}
```

Success response:

```json
{
  "collection": {
    "id": "photos-2024",
    "files": 18234,
    "bytes": 78123456789,
    "hot_bytes": 78123456789,
    "archived_bytes": 0,
    "pending_bytes": 78123456789
  }
}
```

Required behavior:

1. The staged directory at `path` is scanned and frozen into one new collection.
2. The collection id is derived deterministically by implementation rule or explicit internal policy; for acceptance testing, fixture setup may assert the resulting id.
3. All files in the new collection are immediately materialized in hot storage.
4. The collection becomes eligible for planning.
5. `archived_bytes` is `0` until one or more copies covering bytes of the collection are registered.

Failure conditions:

* path does not exist
* path is not a directory
* path is empty if empty collections are disallowed
* collection id collision
* path already closed

2. Search

---

`GET /v1/search?q=<query>&limit=<n>`

Success response:

```json
{
  "query": "invoice",
  "results": [
    {
      "kind": "file",
      "target": "docs:/tax/2022/invoice-123.pdf",
      "collection": "docs",
      "path": "/tax/2022/invoice-123.pdf",
      "bytes": 91233,
      "hot": false,
      "copies": [
        { "id": "BR-014-A", "location": "Shelf A3" }
      ]
    }
  ]
}
```

Required behavior:

1. Search must return targets that can be passed unchanged to `pin` or `release`.

2. File results must indicate current hot availability.

3. File results must indicate available copies, if any.

4. Limit must be honored.

5. Get collection summary

---

`GET /v1/collections/{collection_id}`

Success response:

```json
{
  "id": "photos-2024",
  "files": 18234,
  "bytes": 78123456789,
  "hot_bytes": 1123456789,
  "archived_bytes": 78123456789,
  "pending_bytes": 0,
  "copies": [
    { "id": "BR-021-A", "location": "Shelf B1" }
  ]
}
```

Required behavior:

1. `pending_bytes = bytes - archived_bytes`

2. `0 <= hot_bytes <= bytes`

3. `0 <= archived_bytes <= bytes`

4. Get plan

---

`GET /v1/plan`

Success response:

```json
{
  "ready": false,
  "target_bytes": 50000000000,
  "min_fill_bytes": 45000000000,
  "images": [
    {
      "id": "img_2026-04-20_01",
      "bytes": 43124567890,
      "fill": 0.8625,
      "collections": 7,
      "files": 14022,
      "iso_ready": true
    }
  ],
  "unplanned_bytes": 8123456789,
  "note": "Best current image is below preferred fill threshold."
}
```

Required behavior:

1. `fill = image.bytes / target_bytes`

2. `ready` indicates whether there exists at least one candidate image meeting implementation-defined readiness criteria

3. Returned images are ordered best-first by planner policy

4. Get image summary

---

`GET /v1/images/{image_id}`

Success response:

```json
{
  "id": "img_2026-04-20_01",
  "bytes": 43124567890,
  "fill": 0.8625,
  "iso_ready": true,
  "files": 14022,
  "collections": [
    "photos-2024",
    "docs"
  ]
}
```

6. Download image ISO

---

`GET /v1/images/{image_id}/iso`

Required behavior:

1. Returns the ISO bytes for that image if `iso_ready = true`.

2. Fails if the ISO is not available.

3. Register copy

---

`POST /v1/images/{image_id}/copies`

Request:

```json
{
  "id": "BR-021-A",
  "location": "Shelf B1"
}
```

Success response:

```json
{
  "copy": {
    "id": "BR-021-A",
    "image": "img_2026-04-20_01",
    "location": "Shelf B1",
    "created_at": "2026-04-20T18:33:12Z"
  }
}
```

Required behavior:

1. Copy id must be unique.

2. Registering a copy makes the image’s archived bytes count toward covered files.

3. If a file exists on at least one registered copy, that file counts as archived.

4. Pin target

---

`POST /v1/pin`

Request:

```json
{
  "target": "docs:/tax/2022/invoice-123.pdf"
}
```

Success response when already hot:

```json
{
  "target": "docs:/tax/2022/invoice-123.pdf",
  "pin": true,
  "hot": {
    "state": "ready",
    "present_bytes": 91233,
    "missing_bytes": 0
  },
  "fetch": null
}
```

Success response when fetch is required:

```json
{
  "target": "docs:/tax/2022/invoice-123.pdf",
  "pin": true,
  "hot": {
    "state": "waiting",
    "present_bytes": 0,
    "missing_bytes": 91233
  },
  "fetch": {
    "id": "fx_01JV8W5J8M8F3J5V4A8Q",
    "state": "waiting_media",
    "copies": [
      { "id": "BR-014-A", "location": "Shelf A3" }
    ]
  }
}
```

Required behavior:

1. A successful pin guarantees the target remains desired in hot until explicitly released.

2. If all bytes for the target are already hot, no fetch is created.

3. If any bytes for the target are not hot but are archived, a fetch is created or reused.

4. If target bytes are neither hot nor archived, the request fails.

5. Repeating the same pin request must not create duplicate pins.

6. `present_bytes + missing_bytes` must equal the logical size of the targeted file set.

7. Release target

---

`POST /v1/release`

Request:

```json
{
  "target": "docs:/tax/2022/"
}
```

Success response:

```json
{
  "target": "docs:/tax/2022/",
  "pin": false
}
```

Required behavior:

1. Removes exactly the pin matching `target`, if present.

2. If no such pin exists, success is still returned.

3. Releasing a broader target must not release narrower remaining pins.

4. Releasing a narrower target must not release broader remaining pins.

5. After release, files remain hot if still covered by other active pins.

6. After release, files may cease to appear in hot view immediately or after short internal reconciliation, but externally they must eventually reflect the union-of-pins rule.

7. List pins

---

`GET /v1/pins`

Success response:

```json
{
  "pins": [
    { "target": "docs:/tax/2022/" },
    { "target": "photos-2024:/albums/japan/" }
  ]
}
```

Required behavior:

* Returns all active pins exactly once each.

11. Get fetch summary

---

`GET /v1/fetches/{fetch_id}`

Success response:

```json
{
  "id": "fx_01JV8W5J8M8F3J5V4A8Q",
  "target": "docs:/tax/2022/invoice-123.pdf",
  "state": "waiting_media",
  "files": 1,
  "bytes": 91233,
  "copies": [
    { "id": "BR-014-A", "location": "Shelf A3" }
  ]
}
```

12. Get fetch manifest

---

`GET /v1/fetches/{fetch_id}/manifest`

Success response:

```json
{
  "id": "fx_01JV8W5J8M8F3J5V4A8Q",
  "target": "docs:/tax/2022/invoice-123.pdf",
  "entries": [
    {
      "id": "e1",
      "path": "/tax/2022/invoice-123.pdf",
      "bytes": 91233,
      "sha256": "2a6c...",
      "copies": [
        {
          "copy": "BR-014-A",
          "location": "Shelf A3",
          "disc_path": "/payload/00/1f/7a.enc",
          "enc": {
            "alg": "age",
            "key_slot": "k1",
            "nonce": "..."
          }
        }
      ]
    }
  ]
}
```

Required behavior:

1. Every entry corresponds to one logical file needed by the fetch.

2. Each entry includes enough information for `arc-disc` to locate and recover candidate encrypted payloads from at least one registered copy.

3. The manifest must be stable for the lifetime of the fetch.

4. Upload recovered file

---

`PUT /v1/fetches/{fetch_id}/files/{entry_id}`

Headers:

```text
Content-Type: application/octet-stream
X-Sha256: <sha256>
```

Body:

* plaintext recovered file bytes

Success response:

```json
{
  "entry": "e1",
  "accepted": true,
  "bytes": 91233
}
```

Required behavior:

1. Uploaded bytes must be verified against the expected logical file hash.

2. On hash mismatch, the request must fail and the bytes must not be materialized.

3. Accepted bytes become available for finalization of the fetch.

4. Complete fetch

---

`POST /v1/fetches/{fetch_id}/complete`

Success response:

```json
{
  "id": "fx_01JV8W5J8M8F3J5V4A8Q",
  "state": "done",
  "hot": {
    "state": "ready",
    "present_bytes": 91233,
    "missing_bytes": 0
  }
}
```

Required behavior:

1. Completion succeeds only if every required entry has been uploaded and verified.
2. On success, all files in the fetch target are hot.
3. The pin that caused the fetch remains active.
4. Repeating complete on a done fetch should return done.

# CLI contract

## General

* `arc` is a thin API client.
* `arc-disc` is a fetch-fulfillment client for a machine with an optical drive.
* Both CLIs must exit `0` on success and non-zero on failure.
* Both CLIs must print machine-readable JSON when invoked with `--json`.
* In non-JSON mode, both CLIs must print concise stable human-readable output.

## `arc` commands

### `arc close PATH`

Required behavior:

* Calls `POST /v1/collections/close`
* On success, prints the resulting collection id and summary

### `arc find QUERY`

Required behavior:

* Calls `GET /v1/search`
* Prints returned targets
* Returned targets must be usable directly with `arc pin` and `arc release`

### `arc show COLLECTION`

Required behavior:

* Calls `GET /v1/collections/{collection_id}`

### `arc plan`

Required behavior:

* Calls `GET /v1/plan`

### `arc iso get IMAGE_ID [-o FILE]`

Required behavior:

* Calls `GET /v1/images/{image_id}/iso`
* Writes bytes to `FILE` or stdout if supported by implementation

### `arc copy add IMAGE_ID COPY_ID --at LOCATION`

Required behavior:

* Calls `POST /v1/images/{image_id}/copies`

### `arc pin TARGET`

Required behavior:

* Calls `POST /v1/pin`
* If a fetch is returned, prints the fetch id and candidate copies

### `arc release TARGET`

Required behavior:

* Calls `POST /v1/release`

### `arc pins`

Required behavior:

* Calls `GET /v1/pins`

### `arc fetch FETCH_ID`

Required behavior:

* Calls `GET /v1/fetches/{fetch_id}`

## `arc-disc` commands

### `arc-disc fetch FETCH_ID [--device DEVICE]`

Required behavior:

1. Calls `GET /v1/fetches/{fetch_id}/manifest`
2. Prompts or prints which copy to insert based on manifest data
3. Reads encrypted payloads from the selected optical copy
4. Decrypts recovered file bytes
5. Uploads each recovered file through `PUT /v1/fetches/{fetch_id}/files/{entry_id}`
6. Calls `POST /v1/fetches/{fetch_id}/complete`
7. Exits success only if the fetch reaches `done`

# Behavioral invariants

These are the most important acceptance-test targets.

## Pin invariants

1. Pinning the same target twice results in exactly one active pin.
2. Releasing a target that is not pinned is a successful no-op.
3. A file is logically required in hot iff at least one active pin selects it.
4. Releasing a broad pin does not affect remaining narrow pins.
5. Releasing a narrow pin does not affect remaining broad pins.

## Hot invariants

1. Immediately after collection close, every file in the collection is hot.
2. A file may stop being hot only if no active pin requires it and implementation policy has reconciled the hot view.
3. A file restored by a completed fetch is hot.
4. A file that is hot and pinned must remain hot across unrelated releases.

## Archive invariants

1. A file counts as archived if and only if at least one registered copy contains it.
2. `pending_bytes = bytes - archived_bytes` for every collection.
3. Registering a copy cannot reduce archived coverage.

## Fetch invariants

1. Pinning a fully hot target creates no fetch.
2. Pinning a partially or fully cold archived target creates or reuses one fetch.
3. A fetch cannot complete until all required entries are uploaded and verified.
4. Uploading bytes with a wrong hash never results in a completed fetch.
5. Completing a fetch results in the fetch target being hot.

## Selector invariants

1. The same canonical target string means the same file set everywhere in API and CLI.
2. A directory target includes all descendant files and no siblings.
3. A file target includes exactly one file.
4. A collection target includes every file in the collection.

# Recommended acceptance test set

Below is the concise suite I would start with.

## A. Collection lifecycle

A1. Close staged collection
Given a staged directory with known files
When `POST /collections/close` is called
Then a new collection exists
And its `bytes` and `files` match fixture contents
And `hot_bytes = bytes`
And `archived_bytes = 0`

A2. Duplicate close fails
Given a previously closed staged path
When close is called again
Then the API returns failure with a stable error code

## B. Pin and release semantics

B1. Pin whole collection already hot
Given a newly closed collection
When `POST /pin` with the collection target
Then response has `fetch = null`
And pin appears exactly once in `GET /pins`

B2. Pin single file already hot
Given a newly closed collection
When pinning one file target
Then response reports `missing_bytes = 0`

B3. Release non-existent pin is no-op
Given no active pin for target T
When releasing T
Then success is returned
And pin list is unchanged

B4. Broad and narrow coexist
Given pins for `docs:/tax/` and `docs:/tax/2022/invoice.pdf`
When releasing `docs:/tax/`
Then `docs:/tax/2022/invoice.pdf` remains pinned
And that file remains hot

B5. Narrow release under broad pin
Given pins for `docs:/tax/` and `docs:/tax/2022/invoice.pdf`
When releasing `docs:/tax/2022/invoice.pdf`
Then `docs:/tax/` remains pinned
And that file remains hot

## C. Archive coverage

C1. Register copy increases archived coverage
Given an image covering files in a collection
When a copy is registered
Then affected collection summaries reflect increased `archived_bytes`
And `pending_bytes` decreases accordingly

C2. Duplicate copy id fails
Given an existing copy id
When registering another copy with the same id
Then the request fails

## D. Fetch lifecycle

D1. Pin cold archived file creates fetch
Given a file that is archived but not hot
When pinning that file
Then response contains a fetch id
And fetch state is `waiting_media`

D2. Fetch manifest is stable
Given a newly created fetch
When reading its manifest twice
Then the entry ids and required logical files are identical

D3. Wrong hash upload fails
Given a fetch with one required entry
When uploading incorrect plaintext bytes
Then upload fails with `hash_mismatch`
And fetch cannot complete

D4. Successful fetch completion
Given a fetch with all correct entry uploads
When `complete` is called
Then fetch state is `done`
And the target is hot

D5. Pin remains after fetch
Given a completed fetch created by a pin
When listing pins
Then the original target remains pinned

## E. Selector behavior

E1. Collection selector covers all files
Given a collection with N files
When pinning the collection target
Then logical targeted bytes equal collection bytes

E2. Directory selector covers descendants only
Given files under `/a/` and `/b/`
When pinning `collection:/a/`
Then only `/a/` descendants are targeted

E3. File selector covers exactly one file
Given a known file target
When pinning it
Then targeted bytes equal that file’s bytes

E4. Invalid selector rejected
Given malformed targets
When pinning or releasing them
Then API returns `invalid_target`

## F. CLI parity

F1. `arc pin` mirrors API
Given a valid target
When `arc pin TARGET --json` is run
Then its JSON matches the structure of `POST /pin`

F2. `arc release` mirrors API
Given a valid target
When `arc release TARGET --json` is run
Then its JSON matches the structure of `POST /release`

F3. `arc-disc fetch` completes fetch
Given a recoverable fetch and suitable test optical fixture
When `arc-disc fetch FETCH_ID --json` is run
Then the fetch reaches `done`

# Minimal formal fixture model

For test design, use fixtures with these characteristics:

* one small collection with nested directories
* at least one file unique to `/a/`
* at least one file unique to `/b/`
* at least one collection large enough to participate in an image
* at least one archived-but-not-hot file
* at least one hot-and-archived file
* at least one hot-but-not-yet-archived file

That is enough to exercise all core transitions.

# Deliberate non-goals for MVP

These should not appear in acceptance tests:

* browsing collection trees over the API
* partial directory listing endpoints
* writable mutation of hot filesystem by user action outside API
* automatic cache eviction policy details
* multiple fetch merge or dedup policy beyond observable idempotent behavior
* advanced planner heuristics beyond reported plan outputs

# Strong recommendation on test style

Write acceptance tests against three layers separately:

1. API black-box tests for all endpoint contracts and invariants
2. `arc` CLI snapshot tests in `--json` mode
3. `arc-disc` integration tests against a fixture disc image or a fake disc reader
