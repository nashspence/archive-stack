# ADR 0006: Use a canonical disc layout with generic object names

## Status

Accepted.

## Context

Optical images are both machine artifacts and long-lived human recovery media. The disc contents need a stable shape
that `arc-disc` can automate against, while still remaining understandable if a person must recover bytes manually.

## Decision

- every ISO uses one canonical root layout with `README.md`, `DISC.yml.age`, `files/`, and `collections/`
- `README.md` is the only plaintext file
- every other leaf file on the disc is individually encrypted
- payload objects and sidecars use generic stable names under `files/`
- collection-level artifacts use generic stable names under `collections/`
- canonical logical paths are recorded only inside decrypted YAML, not in on-disc filenames

## Consequences

- partial filesystem survival still leaves individually recoverable objects
- a person can inspect the disc structure without learning logical filenames from the directory tree
- the API, planner, and `arc-disc` can all target the same durable disc contract
