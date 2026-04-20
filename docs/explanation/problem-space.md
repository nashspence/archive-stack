# Problem space

The archival service manages content across a hot materialized cache and cold optical media. Users stage a directory
and close it into a collection. Collections are the user-facing unit, but collections can be large and can span
multiple images and physical copies.

The main tension is this: users think in collections, while restore and eviction need to happen at file or subtree
granularity. Requiring whole-collection restore is too coarse. Letting users mutate the hot directory tree directly
introduces ambiguity and makes the UI and operational model harder to reason about.

The product goal is therefore:

- preserve collections as the logical namespace
- permit restore and release at collection, directory, and file granularity
- keep the web UI focused on search, summaries, and actions rather than browsing
- make hot storage a projection of system state rather than a writable source of intent

That means the system should treat the API state and catalog as authoritative, and the visible hot tree as generated.
