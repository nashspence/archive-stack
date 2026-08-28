from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from state_schema import StateConnection, StateSchema, sqlite_engine


def _write_migrations(path: Path, *, include_second: bool = False) -> None:
    versions = path / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (path / "env.py").write_text(
        "from state_schema import run_migration_environment\nrun_migration_environment()\n",
        encoding="utf-8",
    )
    (versions / "v1_0001.py").write_text(
        "from alembic import op\n"
        'revision = "v1_0001"\n'
        "down_revision = None\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.get_bind().exec_driver_sql("
        '"CREATE TABLE records (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"'
        ")\n"
        "def downgrade():\n"
        "    raise RuntimeError('forward-only')\n",
        encoding="utf-8",
    )
    if include_second:
        (versions / "v1_0002.py").write_text(
            "import os\n"
            "from alembic import op\n"
            "import sqlalchemy as sa\n"
            'revision = "v1_0002"\n'
            'down_revision = "v1_0001"\n'
            "branch_labels = None\n"
            "depends_on = None\n"
            "def upgrade():\n"
            "    op.add_column('records', sa.Column('label', sa.Text(), nullable=True))\n"
            "    if os.environ.get('STATE_SCHEMA_TEST_INTERRUPT'):\n"
            "        raise RuntimeError('simulated interruption')\n"
            "def downgrade():\n"
            "    raise RuntimeError('forward-only')\n",
            encoding="utf-8",
        )


def _schema(database: Path, migrations: Path) -> StateSchema:
    def verify(connection: StateConnection) -> None:
        assert "records" in inspect(connection).get_table_names()

    return StateSchema(
        name="test",
        engine_factory=lambda: sqlite_engine(database),
        script_location=migrations,
        verify=verify,
        is_empty=lambda: not database.exists() or database.stat().st_size == 0,
    )


def test_explicit_bootstrap_produces_current_verified_state(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    _write_migrations(migrations)
    schema = _schema(tmp_path / "state.sqlite3", migrations)

    assert schema.status().as_dict() == {
        "name": "test",
        "condition": "empty",
        "current_revision": None,
        "head_revision": "v1_0001",
    }

    upgraded = schema.upgrade()

    assert upgraded.condition == "current"
    assert upgraded.current_revision == "v1_0001"
    assert schema.validate() == upgraded


def test_forward_upgrade_preserves_state_and_reaches_the_single_head(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    _write_migrations(migrations)
    database = tmp_path / "state.sqlite3"
    schema = _schema(database, migrations)
    schema.upgrade()
    engine = sqlite_engine(database)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO records (id, payload) VALUES (1, 'kept')")
    engine.dispose()

    _write_migrations(migrations, include_second=True)
    assert schema.status().condition == "upgrade_required"

    upgraded = schema.upgrade()
    engine = sqlite_engine(database)
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT id, payload, label FROM records").one()
    engine.dispose()

    assert upgraded.condition == "current"
    assert upgraded.current_revision == upgraded.head_revision == "v1_0002"
    assert tuple(row) == (1, "kept", None)


def test_interrupted_upgrade_rolls_back_and_can_be_reentered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = tmp_path / "migrations"
    _write_migrations(migrations)
    database = tmp_path / "state.sqlite3"
    schema = _schema(database, migrations)
    schema.upgrade()
    _write_migrations(migrations, include_second=True)

    monkeypatch.setenv("STATE_SCHEMA_TEST_INTERRUPT", "1")
    with pytest.raises(RuntimeError, match="simulated interruption"):
        schema.upgrade()
    monkeypatch.delenv("STATE_SCHEMA_TEST_INTERRUPT")

    engine = sqlite_engine(database)
    with engine.connect() as connection:
        columns_after_interruption = {
            column["name"] for column in inspect(connection).get_columns("records")
        }
    engine.dispose()
    assert schema.status().current_revision == "v1_0001"
    assert columns_after_interruption == {"id", "payload"}

    assert schema.upgrade().current_revision == "v1_0002"
