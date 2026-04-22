# ADR 0010: Make fetch manifests pin-scoped selector recovery contracts

## Status

Accepted.

## Context

Fetch recovery needs a precise contract. Several important operational questions must be explicit:

- whether hot pins should still have a durable recovery contract
- how one selector maps to one fetch manifest over time
- how selectors should address projected hot-storage directories that may span multiple collections
- how `arc-disc` should resume large uploads while streaming directly from optical recovery
- how incomplete server-side upload state expires without leaving abandoned partial files behind forever

Optical recovery is easier to reason about if the user's durable intent remains "one pin on one canonical selector" and
the fetch manifest is the recovery view of that pin.

## Decision

- pinning one exact canonical selector creates or reuses exactly one fetch manifest for that same selector
- the fetch manifest persists for the lifetime of that exact pin, even when the selector is already fully hot
- the manifest is satisfied when every byte selected by that pin is currently hot; a satisfied manifest remains
  readable until the user releases the pin
- releasing the exact pin abandons and removes the associated fetch manifest
- selectors operate only over the projected hot namespace:
  - if the selector identifies a projected directory, it selects every file beneath that directory
  - if the selector identifies a projected file, it selects that whole file
- projected-directory selectors may span multiple collections
- releasing one exact pin immediately reconciles hot storage against the remaining pin set
- incomplete server-side upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last successfully accepted chunk
- upload-state expiry moves the manifest back to `waiting_media`
- the fetch summary exposes an audit field for that expiry boundary, for example `upload_state_expires_at`
- the fetch summary and manifest expose enough progress state for a client to list pending files, partial files still
  inside the TTL window, and uploaded files
- `arc-disc` reports precise progress for both the current file and the whole manifest during recovery and upload

## Consequences

- hot and cold pins share one durable selector-to-manifest model
- repeated pinning of the same exact selector converges on one manifest as long as the exact pin remains present
- the fetch manifest becomes the stable orchestration contract for `arc-disc`, even when recovery pauses and resumes
- multi-disc recovery can resume without leaving unbounded server-side partial uploads behind
- users only need to understand one selector namespace: projected paths
