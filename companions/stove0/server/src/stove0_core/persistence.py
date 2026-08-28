"""One SQLAlchemy persistence authority for stove0 control-plane state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import (
    Index,
    Integer,
    String,
    Table,
    Text,
    asc,
    create_engine,
    delete,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import Select
from state_schema import StateSchema, assert_schema_matches_metadata
from stove0_operator_contracts import (
    BRANCH_SET_ADMITTED,
    EVALUATION_CREATED,
    EVALUATION_UPDATED,
    JOIN_ADMITTED,
    WORK_CREATED,
    WORK_UPDATED,
    Stove0EventPage,
    Stove0EventType,
    parse_stove0_event,
    stove0_event,
)
from stove0_protocol import ArtifactSelection, BranchSetDecision, JoinPlan, branch_work
from time_formats import utc_timestamp_now

from stove0_core.evaluation import (
    ConcurrentEvaluationUpdate,
    EvaluationRecord,
)
from stove0_core.work_state import (
    ConcurrentWorkUpdate,
    Stove0StateError,
    WorkRecord,
    _admitted_child_records,
    _preview_target_expectations,
)

SortOrder = Literal["asc", "desc"]
WorkSort = Literal["updated_at", "phase", "work_id"]
EvaluationSort = Literal["updated_at", "phase", "evaluation_id"]
STATE_VERSION_TABLE = "stove0_state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")


class _Base(DeclarativeBase):
    pass


class _WorkRow(_Base):
    __tablename__ = "stove0_work_records"
    __table_args__ = (Index("ix_stove0_work_records_phase_work_id", "phase", "work_id"),)

    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)


class _ArtifactSelectionRow(_Base):
    __tablename__ = "stove0_artifact_selections"

    selection_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)


def create_state_engine(database_url: str) -> Engine:
    _prepare_sqlite_parent(database_url)
    return create_engine(database_url, pool_pre_ping=True)


def _assert_schema_matches_models(bind: Connection) -> None:
    assert_schema_matches_metadata(
        bind,
        _Base.metadata,
        version_table=STATE_VERSION_TABLE,
    )


def stove0_state_schema(database_url: str) -> StateSchema:
    return StateSchema(
        name="stove0 control",
        engine_factory=lambda: create_state_engine(database_url),
        script_location=STATE_MIGRATIONS,
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
                    type=WORK_CREATED,
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
            if (
                existing.work != record.work
                or (
                    record.preview_acceptance is not None
                    and existing.preview_acceptance != record.preview_acceptance
                )
                or (
                    record.expected_target_plan_sha256 is not None
                    and existing.expected_target_plan_sha256 != record.expected_target_plan_sha256
                )
            ):
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
                type=WORK_UPDATED,
                subject=work_id,
                data={
                    "work_id": work_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                },
            )
        return replacement

    def admit_branch_set(
        self,
        work_id: str,
        *,
        expected_revision: int,
        decision: BranchSetDecision,
    ) -> WorkRecord:
        now = utc_timestamp_now()
        with self.sessions() as session, session.begin():
            row = session.scalar(
                select(_WorkRow).where(_WorkRow.work_id == work_id).with_for_update()
            )
            if row is None:
                raise KeyError(work_id)
            current = WorkRecord.model_validate_json(row.document_json)
            if current.branch_set_plan == decision.plan:
                return current
            if current.revision != expected_revision:
                raise ConcurrentWorkUpdate(
                    f"stale stove0 work revision: {expected_revision} != {current.revision}"
                )
            if current.phase != "planning" or decision.plan.parent_work != current.work:
                raise Stove0StateError("work cannot admit this branch set")

            expectations = _preview_target_expectations(current, decision)
            owners = {
                branch_work(branch).work_id: plan
                for plan in decision.branch_set_documents.values()
                for branch in plan.branches
            }

            for selection in decision.selections:
                _insert_or_verify_selection(session, selection)

            children = _admitted_child_records(decision, expectations)
            for child in children:
                encoded = _encode(child.model_dump(mode="json", by_alias=True, exclude_none=True))
                if _insert_work_atomically(session, child, encoded=encoded, now=now):
                    _emit(
                        session,
                        type=WORK_CREATED,
                        subject=child.work_id,
                        data={
                            "work_id": child.work_id,
                            "phase": child.phase,
                            "parent_work_id": owners[child.work_id].parent_work.work_id,
                            "branch_set_sha256": owners[child.work_id].branch_set_sha256,
                        },
                    )
                    continue
                existing_row = session.get(_WorkRow, child.work_id)
                if existing_row is None:
                    raise RuntimeError("stove0 branch child disappeared during admission")
                existing = WorkRecord.model_validate_json(existing_row.document_json)
                if (
                    existing.work != child.work
                    or existing.workflow_plan != child.workflow_plan
                    or existing.branch_set_plan != child.branch_set_plan
                    or existing.expected_target_plan_sha256 != child.expected_target_plan_sha256
                ):
                    raise ConcurrentWorkUpdate("branch child identity was reused")

            replacement = WorkRecord.model_validate(
                current.model_copy(
                    update={
                        "phase": "coordinating",
                        "branch_set_plan": decision.plan,
                        "revision": current.revision + 1,
                    }
                ).model_dump(mode="python")
            )
            row.revision = replacement.revision
            row.phase = replacement.phase
            row.updated_at = now
            row.document_json = _encode(
                replacement.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            _emit(
                session,
                type=BRANCH_SET_ADMITTED,
                subject=work_id,
                data={
                    "work_id": work_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                    "branch_set_sha256": decision.plan.branch_set_sha256,
                    "branch_count": len(decision.plan.branches),
                    "admitted_work_count": len(children),
                },
            )
            return replacement

    def admit_join(
        self,
        work_id: str,
        *,
        expected_revision: int,
        plan: JoinPlan,
        selections: Sequence[ArtifactSelection],
    ) -> WorkRecord:
        selection_documents = {item.selection_sha256: item for item in selections}
        if len(selection_documents) != len(tuple(selections)):
            raise ValueError("resolved join selections must be unique")
        now = utc_timestamp_now()
        with self.sessions() as session, session.begin():
            row = session.scalar(
                select(_WorkRow).where(_WorkRow.work_id == work_id).with_for_update()
            )
            if row is None:
                raise KeyError(work_id)
            current = WorkRecord.model_validate_json(row.document_json)
            if current.join_plan == plan:
                return current
            if current.revision != expected_revision:
                raise ConcurrentWorkUpdate(
                    f"stale stove0 work revision: {expected_revision} != {current.revision}"
                )
            if (
                current.phase != "coordinating"
                or current.branch_set_plan is None
                or plan.branch_set_sha256 != current.branch_set_plan.branch_set_sha256
            ):
                raise Stove0StateError("work cannot admit this resolved join")
            if current.join_plan is not None:
                if current.join_plan != plan:
                    raise ConcurrentWorkUpdate("branch set already resolved another join plan")
                return current

            for selection in selection_documents.values():
                _insert_or_verify_selection(session, selection)

            child = WorkRecord(work=plan.work, workflow_plan=plan.workflow_plan)
            encoded = _encode(child.model_dump(mode="json", by_alias=True, exclude_none=True))
            if _insert_work_atomically(session, child, encoded=encoded, now=now):
                _emit(
                    session,
                    type=WORK_CREATED,
                    subject=child.work_id,
                    data={
                        "work_id": child.work_id,
                        "phase": child.phase,
                        "parent_work_id": work_id,
                        "branch_set_sha256": plan.branch_set_sha256,
                        "join_plan_sha256": plan.join_plan_sha256,
                    },
                )
            else:
                existing_row = session.get(_WorkRow, child.work_id)
                if existing_row is None:
                    raise RuntimeError("stove0 join child disappeared during admission")
                existing = WorkRecord.model_validate_json(existing_row.document_json)
                if existing.work != child.work or existing.workflow_plan != child.workflow_plan:
                    raise ConcurrentWorkUpdate("join work identity was reused")

            replacement = WorkRecord.model_validate(
                current.model_copy(
                    update={
                        "join_plan": plan,
                        "revision": current.revision + 1,
                    }
                ).model_dump(mode="python")
            )
            row.revision = replacement.revision
            row.phase = replacement.phase
            row.updated_at = now
            row.document_json = _encode(
                replacement.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            _emit(
                session,
                type=JOIN_ADMITTED,
                subject=work_id,
                data={
                    "work_id": work_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                    "branch_set_sha256": plan.branch_set_sha256,
                    "join_plan_sha256": plan.join_plan_sha256,
                    "join_work_id": child.work_id,
                },
            )
            return replacement

    def load_selection(self, selection_sha256: str) -> ArtifactSelection | None:
        with self.sessions() as session:
            row = session.get(_ArtifactSelectionRow, selection_sha256)
            return None if row is None else ArtifactSelection.model_validate_json(row.document_json)

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

    def scan_work(
        self,
        *,
        phases: Sequence[str],
        after_work_id: str,
        limit: int,
    ) -> tuple[list[WorkRecord], str]:
        """Return one bounded keyset page containing only runnable work."""

        if not phases or limit < 1 or limit > 100:
            raise ValueError("stove0 work scan is invalid")
        canonical_phases = tuple(sorted(set(phases)))

        def statement(*, after: str) -> Select[tuple[_WorkRow]]:
            query = (
                select(_WorkRow)
                .where(_WorkRow.phase.in_(canonical_phases))
                .order_by(_WorkRow.work_id)
                .limit(limit)
            )
            return query if not after else query.where(_WorkRow.work_id > after)

        with self.sessions() as session:
            rows = list(session.scalars(statement(after=after_work_id)))
            if not rows and after_work_id:
                rows = list(session.scalars(statement(after="")))
        records = [WorkRecord.model_validate_json(row.document_json) for row in rows]
        return records, (records[-1].work_id if records else "")

    def prune_operational_state(self, *, cutoff: str) -> dict[str, int]:
        """Atomically prune only complete, expired control-state components."""

        terminal_work = frozenset({"complete", "inapplicable", "failed", "canceled"})
        terminal_evaluations = frozenset({"complete", "failed", "canceled"})
        with self.sessions() as session, session.begin():
            work_rows = list(
                session.scalars(select(_WorkRow).order_by(_WorkRow.work_id).with_for_update())
            )
            work_records = {
                row.work_id: WorkRecord.model_validate_json(row.document_json) for row in work_rows
            }
            row_by_work = {row.work_id: row for row in work_rows}
            neighbors: dict[str, set[str]] = {work_id: set() for work_id in work_records}
            blocked: set[str] = set()

            def connect(left: str, right: str) -> None:
                if left not in neighbors or right not in neighbors:
                    if left in neighbors:
                        blocked.add(left)
                    if right in neighbors:
                        blocked.add(right)
                    return
                neighbors[left].add(right)
                neighbors[right].add(left)

            evaluation_members: dict[str, set[str]] = {}
            for work_id, record in work_records.items():
                binding = record.work.fork_join
                if binding is not None:
                    connect(work_id, binding.parent_work_id)
                if record.branch_set_plan is not None:
                    for branch in record.branch_set_plan.branches:
                        connect(work_id, branch_work(branch).work_id)
                if record.join_plan is not None:
                    connect(work_id, record.join_plan.work.work_id)
                if record.work.evaluation is not None:
                    evaluation_members.setdefault(record.work.evaluation.evaluation_id, set()).add(
                        work_id
                    )

            evaluation_rows = list(
                session.scalars(
                    select(_EvaluationRow).order_by(_EvaluationRow.evaluation_id).with_for_update()
                )
            )
            evaluations = {
                row.evaluation_id: EvaluationRecord.model_validate_json(row.document_json)
                for row in evaluation_rows
            }
            evaluation_row_by_id = {row.evaluation_id: row for row in evaluation_rows}
            blocked_evaluations: set[str] = set()
            for evaluation_id, evaluation in evaluations.items():
                declared = {child.work_id for child in evaluation.children}
                evaluation_members.setdefault(evaluation_id, set()).update(declared)
                missing = declared - set(work_records)
                if missing:
                    blocked_evaluations.add(evaluation_id)
                    blocked.update(declared & set(work_records))
            for evaluation_id, members in evaluation_members.items():
                present = sorted(members & set(work_records))
                if evaluation_id not in evaluations:
                    blocked.update(present)
                for member in present[1:]:
                    connect(present[0], member)

            removed_work: set[str] = set()
            removed_evaluations: set[str] = set()
            pending = set(work_records)
            while pending:
                start = min(pending)
                component: set[str] = set()
                frontier = [start]
                while frontier:
                    current = frontier.pop()
                    if current in component:
                        continue
                    component.add(current)
                    frontier.extend(neighbors[current] - component)
                pending -= component
                evaluation_ids = {
                    record.work.evaluation.evaluation_id
                    for work_id in component
                    if (record := work_records[work_id]).work.evaluation is not None
                }
                if component & blocked or evaluation_ids & blocked_evaluations:
                    continue
                if any(
                    row_by_work[work_id].phase not in terminal_work
                    or row_by_work[work_id].updated_at > cutoff
                    for work_id in component
                ):
                    continue
                if any(
                    evaluation_id not in evaluations
                    or evaluation_row_by_id[evaluation_id].phase not in terminal_evaluations
                    or evaluation_row_by_id[evaluation_id].updated_at > cutoff
                    for evaluation_id in evaluation_ids
                ):
                    continue
                removed_work.update(component)
                removed_evaluations.update(evaluation_ids)

            work_bytes = sum(
                len(row_by_work[work_id].document_json.encode("utf-8")) for work_id in removed_work
            )
            evaluation_bytes = sum(
                len(evaluation_row_by_id[evaluation_id].document_json.encode("utf-8"))
                for evaluation_id in removed_evaluations
            )
            if removed_work:
                session.execute(delete(_WorkRow).where(_WorkRow.work_id.in_(removed_work)))
            if removed_evaluations:
                session.execute(
                    delete(_EvaluationRow).where(
                        _EvaluationRow.evaluation_id.in_(removed_evaluations)
                    )
                )

            remaining = [
                record for work_id, record in work_records.items() if work_id not in removed_work
            ]
            referenced_selections = _selection_references(remaining)
            selection_rows = list(
                session.scalars(
                    select(_ArtifactSelectionRow)
                    .order_by(_ArtifactSelectionRow.selection_sha256)
                    .with_for_update()
                )
            )
            removed_selection_ids = {
                row.selection_sha256
                for row in selection_rows
                if row.selection_sha256 not in referenced_selections
            }
            selection_bytes = sum(
                len(row.document_json.encode("utf-8"))
                for row in selection_rows
                if row.selection_sha256 in removed_selection_ids
            )
            if removed_selection_ids:
                session.execute(
                    delete(_ArtifactSelectionRow).where(
                        _ArtifactSelectionRow.selection_sha256.in_(removed_selection_ids)
                    )
                )

            event_rows = list(
                session.scalars(
                    select(_EventRow)
                    .where(_EventRow.created_at <= cutoff)
                    .order_by(_EventRow.sequence)
                    .with_for_update()
                )
            )
            event_bytes = sum(len(row.event_json.encode("utf-8")) for row in event_rows)
            if event_rows:
                session.execute(
                    delete(_EventRow).where(
                        _EventRow.sequence.in_(row.sequence for row in event_rows)
                    )
                )

        return {
            "work": len(removed_work),
            "work_bytes": work_bytes,
            "evaluations": len(removed_evaluations),
            "evaluation_bytes": evaluation_bytes,
            "selections": len(removed_selection_ids),
            "selection_bytes": selection_bytes,
            "events": len(event_rows),
            "event_bytes": event_bytes,
        }

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
                    type=EVALUATION_CREATED,
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
                type=EVALUATION_UPDATED,
                subject=evaluation_id,
                data={
                    "evaluation_id": evaluation_id,
                    "phase": replacement.phase,
                    "revision": replacement.revision,
                },
            )
        return replacement

    def list_events(self, *, after: str | None = None, limit: int = 100) -> Stove0EventPage:
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
        return Stove0EventPage(
            events=[parse_stove0_event(json.loads(row.event_json)) for row in selected],
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


def _insert_or_verify_selection(
    session: Session,
    selection: ArtifactSelection,
) -> None:
    encoded = _encode(selection.model_dump(mode="json", by_alias=True, exclude_none=True))
    table = cast(Table, _ArtifactSelectionRow.__table__)
    values = {
        "selection_sha256": selection.selection_sha256,
        "document_json": encoded,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(postgresql_insert(table).values(**values).on_conflict_do_nothing())
    elif dialect == "sqlite":
        session.execute(sqlite_insert(table).values(**values).on_conflict_do_nothing())
    else:
        raise RuntimeError(f"unsupported Stove0 state database dialect: {dialect}")
    existing = session.get(_ArtifactSelectionRow, selection.selection_sha256)
    if existing is None:
        raise RuntimeError("stove0 artifact selection disappeared during admission")
    if ArtifactSelection.model_validate_json(existing.document_json) != selection:
        raise ConcurrentWorkUpdate("artifact selection identity was reused")


def _insert_work_atomically(
    session: Session,
    record: WorkRecord,
    *,
    encoded: str,
    now: str,
) -> bool:
    table = cast(Table, _WorkRow.__table__)
    values = {
        "work_id": record.work_id,
        "revision": record.revision,
        "phase": record.phase,
        "updated_at": now,
        "document_json": encoded,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = (
            postgresql_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c.work_id)
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c.work_id)
        )
    else:
        raise RuntimeError(f"unsupported Stove0 state database dialect: {dialect}")
    return session.scalar(statement) is not None


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
    type: Stove0EventType,
    subject: str,
    data: dict[str, object],
) -> None:
    event = stove0_event(
        type=type,
        subject=subject,
        data=data,
    )
    session.add(
        _EventRow(
            created_at=event.time,
            event_json=event.model_dump_json(exclude_none=True),
        )
    )


def _selection_references(records: Sequence[WorkRecord]) -> set[str]:
    references: set[str] = set()
    for record in records:
        binding = record.work.fork_join
        if binding is not None:
            if binding.kind == "branch":
                references.add(binding.artifact_selection_sha256)
            else:
                references.update(item.artifact_selection_sha256 for item in binding.members)
        if record.branch_set_plan is not None:
            references.update(
                branch.artifact_selection.selection_sha256
                for branch in record.branch_set_plan.branches
            )
        if record.join_plan is not None:
            references.update(
                member.artifact_selection.selection_sha256 for member in record.join_plan.inputs
            )
    return references


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
