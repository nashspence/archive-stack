# Selector grammar

The same canonical target syntax is used in API and CLI.

## Canonical syntax

```text
<collection>
<collection>:/dir/
<collection>:/dir/file.ext
```

## Meaning

- `<collection>` targets the entire collection
- `<collection>:/dir/` targets every file whose logical path begins with `/dir/`
- `<collection>:/dir/file.ext` targets exactly one file

## Normalization rules

1. The collection id is case-sensitive.
2. Paths are absolute within the collection and must begin with `/` when present.
3. Directory targets must end with `/`.
4. File targets must not end with `/`.
5. Empty path after `:` is invalid.
6. `.` and `..` path segments are invalid.
7. Repeated `/` separators are rejected for MVP.
8. API and CLI preserve and echo canonical targets in canonical form.

## Valid examples

```text
photos-2024
photos-2024:/raw/
photos-2024:/albums/japan/img_0042.cr3
```

## Invalid examples

```text
photos-2024:
photos-2024:raw/
photos-2024:/raw
photos-2024:/a/../b
```
