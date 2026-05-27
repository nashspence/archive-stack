# Architecture overview

The runtime uses four cooperating surfaces.

## Catalog

The catalog is the durable authoritative metadata layer. It tracks collections,
logical files, file hashes, collection Glacier archive state, physical copy
coverage, pins, fetches, upload state, and hot presence across service restarts.

## Upload staging

Collection ingest and fetch recovery both stream bytes through Riverhog-managed
tus-compatible upload resources. Incomplete bytes stage under `.riverhog/uploads/`
inside the S3-compatible object store and remain outside the committed hot
namespace until Riverhog verifies them.

Collection ingest begins with a human-readable slug and a complete file
manifest. Riverhog normalizes the slug, mints a timestamped canonical collection
id, and returns that id for subsequent file-upload and status calls. Migration
uploads may provide the timestamp explicitly in UTC basic form while still
providing the slug. Retrying the same normalized slug with the same manifest
resumes the same unfinished upload or returns the already-finalized collection.

Collection ingest has two gates. The upload gate verifies every declared file.
The archive gate builds the whole-collection Glacier archive package, uploads
the archive, manifest, and OpenTimestamps proof, verifies the archive receipt,
and only then admits the collection.

## Committed hot storage

Committed hot files live in one collection-shaped object namespace:

```text
collections/{collection_id}/{path}
```

Only promoted, verified files count as hot. Staged upload keys and other `.riverhog/`
paths are not committed hot files.

Promotion happens after collection Glacier archiving succeeds. A collection still
in `uploading`, `archiving`, or `failed` upload state is not visible in hot
storage, search, read-only browsing, or disc planning.

## Collection Glacier archive

Accepted collections have a deterministic whole-collection archive package under
the Glacier archive prefix. The package uses a deterministic tar archive for
the logical files in the configured Glacier storage class, plus sibling Standard
S3 collection manifest and OpenTimestamps proof objects under the same
collection prefix.

Glacier stores collection archives. Finalized images remain physical disc
artifacts and do not define the cloud archive unit.

Finalization is gated by verified archive receipt. Riverhog persists that
receipt, promotes staged bytes into the hot collection namespace with per-file
verification markers, and commits the collection/archive records before deleting
staging objects. A retry after restart resumes the archive multipart upload, the
completed archive receipt, or the hot-file promotion phase according to the last
durable state.

## Read-only browsing

Read-only WebDAV exposes the committed `collections/` namespace for day-to-day
browsing and download. It must not expose `.riverhog/`, and it is never an upload
surface.

## Why pins exist

Users do not delete or restore by mutating storage surfaces. Instead they:

- pin a selector into hot
- release a previously pinned target
- let the system materialize or reconcile hot state based on active pins

This keeps intent explicit and makes the system safer than inferring meaning
from storage mutations.

## Restore flow

1. The user pins a target.
2. The system creates or reuses one fetch manifest for that exact selector.
3. If all selected bytes are already hot, the fetch manifest is immediately satisfied.
4. If some bytes are archived but not hot, Riverhog can either guide optical
   recovery from verified physical copies or start a Glacier collection-restore
   session for the selected collection content.
5. The server stages the uploaded recovery bytes under `.riverhog/uploads/`, verifies
   and decrypts them, then promotes the recovered logical file into
   `collections/{collection_id}/{path}`.
6. The explicit pin remains active after fetch completion, and the satisfied fetch manifest remains readable until
   release.

## Release flow

1. The user releases an exact selector.
2. The system removes that exact pin, if present.
3. Hot files selected by that released pin are evicted when no remaining pin still covers them.
4. Unrelated hot files are left alone, and a missing exact pin is a successful no-op.

## Eviction flow

1. The user evicts a file, directory, or collection selector from the hot cache.
2. Riverhog skips any selected file still covered by an active pin.
3. Riverhog skips any selected file that is not archived, because hot storage is
   still the only authoritative copy for that file.
4. Archived selected files are removed from committed hot storage and remain
   recoverable through pin/fetch from optical media or Glacier.
