# ADR-0041: Use Postgres Sidecar as Authoritative Catalog

## Decision

Riverhog uses a Compose-managed Postgres sidecar as the durable authoritative catalog.

## Reason

The app and workers share catalog state through a normal database service,
avoiding SQLite file sharing as the runtime stack grows more concurrent and
sidecar-oriented.
