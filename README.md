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
billing, lifecycle policies, redundant copies, archive passphrase-map safekeeping, archived
data, and tested recovery. Loss of the passphrase or all valid archive copies may permanently
prevent recovery. This warning supplements, but does not replace, the warranty and liability
limitations in the applicable software license.

## Contributions

New integrations should be independent applications over Riverhog's published HTTP and
CloudEvents contracts rather than additions to the platform server implementation. Report
suspected vulnerabilities privately through [security reporting](SECURITY.md).

## Start here

Use `make help` for development and validation commands. Use each installed command's
`--help` output for its current interface. A running API publishes its current OpenAPI
document at `/openapi.json`.

The [Riverhog Compose stack](riverhog/server/compose.yaml) accepts deployment-owned object
storage. Its `development` profile provides a disposable local Garage store for the checked-in
development and test rails.

## Context

- [Architecture](docs/architecture.md) explains authority, component boundaries, and the
  repository layout.
- [Operator responsibilities](docs/operator-responsibilities.md) covers the human checks
  around durable storage, recovery, and deletion.
- [Provider qualification](docs/how-to/provider-qualification.md) defines the disposable B2,
  Deep Archive, and CloudFront release test and its operator configuration.
- [Licensing](LICENSE.md) defines the repository's release terms.

Release-level reference documentation belongs to tagged releases. The documentation on
`main` is intentionally limited to current context that cannot be recovered quickly from
the executable contracts.
