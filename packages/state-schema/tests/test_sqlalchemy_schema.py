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
from state_schema import assert_schema_matches_metadata


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
