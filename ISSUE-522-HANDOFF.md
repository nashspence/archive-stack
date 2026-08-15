# Issue #522 implementation handoff

Base: `release/v1` at `a2cabb8af3c7d552ac7f484af06d4cf9dafa1c3d`.

This branch is a comprehensive architectural starting point for #522. It hard-cuts
the durable payload model around finalized Riverhog collections and implements the
new shared contracts and control-plane substrate. It intentionally does not retain
compatibility aliases or migration readers.

## Implemented

- Canonical producer, transform-intent, collection-root, derivation, and artifact-
  disposition contracts in `riverhog-protocol`.
- Riverhog SQL models and service for deterministic processing claims, lease/fence
  renewal, scoped bearer capabilities, immutable derivation verification,
  retirement transition, claim release, and deletion blockers.
- Riverhog API schemas/routes and official API client methods for those primitives.
- Finalized collection receipts project the content etag and immutable manifest
  SHA-256 required by companions.
- Reusable `CollectionProducer` for protocol adapters and transform outputs,
  including producer evidence, provenance, raw-part hashing, resumable upload, and
  fail-closed finalization receipts.
- A minimal FDE-scratch FTP/watch adapter reference that atomically claims stable
  files and deletes transient bytes only after Riverhog finalization.
- Payload-free Jeb collection controller: tag selection, bounded input grouping,
  deterministic claims, Munchy submission, lease renewal, independent Riverhog
  verification, and optional fenced retirement.
- Munchy collection-transform coordinator: durable control state, target-owned
  workspace boundary, scoped Riverhog capability, exactly one output collection,
  immutable derivation evidence, and no output on failure.
- Official Munchy collection-transform client and HTTP binding.
- Focused deterministic tests and architecture documentation.

## Deliberate hard cut

The repository is pre-release and unused. The intended follow-up should remove,
not preserve:

- Jeb FTP/TUS/watch payload custody and media-preflight/repair paths;
- Jeb-to-Munchy file upload and safe-delete handoff state;
- Munchy input TUS/spool state and upload API;
- Munchy custody holds;
- command/rclone/generic destination handoffs; and
- the special optional Riverhog handoff adapter.

The new collection-first path should become the only runtime path, current schema,
configuration inventory, client surface, CLI surface, Compose deployment, and
qualification contract.

## Remaining conformance work for the coding agent

1. Wire the existing production transform-target implementations behind
   `CollectionTransformTarget`. Targets must fetch claimed Riverhog artifacts with
   the scoped capability and use only encrypted/ephemeral job workspace.
2. Replace the current Munchy monolithic submission/job runtime with the new
   collection-transform service, including restart scheduling, cancellation,
   CloudEvents, diagnostics, CLI, and operation-matrix classification.
3. Replace Jeb's current source registry/ingress runtime with recipe and cursor
   persistence around `JebCollectionController`; keep source retirement separately
   observable and resumable.
4. Split the reference FTP/watch adapter into an independently packaged minimal
   protocol adapter and add the actual FTP daemon boundary. Reuse
   `CollectionProducer`; do not reintroduce adapter-specific durable custody.
5. Extend scoped logical-file reads for efficient target streaming/range access.
   Do not expose archive keys, passphrases, or broad S3 credentials.
6. Make derivation evidence part of the immutable archive/root contract rather than
   relying only on a reserved ordinary file plus catalog projection if the richer
   archive schema warrants it.
7. Fold the workflow tables directly into the current pre-release state fixture and
   remove obsolete Jeb/Munchy tables instead of adding compatibility migrations.
8. Regenerate OpenAPI, clients, CLI references, configuration inventory,
   `release.toml`, distribution/image inventories, Compose, docs, #469, and #492.
9. Run destructive failure injection at claim expiry/fencing, output finalization,
   Jeb verification, retirement, and adapter-source cleanup boundaries.

## Acceptance focus

The strongest vertical proof is:

1. An FTP-style adapter commits a tagged intake collection and retains its claimed
   source until the finalized root receipt.
2. Jeb selects the exact immutable root and creates one deterministic fenced claim.
3. Munchy reads only through the claim capability and produces one finalized
   derived collection.
4. The output contains exact derivation/provenance evidence; Jeb verifies it from
   Riverhog.
5. A malformed input produces no false output and remains recoverable in the intake
   collection while later work proceeds.
6. A retirement recipe deletes inputs only through the exact retiring claim and
   survives interruption without affecting the settled output.

The branch includes focused tests for the new contracts. Full legacy repository
checks are expected to identify surfaces that must be deleted or regenerated as
part of the hard cut; do not restore old behavior merely to satisfy those tests.
