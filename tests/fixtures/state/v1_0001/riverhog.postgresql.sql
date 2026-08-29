-- Exact current Riverhog PostgreSQL v1 baseline conformance fixture.

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE app_key_access_grants (
    key_id character varying NOT NULL,
    permission character varying NOT NULL,
    resource character varying NOT NULL,
    created_at character varying NOT NULL,
    search_text character varying GENERATED ALWAYS AS (lower((((permission)::text || ' '::text) || (resource)::text))) STORED NOT NULL
);

CREATE TABLE app_keys (
    id character varying NOT NULL,
    app character varying NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    monthly_download_quota_bytes bigint,
    created_at character varying NOT NULL,
    expires_at character varying,
    revoked_at character varying,
    last_used_at character varying,
    search_text character varying GENERATED ALWAYS AS (lower((((app)::text || ' '::text) || (id)::text))) STORED NOT NULL,
    CONSTRAINT ck_app_keys_download_quota CHECK (((monthly_download_quota_bytes IS NULL) OR (monthly_download_quota_bytes >= 0))),
    CONSTRAINT ck_app_keys_token_sha256 CHECK ((length((token_sha256)::text) = 64))
);

CREATE TABLE archive_copy_jobs (
    collection_id bigint NOT NULL,
    destination_store character varying NOT NULL,
    destination_storage_prefix character varying NOT NULL,
    source_store character varying NOT NULL,
    initiated_by_app character varying NOT NULL,
    initiated_by_key_id character varying,
    event_context_json text,
    state character varying NOT NULL,
    requested_at character varying NOT NULL,
    read_requested_at character varying,
    ready_at character varying,
    expires_at character varying,
    next_attempt_at character varying,
    completed_at character varying,
    failure character varying,
    search_text character varying GENERATED ALWAYS AS (lower((((((((collection_id)::text || ' '::text) || (source_store)::text) || ' '::text) || (destination_store)::text) || ' '::text) || (state)::text))) STORED NOT NULL,
    CONSTRAINT ck_archive_copy_jobs_state CHECK (((state)::text = ANY ((ARRAY['requested'::character varying, 'waiting'::character varying, 'checking'::character varying, 'copying'::character varying, 'canceling'::character varying, 'completed'::character varying, 'failed'::character varying, 'canceled'::character varying])::text[])))
);

CREATE TABLE archive_copy_object_uploads (
    collection_id bigint NOT NULL,
    destination_store character varying NOT NULL,
    object_id character varying NOT NULL,
    kind character varying NOT NULL,
    object_path character varying NOT NULL,
    plaintext_bytes bigint NOT NULL,
    sha256 character varying(64),
    write_token character varying,
    expected_stored_bytes bigint,
    write_segments_json character varying,
    uploaded_bytes bigint NOT NULL,
    uploaded_segments integer NOT NULL,
    total_segments integer NOT NULL,
    CONSTRAINT ck_archive_copy_uploads_plaintext CHECK ((plaintext_bytes >= 0)),
    CONSTRAINT ck_archive_copy_uploads_segment_progress CHECK ((uploaded_segments <= total_segments)),
    CONSTRAINT ck_archive_copy_uploads_total_segments CHECK ((total_segments >= 0)),
    CONSTRAINT ck_archive_copy_uploads_uploaded_bytes CHECK ((uploaded_bytes >= 0)),
    CONSTRAINT ck_archive_copy_uploads_uploaded_segments CHECK ((uploaded_segments >= 0))
);

CREATE TABLE archive_copy_retirements (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    challenge character varying NOT NULL,
    plan_json text NOT NULL,
    started_at character varying NOT NULL
);

CREATE TABLE archive_download_reservations (
    id character varying NOT NULL,
    store character varying NOT NULL,
    month_started_at character varying NOT NULL,
    reserved_bytes bigint NOT NULL,
    created_at character varying NOT NULL,
    expires_at character varying NOT NULL,
    CONSTRAINT ck_archive_download_reservations_bytes CHECK ((reserved_bytes >= 0))
);

CREATE TABLE archive_download_usage (
    store character varying NOT NULL,
    month_started_at character varying NOT NULL,
    accounted_bytes bigint NOT NULL,
    updated_at character varying NOT NULL,
    CONSTRAINT ck_archive_download_usage_bytes CHECK ((accounted_bytes >= 0))
);

CREATE TABLE catalog_event_tags (
    sequence integer NOT NULL,
    phase character varying NOT NULL,
    tag_id character varying NOT NULL,
    CONSTRAINT ck_catalog_event_tags_phase CHECK (((phase)::text = ANY ((ARRAY['before'::character varying, 'after'::character varying])::text[])))
);

CREATE TABLE catalog_events (
    sequence integer NOT NULL,
    change character varying NOT NULL,
    collection_id bigint NOT NULL,
    occurred_at character varying NOT NULL,
    record_etag character varying(64) NOT NULL
);

CREATE SEQUENCE catalog_events_sequence_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE catalog_events_sequence_seq OWNED BY catalog_events.sequence;

CREATE TABLE collection_archive_attestations (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    state character varying NOT NULL,
    attempt_count integer NOT NULL,
    next_attempt_at character varying NOT NULL,
    last_attempt_at character varying,
    published_at character varying,
    matured_at character varying,
    failure text,
    CONSTRAINT ck_archive_attestations_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_archive_attestations_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'publishing'::character varying, 'publish_retry'::character varying, 'upgrading'::character varying, 'upgrade_retry'::character varying, 'waiting'::character varying, 'matured'::character varying])::text[])))
);

CREATE TABLE collection_archive_copies (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    state character varying NOT NULL,
    archive_storage_prefix character varying,
    last_uploaded_at character varying,
    last_verified_at character varying,
    failure character varying,
    CONSTRAINT ck_collection_archive_copies_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'uploading'::character varying, 'uploaded'::character varying, 'retrying'::character varying, 'failed'::character varying])::text[])))
);

CREATE TABLE collection_archive_file_objects (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    path character varying NOT NULL,
    sequence integer NOT NULL,
    object_id character varying NOT NULL,
    file_offset bigint NOT NULL,
    object_offset bigint NOT NULL,
    bytes bigint NOT NULL,
    member character varying,
    CONSTRAINT ck_archive_file_objects_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_archive_file_objects_file_offset CHECK ((file_offset >= 0)),
    CONSTRAINT ck_archive_file_objects_object_offset CHECK ((object_offset >= 0)),
    CONSTRAINT ck_archive_file_objects_sequence CHECK ((sequence >= 0))
);

CREATE TABLE collection_archive_object_uploads (
    collection_id bigint NOT NULL,
    object_id character varying NOT NULL,
    sequence integer NOT NULL,
    kind character varying NOT NULL,
    relative_path character varying NOT NULL,
    object_path character varying NOT NULL,
    plaintext_bytes bigint NOT NULL,
    source_bytes bigint NOT NULL,
    unit_plaintext_bytes bigint NOT NULL,
    plan_json text NOT NULL,
    plan_sha256 character varying(64) NOT NULL,
    state character varying NOT NULL,
    checkpoint_json text,
    sealed_receipt_json text,
    failure text,
    uploaded_bytes bigint NOT NULL,
    uploaded_units integer NOT NULL,
    total_units integer NOT NULL,
    updated_at character varying NOT NULL,
    sealed_at character varying,
    CONSTRAINT ck_archive_object_uploads_plaintext CHECK ((plaintext_bytes >= 0)),
    CONSTRAINT ck_archive_object_uploads_sequence CHECK ((sequence >= 0)),
    CONSTRAINT ck_archive_object_uploads_source CHECK ((source_bytes >= 0)),
    CONSTRAINT ck_archive_object_uploads_state CHECK (((state)::text = ANY ((ARRAY['planned'::character varying, 'uploading'::character varying, 'sealed'::character varying])::text[]))),
    CONSTRAINT ck_archive_object_uploads_total_units CHECK ((total_units >= 0)),
    CONSTRAINT ck_archive_object_uploads_unit CHECK ((unit_plaintext_bytes > 0)),
    CONSTRAINT ck_archive_object_uploads_unit_progress CHECK ((uploaded_units <= total_units)),
    CONSTRAINT ck_archive_object_uploads_uploaded_bytes CHECK ((uploaded_bytes >= 0)),
    CONSTRAINT ck_archive_object_uploads_uploaded_units CHECK ((uploaded_units >= 0))
);

CREATE TABLE collection_archive_objects (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    object_id character varying NOT NULL,
    object_order integer NOT NULL,
    kind character varying NOT NULL,
    object_path character varying NOT NULL,
    plaintext_bytes bigint NOT NULL,
    stored_bytes bigint NOT NULL,
    sha256 character varying(64),
    stored_sha256 character varying(64),
    revision character varying,
    age_state_json text,
    archive_parts_json text,
    plan_sha256 character varying(64),
    index_sha256 character varying(64),
    uploaded_at character varying NOT NULL,
    verified_at character varying,
    CONSTRAINT ck_collection_archive_objects_order CHECK ((object_order >= 0)),
    CONSTRAINT ck_collection_archive_objects_plaintext CHECK ((plaintext_bytes >= 0)),
    CONSTRAINT ck_collection_archive_objects_stored CHECK ((stored_bytes >= 0))
);

CREATE TABLE collection_deletions (
    collection_id bigint NOT NULL,
    challenge character varying NOT NULL,
    plan_json text NOT NULL,
    started_at character varying NOT NULL
);

CREATE SEQUENCE collection_deletions_collection_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE collection_deletions_collection_id_seq OWNED BY collection_deletions.collection_id;

CREATE TABLE collection_derivations (
    collection_id bigint NOT NULL,
    execution_id character varying(64) NOT NULL,
    claim_id character varying(64) NOT NULL,
    fence bigint NOT NULL,
    document_json text NOT NULL,
    document_sha256 character varying(64) NOT NULL,
    created_at character varying NOT NULL,
    CONSTRAINT ck_collection_derivations_fence CHECK ((fence >= 1))
);

CREATE TABLE collection_file_provenance (
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    status character varying NOT NULL,
    journal_id character varying,
    current_state_id character varying,
    omission_reason text,
    CONSTRAINT ck_collection_file_provenance_binding CHECK (((((status)::text = 'captured'::text) AND (journal_id IS NOT NULL) AND (current_state_id IS NOT NULL) AND (omission_reason IS NULL)) OR (((status)::text = 'omitted'::text) AND (journal_id IS NULL) AND (current_state_id IS NULL) AND (omission_reason IS NOT NULL)))),
    CONSTRAINT ck_collection_file_provenance_status CHECK (((status)::text = ANY ((ARRAY['captured'::character varying, 'omitted'::character varying])::text[])))
);

CREATE TABLE collection_files (
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    provenance_status character varying DEFAULT 'missing'::character varying NOT NULL,
    search_text character varying GENERATED ALWAYS AS ((((collection_id)::text || '/'::text) || lower((path)::text))) STORED NOT NULL,
    path_search_text character varying GENERATED ALWAYS AS (lower((path)::text)) STORED NOT NULL,
    CONSTRAINT ck_collection_files_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_collection_files_provenance_status CHECK (((provenance_status)::text = ANY ((ARRAY['captured'::character varying, 'omitted'::character varying, 'missing'::character varying])::text[]))),
    CONSTRAINT ck_collection_files_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_metadata_publications (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    desired_revision bigint NOT NULL,
    published_revision bigint,
    state character varying NOT NULL,
    attempt_count integer NOT NULL,
    next_attempt_at character varying NOT NULL,
    last_attempt_at character varying,
    failure text,
    object_path character varying,
    revision character varying,
    stored_bytes bigint,
    stored_sha256 character varying(64),
    published_at character varying,
    CONSTRAINT ck_metadata_publications_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_metadata_publications_desired_revision CHECK ((desired_revision >= 1)),
    CONSTRAINT ck_metadata_publications_published_revision CHECK (((published_revision IS NULL) OR (published_revision >= 1))),
    CONSTRAINT ck_metadata_publications_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'publishing'::character varying, 'published'::character varying, 'retry_wait'::character varying])::text[])))
);

CREATE TABLE collection_processing_claim_artifacts (
    claim_id character varying(64) NOT NULL,
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_processing_claim_artifacts_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_processing_claim_artifacts_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_processing_claim_inputs (
    claim_id character varying(64) NOT NULL,
    collection_id bigint NOT NULL,
    collection_order integer NOT NULL,
    archive_root_sha256 character varying(64) NOT NULL,
    content_identity character varying(64) NOT NULL,
    CONSTRAINT ck_claim_inputs_archive_root CHECK ((length((archive_root_sha256)::text) = 64)),
    CONSTRAINT ck_claim_inputs_content_identity CHECK ((length((content_identity)::text) = 64)),
    CONSTRAINT ck_processing_claim_inputs_order CHECK ((collection_order >= 0))
);

CREATE TABLE collection_processing_claims (
    id character varying(64) NOT NULL,
    work_id character varying(64) NOT NULL,
    consumer_app character varying NOT NULL,
    consumer_key_id character varying,
    purpose character varying NOT NULL,
    work_document_json text NOT NULL,
    work_document_sha256 character varying(64) NOT NULL,
    execution_id character varying(64),
    controller_evidence_json text,
    controller_evidence_sha256 character varying(64),
    operation_id character varying,
    operation_sha256 character varying(64),
    output_tags_json text,
    retirement_policy character varying,
    retirement_grace_seconds bigint NOT NULL,
    plan_sealed_at character varying,
    state character varying NOT NULL,
    fence bigint NOT NULL,
    expires_at character varying NOT NULL,
    output_collection_id bigint,
    created_at character varying NOT NULL,
    updated_at character varying NOT NULL,
    settled_at character varying,
    abandoned_at character varying,
    abandonment_reason text,
    released_at character varying,
    CONSTRAINT ck_collection_processing_claims_document_sha256 CHECK ((length((work_document_sha256)::text) = 64)),
    CONSTRAINT ck_collection_processing_claims_fence CHECK ((fence >= 1)),
    CONSTRAINT ck_collection_processing_claims_grace CHECK ((retirement_grace_seconds >= 0)),
    CONSTRAINT ck_collection_processing_claims_id CHECK ((length((id)::text) = 64)),
    CONSTRAINT ck_collection_processing_claims_state CHECK (((state)::text = ANY ((ARRAY['active'::character varying, 'settled'::character varying, 'retiring'::character varying, 'abandoned'::character varying, 'released'::character varying])::text[]))),
    CONSTRAINT ck_collection_processing_claims_work_id CHECK ((length((work_id)::text) = 64))
);

CREATE TABLE collection_processing_outcomes (
    claim_id character varying(64) NOT NULL,
    outcome_id character varying(160) NOT NULL,
    source_claim_id character varying(64) NOT NULL,
    collection_id bigint NOT NULL,
    archive_root_sha256 character varying(64) NOT NULL,
    content_identity character varying(64) NOT NULL,
    derivation_sha256 character varying(64) NOT NULL,
    created_at character varying NOT NULL
);

CREATE TABLE collection_proof_maturations (
    collection_id bigint NOT NULL,
    store character varying NOT NULL,
    state character varying NOT NULL,
    attempt_count integer NOT NULL,
    next_attempt_at character varying NOT NULL,
    last_attempt_at character varying,
    matured_at character varying,
    failure text,
    CONSTRAINT ck_proof_maturations_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_proof_maturations_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'upgrading'::character varying, 'waiting'::character varying, 'retry_wait'::character varying, 'matured'::character varying])::text[])))
);

CREATE TABLE collection_provenance_entities (
    collection_id bigint NOT NULL,
    journal_id character varying NOT NULL,
    entity_type character varying NOT NULL,
    entity_id character varying NOT NULL,
    entry_id character varying NOT NULL,
    document_json text NOT NULL
);

CREATE TABLE collection_provenance_external_state_references (
    collection_id bigint NOT NULL,
    from_journal_id character varying NOT NULL,
    to_journal_id character varying NOT NULL,
    entry_id character varying NOT NULL,
    state_id character varying NOT NULL,
    entry_json_sha256 character varying(64) NOT NULL
);

CREATE TABLE collection_provenance_journals (
    collection_id bigint NOT NULL,
    journal_id character varying NOT NULL,
    journal_bytes bytea NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    entries bigint NOT NULL,
    agent_ids_json text NOT NULL,
    entity_counts_json text NOT NULL,
    current_state_id character varying NOT NULL,
    current_path character varying NOT NULL,
    current_bytes bigint NOT NULL,
    current_sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_provenance_journals_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_provenance_journals_current_bytes CHECK ((current_bytes >= 0)),
    CONSTRAINT ck_provenance_journals_entries CHECK ((entries >= 0)),
    CONSTRAINT ck_provenance_journals_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_tags (
    collection_id bigint NOT NULL,
    tag_id character varying NOT NULL,
    assigned_by_app character varying NOT NULL,
    assigned_by_key_id character varying,
    assigned_at character varying NOT NULL
);

CREATE TABLE collection_transform_capabilities (
    id character varying(32) NOT NULL,
    claim_id character varying(64) NOT NULL,
    fence bigint NOT NULL,
    audience character varying(300) NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    actions_json text NOT NULL,
    state character varying NOT NULL,
    expires_at character varying NOT NULL,
    created_at character varying NOT NULL,
    revoked_at character varying,
    CONSTRAINT ck_collection_transform_capabilities_fence CHECK ((fence >= 1)),
    CONSTRAINT ck_collection_transform_capabilities_state CHECK (((state)::text = ANY ((ARRAY['active'::character varying, 'revoked'::character varying])::text[])))
);

CREATE TABLE collection_transform_capability_artifacts (
    capability_id character varying(32) NOT NULL,
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_capability_artifacts_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_capability_artifacts_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_upload_files (
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    file_order integer NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    raw_part_plaintext_bytes bigint,
    raw_digest_manifest_json text,
    provenance_status character varying NOT NULL,
    provenance_journal_id character varying,
    provenance_current_state_id character varying,
    provenance_omission_reason text,
    custodied_at character varying,
    custody_receipt_json text,
    CONSTRAINT ck_collection_upload_files_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_collection_upload_files_order CHECK ((file_order >= 0)),
    CONSTRAINT ck_collection_upload_files_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_upload_provenance_journals (
    collection_id bigint NOT NULL,
    journal_id character varying NOT NULL,
    journal_bytes bytea NOT NULL,
    bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    current_state_id character varying NOT NULL,
    current_path character varying NOT NULL,
    current_bytes bigint NOT NULL,
    current_sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_upload_provenance_journals_bytes CHECK ((bytes >= 0)),
    CONSTRAINT ck_upload_provenance_journals_current_bytes CHECK ((current_bytes >= 0)),
    CONSTRAINT ck_upload_provenance_journals_sha256 CHECK ((length((sha256)::text) = 64))
);

CREATE TABLE collection_upload_tags (
    collection_id bigint NOT NULL,
    tag_id character varying NOT NULL
);

CREATE TABLE collection_uploads (
    collection_id bigint NOT NULL,
    idempotency_key character varying NOT NULL,
    creation_identity_sha256 character varying(64) NOT NULL,
    ingest_source character varying,
    provenance_mode character varying NOT NULL,
    provenance_omission_reason text,
    provenance_identity character varying(64),
    encryption_format character varying NOT NULL,
    passphrase_id character varying NOT NULL,
    initiated_by_app character varying NOT NULL,
    initiated_by_key_id character varying,
    event_context_json text,
    state character varying NOT NULL,
    custody_mode character varying NOT NULL,
    lease_expires_at character varying,
    orphaned_at character varying,
    archive_store character varying NOT NULL,
    opened_at character varying NOT NULL,
    last_activity_at character varying NOT NULL,
    closed_at character varying,
    archive_phase character varying NOT NULL,
    archive_phase_updated_at character varying NOT NULL,
    archive_attempt_count integer NOT NULL,
    archive_next_attempt_at character varying,
    archive_last_attempt_at character varying,
    archive_failure character varying,
    archive_storage_prefix character varying NOT NULL,
    collection_manifest_bytes_b64 character varying,
    collection_manifest_proof_bytes_b64 character varying,
    planner_checkpoint_json text NOT NULL,
    file_count bigint DEFAULT 0 NOT NULL,
    file_bytes bigint DEFAULT 0 NOT NULL,
    custodied_file_count bigint DEFAULT 0 NOT NULL,
    custodied_file_bytes bigint DEFAULT 0 NOT NULL,
    search_text character varying GENERATED ALWAYS AS (lower((COALESCE(ingest_source, ''::character varying))::text)) STORED NOT NULL,
    CONSTRAINT ck_collection_uploads_archive_phase CHECK (((archive_phase)::text = ANY ((ARRAY['planning'::character varying, 'uploading'::character varying, 'finalization_queued'::character varying, 'finalizing'::character varying, 'retry_wait'::character varying, 'orphaned'::character varying, 'discarding'::character varying])::text[]))),
    CONSTRAINT ck_collection_uploads_attempt_count CHECK ((archive_attempt_count >= 0)),
    CONSTRAINT ck_collection_uploads_custodied_file_bytes CHECK (((custodied_file_bytes >= 0) AND (custodied_file_bytes <= file_bytes))),
    CONSTRAINT ck_collection_uploads_custodied_file_count CHECK (((custodied_file_count >= 0) AND (custodied_file_count <= file_count))),
    CONSTRAINT ck_collection_uploads_custody_mode CHECK (((custody_mode)::text = ANY ((ARRAY['producer-retained'::character varying, 'custody-transfer'::character varying])::text[]))),
    CONSTRAINT ck_collection_uploads_empty_custody CHECK (((custodied_file_count > 0) OR (custodied_file_bytes = 0))),
    CONSTRAINT ck_collection_uploads_file_bytes CHECK ((file_bytes >= 0)),
    CONSTRAINT ck_collection_uploads_file_count CHECK ((file_count >= 0)),
    CONSTRAINT ck_collection_uploads_provenance_mode CHECK (((provenance_mode)::text = ANY ((ARRAY['captured'::character varying, 'omitted'::character varying])::text[]))),
    CONSTRAINT ck_collection_uploads_state CHECK (((state)::text = ANY ((ARRAY['open'::character varying, 'closing'::character varying, 'uploading'::character varying, 'finalizing'::character varying, 'orphaned'::character varying, 'discarding'::character varying])::text[])))
);

ALTER TABLE collection_uploads ALTER COLUMN collection_id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME collection_uploads_collection_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE collections (
    id bigint NOT NULL,
    search_text character varying GENERATED ALWAYS AS ((id)::text) STORED NOT NULL,
    creation_idempotency_key character varying NOT NULL,
    creation_identity_sha256 character varying(64) NOT NULL,
    creation_custody_mode character varying NOT NULL,
    content_identity character varying(64) NOT NULL,
    encryption_format character varying NOT NULL,
    passphrase_id character varying NOT NULL,
    provenance_mode character varying NOT NULL,
    provenance_identity character varying(64),
    record_etag character varying(64) NOT NULL,
    metadata_revision bigint NOT NULL,
    metadata_updated_at character varying NOT NULL,
    ingest_source character varying,
    created_by_app character varying NOT NULL,
    created_by_key_id character varying,
    created_at character varying NOT NULL,
    file_count bigint DEFAULT 0 NOT NULL,
    file_bytes bigint DEFAULT 0 NOT NULL,
    CONSTRAINT ck_collections_content_identity CHECK ((length((content_identity)::text) = 64)),
    CONSTRAINT ck_collections_file_bytes CHECK ((file_bytes >= 0)),
    CONSTRAINT ck_collections_file_count CHECK ((file_count >= 0)),
    CONSTRAINT ck_collections_metadata_revision CHECK ((metadata_revision >= 1)),
    CONSTRAINT ck_collections_provenance_identity CHECK (((((provenance_mode)::text = ANY ((ARRAY['captured'::character varying, 'mixed'::character varying])::text[])) AND (provenance_identity IS NOT NULL)) OR (((provenance_mode)::text = 'omitted'::text) AND (provenance_identity IS NULL)))),
    CONSTRAINT ck_collections_provenance_mode CHECK (((provenance_mode)::text = ANY ((ARRAY['captured'::character varying, 'mixed'::character varying, 'omitted'::character varying])::text[]))),
    CONSTRAINT ck_collections_record_etag CHECK ((length((record_etag)::text) = 64))
);

CREATE SEQUENCE collections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE collections_id_seq OWNED BY collections.id;

CREATE TABLE key_download_reservations (
    id character varying NOT NULL,
    key_id character varying NOT NULL,
    job_id character varying NOT NULL,
    kind character varying NOT NULL,
    month_started_at character varying NOT NULL,
    reserved_bytes bigint NOT NULL,
    created_at character varying NOT NULL,
    expires_at character varying NOT NULL,
    CONSTRAINT ck_key_download_reservations_bytes CHECK ((reserved_bytes >= 0)),
    CONSTRAINT ck_key_download_reservations_kind CHECK (((kind)::text = ANY ((ARRAY['job'::character varying, 'stream'::character varying])::text[])))
);

CREATE TABLE key_download_usage (
    key_id character varying NOT NULL,
    month_started_at character varying NOT NULL,
    accounted_bytes bigint NOT NULL,
    updated_at character varying NOT NULL,
    CONSTRAINT ck_key_download_usage_bytes CHECK ((accounted_bytes >= 0))
);

CREATE TABLE lifecycle_events (
    sequence integer NOT NULL,
    event_id character varying NOT NULL,
    owner_app character varying NOT NULL,
    subject character varying,
    event_json text NOT NULL,
    context_json text,
    context_expires_at character varying
);

CREATE SEQUENCE lifecycle_events_sequence_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE lifecycle_events_sequence_seq OWNED BY lifecycle_events.sequence;

CREATE TABLE retrieval_cache_leases (
    owner character varying NOT NULL,
    source_store character varying NOT NULL,
    collection_id bigint NOT NULL,
    object_id character varying NOT NULL,
    expires_at character varying NOT NULL
);

CREATE TABLE retrieval_cache_objects (
    source_store character varying NOT NULL,
    collection_id bigint NOT NULL,
    object_id character varying NOT NULL,
    object_path character varying NOT NULL,
    revision character varying,
    stored_bytes bigint NOT NULL,
    stored_sha256 character varying(64) NOT NULL,
    cached_at character varying NOT NULL,
    verified_at character varying NOT NULL,
    state character varying NOT NULL,
    search_text character varying GENERATED ALWAYS AS (lower((((source_store)::text || ' '::text) || (object_id)::text))) STORED NOT NULL,
    CONSTRAINT ck_retrieval_cache_objects_bytes CHECK ((stored_bytes >= 0)),
    CONSTRAINT ck_retrieval_cache_objects_sha256 CHECK ((length((stored_sha256)::text) = 64)),
    CONSTRAINT ck_retrieval_cache_objects_state CHECK (((state)::text = ANY ((ARRAY['ready'::character varying, 'delete_pending'::character varying, 'deleting'::character varying])::text[])))
);

CREATE TABLE retrieval_job_files (
    job_id character varying NOT NULL,
    collection_id bigint NOT NULL,
    path character varying NOT NULL,
    file_order integer NOT NULL,
    CONSTRAINT ck_retrieval_job_files_order CHECK ((file_order >= 0))
);

CREATE TABLE retrieval_job_objects (
    job_id character varying NOT NULL,
    collection_id bigint NOT NULL,
    source_store character varying NOT NULL,
    object_id character varying NOT NULL,
    object_order integer NOT NULL,
    read_mode character varying NOT NULL,
    CONSTRAINT ck_retrieval_job_objects_order CHECK ((object_order >= 0)),
    CONSTRAINT ck_retrieval_job_objects_read_mode CHECK (((read_mode)::text = ANY ((ARRAY['immediate'::character varying, 'restore_required'::character varying])::text[])))
);

CREATE TABLE retrieval_jobs (
    id character varying NOT NULL,
    app character varying NOT NULL,
    initiated_by_key_id character varying,
    event_context_json text,
    state character varying NOT NULL,
    plan_etag character varying(64) NOT NULL,
    constraints_json text NOT NULL,
    created_at character varying NOT NULL,
    requested_at character varying,
    restore_requested_at character varying,
    ready_at character varying,
    expires_at character varying,
    next_poll_at character varying,
    completed_at character varying,
    canceled_at character varying,
    failure text,
    CONSTRAINT ck_retrieval_jobs_plan_etag CHECK ((length((plan_etag)::text) = 64)),
    CONSTRAINT ck_retrieval_jobs_state CHECK (((state)::text = ANY ((ARRAY['requested'::character varying, 'ready'::character varying, 'completed'::character varying, 'canceled'::character varying, 'expired'::character varying, 'failed'::character varying])::text[])))
);

CREATE TABLE state_schema_revision (
    version_num character varying(32) NOT NULL
);

CREATE TABLE tags (
    id character varying NOT NULL,
    created_by_app character varying NOT NULL,
    created_by_key_id character varying,
    created_at character varying NOT NULL,
    collection_count bigint DEFAULT 0 NOT NULL,
    CONSTRAINT ck_tags_collection_count CHECK ((collection_count >= 0))
);

ALTER TABLE ONLY catalog_events ALTER COLUMN sequence SET DEFAULT nextval('catalog_events_sequence_seq'::regclass);

ALTER TABLE ONLY collection_deletions ALTER COLUMN collection_id SET DEFAULT nextval('collection_deletions_collection_id_seq'::regclass);

ALTER TABLE ONLY collections ALTER COLUMN id SET DEFAULT nextval('collections_id_seq'::regclass);

ALTER TABLE ONLY lifecycle_events ALTER COLUMN sequence SET DEFAULT nextval('lifecycle_events_sequence_seq'::regclass);

ALTER TABLE ONLY app_key_access_grants
    ADD CONSTRAINT app_key_access_grants_pkey PRIMARY KEY (key_id, permission, resource);

ALTER TABLE ONLY app_keys
    ADD CONSTRAINT app_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY archive_copy_jobs
    ADD CONSTRAINT archive_copy_jobs_pkey PRIMARY KEY (collection_id, destination_store);

ALTER TABLE ONLY archive_copy_object_uploads
    ADD CONSTRAINT archive_copy_object_uploads_pkey PRIMARY KEY (collection_id, destination_store, object_id);

ALTER TABLE ONLY archive_copy_retirements
    ADD CONSTRAINT archive_copy_retirements_pkey PRIMARY KEY (collection_id, store);

ALTER TABLE ONLY archive_download_reservations
    ADD CONSTRAINT archive_download_reservations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY archive_download_usage
    ADD CONSTRAINT archive_download_usage_pkey PRIMARY KEY (store);

ALTER TABLE ONLY catalog_event_tags
    ADD CONSTRAINT catalog_event_tags_pkey PRIMARY KEY (sequence, phase, tag_id);

ALTER TABLE ONLY catalog_events
    ADD CONSTRAINT catalog_events_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY collection_archive_attestations
    ADD CONSTRAINT collection_archive_attestations_pkey PRIMARY KEY (collection_id, store);

ALTER TABLE ONLY collection_archive_copies
    ADD CONSTRAINT collection_archive_copies_pkey PRIMARY KEY (collection_id, store);

ALTER TABLE ONLY collection_archive_file_objects
    ADD CONSTRAINT collection_archive_file_objects_pkey PRIMARY KEY (collection_id, store, path, sequence);

ALTER TABLE ONLY collection_archive_object_uploads
    ADD CONSTRAINT collection_archive_object_uploads_pkey PRIMARY KEY (collection_id, object_id);

ALTER TABLE ONLY collection_archive_objects
    ADD CONSTRAINT collection_archive_objects_pkey PRIMARY KEY (collection_id, store, object_id);

ALTER TABLE ONLY collection_deletions
    ADD CONSTRAINT collection_deletions_pkey PRIMARY KEY (collection_id);

ALTER TABLE ONLY collection_derivations
    ADD CONSTRAINT collection_derivations_execution_id_key UNIQUE (execution_id);

ALTER TABLE ONLY collection_derivations
    ADD CONSTRAINT collection_derivations_pkey PRIMARY KEY (collection_id);

ALTER TABLE ONLY collection_file_provenance
    ADD CONSTRAINT collection_file_provenance_pkey PRIMARY KEY (collection_id, path);

ALTER TABLE ONLY collection_files
    ADD CONSTRAINT collection_files_pkey PRIMARY KEY (collection_id, path);

ALTER TABLE ONLY collection_metadata_publications
    ADD CONSTRAINT collection_metadata_publications_pkey PRIMARY KEY (collection_id, store);

ALTER TABLE ONLY collection_processing_claim_artifacts
    ADD CONSTRAINT collection_processing_claim_artifacts_pkey PRIMARY KEY (claim_id, collection_id, path);

ALTER TABLE ONLY collection_processing_claim_inputs
    ADD CONSTRAINT collection_processing_claim_inputs_pkey PRIMARY KEY (claim_id, collection_id);

ALTER TABLE ONLY collection_processing_claims
    ADD CONSTRAINT collection_processing_claims_execution_id_key UNIQUE (execution_id);

ALTER TABLE ONLY collection_processing_claims
    ADD CONSTRAINT collection_processing_claims_pkey PRIMARY KEY (id);

ALTER TABLE ONLY collection_processing_outcomes
    ADD CONSTRAINT collection_processing_outcomes_pkey PRIMARY KEY (claim_id, outcome_id);

ALTER TABLE ONLY collection_proof_maturations
    ADD CONSTRAINT collection_proof_maturations_pkey PRIMARY KEY (collection_id, store);

ALTER TABLE ONLY collection_provenance_entities
    ADD CONSTRAINT collection_provenance_entities_pkey PRIMARY KEY (collection_id, journal_id, entity_type, entity_id);

ALTER TABLE ONLY collection_provenance_external_state_references
    ADD CONSTRAINT collection_provenance_external_state_references_pkey PRIMARY KEY (collection_id, from_journal_id, to_journal_id, entry_id, state_id);

ALTER TABLE ONLY collection_provenance_journals
    ADD CONSTRAINT collection_provenance_journals_pkey PRIMARY KEY (collection_id, journal_id);

ALTER TABLE ONLY collection_tags
    ADD CONSTRAINT collection_tags_pkey PRIMARY KEY (collection_id, tag_id);

ALTER TABLE ONLY collection_transform_capabilities
    ADD CONSTRAINT collection_transform_capabilities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY collection_transform_capabilities
    ADD CONSTRAINT collection_transform_capabilities_token_sha256_key UNIQUE (token_sha256);

ALTER TABLE ONLY collection_transform_capability_artifacts
    ADD CONSTRAINT collection_transform_capability_artifacts_pkey PRIMARY KEY (capability_id, collection_id, path);

ALTER TABLE ONLY collection_upload_files
    ADD CONSTRAINT collection_upload_files_pkey PRIMARY KEY (collection_id, path);

ALTER TABLE ONLY collection_upload_provenance_journals
    ADD CONSTRAINT collection_upload_provenance_journals_pkey PRIMARY KEY (collection_id, journal_id);

ALTER TABLE ONLY collection_upload_tags
    ADD CONSTRAINT collection_upload_tags_pkey PRIMARY KEY (collection_id, tag_id);

ALTER TABLE ONLY collection_uploads
    ADD CONSTRAINT collection_uploads_pkey PRIMARY KEY (collection_id);

ALTER TABLE ONLY collections
    ADD CONSTRAINT collections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY key_download_reservations
    ADD CONSTRAINT key_download_reservations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY key_download_usage
    ADD CONSTRAINT key_download_usage_pkey PRIMARY KEY (key_id);

ALTER TABLE ONLY lifecycle_events
    ADD CONSTRAINT lifecycle_events_event_id_key UNIQUE (event_id);

ALTER TABLE ONLY lifecycle_events
    ADD CONSTRAINT lifecycle_events_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY retrieval_cache_leases
    ADD CONSTRAINT retrieval_cache_leases_pkey PRIMARY KEY (owner, source_store, collection_id, object_id);

ALTER TABLE ONLY retrieval_cache_objects
    ADD CONSTRAINT retrieval_cache_objects_pkey PRIMARY KEY (source_store, collection_id, object_id);

ALTER TABLE ONLY retrieval_job_files
    ADD CONSTRAINT retrieval_job_files_pkey PRIMARY KEY (job_id, collection_id, path);

ALTER TABLE ONLY retrieval_job_objects
    ADD CONSTRAINT retrieval_job_objects_pkey PRIMARY KEY (job_id, collection_id, source_store, object_id);

ALTER TABLE ONLY retrieval_jobs
    ADD CONSTRAINT retrieval_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY state_schema_revision
    ADD CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);

ALTER TABLE ONLY collection_processing_claim_inputs
    ADD CONSTRAINT uq_collection_processing_claim_inputs_order UNIQUE (claim_id, collection_order);

ALTER TABLE ONLY collection_processing_claims
    ADD CONSTRAINT uq_collection_processing_claims_owner_work UNIQUE (consumer_app, purpose, work_id);

ALTER TABLE ONLY collection_processing_outcomes
    ADD CONSTRAINT uq_collection_processing_outcomes_output UNIQUE (claim_id, collection_id);

ALTER TABLE ONLY collection_processing_outcomes
    ADD CONSTRAINT uq_collection_processing_outcomes_source_claim UNIQUE (claim_id, source_claim_id);

ALTER TABLE ONLY collections
    ADD CONSTRAINT uq_collections_application_idempotency_key UNIQUE (created_by_app, creation_idempotency_key);

CREATE INDEX idx_collection_archive_file_objects_object ON collection_archive_file_objects USING btree (collection_id, store, object_id);

CREATE INDEX idx_collection_archive_objects_order ON collection_archive_objects USING btree (collection_id, store, object_order);

CREATE INDEX idx_collection_upload_files_collection_order ON collection_upload_files USING btree (collection_id, file_order);

CREATE INDEX ix_app_key_access_grants_created ON app_key_access_grants USING btree (created_at, key_id, permission, resource);

CREATE INDEX ix_app_key_access_grants_permission ON app_key_access_grants USING btree (permission, resource, key_id);

CREATE INDEX ix_app_key_access_grants_resource ON app_key_access_grants USING btree (resource, permission, key_id);

CREATE INDEX ix_app_key_access_grants_search_trgm ON app_key_access_grants USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_app_keys_active ON app_keys USING btree (revoked_at, expires_at, id);

CREATE INDEX ix_app_keys_app ON app_keys USING btree (app, id);

CREATE INDEX ix_app_keys_app_active ON app_keys USING btree (app, revoked_at, expires_at, id);

CREATE INDEX ix_app_keys_app_created ON app_keys USING btree (app, created_at, id);

CREATE INDEX ix_app_keys_app_expires ON app_keys USING btree (app, expires_at, id);

CREATE INDEX ix_app_keys_app_last_used ON app_keys USING btree (app, last_used_at, id);

CREATE INDEX ix_app_keys_app_trgm ON app_keys USING gin (app gin_trgm_ops);

CREATE INDEX ix_app_keys_id_trgm ON app_keys USING gin (id gin_trgm_ops);

CREATE INDEX ix_app_keys_search_trgm ON app_keys USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_archive_copy_jobs_destination ON archive_copy_jobs USING btree (destination_store, collection_id);

CREATE INDEX ix_archive_copy_jobs_due ON archive_copy_jobs USING btree (state, next_attempt_at, requested_at);

CREATE INDEX ix_archive_copy_jobs_requested ON archive_copy_jobs USING btree (requested_at, collection_id);

CREATE INDEX ix_archive_copy_jobs_search_trgm ON archive_copy_jobs USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_archive_copy_jobs_source ON archive_copy_jobs USING btree (source_store, collection_id);

CREATE INDEX ix_archive_copy_jobs_state ON archive_copy_jobs USING btree (state, collection_id);

CREATE INDEX ix_archive_download_reservations_expiry ON archive_download_reservations USING btree (store, expires_at);

CREATE INDEX ix_catalog_event_tags_visibility ON catalog_event_tags USING btree (phase, tag_id, sequence);

CREATE INDEX ix_catalog_events_collection ON catalog_events USING btree (collection_id, sequence);

CREATE INDEX ix_collection_archive_attestations_due ON collection_archive_attestations USING btree (state, next_attempt_at, collection_id, store);

CREATE INDEX ix_collection_derivations_claim ON collection_derivations USING btree (claim_id, collection_id);

CREATE INDEX ix_collection_file_provenance_journal ON collection_file_provenance USING btree (collection_id, journal_id);

CREATE INDEX ix_collection_files_bytes ON collection_files USING btree (bytes, collection_id, path);

CREATE INDEX ix_collection_files_collection_bytes ON collection_files USING btree (collection_id, bytes, path);

CREATE INDEX ix_collection_files_collection_provenance ON collection_files USING btree (collection_id, provenance_status, path);

CREATE INDEX ix_collection_files_path ON collection_files USING btree (path, collection_id);

CREATE INDEX ix_collection_files_path_search_trgm ON collection_files USING gin (path_search_text gin_trgm_ops);

CREATE INDEX ix_collection_files_search_trgm ON collection_files USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_collection_metadata_publications_due ON collection_metadata_publications USING btree (state, next_attempt_at, collection_id, store);

CREATE INDEX ix_collection_processing_claim_artifacts_collection ON collection_processing_claim_artifacts USING btree (collection_id, path, claim_id);

CREATE INDEX ix_collection_processing_claim_inputs_collection ON collection_processing_claim_inputs USING btree (collection_id, claim_id);

CREATE INDEX ix_collection_processing_claims_expiry ON collection_processing_claims USING btree (state, expires_at);

CREATE INDEX ix_collection_processing_claims_owner_created ON collection_processing_claims USING btree (consumer_app, created_at, id);

CREATE INDEX ix_collection_processing_claims_owner_execution ON collection_processing_claims USING btree (consumer_app, execution_id, id);

CREATE INDEX ix_collection_processing_claims_owner_expires ON collection_processing_claims USING btree (consumer_app, expires_at, id);

CREATE INDEX ix_collection_processing_claims_owner_state ON collection_processing_claims USING btree (consumer_app, state, updated_at);

CREATE INDEX ix_collection_processing_claims_owner_state_id ON collection_processing_claims USING btree (consumer_app, state, id);

CREATE INDEX ix_collection_processing_claims_owner_updated ON collection_processing_claims USING btree (consumer_app, updated_at, id);

CREATE INDEX ix_collection_processing_claims_owner_work_id ON collection_processing_claims USING btree (consumer_app, work_id, id);

CREATE INDEX ix_collection_processing_claims_work ON collection_processing_claims USING btree (work_id, consumer_app);

CREATE INDEX ix_collection_processing_outcomes_collection ON collection_processing_outcomes USING btree (collection_id, claim_id);

CREATE INDEX ix_collection_proof_maturations_due ON collection_proof_maturations USING btree (state, next_attempt_at, collection_id, store);

CREATE INDEX ix_collection_provenance_entities_type ON collection_provenance_entities USING btree (collection_id, entity_type, entity_id);

CREATE INDEX ix_collection_provenance_external_state_references_target ON collection_provenance_external_state_references USING btree (collection_id, to_journal_id);

CREATE INDEX ix_collection_provenance_journals_sha256 ON collection_provenance_journals USING btree (sha256, collection_id);

CREATE INDEX ix_collection_tags_tag ON collection_tags USING btree (tag_id, collection_id);

CREATE INDEX ix_collection_tags_tag_trgm ON collection_tags USING gin (tag_id gin_trgm_ops);

CREATE INDEX ix_collection_transform_capabilities_claim_state ON collection_transform_capabilities USING btree (claim_id, state, expires_at);

CREATE INDEX ix_collection_transform_capability_artifacts_collection ON collection_transform_capability_artifacts USING btree (collection_id, path, capability_id);

CREATE INDEX ix_collection_upload_tags_tag ON collection_upload_tags USING btree (tag_id, collection_id);

CREATE INDEX ix_collection_upload_tags_tag_trgm ON collection_upload_tags USING gin (tag_id gin_trgm_ops);

CREATE INDEX ix_collection_uploads_file_bytes ON collection_uploads USING btree (file_bytes, collection_id);

CREATE INDEX ix_collection_uploads_file_count ON collection_uploads USING btree (file_count, collection_id);

CREATE INDEX ix_collection_uploads_opened_at ON collection_uploads USING btree (opened_at, collection_id);

CREATE INDEX ix_collection_uploads_search_trgm ON collection_uploads USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_collection_uploads_state ON collection_uploads USING btree (state, collection_id);

CREATE INDEX ix_collections_created_at_id ON collections USING btree (created_at, id);

CREATE INDEX ix_collections_encryption_format ON collections USING btree (encryption_format, id);

CREATE INDEX ix_collections_file_bytes_id ON collections USING btree (file_bytes, id);

CREATE INDEX ix_collections_file_count_id ON collections USING btree (file_count, id);

CREATE INDEX ix_collections_passphrase_id ON collections USING btree (passphrase_id, id);

CREATE INDEX ix_collections_search_trgm ON collections USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_key_download_reservations_expiry ON key_download_reservations USING btree (expires_at, key_id);

CREATE INDEX ix_key_download_reservations_job ON key_download_reservations USING btree (job_id, kind);

CREATE INDEX ix_key_download_reservations_key_month ON key_download_reservations USING btree (key_id, month_started_at);

CREATE INDEX ix_lifecycle_events_context_expiry ON lifecycle_events USING btree (context_expires_at);

CREATE INDEX ix_lifecycle_events_owner_sequence ON lifecycle_events USING btree (owner_app, sequence);

CREATE INDEX ix_lifecycle_events_owner_subject_context ON lifecycle_events USING btree (owner_app, subject, context_expires_at);

CREATE INDEX ix_retrieval_cache_leases_expiry ON retrieval_cache_leases USING btree (expires_at, owner);

CREATE INDEX ix_retrieval_cache_leases_object_expiry ON retrieval_cache_leases USING btree (source_store, collection_id, object_id, expires_at, owner);

CREATE INDEX ix_retrieval_cache_objects_bytes ON retrieval_cache_objects USING btree (stored_bytes, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_cached ON retrieval_cache_objects USING btree (cached_at, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_cleanup ON retrieval_cache_objects USING btree (state, cached_at);

CREATE INDEX ix_retrieval_cache_objects_collection ON retrieval_cache_objects USING btree (collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_cache_objects_object ON retrieval_cache_objects USING btree (object_id, collection_id, source_store);

CREATE INDEX ix_retrieval_cache_objects_search_trgm ON retrieval_cache_objects USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_retrieval_cache_objects_verified ON retrieval_cache_objects USING btree (verified_at, collection_id, source_store, object_id);

CREATE INDEX ix_retrieval_job_files_order ON retrieval_job_files USING btree (job_id, file_order);

CREATE INDEX ix_retrieval_job_objects_order ON retrieval_job_objects USING btree (job_id, object_order);

CREATE INDEX ix_retrieval_jobs_due ON retrieval_jobs USING btree (state, next_poll_at, id);

CREATE INDEX ix_tags_collection_count_id ON tags USING btree (collection_count, id);

CREATE INDEX ix_tags_created_at_id ON tags USING btree (created_at, id);

CREATE INDEX ix_tags_id_trgm ON tags USING gin (id gin_trgm_ops);

CREATE UNIQUE INDEX ux_app_keys_token_sha256 ON app_keys USING btree (token_sha256);

CREATE UNIQUE INDEX ux_collection_archive_object_uploads_sequence ON collection_archive_object_uploads USING btree (collection_id, sequence);

CREATE UNIQUE INDEX ux_collection_upload_files_order ON collection_upload_files USING btree (collection_id, file_order);

CREATE UNIQUE INDEX ux_collection_uploads_application_idempotency_key ON collection_uploads USING btree (initiated_by_app, idempotency_key);

ALTER TABLE ONLY app_key_access_grants
    ADD CONSTRAINT app_key_access_grants_key_id_fkey FOREIGN KEY (key_id) REFERENCES app_keys(id) ON DELETE CASCADE;

ALTER TABLE ONLY archive_copy_jobs
    ADD CONSTRAINT archive_copy_jobs_collection_id_source_store_fkey FOREIGN KEY (collection_id, source_store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY archive_copy_object_uploads
    ADD CONSTRAINT archive_copy_object_uploads_collection_id_destination_stor_fkey FOREIGN KEY (collection_id, destination_store) REFERENCES archive_copy_jobs(collection_id, destination_store) ON DELETE CASCADE;

ALTER TABLE ONLY archive_copy_retirements
    ADD CONSTRAINT archive_copy_retirements_collection_id_store_fkey FOREIGN KEY (collection_id, store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY archive_download_reservations
    ADD CONSTRAINT archive_download_reservations_store_fkey FOREIGN KEY (store) REFERENCES archive_download_usage(store) ON DELETE CASCADE;

ALTER TABLE ONLY catalog_event_tags
    ADD CONSTRAINT catalog_event_tags_sequence_fkey FOREIGN KEY (sequence) REFERENCES catalog_events(sequence) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_attestations
    ADD CONSTRAINT collection_archive_attestations_collection_id_store_fkey FOREIGN KEY (collection_id, store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_copies
    ADD CONSTRAINT collection_archive_copies_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_file_objects
    ADD CONSTRAINT collection_archive_file_objec_collection_id_store_object_i_fkey FOREIGN KEY (collection_id, store, object_id) REFERENCES collection_archive_objects(collection_id, store, object_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_file_objects
    ADD CONSTRAINT collection_archive_file_objects_collection_id_path_fkey FOREIGN KEY (collection_id, path) REFERENCES collection_files(collection_id, path) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_object_uploads
    ADD CONSTRAINT collection_archive_object_uploads_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collection_uploads(collection_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_archive_objects
    ADD CONSTRAINT collection_archive_objects_collection_id_store_fkey FOREIGN KEY (collection_id, store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY collection_derivations
    ADD CONSTRAINT collection_derivations_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES collection_processing_claims(id) ON DELETE RESTRICT;

ALTER TABLE ONLY collection_derivations
    ADD CONSTRAINT collection_derivations_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_file_provenance
    ADD CONSTRAINT collection_file_provenance_collection_id_journal_id_fkey FOREIGN KEY (collection_id, journal_id) REFERENCES collection_provenance_journals(collection_id, journal_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_file_provenance
    ADD CONSTRAINT collection_file_provenance_collection_id_path_fkey FOREIGN KEY (collection_id, path) REFERENCES collection_files(collection_id, path) ON DELETE CASCADE;

ALTER TABLE ONLY collection_files
    ADD CONSTRAINT collection_files_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_metadata_publications
    ADD CONSTRAINT collection_metadata_publications_collection_id_store_fkey FOREIGN KEY (collection_id, store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY collection_processing_claim_artifacts
    ADD CONSTRAINT collection_processing_claim_artifac_claim_id_collection_id_fkey FOREIGN KEY (claim_id, collection_id) REFERENCES collection_processing_claim_inputs(claim_id, collection_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_processing_claim_inputs
    ADD CONSTRAINT collection_processing_claim_inputs_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES collection_processing_claims(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_processing_claims
    ADD CONSTRAINT collection_processing_claims_output_collection_id_fkey FOREIGN KEY (output_collection_id) REFERENCES collections(id) ON DELETE SET NULL;

ALTER TABLE ONLY collection_processing_outcomes
    ADD CONSTRAINT collection_processing_outcomes_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES collection_processing_claims(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_processing_outcomes
    ADD CONSTRAINT collection_processing_outcomes_source_claim_id_fkey FOREIGN KEY (source_claim_id) REFERENCES collection_processing_claims(id) ON DELETE RESTRICT;

ALTER TABLE ONLY collection_proof_maturations
    ADD CONSTRAINT collection_proof_maturations_collection_id_store_fkey FOREIGN KEY (collection_id, store) REFERENCES collection_archive_copies(collection_id, store) ON DELETE CASCADE;

ALTER TABLE ONLY collection_provenance_entities
    ADD CONSTRAINT collection_provenance_entities_collection_id_journal_id_fkey FOREIGN KEY (collection_id, journal_id) REFERENCES collection_provenance_journals(collection_id, journal_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_provenance_external_state_references
    ADD CONSTRAINT collection_provenance_externa_collection_id_from_journal_i_fkey FOREIGN KEY (collection_id, from_journal_id) REFERENCES collection_provenance_journals(collection_id, journal_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_provenance_external_state_references
    ADD CONSTRAINT collection_provenance_external_collection_id_to_journal_id_fkey FOREIGN KEY (collection_id, to_journal_id) REFERENCES collection_provenance_journals(collection_id, journal_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_provenance_journals
    ADD CONSTRAINT collection_provenance_journals_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_tags
    ADD CONSTRAINT collection_tags_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_tags
    ADD CONSTRAINT collection_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE RESTRICT;

ALTER TABLE ONLY collection_transform_capabilities
    ADD CONSTRAINT collection_transform_capabilities_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES collection_processing_claims(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_transform_capability_artifacts
    ADD CONSTRAINT collection_transform_capability_artifacts_capability_id_fkey FOREIGN KEY (capability_id) REFERENCES collection_transform_capabilities(id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_upload_files
    ADD CONSTRAINT collection_upload_files_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collection_uploads(collection_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_upload_provenance_journals
    ADD CONSTRAINT collection_upload_provenance_journals_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collection_uploads(collection_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_upload_tags
    ADD CONSTRAINT collection_upload_tags_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES collection_uploads(collection_id) ON DELETE CASCADE;

ALTER TABLE ONLY collection_upload_tags
    ADD CONSTRAINT collection_upload_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE RESTRICT;

ALTER TABLE ONLY key_download_reservations
    ADD CONSTRAINT key_download_reservations_key_id_fkey FOREIGN KEY (key_id) REFERENCES app_keys(id) ON DELETE CASCADE;

ALTER TABLE ONLY key_download_usage
    ADD CONSTRAINT key_download_usage_key_id_fkey FOREIGN KEY (key_id) REFERENCES app_keys(id) ON DELETE CASCADE;

ALTER TABLE ONLY retrieval_cache_leases
    ADD CONSTRAINT retrieval_cache_leases_source_store_collection_id_object_i_fkey FOREIGN KEY (source_store, collection_id, object_id) REFERENCES retrieval_cache_objects(source_store, collection_id, object_id) ON DELETE CASCADE;

ALTER TABLE ONLY retrieval_cache_objects
    ADD CONSTRAINT retrieval_cache_objects_collection_id_source_store_object__fkey FOREIGN KEY (collection_id, source_store, object_id) REFERENCES collection_archive_objects(collection_id, store, object_id) ON DELETE CASCADE;

ALTER TABLE ONLY retrieval_job_files
    ADD CONSTRAINT retrieval_job_files_collection_id_path_fkey FOREIGN KEY (collection_id, path) REFERENCES collection_files(collection_id, path);

ALTER TABLE ONLY retrieval_job_files
    ADD CONSTRAINT retrieval_job_files_job_id_fkey FOREIGN KEY (job_id) REFERENCES retrieval_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY retrieval_job_objects
    ADD CONSTRAINT retrieval_job_objects_collection_id_source_store_object_id_fkey FOREIGN KEY (collection_id, source_store, object_id) REFERENCES collection_archive_objects(collection_id, store, object_id);

ALTER TABLE ONLY retrieval_job_objects
    ADD CONSTRAINT retrieval_job_objects_job_id_fkey FOREIGN KEY (job_id) REFERENCES retrieval_jobs(id) ON DELETE CASCADE;

INSERT INTO state_schema_revision (version_num) VALUES ('v1_0001');
