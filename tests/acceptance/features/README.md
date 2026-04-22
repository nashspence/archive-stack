Acceptance feature suite conventions
===================================

These `.feature` files are the normative external contract for the MVP.

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
- use `@xfail_contract` on scenarios that describe accepted spec behavior before implementation catches up
