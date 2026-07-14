# ADR-0021: Catalog Hot Availability

## Decision

Riverhog records hot availability for each logical file and changes that state only when verified bytes are published or explicitly evicted.

Search, collection summaries, fetches, downloads, and read-only browsing use this cataloged state. Storage checks verify that the corresponding committed object exists before serving bytes.

## Reason

Operators need one consistent answer about whether a file is immediately available. Cataloged availability supports efficient queries, while object verification prevents metadata alone from claiming custody of missing bytes.
