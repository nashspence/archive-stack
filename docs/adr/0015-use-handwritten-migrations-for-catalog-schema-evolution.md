# ADR 0015: Use handwritten migrations for catalog schema evolution

## Status

Accepted.

## Context

The catalog database schema evolves as new columns and tables are introduced. Schema changes must
be applied on startup so the catalog remains consistent without requiring a full wipe.

## Decision

Schema migrations are applied on startup via a lightweight handwritten migration system:

1. A `schema_migrations` table records which migration versions have been applied.
2. `initialize_db(sqlite_path)` is called once per startup (in `default_container`) and:
   - runs `Base.metadata.create_all` to create any entirely new tables, then
   - runs `migrate_schema(engine)` to add any columns that are missing from existing tables.
3. Each migration version is a list of `(table, column, sql_type)` tuples. A column is added
   only if the table exists and the column is absent, making all operations idempotent.
4. Migration versions are recorded atomically so each runs at most once per database file.

Column additions use SQLite's `ALTER TABLE ... ADD COLUMN` syntax. Columns added this way are
initialized to NULL in all rows present at migration time; ORM-declared non-nullable columns may
therefore return `None` for those rows.

## Consequences

- The catalog is always schema-consistent at startup regardless of when it was first created.
- Fresh databases receive all tables and columns from `create_all` directly; `migrate_schema`
  records versions as applied without issuing any `ALTER TABLE` statements.
- When a new column is added to a model, a corresponding `(table, column, type)` tuple must be
  appended to `_COLUMN_MIGRATIONS` in `sqlite_db.py`. Entirely new tables require no migration
  entry; `create_all` handles them.
