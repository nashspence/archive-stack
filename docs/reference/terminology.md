# Terminology Contract

Riverhog's current user-facing vocabulary is tracked in
[`contracts/terminology/user-facing-terms.v1.json`](../../contracts/terminology/user-facing-terms.v1.json).

That contract is an inventory, not a full rename plan and not a work tracker. It
records terms exposed through CLIs, API payloads, webhook contracts, service
configs, and operator docs. It deliberately does not mark review state,
priority, ownership, or tracker links; work items should reference contract
terms, not the other way around.

Each term includes a `term_type` that describes the kind of concept being
exposed: `entity`, `activity`, `state`, `policy`, `identifier`,
`metadata_property`, `enum_value`, or `software_agent`. `Riverhog`, `Munchy`,
`Jeb`, and `Gogurt` are intentional named system or agent terms and should use
`software_agent`.

When adding a new operator-visible command, webhook field, config key, or major
API term, update the terminology contract in the same change. When renaming a
term, update the owning API or CLI contract and this inventory together.
