-- Exact current Riverhog local v1 baseline conformance fixture.
BEGIN TRANSACTION;
CREATE TABLE settings (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO settings VALUES('catalog_reconcile_after', '1');
CREATE TABLE desired_collections (
    collection_id INTEGER NOT NULL PRIMARY KEY,
    inventory_identity TEXT NOT NULL,
    inventory_cursor TEXT,
    inventory_complete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    remote_deleted INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT ck_desired_collections_id CHECK (collection_id > 0),
    CONSTRAINT ck_desired_collections_etag CHECK (
        length(inventory_identity) = 64 AND inventory_identity = lower(inventory_identity)
        AND inventory_identity NOT GLOB '*[^0-9a-f]*'
    ),
    CONSTRAINT ck_desired_collections_inventory_complete CHECK (
        inventory_complete IN (0, 1)
    ),
    CONSTRAINT ck_desired_collections_remote_deleted CHECK (remote_deleted IN (0, 1))
);
INSERT INTO desired_collections VALUES(
    1,
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    NULL,
    1,
    '2026-01-01T00:00:00.000000Z',
    0
);
CREATE TABLE desired_files (
    collection_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (collection_id, path),
    CONSTRAINT ck_desired_files_bytes CHECK (bytes >= 0),
    CONSTRAINT ck_desired_files_sha256 CHECK (
        length(sha256) = 64 AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
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
    id TEXT NOT NULL PRIMARY KEY,
    state TEXT NOT NULL CONSTRAINT ck_retrieval_jobs_state CHECK (
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
    ordinal INTEGER NOT NULL CONSTRAINT ck_retrieval_job_files_ordinal CHECK (ordinal >= 0),
    collection_id INTEGER NOT NULL CONSTRAINT ck_retrieval_job_files_collection CHECK (
        collection_id > 0
    ),
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL CONSTRAINT ck_retrieval_job_files_bytes CHECK (bytes >= 0),
    sha256 TEXT NOT NULL CONSTRAINT ck_retrieval_job_files_sha256 CHECK (
        length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (retrieval_job_id, ordinal),
    CONSTRAINT uq_retrieval_job_files_artifact UNIQUE (
        retrieval_job_id, collection_id, path
    ),
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
