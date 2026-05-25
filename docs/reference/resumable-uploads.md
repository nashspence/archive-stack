# Resumable Uploads Reference

Riverhog uses the same resumable-upload lifecycle for collection ingest and fetch recovery:

- the JSON API binds uploads to a server-owned domain resource
- the returned upload resource uses tus-compatible resumable upload semantics within the contract published for that workflow
- incomplete bytes stage under `.riverhog/uploads/` rather than appearing immediately as committed hot files
- Riverhog promotes staged collection bytes to `collections/{collection_id}/{path}`
  only after file verification and collection Glacier archive verification
- upload state survives service restart until `INCOMPLETE_UPLOAD_TTL` expires
- expiry cancels the upload resource, deletes incomplete server-side bytes, and resets the domain resource cleanly

For collection ingest specifically:

- the terminal successful collection-file upload chunk may move the collection
  upload from `uploading` to `archiving` once every required file verifies
- the collection upload reaches `finalized` only after the whole-collection
  Glacier archive package uploads and verifies
- an archive upload failure leaves the collection upload `failed` and keeps the
  collection invisible until retry succeeds
- `riverhog upload` waits for `finalized` by default; it does not treat staged
  file bytes or `archiving` as a successful completed upload
- staged collection bytes are retained until the finalized collection and
  archive records commit; post-finalization staging cleanup is best-effort, so a
  restart cannot make a retry depend on already-deleted staged bytes
- once the last resumable collection-file state expires, Riverhog forgets the upload session instead of keeping an empty pending record

## Collection Upload Creation

`POST /v1/collection-uploads` creates or resumes a collection upload from a
human-readable slug and a complete file manifest. Clients do not provide
collection ids.

Riverhog normalizes the slug, mints the canonical collection id with the server
UTC upload timestamp, and returns that id for all later file-upload and status
calls. Migration uploads may provide `upload_timestamp` in UTC basic form
`YYYYMMDDTHHMMSSZ` to preserve the original archival timestamp; the slug is
still required. Collection ids are shaped like:

```text
2026/20260524T190233Z__mom-iphone-photos
```

Retry behavior is manifest-aware:

- the same normalized slug and same file manifest resumes the existing
  non-finalized upload
- the same normalized slug and same file manifest returns the finalized
  collection payload after the collection has already completed
- the same normalized slug with a different file manifest creates a separate
  timestamped collection id

## Collection File Upload Session

`POST /v1/collection-uploads/{collection_id}/files/{path}/upload` creates or resumes the upload resource for one logical
collection file.

The returned `upload_url` is a Riverhog-managed tus-compatible upload resource for that logical file. Riverhog supports:

- `HEAD` to read `Upload-Offset`, `Upload-Length`, `Upload-Expires`, and `Location`
- `PATCH` to append bytes using `Content-Type: application/offset+octet-stream` plus `Tus-Resumable`, `Upload-Offset`, and `Upload-Checksum`
- `DELETE` to cancel the current upload resource and reset that file back to `pending`
- `OPTIONS` to advertise the supported tus capability headers

The response exposes at least:

- `path`
- `protocol` — always `tus`
- `upload_url`
- `offset`
- `length`
- `expires_at`
- `checksum_algorithm`

## Fetch Entry Upload Session

`POST /v1/fetches/{fetch_id}/entries/{entry_id}/upload` creates or resumes the upload resource for one recovery-manifest
entry.

The returned `upload_url` is a Riverhog-managed tus-compatible upload resource for that manifest entry. Riverhog
supports:

- `HEAD` to read `Upload-Offset`, `Upload-Length`, `Upload-Expires`, and `Location`
- `PATCH` to append bytes using `Content-Type: application/offset+octet-stream` plus `Tus-Resumable`, `Upload-Offset`, and `Upload-Checksum`
- `DELETE` to cancel the current upload resource and reset that manifest entry back to `pending`
- `OPTIONS` to advertise the supported tus capability headers

The response exposes at least:

- `entry`
- `protocol` — always `tus`
- `upload_url`
- `offset`
- `length`
- `expires_at`
- `checksum_algorithm`

## Shared Transport Semantics

Every upload resource must support:

- offset-based resume
- expiration
- checksum validation on streamed chunks
- restart-safe resume until the published expiry time discards incomplete state
- stale background sync or expiry work must not roll committed upload progress backward after a
  request-driven transition has already verified, consumed, or reset that upload resource

Collection uploads measure offsets against the logical file byte stream for that file.
Collection upload resources expose Riverhog-managed tus-compatible `HEAD`/`PATCH`/`DELETE`/`OPTIONS` semantics on the
published `upload_url`.

Fetch uploads measure offsets against the ordered recovery-byte stream for that manifest entry. Fetch upload resources
expose Riverhog-managed tus-compatible `HEAD`/`PATCH`/`DELETE`/`OPTIONS` semantics on the published `upload_url`.
Once the recovery-byte stream reaches full length, the manifest entry becomes `byte_complete`; it does not become
`uploaded` until `POST /v1/fetches/{fetch_id}/complete` verifies and materializes the recovered logical file.
Split files still use one upload resource per logical file; `djdan` streams parts into that one resource in
ascending order.

CLI uploads use bounded request chunks plus paced socket writes. The default
request chunk size is 8 MiB. The default write pacing is 256 KiB sub-writes with
a 0.005 second delay, which keeps upload progress stable on paths where
aggressive client-side bulk writes can stall below HTTP. Operators may tune
`RIVERHOG_UPLOAD_CHUNK_BYTES`, `RIVERHOG_UPLOAD_WRITE_CHUNK_BYTES`, and
`RIVERHOG_UPLOAD_WRITE_DELAY_SECONDS` after validating the target network and
reverse-proxy body limits. After all files are staged, `riverhog upload` polls
until the collection finalizes; `RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS` can
bound that wait when automation needs a hard deadline. See
[Upload Transport Reference](upload-transport.md) for the operational findings
and tuning guidance.

When `complete` rejects `byte_complete` recovery bytes, the canonical operator recovery path is an explicit
`DELETE` of the affected fetch-entry upload resource before retry. `djdan fetch` performs that reset for entries it
has made byte-complete, reports that the fetch remains active and incomplete, and lets the next attempt start from offset
`0` with another registered copy or with media restored through the Glacier recovery workflow.
