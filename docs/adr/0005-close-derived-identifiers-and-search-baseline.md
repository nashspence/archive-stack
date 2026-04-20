# ADR 0005: Derive collection ids from closed paths and keep search minimal

## Status

Accepted.

## Context

The MVP needs deterministic collection identifiers and a minimal search model that supports target-based actions.

## Decision

- the collection id is the final path component of the closed staging directory
- re-closing the same path fails with `conflict`
- search is case-insensitive substring match over collection id and full logical file path
- `archived_bytes` means bytes covered by at least one registered copy
- after `close`, the whole collection is hot even if no pin exists yet

## Consequences

- fixture-driven acceptance tests can assert predictable ids
- search remains simple and directly actionable
- byte coverage values have stable meaning
