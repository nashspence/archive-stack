# ADR-0023: Make Pin and Release Exact-Selector Operations

## Decision

Riverhog makes pin and release operate only on exact canonical selectors.
Release is also the only operator-facing cache removal operation.

## Reason

Overlapping broad and narrow pins must not affect each other accidentally.
Releasing a narrower selector must not subtract it from a broader pin. When a fully compliant exact pin is released,
Riverhog removes only selected hot files that are no longer covered by any remaining pin.
