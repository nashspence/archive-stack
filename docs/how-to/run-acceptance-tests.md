# Run Acceptance Tests

The executable acceptance contract lives in the Gherkin feature files under
`tests/acceptance/features`.

## Preferred commands

Run the same acceptance contract inside the deterministic test container:

```bash
./test prod
```

That path now keeps `pytest` in the canonical test container while `docker compose`
manages the checked-in `app` service and its storage sidecars outside the
container.

Do not run the production-backed harness with direct `pytest`. The supported
entrypoints are `./test prod`, `./test prod-profile`, or the canonical `./test`,
which prepare the compose-managed app and sidecars the harness expects.

Run the production-backed harness lane with built-in timing output for scenario and fixture hotspots:

```bash
./test prod-profile
```

Run the fixture-backed spec harness lane against the same contract:

```bash
./test spec
```

Run the unit lane by itself:

```bash
./test unit
```

Run the non-production lanes together:

```bash
./test fast
```

## Compose-backed sidecars

The canonical `./test` flow reads `./.env.compose` when present, otherwise it falls
back to `./.env.compose.example`.

For prod-backed lanes, `./test` also loads the short recovery timing overrides
from `tests/harness/prod-harness.env`. That keeps the checked-in compose env
product-facing while still giving the acceptance harness the smaller timing
window it needs.

Each `./test ...` invocation uses its own Compose project name by default so
`./test spec` and `./test prod` can run side by side without tearing down each
other's one-off containers, networks, or sidecars.

If you need to reuse one Compose project explicitly, export
`TEST_COMPOSE_PROJECT_NAME` before running `./test`.


## What lives where

- `tests/acceptance/features/` contains the normative external scenarios.
- `tests/harness/test_prod_harness.py` loads those features against the real production app and CLIs.
- `tests/harness/test_spec_harness.py` loads the same feature files against the fixture-backed spec harness.
- `contracts/disc/` holds the machine-readable ISO layout and YAML schema contracts that the acceptance scenarios verify directly.
- `tests/fixtures/bdd_steps.py` holds the shared step definitions used by both lanes.

## Readiness markers

- `@xfail_contract` means the fixture-backed spec harness executes the scenario, but the prod harness is still behind the contract.
- `@xfail_not_backed` means the Gherkin contract exists before the prod harness fully backs that scenario.
- `@xfail_not_backed` XPASSes are strict and fail the run so incomplete-backing markers get cleaned up promptly when the harness catches up.
- `@xfail_contract` is strict in the prod harness and ignored in the spec harness.
