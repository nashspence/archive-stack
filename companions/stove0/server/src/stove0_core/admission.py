"""Durable classification admission through Riverhog's exact public catalog contracts."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Literal, cast

from riverhog_api_client import ApiClient
from riverhog_protocol import (
    CATALOG_SYNC_PAGE_SIZE_MAX,
    CatalogSyncChangePage,
    CatalogSyncCheckpoint,
    CatalogSyncCollectionPage,
    CatalogSyncDelete,
    CatalogSyncDescriptor,
    CatalogSyncUpsert,
)
from riverhog_protocol.errors import RiverhogError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from stove0_operator_contracts import (
    AdmissionCatalog,
    AdmissionIntent,
    AdmissionPolicy,
    AdmissionPolicyCatalogView,
    AdmissionPolicyStatus,
    AdmissionRun,
    AdmissionSort,
    AdmissionState,
    AdmissionView,
    SchedulerFailure,
    SortOrder,
)
from stove0_protocol import CollectionRootRef, WorkflowPreview
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from stove0_core.coordinator import Stove0Coordinator
from stove0_core.persistence import (
    SqlAlchemyStateStore,
    _admission_list_statement,
    _AdmissionCandidateRow,
    _AdmissionMatchRow,
    _AdmissionObservedRevisionRow,
    _AdmissionPolicyRow,
    _keyset_statement,
)
from stove0_core.preview import WorkflowPreviewService
from stove0_core.recipes import RecipePlanner

_POLICY_SCAN_CURSOR = "stove0:classification-admission-policy-scan/v1"
_ADMISSION_LANE_CURSOR = "stove0:classification-admission-lane/v1"
_RESET_ERRORS = frozenset(
    {
        "catalog_sync_cursor_expired",
        "catalog_sync_history_expired",
        "catalog_sync_source_changed",
        "catalog_sync_view_changed",
        "precondition_failed",
        "forbidden",
        "unauthorized",
    }
)


class _AdmissionAuthorityChanged(RuntimeError):
    pass


class ClassificationAdmissionService:
    """Own exact admission decisions without extending Riverhog's ontology."""

    def __init__(
        self,
        *,
        catalog: AdmissionCatalog,
        riverhog: ApiClient,
        state: SqlAlchemyStateStore,
        planner: RecipePlanner,
        preview: WorkflowPreviewService,
        coordinator: Stove0Coordinator,
    ) -> None:
        self.catalog = catalog
        self.riverhog = riverhog
        self.state = state
        self.planner = planner
        self.preview = preview
        self.coordinator = coordinator
        self._policies = {policy.id: policy for policy in catalog.policies}
        self._validate_recipes()
        self._synchronize_policy_rows()

    def advance(self, *, limit: int = 25) -> AdmissionRun:
        if isinstance(limit, bool) or not 1 <= limit <= CATALOG_SYNC_PAGE_SIZE_MAX:
            raise ValueError("classification admission limit is outside the v1 bound")
        progressed: list[str] = []
        failures: list[SchedulerFailure] = []
        saved_lane = self.state.load_cursor(_ADMISSION_LANE_CURSOR)
        lane, lane_revision = saved_lane if saved_lane is not None else ("candidate", None)
        if lane not in {"candidate", "policy"}:
            raise RuntimeError("classification admission lane cursor is invalid")
        for _ in range(limit):
            preferred = lane
            fallback = "policy" if preferred == "candidate" else "candidate"
            performed = False
            for selected_lane in (preferred, fallback):
                if selected_lane == "candidate":
                    candidates = self._pending_candidates(limit=1)
                    if not candidates:
                        continue
                    admission_id = candidates[0]
                    try:
                        self._advance_candidate(admission_id)
                        progressed.append(f"admission:{admission_id}")
                    except Exception as exc:
                        self._record_candidate_failure(admission_id, exc)
                        failures.append(
                            SchedulerFailure(
                                event_id=f"admission:{admission_id}",
                                error=f"{type(exc).__name__}: {exc}"[:1000],
                            )
                        )
                    performed = True
                    lane = "policy"
                    break
                selected, _cursor, cursor_revision = self._policy_slice(limit=1)
                if not selected:
                    continue
                policy = selected[0]
                try:
                    advanced = self._advance_policy(policy)
                    if advanced:
                        progressed.append(f"policy:{policy.id}")
                except Exception as exc:
                    failures.append(
                        SchedulerFailure(
                            event_id=f"admission-policy:{policy.id}",
                            error=f"{type(exc).__name__}: {exc}"[:1000],
                        )
                    )
                    advanced = True
                self._store_cursor(
                    _POLICY_SCAN_CURSOR,
                    expected_revision=cursor_revision,
                    value=policy.id,
                )
                if not advanced:
                    continue
                performed = True
                lane = "candidate"
                break
            if not performed:
                break
        self._store_cursor(
            _ADMISSION_LANE_CURSOR,
            expected_revision=lane_revision,
            value=lane,
        )
        return AdmissionRun(progressed=tuple(progressed), failures=tuple(failures))

    def _store_cursor(
        self,
        stream: str,
        *,
        expected_revision: int | None,
        value: str,
    ) -> None:
        try:
            self.state.compare_and_swap_cursor(
                stream,
                expected_revision=expected_revision,
                cursor=value,
            )
        except Exception:
            current = self.state.load_cursor(stream)
            if current is None or current[0] != value:
                raise

    def policies(self) -> AdmissionPolicyCatalogView:
        with self.state.sessions() as session:
            statuses: list[AdmissionPolicyStatus] = []
            for policy in self.catalog.policies:
                row = session.get(_AdmissionPolicyRow, policy.id)
                if row is None:
                    raise RuntimeError("configured admission policy state is unavailable")
                statuses.append(_policy_status(policy, row))
        return AdmissionPolicyCatalogView(
            catalog_sha256=self.catalog.catalog_sha256,
            policies=tuple(statuses),
        )

    def rebaseline(
        self,
        policy_id: str,
        *,
        mode: Literal["observe", "backfill"] = "observe",
    ) -> AdmissionPolicyStatus:
        policy = self._policy(policy_id)
        checkpoint = self.riverhog.create_catalog_sync_checkpoint()
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionPolicyRow, policy.id, with_for_update=True)
            if row is None or row.policy_sha256 != policy.policy_sha256:
                raise RuntimeError("admission policy state differs from configuration")
            _bind_checkpoint(row, checkpoint, mode=mode)
            return _policy_status(policy, row)

    def list_admissions(
        self,
        *,
        page_size: int,
        position: tuple[str | int | bool | bytes | None, ...] | None,
        policy_id: str | None,
        state: AdmissionState | None,
        query: str | None,
        sort: AdmissionSort,
        order: SortOrder,
    ) -> dict[str, object]:
        if isinstance(page_size, bool) or not 1 <= page_size <= 100:
            raise ValueError("admission page size is outside the v1 bound")
        _, statement, key_columns = _admission_list_statement(
            policy_id=policy_id,
            state=state,
            query=query,
            sort=sort,
            order=order,
        )
        with self.state.sessions() as session:
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
            visible = rows[:page_size]
            next_position = None
            if len(rows) > page_size and visible:
                value = getattr(visible[-1], sort)
                next_position = (
                    (value,) if sort == "admission_id" else (value, visible[-1].admission_id)
                )
            return {
                "page_size": page_size,
                "sort": sort,
                "order": order,
                "filters": {
                    "policy_id": policy_id,
                    "state": state,
                    "query": query,
                },
                "policy_id": policy_id,
                "state": state,
                "admissions": tuple(_admission_view(row) for row in visible),
                "_next_position": next_position,
            }

    def get_admission(self, admission_id: str) -> AdmissionView:
        with self.state.sessions() as session:
            row = session.get(_AdmissionCandidateRow, admission_id)
            if row is None:
                raise KeyError(admission_id)
            return _admission_view(row)

    def _validate_recipes(self) -> None:
        for policy in self.catalog.policies:
            recipe = self.planner.catalog.recipe(policy.recipe_id, policy.recipe_revision)
            if recipe.sha256 != policy.recipe_sha256:
                raise ValueError(f"admission policy {policy.id} pins another recipe identity")

    def _synchronize_policy_rows(self) -> None:
        now = utc_timestamp_now()
        with self.state.sessions() as session, session.begin():
            dialect = session.get_bind().dialect.name
            insert_policy: Any
            if dialect == "postgresql":
                insert_policy = postgresql_insert
            elif dialect == "sqlite":
                insert_policy = sqlite_insert
            else:
                raise RuntimeError(f"unsupported Stove0 state-store dialect: {dialect}")
            for policy in self.catalog.policies:
                session.execute(
                    insert_policy(_AdmissionPolicyRow)
                    .values(
                        policy_id=policy.id,
                        policy_revision=policy.revision,
                        policy_sha256=policy.policy_sha256,
                        phase="new",
                        generation=secrets.token_hex(32),
                        source_identity=None,
                        authorization_view_identity=None,
                        cursor=None,
                        baseline_mode="observe",
                        through_revision="0",
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[_AdmissionPolicyRow.policy_id])
                )
                row = session.get(_AdmissionPolicyRow, policy.id, with_for_update=True)
                if row is None:
                    raise RuntimeError("admission policy state is unavailable after insertion")
                if row.policy_sha256 == policy.policy_sha256:
                    continue
                if policy.revision <= row.policy_revision:
                    raise ValueError(
                        f"admission policy {policy.id} changed without a higher revision"
                    )
                row.policy_revision = policy.revision
                row.policy_sha256 = policy.policy_sha256
                row.phase = "reset_required"
                row.generation = secrets.token_hex(32)
                row.source_identity = None
                row.authorization_view_identity = None
                row.cursor = None
                row.baseline_mode = "observe"
                row.through_revision = "0"
                row.updated_at = now

    def _policy_slice(self, *, limit: int) -> tuple[list[AdmissionPolicy], str, int | None]:
        saved = self.state.load_cursor(_POLICY_SCAN_CURSOR)
        cursor, revision = saved if saved is not None else ("", None)
        ordered = list(self.catalog.policies)
        selected = [policy for policy in ordered if policy.id > cursor][:limit]
        if not selected and cursor:
            selected = ordered[:limit]
        return selected, cursor, revision

    def _advance_policy(self, policy: AdmissionPolicy) -> bool:
        with self.state.sessions() as session:
            row = session.get(_AdmissionPolicyRow, policy.id)
            if row is None:
                raise RuntimeError("admission policy state is unavailable")
            phase = row.phase
            cursor = row.cursor
        if phase == "reset_required":
            return False
        if phase == "new":
            checkpoint = self.riverhog.create_catalog_sync_checkpoint()
            with self.state.sessions() as session, session.begin():
                current = session.get(_AdmissionPolicyRow, policy.id, with_for_update=True)
                if current is None or current.phase != "new":
                    return False
                _bind_checkpoint(current, checkpoint, mode="observe")
            return True
        if not isinstance(cursor, str):
            raise RuntimeError("active admission policy has no catalog cursor")
        try:
            if phase == "baseline":
                catalog_page = self.riverhog.list_catalog_sync_collections(
                    cursor,
                    limit=CATALOG_SYNC_PAGE_SIZE_MAX,
                )
                matches = [(item, self._matches(policy, item)) for item in catalog_page.collections]
                return self._commit_baseline_page(
                    policy,
                    cursor=cursor,
                    page=catalog_page,
                    matches=matches,
                )
            if phase == "following":
                change_page = self.riverhog.list_catalog_sync_changes(cursor, limit=1)
                evaluated: tuple[CatalogSyncUpsert, bool] | None = None
                if change_page.changes and isinstance(change_page.changes[0], CatalogSyncUpsert):
                    evaluated = (
                        change_page.changes[0],
                        self._matches(policy, change_page.changes[0]),
                    )
                return self._commit_change_page(
                    policy,
                    cursor=cursor,
                    page=change_page,
                    evaluated=evaluated,
                )
        except RiverhogError as exc:
            if exc.code in _RESET_ERRORS:
                self._require_rebaseline(policy.id)
            raise
        except _AdmissionAuthorityChanged:
            self._require_rebaseline(policy.id)
            raise
        raise RuntimeError("admission policy phase is invalid")

    def _matches(self, policy: AdmissionPolicy, descriptor: CatalogSyncDescriptor) -> bool:
        for tag in policy.required_tags:
            response = self.riverhog.collection_contains_tag(
                descriptor.collection_id,
                tag=tag,
                revision=descriptor.tag_revision,
                tag_set_identity=descriptor.tag_set_identity,
            )
            if (
                response.get("collection_id") != descriptor.collection_id
                or response.get("revision") != descriptor.tag_revision
                or response.get("tag_set_identity") != descriptor.tag_set_identity
                or response.get("tag") != tag
                or not isinstance(response.get("present"), bool)
            ):
                raise RuntimeError("Riverhog tag membership response changed its authority")
            if not response["present"]:
                return False
        return True

    def _commit_baseline_page(
        self,
        policy: AdmissionPolicy,
        *,
        cursor: str,
        page: CatalogSyncCollectionPage,
        matches: Sequence[tuple[CatalogSyncDescriptor, bool]],
    ) -> bool:
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionPolicyRow, policy.id, with_for_update=True)
            next_cursor = page.next_cursor or page.changes_cursor
            next_phase = "baseline" if page.next_cursor is not None else "following"
            if (
                row is not None
                and row.policy_sha256 == policy.policy_sha256
                and row.source_identity == page.source_identity
                and row.authorization_view_identity == page.authorization_view_identity
                and row.cursor == next_cursor
                and row.phase == next_phase
            ):
                return False
            self._require_page_authority(row, policy, cursor, page)
            assert row is not None
            for descriptor, matched in matches:
                recorded = self._record_match(session, row, descriptor, matched)
                if recorded == "applied" and matched and row.baseline_mode == "backfill":
                    self._record_intent(session, policy, descriptor)
            row.cursor = next_cursor
            row.phase = next_phase
            row.updated_at = utc_timestamp_now()
            return True

    def _commit_change_page(
        self,
        policy: AdmissionPolicy,
        *,
        cursor: str,
        page: CatalogSyncChangePage,
        evaluated: tuple[CatalogSyncUpsert, bool] | None,
    ) -> bool:
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionPolicyRow, policy.id, with_for_update=True)
            if (
                row is not None
                and row.policy_sha256 == policy.policy_sha256
                and row.source_identity == page.source_identity
                and row.authorization_view_identity == page.authorization_view_identity
                and row.cursor == page.next_cursor
                and row.phase == "following"
                and row.through_revision == page.through_revision
            ):
                return False
            self._require_page_authority(row, policy, cursor, page)
            assert row is not None
            if len(page.changes) > 1:
                raise RuntimeError("admission change step exceeded its one-change transaction")
            if page.changes:
                change = page.changes[0]
                prior = session.get(
                    _AdmissionMatchRow,
                    (policy.id, row.generation, change.collection_id),
                )
                if isinstance(change, CatalogSyncDelete):
                    recorded = self._observe_revision(session, row, change)
                    if recorded == "applied" and prior is not None:
                        session.delete(prior)
                else:
                    if evaluated is None or evaluated[0] != change:
                        raise RuntimeError("admission change was not evaluated exactly")
                    matched = evaluated[1]
                    prior_matched = prior is not None and prior.matched
                    recorded = self._record_match(session, row, change, matched)
                    eligible = recorded == "applied" and matched and not prior_matched
                    if eligible:
                        self._record_intent(session, policy, change)
            row.cursor = page.next_cursor
            row.through_revision = page.through_revision
            row.updated_at = utc_timestamp_now()
            return bool(page.changes or page.next_cursor != cursor)

    def _record_match(
        self,
        session: Any,
        policy_row: _AdmissionPolicyRow,
        descriptor: CatalogSyncDescriptor,
        matched: bool,
    ) -> Literal["applied", "duplicate", "stale"]:
        observed = self._observe_revision(session, policy_row, descriptor)
        if observed != "applied":
            return observed
        encoded = json.dumps(
            descriptor.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        row = session.get(
            _AdmissionMatchRow,
            (policy_row.policy_id, policy_row.generation, descriptor.collection_id),
        )
        if row is None:
            session.add(
                _AdmissionMatchRow(
                    policy_id=policy_row.policy_id,
                    generation=policy_row.generation,
                    collection_id=descriptor.collection_id,
                    matched=matched,
                    descriptor_revision=descriptor.revision,
                    tag_revision=descriptor.tag_revision,
                    tag_set_identity=descriptor.tag_set_identity,
                    document_bytes=len(encoded.encode("utf-8")),
                    document_json=encoded,
                )
            )
            return "applied"
        row.matched = matched
        row.descriptor_revision = descriptor.revision
        row.tag_revision = descriptor.tag_revision
        row.tag_set_identity = descriptor.tag_set_identity
        row.document_bytes = len(encoded.encode("utf-8"))
        row.document_json = encoded
        return "applied"

    def _observe_revision(
        self,
        session: Any,
        policy_row: _AdmissionPolicyRow,
        change: CatalogSyncDescriptor | CatalogSyncDelete,
    ) -> Literal["applied", "duplicate", "stale"]:
        operation = "delete" if isinstance(change, CatalogSyncDelete) else "upsert"
        payload = {"operation": operation, **change.model_dump(mode="json")}
        authority_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key = (policy_row.policy_id, policy_row.generation, change.collection_id)
        observed = session.get(_AdmissionObservedRevisionRow, key)
        revision = int(change.revision)
        if observed is not None:
            prior_revision = int(observed.descriptor_revision)
            if revision < prior_revision:
                return "stale"
            if revision == prior_revision:
                if observed.operation != operation or observed.authority_sha256 != authority_sha256:
                    raise RuntimeError("Riverhog catalog revision changed its exact authority")
                return "duplicate"
            observed.descriptor_revision = change.revision
            observed.operation = operation
            observed.authority_sha256 = authority_sha256
            return "applied"
        session.add(
            _AdmissionObservedRevisionRow(
                policy_id=policy_row.policy_id,
                generation=policy_row.generation,
                collection_id=change.collection_id,
                descriptor_revision=change.revision,
                operation=operation,
                authority_sha256=authority_sha256,
            )
        )
        return "applied"

    def _record_intent(
        self,
        session: Any,
        policy: AdmissionPolicy,
        descriptor: CatalogSyncDescriptor,
    ) -> None:
        intent = AdmissionIntent.seal(policy=policy, collection=descriptor)
        if session.get(_AdmissionCandidateRow, intent.admission_id) is not None:
            return
        encoded = json.dumps(
            intent.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        now = utc_timestamp_now()
        session.add(
            _AdmissionCandidateRow(
                admission_id=intent.admission_id,
                policy_id=policy.id,
                state="intent",
                preview_sha256=None,
                work_id=None,
                document_bytes=len(encoded.encode("utf-8")),
                document_json=encoded,
                preview_bytes=None,
                preview_json=None,
                attempt_count=0,
                next_attempt_at=now,
                failure=None,
                created_at=now,
                updated_at=now,
            )
        )

    def _pending_candidates(self, *, limit: int) -> tuple[str, ...]:
        if limit < 1 or not self._policies:
            return ()
        with self.state.sessions() as session:
            return tuple(
                session.scalars(
                    select(_AdmissionCandidateRow.admission_id)
                    .where(
                        _AdmissionCandidateRow.state.in_(("intent", "previewed")),
                        _AdmissionCandidateRow.next_attempt_at <= utc_timestamp_now(),
                    )
                    .order_by(
                        _AdmissionCandidateRow.next_attempt_at,
                        _AdmissionCandidateRow.admission_id,
                    )
                    .limit(limit)
                )
            )

    def _advance_candidate(self, admission_id: str) -> None:
        with self.state.sessions() as session:
            row = session.get(_AdmissionCandidateRow, admission_id)
            if row is None or row.state == "work_bound":
                return
            state = row.state
            intent = AdmissionIntent.model_validate_json(row.document_json)
            preview_json = row.preview_json
        work = self.planner.create_work(
            intent.recipe_id,
            (
                CollectionRootRef(
                    collection_id=intent.collection.collection_id,
                    archive_root_sha256=intent.collection.archive_root_sha256,
                    content_identity=intent.collection.content_identity,
                ),
            ),
            revision=intent.recipe_revision,
            effective_intent=intent.effective_intent,
        )
        if work.recipe.sha256 != intent.recipe_sha256:
            raise RuntimeError("admission intent recipe is no longer available exactly")
        existing = self.state.load(work.work_id)
        if existing is not None:
            acceptance = existing.preview_acceptance
            if acceptance is None:
                raise RuntimeError("existing semantic work has no accepted preview")
            self._bind_candidate(
                admission_id,
                expected_state=cast(AdmissionState, state),
                preview_sha256=acceptance.preview_sha256,
                work_id=work.work_id,
            )
            return
        if state == "intent":
            preview = self.preview.preview(work)
            if preview.state != "ready":
                raise RuntimeError(f"automatic preview is not acceptable: {preview.state}")
            encoded = json.dumps(
                preview.model_dump(mode="json", exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
            )
            with self.state.sessions() as session, session.begin():
                current = session.get(_AdmissionCandidateRow, admission_id, with_for_update=True)
                if current is None or current.state != "intent":
                    return
                current.state = "previewed"
                current.preview_sha256 = preview.preview_sha256
                current.preview_bytes = len(encoded.encode("utf-8"))
                current.preview_json = encoded
                current.attempt_count = 0
                current.next_attempt_at = utc_timestamp_now()
                current.failure = None
                current.updated_at = utc_timestamp_now()
            return
        if state != "previewed" or preview_json is None:
            raise RuntimeError("admission candidate stage is invalid")
        preview = WorkflowPreview.model_validate_json(preview_json)
        if preview.state != "ready" or preview.work != work:
            raise RuntimeError("accepted admission preview differs from semantic work")
        record = self.coordinator.create_or_resume(work, preview=preview)
        self._bind_candidate(
            admission_id,
            expected_state="previewed",
            preview_sha256=preview.preview_sha256,
            work_id=record.work.work_id,
        )

    def _bind_candidate(
        self,
        admission_id: str,
        *,
        expected_state: AdmissionState,
        preview_sha256: str,
        work_id: str,
    ) -> None:
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionCandidateRow, admission_id, with_for_update=True)
            if row is None:
                raise RuntimeError("admission candidate disappeared")
            if row.state == "work_bound":
                if row.work_id != work_id or row.preview_sha256 != preview_sha256:
                    raise RuntimeError("admission work binding changed")
                return
            if row.state != expected_state:
                raise RuntimeError("admission candidate advanced concurrently")
            row.state = "work_bound"
            row.preview_sha256 = preview_sha256
            row.work_id = work_id
            row.attempt_count = 0
            row.next_attempt_at = None
            row.failure = None
            row.updated_at = utc_timestamp_now()

    def _record_candidate_failure(self, admission_id: str, exc: Exception) -> None:
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionCandidateRow, admission_id, with_for_update=True)
            if row is None or row.state == "work_bound":
                return
            row.attempt_count += 1
            delay = min(3600, 2 ** min(row.attempt_count, 10))
            row.next_attempt_at = format_utc_timestamp(utc_now() + timedelta(seconds=delay))
            row.failure = f"{type(exc).__name__}: {exc}"[:1000]
            row.updated_at = utc_timestamp_now()

    def _require_page_authority(
        self,
        row: _AdmissionPolicyRow | None,
        policy: AdmissionPolicy,
        cursor: str,
        page: CatalogSyncCollectionPage | CatalogSyncChangePage,
    ) -> None:
        if (
            row is None
            or row.policy_sha256 != policy.policy_sha256
            or row.cursor != cursor
            or row.source_identity != page.source_identity
            or row.authorization_view_identity != page.authorization_view_identity
        ):
            raise _AdmissionAuthorityChanged(
                "admission catalog authority changed; rebaseline is required"
            )

    def _require_rebaseline(self, policy_id: str) -> None:
        with self.state.sessions() as session, session.begin():
            row = session.get(_AdmissionPolicyRow, policy_id, with_for_update=True)
            if row is not None:
                row.phase = "reset_required"
                row.updated_at = utc_timestamp_now()

    def _policy(self, policy_id: str) -> AdmissionPolicy:
        try:
            return self._policies[policy_id]
        except KeyError as exc:
            raise KeyError(policy_id) from exc


def _bind_checkpoint(
    row: _AdmissionPolicyRow,
    checkpoint: CatalogSyncCheckpoint,
    *,
    mode: Literal["observe", "backfill"],
) -> None:
    row.phase = "baseline"
    row.generation = secrets.token_hex(32)
    row.source_identity = checkpoint.source_identity
    row.authorization_view_identity = checkpoint.authorization_view_identity
    row.cursor = checkpoint.catalog_cursor
    row.baseline_mode = mode
    row.through_revision = "0"
    row.updated_at = utc_timestamp_now()


def _policy_status(policy: AdmissionPolicy, row: _AdmissionPolicyRow) -> AdmissionPolicyStatus:
    return AdmissionPolicyStatus(
        policy=policy,
        policy_sha256=row.policy_sha256,
        phase=cast(Any, row.phase),
        source_identity=row.source_identity,
        authorization_view_identity=row.authorization_view_identity,
        baseline_mode=cast(Any, row.baseline_mode),
        through_revision=row.through_revision,
        updated_at=row.updated_at,
    )


def _admission_view(row: _AdmissionCandidateRow) -> AdmissionView:
    return AdmissionView(
        intent=AdmissionIntent.model_validate_json(row.document_json),
        state=cast(Any, row.state),
        preview_sha256=row.preview_sha256,
        work_id=row.work_id,
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
        failure=row.failure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["ClassificationAdmissionService"]
