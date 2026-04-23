# ADR 0013: Use conventional listing controls for plan and images

## Status

Accepted.

## Context

The API now has two operator-facing listing surfaces:

- `GET /v1/plan` for provisional planner candidates
- `GET /v1/images` for finalized images

Operators and API clients benefit when those listing surfaces feel similar to page through, sort, and filter. At the
same time, provisional candidates and finalized images are distinct nouns with distinct lifecycles and must not be
collapsed into one shared representation.

## Decision

- `GET /v1/plan` and `GET /v1/images` both use conventional query params for listing controls:
  - `page`
  - `per_page`
  - `sort`
  - `order`
- both responses expose conventional pagination metadata:
  - `page`
  - `per_page`
  - `total`
  - `pages`
  - `sort`
  - `order`
- `GET /v1/plan` remains provisional and uses noun-specific result and filter semantics:
  - the result array is named `candidates`
  - candidate objects expose `candidate_id`, not finalized image ids
  - candidate listing supports `q`, `collection`, and `iso_ready`
  - `q` matches candidate-oriented material such as candidate ids, contained collection ids, and represented
    projected file paths
  - default ordering remains planner-friendly with fullest candidates first
- `GET /v1/images` remains finalized and uses finalized-image-specific nouns and filters
- plan-specific metadata such as `ready`, `target_bytes`, `min_fill_bytes`, and `unplanned_bytes` remains part of the
  `GET /v1/plan` contract even though the candidate list mechanics become more conventional
- CLI parity follows the same rule:
  - `arc plan` and `arc images` use parallel paging/sort/filter conventions
  - each command still talks in the nouns of its own surface

## Consequences

- operators can learn one general listing model across provisional and finalized views
- the contract becomes easier for thin clients to reason about
- provisional and finalized lifecycles stay explicit because the result arrays, identifiers, and summary fields remain
  noun-specific
- collection-age-based plan sorting or filtering is deferred until collection age metadata is part of the accepted
  contract
