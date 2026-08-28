-- Exact current Riverhog local v1 baseline conformance fixture.
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
    state TEXT NOT NULL CHECK (
        state IN ('requested', 'ready', 'completed', 'expired', 'failed', 'canceled')
    ),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO retrieval_jobs VALUES(
    'fixture-retrieval',
    'ready',
    '2026-01-01T00:00:00.000000Z'
);
CREATE TABLE retrieval_job_files (
    retrieval_job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    collection_id INTEGER NOT NULL CHECK (collection_id > 0),
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (retrieval_job_id, ordinal),
    UNIQUE (retrieval_job_id, collection_id, path),
    FOREIGN KEY (retrieval_job_id) REFERENCES retrieval_jobs(id)
        ON DELETE CASCADE
);
INSERT INTO retrieval_job_files VALUES(
    'fixture-retrieval',
    0,
    1,
    'notes/fixture.txt',
    12,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
);
CREATE TABLE state_schema_revision (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);
INSERT INTO state_schema_revision VALUES('v1_0001');
COMMIT;
