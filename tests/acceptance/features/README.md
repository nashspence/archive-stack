Acceptance feature suite conventions
===================================

These `.feature` files are the normative external contract for the MVP.

They are executed in two lanes:

- `tests/harness/test_prod_harness.py` runs the contract against the real production app and CLIs.
- `tests/harness/test_spec_harness.py` runs the same contract through the fixture-backed spec harness.

Conventions:

- Feature files describe externally visible behavior only.
- Scenario titles should remain stable even if implementation details change.
- Step wording is intentionally repetitive where it protects exact semantics, especially for:
  - selector validity
  - pin/release exact-selector behavior
  - archived vs hot coverage
  - fetch lifecycle and hash verification
- `arc` and `arc-disc` acceptance cases are contract tests for CLI behavior, not internal command structure.
- disc-media scenarios should validate against the machine-readable contracts in `contracts/disc/`, not duplicate ad hoc path and schema rules in steps.
- use `@xfail_contract` when the scenario is executable and backed in the spec harness, but the real prod harness still does not satisfy it
- use `@xfail_not_backed` when the Gherkin scenario is accepted into the contract before the prod harness fully backs it
- do not combine `@xfail_contract` and `@xfail_not_backed` on the same scenario
- `@xfail_not_backed` is strict in both lanes: if the harness starts passing it, the suite fails until the marker is cleaned up or reclassified
- `@xfail_contract` is strict in `tests/harness/test_prod_harness.py` and ignored in `tests/harness/test_spec_harness.py`
