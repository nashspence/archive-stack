-- Exact current Riverhog PostgreSQL v1 baseline conformance fixture.

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE TABLE state_schema_revision (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);

CREATE TABLE app_keys (
	id VARCHAR NOT NULL,
	app VARCHAR NOT NULL,
	token_sha256 VARCHAR(64) NOT NULL,
	monthly_download_quota_bytes BIGINT,
	created_at VARCHAR NOT NULL,
	expires_at VARCHAR,
	revoked_at VARCHAR,
	last_used_at VARCHAR,
	search_text VARCHAR GENERATED ALWAYS AS (lower(app || ' ' || id)) STORED NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_app_keys_download_quota CHECK (monthly_download_quota_bytes IS NULL OR monthly_download_quota_bytes >= 0),
	CONSTRAINT ck_app_keys_token_sha256 CHECK (length(token_sha256) = 64),
	CONSTRAINT ck_app_keys_token_sha256_hex CHECK (length(token_sha256) = 64 AND lower(token_sha256) = token_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(token_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_app_keys_active ON app_keys (revoked_at, expires_at, id);

CREATE INDEX ix_app_keys_app ON app_keys (app, id);

CREATE INDEX ix_app_keys_app_active ON app_keys (app, revoked_at, expires_at, id);

CREATE INDEX ix_app_keys_app_created ON app_keys (app, created_at, id);

CREATE INDEX ix_app_keys_app_expires ON app_keys (app, expires_at, id);

CREATE INDEX ix_app_keys_app_last_used ON app_keys (app, last_used_at, id);

CREATE INDEX ix_app_keys_app_trgm ON app_keys USING gin (app gin_trgm_ops);

CREATE INDEX ix_app_keys_id_trgm ON app_keys USING gin (id gin_trgm_ops);

CREATE INDEX ix_app_keys_search_trgm ON app_keys USING gin (search_text gin_trgm_ops);

CREATE UNIQUE INDEX ux_app_keys_token_sha256 ON app_keys (token_sha256);

CREATE TABLE archive_download_usage (
	store VARCHAR NOT NULL,
	month_started_at VARCHAR NOT NULL,
	accounted_bytes BIGINT NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (store),
	CONSTRAINT ck_archive_download_usage_bytes CHECK (accounted_bytes >= 0)
);

CREATE TABLE catalog_events (
	sequence BIGSERIAL NOT NULL,
	revision BIGINT,
	change VARCHAR NOT NULL,
	collection_id BIGINT NOT NULL,
	occurred_at VARCHAR NOT NULL,
	inventory_identity VARCHAR(64) NOT NULL,
	archive_root_sha256 VARCHAR(64) NOT NULL,
	content_identity VARCHAR(64) NOT NULL,
	committed_at VARCHAR,
	published BOOLEAN DEFAULT true NOT NULL,
	PRIMARY KEY (sequence),
	CONSTRAINT ck_catalog_events_revision CHECK (revision IS NULL OR revision > 0),
	UNIQUE (revision),
	CONSTRAINT ck_catalog_events_inventory_identity_hex CHECK (length(inventory_identity) = 64 AND lower(inventory_identity) = inventory_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(inventory_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_catalog_events_archive_root_sha256_hex CHECK (length(archive_root_sha256) = 64 AND lower(archive_root_sha256) = archive_root_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_root_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_catalog_events_content_identity_hex CHECK (length(content_identity) = 64 AND lower(content_identity) = content_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_catalog_events_collection ON catalog_events (collection_id, sequence);

CREATE INDEX ix_catalog_events_published ON catalog_events (published, sequence);

CREATE INDEX ix_catalog_events_revision ON catalog_events (revision);

CREATE INDEX ix_catalog_events_committed ON catalog_events (committed_at, revision);

CREATE TABLE catalog_sync_state (
	singleton SERIAL NOT NULL,
	source_identity VARCHAR(64) NOT NULL,
	committed_revision BIGINT DEFAULT 0 NOT NULL,
	retained_revision BIGINT DEFAULT 0 NOT NULL,
	PRIMARY KEY (singleton),
	CONSTRAINT ck_catalog_sync_state_singleton CHECK (singleton = 1),
	CONSTRAINT ck_catalog_sync_state_committed_revision CHECK (committed_revision >= 0),
	CONSTRAINT ck_catalog_sync_state_retained_revision CHECK (retained_revision >= 0 AND retained_revision <= committed_revision),
	CONSTRAINT ck_catalog_sync_state_source_identity_hex CHECK (length(source_identity) = 64 AND lower(source_identity) = source_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(source_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

INSERT INTO catalog_sync_state (singleton, source_identity, committed_revision, retained_revision)
VALUES (1, md5(random()::text || clock_timestamp()::text) || md5(random()::text || clock_timestamp()::text), 0, 0);

CREATE TABLE collection_deletions (
	collection_id BIGSERIAL NOT NULL,
	challenge VARCHAR NOT NULL,
	plan_json TEXT NOT NULL,
	started_at VARCHAR NOT NULL,
	PRIMARY KEY (collection_id)
);

CREATE TABLE collection_uploads (
	collection_id BIGINT GENERATED BY DEFAULT AS IDENTITY,
	idempotency_key VARCHAR NOT NULL,
	creation_identity_sha256 VARCHAR(64) NOT NULL,
	archive_generation VARCHAR(64) NOT NULL,
	ingest_source VARCHAR,
	provenance_mode VARCHAR NOT NULL,
	provenance_omission_reason TEXT,
	provenance_identity VARCHAR(64),
	encryption_format VARCHAR NOT NULL,
	passphrase_id VARCHAR NOT NULL,
	initiated_by_app VARCHAR NOT NULL,
	initiated_by_key_id VARCHAR,
	event_context_json TEXT,
	state VARCHAR NOT NULL,
	custody_mode VARCHAR NOT NULL,
	lease_expires_at VARCHAR,
	orphaned_at VARCHAR,
	archive_store VARCHAR NOT NULL,
	opened_at VARCHAR NOT NULL,
	last_activity_at VARCHAR NOT NULL,
	closed_at VARCHAR,
	archive_phase VARCHAR NOT NULL,
	archive_phase_updated_at VARCHAR NOT NULL,
	archive_attempt_count INTEGER NOT NULL,
	archive_next_attempt_at VARCHAR,
	archive_last_attempt_at VARCHAR,
	archive_failure VARCHAR,
	archive_storage_prefix VARCHAR NOT NULL,
	planner_checkpoint_json TEXT NOT NULL,
	archive_tree_next_file_order BIGINT DEFAULT 0 NOT NULL,
	archive_tree_hash_state TEXT,
	archive_tree_sha256 VARCHAR(64),
	archive_volume_next_sequence VARCHAR(64) DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' NOT NULL,
	archive_volume_hash_state TEXT,
	archive_ordered_volume_sha256 VARCHAR(64),
	archive_terminal_receipt_json TEXT,
	provenance_validation_next_file_order BIGINT DEFAULT 0 NOT NULL,
	provenance_closure_validated BOOLEAN DEFAULT false NOT NULL,
	derivative_provenance_state VARCHAR DEFAULT 'not-required' NOT NULL,
	derivative_provenance_cursor_json TEXT DEFAULT '{}' NOT NULL,
	provenance_archive_next_file_order BIGINT DEFAULT 0 NOT NULL,
	provenance_archive_last_journal_id VARCHAR,
	provenance_archive_current_journal_id VARCHAR,
	provenance_archive_current_journal_offset BIGINT DEFAULT 0 NOT NULL,
	provenance_archive_next_sequence VARCHAR(64) DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' NOT NULL,
	provenance_archive_hash_state TEXT,
	provenance_archive_ordered_sha256 VARCHAR(64),
	provenance_archive_terminal_receipt_json TEXT,
	provenance_archive_root_receipt_json TEXT,
	final_authority_json TEXT,
	catalog_phase VARCHAR DEFAULT 'content-identity' NOT NULL,
	catalog_cursor_json TEXT DEFAULT '{}' NOT NULL,
	catalog_hash_state TEXT,
	catalog_content_identity VARCHAR(64),
	catalog_inventory_identity VARCHAR(64),
	file_count BIGINT DEFAULT 0 NOT NULL,
	file_bytes BIGINT DEFAULT 0 NOT NULL,
	custodied_file_count BIGINT DEFAULT 0 NOT NULL,
	custodied_file_bytes BIGINT DEFAULT 0 NOT NULL,
	uploaded_payload_bytes BIGINT DEFAULT 0 NOT NULL,
	search_text VARCHAR NOT NULL,
	PRIMARY KEY (collection_id),
	CONSTRAINT ck_collection_uploads_file_count CHECK (file_count >= 0),
	CONSTRAINT ck_collection_uploads_tree_progress CHECK (archive_tree_next_file_order >= 0),
	CONSTRAINT ck_collection_uploads_volume_progress CHECK (length(archive_volume_next_sequence) = 64 AND lower(archive_volume_next_sequence) = archive_volume_next_sequence AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_volume_next_sequence, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_collection_uploads_provenance_progress CHECK (provenance_validation_next_file_order >= 0 AND provenance_archive_next_file_order >= 0 AND provenance_archive_current_journal_offset >= 0 AND length(provenance_archive_next_sequence) = 64 AND lower(provenance_archive_next_sequence) = provenance_archive_next_sequence AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(provenance_archive_next_sequence, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_collection_uploads_catalog_phase CHECK (catalog_phase IN ('content-identity','inventory-identity','collection','files','journals','provenance-relations','bindings','archive-objects','file-objects','terminal','complete')),
	CONSTRAINT ck_collection_uploads_file_bytes CHECK (file_bytes >= 0),
	CONSTRAINT ck_collection_uploads_custodied_file_count CHECK (custodied_file_count >= 0 AND custodied_file_count <= file_count),
	CONSTRAINT ck_collection_uploads_custodied_file_bytes CHECK (custodied_file_bytes >= 0 AND custodied_file_bytes <= file_bytes),
	CONSTRAINT ck_collection_uploads_empty_custody CHECK (custodied_file_count > 0 OR custodied_file_bytes = 0),
	CONSTRAINT ck_collection_uploads_uploaded_payload_bytes CHECK (uploaded_payload_bytes >= 0),
	CONSTRAINT ck_collection_uploads_state CHECK (state IN ('open','closing','uploading','finalizing','orphaned','discarding')),
	CONSTRAINT ck_collection_uploads_custody_mode CHECK (custody_mode IN ('producer-retained','custody-transfer')),
	CONSTRAINT ck_collection_uploads_provenance_mode CHECK (provenance_mode IN ('captured','omitted')),
	CONSTRAINT ck_collection_uploads_derivative_provenance_state CHECK (derivative_provenance_state IN ('not-required','discovering','copying','generating','complete','failed')),
	CONSTRAINT ck_collection_uploads_archive_phase CHECK (archive_phase IN ('planning','uploading','finalization_queued','finalizing','retry_wait','orphaned','discarding')),
	CONSTRAINT ck_collection_uploads_attempt_count CHECK (archive_attempt_count >= 0),
	CONSTRAINT ck_collection_uploads_creation_identity_sha256_hex CHECK (length(creation_identity_sha256) = 64 AND lower(creation_identity_sha256) = creation_identity_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(creation_identity_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_archive_generation_hex CHECK (length(archive_generation) = 64 AND lower(archive_generation) = archive_generation AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_generation, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_provenance_identity_hex CHECK (provenance_identity IS NULL OR length(provenance_identity) = 64 AND lower(provenance_identity) = provenance_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(provenance_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_archive_tree_sha256_hex CHECK (archive_tree_sha256 IS NULL OR length(archive_tree_sha256) = 64 AND lower(archive_tree_sha256) = archive_tree_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_tree_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_archive_ordered_volume_sha256_hex CHECK (archive_ordered_volume_sha256 IS NULL OR length(archive_ordered_volume_sha256) = 64 AND lower(archive_ordered_volume_sha256) = archive_ordered_volume_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_ordered_volume_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_provenance_archive_ordered_sha256_hex CHECK (provenance_archive_ordered_sha256 IS NULL OR length(provenance_archive_ordered_sha256) = 64 AND lower(provenance_archive_ordered_sha256) = provenance_archive_ordered_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(provenance_archive_ordered_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_catalog_content_identity_hex CHECK (catalog_content_identity IS NULL OR length(catalog_content_identity) = 64 AND lower(catalog_content_identity) = catalog_content_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(catalog_content_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_uploads_catalog_inventory_identity_hex CHECK (catalog_inventory_identity IS NULL OR length(catalog_inventory_identity) = 64 AND lower(catalog_inventory_identity) = catalog_inventory_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(catalog_inventory_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_uploads_file_bytes ON collection_uploads (file_bytes, collection_id);

CREATE INDEX ix_collection_uploads_file_count ON collection_uploads (file_count, collection_id);

CREATE INDEX ix_collection_uploads_opened_at ON collection_uploads (opened_at, collection_id);

CREATE INDEX ix_collection_uploads_search_trgm ON collection_uploads USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_collection_uploads_state ON collection_uploads (state, collection_id);

CREATE UNIQUE INDEX ux_collection_uploads_application_idempotency_key ON collection_uploads (initiated_by_app, idempotency_key);

CREATE TABLE collections (
	id BIGSERIAL NOT NULL,
	search_text VARCHAR GENERATED ALWAYS AS (CAST(id AS TEXT)) STORED NOT NULL,
	creation_idempotency_key VARCHAR NOT NULL,
	creation_identity_sha256 VARCHAR(64) NOT NULL,
	creation_custody_mode VARCHAR NOT NULL,
	archive_generation VARCHAR(64) NOT NULL,
	content_identity VARCHAR(64) NOT NULL,
	encryption_format VARCHAR NOT NULL,
	passphrase_id VARCHAR NOT NULL,
	provenance_mode VARCHAR NOT NULL,
	provenance_identity VARCHAR(64),
	inventory_identity VARCHAR(64) NOT NULL,
	archive_root_sha256 VARCHAR(64),
	catalog_revision BIGINT,
	ingest_source VARCHAR,
	created_by_app VARCHAR NOT NULL,
	created_by_key_id VARCHAR,
	created_at VARCHAR NOT NULL,
	is_published BOOLEAN DEFAULT true NOT NULL,
	file_count BIGINT DEFAULT 0 NOT NULL,
	file_bytes BIGINT DEFAULT 0 NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_collections_application_idempotency_key UNIQUE (created_by_app, creation_idempotency_key),
	CONSTRAINT ck_collections_file_count CHECK (file_count >= 0),
	CONSTRAINT ck_collections_file_bytes CHECK (file_bytes >= 0),
	CONSTRAINT ck_collections_provenance_mode CHECK (provenance_mode IN ('captured','mixed','omitted')),
	CONSTRAINT ck_collections_provenance_identity CHECK (provenance_mode IN ('captured','mixed') AND provenance_identity IS NOT NULL OR provenance_mode = 'omitted' AND provenance_identity IS NULL),
	CONSTRAINT ck_collections_content_identity CHECK (length(content_identity) = 64),
	CONSTRAINT ck_collections_inventory_identity CHECK (length(inventory_identity) = 64),
	CONSTRAINT ck_collections_archive_root_sha256 CHECK (archive_root_sha256 IS NULL OR length(archive_root_sha256) = 64),
	CONSTRAINT ck_collections_catalog_revision CHECK (catalog_revision IS NULL OR catalog_revision > 0),
	CONSTRAINT ck_collections_creation_identity_sha256_hex CHECK (length(creation_identity_sha256) = 64 AND lower(creation_identity_sha256) = creation_identity_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(creation_identity_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collections_archive_generation_hex CHECK (length(archive_generation) = 64 AND lower(archive_generation) = archive_generation AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_generation, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collections_content_identity_hex CHECK (length(content_identity) = 64 AND lower(content_identity) = content_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collections_provenance_identity_hex CHECK (provenance_identity IS NULL OR length(provenance_identity) = 64 AND lower(provenance_identity) = provenance_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(provenance_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collections_inventory_identity_hex CHECK (length(inventory_identity) = 64 AND lower(inventory_identity) = inventory_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(inventory_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collections_archive_root_sha256_hex CHECK (archive_root_sha256 IS NULL OR length(archive_root_sha256) = 64 AND lower(archive_root_sha256) = archive_root_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_root_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collections_created_at_id ON collections (created_at, id);

CREATE INDEX ix_collections_encryption_format ON collections (encryption_format, id);

CREATE INDEX ix_collections_file_bytes_id ON collections (file_bytes, id);

CREATE INDEX ix_collections_file_count_id ON collections (file_count, id);

CREATE INDEX ix_collections_passphrase_id ON collections (passphrase_id, id);

CREATE INDEX ix_collections_search_trgm ON collections USING gin (search_text gin_trgm_ops);

CREATE TABLE lifecycle_events (
	sequence SERIAL NOT NULL,
	event_id VARCHAR NOT NULL,
	owner_app VARCHAR NOT NULL,
	subject VARCHAR,
	event_json TEXT NOT NULL,
	context_json TEXT,
	context_expires_at VARCHAR,
	PRIMARY KEY (sequence),
	UNIQUE (event_id)
);

CREATE INDEX ix_lifecycle_events_context_expiry ON lifecycle_events (context_expires_at, sequence);

CREATE INDEX ix_lifecycle_events_owner_sequence ON lifecycle_events (owner_app, sequence);

CREATE INDEX ix_lifecycle_events_owner_subject_context ON lifecycle_events (owner_app, subject, context_expires_at);

CREATE TABLE retrieval_cache_populations (
	source_store VARCHAR NOT NULL,
	collection_id BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	cache_store VARCHAR,
	object_path VARCHAR,
	write_token VARCHAR,
	expected_bytes BIGINT NOT NULL,
	state VARCHAR NOT NULL,
	initiated_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	failure TEXT,
	PRIMARY KEY (source_store, collection_id, object_id),
	CONSTRAINT ck_retrieval_cache_populations_expected_bytes CHECK (expected_bytes >= 1),
	CONSTRAINT ck_retrieval_cache_populations_state CHECK (state IN ('waiting','admitting','admitted','writing','abandoning')),
	CONSTRAINT ck_retrieval_cache_populations_session CHECK (cache_store IS NULL AND object_path IS NULL AND write_token IS NULL AND state IN ('waiting','abandoning') OR cache_store IS NOT NULL AND object_path IS NOT NULL AND (write_token IS NULL AND state = 'admitting' OR write_token IS NOT NULL AND state IN ('admitted','writing') OR state = 'abandoning'))
);

CREATE INDEX ix_retrieval_cache_populations_store_state ON retrieval_cache_populations (cache_store, state, updated_at, collection_id, source_store, object_id);

CREATE TABLE retrieval_cache_store_accounting (
	cache_store VARCHAR NOT NULL,
	reserved_bytes BIGINT DEFAULT 0 NOT NULL,
	committed_bytes BIGINT DEFAULT 0 NOT NULL,
	generation BIGINT DEFAULT 0 NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (cache_store),
	CONSTRAINT ck_retrieval_cache_store_accounting_reserved CHECK (reserved_bytes >= 0),
	CONSTRAINT ck_retrieval_cache_store_accounting_committed CHECK (committed_bytes >= 0),
	CONSTRAINT ck_retrieval_cache_store_accounting_generation CHECK (generation >= 0)
);

CREATE TABLE retrieval_cache_accounting_reconciliations (
	cache_store VARCHAR NOT NULL,
	generation BIGINT NOT NULL,
	after_source_store VARCHAR,
	after_collection_id BIGINT,
	after_object_id VARCHAR,
	accumulated_bytes BIGINT DEFAULT 0 NOT NULL,
	started_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (cache_store),
	FOREIGN KEY(cache_store) REFERENCES retrieval_cache_store_accounting (cache_store) ON DELETE CASCADE,
	CONSTRAINT ck_cache_accounting_reconciliations_generation CHECK (generation >= 0),
	CONSTRAINT ck_cache_accounting_reconciliations_bytes CHECK (accumulated_bytes >= 0)
);

CREATE TABLE retrieval_plans (
	id VARCHAR NOT NULL,
	app VARCHAR NOT NULL,
	initiated_by_key_id VARCHAR,
	idempotency_key VARCHAR NOT NULL,
	creation_identity_sha256 VARCHAR(64) NOT NULL,
	state VARCHAR NOT NULL,
	request_json TEXT NOT NULL,
	lease_seconds BIGINT NOT NULL,
	restore_policy VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	ready_at VARCHAR,
	expires_at VARCHAR NOT NULL,
	failure TEXT,
	next_file_order INTEGER NOT NULL,
	next_placement_sequence VARCHAR(64) NOT NULL,
	object_count VARCHAR(64) NOT NULL,
	retrieval_bytes VARCHAR(64) NOT NULL,
	requires_restore BOOLEAN NOT NULL,
	file_commitment_sha256 VARCHAR(64) NOT NULL,
	segment_commitment_sha256 VARCHAR(64) NOT NULL,
	etag VARCHAR(64),
	PRIMARY KEY (id),
	CONSTRAINT uq_retrieval_plans_key_idempotency UNIQUE (app, initiated_by_key_id, idempotency_key),
	CONSTRAINT ck_retrieval_plans_state CHECK (state IN ('planning','ready','consumed','expired','failed')),
	CONSTRAINT ck_retrieval_plans_lease CHECK (lease_seconds > 0),
	CONSTRAINT ck_retrieval_plans_restore_policy CHECK (restore_policy IN ('allow','never')),
	CONSTRAINT ck_retrieval_plans_file_order CHECK (next_file_order >= 0),
	CONSTRAINT ck_retrieval_plans_creation_identity_sha256_hex CHECK (length(creation_identity_sha256) = 64 AND lower(creation_identity_sha256) = creation_identity_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(creation_identity_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_retrieval_plans_file_commitment_sha256_hex CHECK (length(file_commitment_sha256) = 64 AND lower(file_commitment_sha256) = file_commitment_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(file_commitment_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_retrieval_plans_segment_commitment_sha256_hex CHECK (length(segment_commitment_sha256) = 64 AND lower(segment_commitment_sha256) = segment_commitment_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(segment_commitment_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_retrieval_plans_etag_hex CHECK (etag IS NULL OR length(etag) = 64 AND lower(etag) = etag AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(etag, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_retrieval_plans_owner ON retrieval_plans (app, initiated_by_key_id, id);

CREATE TABLE retrieval_jobs (
	id VARCHAR NOT NULL,
	plan_id VARCHAR NOT NULL,
	app VARCHAR NOT NULL,
	initiated_by_key_id VARCHAR,
	event_context_json TEXT,
	state VARCHAR NOT NULL,
	plan_etag VARCHAR(64) NOT NULL,
	lease_seconds BIGINT NOT NULL,
	created_at VARCHAR NOT NULL,
	requested_at VARCHAR,
	restore_requested_at VARCHAR,
	ready_at VARCHAR,
	expires_at VARCHAR,
	next_poll_at VARCHAR,
	completed_at VARCHAR,
	canceled_at VARCHAR,
	failure TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(plan_id) REFERENCES retrieval_plans (id),
	UNIQUE (id, plan_id),
	CONSTRAINT ck_retrieval_jobs_state CHECK (state IN ('requested','ready','completed','canceled','expired','failed')),
	CONSTRAINT ck_retrieval_jobs_plan_etag CHECK (length(plan_etag) = 64),
	CONSTRAINT ck_retrieval_jobs_lease CHECK (lease_seconds > 0),
	UNIQUE (plan_id),
	CONSTRAINT ck_retrieval_jobs_plan_etag_hex CHECK (length(plan_etag) = 64 AND lower(plan_etag) = plan_etag AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_etag, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_retrieval_jobs_due ON retrieval_jobs (state, next_poll_at, id);

CREATE TABLE app_key_access_grants (
	key_id VARCHAR NOT NULL,
	permission VARCHAR NOT NULL,
	resource VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	search_text VARCHAR GENERATED ALWAYS AS (lower(permission || ' ' || resource)) STORED NOT NULL,
	PRIMARY KEY (key_id, permission, resource),
	FOREIGN KEY(key_id) REFERENCES app_keys (id) ON DELETE CASCADE
);

CREATE INDEX ix_app_key_access_grants_created ON app_key_access_grants (created_at, key_id, permission, resource);

CREATE INDEX ix_app_key_access_grants_permission ON app_key_access_grants (permission, resource, key_id);

CREATE INDEX ix_app_key_access_grants_resource ON app_key_access_grants (resource, permission, key_id);

CREATE INDEX ix_app_key_access_grants_search_trgm ON app_key_access_grants USING gin (search_text gin_trgm_ops);

CREATE TABLE archive_download_reservations (
	id VARCHAR NOT NULL,
	store VARCHAR NOT NULL,
	month_started_at VARCHAR NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	created_at VARCHAR NOT NULL,
	expires_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(store) REFERENCES archive_download_usage (store) ON DELETE CASCADE,
	CONSTRAINT ck_archive_download_reservations_bytes CHECK (reserved_bytes >= 0)
);

CREATE INDEX ix_archive_download_reservations_expiry ON archive_download_reservations (store, expires_at);

CREATE TABLE collection_archive_copies (
	collection_id BIGINT NOT NULL,
	store VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	archive_storage_prefix VARCHAR,
	last_uploaded_at VARCHAR,
	last_verified_at VARCHAR,
	failure VARCHAR,
	PRIMARY KEY (collection_id, store),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_archive_copies_state CHECK (state IN ('pending','uploading','uploaded','retrying','failed'))
);

CREATE TABLE collection_archive_object_uploads (
	collection_id BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	sequence VARCHAR(64) NOT NULL,
	kind VARCHAR NOT NULL,
	relative_path VARCHAR NOT NULL,
	object_path VARCHAR NOT NULL,
	plaintext_bytes BIGINT NOT NULL,
	source_bytes BIGINT NOT NULL,
	source_path VARCHAR,
	source_first_part BIGINT,
	source_part_count BIGINT,
	unit_plaintext_bytes BIGINT NOT NULL,
	plan_json TEXT NOT NULL,
	plan_sha256 VARCHAR(64) NOT NULL,
	state VARCHAR NOT NULL,
	checkpoint_json TEXT,
	sealed_receipt_json TEXT,
	metadata_receipt_json TEXT,
	failure TEXT,
	uploaded_bytes BIGINT NOT NULL,
	uploaded_units INTEGER NOT NULL,
	total_units INTEGER NOT NULL,
	updated_at VARCHAR NOT NULL,
	sealed_at VARCHAR,
	PRIMARY KEY (collection_id, object_id),
	FOREIGN KEY(collection_id) REFERENCES collection_uploads (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_archive_object_uploads_sequence CHECK (length(sequence) = 64 AND lower(sequence) = sequence AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sequence, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_archive_object_uploads_plaintext CHECK (plaintext_bytes >= 0),
	CONSTRAINT ck_archive_object_uploads_source CHECK (source_bytes >= 0),
	CONSTRAINT ck_archive_object_uploads_source_parts CHECK (kind = 'pack' AND source_path IS NULL AND source_first_part IS NULL AND source_part_count IS NULL OR kind = 'segment' AND source_path IS NOT NULL AND source_first_part >= 0 AND source_part_count > 0),
	CONSTRAINT ck_archive_object_uploads_unit CHECK (unit_plaintext_bytes > 0),
	CONSTRAINT ck_archive_object_uploads_state CHECK (state IN ('planned','uploading','sealed')),
	CONSTRAINT ck_archive_object_uploads_uploaded_bytes CHECK (uploaded_bytes >= 0),
	CONSTRAINT ck_archive_object_uploads_uploaded_units CHECK (uploaded_units >= 0),
	CONSTRAINT ck_archive_object_uploads_total_units CHECK (total_units >= 0),
	CONSTRAINT ck_archive_object_uploads_unit_progress CHECK (uploaded_units <= total_units),
	CONSTRAINT ck_collection_archive_object_uploads_plan_sha256_hex CHECK (length(plan_sha256) = 64 AND lower(plan_sha256) = plan_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE UNIQUE INDEX ux_collection_archive_object_uploads_sequence ON collection_archive_object_uploads (collection_id, sequence);

CREATE TABLE collection_files (
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	provenance_status VARCHAR DEFAULT 'missing' NOT NULL,
	path_sort_key BYTEA NOT NULL,
	search_text VARCHAR NOT NULL,
	path_search_text VARCHAR NOT NULL,
	PRIMARY KEY (collection_id, path),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_files_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_collection_files_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_files_provenance_status CHECK (provenance_status IN ('captured','omitted','missing')),
	CONSTRAINT ck_collection_files_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_files_bytes ON collection_files (bytes, collection_id, path_sort_key);

CREATE INDEX ix_collection_files_collection_bytes ON collection_files (collection_id, bytes, path_sort_key);

CREATE INDEX ix_collection_files_collection_path ON collection_files (collection_id, path_sort_key);

CREATE INDEX ix_collection_files_collection_provenance ON collection_files (collection_id, provenance_status, path_sort_key);

CREATE INDEX ix_collection_files_path ON collection_files (path_sort_key, collection_id);

CREATE INDEX ix_collection_files_path_search_trgm ON collection_files USING gin (path_search_text gin_trgm_ops);

CREATE INDEX ix_collection_files_search_trgm ON collection_files USING gin (search_text gin_trgm_ops);

CREATE TABLE collection_processing_claims (
	id VARCHAR(64) NOT NULL,
	work_id VARCHAR(64) NOT NULL,
	consumer_app VARCHAR NOT NULL,
	consumer_key_id VARCHAR,
	purpose VARCHAR NOT NULL,
	work_document_json TEXT NOT NULL,
	work_document_sha256 VARCHAR(64) NOT NULL,
	execution_id VARCHAR(64),
	controller_evidence_json TEXT,
	controller_evidence_sha256 VARCHAR(64),
	operation_id VARCHAR,
	operation_sha256 VARCHAR(64),
	input_count BIGINT NOT NULL,
	input_hash_state TEXT,
	input_set_sha256 VARCHAR(64),
	inputs_sealed_at VARCHAR,
	artifact_count BIGINT NOT NULL,
	artifact_bytes BIGINT NOT NULL,
	artifact_hash_state TEXT,
	artifact_set_sha256 VARCHAR(64),
	artifacts_sealed_at VARCHAR,
	outcome_count BIGINT NOT NULL,
	outcome_state VARCHAR NOT NULL,
	outcome_hash_state TEXT,
	outcome_validation_cursor VARCHAR,
	outcome_validation_count BIGINT NOT NULL,
	outcome_set_sha256 VARCHAR(64),
	outcome_failure TEXT,
	outcomes_sealed_at VARCHAR,
	retirement_policy VARCHAR,
	retirement_grace_seconds BIGINT NOT NULL,
	plan_sealed_at VARCHAR,
	state VARCHAR NOT NULL,
	fence BIGINT NOT NULL,
	expires_at VARCHAR NOT NULL,
	output_collection_id BIGINT,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	settled_at VARCHAR,
	abandoned_at VARCHAR,
	abandonment_reason TEXT,
	released_at VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT uq_collection_processing_claims_owner_work UNIQUE (consumer_app, purpose, work_id),
	CONSTRAINT ck_collection_processing_claims_state CHECK (state IN ('active','settled','retiring','abandoned','released')),
	CONSTRAINT ck_collection_processing_claims_outcome_state CHECK (outcome_state IN ('receiving','sealing','sealed','failed')),
	CONSTRAINT ck_collection_processing_claims_fence CHECK (fence >= 1),
	CONSTRAINT ck_collection_processing_claims_grace CHECK (retirement_grace_seconds >= 0),
	CONSTRAINT ck_collection_processing_claims_artifact_count CHECK (input_count >= 0 AND artifact_count >= 0 AND artifact_bytes >= 0 AND outcome_count >= 0 AND outcome_validation_count >= 0),
	CONSTRAINT ck_collection_processing_claims_id CHECK (length(id) = 64),
	CONSTRAINT ck_collection_processing_claims_work_id CHECK (length(work_id) = 64),
	CONSTRAINT ck_collection_processing_claims_document_sha256 CHECK (length(work_document_sha256) = 64),
	UNIQUE (execution_id),
	FOREIGN KEY(output_collection_id) REFERENCES collections (id) ON DELETE SET NULL,
	CONSTRAINT ck_collection_processing_claims_id_hex CHECK (length(id) = 64 AND lower(id) = id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_work_document_sha256_hex CHECK (length(work_document_sha256) = 64 AND lower(work_document_sha256) = work_document_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_document_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_execution_id_hex CHECK (execution_id IS NULL OR length(execution_id) = 64 AND lower(execution_id) = execution_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(execution_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_c09acb3cbfceaefd CHECK (controller_evidence_sha256 IS NULL OR length(controller_evidence_sha256) = 64 AND lower(controller_evidence_sha256) = controller_evidence_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(controller_evidence_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_operation_sha256_hex CHECK (operation_sha256 IS NULL OR length(operation_sha256) = 64 AND lower(operation_sha256) = operation_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(operation_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_input_set_sha256_hex CHECK (input_set_sha256 IS NULL OR length(input_set_sha256) = 64 AND lower(input_set_sha256) = input_set_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(input_set_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_artifact_set_sha256_hex CHECK (artifact_set_sha256 IS NULL OR length(artifact_set_sha256) = 64 AND lower(artifact_set_sha256) = artifact_set_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(artifact_set_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claims_outcome_set_sha256_hex CHECK (outcome_set_sha256 IS NULL OR length(outcome_set_sha256) = 64 AND lower(outcome_set_sha256) = outcome_set_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(outcome_set_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_processing_claims_expiry ON collection_processing_claims (state, expires_at);

CREATE INDEX ix_collection_processing_claims_owner_created ON collection_processing_claims (consumer_app, created_at, id);

CREATE INDEX ix_collection_processing_claims_owner_execution ON collection_processing_claims (consumer_app, execution_id, id);

CREATE INDEX ix_collection_processing_claims_owner_expires ON collection_processing_claims (consumer_app, expires_at, id);

CREATE INDEX ix_collection_processing_claims_owner_state ON collection_processing_claims (consumer_app, state, updated_at);

CREATE INDEX ix_collection_processing_claims_owner_state_id ON collection_processing_claims (consumer_app, state, id);

CREATE INDEX ix_collection_processing_claims_owner_updated ON collection_processing_claims (consumer_app, updated_at, id);

CREATE INDEX ix_collection_processing_claims_owner_work_id ON collection_processing_claims (consumer_app, work_id, id);

CREATE INDEX ix_collection_processing_claims_work ON collection_processing_claims (work_id, consumer_app);

CREATE TABLE collection_provenance_journals (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	entries BIGINT NOT NULL,
	agent_count BIGINT NOT NULL,
	entity_counts_json TEXT NOT NULL,
	current_state_id VARCHAR NOT NULL,
	current_entry_id VARCHAR NOT NULL,
	current_entry_json_sha256 VARCHAR(64) NOT NULL,
	current_path VARCHAR NOT NULL,
	current_bytes BIGINT NOT NULL,
	current_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (collection_id, journal_id),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	CONSTRAINT ck_provenance_journals_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_provenance_journals_entries CHECK (entries >= 0),
	CONSTRAINT ck_provenance_journals_agent_count CHECK (agent_count >= 0),
	CONSTRAINT ck_provenance_journals_current_bytes CHECK (current_bytes >= 0),
	CONSTRAINT ck_provenance_journals_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_provenance_journals_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_4b4f04aff752e0e1 CHECK (length(current_entry_json_sha256) = 64 AND lower(current_entry_json_sha256) = current_entry_json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(current_entry_json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_provenance_journals_current_sha256_hex CHECK (length(current_sha256) = 64 AND lower(current_sha256) = current_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(current_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_provenance_journals_sha256 ON collection_provenance_journals (sha256, collection_id);

CREATE TABLE collection_provenance_verifications (
	collection_id BIGINT NOT NULL,
	state VARCHAR NOT NULL,
	requested_by_app VARCHAR NOT NULL,
	requested_by_key_id VARCHAR,
	requested_at VARCHAR NOT NULL,
	started_at VARCHAR,
	finished_at VARCHAR,
	next_attempt_at VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	cancel_requested BOOLEAN NOT NULL,
	result_json TEXT,
	failure TEXT,
	phase VARCHAR DEFAULT 'metadata' NOT NULL,
	checkpoint_json TEXT DEFAULT '{}' NOT NULL,
	PRIMARY KEY (collection_id),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_provenance_verifications_state CHECK (state IN ('queued','running','canceling','succeeded','failed','canceled')),
	CONSTRAINT ck_collection_provenance_verifications_attempts CHECK (attempts >= 0),
	CONSTRAINT ck_collection_provenance_verifications_phase CHECK (phase IN ('metadata','identity-tree','identity-bindings','identity-journals','journal-entries','references','reachability','cleanup','complete'))
);

CREATE INDEX ix_collection_provenance_verifications_due ON collection_provenance_verifications (state, next_attempt_at);

CREATE TABLE collection_upload_files (
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	path_sort_key BYTEA NOT NULL,
	semantic_order_rank INTEGER NOT NULL,
	file_order INTEGER NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	raw_part_plaintext_bytes BIGINT,
	raw_part_count BIGINT,
	raw_part_ordered_sha256 VARCHAR(64),
	raw_parts_accepted BIGINT DEFAULT 0 NOT NULL,
	raw_part_commitment_sha256 VARCHAR(64),
	provenance_status VARCHAR NOT NULL,
	provenance_journal_id VARCHAR,
	provenance_current_state_id VARCHAR,
	provenance_omission_reason TEXT,
	custodied_at VARCHAR,
	custody_receipt_json TEXT,
	PRIMARY KEY (collection_id, path),
	FOREIGN KEY(collection_id) REFERENCES collection_uploads (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_upload_files_order CHECK (file_order >= 0),
	CONSTRAINT ck_collection_upload_files_semantic_order_rank CHECK (semantic_order_rank >= 0 AND semantic_order_rank <= 2),
	CONSTRAINT ck_collection_upload_files_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_collection_upload_files_raw_parts CHECK (raw_parts_accepted >= 0),
	CONSTRAINT ck_collection_upload_files_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_upload_files_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_upload_files_raw_part_ordered_sha256_hex CHECK (raw_part_ordered_sha256 IS NULL OR length(raw_part_ordered_sha256) = 64 AND lower(raw_part_ordered_sha256) = raw_part_ordered_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(raw_part_ordered_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_upload_files_raw_part_commitment_sha256_hex CHECK (raw_part_commitment_sha256 IS NULL OR length(raw_part_commitment_sha256) = 64 AND lower(raw_part_commitment_sha256) = raw_part_commitment_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(raw_part_commitment_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX idx_collection_upload_files_collection_order ON collection_upload_files (collection_id, file_order);

CREATE INDEX idx_collection_upload_files_collection_path ON collection_upload_files (collection_id, path_sort_key);

CREATE INDEX idx_collection_upload_files_semantic_order ON collection_upload_files (collection_id, semantic_order_rank, path_sort_key);

CREATE UNIQUE INDEX ux_collection_upload_files_order ON collection_upload_files (collection_id, file_order);

CREATE TABLE collection_upload_provenance_archive_volumes (
	collection_id BIGINT NOT NULL,
	sequence VARCHAR(64) NOT NULL,
	kind VARCHAR NOT NULL,
	document_json TEXT NOT NULL,
	payload_receipt_json TEXT NOT NULL,
	metadata_receipt_json TEXT NOT NULL,
	PRIMARY KEY (collection_id, sequence),
	FOREIGN KEY(collection_id) REFERENCES collection_uploads (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_upload_provenance_archive_volumes_sequence CHECK (length(sequence) = 64 AND lower(sequence) = sequence AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sequence, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_upload_provenance_archive_volumes_kind CHECK (kind IN ('bindings','journal'))
);

CREATE TABLE collection_upload_provenance_journals (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	state VARCHAR NOT NULL,
	accepted_bytes BIGINT DEFAULT 0 NOT NULL,
	next_chunk_ordinal VARCHAR(64) DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' NOT NULL,
	content_hash_state TEXT NOT NULL,
	validation_byte_offset BIGINT DEFAULT 0 NOT NULL,
	validation_sequence BIGINT DEFAULT 0 NOT NULL,
	validation_previous_entry_id VARCHAR,
	validation_previous_json_sha256 VARCHAR(64),
	primary_lineage_id VARCHAR,
	entity_counts_json TEXT DEFAULT '{}' NOT NULL,
	failure TEXT,
	current_state_id VARCHAR,
	current_entry_id VARCHAR,
	current_entry_json_sha256 VARCHAR(64),
	current_path VARCHAR,
	current_bytes BIGINT,
	current_sha256 VARCHAR(64),
	generated_output_path VARCHAR,
	generation_after_journal_id VARCHAR,
	generation_after_state_id VARCHAR,
	PRIMARY KEY (collection_id, journal_id),
	FOREIGN KEY(collection_id) REFERENCES collection_uploads (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_upload_provenance_journals_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_upload_provenance_journals_accepted_bytes CHECK (accepted_bytes >= 0 AND accepted_bytes <= bytes),
	CONSTRAINT ck_upload_provenance_journals_next_chunk_ordinal CHECK (length(next_chunk_ordinal) = 64 AND lower(next_chunk_ordinal) = next_chunk_ordinal AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(next_chunk_ordinal, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_upload_provenance_journals_validation_offset CHECK (validation_byte_offset >= 0 AND validation_byte_offset <= accepted_bytes),
	CONSTRAINT ck_upload_provenance_journals_validation_sequence CHECK (validation_sequence >= 0),
	CONSTRAINT ck_upload_provenance_journals_state CHECK (state IN ('accepting','generating','validating','sealed','failed')),
	CONSTRAINT ck_upload_provenance_journals_current_bytes CHECK (current_bytes IS NULL OR current_bytes >= 0),
	CONSTRAINT ck_upload_provenance_journals_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_upload_provenance_journals_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_164241c9bc3b84b3 CHECK (validation_previous_json_sha256 IS NULL OR length(validation_previous_json_sha256) = 64 AND lower(validation_previous_json_sha256) = validation_previous_json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(validation_previous_json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_beecad13199605d9 CHECK (current_entry_json_sha256 IS NULL OR length(current_entry_json_sha256) = 64 AND lower(current_entry_json_sha256) = current_entry_json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(current_entry_json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_upload_provenance_journals_current_sha256_hex CHECK (current_sha256 IS NULL OR length(current_sha256) = 64 AND lower(current_sha256) = current_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(current_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE key_download_reservations (
	id VARCHAR NOT NULL,
	key_id VARCHAR NOT NULL,
	job_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	month_started_at VARCHAR NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	created_at VARCHAR NOT NULL,
	expires_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(key_id) REFERENCES app_keys (id) ON DELETE CASCADE,
	CONSTRAINT ck_key_download_reservations_kind CHECK (kind IN ('job','stream')),
	CONSTRAINT ck_key_download_reservations_bytes CHECK (reserved_bytes >= 0)
);

CREATE INDEX ix_key_download_reservations_expiry ON key_download_reservations (expires_at, key_id);

CREATE INDEX ix_key_download_reservations_job ON key_download_reservations (job_id, kind);

CREATE INDEX ix_key_download_reservations_key_month ON key_download_reservations (key_id, month_started_at);

CREATE TABLE key_download_usage (
	key_id VARCHAR NOT NULL,
	month_started_at VARCHAR NOT NULL,
	accounted_bytes BIGINT NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (key_id),
	FOREIGN KEY(key_id) REFERENCES app_keys (id) ON DELETE CASCADE,
	CONSTRAINT ck_key_download_usage_bytes CHECK (accounted_bytes >= 0)
);

CREATE TABLE retrieval_cache_population_claims (
	owner VARCHAR NOT NULL,
	source_store VARCHAR NOT NULL,
	collection_id BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (owner, source_store, collection_id, object_id),
	FOREIGN KEY(source_store, collection_id, object_id) REFERENCES retrieval_cache_populations (source_store, collection_id, object_id) ON DELETE CASCADE
);

CREATE INDEX ix_retrieval_cache_population_claims_object ON retrieval_cache_population_claims (source_store, collection_id, object_id, owner);

CREATE TABLE archive_copy_jobs (
	collection_id BIGINT NOT NULL,
	destination_store VARCHAR NOT NULL,
	destination_storage_prefix VARCHAR NOT NULL,
	source_store VARCHAR NOT NULL,
	initiated_by_app VARCHAR NOT NULL,
	initiated_by_key_id VARCHAR,
	event_context_json TEXT,
	state VARCHAR NOT NULL,
	requested_at VARCHAR NOT NULL,
	read_requested_at VARCHAR,
	ready_at VARCHAR,
	expires_at VARCHAR,
	batch_start_order VARCHAR(65),
	batch_end_order VARCHAR(65),
	destination_discarded_at VARCHAR,
	next_attempt_at VARCHAR,
	completed_at VARCHAR,
	failure VARCHAR,
	search_text VARCHAR GENERATED ALWAYS AS (lower(CAST(collection_id AS TEXT) || ' ' || source_store || ' ' || destination_store || ' ' || state)) STORED NOT NULL,
	PRIMARY KEY (collection_id, destination_store),
	FOREIGN KEY(collection_id, source_store) REFERENCES collection_archive_copies (collection_id, store) ON DELETE CASCADE,
	CONSTRAINT ck_archive_copy_jobs_state CHECK (state IN ('requested','waiting','checking','copying','canceling','completed','failed','canceled')),
	CONSTRAINT ck_archive_copy_jobs_batch CHECK (batch_start_order IS NULL AND batch_end_order IS NULL OR batch_start_order IS NOT NULL AND batch_end_order >= batch_start_order),
	CONSTRAINT ck_archive_copy_jobs_batch_start_order CHECK (batch_start_order IS NULL OR length(batch_start_order) = 65 AND lower(batch_start_order) = batch_start_order AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(batch_start_order, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_archive_copy_jobs_batch_end_order CHECK (batch_end_order IS NULL OR length(batch_end_order) = 65 AND lower(batch_end_order) = batch_end_order AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(batch_end_order, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)
);

CREATE INDEX ix_archive_copy_jobs_destination ON archive_copy_jobs (destination_store, collection_id);

CREATE INDEX ix_archive_copy_jobs_due ON archive_copy_jobs (state, next_attempt_at, requested_at);

CREATE INDEX ix_archive_copy_jobs_requested ON archive_copy_jobs (requested_at, collection_id);

CREATE INDEX ix_archive_copy_jobs_search_trgm ON archive_copy_jobs USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_archive_copy_jobs_source ON archive_copy_jobs (source_store, collection_id);

CREATE INDEX ix_archive_copy_jobs_state ON archive_copy_jobs (state, collection_id);

CREATE TABLE archive_copy_retirements (
	collection_id BIGINT NOT NULL,
	store VARCHAR NOT NULL,
	challenge VARCHAR NOT NULL,
	plan_json TEXT NOT NULL,
	started_at VARCHAR NOT NULL,
	PRIMARY KEY (collection_id, store),
	FOREIGN KEY(collection_id, store) REFERENCES collection_archive_copies (collection_id, store) ON DELETE CASCADE
);

CREATE TABLE collection_archive_objects (
	collection_id BIGINT NOT NULL,
	store VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	object_order VARCHAR(65) NOT NULL,
	kind VARCHAR NOT NULL,
	object_path VARCHAR NOT NULL,
	plaintext_bytes BIGINT NOT NULL,
	stored_bytes BIGINT NOT NULL,
	sha256 VARCHAR(64),
	stored_sha256 VARCHAR(64),
	revision VARCHAR,
	age_state_json TEXT,
	archive_parts_json TEXT,
	plan_sha256 VARCHAR(64),
	index_sha256 VARCHAR(64),
	uploaded_at VARCHAR NOT NULL,
	verified_at VARCHAR,
	PRIMARY KEY (collection_id, store, object_id),
	FOREIGN KEY(collection_id, store) REFERENCES collection_archive_copies (collection_id, store) ON DELETE CASCADE,
	CONSTRAINT ck_collection_archive_objects_order CHECK (length(object_order) = 65 AND lower(object_order) = object_order AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(object_order, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_collection_archive_objects_plaintext CHECK (plaintext_bytes >= 0),
	CONSTRAINT ck_collection_archive_objects_stored CHECK (stored_bytes >= 0),
	CONSTRAINT ck_collection_archive_objects_sha256_hex CHECK (sha256 IS NULL OR length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_archive_objects_stored_sha256_hex CHECK (stored_sha256 IS NULL OR length(stored_sha256) = 64 AND lower(stored_sha256) = stored_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(stored_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_archive_objects_plan_sha256_hex CHECK (plan_sha256 IS NULL OR length(plan_sha256) = 64 AND lower(plan_sha256) = plan_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_archive_objects_index_sha256_hex CHECK (index_sha256 IS NULL OR length(index_sha256) = 64 AND lower(index_sha256) = index_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(index_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX idx_collection_archive_objects_order ON collection_archive_objects (collection_id, store, object_order);

CREATE TABLE collection_derivations (
	collection_id BIGINT NOT NULL,
	execution_id VARCHAR(64) NOT NULL,
	claim_id VARCHAR(64) NOT NULL,
	fence BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	document_sha256 VARCHAR(64) NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (collection_id),
	FOREIGN KEY(claim_id) REFERENCES collection_processing_claims (id) ON DELETE RESTRICT,
	CONSTRAINT ck_collection_derivations_fence CHECK (fence >= 1),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	UNIQUE (execution_id),
	CONSTRAINT ck_collection_derivations_execution_id_hex CHECK (length(execution_id) = 64 AND lower(execution_id) = execution_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(execution_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_derivations_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_derivations_document_sha256_hex CHECK (length(document_sha256) = 64 AND lower(document_sha256) = document_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(document_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_derivations_claim ON collection_derivations (claim_id, collection_id);

CREATE TABLE collection_file_provenance (
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	journal_id VARCHAR,
	current_state_id VARCHAR,
	omission_reason TEXT,
	PRIMARY KEY (collection_id, path),
	FOREIGN KEY(collection_id, path) REFERENCES collection_files (collection_id, path) ON DELETE CASCADE,
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_file_provenance_status CHECK (status IN ('captured','omitted')),
	CONSTRAINT ck_collection_file_provenance_binding CHECK (status = 'captured' AND journal_id IS NOT NULL AND current_state_id IS NOT NULL AND omission_reason IS NULL OR status = 'omitted' AND journal_id IS NULL AND current_state_id IS NULL AND omission_reason IS NOT NULL)
);

CREATE INDEX ix_collection_file_provenance_journal ON collection_file_provenance (collection_id, journal_id);

CREATE TABLE collection_processing_claim_inputs (
	claim_id VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	collection_order INTEGER NOT NULL,
	archive_root_sha256 VARCHAR(64) NOT NULL,
	content_identity VARCHAR(64) NOT NULL,
	PRIMARY KEY (claim_id, collection_id),
	CONSTRAINT uq_collection_processing_claim_inputs_order UNIQUE (claim_id, collection_order),
	CONSTRAINT ck_processing_claim_inputs_order CHECK (collection_order >= 0),
	CONSTRAINT ck_claim_inputs_archive_root CHECK (length(archive_root_sha256) = 64),
	CONSTRAINT ck_claim_inputs_content_identity CHECK (length(content_identity) = 64),
	FOREIGN KEY(claim_id) REFERENCES collection_processing_claims (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_processing_claim_inputs_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_0bcbb66e83231f7f CHECK (length(archive_root_sha256) = 64 AND lower(archive_root_sha256) = archive_root_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_root_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claim_inputs_content_identity_hex CHECK (length(content_identity) = 64 AND lower(content_identity) = content_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_processing_claim_inputs_collection ON collection_processing_claim_inputs (collection_id, claim_id);

CREATE TABLE collection_processing_disposition_sets (
	claim_id VARCHAR(64) NOT NULL,
	state VARCHAR NOT NULL,
	disposition_count BIGINT NOT NULL,
	output_edge_count BIGINT NOT NULL,
	output_artifact_count BIGINT NOT NULL,
	transformed_count BIGINT NOT NULL,
	transformed_with_outputs_count BIGINT NOT NULL,
	validation_phase VARCHAR,
	validation_collection_id BIGINT,
	validation_input_path VARCHAR,
	validation_output_path VARCHAR,
	validation_output_collection_id BIGINT,
	validation_output_input_path VARCHAR,
	disposition_hash_state TEXT,
	output_hash_state TEXT,
	disposition_sha256 VARCHAR(64),
	output_sha256 VARCHAR(64),
	identity_sha256 VARCHAR(64),
	failure TEXT,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	sealed_at VARCHAR,
	PRIMARY KEY (claim_id),
	CONSTRAINT ck_processing_disposition_sets_state CHECK (state IN ('receiving','sealing','sealed','failed')),
	CONSTRAINT ck_processing_disposition_sets_phase CHECK (validation_phase IS NULL OR validation_phase IN ('dispositions','outputs')),
	CONSTRAINT ck_processing_disposition_sets_counts CHECK (disposition_count >= 0 AND output_edge_count >= 0 AND output_artifact_count >= 0 AND transformed_count >= 0 AND transformed_with_outputs_count >= 0),
	CONSTRAINT ck_processing_disposition_sets_output_counts CHECK (output_artifact_count <= output_edge_count),
	FOREIGN KEY(claim_id) REFERENCES collection_processing_claims (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_processing_disposition_sets_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_66cafd8d5821f368 CHECK (disposition_sha256 IS NULL OR length(disposition_sha256) = 64 AND lower(disposition_sha256) = disposition_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(disposition_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_disposition_sets_output_sha256_hex CHECK (output_sha256 IS NULL OR length(output_sha256) = 64 AND lower(output_sha256) = output_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(output_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_sha256_11dda87dcbdbf203 CHECK (identity_sha256 IS NULL OR length(identity_sha256) = 64 AND lower(identity_sha256) = identity_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(identity_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_processing_disposition_sets_state ON collection_processing_disposition_sets (state, updated_at, claim_id);

CREATE TABLE collection_processing_outcomes (
	claim_id VARCHAR(64) NOT NULL,
	outcome_id VARCHAR(160) NOT NULL,
	source_claim_id VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	archive_root_sha256 VARCHAR(64) NOT NULL,
	content_identity VARCHAR(64) NOT NULL,
	derivation_sha256 VARCHAR(64) NOT NULL,
	outcome_order BIGINT,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (claim_id, outcome_id),
	CONSTRAINT uq_collection_processing_outcomes_source_claim UNIQUE (claim_id, source_claim_id),
	CONSTRAINT uq_collection_processing_outcomes_output UNIQUE (claim_id, collection_id),
	CONSTRAINT ck_collection_processing_outcomes_order CHECK (outcome_order IS NULL OR outcome_order >= 0),
	FOREIGN KEY(claim_id) REFERENCES collection_processing_claims (id) ON DELETE CASCADE,
	FOREIGN KEY(source_claim_id) REFERENCES collection_processing_claims (id) ON DELETE RESTRICT,
	CONSTRAINT ck_collection_processing_outcomes_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_outcomes_source_claim_id_hex CHECK (length(source_claim_id) = 64 AND lower(source_claim_id) = source_claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(source_claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_outcomes_archive_root_sha256_hex CHECK (length(archive_root_sha256) = 64 AND lower(archive_root_sha256) = archive_root_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(archive_root_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_outcomes_content_identity_hex CHECK (length(content_identity) = 64 AND lower(content_identity) = content_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_outcomes_derivation_sha256_hex CHECK (length(derivation_sha256) = 64 AND lower(derivation_sha256) = derivation_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(derivation_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_processing_outcomes_collection ON collection_processing_outcomes (collection_id, claim_id);

CREATE UNIQUE INDEX ix_collection_processing_outcomes_order ON collection_processing_outcomes (claim_id, outcome_order);

CREATE TABLE collection_provenance_entities (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	entity_type VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	entry_id VARCHAR NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (collection_id, journal_id, entity_type, entity_id),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE
);

CREATE INDEX ix_collection_provenance_entities_type ON collection_provenance_entities (collection_id, entity_type, entity_id);

CREATE TABLE collection_provenance_external_state_references (
	collection_id BIGINT NOT NULL,
	from_journal_id VARCHAR NOT NULL,
	to_journal_id VARCHAR NOT NULL,
	entry_id VARCHAR NOT NULL,
	state_id VARCHAR NOT NULL,
	entry_json_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (collection_id, from_journal_id, to_journal_id, entry_id, state_id),
	FOREIGN KEY(collection_id, from_journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	FOREIGN KEY(collection_id, to_journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	CONSTRAINT ck_sha256_f8b89f54b8ad6ccb CHECK (length(entry_json_sha256) = 64 AND lower(entry_json_sha256) = entry_json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(entry_json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_provenance_external_state_references_target ON collection_provenance_external_state_references (collection_id, to_journal_id);

CREATE TABLE collection_provenance_journal_agents (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	agent_id VARCHAR NOT NULL,
	PRIMARY KEY (collection_id, journal_id, agent_id),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE
);

CREATE INDEX ix_collection_provenance_journal_agents_agent ON collection_provenance_journal_agents (agent_id, collection_id);

CREATE TABLE collection_provenance_journal_chunks (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	ordinal VARCHAR(64) NOT NULL,
	byte_offset BIGINT NOT NULL,
	content BYTEA NOT NULL,
	PRIMARY KEY (collection_id, journal_id, ordinal),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	CONSTRAINT ck_provenance_journal_chunks_ordinal CHECK (length(ordinal) = 64 AND lower(ordinal) = ordinal AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(ordinal, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_provenance_journal_chunks_offset CHECK (byte_offset >= 0),
	CONSTRAINT ck_provenance_journal_chunks_content CHECK (length(content) > 0)
);

CREATE TABLE collection_provenance_verification_agents (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	agent_id VARCHAR NOT NULL,
	PRIMARY KEY (collection_id, journal_id, agent_id),
	FOREIGN KEY(collection_id) REFERENCES collection_provenance_verifications (collection_id) ON DELETE CASCADE
);

CREATE TABLE collection_provenance_verification_entities (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	entity_type VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	entry_id VARCHAR NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (collection_id, journal_id, entity_type, entity_id),
	FOREIGN KEY(collection_id) REFERENCES collection_provenance_verifications (collection_id) ON DELETE CASCADE
);

CREATE INDEX ix_provenance_verification_entities_entry ON collection_provenance_verification_entities (collection_id, journal_id, entry_id);

CREATE TABLE collection_provenance_verification_entries (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	entry_id VARCHAR NOT NULL,
	json_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (collection_id, journal_id, entry_id),
	FOREIGN KEY(collection_id) REFERENCES collection_provenance_verifications (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_sha256_10ab1519eb5bc179 CHECK (length(json_sha256) = 64 AND lower(json_sha256) = json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE collection_provenance_verification_external_states (
	collection_id BIGINT NOT NULL,
	from_journal_id VARCHAR NOT NULL,
	to_journal_id VARCHAR NOT NULL,
	entry_id VARCHAR NOT NULL,
	state_id VARCHAR NOT NULL,
	entry_json_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (collection_id, from_journal_id, to_journal_id, entry_id, state_id),
	FOREIGN KEY(collection_id) REFERENCES collection_provenance_verifications (collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_sha256_5bae1ac3001e4f72 CHECK (length(entry_json_sha256) = 64 AND lower(entry_json_sha256) = entry_json_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(entry_json_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_provenance_verification_external_states_target ON collection_provenance_verification_external_states (collection_id, to_journal_id);

CREATE TABLE collection_provenance_verification_reachability (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	expanded BOOLEAN DEFAULT false NOT NULL,
	after_to_journal_id VARCHAR,
	after_entry_id VARCHAR,
	after_state_id VARCHAR,
	PRIMARY KEY (collection_id, journal_id),
	FOREIGN KEY(collection_id) REFERENCES collection_provenance_verifications (collection_id) ON DELETE CASCADE
);

CREATE INDEX ix_provenance_verification_reachability_work ON collection_provenance_verification_reachability (collection_id, expanded, journal_id);

CREATE TABLE collection_transform_capabilities (
	id VARCHAR(32) NOT NULL,
	claim_id VARCHAR(64) NOT NULL,
	fence BIGINT NOT NULL,
	audience VARCHAR(300) NOT NULL,
	token_sha256 VARCHAR(64) NOT NULL,
	actions_json TEXT NOT NULL,
	artifact_count BIGINT NOT NULL,
	artifact_bytes BIGINT NOT NULL,
	artifact_hash_state TEXT,
	artifact_set_sha256 VARCHAR(64),
	artifacts_sealed_at VARCHAR,
	state VARCHAR NOT NULL,
	expires_at VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	revoked_at VARCHAR,
	PRIMARY KEY (id),
	CONSTRAINT ck_collection_transform_capabilities_state CHECK (state IN ('receiving','active','revoked')),
	CONSTRAINT ck_collection_transform_capabilities_fence CHECK (fence >= 1),
	CONSTRAINT ck_collection_transform_capabilities_artifact_totals CHECK (artifact_count >= 0 AND artifact_bytes >= 0),
	FOREIGN KEY(claim_id) REFERENCES collection_processing_claims (id) ON DELETE CASCADE,
	UNIQUE (token_sha256),
	CONSTRAINT ck_collection_transform_capabilities_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_transform_capabilities_token_sha256_hex CHECK (length(token_sha256) = 64 AND lower(token_sha256) = token_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(token_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_transform_capabilities_artifact_set_sha256_hex CHECK (artifact_set_sha256 IS NULL OR length(artifact_set_sha256) = 64 AND lower(artifact_set_sha256) = artifact_set_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(artifact_set_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_transform_capabilities_claim_state ON collection_transform_capabilities (claim_id, state, expires_at);

CREATE TABLE collection_upload_provenance_journal_chunks (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	ordinal VARCHAR(64) NOT NULL,
	byte_offset BIGINT NOT NULL,
	content BYTEA NOT NULL,
	PRIMARY KEY (collection_id, journal_id, ordinal),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_upload_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	CONSTRAINT ck_upload_provenance_journal_chunks_ordinal CHECK (length(ordinal) = 64 AND lower(ordinal) = ordinal AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(ordinal, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0),
	CONSTRAINT ck_upload_provenance_journal_chunks_offset CHECK (byte_offset >= 0),
	CONSTRAINT ck_upload_provenance_journal_chunks_content CHECK (length(content) > 0)
);

CREATE TABLE collection_upload_provenance_reachability (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	after_external_fact_key VARCHAR,
	expanded BOOLEAN DEFAULT false NOT NULL,
	PRIMARY KEY (collection_id, journal_id),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_upload_provenance_journals (collection_id, journal_id) ON DELETE CASCADE
);

CREATE INDEX ix_upload_provenance_reachability_pending ON collection_upload_provenance_reachability (collection_id, expanded, journal_id);

CREATE TABLE collection_upload_provenance_sources (
	collection_id BIGINT NOT NULL,
	source_collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	expanded BOOLEAN DEFAULT false NOT NULL,
	after_to_journal_id VARCHAR,
	after_entry_id VARCHAR,
	after_state_id VARCHAR,
	copied BOOLEAN DEFAULT false NOT NULL,
	copy_offset BIGINT DEFAULT 0 NOT NULL,
	PRIMARY KEY (collection_id, source_collection_id, journal_id),
	FOREIGN KEY(collection_id) REFERENCES collection_uploads (collection_id) ON DELETE CASCADE,
	FOREIGN KEY(source_collection_id, journal_id) REFERENCES collection_provenance_journals (collection_id, journal_id) ON DELETE RESTRICT,
	CONSTRAINT ck_upload_provenance_sources_offset CHECK (copy_offset >= 0)
);

CREATE INDEX ix_collection_upload_provenance_sources_work ON collection_upload_provenance_sources (collection_id, expanded, copied, source_collection_id, journal_id);

CREATE TABLE collection_upload_provenance_validation_facts (
	collection_id BIGINT NOT NULL,
	journal_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	fact_key VARCHAR NOT NULL,
	value_json TEXT NOT NULL,
	PRIMARY KEY (collection_id, journal_id, kind, fact_key),
	FOREIGN KEY(collection_id, journal_id) REFERENCES collection_upload_provenance_journals (collection_id, journal_id) ON DELETE CASCADE,
	CONSTRAINT ck_upload_provenance_validation_fact_kind CHECK (kind IN ('entry','agent','event','state','binding','entity','external-state'))
);

CREATE TABLE collection_upload_raw_part_digests (
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	part_number BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (collection_id, path, part_number),
	FOREIGN KEY(collection_id, path) REFERENCES collection_upload_files (collection_id, path) ON DELETE CASCADE,
	CONSTRAINT ck_upload_raw_part_digest_number CHECK (part_number >= 0),
	CONSTRAINT ck_upload_raw_part_digest_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_upload_raw_part_digests_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE retrieval_plan_files (
	plan_id VARCHAR NOT NULL,
	file_order INTEGER NOT NULL,
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	source_store VARCHAR NOT NULL,
	requires_restore BOOLEAN NOT NULL,
	PRIMARY KEY (plan_id, file_order),
	FOREIGN KEY(plan_id) REFERENCES retrieval_plans (id) ON DELETE CASCADE,
	FOREIGN KEY(collection_id, path) REFERENCES collection_files (collection_id, path),
	UNIQUE (plan_id, collection_id, path),
	CONSTRAINT ck_retrieval_plan_files_order CHECK (file_order >= 0),
	CONSTRAINT ck_retrieval_plan_files_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_retrieval_plan_files_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_retrieval_plan_files_collection ON retrieval_plan_files (collection_id, plan_id);

CREATE TABLE archive_copy_object_uploads (
	collection_id BIGINT NOT NULL,
	destination_store VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	object_path VARCHAR NOT NULL,
	plaintext_bytes BIGINT NOT NULL,
	sha256 VARCHAR(64),
	write_token VARCHAR,
	expected_stored_bytes BIGINT,
	write_segments_json VARCHAR,
	uploaded_bytes BIGINT NOT NULL,
	uploaded_segments INTEGER NOT NULL,
	total_segments INTEGER NOT NULL,
	PRIMARY KEY (collection_id, destination_store, object_id),
	FOREIGN KEY(collection_id, destination_store) REFERENCES archive_copy_jobs (collection_id, destination_store) ON DELETE CASCADE,
	CONSTRAINT ck_archive_copy_uploads_plaintext CHECK (plaintext_bytes >= 0),
	CONSTRAINT ck_archive_copy_uploads_uploaded_bytes CHECK (uploaded_bytes >= 0),
	CONSTRAINT ck_archive_copy_uploads_uploaded_segments CHECK (uploaded_segments >= 0),
	CONSTRAINT ck_archive_copy_uploads_total_segments CHECK (total_segments >= 0),
	CONSTRAINT ck_archive_copy_uploads_segment_progress CHECK (uploaded_segments <= total_segments),
	CONSTRAINT ck_archive_copy_object_uploads_sha256_hex CHECK (sha256 IS NULL OR length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE collection_archive_file_objects (
	collection_id BIGINT NOT NULL,
	store VARCHAR NOT NULL,
	path VARCHAR NOT NULL,
	sequence BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	file_offset BIGINT NOT NULL,
	object_offset BIGINT NOT NULL,
	bytes BIGINT NOT NULL,
	member VARCHAR,
	PRIMARY KEY (collection_id, store, path, sequence),
	FOREIGN KEY(collection_id, store, object_id) REFERENCES collection_archive_objects (collection_id, store, object_id) ON DELETE CASCADE,
	FOREIGN KEY(collection_id, path) REFERENCES collection_files (collection_id, path) ON DELETE CASCADE,
	CONSTRAINT ck_archive_file_objects_sequence CHECK (sequence >= 0),
	CONSTRAINT ck_archive_file_objects_file_offset CHECK (file_offset >= 0),
	CONSTRAINT ck_archive_file_objects_object_offset CHECK (object_offset >= 0),
	CONSTRAINT ck_archive_file_objects_bytes CHECK (bytes >= 0)
);

CREATE INDEX idx_collection_archive_file_objects_object ON collection_archive_file_objects (collection_id, store, object_id);

CREATE TABLE collection_processing_claim_artifacts (
	claim_id VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	artifact_order BIGINT NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (claim_id, collection_id, path),
	FOREIGN KEY(claim_id, collection_id) REFERENCES collection_processing_claim_inputs (claim_id, collection_id) ON DELETE CASCADE,
	CONSTRAINT ck_processing_claim_artifacts_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_processing_claim_artifacts_order CHECK (artifact_order >= 0),
	CONSTRAINT ck_processing_claim_artifacts_sha256 CHECK (length(sha256) = 64),
	CONSTRAINT ck_collection_processing_claim_artifacts_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_collection_processing_claim_artifacts_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_processing_claim_artifacts_collection ON collection_processing_claim_artifacts (collection_id, path, claim_id);

CREATE UNIQUE INDEX ix_collection_processing_claim_artifacts_order ON collection_processing_claim_artifacts (claim_id, artifact_order);

CREATE TABLE collection_transform_capability_artifacts (
	capability_id VARCHAR(32) NOT NULL,
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	artifact_order BIGINT NOT NULL,
	bytes BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (capability_id, collection_id, path),
	CONSTRAINT ck_capability_artifacts_bytes CHECK (bytes >= 0),
	CONSTRAINT ck_capability_artifacts_order CHECK (artifact_order >= 0),
	CONSTRAINT ck_capability_artifacts_sha256 CHECK (length(sha256) = 64),
	FOREIGN KEY(capability_id) REFERENCES collection_transform_capabilities (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_transform_capability_artifacts_sha256_hex CHECK (length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_transform_capability_artifacts_collection ON collection_transform_capability_artifacts (collection_id, path, capability_id);

CREATE UNIQUE INDEX ix_collection_transform_capability_artifacts_order ON collection_transform_capability_artifacts (capability_id, artifact_order);

CREATE TABLE retrieval_cache_objects (
	source_store VARCHAR NOT NULL,
	collection_id BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	cache_store VARCHAR NOT NULL,
	object_path VARCHAR NOT NULL,
	revision VARCHAR,
	stored_bytes BIGINT NOT NULL,
	stored_sha256 VARCHAR(64),
	cached_at VARCHAR NOT NULL,
	verified_at VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	search_text VARCHAR GENERATED ALWAYS AS (lower(source_store || ' ' || cache_store || ' ' || object_id)) STORED NOT NULL,
	PRIMARY KEY (source_store, collection_id, object_id),
	FOREIGN KEY(collection_id, source_store, object_id) REFERENCES collection_archive_objects (collection_id, store, object_id) ON DELETE CASCADE,
	CONSTRAINT ck_retrieval_cache_objects_bytes CHECK (stored_bytes >= 0),
	CONSTRAINT ck_retrieval_cache_objects_sha256 CHECK (stored_sha256 IS NULL OR length(stored_sha256) = 64),
	CONSTRAINT ck_retrieval_cache_objects_state CHECK (state IN ('ready','delete_pending','deleting')),
	CONSTRAINT ck_retrieval_cache_objects_stored_sha256_hex CHECK (stored_sha256 IS NULL OR length(stored_sha256) = 64 AND lower(stored_sha256) = stored_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(stored_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_retrieval_cache_objects_bytes ON retrieval_cache_objects (stored_bytes, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_cached ON retrieval_cache_objects (cached_at, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_cleanup ON retrieval_cache_objects (state, cached_at);

CREATE INDEX ix_retrieval_cache_objects_collection ON retrieval_cache_objects (collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_object ON retrieval_cache_objects (object_id, collection_id, source_store);

CREATE INDEX ix_retrieval_cache_objects_search_trgm ON retrieval_cache_objects USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_retrieval_cache_objects_store_cleanup ON retrieval_cache_objects (cache_store, state, cached_at, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_verified ON retrieval_cache_objects (verified_at, collection_id, source_store, object_id);

CREATE TABLE retrieval_plan_objects (
	plan_id VARCHAR NOT NULL,
	object_order VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	source_store VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	plaintext_bytes BIGINT NOT NULL,
	stored_bytes BIGINT NOT NULL,
	sha256 VARCHAR(64),
	read_mode VARCHAR NOT NULL,
	cache_store VARCHAR,
	retrieval_bytes VARCHAR(64) NOT NULL,
	PRIMARY KEY (plan_id, object_order),
	FOREIGN KEY(plan_id) REFERENCES retrieval_plans (id) ON DELETE CASCADE,
	FOREIGN KEY(collection_id, source_store, object_id) REFERENCES collection_archive_objects (collection_id, store, object_id),
	UNIQUE (plan_id, collection_id, source_store, object_id),
	CONSTRAINT ck_retrieval_plan_objects_kind CHECK (kind IN ('pack','segment')),
	CONSTRAINT ck_retrieval_plan_objects_read_mode CHECK (read_mode IN ('immediate','restore_required','cache')),
	CONSTRAINT ck_retrieval_plan_objects_plaintext CHECK (plaintext_bytes >= 0),
	CONSTRAINT ck_retrieval_plan_objects_stored CHECK (stored_bytes > 0),
	CONSTRAINT ck_retrieval_plan_objects_sha256_hex CHECK (sha256 IS NULL OR length(sha256) = 64 AND lower(sha256) = sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_retrieval_plan_objects_copy ON retrieval_plan_objects (collection_id, source_store, plan_id);

CREATE TABLE retrieval_plan_placements (
	plan_id VARCHAR NOT NULL,
	file_order INTEGER NOT NULL,
	sequence VARCHAR(64) NOT NULL,
	object_order VARCHAR(64) NOT NULL,
	file_offset BIGINT NOT NULL,
	object_offset BIGINT NOT NULL,
	bytes BIGINT NOT NULL,
	member VARCHAR,
	PRIMARY KEY (plan_id, file_order, sequence),
	FOREIGN KEY(plan_id, file_order) REFERENCES retrieval_plan_files (plan_id, file_order) ON DELETE CASCADE,
	FOREIGN KEY(plan_id, object_order) REFERENCES retrieval_plan_objects (plan_id, object_order),
	CONSTRAINT ck_retrieval_plan_placements_file_offset CHECK (file_offset >= 0),
	CONSTRAINT ck_retrieval_plan_placements_object_offset CHECK (object_offset >= 0),
	CONSTRAINT ck_retrieval_plan_placements_bytes CHECK (bytes >= 0)
);

CREATE INDEX ix_retrieval_plan_placements_object ON retrieval_plan_placements (plan_id, object_order);

CREATE TABLE retrieval_job_object_progress (
	job_id VARCHAR NOT NULL,
	object_order VARCHAR(64) NOT NULL,
	plan_id VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	prepare_requested_at VARCHAR,
	next_poll_at VARCHAR NOT NULL,
	cache_store VARCHAR,
	PRIMARY KEY (job_id, object_order),
	FOREIGN KEY(job_id, plan_id) REFERENCES retrieval_jobs (id, plan_id) ON DELETE CASCADE,
	FOREIGN KEY(plan_id, object_order) REFERENCES retrieval_plan_objects (plan_id, object_order),
	CONSTRAINT ck_retrieval_job_object_progress_state CHECK (state IN ('preparing','requested','ready'))
);

CREATE INDEX ix_retrieval_job_object_progress_due ON retrieval_job_object_progress (state, next_poll_at, job_id);

CREATE TABLE collection_processing_dispositions (
	claim_id VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	path VARCHAR NOT NULL,
	disposition_order BIGINT,
	status VARCHAR NOT NULL,
	failure_code VARCHAR,
	failure_message TEXT,
	PRIMARY KEY (claim_id, collection_id, path),
	FOREIGN KEY(claim_id, collection_id, path) REFERENCES collection_processing_claim_artifacts (claim_id, collection_id, path) ON DELETE CASCADE,
	CONSTRAINT ck_processing_dispositions_status CHECK (status IN ('transformed','preserved','omitted','rejected')),
	CONSTRAINT ck_processing_dispositions_order_nonnegative CHECK (disposition_order IS NULL OR disposition_order >= 0),
	CONSTRAINT ck_collection_processing_dispositions_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE UNIQUE INDEX ix_processing_dispositions_order ON collection_processing_dispositions (claim_id, disposition_order);

CREATE TABLE retrieval_cache_leases (
	owner VARCHAR NOT NULL,
	source_store VARCHAR NOT NULL,
	collection_id BIGINT NOT NULL,
	object_id VARCHAR NOT NULL,
	expires_at VARCHAR NOT NULL,
	PRIMARY KEY (owner, source_store, collection_id, object_id),
	FOREIGN KEY(source_store, collection_id, object_id) REFERENCES retrieval_cache_objects (source_store, collection_id, object_id) ON DELETE CASCADE
);

CREATE INDEX ix_retrieval_cache_leases_expiry ON retrieval_cache_leases (expires_at, owner);

CREATE INDEX ix_retrieval_cache_leases_object_expiry ON retrieval_cache_leases (source_store, collection_id, object_id, expires_at, owner);

CREATE TABLE collection_processing_disposition_outputs (
	claim_id VARCHAR(64) NOT NULL,
	output_path VARCHAR NOT NULL,
	input_collection_id BIGINT NOT NULL,
	input_path VARCHAR NOT NULL,
	output_order BIGINT,
	PRIMARY KEY (claim_id, output_path, input_collection_id, input_path),
	FOREIGN KEY(claim_id, input_collection_id, input_path) REFERENCES collection_processing_dispositions (claim_id, collection_id, path) ON DELETE CASCADE,
	CONSTRAINT ck_processing_disposition_outputs_order_nonnegative CHECK (output_order IS NULL OR output_order >= 0),
	CONSTRAINT ck_collection_processing_disposition_outputs_claim_id_hex CHECK (length(claim_id) = 64 AND lower(claim_id) = claim_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(claim_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE UNIQUE INDEX ix_processing_disposition_outputs_order ON collection_processing_disposition_outputs (claim_id, output_order);

CREATE INDEX ix_processing_disposition_outputs_source ON collection_processing_disposition_outputs (claim_id, input_collection_id, input_path, output_path);

CREATE TABLE collection_access_groups (
	id VARCHAR(64) NOT NULL,
	creation_idempotency_key VARCHAR NOT NULL,
	created_by_app VARCHAR NOT NULL,
	created_by_key_id VARCHAR,
	display_label VARCHAR,
	display_label_sort VARCHAR GENERATED ALWAYS AS (lower(coalesce(display_label, ''))) STORED NOT NULL,
	status VARCHAR DEFAULT 'active' NOT NULL,
	authorization_revision BIGINT DEFAULT 1 NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	collection_count BIGINT DEFAULT 0 NOT NULL,
	search_text VARCHAR GENERATED ALWAYS AS (lower(id || ' ' || coalesce(display_label, ''))) STORED NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_collection_access_groups_application_idempotency_key UNIQUE (created_by_app, creation_idempotency_key),
	CONSTRAINT ck_collection_access_groups_collection_count CHECK (collection_count >= 0),
	CONSTRAINT ck_collection_access_groups_status CHECK (status IN ('active','disabled')),
	CONSTRAINT ck_collection_access_groups_authorization_revision CHECK (authorization_revision >= 1),
	CONSTRAINT ck_collection_access_groups_id_hex CHECK (length(id) = 64 AND lower(id) = id AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)
);

CREATE INDEX ix_collection_access_groups_collection_count_id ON collection_access_groups (collection_count, id);

CREATE INDEX ix_collection_access_groups_created_at_id ON collection_access_groups (created_at, id);

CREATE INDEX ix_collection_access_groups_display_label_id ON collection_access_groups (display_label_sort, id);

CREATE INDEX ix_collection_access_groups_search_trgm ON collection_access_groups USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_collection_access_groups_status_id ON collection_access_groups (status, id);

CREATE INDEX ix_collection_access_groups_updated_at_id ON collection_access_groups (updated_at, id);

CREATE TABLE catalog_event_access_groups (
	sequence BIGINT NOT NULL,
	phase VARCHAR NOT NULL,
	group_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (sequence, phase, group_id),
	FOREIGN KEY(sequence) REFERENCES catalog_events (sequence) ON DELETE CASCADE,
	CONSTRAINT ck_catalog_event_access_groups_phase CHECK (phase IN ('before', 'after')),
	CONSTRAINT ck_catalog_event_access_groups_group_id_hex CHECK (length(group_id) = 64 AND lower(group_id) = group_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(group_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_catalog_event_access_groups_visibility ON catalog_event_access_groups (phase, group_id, sequence);

CREATE TABLE collection_access_group_memberships (
	collection_id BIGINT NOT NULL,
	group_id VARCHAR(64) NOT NULL,
	added_by_app VARCHAR NOT NULL,
	added_by_key_id VARCHAR,
	added_at VARCHAR NOT NULL,
	PRIMARY KEY (collection_id, group_id),
	FOREIGN KEY(collection_id) REFERENCES collections (id) ON DELETE CASCADE,
	FOREIGN KEY(group_id) REFERENCES collection_access_groups (id) ON DELETE CASCADE,
	CONSTRAINT ck_collection_access_group_memberships_group_id_hex CHECK (length(group_id) = 64 AND lower(group_id) = group_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(group_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_collection_access_group_memberships_group ON collection_access_group_memberships (group_id, collection_id);

INSERT INTO state_schema_revision (version_num) VALUES ('v1_0001');
