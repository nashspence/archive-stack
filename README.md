# riverhog

A clean starter skeleton for the riverhog described in the contract:

- FastAPI HTTP API
- `arc` CLI
- `arc-disc` CLI
- shared core domain and service interfaces
- executable pytest-bdd acceptance scenarios and route/command scaffolding

## What is implemented

- package layout
- selector parsing and canonicalization
- domain types, errors, and models
- API schemas and route signatures
- exception mapping
- shared HTTP client
- Typer CLIs with `--json` output mode
- unit tests for selector parsing and API/CLI smoke coverage
- executable pytest-bdd acceptance scenarios for the contract

## What is intentionally stubbed

Business services currently raise `NotYetImplemented`. This keeps the boundary between
contract and implementation crisp while giving you a runnable app and installable CLIs.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev,db]
uvicorn arc_api.app:create_app --factory --reload
```

In another shell:

```bash
arc --help
arc-disc --help
```

## Testing

Run the full suite with `pytest` or `./test`.

Run the executable acceptance contract with `pytest tests/acceptance` or `./test acceptance`.
The `.feature` files under `tests/acceptance/features` are the source of truth for those scenarios.

## Environment variables

- `ARC_BASE_URL` default: `http://127.0.0.1:8000`
- `ARC_TOKEN` optional bearer token
- `ARC_SEAWEEDFS_FILER_URL` default: `http://localhost:8888`
- `ARC_DB_PATH` default: `.arc/state.sqlite3`

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
