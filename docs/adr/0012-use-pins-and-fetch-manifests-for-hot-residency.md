# ADR-0012: Use Pins and Fetch Manifests for Hot Residency

## Decision

Riverhog uses pins to declare desired hot residency and fetch manifests to recover missing bytes.
Pin release is the only operator-facing hot-cache removal path.

## Reason

The system needs one stable selector-based mechanism for keeping content hot and for guiding recovery when content is archived-only.
Under-protected finalized uploads stay pinned by default, so Riverhog never discards hot bytes that are still needed for
physical compliance. Once selected files are fully compliant, `release` removes the exact pin and deletes any selected hot
bytes no longer covered by another active pin.
