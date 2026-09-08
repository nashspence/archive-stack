# Architecture

Contracts define packages; architecture records ownership.

## Authority model

- **Archive authority.** Archive stores own encrypted bytes. Only sealed objects and published
  immutable roots are archive authority; retrieval caches are rebuildable. Retirement and deletion
  mutate archives; archives remain recoverable with standard tools without Riverhog's database.
- **Trust boundary.** Riverhog's host is the plaintext/encryption boundary. Ingress encrypts there
  before writing immutable final-object units to the selected archive store; ingress is not a
  storage tier.
  Collections freeze their encryption format and opaque key identity; configuration owns keys.
- **Operational state.** Database state records identity, placement, and workflows.
  Checkpoints, unsealed membership, and open resumable writes are not archive authority.
- **Provenance authority.** Per-file provenance is append-only custody history. Journals remain
  exact prefixes across handoffs; clients capture or continue them by default, and omissions require
  a reason. Journals and their index are authoritative; database rows are a rebuildable projection.
- **Collection views.** Collections are immutable namespaces/deletion units. Mutable descriptions
  and classification-tag sets are copy-adjacent recovery material projected into the catalog;
  tag membership may label authorization; applications own richer indexes.
- **Deployment configuration.** Deployments own credentials, topology, and policies.

## Boundary model

- **Implementation ownership.** Products own implementations. Packages contain product contracts or
  implementation-neutral tooling; reference contracts and support stay with their family.
  Non-reference release units do not depend on references; reference images, qualification, and
  tests compose them explicitly.
- **Public contracts.** Published HTTP and CloudEvents contracts define integration. Shared models
  own identities; HTTP/OpenAPI owns CRUD and official client JSON. The generated freeze inventories
  external extents.
- **Riverhog platform.** Server owns sealed membership, canonical content identity, archive
  construction, copies, retrieval, and verified delivery; clients own materializations.
- **Ingress adapters.** Ingress adapters are content-opaque, bounded-custody Riverhog clients; they
  relinquish bytes only after finalization.
- **Storage adapters.** Storage adapters translate opaque-object capabilities into
  provider mechanisms. Provider policy and mechanisms remain outside Riverhog.
- **Companions.** Companions use Riverhog capabilities and own workflow state. Stove0 is one; its
  core does not interpret content.
- **Extensions.** Observers report immutable-artifact facts; targets perform declared operations.
  Each selected distribution owns one capability; shared-dependency image bundles preserve separate
  identities and selection. Names identify families; only exact digest-bound contracts or selected
  bindings carry authority.
- **Transfer path.** Payload loops exclude control-plane status and reporting work.

Workspace checks enforce ownership boundaries.

## Repository map

- [`riverhog/server`](../riverhog/server/): archive service.
- [`riverhog/client`](../riverhog/client/): client.
- [`riverhog/recovery`](../riverhog/recovery/): permissively licensed independent recovery tool.
- [`reference/gogurt`](../reference/gogurt/): Gogurt references.
- [`reference/riverhog`](../reference/riverhog/): Riverhog references.
- [`companions`](../companions/): independent Riverhog applications.
- [`reference/stove0`](../reference/stove0/): Stove0 references.
- [`utilities`](../utilities/): operator and event utilities.
- [`packages`](../packages/): product-owned contracts and implementation-neutral support.
