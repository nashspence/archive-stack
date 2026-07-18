# Architecture

Riverhog accepts logical collections and preserves each as independently encrypted objects
in one or more named archive stores. PostgreSQL records immutable logical identity, object
placement, archive copies, external catalog events, retrieval work, and cache leases. Object
stores hold encrypted bytes.

## Ingress and custody

A collection is the deletion unit. Its immutable name contains its second-precision creation
time and slug. Each file has an immutable relative path, size, SHA-256 digest, and optional
portable metadata supplied by the client.

Authenticated preflight creates a random per-file ingress secret and returns it to that
client once as part of the upload descriptor. The secret is envelope-encrypted in the
catalog. Clients stream independently framed age ciphertext through TUS into the configured
ingress object store; TUS metadata carries only an opaque upload identifier. Riverhog reads
and decrypts bounded ranges while constructing the permanent archive, so its host never
needs space for a complete file or collection. It removes ingress objects only after the
archive copy is complete and verified.

Each archive copy contains:

- packs containing files smaller than 16 MiB, with at most 32 MiB of file payload per pack;
- one object per file from 16 MiB through the maximum plaintext object size;
- sequential objects for larger files;
- a portable manifest mapping every logical byte to its object or pack member;
- a proof binding that manifest.

Every object is independently age encrypted and checksummed. No encrypted object exceeds
32 GiB. The manifest and proof remain immediately readable even when data objects use an
archive class that requires provider-side retrieval. Archive object keys are opaque, and
plaintext archive-root guidance contains no collection identity.

Archive stores are the durable authority. A collection's archive-copy set can change only
through guarded copy and retirement operations. An archive copy reads and verifies the
source object set, writes and verifies an equivalent destination set, and records the copy
only after completion.

## External applications and retrieval

Riverhog publishes portable collection manifests and current collection changes through a
narrow ResourceSync profile. External applications keep their own desired state and data;
Riverhog does not track whether an application has materialized a file.

An application submits exact immutable file references from a retrieval plan. Riverhog
chooses a complete archive copy and creates an application-owned retrieval job. Immediately
readable objects are served from their archive store. For a store whose provider requires
retrieval preparation, Riverhog performs that work asynchronously and copies the existing
archive ciphertext into a separate retrieval-cache store. Application leases bound cache
retention, and acknowledgment releases the job's leases. The initial archive copy retains
the same ciphertext in that cache for a configurable 30-day lease when the write store
requires retrieval preparation.

The content endpoint reconstructs one logical file, supports validators and byte ranges,
and verifies archive and file checksums. Fishbox is the reference external application: its
CLI owns a local directory and SQLite catalog, follows ResourceSync changes, obtains
retrieval jobs, verifies downloads, and atomically publishes local files. Other applications
use the same interface and remain isolated from Fishbox state.

## Component boundaries

- Riverhog owns custody, search, portable catalog publication, retrieval preparation, and
  verified logical-file delivery.
- Fishbox owns one local materialization, including its directory, SQLite state, selection,
  repair, audit, and eviction.
- Munchy owns media discovery, routing, transformation, metadata projection, and assembly
  before handing completed artifacts to a named destination adapter.
- Jeb owns source enrollment and credentials, transport-neutral landing, watched-drop
  scheduling, and named target submission.
- Gogurt maps mounted-volume markers to configured operator actions.
- Downstream private configuration owns real identity, destinations, recipients, remotes,
  application tokens, and deployment topology.

## Core terms

- **Collection:** the immutable logical namespace and deletion unit.
- **Archive object:** one independently encrypted pack, file, segment, manifest, or proof.
- **Archive store:** a named durable object-store destination with defined read behavior.
- **Archive copy:** one verified object set for a collection in one archive store.
- **Ingress store:** temporary encrypted upload storage.
- **Retrieval cache:** leased encrypted storage used only for archive objects whose provider
  cannot make them immediately readable.
- **Retrieval plan:** a stable resolution of logical files to one complete archive copy.
- **Retrieval job:** application-owned preparation and lease state for one plan.
- **Handoff:** Munchy's delivery of completed artifacts through one named destination
  adapter.
