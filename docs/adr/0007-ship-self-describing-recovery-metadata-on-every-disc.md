# ADR 0007: Ship self-describing recovery metadata on every disc

## Status

Accepted.

## Context

Discs must be recoverable even when the API is unavailable or only part of a collection survives on any one image.
That means each disc needs enough local metadata to map generic objects back to logical files and to verify recovered
collections after reconstruction.

## Decision

- each disc includes an encrypted YAML disc manifest that maps generic object names to logical collection paths
- each payload object has an encrypted YAML sidecar with per-file metadata and split-part information when applicable
- any disc that represents any bytes from a collection also includes that collection's whole hash manifest and its
  OpenTimestamps proof
- the planner must budget all of this metadata, not just payload ciphertext
- `README.md` documents the intended manual recovery procedure, including how to reconstruct files and collections that
  span multiple discs

## Consequences

- `arc-disc` can recover files directly from disc-local metadata instead of depending on filename conventions
- manual recovery remains possible with only the disc contents and decryption capability
- split collections can be reassembled deterministically by collection id, logical path, and part index
