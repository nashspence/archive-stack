# API reference

The FastAPI application exposes its canonical OpenAPI document at `/openapi.json`; the checked copy is `contracts/openapi/riverhog.v1.yaml`. All public resources use the `/v1` prefix.

## Collections and uploads

- `GET /v1/collections` lists collection summaries.
- `GET /v1/collections/{collection_id}` returns one collection.
- `POST /v1/collection-uploads` creates or resumes a determinate upload from a complete manifest.
- `GET /v1/collection-uploads/{collection_id}` returns upload progress.
- `POST /v1/collection-uploads/{collection_id}/files/{path}/upload` creates or resumes a file upload.
- `POST /v1/collection-upload-sessions` creates or resumes an incremental session.
- `POST /v1/collection-upload-sessions/{collection_id}/files` registers a file.
- `POST /v1/collection-upload-sessions/{collection_id}/files/upload` registers a file and creates its upload.
- `POST /v1/collection-upload-sessions/{collection_id}/complete` closes an incremental manifest.
- `POST /v1/collection-upload-sessions/{collection_id}/cancel` cancels an open session.

## Search and files

- `GET /v1/search` provides paginated query, collection, hot-state, sort, and order filters.
- `GET /v1/files?target=...` resolves a canonical selector.
- `GET /v1/files/{target}/content` downloads a hot file.

## Fetches and hot storage

- `GET /v1/fetches` lists fetches.
- `POST /v1/fetches` creates a draft.
- `POST /v1/fetches/{fetch_id}/targets` adds selectors.
- `DELETE /v1/fetches/{fetch_id}/targets` removes selectors.
- `POST /v1/fetches/{fetch_id}/start` freezes and starts retrieval.
- `POST /v1/fetches/{fetch_id}/cancel` cancels active retrieval.
- `GET /v1/fetches/{fetch_id}` returns the fetch resource.
- `GET /v1/fetches/{fetch_id}/status` returns progress and archive restores.
- `GET /v1/fetches/{fetch_id}/files` lists the resolved files.
- `POST /v1/hot/evict` previews or performs selector-based eviction.

## Archive

- `GET /v1/archive` returns archive usage and measured remote storage.
- `GET /v1/archive-restores` lists restore operations.
- `GET /v1/archive-restores/{archive_restore_id}` returns one restore.
- `POST /v1/archive-restores/{archive_restore_id}/cancel` cancels an active restore.

## Jeb

- `GET /v1/jeb/status` returns scheduler status.
- `GET /v1/jeb/attempts` lists collection attempts.
- `GET /v1/jeb/config/check` validates active configuration.
- `POST /v1/jeb/once` starts one scheduler pass.
- `POST /v1/jeb/archive-now` starts or previews one account submission.

## Errors

Errors use a stable JSON object with a machine code and human-readable detail. Validation errors use HTTP 422; missing resources use 404; state conflicts use 409; temporarily unavailable storage uses 503.
