# Resumable Uploads Reference

Riverhog uses the same resumable-upload lifecycle for collection ingest and fetch recovery:

- the JSON API binds uploads to a server-owned domain resource
- the returned upload resource uses tus-compatible resumable upload semantics within the contract published for that workflow
- incomplete bytes stage under `.arc/uploads/` rather than appearing immediately as committed hot files
- Riverhog promotes staged bytes to `collections/{collection_id}/{path}` only after verification and workflow completion
- upload state survives service restart until `INCOMPLETE_UPLOAD_TTL` expires
- expiry cancels the upload resource, deletes incomplete server-side bytes, and resets the domain resource cleanly

For collection ingest specifically:

- the terminal successful collection-file upload chunk finalizes the collection immediately once every required file verifies
- once the last resumable collection-file state expires, Riverhog forgets the upload session instead of keeping an empty pending record

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
Split files still use one upload resource per logical file; `arc-disc` streams parts into that one resource in
ascending order.
