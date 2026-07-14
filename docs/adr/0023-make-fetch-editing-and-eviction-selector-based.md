# ADR-0023: Make Fetch Editing And Eviction Selector-Based

## Decision

Riverhog fetch editing and hot eviction operate on canonical target selectors.

Draft fetches can add or remove selectors. Started fetches are frozen. Eviction accepts the same selectors and requires verified collection archive coverage for every selected file.

## Reason

Operators should use one namespace for finding, fetching, downloading, and evicting files. Exact selector handling keeps directory and file requests predictable without a second path vocabulary.
