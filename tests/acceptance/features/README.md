Acceptance feature suite conventions
===================================

These `.feature` files are the normative external contract for the MVP.

Conventions:

- Feature files describe externally visible behavior only.
- Scenario titles should remain stable even if implementation details change.
- Step wording is intentionally repetitive where it protects exact semantics, especially for:
  - selector validity
  - pin/release exact-target behavior
  - archived vs hot coverage
  - fetch lifecycle and hash verification
- `arc` and `arc-disc` acceptance cases are contract tests for CLI behavior, not internal command structure.
- The placeholder pytest modules in `tests/acceptance/` are intentionally thin; they should later be replaced by one concrete test module per feature file or by a small adapter layer that maps scenarios to pytest tests.

Important implementation note:

- If hot reconciliation after release is eventually consistent, concrete tests should use a bounded helper such as
  `wait_until_hot_matches_pins()` before asserting the final hot set.
