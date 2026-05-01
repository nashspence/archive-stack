# ADR-0042: Use Action-Needed Notification Payloads

## Decision

Riverhog emits outbound action-needed notifications with a human title, human body, and exactly one action: run `arc` or run `arc-disc`.

The notification payload keeps machine-readable event fields for integrations, but the normal operator action is always one no-argument CLI.

Riverhog does not emit routine-success notifications or standalone labeling notifications.

## Reason

Notifications should tell the operator which guided flow can make progress, while integrations still need durable event identity and metadata.
