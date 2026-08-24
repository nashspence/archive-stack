# Architecture

Riverhog is an encrypted archive platform with independently packaged components. Executable
contracts define interfaces, configuration, formats, and commands; this page records authority.

## Authority model

- Archive stores own durable encrypted bytes. Collections may have verified named-store copies;
  retirement and deletion are archive mutations.
- Component databases own operational state. Riverhog's catalog records identity, placement,
  and workflows but is unnecessary to recover a known copy.
- Collection ingress writes server-encrypted units directly to immutable final archive keys in
  its selected store; ingress is not a storage tier. Checkpoints, unsealed membership, and
  multipart uploads are operational state. Sealed objects and published immutable roots alone
  are archive authority; retrieval caches are rebuildable.
- Riverhog's host is the trusted plaintext and encryption boundary. Each collection freezes
  v1 format and opaque passphrase ID before ingress. Configuration holds secrets; plaintext
  `recovery.json` selects the exact ID for independent recovery.
- Payload loops exclude control-plane and reporting work. Phase-separated timing records and
  `make transfer-profile` qualify goodput against a same-path raw baseline.
- Per-file provenance is append-only custody history. Journals remain exact prefixes across
  handoffs; clients capture or continue them by default, and omissions require a reason.
  Immutable journals and their index are authoritative; database rows are a rebuildable projection.
- Collections are immutable namespaces and deletion units; tags are mutable. Applications own
  API-derived desired state and materializations.
- Downstream configuration owns deployment identity, credentials, destinations, recipients,
  and topology.

## Boundary model

Each product owns its implementation. Focused packages may be shared; implementation modules
may not. Runtime integration crosses published HTTP and CloudEvents contracts.

- The Riverhog server owns archive construction, catalog publication, copies, retrieval, and
  verified logical-file delivery.
- The official Riverhog client is an ordinary API consumer. Its `local` commands own one
  local materialization and its rebuildable views.
- Protocol adapters are content-opaque producers with bounded custody. They relinquish bytes
  only after Riverhog returns the finalized receipt.
- stove0 is the sole transformation workflow authority and stores only control state. Its core
  consumes identities, observations, plans, and outcomes without interpreting content.
- Content observers report bounded facts about exact immutable artifacts. Transform targets
  perform one declared operation and publish through one exact derived-collection capability.
- End-user clients and utilities support Linux, macOS, and Windows. Server applications ship
  as Linux OCI images; hardware-bound execution targets may declare a narrower platform.
  Downstream configuration supplies private policy and deployment identity.

Workspace dependency and import checks enforce the implementation-owner boundaries.

## Repository map

- [`riverhog/server`](../riverhog/server/) is the archive service.
- [`riverhog/client`](../riverhog/client/) is the direct platform CLI. Its `local` commands
  maintain client-owned local materialization.
- [`reference/riverhog/ingress`](../reference/riverhog/ingress/) contains maintained
  content-opaque protocol-ingress implementations; v1 includes the FTP adapter.
- [`reference/riverhog/storage`](../reference/riverhog/storage/) contains isolated
  implementations of the provider-neutral storage-adapter contract.
- [`riverhog/recovery`](../riverhog/recovery/) is a permissively licensed reference;
  archives remain recoverable with standard tools.
- [`companions`](../companions/) contains content-opaque applications.
- [`reference/stove0`](../reference/stove0/) contains first-party implementations
  of independently implementable Stove0 observer, target, and target-internal sampler contracts.
- [`utilities`](../utilities/) contains portable operator and event tools.
- [`packages`](../packages/) contains reusable protocol, client,
  configuration, event, transport, and CLI primitives.
