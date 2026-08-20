"""One SQLAlchemy persistence authority for stove0 control-plane state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

from lifecycle_events import CloudEvent, EventPage, cloud_event
from sqlalchemy import (
    Integer,
    String,
    Text,
    asc,
    create_engine,
    desc,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from state_schema import StateSchema
from time_formats import utc_timestamp_now

from stove0_core.evaluation import (
    ConcurrentEvaluationUpdate,
    EvaluationRecord,
)
from stove0_core.work_state import ConcurrentWorkUpdate, WorkRecord

SortOrder = Literal["asc", "desc"]
WorkSort = Literal["updated_at", "phase", "work_id"]
EvaluationSort = Literal["updated_at", "phase", "evaluation_id"]
STATE_VERSION_TABLE = "stove0_state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")


class _Base(DeclarativeBase):
    pass


class _WorkRow(_Base):
    __tablename__ = "stove0_work_records"

    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)


class _EvaluationRow(_Base):
    __tablename__ = "stove0_evaluation_records"

    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)


class _CursorRow(_Base):
    __tablename__ = "stove0_event_cursors"

    stream: Mapped[str] = mapped_column(String(160), primary_key=True)
    cursor: Mapped[str] = mapped_column(String(500), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class _EventRow(_Base):
    __tablename__ = "stove0_lifecycle_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)


def create_state_engine(database_url: str) -> Engine:
    _prepare_sqlite_parent(database_url)
    return create_engine(database_url, pool_pre_ping=True)


def _type_name(value: object, bind: Connection) -> str:
    return " ".join(str(value.compile(dialect=bind.dialect)).upper().split())  # type: ignore[attr-defined]


def _assert_schema_matches_models(bind: Connection) -> None:
    inspector = inspect(bind)
    expected = {table.name: table for table in _Base.metadata.sorted_tables}
    actual_tables = set(inspector.get_table_names()) - {STATE_VERSION_TABLE}
    differences = [f"unexpected table {name}" for name in sorted(actual_tables - set(expected))]
    differences.extend(f"missing table {name}" for name in sorted(set(expected) - actual_tables))
    for table_name in sorted(actual_tables & set(expected)):
        table = expected[table_name]
        expected_columns = {column.name: column for column in table.columns}
        actual_columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        differences.extend(
            f"unexpected column {table_name}.{name}"
            for name in sorted(set(actual_columns) - set(expected_columns))
        )
        differences.extend(
            f"missing column {table_name}.{name}"
            for name in sorted(set(expected_columns) - set(actual_columns))
        )
        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected_column = expected_columns[column_name]
            actual_column = actual_columns[column_name]
            actual_type = _type_name(actual_column["type"], bind)
            expected_type = _type_name(expected_column.type, bind)
            if actual_type != expected_type:
                differences.append(
                    f"column {table_name}.{column_name} has type {actual_type}, "
                    f"expected {expected_type}"
                )
            if bool(actual_column["nullable"]) != bool(expected_column.nullable):
                differences.append(
                    f"column {table_name}.{column_name} has nullable="
                    f"{bool(actual_column['nullable'])}, expected {bool(expected_column.nullable)}"
                )
        expected_pk = tuple(str(column.name) for column in table.primary_key.columns)
        actual_pk = tuple(
            str(name)
            for name in (inspector.get_pk_constraint(table_name)["constrained_columns"] or ())
        )
        if actual_pk != expected_pk:
            differences.append(
                f"table {table_name} has primary key {actual_pk}, expected {expected_pk}"
            )
        expected_indexes = {
            str(index.name): (
                tuple(str(column.name) for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        actual_indexes = {
            str(index["name"]): (
                tuple(str(name) for name in index["column_names"]),
                bool(index["unique"]),
            )
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        }
        if actual_indexes != expected_indexes:
            differences.append(
                f"table {table_name} indexes are {actual_indexes}, expected {expected_indexes}"
            )
    if differences:
        raise RuntimeError("stove0 schema does not match current models: " + "; ".join(differences))


def stove0_state_schema(database_url: str) -> StateSchema:
    return StateSchema(
        name="stove0 control",
        engine_factory=lambda: create_state_engine(database_url),
        script_location=STATE_MIGRATIONS,
        bootstrap=lambda connection: _Base.metadata.create_all(connection),
        verify=_assert_schema_matches_models,
        version_table=STATE_VERSION_TABLE,
    )


class SqlAlchemyStateStore:
    """Durable CAS store shared by controller and worker processes.

    Only canonical control documents are retained. Capability bearer tokens,
    payload bytes, archive credentials, and target workspaces are prohibited.
    PostgreSQL is the deployment database; SQLite remains useful only as the
    same implementation's disposable unit-test backend.
    """

    def __init__(
        self,
        database_url: str,
        *,
        engine: Engine | None = None,
        initialize: bool = True,
    ) -> None:
        self.engine = engine or create_state_engine(database_url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if initialize:
            _Base.metadata.create_all(self.engine)

    def load(self, work_id: str) -> WorkRecord | None:
        with self.sessions() as session:
            row = session.get(_WorkRow, work_id)
            return None if row is None else WorkRecord.model_validate_json(row.document_json)

    def create(self, record: WorkRecord) -> WorkRecord:
        encoded = _encode(record.model_dump(mode="json", by_alias=True, exclude_none=True))
        now = utc_timestamp_now()
        with self.sessions() as session, session.begin():
            inserted = _insert_work(session, record, encoded=encoded, now=now)
            if inserted:
                _emit(
                    session,
                    type="work.created",
                    subject=record.work_id,
                    data={"work_id": record.work_id, "phase": record.phase},
                )
                return record
            row = session.scalar(
                select(_WorkRow).where(_WorkRow.work_id == record.work_id).with_for_update()
            )
            if row is None:
                raise RuntimeError("stove0 work record disappeared during creation")
            existing = WorkRecord.model_validate_json(row.document_json)
            if existing.work != record.work:
                raise ConcurrentWorkUpdate("work identity was reused with another payload")
            return existing

    def compare_and_swap(
        self,
        work_id: str,
        *,
        expected_revision: int,
        replacement: WorkRecord,
    ) -> WorkRecord:
        if replacement.work_id != work_id or replacement.revision != expected_revision + 1:
            raise ValueError("replacement work record has an invalid identity or revision")
        encoded = _encode(replacement.model_dump(mode="json", by_alias=True, exclude_none=True))
        with self.sessions() as session, session.begin():
            changed = cast(
                CursorResult[object],
                session.execute(
                    update(_WorkRow)
                    .where(_WorkRow.work_id == work_id, _WorkRow.revision == expected_revision)
                    .values(
                        revision=replacement.revision,
                        phase=replacement.phase,
                        updated_at=utc_timestamp_now(),
                        document_json=encoded,
                    )
                ),
            ).rowcount
            if changed != 1:
                current = session.get(_WorkRow, work_id)
                if current is None:
                    raise KeyError(work_id)
                raise ConcurrentWorkUpdate(
                    f"stale stove0 work revision: {expected_revision} != {current.revision}"
                )
            _emit(
                session,
                type="work.updated",
                subject=work_id,
                data={
                    "work_id": work_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                },
            )
        return replacement

    def list_work(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: str | None = None,
        query: str | None = None,
        sort: WorkSort = "updated_at",
        order: SortOrder = "desc",
        all_items: bool = False,
    ) -> dict[str, object]:
        _pagination(page, per_page)
        columns = {
            "updated_at": _WorkRow.updated_at,
            "phase": _WorkRow.phase,
            "work_id": _WorkRow.work_id,
        }
        filters = []
        if phase:
            filters.append(_WorkRow.phase == phase)
        if query:
            filters.append(_WorkRow.work_id.contains(query.strip().casefold()))
        statement = select(_WorkRow).where(*filters)
        statement = statement.order_by(
            (desc if order == "desc" else asc)(columns[sort]),
            asc(_WorkRow.work_id),
        )
        with self.sessions() as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(_WorkRow.work_id).where(*filters).subquery()
                    )
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            records = [
                WorkRecord.model_validate_json(row.document_json)
                for row in session.scalars(statement)
            ]
        return _page(
            records,
            item_key="work",
            page=page,
            per_page=per_page,
            total=total,
            sort=sort,
            order=order,
            all_items=all_items,
            filters={"phase": phase, "query": query},
        )

    def load_evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        with self.sessions() as session:
            row = session.get(_EvaluationRow, evaluation_id)
            return None if row is None else EvaluationRecord.model_validate_json(row.document_json)

    def list_evaluations(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        phase: str | None = None,
        query: str | None = None,
        sort: EvaluationSort = "updated_at",
        order: SortOrder = "desc",
        all_items: bool = False,
    ) -> dict[str, object]:
        _pagination(page, per_page)
        columns = {
            "updated_at": _EvaluationRow.updated_at,
            "phase": _EvaluationRow.phase,
            "evaluation_id": _EvaluationRow.evaluation_id,
        }
        filters = []
        if phase:
            filters.append(_EvaluationRow.phase == phase)
        if query:
            filters.append(_EvaluationRow.evaluation_id.contains(query.strip().casefold()))
        statement = (
            select(_EvaluationRow)
            .where(*filters)
            .order_by(
                (desc if order == "desc" else asc)(columns[sort]),
                asc(_EvaluationRow.evaluation_id),
            )
        )
        with self.sessions() as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(_EvaluationRow.evaluation_id).where(*filters).subquery()
                    )
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            records = [
                EvaluationRecord.model_validate_json(row.document_json)
                for row in session.scalars(statement)
            ]
        return _model_page(
            records,
            item_key="evaluations",
            page=page,
            per_page=per_page,
            total=total,
            sort=sort,
            order=order,
            all_items=all_items,
            filters={"phase": phase, "query": query},
        )

    def create_evaluation(self, record: EvaluationRecord) -> EvaluationRecord:
        encoded = _encode(record.model_dump(mode="json", by_alias=True, exclude_none=True))
        now = utc_timestamp_now()
        with self.sessions() as session, session.begin():
            inserted = _insert_evaluation(session, record, encoded=encoded, now=now)
            if inserted:
                _emit(
                    session,
                    type="evaluation.created",
                    subject=record.evaluation_id,
                    data={
                        "evaluation_id": record.evaluation_id,
                        "phase": record.phase,
                    },
                )
                return record
            row = session.scalar(
                select(_EvaluationRow)
                .where(_EvaluationRow.evaluation_id == record.evaluation_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("stove0 evaluation disappeared during creation")
            existing = EvaluationRecord.model_validate_json(row.document_json)
            if existing.definition != record.definition:
                raise ConcurrentEvaluationUpdate(
                    "evaluation identity was reused with another definition"
                )
            return existing

    def compare_and_swap_evaluation(
        self,
        evaluation_id: str,
        *,
        expected_revision: int,
        replacement: EvaluationRecord,
    ) -> EvaluationRecord:
        if (
            replacement.evaluation_id != evaluation_id
            or replacement.revision != expected_revision + 1
        ):
            raise ValueError("replacement evaluation has an invalid identity or revision")
        encoded = _encode(replacement.model_dump(mode="json", by_alias=True, exclude_none=True))
        with self.sessions() as session, session.begin():
            changed = cast(
                CursorResult[object],
                session.execute(
                    update(_EvaluationRow)
                    .where(
                        _EvaluationRow.evaluation_id == evaluation_id,
                        _EvaluationRow.revision == expected_revision,
                    )
                    .values(
                        revision=replacement.revision,
                        phase=replacement.phase,
                        updated_at=utc_timestamp_now(),
                        document_json=encoded,
                    )
                ),
            ).rowcount
            if changed != 1:
                current = session.get(_EvaluationRow, evaluation_id)
                if current is None:
                    raise KeyError(evaluation_id)
                raise ConcurrentEvaluationUpdate(
                    f"stale evaluation revision: {expected_revision} != {current.revision}"
                )
            _emit(
                session,
                type="evaluation.updated",
                subject=evaluation_id,
                data={
                    "evaluation_id": evaluation_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                },
            )
        return replacement

    def list_events(self, *, after: str | None = None, limit: int = 100) -> EventPage:
        try:
            cursor = int((after or "0").strip())
        except ValueError as exc:
            raise ValueError("after must be a non-negative event cursor") from exc
        if cursor < 0 or limit < 1 or limit > 100:
            raise ValueError("stove0 event pagination is invalid")
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(_EventRow)
                    .where(_EventRow.sequence > cursor)
                    .order_by(_EventRow.sequence)
                    .limit(limit + 1)
                )
            )
        selected = rows[:limit]
        return EventPage(
            events=[CloudEvent.model_validate_json(row.event_json) for row in selected],
            next_cursor=str(selected[-1].sequence if selected else cursor),
            has_more=len(rows) > limit,
        )

    # EvaluationStore aliases keep one physical authority without making the
    # work and evaluation identities share an accidental method namespace.
    def evaluation_store(self) -> _EvaluationStoreView:
        return _EvaluationStoreView(self)

    def load_cursor(self, stream: str) -> tuple[str, int] | None:
        with self.sessions() as session:
            row = session.get(_CursorRow, stream)
            return None if row is None else (row.cursor, row.revision)

    def compare_and_swap_cursor(
        self,
        stream: str,
        *,
        expected_revision: int | None,
        cursor: str,
    ) -> tuple[str, int]:
        now = utc_timestamp_now()
        with self.sessions() as session, session.begin():
            if expected_revision is None:
                try:
                    with session.begin_nested():
                        session.add(
                            _CursorRow(
                                stream=stream,
                                cursor=cursor,
                                revision=1,
                                updated_at=now,
                            )
                        )
                        session.flush()
                    return cursor, 1
                except IntegrityError:
                    pass
                raise ConcurrentWorkUpdate("stove0 event cursor was created concurrently")
            changed = cast(
                CursorResult[object],
                session.execute(
                    update(_CursorRow)
                    .where(_CursorRow.stream == stream, _CursorRow.revision == expected_revision)
                    .values(cursor=cursor, revision=expected_revision + 1, updated_at=now)
                ),
            ).rowcount
            if changed != 1:
                raise ConcurrentWorkUpdate("stove0 event cursor revision is stale")
        return cursor, expected_revision + 1


class _EvaluationStoreView:
    def __init__(self, owner: SqlAlchemyStateStore) -> None:
        self.owner = owner

    def load(self, evaluation_id: str) -> EvaluationRecord | None:
        return self.owner.load_evaluation(evaluation_id)

    def create(self, record: EvaluationRecord) -> EvaluationRecord:
        return self.owner.create_evaluation(record)

    def compare_and_swap(
        self,
        evaluation_id: str,
        *,
        expected_revision: int,
        replacement: EvaluationRecord,
    ) -> EvaluationRecord:
        return self.owner.compare_and_swap_evaluation(
            evaluation_id,
            expected_revision=expected_revision,
            replacement=replacement,
        )


def _prepare_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or url.database in {None, "", ":memory:"}:
        return
    parent = Path(url.database).expanduser().resolve().parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def _insert_work(session: Session, record: WorkRecord, *, encoded: str, now: str) -> bool:
    try:
        with session.begin_nested():
            session.add(
                _WorkRow(
                    work_id=record.work_id,
                    revision=record.revision,
                    phase=record.phase,
                    updated_at=now,
                    document_json=encoded,
                )
            )
            session.flush()
        return True
    except IntegrityError:
        return False


def _insert_evaluation(
    session: Session,
    record: EvaluationRecord,
    *,
    encoded: str,
    now: str,
) -> bool:
    try:
        with session.begin_nested():
            session.add(
                _EvaluationRow(
                    evaluation_id=record.evaluation_id,
                    revision=record.revision,
                    phase=record.phase,
                    updated_at=now,
                    document_json=encoded,
                )
            )
            session.flush()
        return True
    except IntegrityError:
        return False


def _emit(
    session: Session,
    *,
    type: str,
    subject: str,
    data: dict[str, object],
) -> None:
    event = cloud_event(
        source="urn:riverhog:stove0",
        type=f"io.riverhog.stove0.{type}",
        subject=subject,
        data=data,
    )
    session.add(_EventRow(event_json=event.model_dump_json(exclude_none=True)))


def _encode(payload: object) -> str:
    if _contains_secret_field(payload):
        raise ValueError("stove0 durable state contains prohibited secret material")
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, current in value.items():
            if str(key).casefold() in {
                "capability_token",
                "authorization",
                "archive_passphrase",
                "secret",
                "token",
            }:
                return True
            if _contains_secret_field(current):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


def _pagination(page: int, per_page: int) -> None:
    if page < 1 or per_page < 1 or per_page > 100:
        raise ValueError("stove0 pagination is invalid")


def _page(
    records: Sequence[WorkRecord],
    *,
    item_key: str,
    page: int,
    per_page: int,
    total: int,
    sort: str,
    order: str,
    all_items: bool,
    filters: dict[str, object],
) -> dict[str, object]:
    return {
        "page": 1 if all_items else page,
        "per_page": total if all_items else per_page,
        "total": total,
        "pages": (1 if total else 0) if all_items else (total + per_page - 1) // per_page,
        "sort": sort,
        "order": order,
        "filters": filters,
        item_key: [
            record.model_dump(mode="json", by_alias=True, exclude_none=True) for record in records
        ],
    }


def _model_page(
    records: Sequence[EvaluationRecord],
    *,
    item_key: str,
    page: int,
    per_page: int,
    total: int,
    sort: str,
    order: str,
    all_items: bool,
    filters: dict[str, object],
) -> dict[str, object]:
    return {
        "page": 1 if all_items else page,
        "per_page": total if all_items else per_page,
        "total": total,
        "pages": (1 if total else 0) if all_items else (total + per_page - 1) // per_page,
        "sort": sort,
        "order": order,
        "filters": filters,
        item_key: [
            record.model_dump(mode="json", by_alias=True, exclude_none=True) for record in records
        ],
    }


__all__ = ["SqlAlchemyStateStore"]
