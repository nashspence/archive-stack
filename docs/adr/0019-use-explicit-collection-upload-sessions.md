# ADR 0019: Use explicit collection upload sessions

## Status

Accepted.

## Context

Collection ingest flows through `arc`. Riverhog needs a
collection contract that:

- gives the client an explicit collection identity up front
- supports resumable direct uploads into SeaweedFS
- keeps collection visibility separate from in-progress upload state
- preserves the existing prefix-free collection-id namespace

## Decision

- collection ids are explicit client-supplied canonical ids
- collection ids may be slash-delimited, for example `tax/2022`
- collection ids remain prefix-free on path segments:
  - if `tax` exists, reject `tax/2022`
  - if `tax/2022` exists, reject `tax`
- collection ingest uses a collection-upload session plus one resumable upload resource per logical file
- collection-upload sessions persist enough state to survive service restart and repeated CLI runs
- collection-upload files use the same `INCOMPLETE_UPLOAD_TTL` expiry model as fetch recovery uploads
- a collection remains invisible until every required file upload completes and Riverhog verifies the advertised hashes
- the terminal successful collection-file upload chunk auto-finalizes the collection without a second explicit completion operation
- once the last resumable collection-file state expires, Riverhog forgets the collection-upload session instead of keeping an empty pending record
- optional ingest-source metadata is descriptive only and is never part of collection identity

## Consequences

- `arc upload COLLECTION_ID ROOT` becomes the canonical collection-ingest command
- slash-bearing collection ids are first-class throughout API, CLI, planner, search, and file introspection
- collection ingest and fetch recovery share one resumable-upload lifecycle model
