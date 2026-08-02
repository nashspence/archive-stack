-- Immutable Riverhog local v1.0 database fixture. Add later baselines; do not edit.
BEGIN TRANSACTION;
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO settings VALUES('catalog_cursor', '19');
CREATE TABLE desired_collections (
    collection_id INTEGER PRIMARY KEY,
    record_etag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    remote_deleted INTEGER NOT NULL DEFAULT 0
);
INSERT INTO desired_collections VALUES(
    1,
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    '2026-01-01T00:00:00.000000Z',
    '["fixture"]',
    0
);
CREATE TABLE desired_files (
    collection_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (collection_id, path),
    FOREIGN KEY (collection_id) REFERENCES desired_collections(collection_id)
        ON DELETE CASCADE
);
INSERT INTO desired_files VALUES(
    1,
    'notes/fixture.txt',
    12,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
);
CREATE TABLE retrieval_jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    files_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO retrieval_jobs VALUES(
    'fixture-retrieval',
    'ready',
    '["notes/fixture.txt"]',
    '2026-01-01T00:00:00.000000Z'
);
CREATE TABLE state_schema_revision (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);
INSERT INTO state_schema_revision VALUES('v1_0001');
COMMIT;
