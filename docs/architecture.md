# Architecture

Executable contracts define packages; this page records authority, ownership, and placement.

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
- **Deployment configuration.** Downstream configuration owns identity, credentials, destinations,
  topology, and private policy.

## Boundary model

- **Implementation ownership.** Products own implementations. Focused packages may share contracts
  or behavior; implementations do not cross product boundaries.
- **Public contracts.** Runtime integration crosses published HTTP and CloudEvents contracts.
  Shared models own identity-bearing documents. Exact HTTP/OpenAPI contracts own ordinary CRUD;
  official clients may expose their JSON.
- **Riverhog platform.** Server owns archive construction, publication, copies, retrieval, and
  verified file delivery. The client is an API consumer owning only local
  materializations.
- **Ingress adapters.** Ingress adapters accept external protocols as content-opaque, bounded-custody
  Riverhog clients; they relinquish bytes only after finalization.
- **Storage adapters.** Storage adapters translate Riverhog's opaque-object capabilities into
  provider mechanisms. Provider policy and mechanisms remain outside Riverhog.
- **Companions.** Companions consume Riverhog capabilities and own their workflow state. Stove0 is
  a provided transformation companion; its core does not interpret content.
- **Extensions.** Content observers report bounded facts about exact immutable artifacts. Transform
  targets perform one operation through an exact derived-collection capability. Native mechanisms
  remain behind focused platform boundaries.
- **Transfer path.** Payload loops exclude control-plane status and reporting work.

Workspace checks enforce ownership boundaries.

## Repository map

- [`riverhog/server`](../riverhog/server/): archive service.
- [`riverhog/client`](../riverhog/client/): official client.
- [`riverhog/recovery`](../riverhog/recovery/): permissively licensed reference for recovery.
- [`reference/riverhog/ingress`](../reference/riverhog/ingress/): maintained ingress adapters.
- [`reference/riverhog/storage`](../reference/riverhog/storage/): maintained storage adapters.
- [`companions`](../companions/): independent Riverhog applications.
- [`reference/stove0`](../reference/stove0/): maintained Stove0 components.
- [`utilities`](../utilities/): operator and event utilities.
- [`packages`](../packages/): shared contracts and implementation-neutral support.
