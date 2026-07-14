# Remote archive operations

The remote archive account is Riverhog's durable storage authority. Collection archives are deterministic encrypted packages with a cataloged manifest, SHA-256 digest, encryption metadata, and OpenTimestamps proof.

## Account readiness

Keep these controls current and tested:

- account recovery and multi-factor authentication
- valid payment method and billing alerts
- service credentials and least-privilege bucket permissions
- provider contacts and recovery procedures
- bucket listing, object metadata, and object read access
- restore requests for the configured storage class
- archive passphrase custody and recovery

Account and credential changes should include an authenticated archive report and a test retrieval of a known collection archive.

## Collection acceptance

Riverhog accepts a collection after its logical files are verified, its encrypted archive is uploaded, the remote object matches the expected bytes, and its manifest and proof are recorded. The archive record is the collection's durability evidence.

## Retrieval

A fetch selects logical paths. Riverhog groups missing paths by collection, requests the relevant archive objects, verifies each encrypted object, extracts only the selected files, verifies their logical sizes and digests, and publishes them into hot storage. Archive restores expire according to the provider-ready window and can be retried while the collection archive remains verified.

## Archive-root guidance

Riverhog maintains unencrypted `README.md` and `AGENTS.md` files at the configured archive root. They identify the encrypted collection objects as the sole durable copies Riverhog relies on, explain opaque archive naming, and direct people and agents to treat the archive as read-only without exact operator authorization.

## Collection deletion

Use `riverhog collection delete <collection-id> --dry-run` to retrieve the exact deletion plan, sole-copy warning, affected bytes and objects, blockers, expiry, and short-lived confirmation challenge. The plan does not change catalog or storage state.

Interactive `riverhog collection delete <collection-id>` displays that plan and requires the complete collection id. Noninteractive clients return the prior challenge with `--confirm <plan-challenge>`. Riverhog refuses active upload, fetch, or archive-restore conflicts and keeps only transient operation state needed to retry an incomplete deletion.

Successful deletion removes the hot objects, encrypted archive package, manifest, proof, recovery-catalog entry, database rows, and current projections. Measured catalog bytes do not guarantee immediate provider billing changes when retention, object versions, minimum-storage duration, or billing timing apply.

## Routine checks

Use `riverhog collection show <collection-id>` for collection custody details and `GET /v1/archive` for the aggregate report. Periodically verify credentials, retrieve a known archive, decrypt it in a controlled recovery environment, and compare its manifest and file digests with the catalog.
