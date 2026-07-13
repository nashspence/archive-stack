# riverhog

Public archival custody, recovery, and media-ingest software.

Riverhog owns the generic archive API, operator CLIs, optical-media recovery
flows, Munchy media ingest, Jeb watched-drop collection, Djdan disc workflows,
and fake examples. Real deployments should keep private hostnames, device names,
credentials, webhook recipients, and destination policy outside this repository
and pass them in as configuration.

## Critical Risks

This repo handles archive custody semantics. Changes to upload finalization,
catalog state, fetch/recovery, optical media, Munchy source artifacts, Jeb
cleanup, notification routing, and storage credentials can affect whether bytes
are recoverable or whether source media is deleted at the right time.

Keep public code generic. Do not add real private topology, personal device
names, household-specific examples, production credentials, or deployment-only
shortcuts here.

## Normal Work

Install the checked-in toolchain and run the normal local gates:

```bash
mise install
make lint
make unit
```

Run local Compose packaging when service images or runtime configuration change:

```bash
make build
make bootstrap-garage
make down
```

Run the fixture-backed acceptance harness when changing external contracts:

```bash
make spec
make stop-spec
```

The Makefile expects `mise` on `PATH`. If needed, pass
`MISE_BIN=/abs/path/to/mise`.

## Validation

Use `make lint` and `make unit` for the supported fast lane. Use `make test`
when one serial command is preferred. Use `make spec` for acceptance-contract
work, and stop/restart that lane before editing source or fixtures.
`requirements-runtime.txt` and `requirements-service.txt` come from `uv.lock`.
They are generated deployment exports for Docker image installs.

Use focused selectors while iterating:

```bash
make unit TESTS=tests/unit/test_mount_markers.py
make unit args='tests/unit/test_jeb_collector.py -k preflight'
```

## Ownership and Routes

| Need | Source |
| --- | --- |
| Current architecture and source layout | [docs/explanation/architecture-overview.md](docs/explanation/architecture-overview.md), [docs/explanation/codebase-layout.md](docs/explanation/codebase-layout.md) |
| Local stack and acceptance tests | [docs/how-to/run-the-compose-stack.md](docs/how-to/run-the-compose-stack.md), [docs/how-to/run-acceptance-tests.md](docs/how-to/run-acceptance-tests.md) |
| Riverhog API, configuration, and domain model | [docs/reference/api.md](docs/reference/api.md), [docs/reference/configuration.md](docs/reference/configuration.md), [docs/reference/domain-model.md](docs/reference/domain-model.md) |
| Munchy ingest, reusable device profiles, and review sweeps | [docs/reference/munchy.md](docs/reference/munchy.md) |
| Jeb watched-drop collection and the Jeb/Munchy boundary | [docs/reference/jeb.md](docs/reference/jeb.md) |
| Mount-marker route config and macOS listener contract | [docs/reference/mount-markers.md](docs/reference/mount-markers.md) |
| Djdan and optical-media recovery | [docs/reference/disc.md](docs/reference/disc.md), [docs/how-to/run-a-guided-burn-session.md](docs/how-to/run-a-guided-burn-session.md), [docs/how-to/fulfill-a-fetch-from-optical-media.md](docs/how-to/fulfill-a-fetch-from-optical-media.md) |
| Architecture decisions | [docs/adr/README.md](docs/adr/README.md) |
