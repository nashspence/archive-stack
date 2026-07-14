# ADR-0021: Treat Hot Availability As Derived

## Decision

Riverhog treats hot availability as a materialized projection of catalog state,
not as source-of-truth state.

The projection is driven by collection archive state, verified disc coverage,
named fetches, recovery upload state, and the actual committed hot-file
namespace.

## Reason

Hot bytes can be browsed, downloaded, evicted, lost, or rebuilt, but archive
membership and operator recovery intent must remain unambiguous.

Derived hot availability lets Riverhog answer operator list/show/search
commands quickly without scanning unbounded file history. It also lets recovery
workers repair missing hot files from the right source: queued `djdan fetch`
work when verified discs exist, or fetch fetch materialization when
the selected bytes must come from the archive.
