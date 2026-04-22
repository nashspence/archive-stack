# ADR 0002: Use one canonical projected-path selector syntax in API and CLI

## Status

Accepted.

## Context

The system needs one selector syntax that matches the projected hot namespace the user sees.

## Decision

Use one selector string syntax everywhere:

```text
<projected-dir>/
<projected-file>
```

- selectors are canonical relative paths beneath the projected hot root
- directory selectors end with `/`
- file selectors do not end with `/`
- collection-root selectors are just directory selectors rooted at the collection id, for example `docs/`
- projected parent directories such as `photos/` may span multiple collections
- reject leading `/`, `.` segments, `..` segments, repeated `/`, and equivalent non-canonical spellings

## Consequences

- API and CLI can share selector parsing and normalization
- search results can be fed directly into `pin` and `release`
- acceptance tests can assert selector behavior uniformly
- the projected hot namespace becomes the only selector namespace users need to learn
