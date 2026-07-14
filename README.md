# riverhog

Riverhog is a generic custody system for collection uploads, encrypted remote archives, a materialized hot cache, search, fetches, and read-only browsing. Munchy handles media ingest, Jeb handles watched-drop collection, and Gogurt provides the web interface.

## Critical Risks

- The remote archive account is the durable storage authority. Keep account recovery, authentication, billing, bucket access, and object retrieval healthy and tested.
- Collection acceptance requires a verified encrypted archive package and manifest proof.
- Keep public code generic. Do not add private deployment details, real deployment identity, credentials, hostnames, device names, or downstream private configuration.
- Treat catalog and object-store changes as custody operations: verify both state and bytes before declaring success.

## Normal Work

- Read the current contracts and tests before changing behavior.
- Use canonical names directly; avoid aliases and compatibility shims unless a current supported dependency requires one.
- Keep the collection, archive, hot-cache, fetch, Munchy, Jeb, and Gogurt boundaries distinct.
- Put generic behavior and fake examples here. Keep deployment-specific values in downstream private configuration.
- Prefer focused services behind ports, with the HTTP API and CLIs as adapters.

## Validation

Run the scoped checks while iterating, then the full gates before delivery:

```bash
make lint
make unit
make spec
make build
```

See [the testing guide](docs/how-to/run-acceptance-tests.md) for test layers and [the Compose guide](docs/how-to/run-the-compose-stack.md) for the local stack.

`requirements-runtime.txt` and `requirements-service.txt` are generated deployment exports from the locked project environment.

## Ownership and Routes

| Area | Route |
| --- | --- |
| Architecture and custody model | [architecture overview](docs/explanation/architecture-overview.md), [archive operations](docs/reference/archive.md) |
| API and domain vocabulary | [API reference](docs/reference/api.md), [domain model](docs/reference/domain-model.md), [terminology](docs/reference/terminology.md) |
| Operator commands | [CLI design](docs/reference/cli-design.md), [fetch and eviction guide](docs/how-to/create-fetches-and-evict-hot-files.md) |
| Runtime settings and uploads | [configuration](docs/reference/configuration.md), [resumable uploads](docs/reference/resumable-uploads.md) |
| Media ingest | [Munchy](docs/reference/munchy.md), [Jeb](docs/reference/jeb.md), [Gogurt](docs/reference/gogurt.md) |
| Public/private boundary | [ADR 0043](docs/adr/0043-add-munchy-as-generic-ingest-layer.md) |
