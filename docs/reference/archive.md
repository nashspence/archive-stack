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

## Routine checks

Use `riverhog collection show <collection-id>` for collection custody details and `GET /v1/archive` for the aggregate report. Periodically verify credentials, retrieve a known archive, decrypt it in a controlled recovery environment, and compare its manifest and file digests with the catalog.
