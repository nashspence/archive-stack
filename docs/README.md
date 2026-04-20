# Documentation

This repository uses a split between proposal documents, durable product/architecture docs, reference docs,
architecture decision records, and historical migration notes.

## Layout

- `rfcs/` — proposals that explain the problem, constraints, and the chosen direction before the design is fully settled
- `explanation/` — durable conceptual documentation explaining why the system exists and how it works
- `reference/` — normative behavior, contracts, grammars, and state models
- `adr/` — one significant architectural or behavioral decision per file
- `how-to/` — task-oriented usage guides
- `archive/` — historical notes kept for context but not treated as current source of truth

## Source of truth

- The current product and architecture story lives in `explanation/`
- The current external contract lives in `reference/` and `openapi/arc.v1.yaml`
- The project decision log lives in `adr/`
- Historical donor/transplant notes live in `archive/`

## Initial migration from the working notes

The original sequential notes were reorganized as follows:

- `0_PROBLEM.md` → RFC context + `explanation/problem-space.md`
- `1_ANSWER.md` → RFC proposal + `explanation/architecture-overview.md`
- `2_API_PLAN.md` → `reference/selector-grammar.md`, `reference/domain-model.md`, `reference/api.md`
- `3_API_COMMITMENT.md` → `reference/api.md`, `reference/fetch-state-machine.md`, `openapi/arc.v1.yaml`, `tests/acceptance/test_mvp_contract.py`
- `4_IMPLEMENTATION_SCAFFOLDING.md` → `explanation/codebase-layout.md` + ADRs
- `5_ADAPTATION_NOTES.md` and `6_TRANSPLANT_NOTES.md` → `archive/`

## Conventions

- Use numbered IDs only for RFCs and ADRs.
- Keep stable file names based on topic, not on drafting sequence.
- Put normative API behavior in reference docs and machine-readable specs.
- Keep executable acceptance criteria under `tests/acceptance/`.
