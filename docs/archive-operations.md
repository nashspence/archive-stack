# Archive operations

The remote archive account is Riverhog's durable storage authority. Operational readiness
therefore includes human and provider controls that application tests cannot establish.

## Account readiness

Keep these current and periodically exercise them:

- account recovery and multi-factor authentication;
- payment method, billing alerts, and enough budget for retained storage;
- service credentials and least-privilege bucket permissions;
- provider contacts and recovery procedures;
- bucket listing, object metadata, object reads, and restore requests;
- archive passphrase custody and recovery.

After account, credential, provider, or storage-class changes, run an authenticated
archive report and retrieve a known collection.

## Recovery exercise

Periodically retrieve a known archive into a controlled recovery environment, verify the
encrypted object, decrypt it with the configured age tooling, and compare its manifest and
logical file digests with the catalog. A report or object listing alone does not establish
recoverability.

Riverhog maintains plaintext `README.md` and `AGENTS.md` guidance at the archive root.
Opaque names do not mean objects are unused; encrypted collection objects may be the sole
durable copies.

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
