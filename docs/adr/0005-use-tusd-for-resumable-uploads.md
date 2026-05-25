# ADR-0005: Use tusd for Resumable Uploads

## Decision

Riverhog delegates resumable byte transport to `tusd` while retaining ownership of archive state.

Public clients still upload through Riverhog-managed tus-compatible URLs. The
CLI sends bounded request chunks and paces socket writes before Riverhog
forwards accepted chunks to the internal `tusd` service.

## Reason

The system needs resumable upload mechanics without making the upload transport authoritative.
Riverhog also needs client-side transport behavior that remains stable on real
LAN and Wi-Fi paths where aggressive bulk writes can stall below HTTP before
nginx, Riverhog, or `tusd` receives a complete request body.
