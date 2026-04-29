# riverhog

## Linting

Run the canonical lint flow with `./test lint`.

That lane runs `ruff check .` and then runs strict `mypy` in a local locked `uv`
environment built from `requirements-test.txt` plus the editable project.

## Testing

For the fastest full check, run `./test lint`, `./test unit`, `./test spec`,
and `./test prod` in separate terminals. The lint, unit, and spec lanes run
locally in the same locked `uv` environment, and the prod-backed lane stays on
the checked-in Compose surface.

Run the supported serial aggregate flow with `./test` when one command is more
convenient. That wrapper runs lint first, then the unit, spec, and prod-backed
acceptance lanes.

Run the production-backed harness against the executable acceptance contract with `./test prod`.
Profile the production-backed harness with `./test prod-profile`.
Run the fixture-backed spec harness lane with `./test spec`.
Run the unit lane with `./test unit`.
Run the non-production lanes together with `./test fast`.
The `.feature` files under `tests/acceptance/features` are the source of truth for those scenarios.
