# Archive Storage MVP — Features and Test Alignment

This document defines the product surface as a feature inventory that can be
used for test planning, coverage tracking, and regression review.

It is intentionally organized around feature areas rather than around API
endpoints. The goal is to make it easy to answer:

- what the product promises
- what behaviors must be tested
- what failure cases matter
- which tests prove which features

Source split from the original combined project draft. fileciteturn0file0

## Product Summary

Archive Storage MVP is a minimal, self-hosted archive backend for moving large
file sets from online hot storage into partitioned offline media while
preserving catalog visibility, deterministic archive outputs, and controlled
re-hydration back into online access.

## Feature Inventory

---

## F1. Private-deployment authentication boundary

### Description
All public `/v1/...` routes require a shared bearer token. Internal tus hook
callbacks require a separate shared secret.

### Why it exists
The service is intended for trusted, self-hosted deployments with a deliberately
small auth model.

### Core behaviors
- Public API rejects missing or incorrect bearer tokens.
- Internal hook endpoint rejects missing or incorrect hook secret.
- Authorized requests are accepted.

### Primary test alignment
- missing bearer token
- wrong bearer token
- missing hook secret
- wrong hook secret
- public route allowed with correct token
- internal hook route allowed with correct secret

---

## F2. Resumable upload reservation and ingestion

### Description
Jobs are created first, then file upload slots are reserved. Each slot binds a
relative path, size, and optional SHA-256 expectation. Uploads are performed via
`tusd` and validated against the reserved slot.

### Why it exists
This gives the service resumable transfer behavior while keeping the catalog in
control of what the upload is supposed to be.

### Core behaviors
- Uploads cannot appear without a reserved slot.
- Uploaded metadata must match the reserved path and expected size.
- Completed uploads compute SHA-256 and are recorded into hot storage.
- Failed or terminated uploads do not become catalog-valid files.

### Primary test alignment
- invalid relative paths
- duplicate file path reservation within a job
- invalid file mode format
- invalid timestamp format
- mismatched upload size
- upload metadata path not matching reserved slot
- happy path from slot reservation to completed file
- post-receive progress updates
- SHA-256 mismatch failure
- terminated upload failure
- missing incoming tusd file on finish

---

## F3. Catalog-first job model

### Description
A job is modeled as a logical file tree consisting of explicit directories and
uploaded files. Paths are normalized and constrained to remain within the job
root.

### Why it exists
The catalog must remain the source of truth for what a job contains, regardless
of whether file bytes are currently online.

### Core behaviors
- Jobs can contain directories and files.
- Job tree responses represent the full logical structure.
- Offline archived files remain visible in the API.
- File content endpoints distinguish between online and offline states.

### Primary test alignment
- invalid relative paths
- duplicate file path reservation within a job
- online job file read from hot buffer
- offline job file response with disc IDs
- exports tree includes online files only

---

## F4. Real-time progress streaming

### Description
The service emits replayable server-sent event streams for upload progress,
aggregate job progress, cache-session upload progress, and ISO download
progress.

### Why it exists
Long-running archive operations need observable progress without requiring the
client to poll.

### Core behaviors
- Progress is emitted per upload.
- Progress is emitted per job.
- Progress is emitted per cache session.
- Progress is emitted per ISO download session.
- Streams are replayable through Redis-backed event history.

### Primary test alignment
- post-receive progress updates
- ISO streaming progress transitions

---

## F5. Planner-driven partition closure

### Description
When a job is sealed, its files are ingested into planner state. The planner may
close zero, one, or multiple partitions depending on fill rules. Large files may
be split when necessary.

### Why it exists
The archive layer needs deterministic partition outputs suitable for offline
media creation and later verification.

### Core behaviors
- Sealing validates job readiness.
- Files move into planner state.
- Planner can emit zero or more closed partitions.
- Closed partitions are materialized as rooted directory trees.
- Closed partitions are imported into the catalog as discs and archive
  mappings.

### Primary test alignment
- sealing nonexistent job
- sealing already sealed job
- sealing with missing uploads
- sealing empty job
- sealing job that closes no discs
- sealing job that closes one or more discs
- planner output imported into catalog

---

## F6. Archive metadata and encrypted sidecars

### Description
Each closed partition includes archive metadata artifacts and individually
encrypted leaf files.

### Why it exists
Offline media must carry enough metadata to support integrity, recovery, and
manual handling without depending entirely on the live service.

### Core behaviors
- Each partition contains encrypted `MANIFEST.yml`.
- Each partition contains plaintext `README.txt` with manual recovery guidance.
- Payload files live under `files/...`.
- Each payload has an encrypted YAML sidecar using `sidecar/v1`.
- Each decrypted manifest conforms to `manifest/v1`.
- Split files carry part metadata for reconstruction.
- Every emitted leaf file except `README.txt` is encrypted with `age` plus
  `age-plugin-batchpass`.
- Batchpass work factor is configurable.

### Primary test alignment
- planner output imported into catalog
- split-file reconstruction from cached chunks
- ISO authoring success

### Additional validation worth making explicit
- partition root contains expected manifest and readme artifacts
- payload sidecars conform to `sidecar/v1`
- encrypted outputs are emitted for all required leaf files
- split-file metadata is sufficient for reconstruction

---

## F7. Offline-aware file access and reconstruction

### Description
The service serves content directly when bytes are available and returns a
structured offline explanation when they are not. Split files can be
reconstructed when all required chunks are online across cached partitions.

### Why it exists
Archived files should remain addressable and understandable even when their
bytes are not immediately available.

### Core behaviors
- Hot-buffer files are directly readable.
- Cached archived payloads are directly readable.
- Split files can be materialized on demand from available chunks.
- Offline responses identify candidate disc IDs when content is unavailable.

### Primary test alignment
- online job file read from hot buffer
- online job file read from cached disc
- split-file reconstruction from cached chunks
- offline job file response with disc IDs
- online disc file read from cache
- offline disc file response

---

## F8. Verified partition-root cache uploads

### Description
Known archived partition roots can be uploaded back into cache, but only if the
uploaded tree exactly matches the archived partition.

### Why it exists
Re-hydration must be safe. A cache upload is accepted only when it proves it is
bit-for-bit the same logical archive root the service already knows.

### Core behaviors
- Cache session creation is restricted to known discs.
- Upload slots are restricted to expected partition-root paths.
- Completion computes a canonical tree hash.
- Activation requires exact match on file set, sizes, digests, and hash.
- Accepted cached roots restore online read availability.

### Primary test alignment
- cache session creation for known disc
- cache session creation for unknown disc
- cache upload slot for known path
- cache upload slot rejection for unknown path
- cache completion hash mismatch
- cache completion file-set mismatch
- cache completion success
- cache eviction success

---

## F9. Disc browsing and content access

### Description
Closed partitions are exposed as browsable logical trees, and their content can
be read from cached partition roots when online.

### Why it exists
Operators need a direct disc-centric view in addition to the job-centric view.

### Core behaviors
- Disc tree endpoints expose partition contents.
- Disc content reads succeed when the partition root is cached.
- Disc content remains catalog-visible even when offline.

### Primary test alignment
- online disc file read from cache
- offline disc file response
- cache completion success
- cache eviction success

---

## F10. ISO authoring, registration, and download

### Description
The service can author ISOs from partition roots or register externally created
ISOs. Download sessions are tracked independently and expose progress.

### Why it exists
The archive workflow is designed for file-based offline media processes,
including disc-burning workflows that need downloadable ISO artifacts.

### Core behaviors
- ISO can be authored from a partition root.
- External ISO files can be registered.
- ISO download sessions are tracked separately from ISO creation.
- ISO content downloads emit progress updates.
- ISO authoring uses `xorriso` with ISO9660, Joliet, Rock Ridge, and UDF.

### Primary test alignment
- ISO authoring success
- ISO authoring failure when tool is unavailable or root is missing
- ISO overwrite guard
- external ISO registration success
- download session creation for registered/authored ISO
- ISO streaming progress transitions

---

## F11. Conservative hot-buffer retention

### Description
Hot-buffer originals are retained until archived bytes are fully represented and
related discs have been burn-confirmed, unless a job explicitly opts out of that
cleanup policy.

### Why it exists
Archive completion should be conservative by default so originals are not
removed before archive safety conditions are met.

### Core behaviors
- Cleanup waits for archive completeness.
- Cleanup waits for burn confirmation.
- Jobs can opt into keeping the hot buffer after archive.
- Operators can manually release the buffer.

### Primary test alignment
- default cleanup after all related discs are burn-confirmed
- no cleanup before burn confirmation
- no cleanup when archived bytes are incomplete
- no cleanup when job opted into keeping hot buffer
- manual buffer release

---

## F12. Online-only export tree

### Description
The service maintains an export tree for each job that includes only files that
are currently online.

### Why it exists
Other local systems can safely mount exported jobs without being misled into
believing offline bytes are available.

### Core behaviors
- Export tree shows only online files.
- Export tree updates after upload completion.
- Export tree updates after cache activation.
- Export tree updates after cache eviction.
- Export tree updates after buffer release.

### Primary test alignment
- exports tree includes online files only
- exports tree updates after upload completion
- exports tree updates after cache activation
- exports tree updates after cache eviction
- exports tree updates after buffer release

---

## Feature-to-Test Coverage Matrix

| Feature | Coverage status from current test map |
|---|---|
| F1 Auth boundary | Covered |
| F2 Resumable upload reservation and ingestion | Covered |
| F3 Catalog-first job model | Partially covered |
| F4 Real-time progress streaming | Partially covered |
| F5 Planner-driven partition closure | Covered |
| F6 Archive metadata and encrypted sidecars | Partially covered |
| F7 Offline-aware file access and reconstruction | Covered |
| F8 Verified partition-root cache uploads | Covered |
| F9 Disc browsing and content access | Covered |
| F10 ISO authoring, registration, and download | Covered |
| F11 Conservative hot-buffer retention | Covered |
| F12 Online-only export tree | Covered |

## Most Important Coverage Gaps To Make Explicit

These behaviors are implied by the feature set but are not as explicitly called
out in the current coverage map as the others:

- encrypted partition contents are emitted for all non-README leaf files
- `MANIFEST.yml` and `README.txt` are present in every closed partition
- sidecar files conform to the expected schema version
- replayability semantics for SSE streams, not just progress emission
- job tree correctness when a mix of online and offline files exists
- disc tree correctness after catalog import
- exact reconstruction rules for split files across multiple cached discs

## Recommended Test Suite Structure

A clean way to align implementation and tests is to organize automated coverage
by feature ID:

- `test_f1_auth_boundary.py`
- `test_f2_upload_ingestion.py`
- `test_f3_catalog_job_model.py`
- `test_f4_progress_streams.py`
- `test_f5_partition_planner.py`
- `test_f6_archive_metadata_encryption.py`
- `test_f7_offline_access.py`
- `test_f8_cache_verification.py`
- `test_f9_disc_browsing.py`
- `test_f10_iso_flow.py`
- `test_f11_retention_cleanup.py`
- `test_f12_export_tree.py`

That makes it straightforward to review regressions against user-visible product
promises rather than only against endpoint groups.
