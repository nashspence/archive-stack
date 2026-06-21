# ADR-0040: Keep File Introspection Read-Only and Projected

## Decision

Riverhog file introspection exposes projected logical file state without becoming a mutation surface.

## Reason

Operators need to inspect collection contents and hot availability without bypassing fetch, eviction, upload, or recovery workflows.
