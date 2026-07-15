# Architecture

Riverhog accepts logical collections, preserves each as a verified age-encrypted package
in a named archive store, and can materialize complete collections into a fast hot cache.
A collection may have verified copies in multiple archive stores. PostgreSQL records
identity, manifests, custody evidence, copies, and current materialization; object stores
hold the bytes.

## Custody model

A collection is the deletion and recovery unit. Its files have stable relative paths,
sizes, and SHA-256 digests. A collection becomes accepted only after Riverhog verifies an
archive copy's package, encrypted manifest, and OpenTimestamps proof. Uploads target the
configured default archive store and retain a hot materialization unless the caller
explicitly selects another store or archive-only storage.

Archive stores are the durable authority. Upload staging is temporary ingest state, and
hot storage is a replaceable materialization for browsing and direct access. A collection
is immutable while present; changing the retained set means accepting a new collection or
deliberately deleting an existing collection as a whole.

An archive copy job reads and verifies an existing copy, streams the package through
decryption and destination encryption, verifies the destination, and records the new copy.
It does not materialize collection files in hot storage. Source reads are prepared only
when the selected store requires it, and interrupted jobs are requeued from durable
catalog state.

Archive object keys are opaque. Human identity comes from the catalog and encrypted
manifest rather than bucket paths. Riverhog keeps plaintext safety guidance at the
archive root without exposing collection identity.

## Retrieval

A fetch records an exact collection set. If any file in a fetched collection is missing
from hot storage, Riverhog retrieves and verifies its encrypted archive and materializes
the complete collection. Eviction likewise removes complete collections from hot storage.
Before eviction, Riverhog confirms that at least one recorded archive copy is still
present in its store and matches its recorded checksum.

## Component boundaries

- Riverhog owns custody, search, retrieval, and hot-cache state.
- Munchy owns media discovery, routing, transformation, metadata projection, and assembly
  before custody.
- Jeb owns watched-drop scheduling and submission to Munchy.
- Gogurt maps mounted-volume markers to configured operator actions.
- Downstream private configuration owns real devices, accounts, destinations, recipients,
  remotes, credentials, and deployment topology.

## Core terms

- **Collection:** the logical deletion and recovery unit.
- **Collection archive:** the canonical package, manifest, and proof for a collection.
- **Archive store:** a named remote object-store destination with its own access and read
  behavior.
- **Archive copy:** a verified encrypted instance of a collection archive in one store.
- **Hot storage:** the replaceable materialized cache of verified logical files.
- **Fetch:** a named request to make exact collections hot.
- **Restore:** verified whole-collection materialization into hot storage, including remote
  read preparation when the selected archive store requires it.
