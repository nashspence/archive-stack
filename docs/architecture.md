# Architecture

Riverhog accepts logical collections and preserves each as independently encrypted objects
in one or more named archive stores. PostgreSQL records immutable logical identity, object
placement, archive copies, external catalog events, retrieval work, and cache leases. Object
stores hold encrypted bytes.

## Ingress and archiving

A collection is the deletion unit. PostgreSQL assigns its immutable positive-integer id when
an idempotent upload session is opened. Existing tags may be assigned at creation and changed
later without changing identity. Each file has an immutable relative path, size, and SHA-256
digest.

Authenticated preflight creates a random per-file ingress secret and returns it to that
client once as part of the upload descriptor. The secret is envelope-encrypted in the
catalog. Clients stream independently framed age ciphertext through TUS into the configured
ingress staging store; TUS metadata carries only an opaque upload identifier. Riverhog reads
and decrypts bounded ranges while constructing the permanent archive, so its host never
needs space for a complete file or collection. It removes ingress objects only after the
archive copy is complete and verified.

Each archive copy contains:

- packs containing files smaller than 16 MiB, with at most 32 MiB of file payload per pack;
- one object per file from 16 MiB through the maximum plaintext object size;
- sequential objects for larger files;
- a portable manifest mapping every logical byte to its object or pack member;
- a proof binding that manifest.

Each copy also has a mutable, independently age-encrypted `metadata.yml.age`. Its current
revision records collection-level metadata such as tags. A database outbox coalesces changes
and republishes this small recovery-discovery record without rewriting the immutable data
objects.

Every object is independently age encrypted and checksummed. No encrypted object exceeds
32 GiB. The manifest and proof remain immediately readable even when data objects use an
archive class that requires provider-side retrieval. Archive object keys are opaque, and
plaintext archive-root guidance contains no collection identity.

Riverhog periodically asks the OpenTimestamps calendars named by pending proofs for their
Bitcoin attestations. It replaces an encrypted proof only after binding the result to the
exact archived manifest and rereading and reverifying the replacement.

After that proof matures, Riverhog publishes a deterministic `SHA256SUMS` for the exact
ciphertext objects in each archive copy, signs it with Minisign, and timestamps the
signature. The signature and both plaintext verification artifacts travel with the copy;
the mutable `metadata.yml.age` remains outside the signed immutable inventory. The signing
key is owner-only private deployment configuration, while its public key can be distributed
independently.

Archive stores are the durable authority. A collection's archive-copy set can change only
through guarded copy and retirement operations. An archive copy reads and verifies the
source object set, writes and verifies an equivalent destination set, and records the copy
only after completion.

### Object-store configuration

Riverhog uses the S3 API for three separate storage roles:

| Role | Supported count | Purpose |
| --- | ---: | --- |
| Ingress staging store | Exactly one | Temporary, immediately readable client-encrypted uploads |
| Archive store | One or more | Named durable authorities; one receives new writes and an ordered set serves reads |
| Retrieval cache | Zero or one | Leased encrypted copies of objects whose archive provider requires retrieval preparation |

Archive-store names are operator-defined identities, not provider types. List them with
`RIVERHOG_ARCHIVE_STORES`, select one with `RIVERHOG_ARCHIVE_WRITE_STORE`, and order
reads with `RIVERHOG_ARCHIVE_READ_ORDER`. A store named `cold-copy` uses configuration
keys beginning `RIVERHOG_ARCHIVE_STORE_COLD_COPY_`. Variables from an optional Compose
environment file are passed through to the server, so arbitrary configured store names are
supported without editing the Compose file.

Each archive store chooses one supported backend profile: `s3` for a generic
S3-compatible immediate-read service, `b2` for Backblaze B2's S3 API, or `aws` for
AWS S3. The `aws` profile alone supports `restore_required` reads and optional
CloudFront delivery; `s3` and `b2` are immediate-read profiles. Any
`restore_required` store requires the separate retrieval cache. Production deployments
should use separate buckets for ingress staging and retrieval caching. A shared bucket is
supported only when the two roles use non-overlapping prefixes. The checked-in
development stack has one immediate-read Garage archive named `archive` and no retrieval
cache.

## Applications and retrieval

Riverhog publishes portable collection manifests and current collection changes through a
narrow ResourceSync profile. Applications keep their own desired state and data;
Riverhog does not track whether an application has materialized a file.

Every application key carries explicit action-and-resource bindings. Authentication
establishes a named application principal, while independent bindings control what that
principal may do to which resources. Creating a tag is separate from creating a collection
and grants the creating key only collection-creation access under that tag. Post-creation
tag changes require their own explicit permission. The bootstrap credential can issue
application keys and assign their download quotas but has no operational collection
authority. Riverhog's guarded deletion and download policies remain in force after
authorization.

An application submits exact immutable file references from a retrieval plan. Riverhog
chooses a complete archive copy and creates an application-owned retrieval job. Immediately
readable objects are served from their archive store. For a store whose provider requires
retrieval preparation, Riverhog performs that work asynchronously and copies the existing
archive ciphertext into a separate retrieval-cache store. Application leases bound cache
retention, and acknowledgment releases the job's leases. When the write store itself
requires retrieval preparation, Riverhog retains newly written canonical archive ciphertext
under a bounded configurable retrieval-cache lease.

The content endpoint reconstructs one logical file, supports validators and byte ranges,
and verifies archive and file checksums. The default client's `local` subtree is the
reference application: it owns a local directory and SQLite catalog, follows
ResourceSync changes, obtains retrieval jobs, verifies downloads, and atomically publishes
local files. Other applications use the same interface and remain isolated from its state.

Each service publishes its operational lifecycle as a durable CloudEvents 1.0 cursor log.
Riverhog and Munchy scope normal event readers to their authenticated application. Munchy
records the initiating application on each submission and owns both native and translated
job events to that application. It consumes its Riverhog stream, correlates collection
identities through the Munchy database, and emits the corresponding job event with the
Riverhog event as its cause. Jeb consumes only its application-owned Munchy stream,
correlates target job identities through the Jeb database, and emits attempt events. A
relay for a direct Munchy client therefore cannot see Jeb-owned Munchy events. An optional
bounded JSON context follows one operation for downstream routing but is not application
identity or state; it expires after terminal work. A generic relay may forward the exact
structured events to a webhook, while the receiver owns filtering, wording, urgency, and
presentation.

## Component boundaries

- The Riverhog archive platform server owns archive construction, search, portable catalog
  publication, retrieval preparation, and verified logical-file delivery.
- The official `riverhog` client is a separate API consumer. It contains no platform
  server implementation and carries no privileged in-process access.
- The `riverhog local` client owns one local materialization, including its directory,
  SQLite state, selection, repair, audit, and eviction. Collection bytes stay in numeric
  collection directories; rebuildable relative-symlink views project materialized
  collections by their current tags. It uses the same permission-bearing application API
  as every other Riverhog client.
- Munchy is a companion application. Its server owns media discovery, routing,
  transformation, metadata projection, and assembly before handing completed artifacts to
  a named destination adapter; its client communicates through the Munchy API.
- A Munchy execution target implements a server-owned target contract. The NVIDIA AV1
  target is isolated in its own image solely for placement on compatible GPU hosts and is
  not independently useful as a companion application.
- Jeb owns source enrollment and credentials, transport-neutral landing, watched-drop
  scheduling, and named target submission. Its separately packaged client communicates
  through Jeb's independently authenticated API.
- Mango Fish owns generic credentialed CloudEvents cursor consumption and exact event
  delivery to configured webhooks; it is a platform-agnostic utility and does not own
  notification policy or presentation.
- Gogurt is operator tooling that maps mounted-volume markers to configured actions.
- Focused shared packages own reusable protocol, client, configuration, transport, event,
  and CLI primitives. Applications and tools depend on those packages instead of one
  another's implementation.
- Downstream private configuration owns real identity, destinations, recipients, remotes,
  application keys, and deployment topology.

## Core terms

- **Collection:** an integer-identified immutable logical namespace and deletion unit.
- **Tag:** a mutable catalog label that must exist before assignment to a collection.
- **Archive object:** one independently encrypted pack, file, segment, manifest, proof, or
  mutable metadata manifest.
- **Archive store:** a named durable object-store destination with defined read behavior.
- **Archive copy:** one verified object set for a collection in one archive store.
- **Ingress staging store:** temporary client-encrypted upload-session storage. Its objects
  are not canonical archive objects.
- **Retrieval cache:** leased encrypted storage used only for archive objects whose provider
  cannot make them immediately readable. Its objects are exact canonical archive ciphertext.
- **Retrieval plan:** a stable resolution of logical files to one complete archive copy.
- **Retrieval job:** application-owned preparation and lease state for one plan.
- **Handoff:** Munchy's delivery of completed artifacts through one named destination
  adapter.
