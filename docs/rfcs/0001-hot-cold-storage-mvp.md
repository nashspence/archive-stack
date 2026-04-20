# RFC 0001: Hot/cold archival storage MVP

## Status

Accepted for MVP implementation baseline.

## Context

The system is a self-hosted archival service built around optical media, specifically 50 GB Blu-ray discs.
Users upload collections into a staging directory and then close a staged directory into the archive. Closing
a collection catalogs its contents, materializes it into hot storage, and makes it eligible for planning into
future optical images.

The design constraints are:

- collections are the main user-visible namespace
- collections can be larger than one optical disc and may be split across multiple images
- the web UI should stay very small and should not become a file browser
- users must be able to restore a single file or subtree without restoring an entire collection
- hot storage should be treated as a read-only projection from the user point of view
- archival truth must not depend on mutating the hot directory tree directly

## Problem

Users think in collections, but storage and recovery work at file granularity. A naive “hot storage is just a writable
collection tree” approach makes restore and eviction ambiguous:

- deleting from the hot tree does not clearly express user intent
- scanning a mirror tree to infer intent is awkward and error-prone
- partial restore and partial eviction become hard to model safely
- the UI drifts toward re-implementing file browsing

## Decision

The system uses these core ideas:

1. A collection is the logical namespace.
2. A file is the operational unit for restore and eviction.
3. Hot storage is a materialization layer, not the source of truth.
4. The API state is the system of record for archive membership and hot residency.
5. Users express hot intent through pins, not through direct filesystem mutation.
6. The visible hot tree is a read-only projection from metadata.

## Proposed model

The system is split into three conceptual layers.

### 1. Catalog

The catalog stores logical metadata such as:

- collection id
- original relative path
- file hash
- size
- encrypted archival locations
- optical copies that contain the file
- hot state
- pin state
- last accessed / restored / explicitly requested timestamps

### 2. Hot object store

The hot store keeps immutable file bytes keyed by content hash. It has no collection-path semantics of its own.

### 3. Projected namespace

The visible hot tree is generated from metadata and points into the hot object store, for example through symlinks,
hardlinks, or another generated read-only view.

## API shape

The minimal external surface is:

- close a collection
- search globally
- show a collection summary
- show a burn plan
- inspect an image
- download an ISO
- register a physical copy
- pin a target
- release a target
- list pins
- inspect a fetch
- read a fetch manifest
- upload recovered bytes
- complete a fetch

The same canonical target string is used everywhere in API and CLI.

## Consequences

This design keeps the UI small, allows single-file restore, avoids direct manipulation of the hot tree, and gives the
system a clean place to enforce idempotency and recovery rules. It also makes important invariants testable at the API
boundary.

## Non-goals for MVP

- a full web file browser
- direct user mutation of the hot tree
- exposing internal database schema
- specifying the on-disc format beyond what the fetch manifest needs
