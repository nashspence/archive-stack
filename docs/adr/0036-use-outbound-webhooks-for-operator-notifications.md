# ADR-0036: Use Outbound Webhooks for Operator Notifications

## Decision

Riverhog emits configured outbound webhooks for operator-relevant collection,
archive, planner, and recovery events.

## Reason

Recovery readiness, collection upload handoff, archive/promotion progress, planner
completion, and persistent failures need notification without creating additional
product API surface.
