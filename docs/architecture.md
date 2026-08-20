# Architecture

Riverhog is an encrypted archive platform with independently packaged clients, adapters,
orchestration, extensions, utilities, and shared packages. Executable contracts define exact
interfaces, configuration, formats, and commands; this page records ownership and authority.

## Authority model

- Archive stores are the durable authority for encrypted archive bytes. A collection may
  have verified copies in multiple named stores; copy retirement and collection deletion
  are archive mutations.
- Each component's database is its operational state and schema authority. Riverhog's catalog
  records identity, placement, and workflows but is unnecessary to recover a known copy.
- Collection ingress writes server-encrypted units directly to immutable final archive keys
  in its selected store; ingress is not a storage tier. Checkpoints, unsealed membership, and
  open multipart uploads are operational state. Sealed objects and published immutable roots
  alone are archive authority. Retrieval caches hold rebuildable ciphertext.
- Payload loops exclude control-plane and reporting work. Phase-separated timing records and
  `make transfer-profile` qualify goodput against a same-path raw baseline.
- Per-file provenance is append-only custody history. Journals remain exact prefixes across
  handoffs; clients capture or continue them by default, and omissions require a reason. The
  immutable index and journals are authoritative; database rows are a rebuildable projection.
- A collection is an immutable logical namespace and deletion unit. Tags are mutable
  catalog metadata. Applications own desired state and materializations derived through
  public APIs.
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

- [`riverhog/server`](../riverhog/server/) is the archive platform service.
- [`riverhog/client`](../riverhog/client/) is the direct platform CLI. Its `local` commands
  maintain client-owned local materialization.
- [`riverhog/adapters`](../riverhog/adapters/) contains the maintained content-opaque FTP,
  TUS, and watched-drop collection producers.
- [`riverhog/recovery`](../riverhog/recovery/) provides an independently packaged,
  permissively licensed reference implementation; Riverhog archives remain recoverable
  without Riverhog using standard tools.
- [`companions/stove0`](../companions/stove0/) contains the orchestration server, its
  separately packaged client, and maintained observer/target implementations.
- [`utilities`](../utilities/) contains portable operator and event tools.
- [`packages`](../packages/) contains focused reusable libraries and protocol, client,
  configuration, event, transport, and CLI primitives.
