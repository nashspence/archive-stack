# riverhog

## Linting

Run the canonical lint flow with `make lint`.

That lane runs `ruff check .` and then runs strict `mypy` in a local locked `uv`
environment built from `requirements-test.txt` plus the editable project.
Runtime container dependencies are locked separately in `requirements-runtime.txt`.

## Testing

For the fastest full check, first run `make lint` and `make unit` (1m) in separate terminals.
Once you've cleared those shorter lanes, only then run `make spec` (5m) and
`make prod` (7m) in separate terminals. The lint, unit, and spec lanes run
locally in the same locked `uv` environment, and the prod-backed lane stays on
the checked-in Compose surface with per-run project names and ephemeral host
ports, plus project-scoped harness state and workspaces under `.compose/`.
Successful isolated prod-backed runs remove their generated `.compose/` state;
explicit shared project runs keep it. There is no supported override for this
state root; use `TEST_COMPOSE_PROJECT_NAME` when you need deliberate reuse.

Run the serial aggregate flow with `make test` when one command is more
convenient. That target runs lint first, then the unit, spec, and prod-backed
acceptance lanes.

Run `make ruff` or `make mypy` to execute those atomic quality gates directly.
Run `make build-app`, `make build-test`, or `make build` to refresh the local Docker images.
Run `make bootstrap-garage` to apply the checked-in Garage bucket and key bootstrap.
Run the production-backed harness against the executable acceptance contract with `make prod`.
Profile the production-backed harness with `make prod-profile`.
Run the fixture-backed spec harness lane with `make spec`.
Run the unit lane with `make unit`.
Pass mypy or pytest selectors with `args='...'`.
The `.feature` files under `tests/acceptance/features` are the source of truth for those scenarios.
