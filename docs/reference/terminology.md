# Terminology Contract

Riverhog's current user-facing vocabulary is tracked in
[`contracts/terminology/user-facing-terms.v1.json`](../../contracts/terminology/user-facing-terms.v1.json).

That contract is an inventory, not a full rename plan. It records terms exposed
through CLIs, API payloads, webhook contracts, service configs, and operator
docs, then marks each term as:

- `preferred` for ordinary operator-facing language.
- `machine_visible` for API/config/JSON terms that are acceptable but should not
  casually leak into prose.
- `needs_review` for exposed vocabulary that needs deliberate ontology cleanup.
- `internal_only` for implementation vocabulary that should not be newly exposed.

When adding a new operator-visible command, webhook field, config key, or major
API term, update the terminology contract in the same change. When renaming a
term, keep behavior and wire compatibility decisions in the owning API or CLI
contract, then update this inventory to reflect the chosen status.
