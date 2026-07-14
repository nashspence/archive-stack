Acceptance feature suite conventions
===================================

These `.feature` files are the normative external contract for the MVP.

They are executed by the fixture-backed spec harness:

- `tests/harness/test_spec_harness.py` loads every feature file through the fixture-backed spec harness.

Conventions:

- Feature files describe externally visible behavior only.
- Scenario titles should remain stable even if implementation details change.
- Step wording is intentionally repetitive where it protects exact semantics, especially for:
  - selector validity
  - fetch-editing and hot-eviction selector behavior
  - disc coverage vs hot availability
  - fetch lifecycle and hash verification
- `riverhog` and `djdan` acceptance cases are contract tests for CLI behavior, not internal command structure.
- disc-media scenarios should validate against the machine-readable contracts in `contracts/disc/`, not duplicate ad hoc path and schema rules in steps.
