# ADR-0018: Use Locked Test and Runtime Dependencies

## Decision

Riverhog runs local quality lanes in locked uv environments and builds
containers from hashed lockfiles. Deployed service images install from
`requirements-service.txt`, which is generated from the runtime dependency set
plus service-only Uvicorn extras.

## Reason

The project needs reproducible checks and container builds without allowing
test, runtime, or deployed-service dependency drift.
