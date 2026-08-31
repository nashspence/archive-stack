"""Immutable DDL snapshot for the Riverhog client local state v1 baseline."""

# ruff: noqa: E501

# This module is migration authority. Runtime model metadata must never be imported here.

SQLITE_DDL: tuple[str, ...] = (
    """
CREATE TABLE settings (
	"key" TEXT NOT NULL,
	value TEXT NOT NULL,
	PRIMARY KEY ("key")
)
    """.strip(),
    """
CREATE TABLE desired_collections (
	collection_id INTEGER NOT NULL,
	inventory_identity TEXT NOT NULL,
	inventory_cursor TEXT,
	inventory_complete INTEGER DEFAULT 0 NOT NULL,
	created_at TEXT NOT NULL,
	remote_deleted INTEGER DEFAULT 0 NOT NULL,
	PRIMARY KEY (collection_id),
	CONSTRAINT ck_desired_collections_id CHECK (collection_id > 0),
	CONSTRAINT ck_desired_collections_etag CHECK (length(inventory_identity) = 64 AND inventory_identity = lower(inventory_identity) AND inventory_identity NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_desired_collections_inventory_complete CHECK (inventory_complete IN (0, 1)),
	CONSTRAINT ck_desired_collections_remote_deleted CHECK (remote_deleted IN (0, 1))
)
    """.strip(),
    """
CREATE TABLE retrieval_jobs (
	id TEXT NOT NULL,
	state TEXT NOT NULL,
	updated_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_retrieval_jobs_state CHECK (state IN ('requested', 'ready', 'completed', 'expired', 'failed', 'canceled'))
)
    """.strip(),
    """
CREATE TABLE desired_collection_tags (
	collection_id INTEGER NOT NULL,
	tag TEXT NOT NULL,
	PRIMARY KEY (collection_id, tag),
	FOREIGN KEY(collection_id) REFERENCES desired_collections (collection_id) ON DELETE CASCADE
)
    """.strip(),
    """
CREATE TABLE desired_files (
	collection_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	bytes INTEGER NOT NULL,
	sha256 TEXT NOT NULL,
	PRIMARY KEY (collection_id, path),
	CONSTRAINT ck_desired_files_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_desired_files_sha256 CHECK (length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'),
	FOREIGN KEY(collection_id) REFERENCES desired_collections (collection_id) ON DELETE CASCADE
)
    """.strip(),
    """
CREATE TABLE retrieval_job_files (
	retrieval_job_id TEXT NOT NULL,
	ordinal INTEGER NOT NULL,
	collection_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	bytes INTEGER NOT NULL,
	sha256 TEXT NOT NULL,
	PRIMARY KEY (retrieval_job_id, ordinal),
	CONSTRAINT ck_retrieval_job_files_ordinal CHECK (ordinal >= 0),
	CONSTRAINT ck_retrieval_job_files_collection CHECK (collection_id > 0),
	CONSTRAINT ck_retrieval_job_files_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_retrieval_job_files_sha256 CHECK (length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT uq_retrieval_job_files_artifact UNIQUE (retrieval_job_id, collection_id, path),
	FOREIGN KEY(retrieval_job_id) REFERENCES retrieval_jobs (id) ON DELETE CASCADE
)
    """.strip(),
)
