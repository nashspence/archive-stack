# Architecture overview

The system uses three layers.

## Catalog

The catalog is the authoritative metadata layer. It tracks collections, logical files, file hashes, archival coverage,
pins, fetches, and hot presence.

## Hot object store

The hot object store stores immutable bytes keyed by content hash. This keeps hot storage operationally simple and makes
deduplication and garbage collection feasible.

A representative layout is:

```text
/hot/objects/ab/cd/<sha256>
```

## Projected namespace

The collection-shaped hot tree is generated from metadata and points into the hot object store.

A representative layout is:

```text
/hot/view/docs/tax/2022/original/path/file.ext -> ../../../../objects/ab/cd/<sha256>
```

## Why pins exist

Users do not delete or restore by mutating the hot tree. Instead they:

- pin a projected-namespace selector into hot
- release a previously pinned target
- let the system materialize or reconcile hot state based on active pins

This keeps intent explicit and makes the system safer than inferring meaning from tree mutations.

## Restore flow

1. The user pins a target.
2. The system creates or reuses one fetch manifest for that exact selector.
3. If all selected bytes are already hot, the fetch manifest is immediately satisfied.
4. If some bytes are archived but not hot, a companion recovery tool reads the indicated optical copy, decrypts
   recovered file bytes, and streams them into resumable upload resources.
5. The server verifies file hashes, materializes bytes into the hot object store, and updates the projected namespace.
6. The explicit pin remains active after fetch completion, and the satisfied fetch manifest remains readable until
   release.

## Release flow

1. The user releases an exact selector.
2. The system removes that exact pin, if present.
3. The projected hot view is reconciled against the remaining union of pins.
4. Unneeded hot blobs become eligible for garbage collection immediately.
