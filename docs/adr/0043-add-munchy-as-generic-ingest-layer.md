# ADR-0043: Keep Media Ingest Separate From Custody

## Decision

Munchy is the generic media ingest and transcode layer. Jeb is the generic watched-drop collector. Riverhog accepts finished immutable collections and owns their custody, archive, retrieval, and hot-cache lifecycle.

The public repository contains generic Munchy profiles, runners, Jeb scheduling, fake examples, and Riverhog upload integration. Real devices, destinations, recipients, remotes, hostnames, and deployment overlays are downstream configuration.

## Reason

Media processing happens before custody and has different operational concerns from durable archive storage. A clean boundary lets Munchy and Jeb serve multiple deployments while Riverhog keeps one precise collection acceptance contract.
