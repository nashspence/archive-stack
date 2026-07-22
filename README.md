# riverhog

Riverhog is a custody platform for encrypted remote collection archives. It accepts
logical collections without staging plaintext on its host, preserves independently
restorable archive objects, and gives applications a stable catalog and retrieval
interface. This repository centers the platform server and its official `riverhog` client;
companion applications integrate through published APIs rather than platform internals.

## Custody

Configured archive stores are the durable storage authority. Their encrypted collection
objects are the durable copies Riverhog relies on. Protect account recovery, credentials,
billing, bucket access, and tested retrieval for every store. Delete a collection only
through Riverhog's guarded exact-id operation after accepting the loss.

## Start here

Use `make help` for development and validation commands. Use each installed command's
`--help` output for its current interface. A running API publishes its current OpenAPI
document at `/openapi.json`.

The [Riverhog Compose stack](riverhog/server/compose.yaml) runs from safe development
defaults. The intentionally empty [Compose override example](.env.compose.example) is the
starting point for local overrides. Owner-scoped examples contain fake identities only:
[Munchy](companions/munchy/config/examples/), [Jeb](companions/jeb/server/config/),
[Mango Fish](utilities/mango-fish/config/), and [Gogurt](utilities/gogurt/config/examples/).

## Repository map

- [`riverhog/server`](riverhog/server/) is the custody platform service.
- [`riverhog/client`](riverhog/client/) is the direct platform CLI. Its `local` commands
  are the reference external application and maintain client-owned local materialization.
- [`companions`](companions/) contains applications with first-class ecosystem adapters:
  Munchy for media workflows and Jeb for transport-neutral watched drops. Each has an
  independently packaged `server` and `client`.
- [`companions/munchy/server/targets`](companions/munchy/server/targets/) contains
  server-owned execution targets. The NVIDIA AV1 target has a separate image so it can be
  placed only on compatible hosts; it is not a standalone companion application.
- [`utilities`](utilities/) contains Riverhog-agnostic tools: Mango Fish relays lifecycle
  events and Gogurt maps mounted-volume markers to configured actions.
- [`packages`](packages/) contains reusable libraries and protocol, API-client,
  configuration, event, transport, and CLI primitives.

## Context

- [Architecture](docs/architecture.md) explains custody and component boundaries.
- [Archive operations](docs/archive-operations.md) covers the human checks around
  durable storage, recovery, and deletion.

Release-level reference documentation belongs to tagged releases. The documentation on
`main` is intentionally limited to current context that cannot be recovered quickly from
the executable contracts.
