# Architecture

Riverhog accepts logical collections, preserves each as a verified age-encrypted package
in remote object storage, and materializes selected files into a fast hot cache.
PostgreSQL records identity, manifests, custody evidence, and current materialization;
object stores hold the bytes.

## Custody model

A collection is the deletion and recovery unit. Its files have stable relative paths,
sizes, and SHA-256 digests. A collection becomes accepted only after Riverhog verifies the
uploaded files, remote archive package, encrypted manifest, and OpenTimestamps proof.

The remote archive is the durable authority. Upload staging is temporary ingest state,
and hot storage is a replaceable materialization for browsing and direct access. A
collection is immutable while present; changing the retained set means accepting a new
collection or deliberately deleting an existing collection as a whole.

Archive object keys are opaque. Human identity comes from the catalog and encrypted
manifest rather than bucket paths. Riverhog keeps plaintext safety guidance at the
archive root without exposing collection identity.

## Retrieval

A target selector names one logical file or a projected directory prefix. A fetch records
an intended selector set. Missing selected files cause Riverhog to retrieve their
collection archives, verify encrypted and logical bytes, and publish only the requested
files into hot storage. Eviction removes hot materialization only after verified archive
coverage is established.

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
- **Fetch:** a named request to make selected logical files hot.
- **Archive restore:** provider retrieval and verified materialization of collection bytes.
- **Target selector:** the shared projected-path syntax used to find, fetch, and evict
  logical files.
