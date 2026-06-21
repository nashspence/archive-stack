# Problem space

The archival service manages content across a hot materialized cache and cold optical media. Users stage a directory
and close it into a collection. Collections are the user-facing unit, but collections can be large and can span
multiple images and physical copies.

The main tension is this: users think in collections, while fetch and eviction need to happen at file or subtree
granularity. Requiring whole-collection recovery is too coarse. Letting users mutate the hot directory tree directly
introduces ambiguity and makes the UI and operational model harder to reason about.

The product goal is therefore:

- preserve collections as the logical namespace
- permit fetch and eviction at collection, directory, and file granularity
- keep the web UI focused on search, summaries, and actions rather than browsing
- make hot availability a derived surface rather than a writable source of intent
- keep authoritative archive state durable across service restarts

That means the system should treat the API state and catalog as authoritative,
and the committed hot namespace as a read-only result of that state.

## MVP non-goals

The MVP deliberately does not include:

- a full web file browser
- direct user mutation of the committed hot namespace
- exposing internal database schema as part of the public contract
