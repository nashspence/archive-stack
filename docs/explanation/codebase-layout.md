# Codebase layout

## Riverhog

- `src/riverhog_core/domain/` contains domain values and models.
- `src/riverhog_core/ports/` defines storage, catalog, clock, cryptography, identifier, and projection boundaries.
- `src/riverhog_core/services/` implements collection upload, archive, restore, fetch, search, file, and notification behavior.
- `src/riverhog_core/stores/` contains concrete storage adapters.
- `src/riverhog_api/` exposes the FastAPI application, routers, and schemas.
- `src/riverhog_cli/` exposes the Riverhog operator CLI.
- `src/riverhog_age/` implements the archive encryption format.
- `src/gogurt/` contains the browser interface.

## Ingest

- `src/munchy/` and `src/munchy_cli/` implement generic media ingest contracts and commands.
- `src/jeb/` implements watched-drop collection and scheduling.
- `services/` contains deployable API, worker, ingest, and browsing services.

## Contracts and tests

- `contracts/openapi/` is the checked API contract.
- `contracts/webhooks/` defines operator notification payloads.
- `contracts/terminology/` inventories user-facing vocabulary.
- `tests/unit/` protects focused behavior and policy.
- `tests/harness/` exercises the assembled API contract.
- `config/examples/` contains generic fake configuration only.

Keep domain behavior in services, external effects behind ports, and HTTP or CLI formatting in adapters.
