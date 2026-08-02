# Persistent-state contract

Riverhog repository applications use one convention for relational state: one physical
database has one owning application, one linear Alembic history, and one current migration
head. Schema changes roll forward through an explicit administrative command. Normal
service startup validates the revision and exact schema without creating tables, stamping a
revision, or applying a migration.

This is the v1 contract. Databases created by pre-v1, unversioned code are not supported
inputs. Before adopting v1, replace those databases or perform a one-off operator-controlled
conversion and verification. Compatibility code for unversioned layouts does not belong in
the applications.

## State owners and commands

| Owner | Durable state | Administrative command |
| --- | --- | --- |
| Riverhog server | PostgreSQL catalog | `riverhog-api state status|upgrade|verify` |
| Munchy server | Munchy SQLite database | `munchy-server state status|upgrade|verify` |
| Jeb server | Jeb SQLite database | `jeb-service state status|upgrade|verify` |
| Riverhog client | `riverhog local` SQLite database | `riverhog local state status|upgrade|verify` |
| Mango Fish | CloudEvents cursor SQLite database | `mango-fish --config FILE state status|upgrade|verify` |

`status` is read-only and reports `empty`, `current`, `upgrade_required`, `unversioned`, or
`incompatible`. `upgrade` is the only schema-changing operation. It creates a clean current
database or applies every pending forward revision. `verify` requires the current revision
and checks database integrity plus the application's complete table, column, index,
constraint, and trigger contract.

Compose deployments run `state upgrade` as a one-shot service and start the long-running
service only after it succeeds. Other deployment systems must preserve that separation.

## Durable state, projections, and caches

The databases above are durable operational state and must be backed up. Munchy's template
registry snapshot is an explicit backup/restore artifact. Jeb's FTP projection and
Riverhog local's tag projections are derived from their owning databases and can be rebuilt.

The Munchy file-hash and media-preflight SQLite files are disposable caches. Each carries a
cache-format version and rebuilds when that version changes; it has no migration history and
must never become authoritative. Object-store archives and their portable manifests are a
separate, independently recoverable format. Relational schema revisions do not rewrite
archive objects.

## Backup, upgrade, and rollback

Before an upgrade, stop or quiesce writers and take a consistent backup. Use a transactional
PostgreSQL backup for the Riverhog catalog. Use SQLite's backup API, a quiesced file copy, or
an equivalent volume snapshot that includes the committed database state rather than copying
an active database while ignoring its WAL. Restore the backup to a separate location and run
the owner's `state verify` command against it before relying on it.

Migration revisions are forward-only and run transactionally per revision. A failed revision
must leave the last committed revision readable, and rerunning `state upgrade` is the recovery
path after correcting the cause. Service startup refuses empty, behind, unversioned,
incompatible, or structurally invalid state; it does not silently repair any of those cases.

There is no supported schema downgrade. Rolling application code back after its database was
upgraded means restoring the pre-upgrade database backup with the older application. Never
run an older binary against a newer revision, edit the revision table, or treat `stamp` as a
recovery operation. If integrity verification fails, preserve the failed database for
diagnosis and restore the last verified backup.

## Extending v1

Every later v1.x schema change must:

1. append one forward migration to the owning application's single history;
2. keep runtime startup validation-only and keep all schema DDL in the migration/bootstrap
   boundary;
3. leave existing fixture directories unchanged and add a new immutable baseline only when
   a release creates one;
4. run every earlier fixture forward to the new head and verify application semantics; and
5. prove interruption rollback and idempotent re-entry for the new operation.

The [immutable state fixtures](../tests/fixtures/state/README.md) begin with `v1_0001`. They
contain only fake public data. The fixture matrix verifies Riverhog authorization, lifecycle
events, encrypted archive identity, and retrieval leases; Munchy authorization and event
cursors; and Jeb sources, attempts, event cursors, constraints, indexes, and summary
triggers.
