# ADR 0001: Treat hot storage as a projection, not the source of truth

## Status

Accepted.

## Context

Direct user mutation of a hot collection tree makes intent ambiguous and complicates restore, release, and reconciliation.

## Decision

Hot storage is a materialization layer generated from metadata. The visible hot tree is read-only from the user point of view.
The API state and catalog are the system of record for archive membership and hot residency.

## Consequences

- users express intent through pin and release operations
- the system can reconcile hot state safely
- direct tree scanning is not needed to infer user intent
