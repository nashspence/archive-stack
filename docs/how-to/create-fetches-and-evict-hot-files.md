# Create fetches and evict hot files

Use target selectors from the projected collection namespace. A selector can name a collection, directory, or file.

## Create and inspect a fetch

```bash
riverhog hot fetch create --name "tax records" "2026/tax/**"
riverhog hot fetch list
riverhog hot fetch show <fetch-id>
riverhog hot fetch files <fetch-id>
```

Add or remove selectors while the fetch is in `draft`:

```bash
riverhog hot fetch add <fetch-id> "2026/receipts/**"
riverhog hot fetch remove <fetch-id> "2026/tax/drafts/**"
```

## Start retrieval

```bash
riverhog hot fetch start <fetch-id>
riverhog hot fetch show <fetch-id>
```

Starting freezes the selector set. If every selected file is hot, the fetch completes immediately. Otherwise Riverhog requests the required collection archives and materializes only the missing selected files. Canceling an active fetch returns it to `draft`.

## Evict from hot storage

Preview the selection, then evict it:

```bash
riverhog hot evict --dry-run "2026/tax/**"
riverhog hot evict "2026/tax/**"
```

Riverhog accepts eviction only when every selected file belongs to a verified collection archive. Eviction changes hot-cache availability; it does not change collection identity or archive custody.
