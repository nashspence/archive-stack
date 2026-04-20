# ADR 0003: Pin and release operate on exact canonical targets

## Status

Accepted.

## Context

Broader and narrower targets may overlap. Release semantics must be predictable.

## Decision

- `POST /pin` is exact-target idempotent
- `POST /release` removes only the exact canonical target pin
- broad and narrow pins may coexist independently

## Consequences

- pinning the same target twice does not create duplicates
- releasing a broad pin does not remove narrower remaining pins
- releasing a narrow pin does not remove broader remaining pins
