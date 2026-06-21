# Architecture overview

The runtime uses four cooperating surfaces.

## Catalog

The catalog is the durable authoritative metadata layer. It tracks collections,
logical files, file hashes, collection Glacier archive state, physical copy
coverage, fetches, upload state, and hot presence across service restarts.

## Upload staging

Collection ingest and fetch recovery both stream bytes through Riverhog-managed
tus-compatible upload resources. Incomplete bytes stage under `.riverhog/uploads/`
inside the S3-compatible object store and remain outside the committed hot
namespace until Riverhog verifies them.

Collection ingest begins with a human-readable slug. Determinate uploads provide
a complete file manifest up front; incremental upload sessions register files
one at a time and explicitly complete once the file set is frozen. Riverhog
normalizes the slug, mints a timestamped canonical collection id, and returns
that id for subsequent file-upload and status calls. Migration uploads may
provide the timestamp explicitly in UTC basic form while still providing the
slug. Retrying the same normalized slug with the same determinate manifest
resumes the same unfinished upload or returns the already-finalized collection;
retrying the same normalized slug for an open incremental session resumes that
session until it is completed, canceled, or expired.

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
staged upload files from the shared tusd filesystem directory. A retry after
restart resumes the archive multipart upload, the completed archive receipt, or
the hot-file promotion phase according to the last durable state.

## Hot Storage And Fetches

New uploads enter planner jurisdiction after Glacier archival and verified
promotion into hot storage. Until the required verified disc copies exist,
matching files are not evictable. Once files are compliant, `riverhog hot evict`
can remove the selected hot bytes synchronously.

Operators create named fetch manifests when they need hot bytes materialized
again. A fetch can be queued to the prompt-based `djdan fetch` optical-media
workflow, or started with cloud-fetch so Riverhog automatically restores the
selected collection archive data, verifies it, materializes the selected files,
and cleans up temporary Glacier restore state.

## Read-only browsing

Read-only WebDAV exposes the committed `collections/` namespace for day-to-day
browsing and download. It must not expose `.riverhog/`, and it is never an upload
surface.

## Why fetches and eviction exist

Users do not delete or restore by mutating storage surfaces. Instead they:

- create a named fetch and add target selectors to it
- start that fetch for optical media or cloud-fetch materialization
- evict compliant hot bytes explicitly when they no longer need fast access

This keeps intent explicit and makes the system safer than inferring meaning
from storage mutations.

## Fetch flow

1. The user creates a named fetch and adds one or more target selectors.
2. The system keeps a fetch-keyed summary projection for fast operator list and
   show commands.
3. If all selected bytes are already hot, the fetch manifest is immediately satisfied.
4. If some bytes are archived but not hot, the operator starts the fetch for
   `djdan fetch` or with `--cloud`.
5. The server stages the uploaded recovery bytes under `.riverhog/uploads/`, verifies
   and decrypts them, then promotes the recovered logical file into
   `collections/{collection_id}/{path}`.
6. The fetch remains readable after completion as the recovery record for that
   named operator intent.

## Evict flow

1. The user asks `riverhog hot evict` to remove one or more selectors from hot storage.
2. Riverhog refuses if any selected file lacks verified disc protection.
3. Riverhog deletes selected compliant hot files synchronously and reports what changed.
4. Unrelated hot files are left alone.
