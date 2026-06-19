# ADR-0042: Use Opaque Glacier Archive Keys

## Decision

New collection archive packages are stored under randomly minted opaque archive
ids instead of canonical collection ids:

```text
{RIVERHOG_GLACIER_PREFIX}/archives/{opaque-archive-id}/archive.tar.age
{RIVERHOG_GLACIER_PREFIX}/archives/{opaque-archive-id}/manifest.yml.age
{RIVERHOG_GLACIER_PREFIX}/archives/{opaque-archive-id}/manifest.yml.ots.age
```

The archive id is random and persisted on the collection upload row before the
archive upload begins. Multipart retries, worker retries, and app restarts reuse
the same object keys.

Riverhog also publishes two recovery aids under the configured archive prefix:

```text
README.md
catalog/collections.yml.age
```

`README.md` is plaintext but collection-agnostic. It explains recovery with
standard S3 credentials, the archive passphrase, `age`, `tar`, and optional
OpenTimestamps verification. `catalog/collections.yml.age` is encrypted with the
archive passphrase and maps private collection ids to opaque archive object
paths.

## Reason

Archive encryption protects object contents, but S3 object keys are metadata.
Object listings, access logs, console screenshots, and support
contexts can expose collection slugs if keys include canonical collection ids.
Random opaque ids avoid leaking those slugs while keeping each archive package's
three objects grouped together.

The encrypted catalog preserves a humane disaster-recovery path without making
the bucket listing itself private-data-bearing. The database remains
authoritative; the encrypted catalog is a derived recovery aid that can be
refreshed after successful archive finalization.

## Consequences

- S3 object listings no longer reveal collection ids for newly uploaded archives.
- Operators need the database or encrypted catalog to map a collection id to an
  opaque archive id.
- The plaintext README must avoid collection names, timestamps, and app-specific
  assumptions.
- Recovery with standard tools still works when the operator has S3 access and
  the archive passphrase.
- Archive upload resume state must persist the opaque storage prefix before any
  object upload begins.
