# Selector grammar

The same canonical projected-path selector syntax is used in API and CLI.

## Canonical syntax

```text
<projected-dir>/
<projected-file>
```

## Meaning

- `<projected-dir>/` targets every file whose projected hot path begins with that directory; it may span multiple
  collections
- `<projected-file>` targets exactly one projected file

## Normalization rules

1. Selectors operate over the projected hot namespace, not over staging paths or literal filesystem paths.
2. Selectors are relative to the projected hot root and must not begin with `/`.
3. Selector spelling is case-sensitive.
4. Directory selectors must end with `/`.
5. File selectors must not end with `/`.
6. Empty selectors are invalid.
7. `.` and `..` path segments are invalid.
8. Repeated `/` separators are rejected for MVP.
9. API and CLI preserve and echo canonical selectors in canonical form.

## Valid examples

```text
photos/
photos-2024/
photos-2024/raw/
photos-2024/albums/japan/img_0042.cr3
docs/tax/2022/invoice-123.pdf
```

## Invalid examples

```text
/photos-2024/
photos//
photos/./2024/
photos/../2024/
```
