# ADR 0011: Use tus-compatible resumable upload resources for fetches

## Status

Accepted.

## Context

Fetch fulfillment now requires direct streaming from optical media into the server without first materializing complete
files on local disk. Some logical files may exceed 100 GB and may span multiple discs. Recovery therefore needs:

- reliable offset-based resume
- upload expiry for incomplete partial server-side state
- checksum verification
- good progress reporting

The transport should be commodity rather than bespoke when the protocol already exists and fits the problem.

## Decision

- each fetch-manifest entry uses one resumable upload resource
- the JSON API creates or resumes that resource for the caller and returns its upload URL
- the upload URL speaks tus-compatible resumable upload semantics
- the required protocol surface is tus core plus checksum and expiration support
- `arc-disc` streams plaintext directly from disc recovery into that upload resource
- split logical files stream into the same upload resource in ascending part order
- if temporary buffering is needed internally, it uses conventional temporary storage as an implementation detail only

## Consequences

- reliable direct-from-disc resume does not require reimplementing upload offsets, expiry, and checksum negotiation
- the fetch manifest remains the domain contract while tus handles byte transport
- implementation still owns domain-specific binding of uploads to manifest entries, final hash validation, and pin-based
  cleanup
