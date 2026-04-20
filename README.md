# riverhog

A clean starter skeleton for the riverhog described in the contract:

- FastAPI HTTP API
- `arc` CLI
- `arc-disc` CLI
- shared core domain and service interfaces
- acceptance-test-ready route and command scaffolding

## What is implemented

- package layout
- selector parsing and canonicalization
- domain types, errors, and models
- API schemas and route signatures
- exception mapping
- shared HTTP client
- Typer CLIs with `--json` output mode
- unit tests for selector parsing and API/CLI smoke coverage
- acceptance test skeletons for the contract

## What is intentionally stubbed

Business services currently raise `NotYetImplemented`. This keeps the boundary between
contract and implementation crisp while giving you a runnable app and installable CLIs.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn arc_api.app:create_app --factory --reload
```

In another shell:

```bash
arc --help
arc-disc --help
```

## Environment variables

- `ARC_BASE_URL` default: `http://127.0.0.1:8000`
- `ARC_TOKEN` optional bearer token

## Suggested implementation order

1. `arc_core.domain.selectors`
2. collection close vertical slice
3. search and collection summary
4. pin/release
5. fetch manifest/upload/complete
6. `arc-disc fetch`
7. planner and ISO download
8. copy registration and archive coverage
```

## Adapted donor modules included

This scaffold now also includes:

- `arc_core.planner` for manifest budgeting, tree splitting, MILP packing, and preview layout helpers
- `arc_core.iso` for process-backed `xorriso` streaming helpers
- `arc_core.imports` for future streamed tar bulk-ingest helpers

See `ADAPTATION_NOTES.md` for the exact placement and intended wiring.


## Clean transplants from the previous version

This scaffold now includes a few self-contained donors from the previous codebase, adapted into the clean MVP layout:

- `arc_core.fs_paths` for path normalization and safe filesystem cleanup
- `arc_core.hashing` for deterministic file and tree hashing
- `arc_core.crypto_age` for age-oriented size math and stream encrypt/decrypt helpers
- `arc_core.archive_artifacts` and `arc_core.proofs` for per-collection hash manifest and proof generation
- `arc_core.webhooks` for image-ready webhook reminder delivery
- `arc_api.auth` for optional bearer-token protection via `ARC_API_TOKEN`
- `arc_core.sqlite_db` for SQLite WAL / foreign key session helpers under the optional `db` extra

Collection-hash artifacts are intentionally present as a capability in the clean skeleton, not yet wired into a persistence flow.
The webhook reminder service is similarly generic and image-oriented, ready to be backed by a repository when real image planning and ISO readiness are implemented.
