# riverhog

Riverhog is a generic custody system for encrypted remote collection archives. It accepts
logical collections without staging plaintext on its host, preserves independently
restorable archive objects, and gives external applications a stable catalog and retrieval
interface. Fishbox is the reference local materializer.

## Custody

Configured archive stores are the durable storage authority. Their encrypted collection
objects are the durable copies Riverhog relies on. Protect account recovery, credentials,
billing, bucket access, and tested retrieval for every store. Delete a collection only
through Riverhog's guarded exact-id operation after accepting the loss.

## Start here

Use `make help` for development and validation commands. Use each installed command's
`--help` output for its current interface. A running API publishes its current OpenAPI
document at `/openapi.json`.

The checked Compose stack runs from safe development defaults. The intentionally empty
[Compose override example](.env.compose.example) is the starting point for local overrides;
[example configurations](config/examples/) contain fake identities only.

## Context

- [Architecture](docs/architecture.md) explains custody and component boundaries.
- [Archive operations](docs/archive-operations.md) covers the human checks around
  durable storage, recovery, and deletion.

Release-level reference documentation belongs to tagged releases. The documentation on
`main` is intentionally limited to current context that cannot be recovered quickly from
the executable contracts.
