# ADR-0012: Use Named Fetches And Explicit Hot Eviction

## Decision

Riverhog uses named fetches to declare retrieval intent and `riverhog hot evict` to remove selected files from hot storage.

A fetch contains target selectors. Draft fetches can be edited; starting a fetch freezes its selectors and automatically materializes missing files from their collection archives.

Hot eviction is separate from fetch creation and requires a verified collection archive for every selected file.

## Reason

A named fetch is a durable, inspectable unit for retrieval. Explicit eviction keeps cache management separate from retrieval intent and ensures that every removed hot byte remains recoverable from verified remote storage.
