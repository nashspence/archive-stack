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

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE stove0_artifact_selections (
    selection_sha256 character varying(64) NOT NULL,
    artifact_count integer NOT NULL,
    total_bytes bigint NOT NULL
);

CREATE TABLE stove0_artifact_selection_members (
    selection_sha256 character varying(64) NOT NULL,
    ordinal integer NOT NULL,
    document_json text NOT NULL
);

CREATE TABLE stove0_evaluation_records (
    evaluation_id character varying(64) NOT NULL,
    revision integer NOT NULL,
    phase character varying(32) NOT NULL,
    updated_at character varying(40) NOT NULL,
    document_json text NOT NULL
);

CREATE TABLE stove0_event_cursors (
    stream character varying(160) NOT NULL,
    cursor character varying(500) NOT NULL,
    revision integer NOT NULL,
    updated_at character varying(40) NOT NULL
);

CREATE TABLE stove0_lifecycle_events (
    sequence integer NOT NULL,
    created_at character varying(40) NOT NULL,
    event_json text NOT NULL
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

CREATE TABLE stove0_work_records (
    work_id character varying(64) NOT NULL,
    revision integer NOT NULL,
    phase character varying(32) NOT NULL,
    updated_at character varying(40) NOT NULL,
    document_json text NOT NULL
);

ALTER TABLE ONLY stove0_lifecycle_events ALTER COLUMN sequence SET DEFAULT nextval('stove0_lifecycle_events_sequence_seq'::regclass);

ALTER TABLE ONLY stove0_artifact_selections
    ADD CONSTRAINT stove0_artifact_selections_pkey PRIMARY KEY (selection_sha256);

ALTER TABLE ONLY stove0_artifact_selection_members
    ADD CONSTRAINT stove0_artifact_selection_members_pkey PRIMARY KEY (selection_sha256, ordinal);

ALTER TABLE ONLY stove0_evaluation_records
    ADD CONSTRAINT stove0_evaluation_records_pkey PRIMARY KEY (evaluation_id);

ALTER TABLE ONLY stove0_event_cursors
    ADD CONSTRAINT stove0_event_cursors_pkey PRIMARY KEY (stream);

ALTER TABLE ONLY stove0_lifecycle_events
    ADD CONSTRAINT stove0_lifecycle_events_pkey PRIMARY KEY (sequence);

ALTER TABLE ONLY stove0_state_schema_revision
    ADD CONSTRAINT stove0_state_schema_revision_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY stove0_work_records
    ADD CONSTRAINT stove0_work_records_pkey PRIMARY KEY (work_id);

ALTER TABLE ONLY stove0_artifact_selection_members
    ADD CONSTRAINT stove0_artifact_selection_members_selection_sha256_fkey
    FOREIGN KEY (selection_sha256) REFERENCES stove0_artifact_selections(selection_sha256)
    ON DELETE CASCADE;

CREATE INDEX ix_stove0_evaluation_records_phase ON stove0_evaluation_records USING btree (phase);

CREATE INDEX ix_stove0_evaluation_records_updated_at ON stove0_evaluation_records USING btree (updated_at);

CREATE INDEX ix_stove0_lifecycle_events_created_at ON stove0_lifecycle_events USING btree (created_at);

CREATE INDEX ix_stove0_work_records_phase ON stove0_work_records USING btree (phase);

CREATE INDEX ix_stove0_work_records_phase_work_id ON stove0_work_records USING btree (phase, work_id);

CREATE INDEX ix_stove0_work_records_updated_at ON stove0_work_records USING btree (updated_at);


INSERT INTO stove0_state_schema_revision (version_num) VALUES ('v1_0001');
