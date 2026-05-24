# Fulfill a fetch from optical media

The `djdan` CLI is the recovery client for a machine with an optical drive.

## Host requirements

- Install `xorriso` on the operator machine.
- Run the command as a user that can read the optical device path, such as `/dev/sr0`.
- A mounted disc directory can be passed to `--device` instead of a raw device path. In that mode, `djdan` reads the
  manifest's hinted payload object path directly from the mounted filesystem.
- For a raw optical device, `djdan` uses `xorriso` to extract each hinted payload object from the inserted disc before
  streaming it to the server.

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

If final server verification rejects a `byte_complete` entry, `djdan fetch` cancels that entry upload resource before
exiting. The manifest stays active and incomplete with the rejected entry back at offset `0`. Try another registered copy
for that entry when one is available. If every registered copy fails, report the
damaged copies and complete an image rebuild session before running
`djdan fetch` again from recovered media.

During fulfillment, `djdan` should show:

- current file progress
- whole-manifest progress
- current transfer speed
- which disc or copy is needed next

CLI example:

```bash
djdan fetch fx_01JV8W5J8M8F3J5V4A8Q --device /dev/sr0 --json
```

Mounted-media example:

```bash
djdan fetch fx_01JV8W5J8M8F3J5V4A8Q --device /media/archive-disc --json
```

The command should exit successfully only if the fetch reaches `done`.

Expected failures include a missing `xorriso` executable, insufficient permission to read the device or mount, missing
payload objects on the inserted disc, and final server-side verification rejecting the recovered bytes.
