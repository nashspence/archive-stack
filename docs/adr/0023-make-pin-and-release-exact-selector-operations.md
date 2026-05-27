# ADR-0023: Make Pin and Release Exact-Selector Operations

## Decision

Riverhog makes pin and release operate only on exact canonical selectors.
Eviction is modeled separately from release.

## Reason

Overlapping broad and narrow pins must not affect each other accidentally.
Releasing a narrower selector must not subtract it from a broader pin; archived
hot-cache removal uses `evict` and skips any file still covered by a pin.
