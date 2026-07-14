# ADR-0012: Use Named Fetches And Explicit Hot Eviction

## Decision

Riverhog uses named fetches to declare operator recovery intent and
`riverhog hot evict` to remove compliant bytes from hot storage.

A fetch contains one or more target selectors. The operator can edit a draft
fetch, start it for the normal `djdan fetch` optical-media workflow, or start it
with fetch materialization for automatic archive materialization.

Hot eviction is separate from fetch creation. Eviction refuses any selected file
that does not have the required verified disc redundancy.

## Reason

Fetches are a clearer unit of thought than implicit hot-residency rules. They let
the operator name intent, build a manifest incrementally, and choose the
fulfillment path only when ready.

Eviction stays explicit and synchronous so Riverhog never treats cache cleanup
as recovery intent. Keeping the two operations separate also keeps list/show
views keyed by fetch id, which makes operator projections fast as the underlying
file set grows.
