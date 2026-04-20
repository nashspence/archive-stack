# Pin and release targets

Use the same target string format in API and CLI.

## Pin a whole collection

```text
photos-2024
```

CLI example:

```bash
arc pin photos-2024
```

## Pin a directory subtree

```text
photos-2024:/albums/japan/
```

CLI example:

```bash
arc pin 'photos-2024:/albums/japan/'
```

## Pin a single file

```text
docs:/tax/2022/invoice-123.pdf
```

CLI example:

```bash
arc pin 'docs:/tax/2022/invoice-123.pdf'
```

## Release a previously pinned target

```bash
arc release 'docs:/tax/2022/'
```

## Notes

- Pin requests are exact-target idempotent.
- Release removes only the exact canonical target pin.
- A file can remain hot after a release if another active pin still requires it.
