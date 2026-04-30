# Fulfill a fetch from optical media

The `arc-disc` CLI is the recovery client for a machine with an optical drive.

## Flow

1. Read the fetch manifest.
2. Determine which disc to insert next from the manifest's part-level recovery hints.
3. Read the raw encrypted payload-object bytes from each required optical copy.
4. Stream those recovery bytes directly into the entry upload resource, continuing in ascending part order when the
   logical file spans multiple discs.
5. Let the server handle any required decryption and final file validation for each manifest entry.
6. Complete the fetch.

If recovery is interrupted after upload has started, the server-side manifest keeps resumable upload state for
`INCOMPLETE_UPLOAD_TTL` after the last accepted chunk. The default is `24h`. After that boundary, incomplete upload
data is discarded and the manifest returns to `waiting_media`.

If final server verification rejects a `byte_complete` entry, `arc-disc fetch` cancels that entry upload resource before
exiting. The manifest stays active and incomplete with the rejected entry back at offset `0`. Try another registered copy
for that entry when one is available. If every registered copy fails, report the damaged copies and complete the
Glacier-backed recovery session before running `arc-disc fetch` again from recovered media.

During fulfillment, `arc-disc` should show:

- current file progress
- whole-manifest progress
- current transfer speed
- which disc or copy is needed next

CLI example:

```bash
arc-disc fetch fx_01JV8W5J8M8F3J5V4A8Q --device /dev/sr0 --json
```

The command should exit successfully only if the fetch reaches `done`.
