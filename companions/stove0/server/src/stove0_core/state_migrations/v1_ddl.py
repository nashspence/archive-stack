"""Immutable DDL snapshot for the Stove0 control state v1 baseline."""

# ruff: noqa: E501

# This module is migration authority. Runtime model metadata must never be imported here.

_ADMISSION_DDL: tuple[str, ...] = (
    """
CREATE TABLE stove0_admission_policies (
	policy_id VARCHAR(160) NOT NULL,
	policy_revision INTEGER NOT NULL,
	policy_sha256 VARCHAR(64) NOT NULL,
	phase VARCHAR(32) NOT NULL,
	generation VARCHAR(64) NOT NULL,
	source_identity VARCHAR(64),
	authorization_view_identity VARCHAR(64),
	cursor VARCHAR(4096),
	baseline_mode VARCHAR(16) NOT NULL,
	through_revision VARCHAR(19) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (policy_id),
	CONSTRAINT ck_stove0_admission_policy_revision CHECK (policy_revision >= 1),
	CONSTRAINT ck_stove0_admission_policy_id CHECK (length(policy_id) >= 1),
	CONSTRAINT ck_stove0_admission_policy_phase CHECK (phase IN ('new','baseline','following','reset_required')),
	CONSTRAINT ck_stove0_admission_policy_baseline_mode CHECK (baseline_mode IN ('observe','backfill')),
	CONSTRAINT ck_stove0_admission_policies_policy_sha256_hex CHECK (length(policy_sha256) = 64 AND lower(policy_sha256) = policy_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(policy_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_policies_generation_hex CHECK (length(generation) = 64 AND lower(generation) = generation AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(generation, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_policies_source_identity_hex CHECK (source_identity IS NULL OR length(source_identity) = 64 AND lower(source_identity) = source_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(source_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_policies_authorization_view_identity_hex CHECK (authorization_view_identity IS NULL OR length(authorization_view_identity) = 64 AND lower(authorization_view_identity) = authorization_view_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(authorization_view_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_policies_phase ON stove0_admission_policies (phase, policy_id)
    """.strip(),
    """
CREATE TABLE stove0_admission_matches (
	policy_id VARCHAR(160) NOT NULL,
	generation VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	matched BOOLEAN NOT NULL,
	descriptor_revision VARCHAR(19) NOT NULL,
	tag_revision BIGINT NOT NULL,
	tag_set_identity VARCHAR(64) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (policy_id, generation, collection_id),
	CONSTRAINT ck_stove0_admission_match_collection CHECK (collection_id >= 1),
	CONSTRAINT ck_stove0_admission_match_tag_revision CHECK (tag_revision >= 1),
	CONSTRAINT ck_stove0_admission_match_bytes CHECK (document_bytes >= 0),
	CONSTRAINT ck_stove0_admission_matches_generation_hex CHECK (length(generation) = 64 AND lower(generation) = generation AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(generation, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_matches_tag_set_identity_hex CHECK (length(tag_set_identity) = 64 AND lower(tag_set_identity) = tag_set_identity AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(tag_set_identity, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_matches_collection ON stove0_admission_matches (collection_id, policy_id, generation)
    """.strip(),
    """
CREATE TABLE stove0_admission_observed_revisions (
	policy_id VARCHAR(160) NOT NULL,
	generation VARCHAR(64) NOT NULL,
	collection_id BIGINT NOT NULL,
	descriptor_revision VARCHAR(19) NOT NULL,
	operation VARCHAR(8) NOT NULL,
	authority_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (policy_id, generation, collection_id),
	CONSTRAINT ck_stove0_admission_observed_revision_collection CHECK (collection_id >= 1),
	CONSTRAINT ck_stove0_admission_observed_revision_operation CHECK (operation IN ('upsert','delete')),
	CONSTRAINT ck_stove0_admission_observed_revisions_generation_hex CHECK (length(generation) = 64 AND lower(generation) = generation AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(generation, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_observed_revisions_authority_sha256_hex CHECK (length(authority_sha256) = 64 AND lower(authority_sha256) = authority_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(authority_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_observed_revisions_collection ON stove0_admission_observed_revisions (collection_id, policy_id, generation)
    """.strip(),
    """
CREATE TABLE stove0_admission_candidates (
	admission_id VARCHAR(64) NOT NULL,
	policy_id VARCHAR(160) NOT NULL,
	state VARCHAR(16) NOT NULL,
	preview_sha256 VARCHAR(64),
	work_id VARCHAR(64),
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	preview_bytes BIGINT,
	preview_json TEXT,
	attempt_count INTEGER NOT NULL,
	next_attempt_at VARCHAR(40),
	failure TEXT,
	created_at VARCHAR(40) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (admission_id),
	CONSTRAINT ck_stove0_admission_candidate_state CHECK (state IN ('intent','previewed','work_bound')),
	CONSTRAINT ck_stove0_admission_candidate_bytes CHECK (document_bytes >= 0),
	CONSTRAINT ck_stove0_admission_candidate_preview_bytes CHECK (preview_bytes IS NULL OR preview_bytes >= 0),
	CONSTRAINT ck_stove0_admission_candidate_attempt_count CHECK (attempt_count >= 0),
	CONSTRAINT ck_stove0_admission_candidate_next_attempt CHECK (state = 'work_bound' AND next_attempt_at IS NULL OR state != 'work_bound' AND next_attempt_at IS NOT NULL),
	CONSTRAINT ck_stove0_admission_candidates_admission_id_hex CHECK (length(admission_id) = 64 AND lower(admission_id) = admission_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(admission_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_candidates_preview_sha256_hex CHECK (preview_sha256 IS NULL OR length(preview_sha256) = 64 AND lower(preview_sha256) = preview_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(preview_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_admission_candidates_work_id_hex CHECK (work_id IS NULL OR length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_policy ON stove0_admission_candidates (policy_id, admission_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_state ON stove0_admission_candidates (state, next_attempt_at, admission_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_work ON stove0_admission_candidates (work_id, admission_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_created ON stove0_admission_candidates (created_at, admission_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_updated ON stove0_admission_candidates (updated_at, admission_id)
    """.strip(),
)

_ADMISSION_SQLITE_INDEX_DDL: tuple[str, ...] = (
    """
CREATE INDEX ix_stove0_admission_candidates_id_trgm ON stove0_admission_candidates (admission_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_policy_trgm ON stove0_admission_candidates (policy_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_work_trgm ON stove0_admission_candidates (work_id)
    """.strip(),
)

_ADMISSION_POSTGRESQL_INDEX_DDL: tuple[str, ...] = (
    """
CREATE INDEX ix_stove0_admission_candidates_id_trgm ON stove0_admission_candidates USING gin (admission_id gin_trgm_ops)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_policy_trgm ON stove0_admission_candidates USING gin (policy_id gin_trgm_ops)
    """.strip(),
    """
CREATE INDEX ix_stove0_admission_candidates_work_trgm ON stove0_admission_candidates USING gin (work_id gin_trgm_ops)
    """.strip(),
)


SQLITE_DDL: tuple[str, ...] = (
    *_ADMISSION_DDL,
    *_ADMISSION_SQLITE_INDEX_DDL,
    """
CREATE TABLE stove0_artifact_selections (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_count INTEGER NOT NULL,
	total_bytes BIGINT NOT NULL,
	PRIMARY KEY (selection_sha256),
	CONSTRAINT ck_stove0_selections_id CHECK (length(selection_sha256) = 64),
	CONSTRAINT ck_stove0_selections_count CHECK (artifact_count >= 0),
	CONSTRAINT ck_stove0_selections_bytes CHECK (total_bytes >= 0),
	CONSTRAINT ck_stove0_artifact_selections_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_id_trgm ON stove0_evaluation_records (evaluation_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_phase_id ON stove0_evaluation_records (phase, evaluation_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_updated_id ON stove0_evaluation_records (updated_at, evaluation_id)
    """.strip(),
    """
CREATE TABLE stove0_event_cursors (
	stream VARCHAR(160) NOT NULL,
	cursor VARCHAR(500) NOT NULL,
	revision INTEGER NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (stream),
	CONSTRAINT ck_stove0_event_cursors_revision CHECK (revision >= 1)
)
    """.strip(),
    """
CREATE TABLE stove0_lifecycle_events (
	sequence INTEGER NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	event_bytes BIGINT NOT NULL,
	event_json TEXT NOT NULL,
	PRIMARY KEY (sequence),
	CONSTRAINT ck_stove0_lifecycle_events_event_bytes CHECK (event_bytes >= 0)
)
    """.strip(),
    """
CREATE INDEX ix_stove0_lifecycle_events_created_at ON stove0_lifecycle_events (created_at)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_id_trgm ON stove0_work_records (work_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_phase_work_id ON stove0_work_records (phase, work_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_updated_work_id ON stove0_work_records (updated_at, work_id)
    """.strip(),
    """
CREATE TABLE stove0_artifact_selection_members (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_id VARCHAR(160) NOT NULL,
	artifact_order INTEGER NOT NULL,
	continuation_sha256 VARCHAR(64) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (selection_sha256, artifact_id),
	CONSTRAINT ck_stove0_selection_members_artifact_id CHECK (length(artifact_id) >= 1),
	CONSTRAINT ck_stove0_selection_members_order CHECK (artifact_order >= 0),
	CONSTRAINT ck_stove0_selection_members_continuation CHECK (length(continuation_sha256) = 64),
	CONSTRAINT ck_stove0_selection_members_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(selection_sha256) REFERENCES stove0_artifact_selections (selection_sha256) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_artifact_selection_members_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_artifact_selection_members_continuation_sha256_hex CHECK (length(continuation_sha256) = 64 AND lower(continuation_sha256) = continuation_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(continuation_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE UNIQUE INDEX ix_stove0_selection_members_continuation ON stove0_artifact_selection_members (selection_sha256, continuation_sha256)
    """.strip(),
    """
CREATE UNIQUE INDEX ix_stove0_selection_members_order ON stove0_artifact_selection_members (selection_sha256, artifact_order)
    """.strip(),
    """
CREATE TABLE stove0_evaluation_children (
	evaluation_id VARCHAR(64) NOT NULL,
	work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (evaluation_id, work_id),
	FOREIGN KEY(evaluation_id) REFERENCES stove0_evaluation_records (evaluation_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_evaluation_children_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_evaluation_children_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_children_work ON stove0_evaluation_children (work_id, evaluation_id)
    """.strip(),
    """
CREATE TABLE stove0_target_input_dispositions (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	status VARCHAR(32) NOT NULL,
	PRIMARY KEY (work_id, job_id, input_id),
	CONSTRAINT ck_stove0_target_dispositions_id CHECK (length(input_id) >= 1),
	CONSTRAINT ck_stove0_target_dispositions_status CHECK (status IN ('omitted','preserved','rejected','transformed')),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_input_dispositions_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_input_dispositions_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE TABLE stove0_target_outputs (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	output_path VARCHAR(4096) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id, output_id),
	CONSTRAINT ck_stove0_target_outputs_id CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_outputs_path CHECK (length(output_path) >= 1),
	CONSTRAINT ck_stove0_target_outputs_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_outputs_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_outputs_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE UNIQUE INDEX uq_stove0_target_outputs_path ON stove0_target_outputs (work_id, job_id, output_path)
    """.strip(),
    """
CREATE TABLE stove0_target_source_edges (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	PRIMARY KEY (work_id, job_id, output_id, input_id),
	CONSTRAINT ck_stove0_target_source_edges_output CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_source_edges_input CHECK (length(input_id) >= 1),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_source_edges_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_source_edges_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_source_edges_input ON stove0_target_source_edges (work_id, job_id, input_id, output_id)
    """.strip(),
    """
CREATE TABLE stove0_target_production_seals (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id),
	CONSTRAINT ck_stove0_target_production_seals_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_target_production_seals_state CHECK (state IN ('receiving','sealing','sealed','failed')),
	CONSTRAINT ck_stove0_target_production_seals_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_production_seals_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_production_seals_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_production_seals_state_updated ON stove0_target_production_seals (state, updated_at, work_id, job_id)
    """.strip(),
    """
CREATE TABLE stove0_target_settlement_seals (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id),
	CONSTRAINT ck_stove0_target_settlement_seals_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_target_settlement_seals_state CHECK (state IN ('binding','sealed','failed')),
	CONSTRAINT ck_stove0_target_settlement_seals_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_settlement_seals_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_settlement_seals_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_settlement_seals_state_updated ON stove0_target_settlement_seals (state, updated_at, work_id, job_id)
    """.strip(),
    """
CREATE TABLE stove0_work_evaluations (
	work_id VARCHAR(64) NOT NULL,
	evaluation_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, evaluation_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_evaluations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_evaluations_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_evaluations_evaluation ON stove0_work_evaluations (evaluation_id, work_id)
    """.strip(),
    """
CREATE TABLE stove0_work_relations (
	work_id VARCHAR(64) NOT NULL,
	related_work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, related_work_id),
	CONSTRAINT ck_stove0_work_relations_distinct CHECK (work_id <> related_work_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_relations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_relations_related_work_id_hex CHECK (length(related_work_id) = 64 AND lower(related_work_id) = related_work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(related_work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_relations_related ON stove0_work_relations (related_work_id, work_id)
    """.strip(),
    """
CREATE TABLE stove0_work_selection_references (
	work_id VARCHAR(64) NOT NULL,
	selection_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, selection_sha256),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_selection_references_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_selection_references_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_selection_references_selection ON stove0_work_selection_references (selection_sha256, work_id)
    """.strip(),
)

POSTGRESQL_DDL: tuple[str, ...] = (
    *_ADMISSION_DDL,
    *_ADMISSION_POSTGRESQL_INDEX_DDL,
    """
CREATE TABLE stove0_artifact_selections (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_count INTEGER NOT NULL,
	total_bytes BIGINT NOT NULL,
	PRIMARY KEY (selection_sha256),
	CONSTRAINT ck_stove0_selections_id CHECK (length(selection_sha256) = 64),
	CONSTRAINT ck_stove0_selections_count CHECK (artifact_count >= 0),
	CONSTRAINT ck_stove0_selections_bytes CHECK (total_bytes >= 0),
	CONSTRAINT ck_stove0_artifact_selections_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_id_trgm ON stove0_evaluation_records USING gin (evaluation_id gin_trgm_ops)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_phase_id ON stove0_evaluation_records (phase, evaluation_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_records_updated_id ON stove0_evaluation_records (updated_at, evaluation_id)
    """.strip(),
    """
CREATE TABLE stove0_event_cursors (
	stream VARCHAR(160) NOT NULL,
	cursor VARCHAR(500) NOT NULL,
	revision INTEGER NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (stream),
	CONSTRAINT ck_stove0_event_cursors_revision CHECK (revision >= 1)
)
    """.strip(),
    """
CREATE TABLE stove0_lifecycle_events (
	sequence SERIAL NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	event_bytes BIGINT NOT NULL,
	event_json TEXT NOT NULL,
	PRIMARY KEY (sequence),
	CONSTRAINT ck_stove0_lifecycle_events_event_bytes CHECK (event_bytes >= 0)
)
    """.strip(),
    """
CREATE INDEX ix_stove0_lifecycle_events_created_at ON stove0_lifecycle_events (created_at)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_id_trgm ON stove0_work_records USING gin (work_id gin_trgm_ops)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_phase_work_id ON stove0_work_records (phase, work_id)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_records_updated_work_id ON stove0_work_records (updated_at, work_id)
    """.strip(),
    """
CREATE TABLE stove0_artifact_selection_members (
	selection_sha256 VARCHAR(64) NOT NULL,
	artifact_id VARCHAR(160) NOT NULL,
	artifact_order INTEGER NOT NULL,
	continuation_sha256 VARCHAR(64) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (selection_sha256, artifact_id),
	CONSTRAINT ck_stove0_selection_members_artifact_id CHECK (length(artifact_id) >= 1),
	CONSTRAINT ck_stove0_selection_members_order CHECK (artifact_order >= 0),
	CONSTRAINT ck_stove0_selection_members_continuation CHECK (length(continuation_sha256) = 64),
	CONSTRAINT ck_stove0_selection_members_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(selection_sha256) REFERENCES stove0_artifact_selections (selection_sha256) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_artifact_selection_members_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_artifact_selection_members_continuation_sha256_hex CHECK (length(continuation_sha256) = 64 AND lower(continuation_sha256) = continuation_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(continuation_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE UNIQUE INDEX ix_stove0_selection_members_continuation ON stove0_artifact_selection_members (selection_sha256, continuation_sha256)
    """.strip(),
    """
CREATE UNIQUE INDEX ix_stove0_selection_members_order ON stove0_artifact_selection_members (selection_sha256, artifact_order)
    """.strip(),
    """
CREATE TABLE stove0_evaluation_children (
	evaluation_id VARCHAR(64) NOT NULL,
	work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (evaluation_id, work_id),
	FOREIGN KEY(evaluation_id) REFERENCES stove0_evaluation_records (evaluation_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_evaluation_children_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_evaluation_children_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_evaluation_children_work ON stove0_evaluation_children (work_id, evaluation_id)
    """.strip(),
    """
CREATE TABLE stove0_target_input_dispositions (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	status VARCHAR(32) NOT NULL,
	PRIMARY KEY (work_id, job_id, input_id),
	CONSTRAINT ck_stove0_target_dispositions_id CHECK (length(input_id) >= 1),
	CONSTRAINT ck_stove0_target_dispositions_status CHECK (status IN ('omitted','preserved','rejected','transformed')),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_input_dispositions_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_input_dispositions_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE TABLE stove0_target_outputs (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	output_path VARCHAR(4096) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id, output_id),
	CONSTRAINT ck_stove0_target_outputs_id CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_outputs_path CHECK (length(output_path) >= 1),
	CONSTRAINT ck_stove0_target_outputs_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_outputs_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_outputs_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE UNIQUE INDEX uq_stove0_target_outputs_path ON stove0_target_outputs (work_id, job_id, output_path)
    """.strip(),
    """
CREATE TABLE stove0_target_source_edges (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	output_id VARCHAR(160) NOT NULL,
	input_id VARCHAR(160) NOT NULL,
	PRIMARY KEY (work_id, job_id, output_id, input_id),
	CONSTRAINT ck_stove0_target_source_edges_output CHECK (length(output_id) >= 1),
	CONSTRAINT ck_stove0_target_source_edges_input CHECK (length(input_id) >= 1),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_source_edges_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_source_edges_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_source_edges_input ON stove0_target_source_edges (work_id, job_id, input_id, output_id)
    """.strip(),
    """
CREATE TABLE stove0_target_production_seals (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id),
	CONSTRAINT ck_stove0_target_production_seals_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_target_production_seals_state CHECK (state IN ('receiving','sealing','sealed','failed')),
	CONSTRAINT ck_stove0_target_production_seals_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_production_seals_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_production_seals_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_production_seals_state_updated ON stove0_target_production_seals (state, updated_at, work_id, job_id)
    """.strip(),
    """
CREATE TABLE stove0_target_settlement_seals (
	work_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	document_bytes BIGINT NOT NULL,
	document_json TEXT NOT NULL,
	PRIMARY KEY (work_id, job_id),
	CONSTRAINT ck_stove0_target_settlement_seals_revision CHECK (revision >= 1),
	CONSTRAINT ck_stove0_target_settlement_seals_state CHECK (state IN ('binding','sealed','failed')),
	CONSTRAINT ck_stove0_target_settlement_seals_document_bytes CHECK (document_bytes >= 0),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_target_settlement_seals_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_target_settlement_seals_job_id_hex CHECK (length(job_id) = 64 AND lower(job_id) = job_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(job_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_target_settlement_seals_state_updated ON stove0_target_settlement_seals (state, updated_at, work_id, job_id)
    """.strip(),
    """
CREATE TABLE stove0_work_evaluations (
	work_id VARCHAR(64) NOT NULL,
	evaluation_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, evaluation_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_evaluations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_evaluations_evaluation_id_hex CHECK (length(evaluation_id) = 64 AND lower(evaluation_id) = evaluation_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evaluation_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_evaluations_evaluation ON stove0_work_evaluations (evaluation_id, work_id)
    """.strip(),
    """
CREATE TABLE stove0_work_relations (
	work_id VARCHAR(64) NOT NULL,
	related_work_id VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, related_work_id),
	CONSTRAINT ck_stove0_work_relations_distinct CHECK (work_id <> related_work_id),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_relations_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_relations_related_work_id_hex CHECK (length(related_work_id) = 64 AND lower(related_work_id) = related_work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(related_work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_relations_related ON stove0_work_relations (related_work_id, work_id)
    """.strip(),
    """
CREATE TABLE stove0_work_selection_references (
	work_id VARCHAR(64) NOT NULL,
	selection_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (work_id, selection_sha256),
	FOREIGN KEY(work_id) REFERENCES stove0_work_records (work_id) ON DELETE CASCADE,
	CONSTRAINT ck_stove0_work_selection_references_work_id_hex CHECK (length(work_id) = 64 AND lower(work_id) = work_id AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(work_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''),
	CONSTRAINT ck_stove0_work_selection_references_selection_sha256_hex CHECK (length(selection_sha256) = 64 AND lower(selection_sha256) = selection_sha256 AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(selection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')
)
    """.strip(),
    """
CREATE INDEX ix_stove0_work_selection_references_selection ON stove0_work_selection_references (selection_sha256, work_id)
    """.strip(),
)
