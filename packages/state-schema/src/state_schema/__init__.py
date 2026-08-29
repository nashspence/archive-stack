from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator as CollectionsIterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from alembic import command, context
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import CheckConstraint, MetaData, String, create_engine, event, inspect, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

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
SessionFactory = sessionmaker[Session]


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
        prerequisite: SchemaVerify | None = None,
        is_empty: EmptyStateCheck | None = None,
        version_table: str = "state_schema_revision",
    ) -> None:
        self.name = name
        self._engine_factory = engine_factory
        self.script_location = Path(script_location)
        self._verify = verify
        self._prerequisite = prerequisite
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
        engine = self._engine_factory()
        try:
            with engine.begin() as connection:
                status = self.upgrade_connection(connection)
        finally:
            engine.dispose()
        return status

    def upgrade_connection(self, connection: Connection) -> StateStatus:
        """Upgrade and verify state through one caller-owned connection."""

        config = self._config()
        script = ScriptDirectory.from_config(config)
        head = self._head(script)
        before = self._status_connection(connection, script=script, head=head)
        if before.condition in {"unversioned", "incompatible"}:
            raise StateSchemaError(self._actionable_message(before))
        if self._prerequisite is not None:
            self._prerequisite(connection)
        config.attributes["connection"] = connection
        if before.condition in {"empty", "upgrade_required"}:
            command.upgrade(config, "head")
        self._verify_database(connection)
        after = self._status_connection(connection, script=script, head=head)
        if after.condition != "current":
            raise StateSchemaError(self._actionable_message(after))
        return after

    def _status_connection(
        self,
        connection: Connection,
        *,
        script: ScriptDirectory,
        head: str,
    ) -> StateStatus:
        tables = set(inspect(connection).get_table_names())
        current_heads = tuple(
            MigrationContext.configure(
                connection,
                opts={"version_table": self.version_table},
            ).get_current_heads()
        )
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

    def _verify_database(self, connection: Connection) -> None:
        try:
            if self._prerequisite is not None:
                self._prerequisite(connection)
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


def require_postgresql_extension(
    connection: Connection,
    *,
    name: str,
    schema: str,
    accepted_versions: tuple[str, ...] = (),
    operator_classes: tuple[str, ...] = (),
) -> None:
    """Require a deployment-installed PostgreSQL extension capability."""

    if connection.dialect.name != "postgresql":
        return
    installed = connection.execute(
        text(
            "SELECT n.nspname, e.extversion FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = :name"
        ),
        {"name": name},
    ).one_or_none()
    if installed is None or installed[0] != schema:
        actual = "absent" if installed is None else f"schema {installed[0]}"
        raise StateSchemaError(
            f"PostgreSQL extension {name} must be installed in schema {schema}; found {actual}"
        )
    version = str(installed[1])
    if accepted_versions and version not in accepted_versions:
        raise StateSchemaError(
            f"PostgreSQL extension {name} version must be one of "
            f"{', '.join(accepted_versions)}; found {version}"
        )
    for operator_class in operator_classes:
        present = connection.execute(
            text(
                "SELECT 1 FROM pg_opclass o "
                "JOIN pg_namespace n ON n.oid = o.opcnamespace "
                "WHERE o.opcname = :operator_class AND n.nspname = :schema"
            ),
            {"operator_class": operator_class, "schema": schema},
        ).scalar_one_or_none()
        if present != 1:
            raise StateSchemaError(
                f"PostgreSQL extension {name} is missing operator class {schema}.{operator_class}"
            )


def attach_sha256_string_constraints(metadata: MetaData) -> None:
    """Reserve ``String(64)`` columns for exact lowercase SHA-256 identities."""

    for table in metadata.tables.values():
        existing = {constraint.name for constraint in table.constraints}
        for column in table.columns:
            if not isinstance(column.type, String) or column.type.length != 64:
                continue
            name = f"ck_{table.name}_{column.name}_hex"
            if len(name) > 60:
                import hashlib  # noqa: PLC0415

                name = f"ck_sha256_{hashlib.sha256(name.encode()).hexdigest()[:16]}"
            if name in existing:
                continue
            identifier = column.name
            remainder = identifier
            for character in "0123456789abcdef":
                remainder = f"replace({remainder}, '{character}', '')"
            exact = (
                f"length({identifier}) = 64 AND lower({identifier}) = {identifier} "
                f"AND {remainder} = ''"
            )
            # ``AND`` binds more tightly than ``OR`` in every supported SQL
            # dialect. Avoid a redundant grouping pair that PostgreSQL removes
            # while deparsing, so migration DDL and runtime metadata have one
            # exact semantic fingerprint.
            expression = exact if not column.nullable else f"{identifier} IS NULL OR {exact}"
            table.append_constraint(CheckConstraint(expression, name=name))
            existing.add(name)


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


@contextmanager
def read_snapshot(session_factory: SessionFactory) -> CollectionsIterator[Session]:
    """Own one read-only, repeatable snapshot for a bounded-memory enumeration."""

    session = session_factory()
    try:
        bind = session.get_bind()
        execution_options = (
            {"isolation_level": "REPEATABLE READ"} if bind.dialect.name == "postgresql" else {}
        )
        connection = session.connection(execution_options=execution_options)
        if bind.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        yield session
    finally:
        # ``Session.close()`` rolls the read transaction back while preserving
        # already-loaded scalar values on objects yielded to response builders.
        # An explicit rollback expires them first and makes otherwise complete
        # page rows attempt detached lazy reloads.
        session.close()


__all__ = [
    "StateCondition",
    "StateConnection",
    "StateEngine",
    "StateSchema",
    "StateSchemaError",
    "StateStatus",
    "attach_sha256_string_constraints",
    "assert_schema_matches_metadata",
    "read_snapshot",
    "require_postgresql_extension",
    "run_migration_environment",
    "sqlite_engine",
]
