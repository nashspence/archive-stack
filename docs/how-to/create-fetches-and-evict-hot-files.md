# Create fetches and evict hot files

Use the same projected-path selector format in API and CLI.

## Create a fetch

```bash
riverhog hot fetch create --name 'Japan album refresh'
```

## Add targets

Whole collection:

```bash
riverhog hot fetch add fx-1 'photos-2024/'
```

Directory subtree:

```bash
riverhog hot fetch add fx-1 'photos-2024/albums/japan/'
```

Single file:

```bash
riverhog hot fetch add fx-1 'docs/tax/2022/invoice-123.pdf'
```

Projected parent directory:

```bash
riverhog hot fetch add fx-1 'photos/'
```

This selects every file projected beneath that hot-namespace directory, even
when the files come from multiple collections.

## Inspect selection

Show the bounded preflight summary and next recommended action:

```bash
riverhog hot fetch show fx-1
```

List the selected files when you need to inspect or search the actual targets:

```bash
riverhog hot fetch files fx-1 --query japan --sort bytes --order desc
```

## Start fulfillment

Queue the fetch for the prompt-based optical-media workflow:

```bash
riverhog hot fetch start fx-1
djdan fetch
```

Start automatic cloud materialization:

```bash
riverhog hot fetch start fx-1 --cloud
riverhog hot fetch show fx-1
```

Cancel an active fetch, whether it was queued for `djdan` or cloud
materialization:

```bash
riverhog hot fetch cancel fx-1
```

## Evict hot files

```bash
riverhog hot evict 'docs/tax/2022/'
```

Eviction is allowed only when every selected file has the required verified disc
copies. It removes matching bytes from hot storage immediately and reports the
selected and evicted counts.

## Notes

- Fetches are named so the operator can remember why they exist.
- Draft fetches can be edited with `riverhog hot fetch add` and
  `riverhog hot fetch remove`.
- A fetch is frozen once queued to `djdan` or cloud-fetch.
- Fetch cancellation returns the fetch to draft when possible.
- `djdan fetch` with no id clears the queued djdan fetch backlog in one guided
  session.
