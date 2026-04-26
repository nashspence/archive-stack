# Resumable Uploads Reference

Riverhog uses the same resumable-upload lifecycle for collection ingest and fetch recovery:

- the JSON API binds uploads to a server-owned domain resource
- the returned upload resource uses tus-compatible resumable upload semantics
- upload state survives service restart until `INCOMPLETE_UPLOAD_TTL` expires
- expiry cancels the upload resource, deletes incomplete server-side bytes, and resets the domain resource cleanly

For collection ingest specifically:

- the terminal successful collection-file upload chunk finalizes the collection immediately once every required file verifies
- once the last resumable collection-file state expires, Riverhog forgets the upload session instead of keeping an empty pending record

## Collection File Upload Session

`POST /v1/collection-uploads/{collection_id}/files/{path}/upload` creates or resumes the upload resource for one logical
collection file.

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

Collection uploads measure offsets against the logical file byte stream for that file.

Fetch uploads measure offsets against the ordered recovery-byte stream for that manifest entry. Split files still use
one upload resource per logical file; `arc-disc` streams parts into that one resource in ascending order.
