# Run tests

## Preferred commands

```bash
make lint
make unit
make spec
make build
```

Use `make unit args='-k expression'` for focused iteration. `make spec` runs the assembled FastAPI contract harness.

The Makefile runs Python through the repository-selected `mise` and locked `uv` environment.

## Test layout

- `tests/unit/` covers focused domain, service, adapter, CLI, and contract behavior.
- `tests/harness/` exercises the assembled API with real service wiring and controlled storage adapters.
- `tests/fixtures/` contains generic positive fixtures.
- `contracts/openapi/`, `contracts/webhooks/`, and `contracts/terminology/` are checked machine contracts.

Tests describe current supported behavior. Use fake identities and storage endpoints; deployment-specific configuration belongs downstream.
