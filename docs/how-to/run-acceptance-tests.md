# Run Acceptance Tests

The executable acceptance contract lives in the Gherkin feature files under
`tests/acceptance/features`.

## Preferred Commands

Run the normal local verification flow:

```bash
make lint
make unit
```

`make lint` runs `ruff check .` and strict `mypy` in the locked local `uv`
environment. `make unit` runs the supported unit test lane.

Run the fixture-backed executable spec harness separately when you are working
on acceptance contracts:

```bash
make spec
```

If the spec harness is running and you need to change source, contracts,
features, fixtures, or harness code, stop it first, make the edit, then restart
it. A canonical run is only valid for the checkout that existed when the lane
started.

```bash
make stop-spec
```

Run the serial aggregate target when one command is more convenient:

```bash
make test
```

That target runs lint, then unit.

Forward pytest selectors or other pytest arguments with `args`:

```bash
make spec args='-k server_rejects_incorrect_recovered_bytes'
make unit args='tests/unit/test_planning_service.py'
```

Run the atomic image build targets when you need fresh local app or test images:

```bash
make build-app
make build-test
make build
```

Run the Garage bootstrap on its own when you want the checked-in buckets and keys
prepared for a local Compose stack:

```bash
make bootstrap-garage
```

The local lanes resolve against `requirements-test.txt` plus the editable
project.

## What Lives Where

- `tests/acceptance/features/` contains the normative external scenarios.
- `tests/harness/test_spec_harness.py` loads those feature files against the
  fixture-backed spec harness.
- `contracts/disc/` holds the machine-readable ISO layout and YAML schema
  contracts that acceptance scenarios verify directly.
- `tests/fixtures/bdd_steps.py` holds the shared step definitions used by the
  spec harness.

## Feature Conventions

- Feature files describe externally visible behavior only.
- Scenario titles should remain stable even if implementation details change.
- Step wording is intentionally repetitive where it protects exact semantics.
- `riverhog` and `djdan` acceptance cases are contract tests for CLI behavior,
  not internal command structure.
- Disc-media scenarios should validate against the machine-readable contracts in
  `contracts/disc/`, not duplicate ad hoc path and schema rules in steps.
