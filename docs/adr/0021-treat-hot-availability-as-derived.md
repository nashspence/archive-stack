# ADR-0021: Treat Hot Availability as Derived

## Decision

Riverhog treats hot availability as a materialized projection of catalog state, not as source-of-truth state.
The projection may include finalized-upload cache entries that are not pinned.

## Reason

Hot bytes can be browsed, downloaded, lost, or rebuilt, but user intent and archive membership must remain unambiguous.
Separating `evict` from `release` keeps cache cleanup distinct from explicit
pin intent.
