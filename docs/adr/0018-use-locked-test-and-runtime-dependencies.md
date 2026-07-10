# ADR-0018: Use Locked Test and Runtime Dependencies

## Decision

Riverhog runs local quality lanes from `uv.lock` with explicit uv dependency
groups and runtime extras. Runtime and service Docker images install from
hashed requirements exports generated from that same lockfile:
`requirements-runtime.txt` for the app runtime and `requirements-service.txt`
for deployed service images.

## Reason

The project needs one source of dependency truth for development and tests,
while still keeping Docker dependency-install layers reproducible and cacheable
for pip-based runtime images.
