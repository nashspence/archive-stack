# Architecture

Riverhog is an encrypted archive platform surrounded by independently packaged clients,
companions, execution targets, utilities, and focused shared packages. Exact public
interfaces, configuration shapes, archive-format details, and command behavior live in
executable contracts; this document records only the ownership and authority model.

## Authority model

- Archive stores are the durable authority for encrypted archive bytes. A collection may
  have verified copies in multiple named stores; copy retirement and collection deletion
  are archive mutations.
- A component's relational database is its durable operational state and schema authority.
  Riverhog's catalog records logical identity, placement, and workflows, but is not required
  to recover a known archive copy.
- Collection ingress writes server-encrypted units directly to immutable final archive keys
  in its selected archive store; it is not a storage tier. Planner checkpoints, unsealed
  membership, and open multipart uploads are operational state. Sealed archive objects and
  published immutable collection roots alone are archive authority. Retrieval caches hold
  exact ciphertext rebuildable from archive stores.
- Payload transfer loops exclude control-plane refresh and reporting work. Phase-separated,
  identity-safe timing records and the `make transfer-profile` harness qualify transfer
  goodput against a same-path raw baseline.
- Per-file provenance is append-only custody history. Existing journals remain exact prefixes
  across in-repo handoffs; clients capture or continue them by default, and every omission has
  an explicit reason. The immutable collection index and journal bundles are authoritative;
  relational provenance rows are a rebuildable query projection.
- A collection is an immutable logical namespace and deletion unit. Tags are mutable
  catalog metadata. Applications own desired state and materializations derived through
  public APIs.
- Downstream configuration owns deployment identity, credentials, destinations, recipients,
  and topology.

## Boundary model

Each server, client, companion, execution target, and utility owns its implementation.
Focused packages may be shared; implementation modules may not. Runtime integration crosses
published HTTP and CloudEvents contracts.

- The Riverhog server owns archive construction, catalog publication, archive-copy
  management, retrieval preparation, and verified logical-file delivery.
- The official Riverhog client is an ordinary API consumer. Its `local` commands own one
  local materialization and its rebuildable views.
- A companion server owns its workflows and adapters; its separately packaged client uses
  the public API. A Munchy execution target implements a server-owned target contract.
- End-user clients and utilities support Linux, macOS, and Windows. Server applications ship
  as Linux OCI images; hardware-bound execution targets may declare a narrower platform.
  Downstream configuration supplies private policy and deployment identity.

Workspace dependency and import checks enforce the implementation-owner boundaries.

## Repository map

- [`riverhog/server`](../riverhog/server/) is the archive platform service.
- [`riverhog/client`](../riverhog/client/) is the direct platform CLI. Its `local` commands
  maintain client-owned local materialization.
- [`riverhog/recovery`](../riverhog/recovery/) provides an independently packaged,
  permissively licensed reference implementation; Riverhog archives remain recoverable
  without Riverhog using standard tools.
- [`companions`](../companions/) contains Munchy and Jeb. Each has an independently
  packaged server and client.
- [`companions/munchy/server/targets`](../companions/munchy/server/targets/) contains
  server-owned execution targets, not standalone companion applications.
- [`utilities`](../utilities/) contains portable operator and event tools.
- [`packages`](../packages/) contains focused reusable libraries and protocol, client,
  configuration, event, transport, and CLI primitives.
