# ADR-0006: Use SQLite as Authoritative Catalog

Status: Superseded by ADR-0041.

## Decision

Riverhog uses SQLite as the durable authoritative catalog.

## Reason

The MVP needs restart-safe archive state without introducing a separate database service.
