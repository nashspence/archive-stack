# Configuration Reference

## `INCOMPLETE_UPLOAD_TTL`

- type: duration
- default: `24h`

This controls how long incomplete server-side upload state for one fetch-manifest entry may remain resumable after the
last successfully accepted chunk.

When the TTL expires:

- incomplete upload bytes are discarded
- the entry returns to `pending`
- the fetch manifest returns to `waiting_media` if any selected bytes are still not hot
- `upload_state_expires_at` becomes `null` until a new upload session is opened
