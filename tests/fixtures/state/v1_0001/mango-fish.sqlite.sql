-- Immutable Mango Fish v1.0 database fixture. Add later baselines; do not edit.
BEGIN TRANSACTION;
CREATE TABLE source_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT NOT NULL
);
INSERT INTO source_cursors VALUES('stove0', '23');
CREATE TABLE state_schema_revision (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT state_schema_revision_pkc PRIMARY KEY (version_num)
);
INSERT INTO state_schema_revision VALUES('v1_0001');
COMMIT;
