# riverhog

Riverhog is a self-hosted encrypted archive management and retrieval platform. It helps an
operator create, catalog, verify, replicate, and retrieve encrypted archives stored in
operator-controlled object-storage accounts. It accepts logical collections without
staging plaintext on its host and preserves independently restorable archive objects. This
repository centers the platform server and its official `riverhog` client; companion
applications integrate through published APIs rather than platform internals.

## Deployment scope

Riverhog is designed for one operator per deployment, not as a multi-tenant storage service.
It is provided as-is software, not a storage provider or independent custodian, and does not
guarantee preservation, availability, confidentiality, or recoverability. The operator
retains control of and responsibility for the configured storage accounts, credentials,
billing, lifecycle policies, redundant copies, archive-passphrase safekeeping, archived
data, and tested recovery. Loss of the passphrase or all valid archive copies may permanently
prevent recovery. This warning supplements, but does not replace, the warranty and liability
limitations in the applicable software license.

## Start here

Use `make help` for development and validation commands. Use each installed command's
`--help` output for its current interface. A running API publishes its current OpenAPI
document at `/openapi.json`.

The [Riverhog Compose stack](riverhog/server/compose.yaml) runs from safe development
defaults. The intentionally empty [Compose override example](.env.compose.example) is the
starting point for local overrides.

## Repository map

- [`riverhog/server`](riverhog/server/) is the archive platform service.
- [`riverhog/client`](riverhog/client/) is the direct platform CLI. Its `local` commands
  are the reference external application and maintain client-owned local materialization.
- [`riverhog/recovery`](riverhog/recovery/) is the independently packaged, permissively
  licensed reference recovery implementation.
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

## Extending Riverhog

New integrations should be independent applications over Riverhog's published HTTP and
CloudEvents contracts rather than additions to the platform server implementation. Report
suspected vulnerabilities privately through [security reporting](SECURITY.md).

## Context

- [Architecture](docs/architecture.md) explains archive and component boundaries.
- [Operator responsibilities](docs/operator-responsibilities.md) covers the human checks
  around durable storage, recovery, and deletion.
- [Recovery without Riverhog](docs/recovery-without-riverhog.md) is the independent archive
  recovery path.
- [Licensing](LICENSE.md) defines the repository's release terms.

Release-level reference documentation belongs to tagged releases. The documentation on
`main` is intentionally limited to current context that cannot be recovered quickly from
the executable contracts.
