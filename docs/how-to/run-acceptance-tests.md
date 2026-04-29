# Run Acceptance Tests

The executable acceptance contract lives in the Gherkin feature files under
`tests/acceptance/features`.

## Preferred commands

Run the last full check in separate terminals:

```bash
make lint
make unit
make spec
make prod
```

That keeps the local lanes and the prod-backed lane visible in parallel output
windows without changing the checked-in test surfaces.

Run the same acceptance contract inside the deterministic test container:

```bash
make prod
```

That path keeps the prod-backed `pytest` run in the canonical test container
while `docker compose` manages the checked-in `app` service and its storage
sidecars outside the container.

Do not run the production-backed harness with direct `pytest`. The supported
entrypoints are `make prod`, `make prod-profile`, or `make test`, which prepare
the compose-managed app and sidecars the harness expects.

Run the production-backed harness lane with built-in timing output for scenario and fixture hotspots:

```bash
make prod-profile
```

Run the fixture-backed spec harness lane against the same contract from a local
locked `uv` environment:

```bash
make spec
```

Run the unit lane by itself:

```bash
make unit
```

Run the atomic image build targets when you need a fresh local app or test
image before the prod-backed lane:

```bash
make build-app
make build-test
```

Run the Garage bootstrap on its own when you want the checked-in buckets and
keys prepared without running the prod harness yet:

```bash
make bootstrap-garage
```

The non-production lanes resolve against the checked-in `requirements-test.txt`
set plus the editable project, so they do not require Docker or BuildKit.

Run the serial aggregate target with `make test` when you want one supported
command to run lint, then unit, spec, and prod in order.

## Compose-backed sidecars

The checked-in test scripts read `./.env.compose` when present, otherwise they fall
back to `./.env.compose.example`.

For prod-backed lanes, `make prod`, `make prod-profile`, and `make test` also
load the short recovery timing overrides
from `tests/harness/prod-harness.env`. That keeps the checked-in compose env
product-facing while still giving the acceptance harness the smaller timing
window it needs.

Each prod-backed `make ...` invocation uses its own Compose project name by
default so independent prod-backed runs do not tear down each other's one-off
containers, networks, or sidecars.

If you need to reuse one Compose project explicitly, export
`TEST_COMPOSE_PROJECT_NAME` before running `make`.
Do that before `make bootstrap-garage` or `make down` as well when you want
those standalone targets to act on the same compose-managed stack.


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
