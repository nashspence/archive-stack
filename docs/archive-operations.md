# Archive operations

Riverhog's configured archive stores are its durable storage authority. Operational
readiness therefore includes human and provider controls that application tests cannot
establish.

## Account readiness

Keep these current and periodically exercise them:

- account recovery and multi-factor authentication;
- payment method, billing alerts, and enough budget for retained storage;
- service credentials and least-privilege bucket permissions;
- provider contacts and recovery procedures;
- bucket listing, object metadata, object reads, and restore requests;
- archive passphrase custody and recovery.

After account, credential, provider, or storage-class changes, run an authenticated
archive report and retrieve a known collection from the affected store.

## Recovery exercise

Periodically retrieve a known archive into a controlled recovery environment, verify the
encrypted object, decrypt it with the configured age tooling, and compare its manifest and
logical file digests with the catalog. A report or object listing alone does not establish
recoverability.

Riverhog maintains plaintext `README.md` and `AGENTS.md` guidance at the archive root.
Opaque names do not mean objects are unused; encrypted collection objects may be the sole
durable copies.

## Archive copies

Use `riverhog archive copy --help` to copy a collection between configured archive stores.
The background job verifies the source, prepares it for reading when necessary, streams a
new encrypted destination copy without using hot storage, and records the copy only after
destination verification. The source copy remains intact.

Use `riverhog archive retire --help` to remove one exact collection-and-store copy. Inspect
the plan and confirm the exact collection, selected store, retained verification candidates,
affected objects and bytes, blockers, warning, and short-lived challenge. Execution first
requires a different complete copy to pass current remote verification. It then removes the
selected package, manifest, and proof, refreshes the affected encrypted catalogs, and records
the resulting usage snapshot.

## Collection deletion

Use `riverhog collection delete --help` for the current command interface. Always inspect
the dry-run plan first. Confirm the exact collection id, sole-copy warning, affected
objects and bytes, active-work blockers, and short-lived challenge before execution.

Successful Riverhog deletion removes the complete logical collection, hot materialization,
encrypted package, manifest, proof, recovery-catalog entry, and catalog projections.
Provider retention, object versions, minimum-storage duration, or billing timing may delay
visible cost changes.

Direct provider credentials can bypass Riverhog's ceremony. Protect those credentials and
treat any provider-console or raw-object deletion as an exceptional custody operation.
