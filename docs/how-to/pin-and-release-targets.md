# Pin and release selectors

Use the same projected-path selector format in API and CLI.

## Pin a whole collection

```text
photos-2024/
```

CLI example:

```bash
arc pin 'photos-2024/'
```

## Pin a directory subtree

```text
photos-2024/albums/japan/
```

CLI example:

```bash
arc pin 'photos-2024/albums/japan/'
```

## Pin a single file

```text
docs/tax/2022/invoice-123.pdf
```

CLI example:

```bash
arc pin 'docs/tax/2022/invoice-123.pdf'
```

## Pin a projected parent directory

```text
photos/
```

This selects every file projected beneath that hot-namespace directory, even when the files come from multiple
collections.

## Release a previously pinned target

```bash
arc release 'docs/tax/2022/'
```

## Notes

- Pin requests are exact-selector idempotent.
- Every exact pin creates or reuses one fetch manifest for that same selector.
- Release removes only the exact canonical selector pin.
- Releasing the last exact pin for a selector abandons that selector's fetch manifest.
- A file can remain hot after a release if another active pin still requires it.
