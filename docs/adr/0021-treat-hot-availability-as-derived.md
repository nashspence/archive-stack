# ADR-0021: Treat Hot Availability as Derived

## Decision

Riverhog treats hot availability as a materialized projection of catalog state, not as source-of-truth state.
The projection is driven by active pins and by repairable archive/disc state.

## Reason

Hot bytes can be browsed, downloaded, lost, or rebuilt, but user intent and archive membership must remain unambiguous.
Release keeps cache cleanup tied to explicit pin intent: non-compliant bytes stay pinned hot, and compliant released bytes
are deleted only when no remaining pin needs them.
