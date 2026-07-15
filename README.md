# riverhog

Riverhog is a generic custody system for encrypted remote collection archives and a
materialized hot cache. Munchy prepares media, Jeb collects watched drops, and Gogurt
connects mounted volumes to configured operator actions.

## Custody

The remote archive account is the durable storage authority. Its encrypted collection
objects are the sole durable copies Riverhog relies on. Protect account recovery,
credentials, billing, bucket access, and tested retrieval. Delete a collection only
through Riverhog's guarded exact-id operation after accepting the loss.

## Start here

Use `make help` for development and validation commands. Use each installed command's
`--help` output for its current interface. A running API publishes its current OpenAPI
document at `/openapi.json`.

The checked [Compose environment](.env.compose.example) and
[example configurations](config/examples/) are fake, executable starting points.

## Context

- [Architecture](docs/architecture.md) explains custody and component boundaries.
- [Archive operations](docs/archive-operations.md) covers the human checks around
  durable storage, recovery, and deletion.

Release-level reference documentation belongs to tagged releases. The documentation on
`main` is intentionally limited to current context that cannot be recovered quickly from
the executable contracts.
