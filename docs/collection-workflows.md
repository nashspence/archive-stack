# Collection producers and transformations

Riverhog collections are the only durable payload units. Protocol adapters create
collections, Jeb coordinates transformations over immutable collection roots, and
Munchy produces one new finalized collection per successful transformation.

## Invariants

1. An adapter retains transient source bytes until Riverhog returns a finalized
   collection receipt containing the immutable manifest and content identities.
2. Tags select work, but a processing claim freezes exact collection-root
   identities. Later tag changes cannot mutate an active transform.
3. A scoped transform capability grants access only to the claimed inputs and one
   predetermined output-tag set. It never grants an archive passphrase, S3
   credentials, arbitrary archive keys, catalog-wide access, or deletion.
4. Munchy retains durable workflow state but no durable payload copy. A transform
   target owns any bounded encrypted or ephemeral workspace.
5. Success means exactly one finalized output collection containing canonical
   derivation evidence. Failure or cancellation means no finalized output.
6. Jeb verifies the output from Riverhog rather than trusting Munchy's terminal
   state.
7. Input retirement is an explicit fenced phase. Output success is not rolled back
   when cleanup is delayed or fails.

## Producer contract

A protocol adapter supplies:

- a stable idempotency and source-event identity;
- an `ingest_source` and an allowed tag set;
- canonical relative paths, byte counts, SHA-256 identities, and provenance;
- immutable producer evidence at `riverhog/producer-evidence.json`; and
- bounded transient custody until collection finalization.

Adapters close collections according to content-opaque policy such as maximum age,
idle time, bytes, file count, protocol session, or explicit flush. They do not
interpret media, select transforms, or retain a second object store.

## Processing claims

A Riverhog processing claim binds:

- the owning application;
- a deterministic transform identity;
- a recipe and operation identity;
- canonically ordered input collection IDs, manifest SHA-256 values, and content
  etags;
- effective intent and exact output tags;
- a renewable expiry and monotonically increasing fence; and
- `retain` or `retire-after-verified-output` policy.

Claims are idempotent for the same owner and sealed intent. An expired active claim
may be resumed only with a new fence. Active, settled, and retiring claims block
input collection deletion, except for the exact owning retirement phase.

## Munchy contract

The Munchy request contains the claim ID and fence, a short-lived Riverhog
capability, and the sealed transform intent. A content-aware target may read the
exact input artifacts and use a bounded encrypted or ephemeral workspace. It
returns output artifacts, provenance, a plan identity, execution identity, and a
complete per-input artifact disposition.

Munchy writes the output through Riverhog's normal direct-to-final collection
upload. It adds `riverhog/collection-derivation.json`, waits for finalization, and
returns the output collection and derivation document. It never receives Riverhog's
archive passphrase or broad storage credentials.

## Derivation and verification

The immutable derivation document binds:

- transform, claim, and fence;
- recipe and operation identities;
- exact input collection roots;
- exact output tags;
- plan and execution identities; and
- canonical per-input artifact dispositions and output paths.

Riverhog records a catalog projection only after the output collection was created
by the claim-scoped capability and contains the exact derivation bytes. Jeb then
reads the finalized output and derivation back from Riverhog and verifies all
identities before considering the transform settled.

## Retirement

A recipe may retain its inputs or request retirement after verified output.
Retirement requires a settled claim, the configured grace period, an exact current
deletion plan, no unrelated claims or activity, and a derived collection that is
already independently recoverable. Riverhog authorizes deletion only to the
retiring claim owner. Deletion and claim release are resumable per input.
