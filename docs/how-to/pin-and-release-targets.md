# Pin and release selectors

Use the same projected-path selector format in API and CLI.

## Pin a whole collection

```text
photos-2024/
```

CLI example:

```bash
riverhog hot pin 'photos-2024/'
```

## Pin a directory subtree

```text
photos-2024/albums/japan/
```

CLI example:

```bash
riverhog hot pin 'photos-2024/albums/japan/'
```

## Pin a single file

```text
docs/tax/2022/invoice-123.pdf
```

CLI example:

```bash
riverhog hot pin 'docs/tax/2022/invoice-123.pdf'
```

## Pin a projected parent directory

```text
photos/
```

This selects every file projected beneath that hot-namespace directory, even when the files come from multiple
collections.

## Release a previously pinned target

```bash
riverhog hot unpin 'docs/tax/2022/'
```

## Notes

- Pin requests are exact-selector idempotent.
- Every exact pin creates or reuses one fetch manifest for that same selector.
- Release removes only the exact canonical selector pin.
- Release is allowed only when every selected file is fully compliant with the required verified disc copies.
- Releasing the last exact pin for a selector abandons that selector's fetch manifest and removes selected hot files that are no longer covered by another pin.
- Releasing a file pin removes only that file unless another active pin still requires it.
- Releasing a file that is covered only by a broader directory or collection pin does not subtract it from the broader pin.
