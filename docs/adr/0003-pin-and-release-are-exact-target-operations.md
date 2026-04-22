# ADR 0003: Pin and release operate on exact canonical selectors

## Status

Accepted.

## Context

Broader and narrower selectors may overlap. Release semantics must be predictable.

## Decision

- `POST /pin` is exact-selector idempotent
- `POST /release` removes only the exact canonical selector pin
- broad and narrow pins may coexist independently
- releasing one selector reconciles hot storage only against the remaining exact pins

## Consequences

- pinning the same selector twice does not create duplicates
- releasing a broad pin does not remove narrower remaining pins
- releasing a narrow pin does not remove broader remaining pins
- releasing a pin removes all hot files that are no longer covered by any remaining pin
