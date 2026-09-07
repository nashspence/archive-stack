from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError
from state_schema import (
    StateSchema,
    assert_schema_matches_metadata,
    attach_sha256_string_constraints,
)
from state_schema.sqlalchemy_schema import _check_expression


def _metadata(*, state_default: str, check: str, index: bool = True) -> MetaData:
    metadata = MetaData()
    Table(
        "records",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "state",
            String(16),
            nullable=False,
            server_default=text(f"'{state_default}'"),
            index=index,
        ),
        CheckConstraint(check, name="ck_records_state"),
    )
    return metadata


def _database(metadata: MetaData):  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE state_schema_revision (version_num VARCHAR(32) PRIMARY KEY)"
        )
    return engine


def test_complete_schema_verifier_accepts_exact_metadata() -> None:
    metadata = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    engine = _database(metadata)

    assert_schema_matches_metadata(
        engine,
        metadata,
        version_table="state_schema_revision",
    )


def test_sha256_string_convention_accepts_exact_lowercase_hex() -> None:
    metadata = MetaData()
    records = Table(
        "digest_records",
        metadata,
        Column("identity", String(64), primary_key=True),
    )
    attach_sha256_string_constraints(metadata)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(records.insert().values(identity="a" * 64))
        with pytest.raises(IntegrityError):
            connection.execute(records.insert().values(identity="G" * 64))


def test_complete_schema_verifier_rejects_changed_server_default() -> None:
    actual = _metadata(state_default="queued", check="state IN ('ready', 'done')")
    expected = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    engine = _database(actual)

    with pytest.raises(RuntimeError, match="default"):
        assert_schema_matches_metadata(
            engine,
            expected,
            version_table="state_schema_revision",
        )


def test_complete_schema_verifier_rejects_changed_check_expression() -> None:
    actual = _metadata(state_default="ready", check="state IN ('ready', 'failed')")
    expected = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    engine = _database(actual)

    with pytest.raises(RuntimeError, match="checks"):
        assert_schema_matches_metadata(
            engine,
            expected,
            version_table="state_schema_revision",
        )


def test_check_expression_normalizes_only_cast_postgres_integer_literals() -> None:
    assert _check_expression("revision <= '9007199254740991'::bigint") == (
        "revision <=9007199254740991"
    )
    assert _check_expression("revision_label = '9007199254740991'::text") == (
        "revision_label='9007199254740991'"
    )
    assert _check_expression("state <> 'published'") == "state !='published'"
    assert _check_expression("revision = (previous_revision + 1)") == (
        "revision=previous_revision + 1"
    )


def test_complete_schema_verifier_rejects_missing_index() -> None:
    actual = _metadata(
        state_default="ready",
        check="state IN ('ready', 'done')",
        index=False,
    )
    expected = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    engine = _database(actual)

    with pytest.raises(RuntimeError, match="indexes"):
        assert_schema_matches_metadata(
            engine,
            expected,
            version_table="state_schema_revision",
        )


def test_complete_schema_verifier_rejects_changed_computed_expression() -> None:
    actual = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    expected = _metadata(state_default="ready", check="state IN ('ready', 'done')")
    actual.tables["records"].append_column(Column("projection", String, Computed("lower(state)")))
    expected.tables["records"].append_column(Column("projection", String, Computed("upper(state)")))
    engine = _database(actual)

    with pytest.raises(RuntimeError, match="computed expression"):
        assert_schema_matches_metadata(
            engine,
            expected,
            version_table="state_schema_revision",
        )


def test_complete_schema_verifier_rejects_changed_index_semantics() -> None:
    actual = _metadata(state_default="ready", check="state IN ('ready', 'done')", index=False)
    expected = _metadata(state_default="ready", check="state IN ('ready', 'done')", index=False)
    Index("ix_records_state_order", actual.tables["records"].c.state.asc())
    Index("ix_records_state_order", expected.tables["records"].c.state.desc())
    engine = _database(actual)

    with pytest.raises(RuntimeError, match="index definitions"):
        assert_schema_matches_metadata(
            engine,
            expected,
            version_table="state_schema_revision",
        )


def test_migration_owned_baseline_converges_fresh_and_upgraded_state(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    migrations = tmp_path / "migrations"
    versions = migrations / "versions"
    versions.mkdir(parents=True)
    (migrations / "env.py").write_text(
        "from state_schema import run_migration_environment\nrun_migration_environment()\n",
        encoding="utf-8",
    )
    (versions / "v1_0001.py").write_text(
        "from alembic import op\n"
        "revision = 'v1_0001'\n"
        "down_revision = None\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE records (id INTEGER NOT NULL PRIMARY KEY)')\n"
        "def downgrade():\n"
        "    raise RuntimeError('forward-only')\n",
        encoding="utf-8",
    )

    baseline_metadata = MetaData()
    Table("records", baseline_metadata, Column("id", Integer, primary_key=True))
    upgraded_metadata = MetaData()
    Table(
        "records",
        upgraded_metadata,
        Column("id", Integer, primary_key=True),
        Column("note", String, nullable=False, server_default=text("''")),
    )

    def schema(database, metadata):  # type: ignore[no-untyped-def]
        return StateSchema(
            name="fixture",
            engine_factory=lambda: create_engine(f"sqlite+pysqlite:///{database}"),
            script_location=migrations,
            verify=lambda connection: assert_schema_matches_metadata(
                connection,
                metadata,
                version_table="state_schema_revision",
            ),
        )

    existing = tmp_path / "existing.sqlite3"
    assert schema(existing, baseline_metadata).upgrade().current_revision == "v1_0001"

    (versions / "v1_0002.py").write_text(
        "from alembic import op\n"
        "revision = 'v1_0002'\n"
        "down_revision = 'v1_0001'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.execute(\"ALTER TABLE records ADD COLUMN note VARCHAR DEFAULT '' NOT NULL\")\n"
        "def downgrade():\n"
        "    raise RuntimeError('forward-only')\n",
        encoding="utf-8",
    )
    fresh = tmp_path / "fresh.sqlite3"
    assert schema(existing, upgraded_metadata).upgrade().current_revision == "v1_0002"
    assert schema(fresh, upgraded_metadata).upgrade().current_revision == "v1_0002"

    def fingerprint(database):  # type: ignore[no-untyped-def]
        engine = create_engine(f"sqlite+pysqlite:///{database}")
        try:
            with engine.connect() as connection:
                return tuple(
                    connection.exec_driver_sql(
                        "SELECT type, name, sql FROM sqlite_schema "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                    )
                )
        finally:
            engine.dispose()

    assert fingerprint(existing) == fingerprint(fresh)
