# ADR-0036: Use Outbound Webhooks for Operator Notifications

## Decision

Riverhog emits configured outbound webhooks for operator-relevant collection upload, archive upload, archive restore, fetch, and Jeb events.

Payloads use the checked webhook contract, stable event names, explicit recipients, and best-effort delivery with recorded failures.

## Reason

Long-running custody and retrieval operations need timely notification without coupling Riverhog to a particular messaging service or adding polling-specific product APIs.
