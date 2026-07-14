# ADR-0005: Use tusd for Resumable Uploads

## Decision

Riverhog delegates resumable byte transport to `tusd` while retaining ownership of archive state.

Public clients still upload through Riverhog-managed tus-compatible URLs. The
CLI sends bounded request chunks and paces socket writes before Riverhog
forwards accepted chunks to the internal `tusd` service.

`tusd` writes accepted upload bytes to a local filesystem directory mounted into
both `tusd` and `riverhog-app`. Riverhog reads from that shared staging
directory when it builds the encrypted archive and when it promotes
finalized files into committed hot storage. Garage is not part of the
per-chunk upload path.

## Reason

The system needs resumable upload mechanics without making the upload transport authoritative.
Riverhog also needs client-side transport behavior that remains stable on real
LAN and Wi-Fi paths where aggressive bulk writes can stall below HTTP before
nginx, Riverhog, or `tusd` receives a complete request body.

Local filesystem staging keeps the highest-volume ingest path append-friendly
and avoids object-store operation overhead for every resumable chunk. The staged
directory is temporary custody only; the collection becomes safely archived only
after archive-compatible storage verifies the deterministic archive package.
