# ADR 0021: Use collection-native Glacier protection

## Status

Accepted.

## Context

Collections are the logical archive unit users upload, search, pin, restore, and
report on. Finalized images are physical ISO artifacts used for disc copies.

Glacier protection must therefore attach to collections before Riverhog admits
them into the rest of the archive.

The opinionated archival workflow depends on answering:

- whether a collection upload has reached `uploading`, `archiving`,
  `finalized`, or `failed`
- whether a collection has a verified whole-collection Glacier archive,
  manifest, and OpenTimestamps proof
- whether a collection is admitted to hot storage, search, WebDAV, and disc
  planning
- how many physical copies are still required for one finalized image
- whether one collection is cloud-only, under-protected, or fully protected

## Decision

- a collection upload is successful only after Riverhog has received every file,
  verified file hashes, built a deterministic whole-collection archive package,
  uploaded the archive package to Glacier, verified the object receipt, and then
  promoted the collection into committed hot storage
- collection upload sessions use four states:
  `uploading`, `archiving`, `finalized`, and `failed`
- Riverhog does not list, search, expose through WebDAV, or plan a collection
  until its collection-native Glacier archive package has uploaded and verified
- each accepted collection carries direct Glacier archive metadata:
  `state`, `object_path`, `stored_bytes`, `backend`, `storage_class`,
  `last_uploaded_at`, `last_verified_at`, and `failure`
- each accepted collection carries archive package metadata:
  `archive_manifest.object_path`, `archive_manifest.sha256`,
  `archive_manifest.ots_object_path`, `archive_format`, and `compression`
- collection summaries expose direct Glacier state, archive manifest/proof state,
  disc coverage, and image coverage
- collection `protection_state` uses overall values:
  `under_protected`, `cloud_only`, `physical_only`, and `fully_protected`
- normal accepted collections are at least `cloud_only` because admission already
  requires verified Glacier upload
- a collection is `fully_protected` only when its collection Glacier archive is
  uploaded and verified and every logical file is covered by enough verified
  physical disc copies
- every finalized image carries a required physical-copy count, defaulting to `2`
- finalized-image summaries expose physical-copy protection fields:
  `physical_protection_state`, `physical_copies_required`,
  `physical_copies_registered`, `physical_copies_verified`, and
  `physical_copies_missing`
- finalized images carry physical-copy protection state
- Glacier reporting measures collection archive objects directly; image entries,
  when present, explain physical coverage of collections
- recovery sessions have explicit recovery types:
  `collection_restore` for restoring collection content from its collection
  archive, and `image_rebuild` for rebuilding lost physical media from restored
  collection archives plus persisted image coverage metadata
- ready `collection_restore` sessions can materialize explicitly selected
  logical file paths into hot storage after verifying the restored manifest,
  proof, archive membership, byte counts, and file hashes
- recovery session summaries expose archive verification, extraction, and
  materialization progress states

## Consequences

- an upload can finish receiving bytes while remaining incomplete from the user
  perspective during `archiving`
- failed Glacier archive upload keeps the collection out of hot storage and disc
  planning until retry succeeds
- disc planning is a physical-copy workflow, not the first durable
  protection step
- Glacier cost and restore reporting uses direct collection archive
  measurements
- rebuilding a lost finalized image from Glacier depends on persisted image
  coverage metadata and collection archives
