# Resumable uploads

Riverhog uses tusd resources for collection file transfer and records upload state in PostgreSQL.

## Determinate collection upload

`POST /v1/collection-uploads` receives a slug and complete file manifest. Riverhog mints the collection id, creates one durable upload record per file, and returns the current session. Repeating the same canonical request resumes the session.

For each file, `POST /v1/collection-uploads/{collection_id}/files/{path}/upload` returns the resumable upload URL and authoritative offset. The client sends bounded chunks and re-reads the offset after an interrupted request.

## Incremental collection upload

`POST /v1/collection-upload-sessions` creates an open session. The client registers each path and digest, uploads it, then calls `complete` to freeze the manifest. Canceling closes the session and releases its staged resources.

## Finalization

After every declared file is byte-complete, Riverhog verifies logical sizes and digests, builds the deterministic encrypted collection package, uploads and verifies the remote object, records the manifest and proof, and materializes logical files into hot storage. The upload is finalized only after archive verification succeeds.

Archive multipart progress is durable. A worker resumes incomplete archive uploads from cataloged part state and retries transient failures using the configured sweep and retry intervals.

## Transport semantics

- one logical upload resource per file
- server-authoritative offsets
- bounded client chunks
- idempotent create-or-resume calls
- explicit expiry for incomplete sessions
- exact logical byte and SHA-256 verification
- safe retry after service or network interruption

See [upload transport](upload-transport.md) for client and proxy sizing.
