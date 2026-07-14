# Domain model

## Collection

An immutable-while-present logical set of files identified by a server-minted timestamped slug. Each file has a stable relative path, byte length, and SHA-256 digest.

A collection is accepted after its encrypted archive package is uploaded and verified. The catalog retains the archive format, compression, object identity, stored bytes, digest, encryption metadata, manifest, proof, and verification time.

Deleting a collection removes the complete logical collection, hot materialization, encrypted archive package, manifest, proof, and catalog projections. Deletion is a guarded operation, not a collection lifecycle state.

## Collection deletion

A short-lived deletion plan enumerates the exact collection impact and carries the archive sole-copy warning. Its state-bound challenge must be returned before execution. An active deletion record exists only while Riverhog completes or retries external storage and catalog changes.

## File

A logical file belongs to exactly one collection. Its projected target combines collection identity and relative path. `hot` records whether verified logical bytes are materialized in the committed cache.

## Archive

One deterministic encrypted package containing a collection's files and manifest. A verified archive is the durability authority for its collection.

## Archive restore

A durable request to retrieve one or more collection archives and materialize selected paths. States are `requested`, `ready`, `completed`, `expired`, `failed`, and `canceled`. Each restore records its collections, selected paths, provider window, verification progress, extraction progress, and materialization progress.

## Fetch

A named ordered resource containing target selectors. States are:

- `draft`: selectors are editable
- `queued_archive`: missing files require archive retrieval
- `restoring_archive`: archive retrieval or materialization is active
- `done`: every selected file is hot
- `failed`: retrieval could not complete

Canceling an active fetch returns it to `draft`.

## Collection upload

A durable upload session contains its slug, collection id, expected or registered files, resumable transfer state, archive phase, timestamps, and failures. Determinate uploads declare the complete manifest at creation. Incremental sessions register files before completion.

## Archive usage snapshot

A point-in-time report of cataloged collections, uploaded collection archives, logical bytes, and measured remote storage bytes.
