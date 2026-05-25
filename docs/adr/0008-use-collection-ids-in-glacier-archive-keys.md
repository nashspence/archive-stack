# ADR-0008: Use Collection IDs in Glacier Archive Keys

## Decision

Riverhog stores each collection archive package under the canonical collection
id instead of a hash-derived surrogate:

```text
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/archive.tar
```

The archive tar is the only Glacier object for the collection. It contains the
logical files plus Riverhog's archive manifest and OpenTimestamps proof at
`.riverhog/manifest.yml` and `.riverhog/manifest.yml.ots`.

## Reason

Riverhog collection ids are server-minted archival identifiers, not arbitrary
user-provided object paths. Using the canonical id keeps AWS object listings
inspectable during real migrations and makes it clear which collection an
archive object belongs to. Keeping the manifest and proof inside the archive
ensures the Glacier object is self-describing and avoids a split-brain package
where the archive data and its integrity metadata can drift as separate objects.

## Consequences

- Object listings reveal collection ids by design.
- Recovery restores one archive object per collection.
- S3 multipart resume state is tracked for that one object.
- The old hashed-prefix, three-object archive layout is not supported.
