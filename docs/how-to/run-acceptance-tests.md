# Run Acceptance Tests

The executable acceptance contract lives in the Gherkin feature files under
`tests/acceptance/features`.

## Preferred commands

Run the acceptance contract directly with pytest:

```bash
pytest tests/acceptance
```

The ISO scenarios invoke `xorriso`. If you want the full suite directly on the host, make sure `xorriso` is
installed first.

If `xorriso` is not present in a non-containerized local environment, ISO-producing scenarios are skipped rather than
failing with tool-not-found errors.

Run the same acceptance contract inside the deterministic test container:

```bash
./test acceptance
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

## What lives where

- `tests/acceptance/features/` contains the normative external scenarios.
- `tests/acceptance/test_bdd_acceptance.py` loads those features against the real production app and CLIs.
- `tests/integration/test_bdd_spec_harness.py` loads the same feature files against the fixture-backed spec harness.
- `tests/acceptance/conftest.py` and `tests/integration/conftest.py` select the real or fixture-backed system for the shared BDD steps.
- `contracts/disc/` holds the machine-readable ISO layout and YAML schema contracts that the acceptance scenarios verify directly.
- `tests/fixtures/bdd_steps.py` holds the shared step definitions used by both lanes.

## Readiness markers

- `@xfail_contract` means the fixture-backed spec harness executes the scenario, but the real production service container is still behind the contract.
- `@xfail_not_backed` means the Gherkin contract exists before the acceptance harness fully backs that scenario.
- `@xfail_not_backed` XPASSes are strict and fail the run so incomplete-backing markers get cleaned up promptly when the harness catches up.
- `@xfail_contract` is strict in the real acceptance lane and ignored in the fixture-backed integration lane.
