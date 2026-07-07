# riverhog

## Linting

Run the canonical lint flow with `mise install` and `make lint`.

That lane runs repo-wide `ruff check .` and then runs strict `mypy` over `src`
and the deployed service app entrypoints through `mise x -- uv`. The project
toolchain is declared in `mise.toml`; Python project dependencies are declared in
`pyproject.toml` and resolved in `uv.lock`.

The hashed `requirements-runtime.txt`, `requirements-test.txt`, and
`requirements-service.txt` files are generated deployment exports for local
lanes and Docker images.

The Makefile expects `mise` on `PATH` and runs uv through the repo-selected
toolchain. If a host keeps `mise` somewhere else, pass it explicitly:

```bash
make lint MISE_BIN=/abs/path/to/mise
```

## Testing

For the fastest supported check, first run `make lint` and `make unit` in
separate terminals. The lint, unit, and spec lanes run locally in the same
locked `uv` environment.

If source, contract, or fixture edits are needed while the canonical spec lane
is still running, stop that lane first, make the edit, then restart it.
Continuing to edit code during an in-flight canonical lane makes that run
invalid. Use `make stop-spec` to send a clean interrupt to the local spec
harness.

Run the serial aggregate flow with `make test` when one command is more
convenient. That target runs lint first, then unit.

Run `make ruff` or `make mypy` to execute those atomic quality gates directly.
Run `make build-app`, `make build-test`, or `make build` to refresh the local Docker images.
Run `make bootstrap-garage` to apply the checked-in Garage bucket and key bootstrap.
Run the fixture-backed spec harness lane with `make spec`.
Run the unit lane with `make unit`.
Pass mypy or pytest selectors with `args='...'`.
The `.feature` files under `tests/acceptance/features` are the source of truth for those scenarios.
