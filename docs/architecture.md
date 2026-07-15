# Architecture

Riverhog accepts logical collections, preserves each as independently encrypted objects
in one or more named archive stores, and materializes selected files into a fast hot cache.
PostgreSQL records identity, object placement, archive copies, and current materialization;
object stores hold the bytes.

## Custody model

A collection is the deletion unit. Its files have stable relative paths, sizes, and
SHA-256 digests. Riverhog accepts a collection only after every required archive object,
the encrypted manifest, and its OpenTimestamps proof are uploaded and verified. Uploads
target the configured default archive store and retain hot files unless the caller
explicitly requests archive-only storage.

Archive stores are the durable authority. Upload staging is temporary ingest state, and
hot storage is a replaceable materialization for browsing and direct access. A collection's
logical contents are immutable while present; changing them means accepting a new collection
or deliberately deleting the existing collection. Its archive-copy set can change through
guarded copy and retirement operations.

Each archive copy contains:

- packs containing files smaller than 16 MiB, with at most 32 MiB of file payload per pack;
- one object per file from 16 MiB through the store's maximum plaintext size;
- sequential objects for larger files;
- a manifest mapping every logical file byte to its object or pack member;
- a proof binding that manifest.

Every stored object is independently age encrypted and checksummed. No encrypted object may
exceed 32 GiB; Riverhog derives the plaintext segment ceiling from the exact age framing
overhead. The manifest and proof are independently readable standard-class objects. Archive
object keys are opaque, and plaintext archive-root guidance contains no collection identity.

An archive copy job reads and verifies every source object, writes and verifies an equivalent
destination object set, then records the new copy. It does not materialize files in hot
storage. Source reads are prepared only when the selected store requires retrieval.

## Retrieval

A fetch records an exact set of logical files. Collection arguments are a convenience that
select every file in each named collection. Riverhog prepares and reads only the data objects
mapped to missing selected files, verifies the manifest, proof, object checksums, and file
checksums, then materializes those files. A shared small-file pack is fetched once when any
of its members is selected.

Eviction accepts the same collection-or-file selection boundary. Before removing a hot file,
Riverhog verifies a recorded archive copy containing its required data objects, manifest,
and proof.

## Component boundaries

- Riverhog owns custody, search, retrieval, and hot-cache state.
- Munchy owns media discovery, routing, transformation, metadata projection, and assembly
  before custody.
- Jeb owns watched-drop scheduling and submission to Munchy.
- Gogurt maps mounted-volume markers to configured operator actions.
- Downstream private configuration owns real devices, accounts, destinations, recipients,
  remotes, credentials, and deployment topology.

## Core terms

- **Collection:** the logical deletion unit and namespace for archived files.
- **Archive object:** one independently encrypted pack, file, segment, manifest, or proof.
- **Archive store:** a named remote object-store destination with its own access and read
  behavior.
- **Archive copy:** one verified object set for a collection in one archive store.
- **Hot storage:** the replaceable materialized cache of verified logical files.
- **Fetch:** a named selection of files to keep materialized in hot storage.
- **Restore:** retrieval and verification of the archive objects needed to materialize
  selected files.
