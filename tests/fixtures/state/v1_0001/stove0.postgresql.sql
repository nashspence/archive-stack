-- Exact current Stove0 PostgreSQL v1 baseline conformance fixture.

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE TABLE stove0_state_schema_revision (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT stove0_state_schema_revision_pkc PRIMARY KEY (version_num)
);

CREATE TABLE stove0_artifact_selections (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_count INTEGER NOT NULL,
	total_bytes BIGINT NOT NULL,
	PRIMARY KEY (selection_sha256),
	CONSTRAINT ck_stove0_selections_id CHECK (length(selection_sha256) = 64),
	CONSTRAINT ck_stove0_selections_count CHECK (artifact_count >= 0),
	CONSTRAINT ck_stove0_selections_bytes CHECK (total_bytes >= 0),
	CONSTRAINT ck_stove0_artifact_selections_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE stove0_evaluation_records (
	evaluation_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	phase VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (evaluation_id),
	CONSTRAINT ck_stove0_evaluation_records_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_evaluation_records_phase CHECK (phase IN ('planning','running','partially_complete','complete','failed','canceled')),
	CONSTRAINT ck_stove0_evaluation_records_id CHECK (length(evaluation_id) = 64),
	CONSTRAINT ck_stove0_evaluation_records_document_bytes CHECK (document_bytes >= 0),
	CONSTRAINT ck_stove0_evaluation_records_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_evaluation_records_id_trgm ON stove0_evaluation_records USING gin (evaluation_id gin_trgm_ops);

CREATE INDEX ix_stove0_evaluation_records_phase_id ON stove0_evaluation_records (phase, evaluation_id);

CREATE INDEX ix_stove0_evaluation_records_updated_id ON stove0_evaluation_records (updated_at, evaluation_id);

CREATE TABLE stove0_event_cursors (
	stream VARCHAR(160) NOT NULL,
	cursor VARCHAR(500) NOT NULL,
	revision INTEGER NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (stream),
	CONSTRAINT ck_stove0_event_cursors_revision CHECK (revision >= 1)
);

CREATE TABLE stove0_lifecycle_events (
	sequence SERIAL NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	event_bytes BIGINT NOT NULL,
	event_json TEXT NOT NULL,
	PRIMARY KEY (sequence),
	CONSTRAINT ck_stove0_lifecycle_events_event_bytes CHECK (event_bytes >= 0)
);

CREATE INDEX ix_stove0_lifecycle_events_created_at ON stove0_lifecycle_events (created_at);

CREATE TABLE stove0_work_records (
	work_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	phase VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id),
	CONSTRAINT ck_stove0_work_records_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_work_records_phase CHECK (phase IN ('eligible','claimed','observing','planning','target_preflight','queued','executing','output_finalizing','verifying','settled','retirement_pending','coordinating','abandon_pending','complete','inapplicable','failed','canceled')),
	CONSTRAINT ck_stove0_work_records_id CHECK (length(work_id) = 64),
	CONSTRAINT ck_stove0_work_records_document_bytes CHECK (document_bytes >= 0),
	CONSTRAINT ck_stove0_work_records_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_work_records_id_trgm ON stove0_work_records USING gin (work_id gin_trgm_ops);

CREATE INDEX ix_stove0_work_records_phase_work_id ON stove0_work_records (phase, work_id);

CREATE INDEX ix_stove0_work_records_updated_work_id ON stove0_work_records (updated_at, work_id);

CREATE TABLE stove0_artifact_selection_members (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_id VARCHAR(160) NOT NULL,
	continuation_sha256 VARCHAR(64) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (selection_sha256, artifact_id),
	CONSTRAINT ck_stove0_selection_members_artifact_id CHECK (length(artifact_id) >= 1),
	CONSTRAINT ck_stove0_selection_members_continuation CHECK (length(continuation_sha256) = 64),
	CONSTRAINT ck_stove0_selection_members_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(selection_sha256) REFERENCES stove0_artifact_selections (selection_sha256) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_artifact_selection_members_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_artifact_selection_members_continuation_sha256_hex CHECK (length(continuation_sha256) = 64 AND lower(continuation_sha256) = continuation_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(continuation_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE UNIQUE INDEX ix_stove0_selection_members_continuation ON stove0_artifact_selection_members (selection_sha256, continuation_sha256);

CREATE TABLE stove0_evaluation_children (
	evaluation_id VARCHAR(64) NOT NULL,
	work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (evaluation_id, work_id),
	FOREIGN KEY(evaluation_id) REFERENCES stove0_evaluation_records (evaluation_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_evaluation_children_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_evaluation_children_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_evaluation_children_work ON stove0_evaluation_children (work_id, evaluation_id);

CREATE TABLE stove0_target_input_dispositions (
	work_id VARCHAR(64) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	status VARCHAR(32) NOT NULL,
	PRIMARY KEY (work_id, input_id),
	CONSTRAINT ck_stove0_target_dispositions_id CHECK (length(input_id) >= 1),
	CONSTRAINT ck_stove0_target_dispositions_status CHECK (status IN ('omitted','preserved','rejected','transformed')),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_input_dispositions_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE TABLE stove0_target_outputs (
	work_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	output_path VARCHAR(4096) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, output_id),
	CONSTRAINT ck_stove0_target_outputs_id CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_outputs_path CHECK (length(output_path) >= 1),
	CONSTRAINT ck_stove0_target_outputs_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_outputs_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE UNIQUE INDEX uq_stove0_target_outputs_path ON stove0_target_outputs (work_id, output_path);

CREATE TABLE stove0_target_source_edges (
	work_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	PRIMARY KEY (work_id, output_id, input_id),
	CONSTRAINT ck_stove0_target_source_edges_output CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_source_edges_input CHECK (length(input_id) >= 1),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_source_edges_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_target_source_edges_input ON stove0_target_source_edges (work_id, input_id, output_id);

CREATE TABLE stove0_work_evaluations (
	work_id VARCHAR(64) NOT NULL,
	evaluation_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, evaluation_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_evaluations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_evaluations_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_work_evaluations_evaluation ON stove0_work_evaluations (evaluation_id, work_id);

CREATE TABLE stove0_work_relations (
	work_id VARCHAR(64) NOT NULL,
	related_work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, related_work_id),
	CONSTRAINT ck_stove0_work_relations_distinct CHECK (work_id <> related_work_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_relations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_relations_related_work_id_hex CHECK (length(related_work_id) = 64 AND lower(related_work_id) = related_work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(related_work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_work_relations_related ON stove0_work_relations (related_work_id, work_id);

CREATE TABLE stove0_work_selection_references (
	work_id VARCHAR(64) NOT NULL,
	selection_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, selection_sha256),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_selection_references_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_selection_references_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
);

CREATE INDEX ix_stove0_work_selection_references_selection ON stove0_work_selection_references (selection_sha256, work_id);

INSERT INTO stove0_state_schema_revision (version_num) VALUES ('v1_0001');
