# Fetch state machine

## States

- `draft`
- `queued_djdan`
- `uploading`
- `verifying`
- `queued_archive`
- `restoring_archive`
- `done`
- `failed`

## Allowed transitions

```text
draft -> queued_djdan -> uploading -> verifying -> done
draft -> queued_archive -> restoring_archive -> done
queued_archive -> draft
restoring_archive -> draft
done -> queued_djdan
uploading -> queued_djdan
uploading -> failed
verifying -> failed
restoring_archive -> failed
```

## Meanings

### draft

The named fetch exists and can still be edited. Draft fetches can add or remove
target selectors. They are not visible to `djdan fetch` until started.

### queued_djdan

The fetch is frozen and queued for the prompt-based optical-media workflow.
When an operator webhook is configured, Riverhog emits `fetches.queued_djdan`
and repeats `fetches.queued_djdan.reminder` on the configured reminder interval
while the fetch is still queued.

### uploading

One or more recovered files are being streamed directly from optical recovery into resumable upload resources.

### verifying

All required files have been uploaded and are being decrypted, verified, and materialized by the server.

### queued_archive

The fetch is frozen and has been selected for archive materialization, but the
fetch materialization archive restores have not started yet.

### restoring_archive

Riverhog is creating or resuming archive restores, waiting for temporary
restored data, verifying archive artifacts, and materializing the selected files
back into hot storage.

### done

All bytes selected by the fetch targets are currently hot. The fetch remains
readable as the recovery record for that named operator intent.

### failed

The fetch cannot currently complete.

Final verification failure for a `byte_complete` entry does not close the fetch. The recovery client deletes the affected
entry upload resource, the entry returns to `pending`, and the fetch remains active so the operator can retry from another
registered disc or from recovered media.

## Upload-state expiry

- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk for that manifest
- expiry discards incomplete cached upload data and moves the manifest back to `queued_djdan`
- the fetch summary should expose the expiry boundary as an audit field such as `upload_state_expires_at`
