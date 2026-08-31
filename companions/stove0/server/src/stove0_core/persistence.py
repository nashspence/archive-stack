"""One SQLAlchemy persistence authority for stove0 control-plane state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

from http_api_contracts import closed_literal_values
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    and_,
    asc,
    create_engine,
    delete,
    desc,
    func,
    literal,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import Select
from state_schema import (
    StateSchema,
    assert_schema_matches_metadata,
    attach_sha256_string_constraints,
    read_snapshot,
    require_postgresql_extension,
    sqlite_engine,
)
from stove0_operator_contracts import (
    BRANCH_SET_ADMITTED,
    EVALUATION_CREATED,
    EVALUATION_UPDATED,
    JOIN_ADMITTED,
    WORK_CREATED,
    WORK_UPDATED,
    EvaluationPhase,
    EvaluationSort,
    SortOrder,
    Stove0EventPage,
    Stove0EventType,
    WorkPhase,
    WorkSort,
    parse_stove0_event,
    stove0_event,
)
from stove0_protocol import (
    ArtifactSelection,
    ArtifactSelectionRef,
    ArtifactSubject,
    BranchSetDecision,
    JoinPlan,
    branch_work,
    canonical_json_bytes,
)
from stove0_target_protocol import (
    InputDispositionDeclaration,
    OutputArtifact,
    OutputSourceEdge,
)
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

_SORT_ORDERS = closed_literal_values(SortOrder)
_WORK_PHASES = closed_literal_values(WorkPhase)
_WORK_SORTS = closed_literal_values(WorkSort)
_EVALUATION_PHASES = closed_literal_values(EvaluationPhase)
_EVALUATION_SORTS = closed_literal_values(EvaluationSort)
_PRUNE_ROOT_BATCH = 100
_PRUNE_ORPHAN_BATCH = 100
_PRUNE_EVENT_BATCH = 1000
_PRUNE_CURSOR_STREAM = "stove0:operational-prune/v1"
STATE_VERSION_TABLE = "stove0_state_schema_revision"
STATE_MIGRATIONS = Path(__file__).with_name("state_migrations")


class _Base(DeclarativeBase):
    pass


class _WorkRow(_Base):
    __tablename__ = "stove0_work_records"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_stove0_work_records_revision"),
        CheckConstraint(
            "phase IN ('eligible','claimed','observing','planning','target_preflight',"
            "'queued','executing','output_finalizing','verifying','settled',"
            "'retirement_pending','coordinating','abandon_pending','complete',"
            "'inapplicable','failed','canceled')",
            name="ck_stove0_work_records_phase",
        ),
        CheckConstraint("length(work_id) = 64", name="ck_stove0_work_records_id"),
        CheckConstraint("document_bytes >= 0", name="ck_stove0_work_records_document_bytes"),
        Index("ix_stove0_work_records_phase_work_id", "phase", "work_id"),
        Index("ix_stove0_work_records_updated_work_id", "updated_at", "work_id"),
        Index(
            "ix_stove0_work_records_id_trgm",
            "work_id",
            postgresql_using="gin",
            postgresql_ops={"work_id": "gin_trgm_ops"},
        ),
    )

    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    document_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)


class _WorkRelationRow(_Base):
    """Rebuildable undirected-retention edge projected from a work document."""

    __tablename__ = "stove0_work_relations"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    related_work_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        CheckConstraint("work_id <> related_work_id", name="ck_stove0_work_relations_distinct"),
        Index("ix_stove0_work_relations_related", "related_work_id", "work_id"),
    )


class _WorkEvaluationRow(_Base):
    """Rebuildable evaluation membership projected from a work document."""

    __tablename__ = "stove0_work_evaluations"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_stove0_work_evaluations_evaluation", "evaluation_id", "work_id"),)


class _WorkSelectionReferenceRow(_Base):
    """Rebuildable artifact-selection reference projected from a work document."""

    __tablename__ = "stove0_work_selection_references"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    selection_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        Index(
            "ix_stove0_work_selection_references_selection",
            "selection_sha256",
            "work_id",
        ),
    )


class _ArtifactSelectionRow(_Base):
    __tablename__ = "stove0_artifact_selections"

    selection_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("length(selection_sha256) = 64", name="ck_stove0_selections_id"),
        CheckConstraint("artifact_count >= 0", name="ck_stove0_selections_count"),
        CheckConstraint("total_bytes >= 0", name="ck_stove0_selections_bytes"),
    )


class _ArtifactSelectionMemberRow(_Base):
    __tablename__ = "stove0_artifact_selection_members"

    selection_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_artifact_selections.selection_sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    artifact_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    artifact_order: Mapped[int] = mapped_column(Integer, nullable=False)
    continuation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("length(artifact_id) >= 1", name="ck_stove0_selection_members_artifact_id"),
        CheckConstraint("artifact_order >= 0", name="ck_stove0_selection_members_order"),
        Index(
            "ix_stove0_selection_members_order",
            "selection_sha256",
            "artifact_order",
            unique=True,
        ),
        CheckConstraint(
            "length(continuation_sha256) = 64",
            name="ck_stove0_selection_members_continuation",
        ),
        Index(
            "ix_stove0_selection_members_continuation",
            "selection_sha256",
            "continuation_sha256",
            unique=True,
        ),
        CheckConstraint("document_bytes >= 0", name="ck_stove0_selection_members_document_bytes"),
    )


class _TargetOutputRow(_Base):
    __tablename__ = "stove0_target_outputs"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    output_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    output_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    document_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("length(output_id) >= 1", name="ck_stove0_target_outputs_id"),
        CheckConstraint("length(output_path) >= 1", name="ck_stove0_target_outputs_path"),
        CheckConstraint("document_bytes >= 0", name="ck_stove0_target_outputs_document_bytes"),
        Index("uq_stove0_target_outputs_path", "work_id", "output_path", unique=True),
    )


class _TargetDispositionRow(_Base):
    __tablename__ = "stove0_target_input_dispositions"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    input_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("length(input_id) >= 1", name="ck_stove0_target_dispositions_id"),
        CheckConstraint(
            "status IN ('omitted','preserved','rejected','transformed')",
            name="ck_stove0_target_dispositions_status",
        ),
    )


class _TargetSourceEdgeRow(_Base):
    __tablename__ = "stove0_target_source_edges"

    work_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_work_records.work_id", ondelete="CASCADE"),
        primary_key=True,
    )
    output_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    input_id: Mapped[str] = mapped_column(String(160), primary_key=True)

    __table_args__ = (
        CheckConstraint("length(output_id) >= 1", name="ck_stove0_target_source_edges_output"),
        CheckConstraint("length(input_id) >= 1", name="ck_stove0_target_source_edges_input"),
        Index("ix_stove0_target_source_edges_input", "work_id", "input_id", "output_id"),
    )


class _EvaluationRow(_Base):
    __tablename__ = "stove0_evaluation_records"

    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    document_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_stove0_evaluation_records_revision"),
        CheckConstraint(
            "phase IN ('planning','running','partially_complete','complete','failed','canceled')",
            name="ck_stove0_evaluation_records_phase",
        ),
        CheckConstraint("length(evaluation_id) = 64", name="ck_stove0_evaluation_records_id"),
        CheckConstraint("document_bytes >= 0", name="ck_stove0_evaluation_records_document_bytes"),
        Index(
            "ix_stove0_evaluation_records_phase_id",
            "phase",
            "evaluation_id",
        ),
        Index(
            "ix_stove0_evaluation_records_updated_id",
            "updated_at",
            "evaluation_id",
        ),
        Index(
            "ix_stove0_evaluation_records_id_trgm",
            "evaluation_id",
            postgresql_using="gin",
            postgresql_ops={"evaluation_id": "gin_trgm_ops"},
        ),
    )


class _EvaluationChildRow(_Base):
    """Rebuildable child membership projected from an evaluation document."""

    __tablename__ = "stove0_evaluation_children"

    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("stove0_evaluation_records.evaluation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_stove0_evaluation_children_work", "work_id", "evaluation_id"),)


class _CursorRow(_Base):
    __tablename__ = "stove0_event_cursors"

    stream: Mapped[str] = mapped_column(String(160), primary_key=True)
    cursor: Mapped[str] = mapped_column(String(500), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (CheckConstraint("revision >= 1", name="ck_stove0_event_cursors_revision"),)


class _EventRow(_Base):
    __tablename__ = "stove0_lifecycle_events"
    __table_args__ = (
        CheckConstraint("event_bytes >= 0", name="ck_stove0_lifecycle_events_event_bytes"),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    event_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)


attach_sha256_string_constraints(_Base.metadata)


def create_state_engine(database_url: str) -> Engine:
    _prepare_sqlite_parent(database_url)
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database is not None:
        return sqlite_engine(Path(url.database))
    return create_engine(database_url, pool_pre_ping=True)


def _assert_schema_matches_models(bind: Connection) -> None:
    assert_schema_matches_metadata(
        bind,
        _Base.metadata,
        version_table=STATE_VERSION_TABLE,
    )


def _require_state_database_capabilities(bind: Connection) -> None:
    require_postgresql_extension(
        bind,
        name="pg_trgm",
        schema="public",
        operator_classes=("gin_trgm_ops",),
    )


def stove0_state_schema(database_url: str) -> StateSchema:
    return StateSchema(
        name="stove0 control",
        engine_factory=lambda: create_state_engine(database_url),
        script_location=STATE_MIGRATIONS,
        prerequisite=_require_state_database_capabilities,
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
            with self.engine.begin() as connection:
                stove0_state_schema(database_url).upgrade_connection(connection)

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
                        document_bytes=_encoded_bytes(encoded),
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
            _replace_work_projections(session, replacement)
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
            replacement_json = _encode(
                replacement.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            row.document_bytes = _encoded_bytes(replacement_json)
            row.document_json = replacement_json
            _replace_work_projections(session, replacement)
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
            replacement_json = _encode(
                replacement.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            row.document_bytes = _encoded_bytes(replacement_json)
            row.document_json = replacement_json
            _replace_work_projections(session, replacement)
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
            if row is None:
                return None
            members = session.scalars(
                select(_ArtifactSelectionMemberRow)
                .where(_ArtifactSelectionMemberRow.selection_sha256 == selection_sha256)
                .order_by(_ArtifactSelectionMemberRow.artifact_order)
            )
            return _selection_from_rows(row, members)

    def retain_selection(self, selection: ArtifactSelection) -> None:
        with self.sessions() as session, session.begin():
            _insert_or_verify_selection(session, selection)

    def load_selection_artifact(
        self,
        selection_sha256: str,
        artifact_id: str,
    ) -> ArtifactSubject | None:
        with self.sessions() as session:
            row = session.get(_ArtifactSelectionMemberRow, (selection_sha256, artifact_id))
            return None if row is None else ArtifactSubject.model_validate_json(row.document_json)

    def record_target_output(self, work_id: str, output: OutputArtifact) -> None:
        encoded = _encode(output.model_dump(mode="json", by_alias=True, exclude_none=True))
        with self.sessions() as session, session.begin():
            existing = session.get(_TargetOutputRow, (work_id, output.id))
            if existing is None:
                session.add(
                    _TargetOutputRow(
                        work_id=work_id,
                        output_id=output.id,
                        output_path=output.path,
                        document_bytes=_encoded_bytes(encoded),
                        document_json=encoded,
                    )
                )
            elif existing.document_json != encoded:
                raise ConcurrentWorkUpdate("target output declaration changed")

    def record_target_disposition(
        self,
        work_id: str,
        disposition: InputDispositionDeclaration,
    ) -> None:
        with self.sessions() as session, session.begin():
            existing = session.get(_TargetDispositionRow, (work_id, disposition.input_id))
            if existing is None:
                session.add(
                    _TargetDispositionRow(
                        work_id=work_id,
                        input_id=disposition.input_id,
                        status=disposition.status,
                    )
                )
            elif existing.status != disposition.status:
                raise ConcurrentWorkUpdate("target input disposition changed")

    def record_target_source_edge(self, work_id: str, edge: OutputSourceEdge) -> None:
        with self.sessions() as session, session.begin():
            if (
                session.get(
                    _TargetSourceEdgeRow,
                    (work_id, edge.output_id, edge.input_id),
                )
                is None
            ):
                session.add(
                    _TargetSourceEdgeRow(
                        work_id=work_id,
                        output_id=edge.output_id,
                        input_id=edge.input_id,
                    )
                )

    def load_target_output(self, work_id: str, output_id: str) -> OutputArtifact | None:
        with self.sessions() as session:
            row = session.get(_TargetOutputRow, (work_id, output_id))
            return None if row is None else OutputArtifact.model_validate_json(row.document_json)

    def load_target_disposition(
        self,
        work_id: str,
        input_id: str,
    ) -> InputDispositionDeclaration | None:
        with self.sessions() as session:
            row = session.get(_TargetDispositionRow, (work_id, input_id))
            return (
                None
                if row is None
                else InputDispositionDeclaration(
                    input_id=row.input_id, status=cast(Any, row.status)
                )
            )

    def iter_target_outputs(self, work_id: str) -> Iterator[OutputArtifact]:
        statement = (
            select(_TargetOutputRow)
            .where(_TargetOutputRow.work_id == work_id)
            .order_by(_TargetOutputRow.output_id)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield OutputArtifact.model_validate_json(row.document_json)

    def iter_target_outputs_by_path(self, work_id: str) -> Iterator[OutputArtifact]:
        statement = (
            select(_TargetOutputRow)
            .where(_TargetOutputRow.work_id == work_id)
            .order_by(_TargetOutputRow.output_path)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield OutputArtifact.model_validate_json(row.document_json)

    def iter_target_dispositions(
        self,
        work_id: str,
    ) -> Iterator[InputDispositionDeclaration]:
        statement = (
            select(_TargetDispositionRow)
            .where(_TargetDispositionRow.work_id == work_id)
            .order_by(_TargetDispositionRow.input_id)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield InputDispositionDeclaration(
                    input_id=row.input_id, status=cast(Any, row.status)
                )

    def iter_target_source_edges(self, work_id: str) -> Iterator[OutputSourceEdge]:
        statement = (
            select(_TargetSourceEdgeRow)
            .where(_TargetSourceEdgeRow.work_id == work_id)
            .order_by(_TargetSourceEdgeRow.output_id, _TargetSourceEdgeRow.input_id)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield OutputSourceEdge(output_id=row.output_id, input_id=row.input_id)

    def iter_target_source_edges_by_input(self, work_id: str) -> Iterator[OutputSourceEdge]:
        statement = (
            select(_TargetSourceEdgeRow)
            .where(_TargetSourceEdgeRow.work_id == work_id)
            .order_by(_TargetSourceEdgeRow.input_id, _TargetSourceEdgeRow.output_id)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield OutputSourceEdge(output_id=row.output_id, input_id=row.input_id)

    def load_selection_ref(self, selection_sha256: str) -> ArtifactSelectionRef | None:
        with self.sessions() as session:
            row = session.get(_ArtifactSelectionRow, selection_sha256)
            return None if row is None else _selection_ref(row)

    def selection_artifact_page(
        self,
        selection_sha256: str,
        *,
        continuation: str | None,
        limit: int,
    ) -> tuple[tuple[ArtifactSubject, ...], str | None, bool]:
        if limit < 1 or limit > 1000:
            raise ValueError("artifact selection page is invalid")
        with self.sessions() as session:
            after = -1
            if continuation is not None:
                prior = session.scalar(
                    select(_ArtifactSelectionMemberRow).where(
                        _ArtifactSelectionMemberRow.selection_sha256 == selection_sha256,
                        _ArtifactSelectionMemberRow.continuation_sha256 == continuation,
                    )
                )
                if prior is None:
                    raise ValueError("artifact selection continuation is invalid")
                after = prior.artifact_order
            rows = session.scalars(
                select(_ArtifactSelectionMemberRow)
                .where(
                    _ArtifactSelectionMemberRow.selection_sha256 == selection_sha256,
                    _ArtifactSelectionMemberRow.artifact_order > after,
                )
                .order_by(_ArtifactSelectionMemberRow.artifact_order)
                .limit(limit + 1)
            )
            selected = list(rows)
            complete = len(selected) <= limit
            page = selected[:limit]
            next_continuation = None if complete or not page else page[-1].continuation_sha256
            return (
                tuple(ArtifactSubject.model_validate_json(row.document_json) for row in page),
                next_continuation,
                complete,
            )

    def iter_selection_artifacts(self, selection_sha256: str) -> Iterator[ArtifactSubject]:
        statement = (
            select(_ArtifactSelectionMemberRow)
            .where(_ArtifactSelectionMemberRow.selection_sha256 == selection_sha256)
            .order_by(_ArtifactSelectionMemberRow.artifact_order)
            .execution_options(yield_per=100)
        )
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement):
                yield ArtifactSubject.model_validate_json(row.document_json)

    def list_work(
        self,
        *,
        page_size: int = 25,
        position: tuple[str | int | bool | bytes | None, ...] | None = None,
        phase: str | None = None,
        query: str | None = None,
        sort: WorkSort = "updated_at",
        order: SortOrder = "desc",
    ) -> dict[str, object]:
        _page_size(page_size)
        _, statement, key_columns = _work_list_statement(
            phase=phase, query=query, sort=sort, order=order
        )
        with read_snapshot(self.sessions) as session:
            rows = list(
                session.scalars(
                    _keyset_statement(
                        statement,
                        columns=key_columns,
                        position=position,
                        order=order,
                        page_size=page_size,
                    )
                )
            )
        page_rows = rows[:page_size]
        records = [WorkRecord.model_validate_json(row.document_json) for row in page_rows]
        return _page(
            records,
            item_key="work",
            page_size=page_size,
            next_position=(
                _work_position(page_rows[-1], sort=sort)
                if len(rows) > page_size and page_rows
                else None
            ),
            sort=sort,
            order=order,
            filters={"phase": phase, "query": query},
        )

    def iter_work(
        self,
        *,
        phase: str | None = None,
        query: str | None = None,
        sort: WorkSort = "updated_at",
        order: SortOrder = "desc",
    ) -> Iterator[WorkRecord]:
        _, statement, key_columns = _work_list_statement(
            phase=phase, query=query, sort=sort, order=order
        )
        direction = desc if order == "desc" else asc
        statement = statement.order_by(*(direction(column) for column in key_columns))
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement.execution_options(yield_per=100)):
                yield WorkRecord.model_validate_json(row.document_json)

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
        """Prune bounded batches using rebuildable graph projections and SQL sets."""

        totals = {
            "work": 0,
            "work_bytes": 0,
            "evaluations": 0,
            "evaluation_bytes": 0,
            "selections": 0,
            "selection_bytes": 0,
            "events": 0,
            "event_bytes": 0,
        }
        with self.sessions() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(select(func.pg_advisory_xact_lock(0x53544F5630505255)))
            for work_id in _prune_root_page(session, cutoff=cutoff):
                removed = _prune_work_component(session, work_id=work_id, cutoff=cutoff)
                for key, value in removed.items():
                    totals[key] += value

            orphan_ids = tuple(
                session.scalars(
                    select(_ArtifactSelectionRow.selection_sha256)
                    .where(
                        ~select(_WorkSelectionReferenceRow.work_id)
                        .where(
                            _WorkSelectionReferenceRow.selection_sha256
                            == _ArtifactSelectionRow.selection_sha256
                        )
                        .exists()
                    )
                    .order_by(_ArtifactSelectionRow.selection_sha256)
                    .limit(_PRUNE_ORPHAN_BATCH)
                )
            )
            if orphan_ids:
                selection_count, selection_bytes = session.execute(
                    select(
                        func.count(_ArtifactSelectionMemberRow.artifact_id),
                        func.coalesce(func.sum(_ArtifactSelectionMemberRow.document_bytes), 0),
                    ).where(_ArtifactSelectionMemberRow.selection_sha256.in_(orphan_ids))
                ).one()
                del selection_count  # member count is not part of the public retention metric
                deleted = session.execute(
                    delete(_ArtifactSelectionRow).where(
                        _ArtifactSelectionRow.selection_sha256.in_(orphan_ids)
                    )
                )
                totals["selections"] = int(getattr(deleted, "rowcount", 0) or 0)
                totals["selection_bytes"] = int(selection_bytes or 0)

            event_rows = list(
                session.execute(
                    select(_EventRow.sequence, _EventRow.event_bytes)
                    .where(_EventRow.created_at <= cutoff)
                    .order_by(_EventRow.sequence)
                    .limit(_PRUNE_EVENT_BATCH)
                )
            )
            if event_rows:
                event_ids = tuple(int(row.sequence) for row in event_rows)
                deleted = session.execute(
                    delete(_EventRow).where(_EventRow.sequence.in_(event_ids))
                )
                totals["events"] = int(getattr(deleted, "rowcount", 0) or 0)
                totals["event_bytes"] = sum(int(row.event_bytes) for row in event_rows)

        return totals

    def load_evaluation(self, evaluation_id: str) -> EvaluationRecord | None:
        with self.sessions() as session:
            row = session.get(_EvaluationRow, evaluation_id)
            return None if row is None else EvaluationRecord.model_validate_json(row.document_json)

    def list_evaluations(
        self,
        *,
        page_size: int = 25,
        position: tuple[str | int | bool | bytes | None, ...] | None = None,
        phase: str | None = None,
        query: str | None = None,
        sort: EvaluationSort = "updated_at",
        order: SortOrder = "desc",
    ) -> dict[str, object]:
        _page_size(page_size)
        _, statement, key_columns = _evaluation_list_statement(
            phase=phase, query=query, sort=sort, order=order
        )
        with read_snapshot(self.sessions) as session:
            rows = list(
                session.scalars(
                    _keyset_statement(
                        statement,
                        columns=key_columns,
                        position=position,
                        order=order,
                        page_size=page_size,
                    )
                )
            )
        page_rows = rows[:page_size]
        records = [EvaluationRecord.model_validate_json(row.document_json) for row in page_rows]
        return _model_page(
            records,
            item_key="evaluations",
            page_size=page_size,
            next_position=(
                _evaluation_position(page_rows[-1], sort=sort)
                if len(rows) > page_size and page_rows
                else None
            ),
            sort=sort,
            order=order,
            filters={"phase": phase, "query": query},
        )

    def iter_evaluations(
        self,
        *,
        phase: str | None = None,
        query: str | None = None,
        sort: EvaluationSort = "updated_at",
        order: SortOrder = "desc",
    ) -> Iterator[EvaluationRecord]:
        _, statement, key_columns = _evaluation_list_statement(
            phase=phase, query=query, sort=sort, order=order
        )
        direction = desc if order == "desc" else asc
        statement = statement.order_by(*(direction(column) for column in key_columns))
        with read_snapshot(self.sessions) as session:
            for row in session.scalars(statement.execution_options(yield_per=100)):
                yield EvaluationRecord.model_validate_json(row.document_json)

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
                        document_bytes=_encoded_bytes(encoded),
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
            _replace_evaluation_projection(session, replacement)
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


def _prune_root_page(session: Session, *, cutoff: str) -> tuple[str, ...]:
    """Return one bounded rotating page of terminal retention roots."""

    terminal = ("complete", "inapplicable", "failed", "canceled")
    cursor = session.get(_CursorRow, _PRUNE_CURSOR_STREAM)
    after = cursor.cursor if cursor is not None else ""

    def query(value: str) -> tuple[str, ...]:
        statement = (
            select(_WorkRow.work_id)
            .where(_WorkRow.phase.in_(terminal), _WorkRow.updated_at <= cutoff)
            .order_by(_WorkRow.work_id)
            .limit(_PRUNE_ROOT_BATCH)
        )
        if value:
            statement = statement.where(_WorkRow.work_id > value)
        return tuple(session.scalars(statement))

    work_ids = query(after)
    if not work_ids and after:
        work_ids = query("")
    next_cursor = work_ids[-1] if work_ids else ""
    now = utc_timestamp_now()
    if cursor is None:
        session.add(
            _CursorRow(
                stream=_PRUNE_CURSOR_STREAM,
                cursor=next_cursor,
                revision=1,
                updated_at=now,
            )
        )
    else:
        cursor.cursor = next_cursor
        cursor.revision += 1
        cursor.updated_at = now
    return work_ids


def _prune_component_ids(work_id: str) -> tuple[Any, Any]:
    """Build recursive work/evaluation identifiers without loading graph members."""

    def work(value: Any) -> Any:
        return literal("w:").concat(value)

    def evaluation(value: Any) -> Any:
        return literal("e:").concat(value)

    edges = union_all(
        select(
            work(_WorkRelationRow.work_id).label("source"),
            work(_WorkRelationRow.related_work_id).label("target"),
        ),
        select(
            work(_WorkRelationRow.related_work_id).label("source"),
            work(_WorkRelationRow.work_id).label("target"),
        ),
        select(
            work(_WorkEvaluationRow.work_id).label("source"),
            evaluation(_WorkEvaluationRow.evaluation_id).label("target"),
        ),
        select(
            evaluation(_WorkEvaluationRow.evaluation_id).label("source"),
            work(_WorkEvaluationRow.work_id).label("target"),
        ),
        select(
            evaluation(_EvaluationChildRow.evaluation_id).label("source"),
            work(_EvaluationChildRow.work_id).label("target"),
        ),
        select(
            work(_EvaluationChildRow.work_id).label("source"),
            evaluation(_EvaluationChildRow.evaluation_id).label("target"),
        ),
    ).subquery("stove0_retention_edges")
    component = select(literal(f"w:{work_id}").label("node")).cte(
        "stove0_retention_component",
        recursive=True,
    )
    component = component.union(select(edges.c.target).where(edges.c.source == component.c.node))
    work_ids = (
        select(func.substr(component.c.node, 3).label("work_id"))
        .where(component.c.node.like("w:%"))
        .cte("stove0_retention_work_ids")
    )
    evaluation_ids = (
        select(func.substr(component.c.node, 3).label("evaluation_id"))
        .where(component.c.node.like("e:%"))
        .cte("stove0_retention_evaluation_ids")
    )
    return work_ids, evaluation_ids


def _prune_work_component(
    session: Session,
    *,
    work_id: str,
    cutoff: str,
) -> dict[str, int]:
    """Delete one complete expired component with constant application memory."""

    empty = {"work": 0, "work_bytes": 0, "evaluations": 0, "evaluation_bytes": 0}
    work_ids, evaluation_ids = _prune_component_ids(work_id)
    missing_work = session.scalar(
        select(work_ids.c.work_id)
        .where(~select(_WorkRow.work_id).where(_WorkRow.work_id == work_ids.c.work_id).exists())
        .limit(1)
    )
    blocked_work = session.scalar(
        select(_WorkRow.work_id)
        .where(
            _WorkRow.work_id.in_(select(work_ids.c.work_id)),
            or_(
                _WorkRow.phase.not_in(("complete", "inapplicable", "failed", "canceled")),
                _WorkRow.updated_at > cutoff,
            ),
        )
        .limit(1)
    )
    missing_evaluation = session.scalar(
        select(evaluation_ids.c.evaluation_id)
        .where(
            ~select(_EvaluationRow.evaluation_id)
            .where(_EvaluationRow.evaluation_id == evaluation_ids.c.evaluation_id)
            .exists()
        )
        .limit(1)
    )
    blocked_evaluation = session.scalar(
        select(_EvaluationRow.evaluation_id)
        .where(
            _EvaluationRow.evaluation_id.in_(select(evaluation_ids.c.evaluation_id)),
            or_(
                _EvaluationRow.phase.not_in(("complete", "failed", "canceled")),
                _EvaluationRow.updated_at > cutoff,
            ),
        )
        .limit(1)
    )
    if any(
        value is not None
        for value in (missing_work, blocked_work, missing_evaluation, blocked_evaluation)
    ):
        return empty

    work_count, work_bytes = session.execute(
        select(
            func.count(_WorkRow.work_id),
            func.coalesce(func.sum(_WorkRow.document_bytes), 0),
        ).where(_WorkRow.work_id.in_(select(work_ids.c.work_id)))
    ).one()
    if not work_count:
        return empty
    evaluation_count, evaluation_bytes = session.execute(
        select(
            func.count(_EvaluationRow.evaluation_id),
            func.coalesce(func.sum(_EvaluationRow.document_bytes), 0),
        ).where(_EvaluationRow.evaluation_id.in_(select(evaluation_ids.c.evaluation_id)))
    ).one()
    deleted_evaluations = session.execute(
        delete(_EvaluationRow).where(
            _EvaluationRow.evaluation_id.in_(select(evaluation_ids.c.evaluation_id))
        )
    )
    deleted_work = session.execute(
        delete(_WorkRow).where(_WorkRow.work_id.in_(select(work_ids.c.work_id)))
    )
    actual_work = int(getattr(deleted_work, "rowcount", 0) or 0)
    actual_evaluations = int(getattr(deleted_evaluations, "rowcount", 0) or 0)
    if actual_work != int(work_count) or actual_evaluations != int(evaluation_count):
        raise RuntimeError("stove0 retention component changed during deletion")
    return {
        "work": actual_work,
        "work_bytes": int(work_bytes or 0),
        "evaluations": actual_evaluations,
        "evaluation_bytes": int(evaluation_bytes or 0),
    }


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
                    document_bytes=_encoded_bytes(encoded),
                    document_json=encoded,
                )
            )
            session.flush()
            _replace_work_projections(session, record)
        return True
    except IntegrityError:
        return False


def _insert_or_verify_selection(
    session: Session,
    selection: ArtifactSelection,
) -> None:
    table = cast(Table, _ArtifactSelectionRow.__table__)
    values = {
        "selection_sha256": selection.selection_sha256,
        "artifact_count": selection.artifact_count,
        "total_bytes": selection.total_bytes,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        inserted = session.scalar(
            postgresql_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c.selection_sha256)
        )
    elif dialect == "sqlite":
        inserted = session.scalar(
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c.selection_sha256)
        )
    else:
        raise RuntimeError(f"unsupported Stove0 state database dialect: {dialect}")
    if inserted is not None:
        session.add_all(
            _selection_member_row(selection.selection_sha256, ordinal, artifact)
            for ordinal, artifact in enumerate(selection.artifacts)
        )
        session.flush()
    existing = session.get(_ArtifactSelectionRow, selection.selection_sha256)
    if existing is None:
        raise RuntimeError("stove0 artifact selection disappeared during admission")
    members = session.scalars(
        select(_ArtifactSelectionMemberRow)
        .where(_ArtifactSelectionMemberRow.selection_sha256 == selection.selection_sha256)
        .order_by(_ArtifactSelectionMemberRow.artifact_order)
    )
    if _selection_from_rows(existing, members) != selection:
        raise ConcurrentWorkUpdate("artifact selection identity was reused")


def _selection_ref(row: _ArtifactSelectionRow) -> ArtifactSelectionRef:
    return ArtifactSelectionRef(
        selection_sha256=row.selection_sha256,
        artifact_count=row.artifact_count,
        total_bytes=row.total_bytes,
    )


def _selection_member_row(
    selection_sha256: str,
    artifact_order: int,
    artifact: ArtifactSubject,
) -> _ArtifactSelectionMemberRow:
    encoded = _encode(artifact.model_dump(mode="json", by_alias=True, exclude_none=True))
    return _ArtifactSelectionMemberRow(
        selection_sha256=selection_sha256,
        artifact_id=artifact.id,
        artifact_order=artifact_order,
        continuation_sha256=hashlib.sha256(
            b"stove0-artifact-selection-continuation/v1\x00"
            + selection_sha256.encode("ascii")
            + b"\x00"
            + canonical_json_bytes(artifact.model_dump(mode="json", exclude_none=True))
        ).hexdigest(),
        document_bytes=_encoded_bytes(encoded),
        document_json=encoded,
    )


def _selection_from_rows(
    row: _ArtifactSelectionRow,
    members: Iterator[_ArtifactSelectionMemberRow],
) -> ArtifactSelection:
    return ArtifactSelection(
        artifacts=tuple(
            ArtifactSubject.model_validate_json(member.document_json) for member in members
        ),
        artifact_count=row.artifact_count,
        total_bytes=row.total_bytes,
        selection_sha256=row.selection_sha256,
    )


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
        "document_bytes": _encoded_bytes(encoded),
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
    inserted = session.scalar(statement) is not None
    if inserted:
        _replace_work_projections(session, record)
    return inserted


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
                    document_bytes=_encoded_bytes(encoded),
                    document_json=encoded,
                )
            )
            session.flush()
            _replace_evaluation_projection(session, record)
        return True
    except IntegrityError:
        return False


def _replace_work_projections(session: Session, record: WorkRecord) -> None:
    """Replace rebuildable relational projections for one canonical work document."""

    session.execute(delete(_WorkRelationRow).where(_WorkRelationRow.work_id == record.work_id))
    session.execute(delete(_WorkEvaluationRow).where(_WorkEvaluationRow.work_id == record.work_id))
    session.execute(
        delete(_WorkSelectionReferenceRow).where(
            _WorkSelectionReferenceRow.work_id == record.work_id
        )
    )
    session.add_all(
        _WorkRelationRow(work_id=record.work_id, related_work_id=related_work_id)
        for related_work_id in sorted(_work_relations(record))
        if related_work_id != record.work_id
    )
    if record.work.evaluation is not None:
        session.add(
            _WorkEvaluationRow(
                work_id=record.work_id,
                evaluation_id=record.work.evaluation.evaluation_id,
            )
        )
    session.add_all(
        _WorkSelectionReferenceRow(
            work_id=record.work_id,
            selection_sha256=selection_sha256,
        )
        for selection_sha256 in sorted(_selection_references((record,)))
    )


def _replace_evaluation_projection(session: Session, record: EvaluationRecord) -> None:
    """Replace rebuildable child membership for one canonical evaluation document."""

    session.execute(
        delete(_EvaluationChildRow).where(_EvaluationChildRow.evaluation_id == record.evaluation_id)
    )
    session.add_all(
        _EvaluationChildRow(evaluation_id=record.evaluation_id, work_id=child.work_id)
        for child in record.children
    )


def _work_relations(record: WorkRecord) -> set[str]:
    related: set[str] = set()
    binding = record.work.fork_join
    if binding is not None:
        related.add(binding.parent_work_id)
    if record.branch_set_plan is not None:
        related.update(branch_work(branch).work_id for branch in record.branch_set_plan.branches)
    if record.join_plan is not None:
        related.add(record.join_plan.work.work_id)
    return related


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
    encoded = event.model_dump_json(exclude_none=True)
    session.add(
        _EventRow(
            created_at=event.time,
            event_bytes=_encoded_bytes(encoded),
            event_json=encoded,
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


def _encoded_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


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


def _work_list_statement(
    *,
    phase: str | None,
    query: str | None,
    sort: WorkSort,
    order: SortOrder,
) -> tuple[list[Any], Select[tuple[_WorkRow]], tuple[Any, ...]]:
    if (
        sort not in _WORK_SORTS
        or order not in _SORT_ORDERS
        or (phase is not None and phase not in _WORK_PHASES)
    ):
        raise ValueError("stove0 work selectors are invalid")
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
    key_columns = tuple(dict.fromkeys((columns[sort], _WorkRow.work_id)))
    return filters, select(_WorkRow).where(*filters), key_columns


def _evaluation_list_statement(
    *,
    phase: str | None,
    query: str | None,
    sort: EvaluationSort,
    order: SortOrder,
) -> tuple[list[Any], Select[tuple[_EvaluationRow]], tuple[Any, ...]]:
    if (
        sort not in _EVALUATION_SORTS
        or order not in _SORT_ORDERS
        or (phase is not None and phase not in _EVALUATION_PHASES)
    ):
        raise ValueError("stove0 evaluation selectors are invalid")
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
    key_columns = tuple(dict.fromkeys((columns[sort], _EvaluationRow.evaluation_id)))
    return filters, select(_EvaluationRow).where(*filters), key_columns


def _page_size(page_size: int) -> None:
    if page_size < 1 or page_size > 100:
        raise ValueError("stove0 page_size is invalid")


def _keyset_statement(
    statement: Select[Any],
    *,
    columns: Sequence[Any],
    position: Sequence[str | int | bool | bytes | None] | None,
    order: str,
    page_size: int,
) -> Select[Any]:
    _page_size(page_size)
    if order not in _SORT_ORDERS or not columns:
        raise ValueError("stove0 browse ordering is invalid")
    if position is not None:
        if len(position) != len(columns):
            raise ValueError("stove0 page token position differs from the ordering")
        comparisons = []
        for index, (column, value) in enumerate(zip(columns, position, strict=True)):
            preceding = [
                (earlier.is_(None) if earlier_value is None else earlier == earlier_value)
                for earlier, earlier_value in zip(columns[:index], position[:index], strict=True)
            ]
            if value is None:
                if order == "desc":
                    comparisons.append(and_(*preceding, column.is_not(None)))
            else:
                comparison = (
                    or_(column > value, column.is_(None)) if order == "asc" else column < value
                )
                comparisons.append(and_(*preceding, comparison))
        if comparisons:
            statement = statement.where(or_(*comparisons))
    ordering = (
        tuple(asc(column).nulls_last() for column in columns)
        if order == "asc"
        else tuple(desc(column).nulls_first() for column in columns)
    )
    return statement.order_by(*ordering).limit(page_size + 1)


def _work_position(row: _WorkRow, *, sort: str) -> tuple[str, ...]:
    value = getattr(row, sort)
    return (value,) if sort == "work_id" else (value, row.work_id)


def _evaluation_position(row: _EvaluationRow, *, sort: str) -> tuple[str, ...]:
    value = getattr(row, sort)
    return (value,) if sort == "evaluation_id" else (value, row.evaluation_id)


def _page(
    records: Sequence[WorkRecord],
    *,
    item_key: str,
    page_size: int,
    next_position: tuple[str, ...] | None,
    sort: str,
    order: str,
    filters: dict[str, object],
) -> dict[str, object]:
    return {
        "page_size": page_size,
        "_next_position": next_position,
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
    page_size: int,
    next_position: tuple[str, ...] | None,
    sort: str,
    order: str,
    filters: dict[str, object],
) -> dict[str, object]:
    return {
        "page_size": page_size,
        "_next_position": next_position,
        "sort": sort,
        "order": order,
        "filters": filters,
        item_key: [
            record.model_dump(mode="json", by_alias=True, exclude_none=True) for record in records
        ],
    }


__all__ = ["SqlAlchemyStateStore"]
