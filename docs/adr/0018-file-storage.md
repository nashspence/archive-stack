# ADR 0018: Use SeaweedFS for archive file storage

## Status

Accepted.

## Context

Riverhog needs durable byte storage for two user-visible workflows:

1. Keeping hot collection files available after a collection is closed.
2. Accepting resumable encrypted recovery uploads while restoring archived files.

The catalog remains the source of truth for collections, images, copies, pins, fetches, upload state, and verification metadata. File bytes live in SeaweedFS, addressed by stable paths through the Filer API.

## Decision

Use SeaweedFS as Riverhog's file storage layer.

Committed hot collection files are stored at:

```text
/collections/{collection_id}/{path}
```

Completed encrypted recovery uploads are stored at:

```text
/.arc/recovery/{fetch_id}/{entry_id}.enc
```

Collection close reads files from the collection path in SeaweedFS, records their metadata in the catalog, and keeps the bytes in SeaweedFS.

Fetch recovery uploads use SeaweedFS TUS upload sessions. Riverhog creates an upload session for the recovery target path, stores the returned TUS URL on the fetch entry, and returns that URL to the client. Clients upload recovery bytes directly to SeaweedFS.

After a recovery upload completes, Riverhog reads the assembled encrypted payload from its recovery path, decrypts and verifies it, then writes the restored plaintext file back to the collection path.

Pin release and hot-state reconciliation remove files from SeaweedFS when they are no longer hot. Released or expired fetches also clean up their recovery upload state.

The runtime configuration exposes the SeaweedFS filer URL as:

```text
ARC_SEAWEEDFS_FILER_URL
```

## Consequences

* Users can close a collection without Riverhog copying file bytes into the catalog database.
* Hot files remain available through Riverhog as long as they are pinned or otherwise hot.
* Recovery uploads can resume through SeaweedFS TUS sessions.
* Restored files are promoted back into the same collection storage namespace.
* The catalog stores metadata and state; SeaweedFS stores bytes.
* Local development and test environments must provide SeaweedFS with Filer and TUS support.
