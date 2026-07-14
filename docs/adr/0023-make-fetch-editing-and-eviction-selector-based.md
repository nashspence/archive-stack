# ADR-0023: Make Fetch Editing And Eviction Selector-Based

## Decision

Riverhog fetch editing and hot eviction operate on canonical projected-path
selectors.

Draft fetches can add or remove selectors. Started fetches are frozen. Hot
eviction accepts selectors too, but refuses any selected file that lacks the
required verified disc redundancy.

## Reason

Operators should use the same namespace for finding, fetching, and evicting
files. Exact selector handling keeps broad directory requests and narrow file
requests easy to reason about without hidden subtraction rules.
