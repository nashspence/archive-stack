# ADR 0004: Reuse active fetches for the same exact target

## Status

Accepted.

## Context

Repeated requests to restore the same cold archived target should not create duplicate in-flight work.

## Decision

If there is an existing non-`failed`, non-`done` fetch for the same exact target, return that fetch instead of creating a
new one.

## Consequences

- repeated pin requests for the same cold exact target converge on one active fetch
- the system avoids duplicate recovery jobs
