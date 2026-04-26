# Configuration Reference

## `ARC_SEAWEEDFS_FILER_URL`

- type: URL
- default: `http://localhost:8888`

This is the SeaweedFS Filer base URL Riverhog uses for committed collection files and fetch recovery upload targets.

Committed hot collection content lives at `/collections/{collection_id}/{path}` within the filer namespace.

Completed encrypted recovery uploads live at `/.arc/recovery/{fetch_id}/{entry_id}.enc`.

## `ARC_DB_PATH`

- type: absolute or relative path
- default: `.arc/state.sqlite3`

This is the SQLite catalog path used for durable authoritative API state.

## `INCOMPLETE_UPLOAD_TTL`

- type: duration
- default: `24h`

This controls how long incomplete server-side upload state for one fetch-manifest entry may remain resumable after the
last successfully accepted chunk.

Service restart does not shorten this TTL or discard unexpired upload state by itself.

When the TTL expires:

- the pending SeaweedFS TUS session is cancelled
- any incomplete recovery target object is deleted
- the entry returns to `pending`
- the fetch manifest returns to `waiting_media` if any selected bytes are still not hot
- `upload_state_expires_at` becomes `null` until a new upload session is opened
