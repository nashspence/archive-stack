# CLI design

Riverhog-family commands keep product boundaries explicit:

- `riverhog` manages collections, search, fetches, and hot-cache eviction.
- `munchy` manages generic media ingest and runner jobs.
- `jeb` manages watched-drop collection and scheduling.
- `gogurt` serves the web interface.

## Riverhog command shape

```text
riverhog collection upload|watch|cancel|list|show
riverhog find
riverhog hot evict
riverhog hot fetch create|add|remove|list|show|files|start|cancel
```

Collection commands use collection ids. Fetch commands use fetch ids. Search, fetch selection, download, and eviction share canonical target selectors.

## Output

Human output leads with the entity and state, uses concise field labels, and reports byte quantities in readable units. `--json` emits the API-shaped machine payload without styling. List commands expose explicit pagination, filters, sort, and order.

Dry runs state the intended operation and resolved selection without making API or storage changes.
