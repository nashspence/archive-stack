# ADR-0003: Use Slug-Based Server-Minted Collection IDs

## Decision

Riverhog accepts a human-readable collection slug at upload creation time and
mints the canonical collection id on the server.

The minted id includes a UTC timestamp and normalized slug, for example:

```text
2026/20260524T190233Z__mom-iphone-photos
```

By default, Riverhog uses the current server UTC upload time. Migration uploads
may provide an explicit timestamp in UTC basic form `YYYYMMDDTHHMMSSZ` to
preserve an original archival timestamp. The slug remains required either way.

Clients use the returned id for later upload status, file upload, collection,
planning, copy, and recovery APIs. There is no supported client-supplied
collection-id path for collection upload creation.

## Reason

Archive identity must not depend on the server's local filesystem paths, and the
operator should not have to manually invent globally unique ids for ordinary
uploads.

Server-minted timestamped ids preserve chronological browsing, keep year
projection simple for WebDAV, and still include the operator's slug for human
recognition. Allowing a validated timestamp override lets older archival sets
migrate without losing their original date identity while still avoiding manual
collection ids.

Resume behavior is based on the normalized slug plus the complete file manifest:
the same pair resumes an unfinished upload or returns the already-finalized
collection, while the same slug with different contents becomes a distinct
timestamped collection.
