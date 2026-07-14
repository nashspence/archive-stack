# Selector grammar

Riverhog uses one projected-path selector syntax for file queries, fetches, and hot
eviction.

A directory selector ends in `/` and selects every logical file under that prefix. A file
selector names exactly one projected logical file. Selectors are relative, case-sensitive,
and canonical: leading or repeated slashes and `.` or `..` segments are invalid. A bare
collection name must end in `/`.

## Valid examples

```text
photos/
photos-2024/raw/
photos-2024/albums/japan/img_0042.cr3
docs/tax/2022/invoice-123.pdf
```

## Invalid examples

```text
photos
/photos-2024/
photos//raw/
photos/./2024/
photos/../2024/
```
