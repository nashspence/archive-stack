-- Exact current Stove0 PostgreSQL v1 baseline conformance fixture.

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

CREATE TABLE stove0_artifact_selection_members (
    selection_sha256 character varying(64) NOT NULL,
    ordinal integer NOT NULL,
    document_bytes bigint NOT NULL,
    document_json text NOT NULL,
    CONSTRAINT ck_stove0_selection_members_document_bytes CHECK ((document_bytes >= 0)),
    CONSTRAINT ck_stove0_selection_members_ordinal CHECK ((ordinal >= 0))
);

CREATE TABLE stove0_artifact_selections (
    selection_sha256 character varying(64) NOT NULL,
    artifact_count integer NOT NULL,
    total_bytes bigint NOT NULL,
    CONSTRAINT ck_stove0_selections_bytes CHECK ((total_bytes >= 0)),
    CONSTRAINT ck_stove0_selections_count CHECK ((artifact_count >= 0)),
    CONSTRAINT ck_stove0_selections_id CHECK ((length((selection_sha256)::text) = 64))
);

CREATE TABLE stove0_evaluation_children (
    evaluation_id character varying(64) NOT NULL,
    work_id character varying(64) NOT NULL
);

CREATE TABLE stove0_evaluation_records (
    evaluation_id character varying(64) NOT NULL,
    revision integer NOT NULL,
    phase character varying(32) NOT NULL,
    updated_at character varying(40) NOT NULL,
    document_bytes bigint NOT NULL,
    document_json text NOT NULL,
    CONSTRAINT ck_stove0_evaluation_records_document_bytes CHECK ((document_bytes >= 0)),
    CONSTRAINT ck_stove0_evaluation_records_id CHECK ((length((evaluation_id)::text) = 64)),
    CONSTRAINT ck_stove0_evaluation_records_phase CHECK (((phase)::text = ANY ((ARRAY['planning'::character varying, 'running'::character varying, 'partially_complete'::character varying, 'complete'::character varying, 'failed'::character varying, 'canceled'::character varying])::text[]))),
    CONSTRAINT ck_stove0_evaluation_records_revision CHECK ((revision >= 1))
);

CREATE TABLE stove0_event_cursors (
    stream character varying(160) NOT NULL,
    cursor character varying(500) NOT NULL,
    revision integer NOT NULL,
    updated_at character varying(40) NOT NULL,
    CONSTRAINT ck_stove0_event_cursors_revision CHECK ((revision >= 1))
);

CREATE TABLE stove0_lifecycle_events (
    sequence integer NOT NULL,
    created_at character varying(40) NOT NULL,
    event_bytes bigint NOT NULL,
    event_json text NOT NULL,
    CONSTRAINT ck_stove0_lifecycle_events_event_bytes CHECK ((event_bytes >= 0))
);

CREATE SEQUENCE stove0_lifecycle_events_sequence_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE stove0_lifecycle_events_sequence_seq OWNED BY stove0_lifecycle_events.sequence;

CREATE TABLE stove0_state_schema_revision (
    version_num character varying(32) NOT NULL
);

CREATE TABLE stove0_work_evaluations (
    work_id character varying(64) NOT NULL,
    evaluation_id character varying(64) NOT NULL
);

CREATE TABLE stove0_work_records (
    work_id character varying(64) NOT NULL,
    revision integer NOT NULL,
    phase character varying(32) NOT NULL,
    updated_at character varying(40) NOT NULL,
    document_bytes bigint NOT NULL,
    document_json text NOT NULL,
    CONSTRAINT ck_stove0_work_records_document_bytes CHECK ((document_bytes >= 0)),
    CONSTRAINT ck_stove0_work_records_id CHECK ((length((work_id)::text) = 64)),
    CONSTRAINT ck_stove0_work_records_phase CHECK (((phase)::text = ANY ((ARRAY['eligible'::character varying, 'claimed'::character varying, 'observing'::character varying, 'planning'::character varying, 'target_preflight'::character varying, 'queued'::character varying, 'executing'::character varying, 'output_finalizing'::character varying, 'verifying'::character varying, 'settled'::character varying, 'retirement_pending'::character varying, 'coordinating'::character varying, 'abandon_pending'::character varying, 'complete'::character varying, 'inapplicable'::character varying, 'failed'::character varying, 'canceled'::character varying])::text[]))),
    CONSTRAINT ck_stove0_work_records_revision CHECK ((revision >= 1))
);

CREATE TABLE stove0_work_relations (
    work_id character varying(64) NOT NULL,
    related_work_id character varying(64) NOT NULL,
    CONSTRAINT ck_stove0_work_relations_distinct CHECK (((work_id)::text <> (related_work_id)::text))
);

CREATE TABLE stove0_work_selection_references (
    work_id character varying(64) NOT NULL,
    selection_sha256 character varying(64) NOT NULL
);

ALTER TABLE ONLY stove0_lifecycle_events ALTER COLUMN sequence SET DEFAULT nextval('stove0_lifecycle_events_sequence_seq'::regclass);

ALTER TABLE ONLY stove0_artifact_selection_members
    ADD CONSTRAINT stove0_artifact_selection_members_pkey PRIMARY KEY (selection_sha256, ordinal);

ALTER TABLE ONLY stove0_artifact_selections
    ADD CONSTRAINT stove0_artifact_selections_pkey PRIMARY KEY (selection_sha256);

ALTER TABLE ONLY stove0_evaluation_children
    ADD CONSTRAINT stove0_evaluation_children_pkey PRIMARY KEY (evaluation_id, work_id);

ALTER TABLE ONLY stove0_evaluation_records
    ADD CONSTRAINT stove0_evaluation_records_pkey PRIMARY KEY (evaluation_id);

ALTER TABLE ONLY stove0_event_cursors
    ADD CONSTRAINT stove0_event_cursors_pkey PRIMARY KEY (stream);

ALTER TABLE ONLY stove0_lifecycle_events
    ADD CONSTRAINT stove0_lifecycle_events_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY stove0_state_schema_revision
    ADD CONSTRAINT stove0_state_schema_revision_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY stove0_work_evaluations
    ADD CONSTRAINT stove0_work_evaluations_pkey PRIMARY KEY (work_id, evaluation_id);

ALTER TABLE ONLY stove0_work_records
    ADD CONSTRAINT stove0_work_records_pkey PRIMARY KEY (work_id);

ALTER TABLE ONLY stove0_work_relations
    ADD CONSTRAINT stove0_work_relations_pkey PRIMARY KEY (work_id, related_work_id);

ALTER TABLE ONLY stove0_work_selection_references
    ADD CONSTRAINT stove0_work_selection_references_pkey PRIMARY KEY (work_id, selection_sha256);

CREATE INDEX ix_stove0_evaluation_children_work ON stove0_evaluation_children USING btree (work_id, evaluation_id);

CREATE INDEX ix_stove0_evaluation_records_id_trgm ON stove0_evaluation_records USING gin (evaluation_id gin_trgm_ops);

CREATE INDEX ix_stove0_evaluation_records_phase_id ON stove0_evaluation_records USING btree (phase, evaluation_id);

CREATE INDEX ix_stove0_evaluation_records_updated_id ON stove0_evaluation_records USING btree (updated_at, evaluation_id);

CREATE INDEX ix_stove0_lifecycle_events_created_at ON stove0_lifecycle_events USING btree (created_at);

CREATE INDEX ix_stove0_work_evaluations_evaluation ON stove0_work_evaluations USING btree (evaluation_id, work_id);

CREATE INDEX ix_stove0_work_records_id_trgm ON stove0_work_records USING gin (work_id gin_trgm_ops);

CREATE INDEX ix_stove0_work_records_phase_work_id ON stove0_work_records USING btree (phase, work_id);

CREATE INDEX ix_stove0_work_records_updated_work_id ON stove0_work_records USING btree (updated_at, work_id);

CREATE INDEX ix_stove0_work_relations_related ON stove0_work_relations USING btree (related_work_id, work_id);

CREATE INDEX ix_stove0_work_selection_references_selection ON stove0_work_selection_references USING btree (selection_sha256, work_id);

ALTER TABLE ONLY stove0_artifact_selection_members
    ADD CONSTRAINT stove0_artifact_selection_members_selection_sha256_fkey FOREIGN KEY (selection_sha256) REFERENCES stove0_artifact_selections(selection_sha256) ON DELETE CASCADE;

ALTER TABLE ONLY stove0_evaluation_children
    ADD CONSTRAINT stove0_evaluation_children_evaluation_id_fkey FOREIGN KEY (evaluation_id) REFERENCES stove0_evaluation_records(evaluation_id) ON DELETE CASCADE;

ALTER TABLE ONLY stove0_work_evaluations
    ADD CONSTRAINT stove0_work_evaluations_work_id_fkey FOREIGN KEY (work_id) REFERENCES stove0_work_records(work_id) ON DELETE CASCADE;

ALTER TABLE ONLY stove0_work_relations
    ADD CONSTRAINT stove0_work_relations_work_id_fkey FOREIGN KEY (work_id) REFERENCES stove0_work_records(work_id) ON DELETE CASCADE;

ALTER TABLE ONLY stove0_work_selection_references
    ADD CONSTRAINT stove0_work_selection_references_work_id_fkey FOREIGN KEY (work_id) REFERENCES stove0_work_records(work_id) ON DELETE CASCADE;

INSERT INTO stove0_state_schema_revision (version_num) VALUES ('v1_0001');
