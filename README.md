# riverhog

## Linting

Run the canonical lint flow with `make lint`.

That lane runs `ruff check .` and then runs strict `mypy` in a local locked `uv`
environment built from `requirements-test.txt` plus the editable project.
Runtime container dependencies are locked separately in `requirements-runtime.txt`.

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
