# ADR-0012: Use Pins and Fetch Manifests for Hot Residency

## Decision

Riverhog uses pins to declare desired hot residency and fetch manifests to recover missing bytes.
Hot-cache eviction is a separate operation from pin release.

## Reason

The system needs one stable selector-based mechanism for keeping content hot and for guiding recovery when content is archived-only.
Finalized uploads may be hot without being pinned, so operators need `evict`
to remove archived cache entries without changing keep-hot intent.
