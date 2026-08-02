# Persistent-state fixtures

Each directory is an immutable released-state baseline. Do not edit a baseline
after it lands; add a new directory for the next baseline.

The SQLite files are portable SQL dumps. The Riverhog fixture is PostgreSQL DDL
plus representative data. Fixture data is deliberately fake and covers the
semantic state that migrations must preserve. Tests restore each fixture, run it
forward to the packaged migration head, validate the exact current schema, and
then exercise the preserved records.

The `v1_0001` fixtures cover:

- Riverhog application-key authorization, lifecycle events, encrypted archive
  object identity, retrieval-cache leases, indexes, and constraints;
- Munchy application-key authorization and lifecycle-event cursors; and
- Jeb source configuration, batch/file summary triggers, attempts, and
  lifecycle-event cursors;
- Riverhog local collection selection, materialization identity, and retrieval
  progress; and
- Mango Fish CloudEvents source cursors.
