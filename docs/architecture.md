# Architecture

Executable contracts define packages; architecture records ownership and placement.

## Authority model

- **Archive authority.** Archive stores own encrypted bytes. Only sealed objects and published
  immutable roots are archive authority; retrieval caches are rebuildable. Retirement and deletion
  mutate archives. Known archives remain recoverable with standard tools and without Riverhog's
  database.
- **Trust boundary.** Riverhog's host is the plaintext/encryption boundary. Ingress encrypts there
  before writing immutable final-object units to the selected archive store; ingress is not a
  storage tier. Collections freeze their encryption format and opaque key identity before ingress;
  configuration owns keys.
- **Operational state.** Databases own operational state. Riverhog's catalog records identity,
  placement, and workflows. Checkpoints, unsealed membership, and open resumable writes are not
  archive authority.
- **Provenance authority.** Per-file provenance is append-only custody history. Journals remain
  exact prefixes across handoffs; clients capture or continue them by default, and omissions require
  a reason. Journals and their index are authoritative; database rows are a rebuildable projection.
- **Collection views.** Collections are immutable namespaces and deletion units; tags are mutable.
  Applications own derived materializations.
- **Deployment configuration.** Deployments own identity, credentials, destinations,
  topology, and private policy.

## Boundary model

- **Implementation ownership.** Products own implementations. Packages contain product contracts or
  implementation-neutral tooling; reference contracts and support stay with their family.
- **Public contracts.** Runtime integration crosses published HTTP and CloudEvents contracts.
  Shared models own identity-bearing documents. Exact HTTP/OpenAPI contracts own ordinary CRUD;
  official clients may expose their JSON.
- **Riverhog platform.** Server owns archive construction, publication, copies, retrieval, and
  verified delivery. The client owns only local materializations.
- **Ingress adapters.** Ingress adapters accept external protocols as content-opaque, bounded-custody
  Riverhog clients; they relinquish bytes only after finalization.
- **Storage adapters.** Storage adapters translate Riverhog's opaque-object capabilities into
  provider mechanisms. Provider policy and mechanisms remain outside Riverhog.
- **Companions.** Companions consume Riverhog capabilities and own their workflow state. Stove0 is
  a companion; its core does not interpret content.
- **Extensions.** Observers report immutable-artifact facts; targets perform declared operations.
  Each explicitly selected distribution owns one capability; shared-dependency image bundles do
  not merge identities or selection. First-party references are optional and nonnormative: their
  inventory is neither complete nor recommended and defines no support matrix.
- **Transfer path.** Payload loops exclude control-plane status and reporting work.

Workspace checks enforce ownership boundaries.

## Repository map

- [`riverhog/server`](../riverhog/server/): archive service.
- [`riverhog/client`](../riverhog/client/): official client.
- [`riverhog/recovery`](../riverhog/recovery/): permissively licensed reference for recovery.
- [`reference/gogurt`](../reference/gogurt/): Gogurt references.
- [`reference/riverhog`](../reference/riverhog/): Riverhog references.
- [`companions`](../companions/): independent Riverhog applications.
- [`reference/stove0`](../reference/stove0/): Stove0 references.
- [`utilities`](../utilities/): operator and event utilities.
- [`packages`](../packages/): product-owned contracts and implementation-neutral support.
