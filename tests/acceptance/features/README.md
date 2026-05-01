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
- operator notification scenarios should validate action-needed payload shape against the machine-readable contracts in `contracts/operator/`.
- normal human-copy scenarios should reference exact copy functions in `contracts/operator/copy.py`; formatting helpers live in `contracts/operator/format.py`.
- use `@ci_opt_in` with the appropriate `@requires_<capability>` marker when a scenario is excluded from the default prod-backed harness
- use capability markers for the real opt-in boundary, not for built-in fixture mechanics
- add a matching `@issue_<number>` marker when an opt-in, todo, or contract-gap scenario implies remaining work
- use `@todo` when the scenario exists before executable backing exists
- use `@contract_gap` when the scenario is executable and backed in the spec harness, but the real prod harness still does not satisfy it
- `@contract_gap` is strict in `tests/harness/test_prod_harness.py` and ignored in `tests/harness/test_spec_harness.py`
