# Problem space

Riverhog manages immutable collections across durable remote archives and a fast materialized cache. Collections are the custody unit, while operators often need to search, fetch, or evict one file or subtree.

The product goals are:

- preserve collections as the logical and durable namespace
- archive every accepted collection as a verified encrypted remote object
- permit fetch and eviction at collection, directory, and file granularity
- keep hot availability explicit, observable, and recoverable
- expose a read-only committed namespace
- make remote archive account access and retrieval readiness an operational priority

The catalog records authoritative identity, manifests, archive verification, and current materialization state. Object stores hold staged, hot, and archived bytes according to those records.

## Product boundaries

Riverhog provides custody, search, retrieval, and cache management. Munchy handles media processing, Jeb handles watched-drop scheduling, and Gogurt provides the web interface. Real devices, accounts, destinations, and deployment topology are supplied by downstream configuration.
