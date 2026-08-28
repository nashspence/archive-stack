from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from alembic import command, context
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import URL, Connection, Engine

from state_schema.sqlalchemy_schema import assert_schema_matches_metadata

StateCondition = Literal[
    "empty",
    "current",
    "upgrade_required",
    "unversioned",
    "incompatible",
]
SchemaVerify = Callable[[Connection], None]
EngineFactory = Callable[[], Engine]
EmptyStateCheck = Callable[[], bool]
StateConnection = Connection
StateEngine = Engine


class StateSchemaError(RuntimeError):
    """Raised when durable state cannot be safely opened or upgraded."""


@dataclass(frozen=True, slots=True)
class StateStatus:
    name: str
    condition: StateCondition
    current_revision: str | None
    head_revision: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "condition": self.condition,
            "current_revision": self.current_revision,
            "head_revision": self.head_revision,
        }


class StateSchema:
    """One forward-only migration history for one physical database."""

    def __init__(
        self,
        *,
        name: str,
        engine_factory: EngineFactory,
        script_location: Path,
        verify: SchemaVerify,
        is_empty: EmptyStateCheck | None = None,
        version_table: str = "state_schema_revision",
    ) -> None:
        self.name = name
        self._engine_factory = engine_factory
        self.script_location = Path(script_location)
        self._verify = verify
        self._is_empty = is_empty
        self.version_table = version_table

    def _config(self) -> Config:
        config = Config()
        config.set_main_option("script_location", str(self.script_location))
        config.set_main_option("state_schema_version_table", self.version_table)
        return config

    def _head(self, script: ScriptDirectory) -> str:
        revisions = tuple(script.walk_revisions())
        heads = tuple(script.get_heads())
        roots = tuple(revision for revision in revisions if revision.down_revision is None)
        branched = tuple(
            revision.revision
            for revision in revisions
            if isinstance(revision.down_revision, tuple) or len(revision.nextrev) > 1
        )
        if len(heads) != 1:
            raise StateSchemaError(
                f"{self.name} state requires exactly one migration head, found {len(heads)}"
            )
        if len(roots) != 1 or branched:
            raise StateSchemaError(
                f"{self.name} state requires one linear migration history; "
                f"roots={len(roots)} branches={','.join(branched) or 'none'}"
            )
        return heads[0]

    def status(self) -> StateStatus:
        config = self._config()
        script = ScriptDirectory.from_config(config)
        head = self._head(script)
        if self._is_empty is not None and self._is_empty():
            return StateStatus(self.name, "empty", None, head)
        engine = self._engine_factory()
        try:
            with engine.connect() as connection:
                tables = set(inspect(connection).get_table_names())
                current_heads = tuple(
                    MigrationContext.configure(
                        connection,
                        opts={"version_table": self.version_table},
                    ).get_current_heads()
                )
        finally:
            engine.dispose()

        if not tables:
            return StateStatus(self.name, "empty", None, head)
        if not current_heads:
            return StateStatus(self.name, "unversioned", None, head)
        if len(current_heads) != 1:
            return StateStatus(self.name, "incompatible", ",".join(current_heads), head)
        current = current_heads[0]
        if current == head:
            return StateStatus(self.name, "current", current, head)
        try:
            revision = script.get_revision(current)
        except ResolutionError:
            return StateStatus(self.name, "incompatible", current, head)
        if revision is None:
            return StateStatus(self.name, "incompatible", current, head)
        return StateStatus(self.name, "upgrade_required", current, head)

    def validate(self) -> StateStatus:
        status = self.status()
        if status.condition != "current":
            raise StateSchemaError(self._actionable_message(status))
        engine = self._engine_factory()
        try:
            with engine.connect() as connection:
                self._verify_database(connection)
        finally:
            engine.dispose()
        return status

    def upgrade(self) -> StateStatus:
        before = self.status()
        if before.condition in {"unversioned", "incompatible"}:
            raise StateSchemaError(self._actionable_message(before))

        config = self._config()
        engine = self._engine_factory()
        try:
            with engine.begin() as connection:
                config.attributes["connection"] = connection
                if before.condition in {"empty", "upgrade_required"}:
                    command.upgrade(config, "head")
                self._verify_database(connection)
        finally:
            engine.dispose()
        return self.validate()

    def _verify_database(self, connection: Connection) -> None:
        try:
            inspector = inspect(connection)
            version_columns = tuple(
                str(column["name"]) for column in inspector.get_columns(self.version_table)
            )
            version_primary_key = tuple(
                str(column)
                for column in inspector.get_pk_constraint(self.version_table)["constrained_columns"]
            )
            if version_columns != ("version_num",) or version_primary_key != ("version_num",):
                raise StateSchemaError(
                    f"{self.name} revision table does not match the state-schema contract"
                )
            if connection.dialect.name == "sqlite":
                quick_check = [
                    str(row[0]) for row in connection.exec_driver_sql("PRAGMA quick_check")
                ]
                if quick_check != ["ok"]:
                    raise StateSchemaError(
                        f"{self.name} state failed SQLite quick_check: {quick_check}"
                    )
                foreign_key_errors = list(connection.exec_driver_sql("PRAGMA foreign_key_check"))
                if foreign_key_errors:
                    raise StateSchemaError(
                        f"{self.name} state failed SQLite foreign_key_check: {foreign_key_errors}"
                    )
            self._verify(connection)
        except StateSchemaError:
            raise
        except Exception as exc:
            raise StateSchemaError(f"{self.name} state verification failed: {exc}") from exc

    def _actionable_message(self, status: StateStatus) -> str:
        if status.condition == "empty":
            return f"{self.name} state is empty; run its state upgrade command"
        if status.condition == "upgrade_required":
            return (
                f"{self.name} state revision {status.current_revision} requires an explicit "
                f"upgrade to {status.head_revision}"
            )
        if status.condition == "unversioned":
            return (
                f"{self.name} state is unversioned and is not a supported v1 database; "
                "restore or explicitly replace it"
            )
        return (
            f"{self.name} state revision {status.current_revision} is incompatible with "
            f"application head {status.head_revision}"
        )


def run_migration_environment() -> None:
    """Run an application's packaged Alembic revisions on its supplied connection."""

    config = context.config
    connection = config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise StateSchemaError("state migrations require a programmatically supplied connection")
    context.configure(
        connection=connection,
        version_table=config.get_main_option("state_schema_version_table"),
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def sqlite_engine(path: Path) -> Engine:
    """Create the repository-standard engine for one application-owned SQLite file."""

    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(Path(path))),
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: object) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin_sqlite_transaction(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    return engine


__all__ = [
    "StateCondition",
    "StateConnection",
    "StateEngine",
    "StateSchema",
    "StateSchemaError",
    "StateStatus",
    "assert_schema_matches_metadata",
    "run_migration_environment",
    "sqlite_engine",
]
