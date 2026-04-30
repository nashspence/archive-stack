# Fetch state machine

## States

- `waiting_media`
- `uploading`
- `verifying`
- `done`
- `failed`

## Allowed transitions

```text
waiting_media -> uploading -> verifying -> done
waiting_media -> uploading -> verifying -> failed
uploading -> waiting_media
waiting_media -> failed
uploading -> failed
verifying -> failed
```

## Meanings

### waiting_media

The pin-scoped fetch manifest exists, is still selected by an exact pin, and requires optical recovery input.

### uploading

One or more recovered files are being streamed directly from optical recovery into resumable upload resources.

### verifying

All required files have been uploaded and are being decrypted, verified, and materialized by the server.

### done

All bytes selected by the exact pinned selector are currently hot. The manifest remains readable until that exact pin is
released.

### failed

The fetch cannot currently complete.

Final verification failure for a `byte_complete` entry does not close the fetch. The recovery client deletes the affected
entry upload resource, the entry returns to `pending`, and the fetch remains active so the operator can retry from another
registered copy or from recovered media.

## Upload-state expiry

- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk for that manifest
- expiry discards incomplete cached upload data and moves the manifest back to `waiting_media`
- the fetch summary should expose the expiry boundary as an audit field such as `upload_state_expires_at`
