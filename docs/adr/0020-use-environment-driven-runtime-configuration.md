# ADR-0020: Use Environment-Driven Runtime Configuration

## Decision

Riverhog configures runtime storage, upload, archiving, recovery, WebDAV, and database behavior through environment variables.

## Reason

The same application needs to run against the checked-in local stack and production-like backends without changing code.
