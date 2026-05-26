# ADR-0008: Use Collection IDs in Glacier Archive Keys

## Decision

Riverhog stores each collection archive package under the canonical collection
id instead of a hash-derived surrogate:

```text
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/archive.tar
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml.ots
```

The archive tar contains only the logical files and uses the configured Glacier
storage class. Riverhog stores the collection manifest and OpenTimestamps proof
as sibling Standard S3 objects under the same collection prefix.

## Reason

Riverhog collection ids are server-minted archival identifiers, not arbitrary
user-provided object paths. Using the canonical id keeps AWS object listings
inspectable during real migrations and makes it clear which collection an
archive object belongs to. Keeping the manifest and proof beside the archive in
Standard S3 keeps those metadata files directly inspectable without a restore
request and lets restore flows fetch and verify the integrity contract
independently of tar extraction.

## Consequences

- Object listings reveal collection ids by design.
- Recovery restores the archive object and reads the Standard S3 manifest/proof objects directly.
- S3 multipart resume state is tracked for the large archive object.
- The old hashed-prefix archive layout is not supported.
