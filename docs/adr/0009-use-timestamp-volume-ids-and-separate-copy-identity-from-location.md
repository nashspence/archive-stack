# ADR 0009: Use timestamp volume ids and separate copy identity from location

## Status

Accepted.

## Context

The system already distinguishes between the API-level image id and the ISO volume id recorded on the disc manifest.
That separation is useful: the API image id is a planning and download handle, while the volume id is the media-facing
identifier that survives printing, burning, and offline inspection.

Before any ISO is actually downloaded, a planned image is still only a proposal. Its collections should remain eligible
for replanning into different images if the planner finds a better allocation. Once an operator starts downloading the
ISO, that proposal becomes an actual media artifact and must stop moving underneath them.

Burn registration also needs a durable identity for each physical disc. The current registration shape accepts a user
copy id plus a location string, but a shelf location is an operational locator rather than stable identity. Operators
need to be able to move a disc later without changing which disc the system believes it is.

## Decision

- each planned ISO gets an automatically assigned `volume_id` in compact UTC basic form
  `YYYYMMDDTHHMMSSZ`, for example `20260421T035331Z`
- before the first ISO download request, a planned image remains provisional and its collections stay in the pool for
  potential re-allocation
- the first ISO download request finalizes that image's allocation and assigns its `volume_id`
- the assigned `volume_id` is stored against the `image.id` from that point forward
- the planner derives `volume_id` from that finalization timestamp and must ensure uniqueness
- if more than one image would otherwise receive the same second-level stamp, the allocator advances by whole seconds
  until it finds an unused value
- after finalization, the planner must not change the image's represented bytes or reallocate those collections away
- `volume_id` is immutable once assigned and is the canonical media identifier carried in the ISO and disc manifest
- a registered physical disc is identified by the tuple `(volume_id, copy_id)`
- `copy_id` is an arbitrary operator-supplied string scoped to one `volume_id`
- registering a `copy_id` that already exists for the same finalized image and `volume_id` is rejected
- `copy_id` and the associated `volume_id` are immutable after registration
- `location` is not part of the disc identity; it is mutable operational metadata and may be updated later through
  `arc`

## Consequences

- operators can label and distinguish physical media using identifiers that exist both in the system and on the disc
- planner output can remain flexible until the first actual ISO download, then becomes stable enough to burn and label
  confidently
- moving a disc between shelves or vaults does not require creating a new registration or changing the disc identity
- API and CLI contracts should expose `volume_id` anywhere an operator needs to correlate a planned image with burned
  media
- copy-registration behavior should reject duplicate `copy_id` values within the finalized image/`volume_id` scope and
  reject attempts to mutate `copy_id` or rebind a registered disc to a different `volume_id`
- a follow-on work item must define the exact API, CLI, and persistence changes for mutable `location` updates and the
  new copy identity rules
