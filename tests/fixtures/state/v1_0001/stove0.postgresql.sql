-- Immutable Stove0 PostgreSQL v1.0 database fixture. Add later baselines; do not edit.
CREATE TABLE stove0_state_schema_revision (
    version_num VARCHAR(32) NOT NULL PRIMARY KEY
);
INSERT INTO stove0_state_schema_revision (version_num) VALUES ('v1_0001');

CREATE TABLE stove0_work_records (
    work_id VARCHAR(64) NOT NULL PRIMARY KEY,
    revision INTEGER NOT NULL,
    phase VARCHAR(32) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    document_json TEXT NOT NULL
);
CREATE INDEX ix_stove0_work_records_phase ON stove0_work_records (phase);
CREATE INDEX ix_stove0_work_records_updated_at ON stove0_work_records (updated_at);
INSERT INTO stove0_work_records (
    work_id, revision, phase, updated_at, document_json
) VALUES (
    '5eadff5ce59d17eb1975ab49c17ba9f8ef2746c0a304f917c8ad779ef9c065c2',
    1,
    'eligible',
    '2026-01-01T00:00:00.000000Z',
    '{"format":"stove0-work-record/v1","work":{"format":"stove0-work/v1","recipe":{"id":"fixture.recipe/v1","revision":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"inputs":[{"collection_id":1,"archive_root_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","content_identity":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}],"effective_intent":{},"work_id":"5eadff5ce59d17eb1975ab49c17ba9f8ef2746c0a304f917c8ad779ef9c065c2"},"phase":"eligible","revision":1,"observation_requests":[],"observation_results":[],"coordination_cancel_requested":false,"retirement_remaining":[]}'
);

CREATE TABLE stove0_evaluation_records (
    evaluation_id VARCHAR(64) NOT NULL PRIMARY KEY,
    revision INTEGER NOT NULL,
    phase VARCHAR(32) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    document_json TEXT NOT NULL
);
CREATE INDEX ix_stove0_evaluation_records_phase ON stove0_evaluation_records (phase);
CREATE INDEX ix_stove0_evaluation_records_updated_at ON stove0_evaluation_records (updated_at);

CREATE TABLE stove0_event_cursors (
    stream VARCHAR(160) NOT NULL PRIMARY KEY,
    cursor VARCHAR(500) NOT NULL,
    revision INTEGER NOT NULL,
    updated_at VARCHAR(40) NOT NULL
);
INSERT INTO stove0_event_cursors (stream, cursor, revision, updated_at) VALUES (
    'riverhog-catalog', '41', 1, '2026-01-01T00:00:00.000000Z'
);

CREATE TABLE stove0_lifecycle_events (
    sequence SERIAL NOT NULL PRIMARY KEY,
    event_json TEXT NOT NULL
);
