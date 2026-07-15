# Architecture

Riverhog accepts logical collections, preserves each as a verified age-encrypted package
in remote object storage, and can materialize complete collections into a fast hot cache.
PostgreSQL records identity, manifests, custody evidence, and current materialization;
object stores hold the bytes.

## Custody model

A collection is the deletion and recovery unit. Its files have stable relative paths,
sizes, and SHA-256 digests. A collection becomes accepted only after Riverhog verifies the
remote archive package, encrypted manifest, and OpenTimestamps proof. Uploads retain a
hot materialization unless the caller explicitly chooses archive-only storage.

The remote archive is the durable authority. Upload staging is temporary ingest state,
and hot storage is a replaceable materialization for browsing and direct access. A
collection is immutable while present; changing the retained set means accepting a new
collection or deliberately deleting an existing collection as a whole.

Archive object keys are opaque. Human identity comes from the catalog and encrypted
manifest rather than bucket paths. Riverhog keeps plaintext safety guidance at the
archive root without exposing collection identity.

## Retrieval

A fetch records an exact collection set. If any file in a fetched collection is missing
from hot storage, Riverhog retrieves and verifies its encrypted archive and materializes
the complete collection. Eviction likewise removes complete collections from hot storage.
Before eviction, Riverhog confirms that each collection's encrypted archive is still
present in remote storage and matches its recorded checksum.

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
- **Collection archive:** the verified encrypted remote package and its recovery evidence.
- **Hot storage:** the replaceable materialized cache of verified logical files.
- **Fetch:** a named request to make exact collections hot.
- **Archive restore:** provider retrieval and verified materialization of collection bytes.
