# ADR-0027: Validate Optical Recovery on the Server

## Decision

Riverhog validates recovered optical bytes on the server side after `djdan` streams the expected recovery byte stream.

## Reason

The optical client should fulfill media reads; the archive service should own final correctness.
