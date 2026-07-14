# AGENTS.md

## Identity

Riverhog owns generic collection custody, encrypted remote archives, hot-cache materialization, fetches, and search. Munchy owns generic media ingest, Jeb owns generic watched-drop collection, and Gogurt owns the web interface.

## Critical Risks

- The remote archive account is the durable authority. Protect account recovery, credentials, billing, bucket access, and tested retrieval.
- Collection acceptance must preserve verified archive bytes, a manifest, and its proof.
- Keep public code generic. Do not add private deployment details, real deployment identity, or downstream private configuration.
- Never expose secrets in code, fixtures, logs, examples, or generated contracts.
- Make catalog and object-store changes together and verify the resulting custody state.

## Normal Work

- Read the relevant docs, tests, and contracts before editing.
- Use `rg` for discovery, `uv` through the repository-selected `mise` toolchain, and `make` for standard gates.
- Use canonical current names. Do not add aliases, migrations, or compatibility shims without a verified supported dependency.
- Keep Riverhog, Munchy, Jeb, and Gogurt responsibilities separate.
- Put only generic behavior and fake examples here; private identity belongs in downstream private configuration.
- Keep docs and tests focused on present behavior.

## Validation

Run focused tests during implementation. Before delivery run:

```bash
make lint
make unit
make spec
make build
```

## Ownership and Routes

- Custody and storage: [architecture](docs/explanation/architecture-overview.md), [archive operations](docs/reference/archive.md).
- API and data model: [API](docs/reference/api.md), [domain model](docs/reference/domain-model.md).
- Commands: [CLI design](docs/reference/cli-design.md).
- Configuration and transport: [configuration](docs/reference/configuration.md), [upload transport](docs/reference/upload-transport.md).
- Media ingest: [Munchy](docs/reference/munchy.md), [Jeb](docs/reference/jeb.md), [Gogurt](docs/reference/gogurt.md).
