-- Exact current Gogurt listener v1 baseline conformance fixture.
BEGIN TRANSACTION;
CREATE TABLE listener_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO listener_meta VALUES ('schema', '1');
CREATE TABLE observed_mounts (
    mount_point TEXT NOT NULL PRIMARY KEY,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    marker_identity TEXT
);
INSERT INTO observed_mounts VALUES (
    '/fixture/mounted-volume', 1, 1,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
);
CREATE TABLE dispatches (
    dispatch_id TEXT NOT NULL PRIMARY KEY,
    mount_point TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    marker_identity TEXT NOT NULL,
    route TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'retry', 'uncertain', 'completed', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    observed_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    next_retry_at REAL,
    exit_code INTEGER,
    error TEXT
);
CREATE INDEX dispatches_state_idx ON dispatches(state, next_retry_at, observed_at, dispatch_id);
INSERT INTO dispatches (
    dispatch_id, mount_point, generation, marker_identity, route, plan_json,
    state, attempts, observed_at, started_at
) VALUES (
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    '/fixture/mounted-volume', 1,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'camera', '{"format":"gogurt-action-plan/v1"}', 'running', 1, 1.0, 2.0
);
COMMIT;
