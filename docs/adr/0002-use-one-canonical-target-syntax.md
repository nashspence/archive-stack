# ADR 0002: Use one canonical target syntax in API and CLI

## Status

Accepted.

## Context

The system needs a compact way to address a whole collection, a directory subtree, or a single file without introducing a
full browsing interface.

## Decision

Use one target string syntax everywhere:

```text
<collection>
<collection>:/dir/
<collection>:/dir/file.ext
```

Reject `.` and `..` segments and repeated `/` separators for MVP.

## Consequences

- API and CLI can share selector parsing and normalization
- search results can be fed directly into `pin` and `release`
- acceptance tests can assert selector behavior uniformly
