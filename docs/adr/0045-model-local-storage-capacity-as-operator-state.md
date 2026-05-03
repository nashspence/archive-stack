# ADR-0045: Model Local Storage Capacity as Operator State

## Decision

Riverhog models local storage capacity as an operator-visible boundary for work that stages or materializes bytes on the local machine.

The operator configures a local storage budget for Riverhog staging and hot-materialization work. Healthy `arc` home can report the configured budget and currently available space as summary information.

Capacity checks are preflight checks before starting collection upload staging, fetch-to-hot materialization, recovery materialization, restored ISO staging, and burn preparation. Long-running work also treats capacity exhaustion as a retryable blocked state instead of surfacing raw filesystem errors.

When capacity blocks work, normal copy tells the operator how much space Riverhog needs, how much is available, and the next safe action: free local storage, raise the configured budget, or choose a different staging location before retrying.

## Reason

Capacity pressure is predictable operational state. Treating it as accepted operator state prevents late low-level filesystem errors and keeps retry behavior consistent across upload, recovery, fetch, and burn workflows.
