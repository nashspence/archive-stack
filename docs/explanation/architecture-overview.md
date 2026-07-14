# Architecture overview

Riverhog accepts immutable logical collections, stores their files in a fast materialized cache, and creates one deterministic encrypted archive package per collection in remote object storage. PostgreSQL is the catalog authority; object stores hold the bytes.

## Custody path

1. A client creates a collection upload from a slug and file manifest.
2. Files arrive through resumable upload resources in staging storage.
3. Riverhog verifies each logical file against its declared size and SHA-256 digest.
4. Riverhog builds and encrypts the collection archive, uploads it to remote storage, verifies the remote object, and records the manifest and OpenTimestamps proof.
5. Verified logical files become available in hot storage and the collection becomes accepted.

A collection is durable only when its archive record is uploaded and verified. The archive package, manifest, digest, encryption parameters, and proof together define the recoverable unit. A collection is immutable while present; operators can change the retained set by accepting a new collection and deliberately deleting an existing collection as a whole.

## Remote archive

The remote archive account is the durable storage authority. Operational readiness includes working account recovery, authentication, billing, bucket permissions, object listing, object reads, and restore requests for the configured storage class. These controls should be checked routinely and after credential or provider changes.

Archive object keys are opaque. User-facing identity comes from the catalog and collection manifest, not from bucket paths. Archive credentials have only the permissions required by the configured service role.

Riverhog publishes plaintext `README.md` and `AGENTS.md` guidance at the archive root. Both warn that encrypted collection objects are the sole durable copies Riverhog relies on and that opaque names do not imply unused data.

## Hot storage

Hot storage is a materialized cache for direct downloads and read-only browsing. The catalog records which logical files are currently materialized. Operators can evict selected hot files only after Riverhog verifies durable collection archive coverage.

Missing selected files are restored automatically from their collection archives. Riverhog verifies the encrypted object and extracted logical files before publishing them into the hot namespace.

## Fetches

A fetch is a named, immutable-on-start set of target selectors. Draft fetches can be edited. Starting a fetch completes immediately when every target is hot; otherwise it creates the required archive restores and tracks materialization until every target is hot.

Selectors use the same projected namespace as search, file download, WebDAV browsing, and eviction.

## Service boundaries

Riverhog owns custody and retrieval. Munchy produces finished media collections, Jeb schedules watched-drop submissions, and Gogurt presents the public API through a browser interface. Deployment identity and device mappings remain downstream configuration.
