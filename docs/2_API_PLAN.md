Use five nouns and do not overload them:

`collection` is the logical namespace the user thinks in.
`image` is one planned ISO.
`copy` is one physical burned disc of an image.
`pin` means “keep this target in hot.”
`fetch` is the operational job created only when a pin needs bytes recovered from optical.

That keeps the user model very small:

* search
* pin
* release
* inspect plan
* download image
* register copy

The key rule is this: hot is a read-only projection, never the place the user edits intent.

## Selector syntax

Use one selector string everywhere in API and CLI:

```text
<collection>
<collection>:/dir/
<collection>:/dir/file.ext
```

Rules:

* no path means whole collection
* trailing `/` means directory prefix
* no trailing `/` means exact file
* paths are absolute within the collection, normalized, and may not contain `..`

Examples:

```text
photos-2024
photos-2024:/raw/
photos-2024:/albums/japan/img_0042.cr3
```

This is the most important design choice. It lets the UI stay tiny while still supporting single-file restore and release.

## HTTP API

Use `/v1` and keep the surface deliberately small.

### 1) Close a staged collection

```http
POST /v1/collections/close
Content-Type: application/json
```

Request:

```json
{
  "path": "/srv/archive/staging/photos-2024"
}
```

Response:

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

Meaning:

* scans and freezes the staged directory
* catalogs all files
* materializes it into hot
* adds it to the planner pool

### 2) Search globally

```http
GET /v1/search?q=invoice&limit=25
```

Response:

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
    },
    {
      "kind": "collection",
      "target": "docs",
      "collection": "docs",
      "files": 8231,
      "bytes": 1245678901,
      "hot_bytes": 41324567,
      "archived_bytes": 1245678901,
      "pending_bytes": 0
    }
  ]
}
```

### 3) Show one collection summary

```http
GET /v1/collections/{collection_id}
```

Response:

```json
{
  "id": "photos-2024",
  "files": 18234,
  "bytes": 78123456789,
  "hot_bytes": 1123456789,
  "archived_bytes": 78123456789,
  "pending_bytes": 0,
  "copies": [
    { "id": "BR-021-A", "location": "Shelf B1" },
    { "id": "BR-022-A", "location": "Shelf B1" }
  ]
}
```

Only summary. No browsing endpoint.

### 4) Show the current burn plan

```http
GET /v1/plan
```

Response:

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

If there are several candidate images, return them in order.

### 5) Inspect one image

```http
GET /v1/images/{image_id}
```

Response:

```json
{
  "id": "img_2026-04-20_01",
  "bytes": 43124567890,
  "fill": 0.8625,
  "iso_ready": true,
  "files": 14022,
  "collections": [
    "photos-2024",
    "docs",
    "audio"
  ]
}
```

### 6) Download the ISO for an image

```http
GET /v1/images/{image_id}/iso
```

Returns the ISO file stream.

### 7) Register a physical burned copy

```http
POST /v1/images/{image_id}/copies
Content-Type: application/json
```

Request:

```json
{
  "id": "BR-021-A",
  "location": "Shelf B1"
}
```

Response:

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

This keeps `image` and `copy` separate in exactly the right way.

### 8) Pin a target into hot

```http
POST /v1/pin
Content-Type: application/json
```

Request:

```json
{
  "target": "docs:/tax/2022/invoice-123.pdf"
}
```

Response when already hot:

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

Response when optical recovery is needed:

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

Semantics:

* idempotent
* creates the desired hot residency
* if bytes are missing from hot, also creates a fetch job
* explicit pin remains after fetch completes

### 9) Release a target from hot

```http
POST /v1/release
Content-Type: application/json
```

Request:

```json
{
  "target": "docs:/tax/2022/"
}
```

Response:

```json
{
  "target": "docs:/tax/2022/",
  "pin": false
}
```

Semantics:

* idempotent
* removes exactly that pin
* does not immediately delete bytes
* bytes disappear from the hot view as soon as no remaining pin requires them
* background GC later deletes unreferenced hot blobs

Exact-match release is the right MVP. Do not make release infer anything from filesystem deletions.

### 10) List active pins

```http
GET /v1/pins
```

Response:

```json
{
  "pins": [
    { "target": "docs:/tax/2022/" },
    { "target": "photos-2024:/albums/japan/" }
  ]
}
```

### 11) Inspect a fetch job

```http
GET /v1/fetches/{fetch_id}
```

Response:

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

States for MVP:

```text
waiting_media
uploading
verifying
done
failed
```

### 12) Get the fetch manifest for the disc-attached tool

```http
GET /v1/fetches/{fetch_id}/manifest
```

Response:

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

This is the only place that needs disc-format detail.

### 13) Upload recovered file content for a fetch entry

```http
PUT /v1/fetches/{fetch_id}/files/{entry_id}
Content-Type: application/octet-stream
X-Sha256: 2a6c...
```

Body is the recovered plaintext file bytes.

Response:

```json
{
  "entry": "e1",
  "accepted": true,
  "bytes": 91233
}
```

### 14) Finalize a fetch after all files are uploaded

```http
POST /v1/fetches/{fetch_id}/complete
```

Response:

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

That is the entire MVP API.

## What the server stores

The minimum durable entities are:

```text
collections
files
images
copies
pins
fetches
fetch_entries
```

The one important behavior rule is:

* effective hot set = union of active pins

So if these both exist:

```text
docs:/tax/
docs:/tax/2022/invoice-123.pdf
```

and you release `docs:/tax/`, the file stays hot because the file pin still exists.

## CLI

Use two binaries.

`arc` talks to the API from anywhere.
`arc-disc` runs on the machine with the optical drive.

That split matches your architecture and keeps each tool intuitive.

### `arc`

Configure once with base URL and token.

Commands:

```text
arc close PATH
arc find QUERY
arc show COLLECTION
arc plan
arc iso get IMAGE_ID [-o FILE]
arc copy add IMAGE_ID COPY_ID --at LOCATION
arc pin TARGET
arc release TARGET
arc pins
arc fetch FETCH_ID
```

Meaning and examples:

```bash
arc close /srv/archive/staging/photos-2024
```

Closes a staged upload into a collection.

```bash
arc find "invoice 123"
```

Searches globally and prints result targets you can pass back into `pin` or `release`.

```bash
arc show photos-2024
```

Shows one collection summary.

```bash
arc plan
```

Shows the current best disc plan.

```bash
arc iso get img_2026-04-20_01 -o img_2026-04-20_01.iso
```

Downloads a planned ISO.

```bash
arc copy add img_2026-04-20_01 BR-021-A --at "Shelf B1"
```

Registers one burned physical disc.

```bash
arc pin docs:/tax/2022/invoice-123.pdf
```

If already hot, it says so.
If not, it prints the fetch id and required copies.

Example output:

```text
pinned: docs:/tax/2022/invoice-123.pdf
fetch: fx_01JV8W5J8M8F3J5V4A8Q
needs: BR-014-A (Shelf A3)
```

```bash
arc release docs:/tax/2022/
```

Removes that exact pin.

```bash
arc pins
```

Lists all active pins.

```bash
arc fetch fx_01JV8W5J8M8F3J5V4A8Q
```

Shows fetch status only.

### `arc-disc`

Keep this tool tiny. It only needs one real command:

```text
arc-disc fetch FETCH_ID [--device /dev/sr0]
```

Example:

```bash
arc-disc fetch fx_01JV8W5J8M8F3J5V4A8Q --device /dev/sr0
```

Behavior:

* downloads the fetch manifest from the API
* tells the operator which copy to insert
* reads the encrypted per-file payloads from the disc
* decrypts and verifies each recovered file
* uploads plaintext bytes back to the API with `PUT /v1/fetches/{id}/files/{entry_id}`
* calls `POST /v1/fetches/{id}/complete`
* exits only when the target is hot

That is the whole companion tool for MVP.

I would not add browsing, ad hoc extraction, or local catalog search to `arc-disc` yet.

## Why this is the elegant MVP

It keeps the public concepts very small.

The user only has to learn:

* collection
* image
* copy
* pin
* fetch

And only one selector syntax:

```text
collection
collection:/dir/
collection:/file
```

That gives you:

* whole-collection pin/release
* directory pin/release
* single-file pin/release
* minimal web UI
* no writable hot tree
* no filesystem scan to infer intent
* no “restore whole collection or nothing”

## Recommended hot layout

This is not part of the API, but it is the right fit for it:

```text
/hot/objects/ab/cd/<sha256>
/hot/view/<collection>/... -> symlink or generated projection into objects
```

So:

* bytes live in a content-addressed hot object store
* the visible hot tree is just a read-only view
* `pin` and `release` update metadata and projection
* GC later removes unreferenced objects

## One deliberate omission

I would not expose a “browse collection paths” endpoint in MVP.

Search plus selector-based actions is enough, and it keeps you from drifting into building a file manager.

## The exact default UX I would ship

Web UI:

* search box
* search results
* collection summary page
* current plan page
* image detail page
* buttons for pin and release

CLI:

* `arc find`
* `arc pin`
* `arc release`
* `arc plan`
* `arc iso get`
* `arc copy add`
* `arc-disc fetch`
