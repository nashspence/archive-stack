# ADR 0017: Add file introspection endpoints for collection listing, target query, and hot content download

## Status

Accepted.

## Context

Users need direct visibility into file availability after a collection is closed. A collection may contain many logical files, and each file may be hot, archived, or available through registered recovery copies. Users need to inspect that state without relying on planner output, internal storage layout, or recovery tooling.

The same target selector should mean the same projected file set across the API and CLI. File discovery, pinning, release, and retrieval should therefore share one user-facing selection model.

## Decision

- collections expose a file listing that includes each logical file path and its current hot or archived state
- files can be queried by target selector using the same canonical selector model used by pin and release
- missing or non-matching target queries return an empty result, because “no matching files” is a valid state
- file listing and target-query responses use paged `page` / `per_page` / `total` / `pages` metadata rather than unbounded result sets
- hot file content can be downloaded directly
- content download applies only to file targets, not directory targets
- archived-only files are not downloaded through the hot-content path and continue to use the fetch/upload recovery flow
- file results expose availability in terms users can act on, including hot state and available copies when applicable
- plan visibility remains available through the existing plan endpoint
- the CLI exposes these capabilities through `arc show --files`, `arc status`, and `arc get`
- collection file listing uses a dedicated `/v1/collection-files/{collection_id}` path so slash-bearing ids remain first-class without reserving `files` as a collection-id segment

## Consequences

- users can answer “what files are in this collection?”, “is this file hot?”, and “can I get it now?” without knowing storage internals
- target-based file discovery is consistent with search, pin, release, and other target-oriented workflows
- the file introspection surface stays usable as collections and directory selectors grow
- hot files are easy to retrieve when they are immediately available
- archived-only files retain a clear recovery path instead of appearing locally downloadable
- collection lookup continues to support slash-bearing collection ids
- the API and CLI expose file state as part of the product surface, not as an implementation detail
