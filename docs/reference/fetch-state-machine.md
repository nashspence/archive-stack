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
waiting_media -> failed
uploading -> failed
verifying -> failed
```

## Meanings

### waiting_media

The fetch exists and requires optical recovery input.

### uploading

One or more recovered files are being uploaded.

### verifying

All required files have been uploaded and are being verified and materialized.

### done

All required files are verified and materialized in hot storage.

### failed

The fetch cannot currently complete.
