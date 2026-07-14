# ADR-0003: Use Slug-Based Server-Minted Collection IDs

## Decision

Riverhog accepts a human-readable collection slug and mints the canonical collection id on the server. The id combines a UTC collection timestamp with the normalized slug:

```text
20260524T190233Z__family-photos
```

Clients may provide the collection timestamp when it is part of the source's archival identity. Otherwise Riverhog uses the current server time. The returned id is the canonical identifier for upload status, catalog, archive, search, and fetch operations.

## Reason

Archive identity must not depend on local filesystem paths, and ordinary uploads should not require operators to invent globally unique ids. Timestamped slugs preserve chronological browsing and readable identity while keeping id construction deterministic and validated.
