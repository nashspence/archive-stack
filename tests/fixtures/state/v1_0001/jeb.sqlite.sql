-- Immutable Jeb v1.0 database fixture. Add a new fixture for later baselines; do not edit.
BEGIN TRANSACTION;
CREATE TABLE attempt_files (
                attempt_id TEXT NOT NULL,
                target_path TEXT NOT NULL,
                staging_path TEXT NOT NULL,
                staged_at TEXT,
                PRIMARY KEY (attempt_id, target_path),
                FOREIGN KEY(attempt_id) REFERENCES batch_attempts(id)
            );
INSERT INTO "attempt_files" VALUES('fixture-attempt','fixture-camera/notes/fixture.txt','/fixture/staging/fixture.txt','2026-01-01T00:00:00.000000Z');
CREATE TABLE batch_attempts (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                state TEXT NOT NULL,
                target_submission_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT,
                emitted_error_fingerprint TEXT,
                emitted_error_at TEXT,
                UNIQUE(batch_id, attempt_number),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );
INSERT INTO "batch_attempts" VALUES('fixture-attempt','fixture-batch',1,'target_complete',NULL,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z',NULL,NULL,NULL);
CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                run_id TEXT NOT NULL,
                cleanup TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
INSERT INTO "batches" VALUES('fixture-batch','fixture-camera','munchy','fixture-run','after_target_success','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,12,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z');
CREATE TABLE files (
                batch_id TEXT NOT NULL,
                input_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT,
                PRIMARY KEY (batch_id, target_path),
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );
INSERT INTO "files" VALUES('fixture-batch','notes/fixture.txt','fixture-camera/notes/fixture.txt',12,1,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
CREATE TABLE lifecycle_event_cursors (
        source TEXT PRIMARY KEY,
        cursor TEXT NOT NULL
    );
INSERT INTO "lifecycle_event_cursors" VALUES('munchy','17');
CREATE TABLE lifecycle_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        event_json TEXT NOT NULL,
        context_json TEXT,
        context_expires_at TEXT
    );
INSERT INTO "lifecycle_events" VALUES(1,'jeb-v1-event','fixture-client','{"data": {"attempt_id": "fixture-attempt"}, "datacontenttype": "application/json", "id": "jeb-v1-event", "source": "urn:fixture:jeb", "specversion": "1.0", "subject": "fixture-attempt", "time": "2026-01-01T00:00:00.000000Z", "type": "io.riverhog.jeb.attempt.completed"}','{"workflow": "fixture"}',NULL);
CREATE TABLE service_operations (
                id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                source TEXT,
                attempt_id TEXT,
                failure TEXT
            );
CREATE TABLE source_removals (
                challenge TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                adapters_json TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                upload_signing_key TEXT NOT NULL,
                stable_seconds INTEGER NOT NULL,
                include_extensions_json TEXT NOT NULL,
                target TEXT NOT NULL,
                target_config_json TEXT NOT NULL,
                threshold_bytes INTEGER NOT NULL,
                cleanup TEXT NOT NULL,
                cadence TEXT NOT NULL,
                weekday INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
INSERT INTO "sources" VALUES('fixture-camera',1,'["tus"]','fixture-password-hash','ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',0,'[".txt"]','munchy','{"template_id": "fixture-archive"}',0,'after_target_success','manual',0,3,0,'2026-01-01T00:00:00.000000Z','2026-01-01T00:00:00.000000Z');
CREATE TABLE state_schema_revision (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);
INSERT INTO "state_schema_revision" VALUES('v1_0001');
CREATE TABLE target_preflight_failures (
                source_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                target_name TEXT NOT NULL,
                input_paths_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                message TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                emitted_error_fingerprint TEXT,
                emitted_error_at TEXT
            );
CREATE TRIGGER trg_jeb_files_summary_insert
            AFTER INSERT ON files
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count + 1,
                    total_bytes = total_bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END;
CREATE TRIGGER trg_jeb_files_summary_delete
            AFTER DELETE ON files
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count - 1,
                    total_bytes = total_bytes - OLD.bytes
                WHERE id = OLD.batch_id;
            END;
CREATE TRIGGER trg_jeb_files_summary_update_same_batch
            AFTER UPDATE OF batch_id, bytes ON files
            WHEN OLD.batch_id = NEW.batch_id
            BEGIN
                UPDATE batches
                SET total_bytes = total_bytes - OLD.bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END;
CREATE TRIGGER trg_jeb_files_summary_update_moved_batch
            AFTER UPDATE OF batch_id, bytes ON files
            WHEN OLD.batch_id != NEW.batch_id
            BEGIN
                UPDATE batches
                SET
                    file_count = file_count - 1,
                    total_bytes = total_bytes - OLD.bytes
                WHERE id = OLD.batch_id;

                UPDATE batches
                SET
                    file_count = file_count + 1,
                    total_bytes = total_bytes + NEW.bytes
                WHERE id = NEW.batch_id;
            END;
CREATE INDEX idx_jeb_batches_source_period ON batches(source_id, run_id);
CREATE INDEX idx_jeb_batches_source ON batches(source_id, id);
CREATE INDEX idx_jeb_batches_target ON batches(target_name, id);
CREATE INDEX idx_jeb_batches_file_count ON batches(file_count, id);
CREATE INDEX idx_jeb_batches_total_bytes ON batches(total_bytes, id);
CREATE INDEX idx_jeb_batch_attempts_state ON batch_attempts(state, created_at);
CREATE INDEX idx_jeb_batch_attempts_updated ON batch_attempts(updated_at, id);
CREATE INDEX idx_jeb_batch_attempts_created ON batch_attempts(created_at, id);
CREATE INDEX idx_jeb_batch_attempts_state_updated ON batch_attempts(state, updated_at, id);
CREATE INDEX idx_jeb_batch_attempts_target_submission ON batch_attempts(target_submission_id, id);
CREATE INDEX idx_jeb_batch_attempts_batch_state ON batch_attempts(batch_id, state);
CREATE INDEX idx_jeb_files_batch ON files(batch_id);
CREATE INDEX idx_jeb_attempt_files_attempt ON attempt_files(attempt_id);
CREATE INDEX idx_jeb_target_preflight_failures_state ON target_preflight_failures(state, updated_at);
CREATE INDEX idx_jeb_source_removals_source ON source_removals(source_id, started_at);
CREATE INDEX idx_jeb_service_operations_started ON service_operations(started_at, id);
CREATE INDEX idx_jeb_service_operations_state_started ON service_operations(state, started_at, id);
CREATE UNIQUE INDEX ux_jeb_service_operations_running ON service_operations(state) WHERE state = 'running';
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
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('lifecycle_events',1);
COMMIT;
