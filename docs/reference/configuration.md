# Configuration Reference

## `ARC_OBJECT_STORE`

- type: enum
- default: `s3`

Selects the committed hot-storage adapter. The active contract is one
S3-compatible object store for committed hot files and incomplete upload
staging.

## `ARC_S3_ENDPOINT_URL`

- type: URL

Base URL for the S3-compatible object-store API.

## `ARC_S3_REGION`

- type: string

Region sent to the S3-compatible object-store client.

## `ARC_S3_BUCKET`

- type: string

Bucket holding both committed hot files and incomplete upload staging.

Committed hot files live at:

```text
collections/{collection_id}/{path}
```

Incomplete staged uploads live at:

```text
.arc/uploads/{upload_id}
```

## `ARC_S3_ACCESS_KEY_ID`

- type: string

Access key used for the S3-compatible object store.

## `ARC_S3_SECRET_ACCESS_KEY`

- type: secret string

Secret key used for the S3-compatible object store.

## `ARC_S3_FORCE_PATH_STYLE`

- type: boolean
- default: implementation-defined; `true` for canonical Garage deployments

Enables path-style S3 requests for backends that require them.

## `ARC_GLACIER_ENDPOINT_URL`

- type: URL
- default: `ARC_S3_ENDPOINT_URL`

Base URL for the archive-upload object-store API.

## `ARC_GLACIER_REGION`

- type: string
- default: `ARC_S3_REGION`

Region sent to the archive-upload object-store client.

## `ARC_GLACIER_BUCKET`

- type: string
- default: `ARC_S3_BUCKET`

Bucket holding finalized-image Glacier uploads.

When this differs from `ARC_S3_BUCKET`, that separate archive bucket must publish
the same abort-incomplete-multipart lifecycle rule as the committed hot-store
bucket.

## `ARC_GLACIER_ACCESS_KEY_ID`

- type: string
- default: `ARC_S3_ACCESS_KEY_ID`

Access key used for Glacier uploads.

## `ARC_GLACIER_SECRET_ACCESS_KEY`

- type: secret string
- default: `ARC_S3_SECRET_ACCESS_KEY`

Secret key used for Glacier uploads.

## `ARC_GLACIER_FORCE_PATH_STYLE`

- type: boolean
- default: `ARC_S3_FORCE_PATH_STYLE`

Enables path-style requests for Glacier-upload backends that require them.

## `ARC_GLACIER_PREFIX`

- type: normalized path prefix
- default: `glacier/finalized-images`

Finalized-image Glacier objects use privacy-safe keys under:

```text
glacier/finalized-images/{image_id}/{image_id}.iso
```

These keys must not embed collection ids or logical file paths.

## `ARC_GLACIER_BACKEND`

- type: string
- default: `s3`

Opaque backend label recorded on finalized-image Glacier summaries.

## `ARC_GLACIER_STORAGE_CLASS`

- type: string
- default: `DEEP_ARCHIVE`

Intended Glacier storage class recorded on finalized-image Glacier summaries.

## `ARC_GLACIER_UPLOAD_RETRY_LIMIT`

- type: integer
- default: `3`

Maximum number of automatic Glacier upload attempts per finalized image before the
upload becomes a persistent failure.

## `ARC_GLACIER_UPLOAD_RETRY_DELAY`

- type: duration
- default: `5m`

Delay between automatic retry attempts for one failed Glacier upload.

## `ARC_GLACIER_UPLOAD_SWEEP_INTERVAL`

- type: duration
- default: `30s`

How often Riverhog's Glacier-upload worker scans for due finalized-image uploads,
retries, and restart-recovered work.

Restart-recovered work resumes one durable job record. It does not resume one
interrupted multipart byte stream inside the remote object store.

## `ARC_GLACIER_FAILURE_WEBHOOK_URL`

- type: URL
- default: unset

Optional webhook endpoint notified when one finalized-image Glacier upload reaches
persistent failure after automatic retries.

The payload includes the finalized `image_id`, failure timestamp, attempt count,
and error context.

## `ARC_TUSD_BASE_URL`

- type: URL

Base URL for the internal `tusd` service that owns resumable staging uploads.
Riverhog remains the public upload contract and maps logical upload resources to
internal `tusd` uploads.

## `ARC_TUSD_HOOK_SECRET`

- type: secret string

Shared secret used to authenticate `tusd` hook callbacks. Hooks are
notifications only; Riverhog's catalog state remains authoritative.

## `ARC_WEBDAV_ENABLED`

- type: boolean
- default: `false`

Enables the supported read-only WebDAV browsing surface for committed hot files.

## `ARC_WEBDAV_ADDR`

- type: address
- default: `127.0.0.1:8080`

Bind address for the read-only WebDAV sidecar when that surface is enabled.
WebDAV must expose only the committed `collections/` namespace and must not
expose `.arc/` staging paths.

## `ARC_DB_PATH`

- type: absolute or relative path
- default: `.arc/state.sqlite3`

This is the SQLite catalog path used for durable authoritative API state.

## `ARC_PUBLIC_BASE_URL`

- type: URL
- default: unset

Optional public API base URL used when Riverhog builds webhook links back to one
finalized image and its ISO download.

## `INCOMPLETE_UPLOAD_TTL`

- type: duration
- default: `24h`

This controls how long incomplete server-side upload state for one collection-upload file or one fetch-manifest entry
may remain resumable after the last successfully accepted chunk.

Service restart does not shorten this TTL or discard unexpired upload state by itself.

When the TTL expires:

- for collection ingest, the staged upload is deleted and that file returns to `pending`
- the pending `tusd` upload is cancelled
- any incomplete staged recovery upload is deleted
- the fetch entry returns to `pending`
- the fetch manifest returns to `waiting_media` if any selected bytes are still not hot
- `upload_state_expires_at` becomes `null` until a new upload session is opened

## `UPLOAD_EXPIRY_SWEEP_INTERVAL`

- type: duration
- default: `30s`

This controls how often Riverhog's background expiry reaper sweeps collection-upload and fetch-upload state looking
for entries whose published `INCOMPLETE_UPLOAD_TTL` has already elapsed.

Lower values reduce how long expired upload state may remain present after its TTL boundary. Higher values reduce
background sweep frequency at the cost of slower cleanup after expiry.
