-- Immutable Munchy v1.0 database fixture. Add a new fixture for later baselines; do not edit.
BEGIN TRANSACTION;
CREATE TABLE application_keys (
        id TEXT PRIMARY KEY,
        app TEXT NOT NULL,
        token_sha256 TEXT NOT NULL UNIQUE,
        permissions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT,
        last_used_at TEXT
    );
INSERT INTO "application_keys" VALUES('fixture-key','fixture-client','0acf3c7a1c0287dba4d20bb031b32a5800f5c3bfa1e03d20b0363f1efe4c64d2','["events:read", "submissions:manage"]','2026-01-01T00:00:00.000000Z',NULL,NULL,NULL);
CREATE TABLE job_diagnostics (
        job_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes >= 0),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
    );
CREATE TABLE job_summaries (
        job_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        phase TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        input_upload_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        workflow_mode TEXT NOT NULL,
        handoff_destination TEXT NOT NULL,
        output_mode TEXT NOT NULL,
        profile TEXT NOT NULL,
        terminal INTEGER NOT NULL,
        cancel_requested INTEGER NOT NULL,
        storage_wait INTEGER NOT NULL
    );
PRAGMA writable_schema=ON;
INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql)VALUES('table','job_summaries_fts','job_summaries_fts',0,'CREATE VIRTUAL TABLE job_summaries_fts USING fts5(job_id UNINDEXED, search_text)');
CREATE TABLE 'job_summaries_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO "job_summaries_fts_config" VALUES('version',4);
CREATE TABLE 'job_summaries_fts_content'(id INTEGER PRIMARY KEY, c0, c1);
CREATE TABLE 'job_summaries_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO "job_summaries_fts_data" VALUES(1,X'');
INSERT INTO "job_summaries_fts_data" VALUES(10,X'00000000000000');
CREATE TABLE 'job_summaries_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'job_summaries_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE job_templates (
        template_id TEXT PRIMARY KEY,
        definition TEXT NOT NULL,
        resolved_job TEXT NOT NULL,
        digest TEXT NOT NULL,
        revision INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE lifecycle_event_cursors (
        source TEXT PRIMARY KEY,
        cursor TEXT NOT NULL
    );
INSERT INTO "lifecycle_event_cursors" VALUES('riverhog','41');
CREATE TABLE lifecycle_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        event_json TEXT NOT NULL,
        context_json TEXT,
        context_expires_at TEXT
    );
INSERT INTO "lifecycle_events" VALUES(1,'munchy-v1-event','fixture-client','{"data": {"job_id": "fixture-job"}, "datacontenttype": "application/json", "id": "munchy-v1-event", "source": "urn:fixture:munchy", "specversion": "1.0", "subject": "fixture-job", "time": "2026-01-01T00:00:00.000000Z", "type": "io.riverhog.munchy.job.completed"}','{"workflow": "fixture"}',NULL);
CREATE TABLE state_schema_revision (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);
INSERT INTO "state_schema_revision" VALUES('v1_0001');
CREATE TABLE states (
        kind TEXT NOT NULL,
        id TEXT NOT NULL,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    );
INSERT INTO "states" VALUES('job','fixture-job','{"job_id": "fixture-job", "state": "completed"}','2026-01-01T00:00:00.000000Z');
CREATE INDEX application_keys_app_id ON application_keys(app, id);
CREATE INDEX states_kind_updated_at ON states(kind, updated_at);
CREATE INDEX riverhog_jobs_adapter_collection
ON states(
    kind,
    json_extract(payload, '$.handoff_adapter_state.collection_id'),
    updated_at
);
CREATE INDEX riverhog_jobs_receipt_collection
ON states(kind, json_extract(payload, '$.handoff_receipt.external_id'), updated_at);
CREATE INDEX riverhog_jobs_progress_collection
ON states(kind, json_extract(payload, '$.handoff_progress.external_id'), updated_at);
CREATE INDEX job_templates_enabled_id ON job_templates(enabled, template_id);
CREATE INDEX job_templates_updated_id ON job_templates(updated_at, template_id);
CREATE INDEX job_summaries_terminal_updated
    ON job_summaries(terminal, updated_at, job_id)
    ;
CREATE INDEX job_summaries_state_updated
    ON job_summaries(state, updated_at, job_id)
    ;
CREATE INDEX job_summaries_workflow_updated
    ON job_summaries(workflow_mode, updated_at, job_id)
    ;
CREATE INDEX job_summaries_handoff_destination_updated
    ON job_summaries(handoff_destination, updated_at, job_id)
    ;
CREATE INDEX job_summaries_run_updated
    ON job_summaries(run_id, updated_at, job_id)
    ;
CREATE INDEX job_diagnostics_created
    ON job_diagnostics(created_at, job_id)
    ;
CREATE INDEX lifecycle_events_owner_sequence
    ON lifecycle_events(owner, sequence)
    ;
CREATE INDEX lifecycle_events_context_expiry
    ON lifecycle_events(context_expires_at)
    WHERE context_json IS NOT NULL
    ;
CREATE INDEX lifecycle_events_owner_subject_context
    ON lifecycle_events(owner, json_extract(event_json, '$.subject'))
    WHERE context_json IS NOT NULL AND context_expires_at IS NULL
    ;
PRAGMA writable_schema=OFF;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('lifecycle_events',1);
COMMIT;
