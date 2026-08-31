"""Generic collection work claims, scoped capabilities, and derivation verification."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

from http_api_contracts import closed_literal_values
from riverhog_protocol import (
    ClaimState,
    ProcessingClaimSort,
    RetirementClaimReferenceDocument,
    SortOrder,
)
from riverhog_protocol.collection_workflow_transport import (
    CONTROLLER_EVIDENCE_MAX_BYTES,
    WORK_DOCUMENT_MAX_BYTES,
    ExactSetAuthorityDocument,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
    CollectionArtifactIdentity,
    CollectionDerivation,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
    DispositionState,
    OperationIdentity,
    canonical_json_bytes,
    canonical_json_sha256,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, InvalidState, NotFound
from riverhog_protocol.transport import COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX
from sqlalchemy import and_, asc, delete, desc, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTION_TRANSFORMS_EXECUTE,
    COLLECTIONS_CREATE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
    collection_resource,
)
from riverhog_core.browse import bounded_page, keyset_statement, validate_page_size
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import (
    CollectionDerivationRecord,
    CollectionProcessingClaimArtifactRecord,
    CollectionProcessingClaimInputRecord,
    CollectionProcessingClaimOutputTagRecord,
    CollectionProcessingClaimRecord,
    CollectionProcessingDispositionOutputRecord,
    CollectionProcessingDispositionRecord,
    CollectionProcessingDispositionSetRecord,
    CollectionProcessingOutcomeRecord,
    CollectionTransformCapabilityArtifactRecord,
    CollectionTransformCapabilityRecord,
)
from riverhog_core.checkpoint_sha256 import CheckpointSHA256
from riverhog_core.runtime_config import RuntimeConfig

_MIN_LEASE_SECONDS = 30
_MAX_LEASE_SECONDS = 24 * 60 * 60
_DEFAULT_LEASE_SECONDS = 30 * 60
_CAPABILITY_ACTIONS = frozenset({"read-inputs", "write-output"})
_CAPABILITY_AUDIENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,299}$", re.ASCII)
_RETIREMENT_POLICIES = frozenset({"retain", "retire-after-verified-output"})
_CLAIM_STATES = closed_literal_values(ClaimState)
_CLAIM_SORT_NAMES = closed_literal_values(ProcessingClaimSort)
_SORT_ORDERS = closed_literal_values(SortOrder)
_DISPOSITION_BATCH_MAX = 128
_DISPOSITION_VALIDATION_BATCH = 128
_CLAIM_SORT_FIELDS = {
    "created_at": CollectionProcessingClaimRecord.created_at,
    "updated_at": CollectionProcessingClaimRecord.updated_at,
    "expires_at": CollectionProcessingClaimRecord.expires_at,
    "state": CollectionProcessingClaimRecord.state,
    "work_id": CollectionProcessingClaimRecord.work_id,
    "execution_id": CollectionProcessingClaimRecord.execution_id,
}


class SqlAlchemyCollectionWorkflowService:
    """Own Riverhog's generic, content-opaque collection-processing authority."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def create_or_resume_claim(
        self,
        *,
        work_id: str,
        work_document: Mapping[str, object],
        work_document_sha256: str,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        purpose: str = "collection-work/v1",
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        lease = _lease_seconds(lease_seconds)
        normalized_work_id = _sha256(work_id, "work identity")
        normalized_purpose = _visible(purpose, "claim purpose", maximum=160)
        encoded_work, normalized_work = _json_document(
            work_document,
            label="work document",
            maximum_bytes=WORK_DOCUMENT_MAX_BYTES,
        )
        normalized_work_sha256 = _sha256(
            work_document_sha256,
            "work document identity",
        )
        if hashlib.sha256(encoded_work).hexdigest() != normalized_work_sha256:
            raise BadRequest("work document identity does not match its canonical JSON")
        claim_id = canonical_json_sha256(
            {
                "format": "riverhog-processing-claim-identity/v1",
                "consumer_app": principal.app,
                "purpose": normalized_purpose,
                "work_id": normalized_work_id,
            }
        )
        now = utc_timestamp_now()
        expires_at = format_utc_timestamp(utc_now() + timedelta(seconds=lease))
        with session_scope(self._session_factory) as session:
            claim = session.scalar(
                select(CollectionProcessingClaimRecord)
                .where(CollectionProcessingClaimRecord.id == claim_id)
                .with_for_update()
            )
            if claim is not None:
                _require_same_claim(
                    session,
                    claim,
                    work_id=normalized_work_id,
                    purpose=normalized_purpose,
                    work_document_json=encoded_work.decode("utf-8"),
                    work_document_sha256=normalized_work_sha256,
                    principal=principal,
                )
                if claim.state == "active" and _expired(claim.expires_at):
                    _require_no_execution_output(session, claim)
                    claim.fence += 1
                    claim.expires_at = expires_at
                    claim.updated_at = now
                    _clear_plan(session, claim)
                    _revoke_capabilities(session, claim.id, now=now)
                elif claim.state == "active" and parse_utc_timestamp(
                    expires_at
                ) > parse_utc_timestamp(claim.expires_at):
                    claim.expires_at = expires_at
                    claim.updated_at = now
                return _claim_payload(session, claim)

            claim = CollectionProcessingClaimRecord(
                id=claim_id,
                work_id=normalized_work_id,
                consumer_app=principal.app,
                consumer_key_id=principal.key_id,
                purpose=normalized_purpose,
                work_document_json=json.dumps(
                    normalized_work,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                work_document_sha256=normalized_work_sha256,
                retirement_grace_seconds=0,
                state="active",
                fence=1,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            try:
                with session.begin_nested():
                    session.add(claim)
                    session.flush()
            except IntegrityError:
                claim = session.scalar(
                    select(CollectionProcessingClaimRecord)
                    .where(CollectionProcessingClaimRecord.id == claim_id)
                    .with_for_update()
                )
                if claim is None:
                    raise
                _require_same_claim(
                    session,
                    claim,
                    work_id=normalized_work_id,
                    purpose=normalized_purpose,
                    work_document_json=encoded_work.decode("utf-8"),
                    work_document_sha256=normalized_work_sha256,
                    principal=principal,
                )
                if claim.state == "active" and parse_utc_timestamp(
                    expires_at
                ) > parse_utc_timestamp(claim.expires_at):
                    claim.expires_at = expires_at
                    claim.updated_at = now
            return _claim_payload(session, claim)

    def append_claim_inputs(
        self,
        claim_id: str,
        *,
        fence: int,
        start_ordinal: int,
        inputs: Sequence[CollectionRootIdentity],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Append one bounded, retry-safe canonical input-root batch."""

        values = tuple(inputs)
        _bounded_batch(values, "input root")
        if values != tuple(sorted(values)) or len({item.collection_id for item in values}) != len(
            values
        ):
            raise BadRequest("input roots must be unique and canonically ordered")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if claim.inputs_sealed_at is not None:
                return _input_set_payload(claim)
            ordinal = _append_start(start_ordinal, claim.input_count)
            checkpoint = _set_checkpoint(claim.input_hash_state, "claim-inputs")
            for value in values:
                if ordinal < claim.input_count:
                    current = session.scalar(
                        select(CollectionProcessingClaimInputRecord).where(
                            CollectionProcessingClaimInputRecord.claim_id == claim.id,
                            CollectionProcessingClaimInputRecord.collection_order == ordinal,
                        )
                    )
                    if current is None or _input_identity(current) != value:
                        raise Conflict("input-root retry differs from staged authority")
                    ordinal += 1
                    continue
                previous = _last_input_identity(session, claim.id)
                if previous is not None and value <= previous:
                    raise BadRequest("input roots must remain canonically ordered across batches")
                if (
                    session.get(
                        CollectionProcessingClaimInputRecord,
                        (claim.id, value.collection_id),
                    )
                    is not None
                ):
                    raise Conflict("input collection is already staged")
                if _collection_root(session, value.collection_id, lock=True) != value:
                    raise Conflict(
                        f"input collection root differs from the claimed identity: "
                        f"{value.collection_id}"
                    )
                session.add(
                    CollectionProcessingClaimInputRecord(
                        claim_id=claim.id,
                        collection_id=value.collection_id,
                        collection_order=ordinal,
                        archive_root_sha256=value.archive_root_sha256,
                        content_identity=value.content_identity,
                    )
                )
                _checkpoint_item(checkpoint, value.as_dict())
                claim.input_count += 1
                ordinal += 1
                session.flush()
            claim.input_hash_state = checkpoint.export_state()
            claim.updated_at = utc_timestamp_now()
            return _input_set_payload(claim)

    def seal_claim_inputs(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if claim.inputs_sealed_at is None:
                if claim.input_count < 1 or claim.input_hash_state is None:
                    raise Conflict("input-root authority is empty")
                claim.input_set_sha256 = CheckpointSHA256.from_state(
                    claim.input_hash_state
                ).hexdigest()
                claim.inputs_sealed_at = utc_timestamp_now()
                claim.updated_at = claim.inputs_sealed_at
            return _input_set_payload(claim)

    def list_claim_inputs(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        with read_snapshot(self._session_factory) as session:
            claim = _claim_actor(session, claim_id, principal)
            authority = _require_set_authority(
                claim.input_count,
                claim.input_set_sha256,
                authority_sha256,
                "input-root",
            )
            rows = list(
                session.scalars(
                    select(CollectionProcessingClaimInputRecord)
                    .where(
                        CollectionProcessingClaimInputRecord.claim_id == claim.id,
                        CollectionProcessingClaimInputRecord.collection_order >= start,
                    )
                    .order_by(CollectionProcessingClaimInputRecord.collection_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": authority,
                "start_ordinal": start,
                "next_ordinal": next_ordinal if next_ordinal < claim.input_count else None,
                "inputs": [_input_identity(item).as_dict() for item in rows],
            }

    def append_claim_artifacts(
        self,
        claim_id: str,
        *,
        fence: int,
        start_ordinal: int,
        artifacts: Sequence[CollectionArtifactIdentity],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        values = tuple(artifacts)
        _bounded_batch(values, "artifact")
        if values != tuple(sorted(values)):
            raise BadRequest("artifacts must be unique and canonically ordered")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            _require_inputs_sealed(claim)
            if claim.artifacts_sealed_at is not None:
                return _artifact_set_payload(claim)
            ordinal = _append_start(start_ordinal, claim.artifact_count)
            checkpoint = _set_checkpoint(claim.artifact_hash_state, "claim-artifacts")
            for value in values:
                if ordinal < claim.artifact_count:
                    current = session.scalar(
                        select(CollectionProcessingClaimArtifactRecord).where(
                            CollectionProcessingClaimArtifactRecord.claim_id == claim.id,
                            CollectionProcessingClaimArtifactRecord.artifact_order == ordinal,
                        )
                    )
                    if current is None or _artifact_identity(session, claim.id, current) != value:
                        raise Conflict("artifact retry differs from staged authority")
                    ordinal += 1
                    continue
                previous = _last_artifact_identity(session, claim.id)
                if previous is not None and value <= previous:
                    raise BadRequest("artifacts must remain canonically ordered across batches")
                _validate_claim_artifacts(session, claim, (value,))
                existing = session.get(
                    CollectionProcessingClaimArtifactRecord,
                    (claim.id, value.collection.collection_id, value.path),
                )
                if existing is not None:
                    raise Conflict("artifact is already staged")
                session.add(
                    CollectionProcessingClaimArtifactRecord(
                        claim_id=claim.id,
                        collection_id=value.collection.collection_id,
                        path=value.path,
                        artifact_order=ordinal,
                        bytes=value.bytes,
                        sha256=value.sha256,
                    )
                )
                _checkpoint_item(checkpoint, value.as_dict())
                claim.artifact_count += 1
                claim.artifact_bytes += value.bytes
                ordinal += 1
                session.flush()
            claim.artifact_hash_state = checkpoint.export_state()
            claim.updated_at = utc_timestamp_now()
            return _artifact_set_payload(claim)

    def seal_claim_artifacts(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if claim.artifacts_sealed_at is None:
                if claim.artifact_count < 1 or claim.artifact_hash_state is None:
                    raise Conflict("artifact authority is empty")
                claim.artifact_set_sha256 = CheckpointSHA256.from_state(
                    claim.artifact_hash_state
                ).hexdigest()
                claim.artifacts_sealed_at = utc_timestamp_now()
                claim.updated_at = claim.artifacts_sealed_at
            return _artifact_set_payload(claim)

    def list_claim_artifacts(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        with read_snapshot(self._session_factory) as session:
            claim = _claim_actor(session, claim_id, principal)
            authority = _require_artifact_authority(claim, authority_sha256)
            rows = list(
                session.scalars(
                    select(CollectionProcessingClaimArtifactRecord)
                    .where(
                        CollectionProcessingClaimArtifactRecord.claim_id == claim.id,
                        CollectionProcessingClaimArtifactRecord.artifact_order >= start,
                    )
                    .order_by(CollectionProcessingClaimArtifactRecord.artifact_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": authority,
                "start_ordinal": start,
                "next_ordinal": next_ordinal if next_ordinal < claim.artifact_count else None,
                "artifacts": [
                    _artifact_identity(session, claim.id, item).as_dict() for item in rows
                ],
            }

    def append_claim_output_tags(
        self,
        claim_id: str,
        *,
        fence: int,
        start_ordinal: int,
        tags: Sequence[str],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        values = tuple(str(item) for item in tags)
        _bounded_batch(values, "output tag")
        if values != tuple(sorted(set(values))):
            raise BadRequest("output tags must be unique and canonically ordered")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if claim.output_tags_sealed_at is not None:
                return _output_tag_set_payload(claim)
            ordinal = _append_start(start_ordinal, claim.output_tag_count)
            checkpoint = _tag_checkpoint(claim.output_tag_hash_state)
            for value in values:
                if session.get(TagRecord, value) is None:
                    raise BadRequest(f"output tag does not exist: {value}")
                if ordinal < claim.output_tag_count:
                    current = session.scalar(
                        select(CollectionProcessingClaimOutputTagRecord).where(
                            CollectionProcessingClaimOutputTagRecord.claim_id == claim.id,
                            CollectionProcessingClaimOutputTagRecord.tag_order == ordinal,
                        )
                    )
                    if current is None or current.tag != value:
                        raise Conflict("output-tag retry differs from staged authority")
                    ordinal += 1
                    continue
                previous = session.scalar(
                    select(CollectionProcessingClaimOutputTagRecord.tag)
                    .where(CollectionProcessingClaimOutputTagRecord.claim_id == claim.id)
                    .order_by(CollectionProcessingClaimOutputTagRecord.tag_order.desc())
                    .limit(1)
                )
                if previous is not None and value <= previous:
                    raise BadRequest("output tags must remain canonically ordered across batches")
                session.add(
                    CollectionProcessingClaimOutputTagRecord(
                        claim_id=claim.id,
                        tag=value,
                        tag_order=ordinal,
                    )
                )
                _checkpoint_tag(checkpoint, value, ordinal=ordinal)
                claim.output_tag_count += 1
                ordinal += 1
                session.flush()
            claim.output_tag_hash_state = checkpoint.export_state()
            claim.updated_at = utc_timestamp_now()
            return _output_tag_set_payload(claim)

    def seal_claim_output_tags(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if claim.output_tags_sealed_at is None:
                if claim.output_tag_count < 1 or claim.output_tag_hash_state is None:
                    raise Conflict("output-tag authority is empty")
                checkpoint = CheckpointSHA256.from_state(claim.output_tag_hash_state)
                checkpoint.update(b"]}")
                claim.output_tag_set_sha256 = checkpoint.hexdigest()
                claim.output_tags_sealed_at = utc_timestamp_now()
                claim.updated_at = claim.output_tags_sealed_at
            return _output_tag_set_payload(claim)

    def list_claim_output_tags(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        with read_snapshot(self._session_factory) as session:
            claim = _claim_actor(session, claim_id, principal)
            authority = _require_set_authority(
                claim.output_tag_count,
                claim.output_tag_set_sha256,
                authority_sha256,
                "output-tag",
            )
            rows = list(
                session.scalars(
                    select(CollectionProcessingClaimOutputTagRecord)
                    .where(
                        CollectionProcessingClaimOutputTagRecord.claim_id == claim.id,
                        CollectionProcessingClaimOutputTagRecord.tag_order >= start,
                    )
                    .order_by(CollectionProcessingClaimOutputTagRecord.tag_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": authority,
                "start_ordinal": start,
                "next_ordinal": next_ordinal if next_ordinal < claim.output_tag_count else None,
                "tags": [item.tag for item in rows],
            }

    def get_claim(
        self,
        claim_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            return _claim_payload(session, _claim_actor(session, claim_id, principal))

    def list_claims(
        self,
        *,
        page_size: int = 25,
        position: tuple[str | int | bool | bytes | None, ...] | None = None,
        state: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        validate_page_size(page_size)
        if sort not in _CLAIM_SORT_NAMES or order not in _SORT_ORDERS:
            raise BadRequest("claim sorting is invalid")
        if state is not None and state not in _CLAIM_STATES:
            raise BadRequest("claim state is invalid")
        _, statement, key_columns = _claim_list_statement(
            state=state, sort=sort, order=order, principal=principal
        )
        with read_snapshot(self._session_factory) as session:
            rows, next_position = bounded_page(
                list(
                    session.scalars(
                        keyset_statement(
                            statement,
                            columns=key_columns,
                            position=position,
                            order=order,
                            page_size=page_size,
                        )
                    )
                ),
                page_size=page_size,
                position_of=lambda claim: _claim_list_position(claim, sort=sort),
            )
            return {
                "page_size": page_size,
                "_next_position": next_position,
                "sort": sort,
                "order": order,
                "filters": {"state": state},
                "claims": [_claim_payload(session, current) for current in rows],
            }

    def iter_claims(
        self,
        *,
        state: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        principal: ApplicationPrincipal,
    ) -> Iterator[dict[str, object]]:
        if sort not in _CLAIM_SORT_NAMES or order not in _SORT_ORDERS:
            raise BadRequest("claim sorting is invalid")
        if state is not None and state not in _CLAIM_STATES:
            raise BadRequest("claim state is invalid")
        _, statement, key_columns = _claim_list_statement(
            state=state, sort=sort, order=order, principal=principal
        )
        direction = desc if order == "desc" else asc
        statement = statement.order_by(*(direction(column) for column in key_columns))
        with read_snapshot(self._session_factory) as session:
            for claim in session.scalars(statement.execution_options(yield_per=100)):
                yield _claim_payload(session, claim)

    def renew_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        lease_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        lease = _lease_seconds(lease_seconds)
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            if claim.state != "active":
                raise Conflict("collection processing claim is not renewable")
            if _expired(claim.expires_at) and not _execution_output_exists(session, claim):
                raise Conflict("collection processing claim is not renewable")
            claim.expires_at = format_utc_timestamp(utc_now() + timedelta(seconds=lease))
            claim.updated_at = utc_timestamp_now()
            return _claim_payload(session, claim)

    def restart_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        lease_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Advance an active claim to a fresh fencing generation for retry.

        The transition revokes every capability and clears the prior sealed
        execution. It fails closed while that execution owns any upload or
        finalized collection, because a retry must never race or duplicate an
        output whose settlement is merely uncertain.
        """

        lease = _lease_seconds(lease_seconds)
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            _require_no_execution_output(session, claim)
            now = utc_timestamp_now()
            claim.fence += 1
            claim.expires_at = format_utc_timestamp(utc_now() + timedelta(seconds=lease))
            claim.updated_at = now
            _clear_plan(session, claim)
            _revoke_capabilities(session, claim.id, now=now)
            return _claim_payload(session, claim)

    def seal_claim_plan(
        self,
        claim_id: str,
        *,
        fence: int,
        execution_id: str,
        controller_evidence: Mapping[str, object],
        controller_evidence_sha256: str,
        operation_id: str,
        operation_sha256: str,
        retirement_policy: str,
        retirement_grace_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_execution_id = _sha256(execution_id, "execution identity")
        evidence_bytes, evidence = _json_document(
            controller_evidence,
            label="controller evidence",
            maximum_bytes=CONTROLLER_EVIDENCE_MAX_BYTES,
        )
        evidence_sha256 = _sha256(
            controller_evidence_sha256,
            "controller evidence identity",
        )
        if hashlib.sha256(evidence_bytes).hexdigest() != evidence_sha256:
            raise BadRequest("controller evidence identity does not match its canonical JSON")
        try:
            operation = OperationIdentity(
                _visible(operation_id, "operation id", maximum=160),
                _sha256(operation_sha256, "operation identity"),
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        policy = _retirement_policy(retirement_policy, retirement_grace_seconds)
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            _require_inputs_sealed(claim)
            if claim.artifacts_sealed_at is None or claim.artifact_set_sha256 is None:
                raise Conflict("artifact authority is not sealed")
            if claim.output_tags_sealed_at is None or claim.output_tag_set_sha256 is None:
                raise Conflict("output-tag authority is not sealed")
            encoded_evidence = json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if claim.plan_sealed_at is not None:
                expected = (
                    normalized_execution_id,
                    encoded_evidence,
                    evidence_sha256,
                    operation.id,
                    operation.sha256,
                    policy,
                    int(retirement_grace_seconds),
                )
                actual = (
                    claim.execution_id,
                    claim.controller_evidence_json,
                    claim.controller_evidence_sha256,
                    claim.operation_id,
                    claim.operation_sha256,
                    claim.retirement_policy,
                    claim.retirement_grace_seconds,
                )
                if actual != expected:
                    raise Conflict("collection processing claim already has another sealed plan")
                return _claim_payload(session, claim)
            conflict = session.scalar(
                select(CollectionProcessingClaimRecord.id).where(
                    CollectionProcessingClaimRecord.execution_id == normalized_execution_id,
                    CollectionProcessingClaimRecord.id != claim.id,
                )
            )
            if conflict is not None:
                raise Conflict("execution identity is already bound to another claim")
            now = utc_timestamp_now()
            claim.execution_id = normalized_execution_id
            claim.controller_evidence_json = encoded_evidence
            claim.controller_evidence_sha256 = evidence_sha256
            claim.operation_id = operation.id
            claim.operation_sha256 = operation.sha256
            claim.retirement_policy = policy
            claim.retirement_grace_seconds = int(retirement_grace_seconds)
            claim.plan_sealed_at = now
            claim.updated_at = now
            # Observation capabilities are no longer required after a plan is sealed.
            # Revocation narrows the active payload readers before target execution.
            _revoke_capabilities(session, claim.id, now=now)
            return _claim_payload(session, claim)

    def issue_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        audience: str,
        actions: Sequence[str],
        ttl_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        ttl = _lease_seconds(ttl_seconds)
        normalized_actions = tuple(sorted(set(str(item) for item in actions)))
        if not normalized_actions or not set(normalized_actions).issubset(_CAPABILITY_ACTIONS):
            raise BadRequest("transform capability actions are invalid")
        normalized_audience = str(audience)
        if _CAPABILITY_AUDIENCE.fullmatch(normalized_audience) is None:
            raise BadRequest("transform capability audience is invalid")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            if "write-output" in normalized_actions and claim.plan_sealed_at is None:
                raise Conflict("write-output capability requires a sealed execution plan")
            claim_expiry = parse_utc_timestamp(claim.expires_at)
            requested_expiry = utc_now() + timedelta(seconds=ttl)
            expiry = min(claim_expiry, requested_expiry)
            token = "rhc_" + secrets.token_urlsafe(32)
            now = utc_timestamp_now()
            capability = CollectionTransformCapabilityRecord(
                id=secrets.token_hex(16),
                claim_id=claim.id,
                fence=claim.fence,
                audience=normalized_audience,
                token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                actions_json=json.dumps(list(normalized_actions), separators=(",", ":")),
                state="receiving",
                expires_at=format_utc_timestamp(expiry),
                created_at=now,
            )
            session.add(capability)
            session.flush()
            claim.updated_at = now
            session.flush()
            return {
                "format": "riverhog-transform-capability/v1",
                "id": capability.id,
                "claim_id": claim.id,
                "fence": claim.fence,
                "audience": capability.audience,
                "actions": list(normalized_actions),
                "state": "receiving",
                "principal_app": _capability_app(claim, normalized_actions),
                "expires_at": capability.expires_at,
                "artifacts": {
                    "state": "receiving",
                    "count": 0,
                    "total_bytes": 0,
                    "authority": None,
                },
                "token": token,
            }

    def append_capability_artifacts(
        self,
        claim_id: str,
        capability_id: str,
        *,
        fence: int,
        start_ordinal: int,
        artifacts: Sequence[CollectionArtifactIdentity],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        values = tuple(artifacts)
        _bounded_batch(values, "capability artifact")
        if values != tuple(sorted(values)):
            raise BadRequest("capability artifacts must be unique and canonically ordered")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            capability = _owned_capability(session, claim, capability_id)
            if capability.state == "active":
                return _capability_artifact_set_payload(capability)
            if capability.state != "receiving":
                raise Conflict("capability no longer accepts artifact scope")
            ordinal = _append_start(start_ordinal, capability.artifact_count)
            checkpoint = _set_checkpoint(capability.artifact_hash_state, "claim-artifacts")
            for value in values:
                if ordinal < capability.artifact_count:
                    current = session.scalar(
                        select(CollectionTransformCapabilityArtifactRecord).where(
                            CollectionTransformCapabilityArtifactRecord.capability_id
                            == capability.id,
                            CollectionTransformCapabilityArtifactRecord.artifact_order == ordinal,
                        )
                    )
                    if (
                        current is None
                        or _capability_artifact_identity(session, claim, current) != value
                    ):
                        raise Conflict("capability artifact retry differs from staged authority")
                    ordinal += 1
                    continue
                previous = _last_capability_artifact_identity(session, claim, capability.id)
                if previous is not None and value <= previous:
                    raise BadRequest(
                        "capability artifacts must remain canonically ordered across batches"
                    )
                _validate_claim_artifacts(session, claim, (value,))
                existing = session.get(
                    CollectionTransformCapabilityArtifactRecord,
                    (capability.id, value.collection.collection_id, value.path),
                )
                if existing is not None:
                    raise Conflict("capability artifact is already staged")
                session.add(
                    CollectionTransformCapabilityArtifactRecord(
                        capability_id=capability.id,
                        collection_id=value.collection.collection_id,
                        path=value.path,
                        artifact_order=ordinal,
                        bytes=value.bytes,
                        sha256=value.sha256,
                    )
                )
                _checkpoint_item(checkpoint, value.as_dict())
                capability.artifact_count += 1
                capability.artifact_bytes += value.bytes
                ordinal += 1
                session.flush()
            capability.artifact_hash_state = checkpoint.export_state()
            claim.updated_at = utc_timestamp_now()
            return _capability_artifact_set_payload(capability)

    def seal_capability_artifacts(
        self,
        claim_id: str,
        capability_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            capability = _owned_capability(session, claim, capability_id)
            if capability.state == "active":
                return _capability_artifact_set_payload(capability)
            if (
                capability.state != "receiving"
                or capability.artifact_count < 1
                or capability.artifact_hash_state is None
            ):
                raise Conflict("capability artifact authority is empty or unavailable")
            identity = CheckpointSHA256.from_state(capability.artifact_hash_state).hexdigest()
            actions = tuple(sorted(set(json.loads(capability.actions_json))))
            if "write-output" in actions and (
                claim.plan_sealed_at is None
                or claim.artifact_set_sha256 != identity
                or claim.artifact_count != capability.artifact_count
                or claim.artifact_bytes != capability.artifact_bytes
            ):
                raise Conflict("write-output capability differs from the sealed artifact plan")
            capability.artifact_set_sha256 = identity
            capability.artifacts_sealed_at = utc_timestamp_now()
            capability.state = "active"
            claim.updated_at = capability.artifacts_sealed_at
            return _capability_artifact_set_payload(capability)

    def authenticate_capability(self, token: str) -> ApplicationPrincipal | None:
        supplied = token.strip()
        if not supplied.startswith("rhc_"):
            return None
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        with session_scope(self._session_factory) as session:
            capability = session.scalar(
                select(CollectionTransformCapabilityRecord).where(
                    CollectionTransformCapabilityRecord.token_sha256 == digest,
                    CollectionTransformCapabilityRecord.state == "active",
                )
            )
            if capability is None or _expired(capability.expires_at):
                return None
            claim = session.get(CollectionProcessingClaimRecord, capability.claim_id)
            if (
                claim is None
                or claim.state != "active"
                or claim.fence != capability.fence
                or _expired(claim.expires_at)
            ):
                return None
            actions = tuple(sorted(set(json.loads(capability.actions_json))))
            if "write-output" in actions and claim.plan_sealed_at is None:
                return None
            grants: set[ApplicationAccess] = set()
            grants.add(ApplicationAccess(COLLECTION_TRANSFORMS_EXECUTE))
            if "read-inputs" in actions:
                representative_collection_id = session.scalar(
                    select(CollectionTransformCapabilityArtifactRecord.collection_id)
                    .where(
                        CollectionTransformCapabilityArtifactRecord.capability_id == capability.id
                    )
                    .order_by(CollectionTransformCapabilityArtifactRecord.artifact_order)
                    .limit(1)
                )
                if representative_collection_id is None:
                    return None
                resource = collection_resource(representative_collection_id)
                grants.update(
                    {
                        ApplicationAccess(CATALOG_READ, resource),
                        ApplicationAccess(RETRIEVAL_MANAGE, resource),
                        ApplicationAccess(PROVENANCE_READ, resource),
                        ApplicationAccess(PROVENANCE_EXPORT, resource),
                    }
                )
            if "write-output" in actions:
                grants.add(ApplicationAccess(COLLECTION_TRANSFORMS_EXECUTE))
                grants.add(ApplicationAccess(COLLECTIONS_CREATE))
            app = _capability_app(claim, actions)
            has_artifact_scope = session.scalar(
                select(CollectionTransformCapabilityArtifactRecord.capability_id)
                .where(CollectionTransformCapabilityArtifactRecord.capability_id == capability.id)
                .limit(1)
            )
            if "read-inputs" in actions and has_artifact_scope is None:
                return None
            return ApplicationPrincipal(
                app=app,
                # Preserve the initiating key for download-budget attribution and
                # revocation while the synthetic app keeps claim-scoped ownership
                # stable across capability refreshes.
                key_id=claim.consumer_key_id,
                access=frozenset(grants),
                artifact_scope_capability_id=(capability.id if "read-inputs" in actions else None),
            )

    def record_dispositions(
        self,
        claim_id: str,
        *,
        fence: int,
        dispositions: Sequence[ArtifactDisposition],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Insert one bounded idempotent disposition batch in any arrival order."""

        values = tuple(dispositions)
        if not values or len(values) > _DISPOSITION_BATCH_MAX:
            raise BadRequest(f"disposition batch must contain 1 to {_DISPOSITION_BATCH_MAX} facts")
        keys = [
            (item.input_collection_id, item.input_archive_root_sha256, item.input_path)
            for item in values
        ]
        if len(keys) != len(set(keys)):
            raise BadRequest("disposition batch repeats an input artifact")
        with session_scope(self._session_factory) as session:
            claim = _claim_execution_actor(
                session,
                claim_id,
                fence=fence,
                principal=principal,
            )
            disposition_set = _receiving_disposition_set(session, claim)
            additions = 0
            transformed = 0
            for item in values:
                _require_disposition_input(session, claim, item)
                existing = session.get(
                    CollectionProcessingDispositionRecord,
                    (claim.id, item.input_collection_id, item.input_path),
                )
                if existing is not None:
                    if _disposition_record_identity(session, claim, existing) != item:
                        raise Conflict("input artifact already has another disposition")
                    continue
                if disposition_set.state != "receiving":
                    raise Conflict("disposition set no longer accepts facts")
                session.add(
                    CollectionProcessingDispositionRecord(
                        claim_id=claim.id,
                        collection_id=item.input_collection_id,
                        path=item.input_path,
                        status=item.status,
                        failure_code=item.code,
                        failure_message=item.message,
                    )
                )
                additions += 1
                transformed += int(item.status == "transformed")
            if additions:
                disposition_set.disposition_count += additions
                disposition_set.transformed_count += transformed
                disposition_set.updated_at = utc_timestamp_now()
                claim.updated_at = disposition_set.updated_at
            session.flush()
            return _disposition_set_payload(disposition_set)

    def record_disposition_outputs(
        self,
        claim_id: str,
        *,
        fence: int,
        outputs: Sequence[ArtifactDispositionOutput],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Insert one bounded idempotent source-to-output edge batch."""

        values = tuple(outputs)
        if not values or len(values) > _DISPOSITION_BATCH_MAX:
            raise BadRequest(
                f"disposition output batch must contain 1 to {_DISPOSITION_BATCH_MAX} edges"
            )
        keys = [(item.output_path, item.input_collection_id, item.input_path) for item in values]
        if len(keys) != len(set(keys)):
            raise BadRequest("disposition output batch repeats a source edge")
        with session_scope(self._session_factory) as session:
            claim = _claim_execution_actor(
                session,
                claim_id,
                fence=fence,
                principal=principal,
            )
            disposition_set = _receiving_disposition_set(session, claim)
            additions = 0
            new_outputs = 0
            newly_mapped_inputs = 0
            batch_output_paths: set[str] = set()
            batch_input_keys: set[tuple[int, str]] = set()
            for item in values:
                _require_disposition_output(session, claim, item)
                existing = session.get(
                    CollectionProcessingDispositionOutputRecord,
                    (
                        claim.id,
                        item.output_path,
                        item.input_collection_id,
                        item.input_path,
                    ),
                )
                if existing is not None:
                    continue
                if disposition_set.state != "receiving":
                    raise Conflict("disposition set no longer accepts output edges")
                output_exists = session.scalar(
                    select(CollectionProcessingDispositionOutputRecord.claim_id)
                    .where(
                        CollectionProcessingDispositionOutputRecord.claim_id == claim.id,
                        CollectionProcessingDispositionOutputRecord.output_path == item.output_path,
                    )
                    .limit(1)
                )
                source_exists = session.scalar(
                    select(CollectionProcessingDispositionOutputRecord.claim_id)
                    .where(
                        CollectionProcessingDispositionOutputRecord.claim_id == claim.id,
                        CollectionProcessingDispositionOutputRecord.input_collection_id
                        == item.input_collection_id,
                        CollectionProcessingDispositionOutputRecord.input_path == item.input_path,
                    )
                    .limit(1)
                )
                session.add(
                    CollectionProcessingDispositionOutputRecord(
                        claim_id=claim.id,
                        output_path=item.output_path,
                        input_collection_id=item.input_collection_id,
                        input_path=item.input_path,
                    )
                )
                additions += 1
                if output_exists is None and item.output_path not in batch_output_paths:
                    new_outputs += 1
                input_key = (item.input_collection_id, item.input_path)
                if source_exists is None and input_key not in batch_input_keys:
                    newly_mapped_inputs += 1
                batch_output_paths.add(item.output_path)
                batch_input_keys.add(input_key)
            if additions:
                disposition_set.output_edge_count += additions
                disposition_set.output_artifact_count += new_outputs
                disposition_set.transformed_with_outputs_count += newly_mapped_inputs
                disposition_set.updated_at = utc_timestamp_now()
                claim.updated_at = disposition_set.updated_at
            session.flush()
            return _disposition_set_payload(disposition_set)

    def seal_disposition_set(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Begin bounded restartable sealing of one exact relational set."""

        with session_scope(self._session_factory) as session:
            claim = _claim_execution_actor(
                session,
                claim_id,
                fence=fence,
                principal=principal,
            )
            disposition_set = _receiving_disposition_set(session, claim)
            if disposition_set.state in {"sealing", "sealed"}:
                return _disposition_set_payload(disposition_set)
            if disposition_set.state == "failed":
                raise Conflict("disposition set sealing failed")
            expected = int(claim.artifact_count)
            if disposition_set.disposition_count != expected:
                raise Conflict("disposition set does not account for every claimed artifact")
            if (
                disposition_set.output_edge_count < 1
                or disposition_set.output_artifact_count < 1
                or disposition_set.transformed_count
                != disposition_set.transformed_with_outputs_count
            ):
                raise Conflict("disposition set does not bind every transformed artifact")
            now = utc_timestamp_now()
            disposition_set.state = "sealing"
            disposition_set.validation_phase = "dispositions"
            disposition_set.disposition_hash_state = CheckpointSHA256().export_state()
            disposition_set.output_hash_state = CheckpointSHA256().export_state()
            disposition_set.failure = None
            disposition_set.updated_at = now
            claim.updated_at = now
        self._advance_disposition_set(claim_id)
        return self.get_disposition_set(claim_id, principal=principal)

    def get_disposition_set(
        self,
        claim_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _claim_actor(session, claim_id, principal)
            disposition_set = session.get(CollectionProcessingDispositionSetRecord, claim.id)
            if disposition_set is None:
                raise NotFound(f"disposition set not found: {claim_id}")
            return _disposition_set_payload(disposition_set)

    def list_dispositions(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        expected = _sha256(authority_sha256, "disposition set identity")
        with read_snapshot(self._session_factory) as session:
            claim = _claim_actor(session, claim_id, principal)
            disposition_set = _sealed_disposition_authority(session, claim.id, expected)
            rows = list(
                session.scalars(
                    select(CollectionProcessingDispositionRecord)
                    .where(
                        CollectionProcessingDispositionRecord.claim_id == claim.id,
                        CollectionProcessingDispositionRecord.disposition_order >= start,
                    )
                    .order_by(CollectionProcessingDispositionRecord.disposition_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": _disposition_set_identity(disposition_set).as_dict(),
                "start_ordinal": start,
                "next_ordinal": (
                    next_ordinal if next_ordinal < int(disposition_set.disposition_count) else None
                ),
                "dispositions": [
                    _disposition_record_identity(session, claim, row).as_dict() for row in rows
                ],
            }

    def list_disposition_outputs(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        expected = _sha256(authority_sha256, "disposition set identity")
        with read_snapshot(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal)
            disposition_set = _sealed_disposition_authority(session, claim.id, expected)
            rows = list(
                session.scalars(
                    select(CollectionProcessingDispositionOutputRecord)
                    .where(
                        CollectionProcessingDispositionOutputRecord.claim_id == claim.id,
                        CollectionProcessingDispositionOutputRecord.output_order >= start,
                    )
                    .order_by(CollectionProcessingDispositionOutputRecord.output_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": _disposition_set_identity(disposition_set).as_dict(),
                "start_ordinal": start,
                "next_ordinal": (
                    next_ordinal if next_ordinal < int(disposition_set.output_edge_count) else None
                ),
                "outputs": [
                    _disposition_output_record_identity(session, claim, row).as_dict()
                    for row in rows
                ],
            }

    def process_due_disposition_sets(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0
        with session_scope(self._session_factory) as session:
            ids = list(
                session.scalars(
                    select(CollectionProcessingDispositionSetRecord.claim_id)
                    .where(CollectionProcessingDispositionSetRecord.state == "sealing")
                    .order_by(
                        CollectionProcessingDispositionSetRecord.updated_at,
                        CollectionProcessingDispositionSetRecord.claim_id,
                    )
                    .limit(limit)
                )
            )
        for claim_id in ids:
            self._advance_disposition_set(claim_id)
        return len(ids)

    def requeue_interrupted_disposition_sets_for_startup(self) -> int:
        """Sealing checkpoints are durable; startup only makes their work visible."""

        with session_scope(self._session_factory) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionProcessingDispositionSetRecord)
                    .where(CollectionProcessingDispositionSetRecord.state == "sealing")
                )
                or 0
            )

    def _advance_disposition_set(self, claim_id: str) -> None:
        with session_scope(self._session_factory) as session:
            disposition_set = session.scalar(
                select(CollectionProcessingDispositionSetRecord)
                .where(CollectionProcessingDispositionSetRecord.claim_id == claim_id)
                .with_for_update(skip_locked=True)
            )
            if disposition_set is None or disposition_set.state != "sealing":
                return
            try:
                if disposition_set.validation_phase == "dispositions":
                    if _advance_disposition_hash(session, disposition_set):
                        return
                    disposition_set.disposition_sha256 = CheckpointSHA256.from_state(
                        cast(str, disposition_set.disposition_hash_state)
                    ).hexdigest()
                    disposition_set.validation_phase = "outputs"
                    disposition_set.updated_at = utc_timestamp_now()
                    return
                if disposition_set.validation_phase == "outputs":
                    if _advance_disposition_output_hash(session, disposition_set):
                        return
                    disposition_set.output_sha256 = CheckpointSHA256.from_state(
                        cast(str, disposition_set.output_hash_state)
                    ).hexdigest()
                    identity = canonical_json_sha256(
                        {
                            "format": "riverhog-artifact-disposition-set/v1",
                            "disposition_count": disposition_set.disposition_count,
                            "dispositions_sha256": disposition_set.disposition_sha256,
                            "output_edge_count": disposition_set.output_edge_count,
                            "output_artifact_count": disposition_set.output_artifact_count,
                            "outputs_sha256": disposition_set.output_sha256,
                        }
                    )
                    now = utc_timestamp_now()
                    disposition_set.identity_sha256 = identity
                    disposition_set.state = "sealed"
                    disposition_set.validation_phase = None
                    disposition_set.sealed_at = now
                    disposition_set.updated_at = now
                    return
                raise RuntimeError("disposition set has no validation phase")
            except Exception as exc:
                disposition_set.state = "failed"
                disposition_set.validation_phase = None
                disposition_set.failure = str(exc)[:1000] or type(exc).__name__
                disposition_set.updated_at = utc_timestamp_now()

    def settle_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, object],
        outcome_claim_id: str | None = None,
        outcome_fence: int | None = None,
        outcome_id: str | None = None,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        try:
            document = CollectionDerivation.from_mapping(derivation)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            _require_sealed_transform_plan(claim)
            if claim.state in {"settled", "retiring", "released"}:
                _require_existing_settlement(
                    session,
                    claim,
                    output_collection_id=output_collection_id,
                    document=document,
                )
                _require_existing_outcome_binding(
                    session,
                    source_claim=claim,
                    outcome_claim_id=outcome_claim_id,
                    outcome_fence=outcome_fence,
                    outcome_id=outcome_id,
                )
                return _claim_payload(session, claim)
            _require_active_generation(claim, fence=fence)
            assert claim.execution_id is not None
            assert claim.controller_evidence_json is not None
            assert claim.controller_evidence_sha256 is not None
            assert claim.operation_id is not None
            assert claim.operation_sha256 is not None
            assert claim.input_set_sha256 is not None
            assert claim.artifact_set_sha256 is not None
            assert claim.output_tag_set_sha256 is not None
            expected_evidence = cast(dict[str, object], json.loads(claim.controller_evidence_json))
            if (
                document.claim_id != claim.id
                or document.execution_id != claim.execution_id
                or document.fence != claim.fence
                or document.input_set_sha256 != claim.input_set_sha256
                or document.artifact_set_sha256 != claim.artifact_set_sha256
                or document.operation
                != OperationIdentity(claim.operation_id, claim.operation_sha256)
                or document.output_tag_set_sha256 != claim.output_tag_set_sha256
                or document.execution_envelope_sha256 != claim.execution_id
                or document.controller_evidence != expected_evidence
                or document.controller_evidence_sha256 != claim.controller_evidence_sha256
            ):
                raise Conflict("derivation differs from the sealed collection work plan")
            output = session.scalar(
                select(CollectionRecord)
                .where(
                    CollectionRecord.id == int(output_collection_id),
                    CollectionRecord.is_published.is_(True),
                )
                .with_for_update()
            )
            if output is None:
                raise NotFound(f"derived collection not found: {output_collection_id}")
            expected_app = f"transform:{claim.execution_id}"
            if (
                output.created_by_app != expected_app
                or output.creation_idempotency_key != claim.execution_id
            ):
                raise Conflict("derived collection was not created by the sealed output intent")
            if output.tag_set_identity != claim.output_tag_set_sha256:
                raise Conflict("derived collection tags differ from the sealed work plan")
            evidence = session.get(
                CollectionFileRecord,
                (output.id, DERIVATION_EVIDENCE_PATH),
            )
            if evidence is None or evidence.sha256 != document.sha256:
                raise Conflict("derived collection does not contain its exact derivation evidence")
            disposition_set = _verified_disposition_set(session, claim, document.disposition_set)
            _verify_dispositions(session, claim, disposition_set, output)
            _collection_root(session, output.id)
            existing = session.get(CollectionDerivationRecord, output.id)
            encoded = document.to_json_bytes().decode("utf-8")
            if existing is not None:
                if existing.document_json != encoded or existing.execution_id != claim.execution_id:
                    raise Conflict("derived collection already has different derivation evidence")
            else:
                session.add(
                    CollectionDerivationRecord(
                        collection_id=output.id,
                        execution_id=claim.execution_id,
                        claim_id=claim.id,
                        fence=claim.fence,
                        document_json=encoded,
                        document_sha256=document.sha256,
                        created_at=utc_timestamp_now(),
                    )
                )
            _attach_processing_outcome(
                session,
                source_claim=claim,
                output_collection_id=output.id,
                derivation=document,
                outcome_claim_id=outcome_claim_id,
                outcome_fence=outcome_fence,
                outcome_id=outcome_id,
            )
            now = utc_timestamp_now()
            claim.state = "settled"
            claim.output_collection_id = output.id
            claim.settled_at = claim.settled_at or now
            claim.updated_at = now
            _revoke_capabilities(session, claim.id, now=now)
            session.flush()
            return _claim_payload(session, claim)

    def settle_claim_outcomes(
        self,
        claim_id: str,
        *,
        fence: int,
        retirement_policy: str,
        retirement_grace_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Seal, then close, the durable exact outcome authority."""

        policy = _retirement_policy(retirement_policy, retirement_grace_seconds)
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            if claim.plan_sealed_at is not None or claim.execution_id is not None:
                raise Conflict("an executed collection claim cannot close delegated outcomes")
            if claim.state in {"settled", "retiring", "released"}:
                if (
                    claim.retirement_policy != policy
                    or claim.retirement_grace_seconds != int(retirement_grace_seconds)
                    or claim.outcome_state != "sealed"
                    or claim.outcome_set_sha256 is None
                ):
                    raise Conflict("collection processing claim has another outcome settlement")
                return _claim_payload(session, claim)
            _require_active_generation(claim, fence=fence)
            if claim.outcome_state == "failed":
                raise Conflict(claim.outcome_failure or "outcome authority sealing failed")
            if claim.outcome_state == "receiving":
                if claim.outcome_count < 1:
                    raise Conflict("outcome authority is empty")
                claim.retirement_policy = policy
                claim.retirement_grace_seconds = int(retirement_grace_seconds)
                claim.outcome_state = "sealing"
                claim.outcome_hash_state = _set_checkpoint(None, "claim-outcomes").export_state()
                claim.outcome_validation_cursor = None
                claim.outcome_validation_count = 0
                claim.updated_at = utc_timestamp_now()
                return _claim_payload(session, claim)
            if claim.outcome_state == "sealing":
                if claim.retirement_policy != policy or claim.retirement_grace_seconds != int(
                    retirement_grace_seconds
                ):
                    raise Conflict("outcome authority is sealing with another retirement policy")
                return _claim_payload(session, claim)
            if claim.outcome_state != "sealed" or claim.outcome_set_sha256 is None:
                raise InvalidState("outcome authority is unavailable")
            if claim.retirement_policy != policy or claim.retirement_grace_seconds != int(
                retirement_grace_seconds
            ):
                raise Conflict("outcome authority was sealed with another retirement policy")
            now = utc_timestamp_now()
            claim.state = "settled"
            claim.settled_at = claim.settled_at or now
            claim.updated_at = now
            _revoke_capabilities(session, claim.id, now=now)
            return _claim_payload(session, claim)

    def process_due_outcome_sets(self, *, limit: int = 1) -> int:
        processed = 0
        for _ in range(max(0, int(limit))):
            with session_scope(self._session_factory) as session:
                claim = session.scalar(
                    select(CollectionProcessingClaimRecord)
                    .where(CollectionProcessingClaimRecord.outcome_state == "sealing")
                    .order_by(CollectionProcessingClaimRecord.updated_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if claim is None:
                    break
                try:
                    _advance_outcome_set(session, claim)
                except Exception as exc:
                    claim.outcome_state = "failed"
                    claim.outcome_failure = str(exc)[:1000] or type(exc).__name__
                    claim.updated_at = utc_timestamp_now()
                processed += 1
        return processed

    def list_claim_outcomes(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        start = _page_start(start_ordinal)
        with read_snapshot(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal)
            authority = _require_set_authority(
                claim.outcome_count,
                claim.outcome_set_sha256,
                authority_sha256,
                "outcome",
            )
            rows = list(
                session.scalars(
                    select(CollectionProcessingOutcomeRecord)
                    .where(
                        CollectionProcessingOutcomeRecord.claim_id == claim.id,
                        CollectionProcessingOutcomeRecord.outcome_order >= start,
                    )
                    .order_by(CollectionProcessingOutcomeRecord.outcome_order)
                    .limit(_DISPOSITION_BATCH_MAX)
                )
            )
            next_ordinal = start + len(rows)
            return {
                "authority": authority,
                "start_ordinal": start,
                "next_ordinal": next_ordinal if next_ordinal < claim.outcome_count else None,
                "outcomes": [_outcome_identity(item).as_dict() for item in rows],
            }

    def get_derivation(
        self,
        collection_id: int,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionDerivationRecord, int(collection_id))
            if record is None:
                raise NotFound(f"collection derivation not found: {collection_id}")
            claim = session.get(CollectionProcessingClaimRecord, record.claim_id)
            if claim is None or claim.consumer_app != principal.app:
                raise NotFound(f"collection derivation not found: {collection_id}")
            return {
                "collection_id": record.collection_id,
                "document_sha256": record.document_sha256,
                "derivation": json.loads(record.document_json),
            }

    def begin_retirement(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            if claim.state == "retiring":
                return _claim_payload(session, claim)
            if (
                claim.state != "settled"
                or claim.retirement_policy != "retire-after-verified-output"
            ):
                raise Conflict("collection processing claim is not eligible for retirement")
            if claim.settled_at is None:
                raise InvalidState("settled claim has no settlement identity")
            if claim.output_collection_id is not None:
                _require_sealed_transform_plan(claim)
                derivation_record = session.get(
                    CollectionDerivationRecord,
                    claim.output_collection_id,
                )
                if derivation_record is None:
                    raise InvalidState("settled claim has no verified derivation record")
                derivation = CollectionDerivation.from_mapping(
                    json.loads(derivation_record.document_json)
                )
                expected_artifact_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(CollectionFileRecord)
                        .join(
                            CollectionProcessingClaimInputRecord,
                            and_(
                                CollectionProcessingClaimInputRecord.claim_id == claim.id,
                                CollectionProcessingClaimInputRecord.collection_id
                                == CollectionFileRecord.collection_id,
                            ),
                        )
                        .where(~CollectionFileRecord.path.startswith("riverhog/"))
                    )
                    or 0
                )
                if claim.artifact_count != expected_artifact_count:
                    raise Conflict(
                        "source retirement requires a plan covering every input artifact"
                    )
                _verified_disposition_set(session, claim, derivation.disposition_set)
                unsafe = session.scalar(
                    select(CollectionProcessingDispositionRecord.path)
                    .where(
                        CollectionProcessingDispositionRecord.claim_id == claim.id,
                        CollectionProcessingDispositionRecord.status.not_in(
                            ("transformed", "preserved")
                        ),
                    )
                    .order_by(CollectionProcessingDispositionRecord.collection_id)
                    .limit(1)
                )
                if unsafe is not None:
                    raise Conflict(
                        "source retirement is not authorized for omitted or rejected artifacts"
                    )
                _collection_root(session, claim.output_collection_id)
            else:
                _require_outcome_retirement_coverage(session, claim)
            eligible_at = parse_utc_timestamp(claim.settled_at) + timedelta(
                seconds=claim.retirement_grace_seconds
            )
            if utc_now() < eligible_at:
                return _claim_payload(session, claim)
            claim.state = "retiring"
            claim.updated_at = utc_timestamp_now()
            return _claim_payload(session, claim)

    def abandon_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        reason: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        """Terminate active collection work that will not produce an output.

        Abandonment is a fenced, idempotent terminal transition. It immediately
        revokes every scoped payload capability and removes the claim from
        deletion blockers without pretending that work settled successfully.
        The exact current fence is sufficient even after lease expiry. Only the
        controller application may call this operation, and a restarted claim
        advances the fence before another generation can exist. This lets a
        controller crash after deciding a terminal no-output outcome and still
        durably converge without waiting for a later lease cycle.
        """

        normalized_reason = _visible(reason, "abandonment reason", maximum=1000)
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            if claim.state == "abandoned":
                if claim.abandonment_reason != normalized_reason:
                    raise Conflict("collection processing claim was abandoned for another reason")
                return _claim_payload(session, claim)
            _require_active_generation(claim, fence=fence)
            _require_no_transform_output(session, claim)
            if claim.output_collection_id is not None or claim.settled_at is not None:
                raise InvalidState("active claim unexpectedly contains a settled output")
            now = utc_timestamp_now()
            claim.state = "abandoned"
            claim.abandoned_at = claim.abandoned_at or now
            claim.abandonment_reason = normalized_reason
            claim.updated_at = now
            _revoke_capabilities(session, claim.id, now=now)
            return _claim_payload(session, claim)

    def release_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            if claim.state == "released":
                return _claim_payload(session, claim)
            if claim.state == "retiring":
                remaining = int(
                    session.scalar(
                        select(func.count())
                        .select_from(CollectionProcessingClaimInputRecord)
                        .join(
                            CollectionRecord,
                            CollectionRecord.id
                            == CollectionProcessingClaimInputRecord.collection_id,
                        )
                        .where(CollectionProcessingClaimInputRecord.claim_id == claim.id)
                    )
                    or 0
                )
                if remaining:
                    raise Conflict("retirement claim still has live input collections")
            elif claim.state == "settled" and claim.retirement_policy != "retain":
                raise Conflict("retiring claim must enter retirement before release")
            elif claim.state not in {"settled", "retiring"}:
                raise Conflict("only settled collection work may be released")
            now = utc_timestamp_now()
            claim.state = "released"
            claim.released_at = now
            claim.updated_at = now
            _revoke_capabilities(session, claim.id, now=now)
            return _claim_payload(session, claim)

    def reap_expired_claims(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            rows = list(
                session.scalars(
                    select(CollectionProcessingClaimRecord)
                    .where(
                        CollectionProcessingClaimRecord.state == "active",
                        CollectionProcessingClaimRecord.expires_at <= now,
                    )
                    .order_by(CollectionProcessingClaimRecord.expires_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for claim in rows:
                _revoke_capabilities(session, claim.id, now=now)
            return len(rows)


def _claim_list_statement(
    *,
    state: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal,
) -> tuple[
    list[ColumnElement[bool]],
    Select[tuple[CollectionProcessingClaimRecord]],
    tuple[Any, ...],
]:
    filters: list[ColumnElement[bool]] = [
        CollectionProcessingClaimRecord.consumer_app == principal.app
    ]
    if state:
        filters.append(CollectionProcessingClaimRecord.state == state)
    key_columns = tuple(
        dict.fromkeys((_CLAIM_SORT_FIELDS[sort], CollectionProcessingClaimRecord.id))
    )
    return filters, select(CollectionProcessingClaimRecord).where(*filters), key_columns


def _claim_list_position(
    claim: CollectionProcessingClaimRecord,
    *,
    sort: str,
) -> tuple[str, ...]:
    value = getattr(claim, sort)
    if not isinstance(value, str):
        raise RuntimeError("processing-claim browse position has an invalid value")
    return value, claim.id


def _canonical_roots(
    values: Sequence[CollectionRootIdentity],
) -> tuple[CollectionRootIdentity, ...]:
    roots = tuple(values)
    if not roots:
        raise BadRequest("at least one finalized input collection is required")
    normalized = tuple(sorted(roots))
    if roots != normalized or len({item.collection_id for item in roots}) != len(roots):
        raise BadRequest("input collection roots must be unique and canonically ordered")
    return roots


def _retirement_policy(value: str, grace_seconds: int) -> str:
    policy = str(value)
    if policy not in _RETIREMENT_POLICIES:
        raise BadRequest("retirement policy is invalid")
    if isinstance(grace_seconds, bool) or grace_seconds < 0:
        raise BadRequest("retirement grace seconds must be non-negative")
    if policy == "retain" and grace_seconds:
        raise BadRequest("retained collection work cannot declare retirement grace")
    return policy


def _json_document(
    value: Mapping[str, object],
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[bytes, dict[str, object]]:
    if not isinstance(value, Mapping):
        raise BadRequest(f"{label} must be a JSON object")
    try:
        encoded = canonical_json_bytes(value)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BadRequest(f"{label} is invalid: {exc}") from exc
    if not isinstance(normalized, dict):
        raise BadRequest(f"{label} must be a JSON object")
    if len(encoded) > maximum_bytes:
        raise BadRequest(f"{label} exceeds the supported size limit")
    return encoded, cast(dict[str, object], normalized)


def _require_same_claim(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    *,
    work_id: str,
    purpose: str,
    work_document_json: str,
    work_document_sha256: str,
    principal: ApplicationPrincipal,
) -> None:
    if (
        claim.consumer_app != principal.app
        or claim.work_id != work_id
        or claim.purpose != purpose
        or claim.work_document_json != work_document_json
        or claim.work_document_sha256 != work_document_sha256
    ):
        raise Conflict("collection work identity is already owned by another request")


def _clear_plan(session: Session, claim: CollectionProcessingClaimRecord) -> None:
    session.execute(
        delete(CollectionProcessingDispositionSetRecord).where(
            CollectionProcessingDispositionSetRecord.claim_id == claim.id
        )
    )
    session.execute(
        delete(CollectionProcessingClaimArtifactRecord).where(
            CollectionProcessingClaimArtifactRecord.claim_id == claim.id
        )
    )
    session.execute(
        delete(CollectionProcessingClaimOutputTagRecord).where(
            CollectionProcessingClaimOutputTagRecord.claim_id == claim.id
        )
    )
    session.execute(
        delete(CollectionProcessingOutcomeRecord).where(
            CollectionProcessingOutcomeRecord.claim_id == claim.id
        )
    )
    claim.execution_id = None
    claim.controller_evidence_json = None
    claim.controller_evidence_sha256 = None
    claim.operation_id = None
    claim.operation_sha256 = None
    claim.artifact_count = 0
    claim.artifact_bytes = 0
    claim.artifact_hash_state = None
    claim.artifact_set_sha256 = None
    claim.artifacts_sealed_at = None
    claim.output_tag_count = 0
    claim.output_tag_hash_state = None
    claim.output_tag_set_sha256 = None
    claim.output_tags_sealed_at = None
    claim.outcome_count = 0
    claim.outcome_state = "receiving"
    claim.outcome_hash_state = None
    claim.outcome_validation_cursor = None
    claim.outcome_validation_count = 0
    claim.outcome_set_sha256 = None
    claim.outcome_failure = None
    claim.outcomes_sealed_at = None
    claim.retirement_policy = None
    claim.retirement_grace_seconds = 0
    claim.plan_sealed_at = None


def _execution_output_exists(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> bool:
    outcome = session.scalar(
        select(CollectionProcessingOutcomeRecord.claim_id).where(
            CollectionProcessingOutcomeRecord.claim_id == claim.id
        )
    )
    return outcome is not None or _transform_output_exists(session, claim)


def _transform_output_exists(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> bool:
    if claim.execution_id is None:
        return False
    execution_app = f"transform:{claim.execution_id}"
    finalized = session.scalar(
        select(func.count())
        .select_from(CollectionRecord)
        .where(CollectionRecord.created_by_app == execution_app)
    )
    uploading = session.scalar(
        select(func.count())
        .select_from(CollectionUploadRecord)
        .where(CollectionUploadRecord.initiated_by_app == execution_app)
    )
    return bool(int(finalized or 0) or int(uploading or 0))


def _require_no_execution_output(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> None:
    """Fail closed before a fencing restart can orphan an output intent."""

    if _execution_output_exists(session, claim):
        raise Conflict(
            "collection processing claim cannot restart while its prior "
            "execution owns an output collection or upload"
        )


def _require_no_transform_output(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> None:
    if _transform_output_exists(session, claim):
        raise Conflict(
            "collection processing claim cannot terminate while its "
            "execution owns an output collection or upload"
        )


def _capability_app(
    claim: CollectionProcessingClaimRecord,
    actions: Sequence[str],
) -> str:
    if "write-output" in actions:
        if claim.execution_id is None:
            raise InvalidState("write capability has no sealed execution identity")
        return f"transform:{claim.execution_id}"
    return f"claim:{claim.id}"


def _require_sealed_transform_plan(claim: CollectionProcessingClaimRecord) -> None:
    if any(
        value is None
        for value in (
            claim.plan_sealed_at,
            claim.execution_id,
            claim.controller_evidence_json,
            claim.controller_evidence_sha256,
            claim.operation_id,
            claim.operation_sha256,
            claim.input_set_sha256,
            claim.artifact_set_sha256,
            claim.output_tag_set_sha256,
            claim.retirement_policy,
        )
    ):
        raise InvalidState("collection processing claim has no sealed execution plan")


def _bounded_batch(values: Sequence[object], label: str) -> None:
    if not values or len(values) > _DISPOSITION_BATCH_MAX:
        raise BadRequest(f"{label} batch must contain 1 to {_DISPOSITION_BATCH_MAX} items")


def _append_start(value: int, count: int) -> int:
    start = int(value)
    if start < 0 or start > count:
        raise Conflict("append ordinal is outside the staged authority")
    return start


def _page_start(value: int) -> int:
    start = int(value)
    if start < 0:
        raise BadRequest("page start ordinal must be non-negative")
    return start


def _set_checkpoint(state: str | None, domain: str) -> CheckpointSHA256:
    if state is not None:
        return CheckpointSHA256.from_state(state)
    return CheckpointSHA256(f"riverhog-{domain}/v1\0".encode("ascii"))


def _tag_checkpoint(state: str | None) -> CheckpointSHA256:
    if state is not None:
        return CheckpointSHA256.from_state(state)
    return CheckpointSHA256(b'{"format":"riverhog-tag-set/v1","tags":[')


def _checkpoint_tag(checkpoint: CheckpointSHA256, value: str, *, ordinal: int) -> None:
    if ordinal:
        checkpoint.update(b",")
    checkpoint.update(b'"')
    checkpoint.update(value.encode("ascii"))
    checkpoint.update(b'"')


def _checkpoint_item(checkpoint: CheckpointSHA256, value: object) -> None:
    encoded = canonical_json_bytes(value)
    checkpoint.update(len(encoded).to_bytes(8, "big"))
    checkpoint.update(encoded)


def _input_identity(row: CollectionProcessingClaimInputRecord) -> CollectionRootIdentity:
    return CollectionRootIdentity(
        collection_id=row.collection_id,
        archive_root_sha256=row.archive_root_sha256,
        content_identity=row.content_identity,
    )


def _last_input_identity(session: Session, claim_id: str) -> CollectionRootIdentity | None:
    row = session.scalar(
        select(CollectionProcessingClaimInputRecord)
        .where(CollectionProcessingClaimInputRecord.claim_id == claim_id)
        .order_by(CollectionProcessingClaimInputRecord.collection_order.desc())
        .limit(1)
    )
    return _input_identity(row) if row is not None else None


def _artifact_identity(
    session: Session,
    claim_id: str,
    row: CollectionProcessingClaimArtifactRecord,
) -> CollectionArtifactIdentity:
    input_row = session.get(
        CollectionProcessingClaimInputRecord,
        (claim_id, row.collection_id),
    )
    if input_row is None:
        raise InvalidState("claim artifact has no exact input root")
    return CollectionArtifactIdentity(
        collection=_input_identity(input_row),
        path=row.path,
        bytes=row.bytes,
        sha256=row.sha256,
    )


def _last_artifact_identity(session: Session, claim_id: str) -> CollectionArtifactIdentity | None:
    row = session.scalar(
        select(CollectionProcessingClaimArtifactRecord)
        .where(CollectionProcessingClaimArtifactRecord.claim_id == claim_id)
        .order_by(CollectionProcessingClaimArtifactRecord.artifact_order.desc())
        .limit(1)
    )
    return _artifact_identity(session, claim_id, row) if row is not None else None


def _owned_capability(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    capability_id: str,
) -> CollectionTransformCapabilityRecord:
    capability = session.get(CollectionTransformCapabilityRecord, capability_id)
    if capability is None or capability.claim_id != claim.id or capability.fence != claim.fence:
        raise NotFound(f"transform capability not found: {capability_id}")
    return capability


def _capability_artifact_identity(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    row: CollectionTransformCapabilityArtifactRecord,
) -> CollectionArtifactIdentity:
    input_row = session.get(
        CollectionProcessingClaimInputRecord,
        (claim.id, row.collection_id),
    )
    if input_row is None:
        raise InvalidState("capability artifact has no exact input root")
    return CollectionArtifactIdentity(
        collection=_input_identity(input_row),
        path=row.path,
        bytes=row.bytes,
        sha256=row.sha256,
    )


def _last_capability_artifact_identity(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    capability_id: str,
) -> CollectionArtifactIdentity | None:
    row = session.scalar(
        select(CollectionTransformCapabilityArtifactRecord)
        .where(CollectionTransformCapabilityArtifactRecord.capability_id == capability_id)
        .order_by(CollectionTransformCapabilityArtifactRecord.artifact_order.desc())
        .limit(1)
    )
    return _capability_artifact_identity(session, claim, row) if row is not None else None


def _capability_artifact_set_payload(
    capability: CollectionTransformCapabilityRecord,
) -> dict[str, object]:
    authority = (
        {
            "count": capability.artifact_count,
            "total_bytes": capability.artifact_bytes,
            "sha256": capability.artifact_set_sha256,
        }
        if capability.artifact_set_sha256 is not None
        else None
    )
    return {
        "state": "sealed" if authority is not None else "receiving",
        "count": capability.artifact_count,
        "total_bytes": capability.artifact_bytes,
        "authority": authority,
    }


def _require_inputs_sealed(claim: CollectionProcessingClaimRecord) -> None:
    if claim.inputs_sealed_at is None or claim.input_set_sha256 is None:
        raise Conflict("input-root authority is not sealed")


def _require_set_authority(
    count: int,
    actual_sha256: str | None,
    requested_sha256: str,
    label: str,
) -> dict[str, object]:
    expected = _sha256(requested_sha256, f"{label} authority")
    if actual_sha256 is None or actual_sha256 != expected or count < 1:
        raise Conflict(f"{label} authority is unavailable or changed")
    return {"count": count, "sha256": actual_sha256}


def _require_artifact_authority(
    claim: CollectionProcessingClaimRecord,
    requested_sha256: str,
) -> dict[str, object]:
    authority = _require_set_authority(
        claim.artifact_count,
        claim.artifact_set_sha256,
        requested_sha256,
        "artifact",
    )
    authority["total_bytes"] = claim.artifact_bytes
    return authority


def _input_set_payload(claim: CollectionProcessingClaimRecord) -> dict[str, object]:
    authority = (
        {"count": claim.input_count, "sha256": claim.input_set_sha256}
        if claim.input_set_sha256 is not None
        else None
    )
    return {
        "state": "sealed" if authority is not None else "receiving",
        "count": claim.input_count,
        "authority": authority,
    }


def _artifact_set_payload(claim: CollectionProcessingClaimRecord) -> dict[str, object]:
    authority = (
        {
            "count": claim.artifact_count,
            "total_bytes": claim.artifact_bytes,
            "sha256": claim.artifact_set_sha256,
        }
        if claim.artifact_set_sha256 is not None
        else None
    )
    return {
        "state": "sealed" if authority is not None else "receiving",
        "count": claim.artifact_count,
        "total_bytes": claim.artifact_bytes,
        "authority": authority,
    }


def _output_tag_set_payload(claim: CollectionProcessingClaimRecord) -> dict[str, object]:
    authority = (
        {"count": claim.output_tag_count, "sha256": claim.output_tag_set_sha256}
        if claim.output_tag_set_sha256 is not None
        else None
    )
    return {
        "state": "sealed" if authority is not None else "receiving",
        "count": claim.output_tag_count,
        "authority": authority,
    }


def _outcome_set_payload(claim: CollectionProcessingClaimRecord) -> dict[str, object]:
    authority = (
        {"count": claim.outcome_count, "sha256": claim.outcome_set_sha256}
        if claim.outcome_set_sha256 is not None
        else None
    )
    return {
        "state": claim.outcome_state,
        "count": claim.outcome_count,
        "authority": authority,
        "failure": claim.outcome_failure,
    }


def _require_active_generation(
    claim: CollectionProcessingClaimRecord,
    *,
    fence: int,
) -> None:
    """Require the current active fence without requiring an unexpired lease.

    Settlement and terminal abandonment are controller-only reconciliation
    operations. They may complete after lease expiry provided no later fencing
    generation has started. Capability issuance and payload execution continue
    to require a live lease through :func:`_require_live_claim`.
    """

    _require_fence(claim, fence)
    if claim.state != "active":
        raise Conflict("collection processing claim is not active")


def _claim_actor(
    session: Session,
    claim_id: str,
    principal: ApplicationPrincipal,
    *,
    require_write: bool = False,
) -> CollectionProcessingClaimRecord:
    normalized_id = _sha256(claim_id, "claim id")
    claim = session.get(CollectionProcessingClaimRecord, normalized_id)
    if claim is None:
        raise NotFound(f"collection processing claim not found: {claim_id}")
    if claim.consumer_app == principal.app:
        return claim
    capability_id = principal.artifact_scope_capability_id
    capability = (
        session.get(CollectionTransformCapabilityRecord, capability_id)
        if capability_id is not None
        else None
    )
    if (
        capability is None
        or capability.claim_id != claim.id
        or capability.fence != claim.fence
        or capability.state != "active"
        or _expired(capability.expires_at)
    ):
        raise NotFound(f"collection processing claim not found: {claim_id}")
    actions = tuple(sorted(set(json.loads(capability.actions_json))))
    if principal.app != _capability_app(claim, actions) or (
        require_write and "write-output" not in actions
    ):
        raise NotFound(f"collection processing claim not found: {claim_id}")
    return claim


def _claim_execution_actor(
    session: Session,
    claim_id: str,
    *,
    fence: int,
    principal: ApplicationPrincipal,
) -> CollectionProcessingClaimRecord:
    claim = _claim_actor(session, claim_id, principal, require_write=True)
    _require_live_claim(claim, fence=fence)
    _require_sealed_transform_plan(claim)
    return claim


def _receiving_disposition_set(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> CollectionProcessingDispositionSetRecord:
    current = session.scalar(
        select(CollectionProcessingDispositionSetRecord)
        .where(CollectionProcessingDispositionSetRecord.claim_id == claim.id)
        .with_for_update()
    )
    if current is not None:
        return current
    now = utc_timestamp_now()
    current = CollectionProcessingDispositionSetRecord(
        claim_id=claim.id,
        state="receiving",
        disposition_count=0,
        output_edge_count=0,
        output_artifact_count=0,
        transformed_count=0,
        transformed_with_outputs_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(current)
    session.flush()
    return current


def _claim_input_root(
    session: Session,
    claim_id: str,
    collection_id: int,
) -> CollectionRootIdentity:
    root = session.get(CollectionProcessingClaimInputRecord, (claim_id, collection_id))
    if root is None:
        raise Conflict("disposition references an unknown input collection")
    return CollectionRootIdentity(
        collection_id=root.collection_id,
        archive_root_sha256=root.archive_root_sha256,
        content_identity=root.content_identity,
    )


def _require_disposition_input(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    disposition: ArtifactDisposition,
) -> None:
    root = _claim_input_root(session, claim.id, disposition.input_collection_id)
    if root.archive_root_sha256 != disposition.input_archive_root_sha256:
        raise Conflict("disposition input archive root differs from the sealed claim")
    artifact = session.get(
        CollectionProcessingClaimArtifactRecord,
        (claim.id, disposition.input_collection_id, disposition.input_path),
    )
    if artifact is None:
        raise Conflict("disposition references an artifact outside the sealed claim scope")


def _disposition_record_identity(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    record: CollectionProcessingDispositionRecord,
) -> ArtifactDisposition:
    root = _claim_input_root(session, claim.id, record.collection_id)
    return ArtifactDisposition(
        input_collection_id=record.collection_id,
        input_archive_root_sha256=root.archive_root_sha256,
        input_path=record.path,
        status=cast(DispositionState, record.status),
        code=record.failure_code,
        message=record.failure_message,
    )


def _require_disposition_output(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    output: ArtifactDispositionOutput,
) -> None:
    if output.output_path.startswith("riverhog/"):
        raise BadRequest("transform output edges may not bind Riverhog control paths")
    root = _claim_input_root(session, claim.id, output.input_collection_id)
    if root.archive_root_sha256 != output.input_archive_root_sha256:
        raise Conflict("disposition output input root differs from the sealed claim")
    disposition = session.get(
        CollectionProcessingDispositionRecord,
        (claim.id, output.input_collection_id, output.input_path),
    )
    if disposition is None or disposition.status != "transformed":
        raise Conflict("output edge requires a transformed input disposition")


def _disposition_output_record_identity(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    record: CollectionProcessingDispositionOutputRecord,
) -> ArtifactDispositionOutput:
    root = _claim_input_root(session, claim.id, record.input_collection_id)
    return ArtifactDispositionOutput(
        input_collection_id=record.input_collection_id,
        input_archive_root_sha256=root.archive_root_sha256,
        input_path=record.input_path,
        output_path=record.output_path,
    )


def _disposition_set_payload(
    record: CollectionProcessingDispositionSetRecord,
) -> dict[str, object]:
    identity: dict[str, object] | None = None
    if record.state == "sealed":
        assert record.identity_sha256 is not None
        identity = ArtifactDispositionSetIdentity(
            disposition_count=record.disposition_count,
            output_edge_count=record.output_edge_count,
            output_artifact_count=record.output_artifact_count,
            sha256=record.identity_sha256,
        ).as_dict()
    return {
        "claim_id": record.claim_id,
        "state": record.state,
        "disposition_count": int(record.disposition_count),
        "output_edge_count": int(record.output_edge_count),
        "output_artifact_count": int(record.output_artifact_count),
        "identity": identity,
        "failure": record.failure,
    }


def _disposition_set_identity(
    record: CollectionProcessingDispositionSetRecord,
) -> ArtifactDispositionSetIdentity:
    if record.state != "sealed" or record.identity_sha256 is None:
        raise Conflict("disposition authority is not sealed")
    return ArtifactDispositionSetIdentity(
        disposition_count=record.disposition_count,
        output_edge_count=record.output_edge_count,
        output_artifact_count=record.output_artifact_count,
        sha256=record.identity_sha256,
    )


def _sealed_disposition_authority(
    session: Session,
    claim_id: str,
    authority_sha256: str,
) -> CollectionProcessingDispositionSetRecord:
    record = session.get(CollectionProcessingDispositionSetRecord, claim_id)
    if record is None or record.state != "sealed":
        raise Conflict("disposition authority is not sealed")
    if record.identity_sha256 != authority_sha256:
        raise Conflict("disposition continuation is bound to another authority")
    return record


def _advance_disposition_hash(
    session: Session,
    disposition_set: CollectionProcessingDispositionSetRecord,
) -> bool:
    claim = session.get(CollectionProcessingClaimRecord, disposition_set.claim_id)
    if claim is None or disposition_set.disposition_hash_state is None:
        raise RuntimeError("disposition sealing authority is unavailable")
    statement = select(CollectionProcessingDispositionRecord).where(
        CollectionProcessingDispositionRecord.claim_id == disposition_set.claim_id
    )
    if disposition_set.validation_collection_id is not None:
        assert disposition_set.validation_input_path is not None
        statement = statement.where(
            or_(
                CollectionProcessingDispositionRecord.collection_id
                > disposition_set.validation_collection_id,
                and_(
                    CollectionProcessingDispositionRecord.collection_id
                    == disposition_set.validation_collection_id,
                    CollectionProcessingDispositionRecord.path
                    > disposition_set.validation_input_path,
                ),
            )
        )
    rows = list(
        session.scalars(
            statement.order_by(
                CollectionProcessingDispositionRecord.collection_id,
                CollectionProcessingDispositionRecord.path,
            ).limit(_DISPOSITION_VALIDATION_BATCH)
        )
    )
    if not rows:
        return False
    digest = CheckpointSHA256.from_state(disposition_set.disposition_hash_state)
    next_ordinal = (
        int(
            session.scalar(
                select(
                    func.coalesce(
                        func.max(CollectionProcessingDispositionRecord.disposition_order), -1
                    )
                ).where(CollectionProcessingDispositionRecord.claim_id == disposition_set.claim_id)
            )
        )
        + 1
    )
    for ordinal, row in enumerate(rows, start=next_ordinal):
        row.disposition_order = ordinal
        identity = _disposition_record_identity(session, claim, row)
        digest.update(canonical_json_bytes(identity.as_dict()) + b"\n")
        disposition_set.validation_collection_id = row.collection_id
        disposition_set.validation_input_path = row.path
    disposition_set.disposition_hash_state = digest.export_state()
    disposition_set.updated_at = utc_timestamp_now()
    return True


def _advance_disposition_output_hash(
    session: Session,
    disposition_set: CollectionProcessingDispositionSetRecord,
) -> bool:
    claim = session.get(CollectionProcessingClaimRecord, disposition_set.claim_id)
    if claim is None or disposition_set.output_hash_state is None:
        raise RuntimeError("disposition output sealing authority is unavailable")
    statement = select(CollectionProcessingDispositionOutputRecord).where(
        CollectionProcessingDispositionOutputRecord.claim_id == disposition_set.claim_id
    )
    if disposition_set.validation_output_path is not None:
        assert disposition_set.validation_output_collection_id is not None
        assert disposition_set.validation_output_input_path is not None
        statement = statement.where(
            or_(
                CollectionProcessingDispositionOutputRecord.output_path
                > disposition_set.validation_output_path,
                and_(
                    CollectionProcessingDispositionOutputRecord.output_path
                    == disposition_set.validation_output_path,
                    CollectionProcessingDispositionOutputRecord.input_collection_id
                    > disposition_set.validation_output_collection_id,
                ),
                and_(
                    CollectionProcessingDispositionOutputRecord.output_path
                    == disposition_set.validation_output_path,
                    CollectionProcessingDispositionOutputRecord.input_collection_id
                    == disposition_set.validation_output_collection_id,
                    CollectionProcessingDispositionOutputRecord.input_path
                    > disposition_set.validation_output_input_path,
                ),
            )
        )
    rows = list(
        session.scalars(
            statement.order_by(
                CollectionProcessingDispositionOutputRecord.output_path,
                CollectionProcessingDispositionOutputRecord.input_collection_id,
                CollectionProcessingDispositionOutputRecord.input_path,
            ).limit(_DISPOSITION_VALIDATION_BATCH)
        )
    )
    if not rows:
        return False
    digest = CheckpointSHA256.from_state(disposition_set.output_hash_state)
    next_ordinal = (
        int(
            session.scalar(
                select(
                    func.coalesce(
                        func.max(CollectionProcessingDispositionOutputRecord.output_order), -1
                    )
                ).where(
                    CollectionProcessingDispositionOutputRecord.claim_id == disposition_set.claim_id
                )
            )
        )
        + 1
    )
    for ordinal, row in enumerate(rows, start=next_ordinal):
        row.output_order = ordinal
        identity = _disposition_output_record_identity(session, claim, row)
        digest.update(canonical_json_bytes(identity.as_dict()) + b"\n")
        disposition_set.validation_output_path = row.output_path
        disposition_set.validation_output_collection_id = row.input_collection_id
        disposition_set.validation_output_input_path = row.input_path
    disposition_set.output_hash_state = digest.export_state()
    disposition_set.updated_at = utc_timestamp_now()
    return True


def _require_existing_settlement(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    *,
    output_collection_id: int,
    document: CollectionDerivation,
) -> None:
    """Accept only an exact replay of an already committed settlement.

    A controller may crash after Riverhog commits settlement but before its local
    record advances. Replaying the same fenced settlement must converge without
    reopening or mutating the claim. Any changed output or derivation remains a
    conflict, including after retirement has begun or the claim has been released.
    """

    output_id = int(output_collection_id)
    encoded = document.to_json_bytes().decode("utf-8")
    if claim.output_collection_id != output_id or claim.settled_at is None:
        raise Conflict("collection processing claim has a different settlement")
    record = session.get(CollectionDerivationRecord, output_id)
    if (
        record is None
        or record.claim_id != claim.id
        or record.fence != claim.fence
        or record.execution_id != claim.execution_id
        or record.document_json != encoded
        or record.document_sha256 != document.sha256
    ):
        raise Conflict("collection processing claim has different derivation evidence")
    _collection_root(session, output_id)


def _outcome_binding_args(
    outcome_claim_id: str | None,
    outcome_fence: int | None,
    outcome_id: str | None,
) -> tuple[str, int, str] | None:
    values = (outcome_claim_id, outcome_fence, outcome_id)
    if all(item is None for item in values):
        return None
    if any(item is None for item in values):
        raise BadRequest("processing outcome binding is incomplete")
    assert outcome_claim_id is not None
    assert outcome_fence is not None
    assert outcome_id is not None
    if isinstance(outcome_fence, bool) or outcome_fence < 1:
        raise BadRequest("processing outcome fence must be positive")
    return outcome_claim_id, int(outcome_fence), outcome_id


def _attach_processing_outcome(
    session: Session,
    *,
    source_claim: CollectionProcessingClaimRecord,
    output_collection_id: int,
    derivation: CollectionDerivation,
    outcome_claim_id: str | None,
    outcome_fence: int | None,
    outcome_id: str | None,
) -> None:
    binding = _outcome_binding_args(
        outcome_claim_id,
        outcome_fence,
        outcome_id,
    )
    if binding is None:
        return
    parent_id, parent_fence, label = binding
    if parent_id == source_claim.id:
        raise Conflict("collection work cannot depend on its own output")
    parent = session.scalar(
        select(CollectionProcessingClaimRecord)
        .where(CollectionProcessingClaimRecord.id == parent_id)
        .with_for_update()
    )
    if parent is None or parent.consumer_app != source_claim.consumer_app:
        raise NotFound(f"collection processing claim not found: {parent_id}")
    _require_fence(parent, parent_fence)
    _require_active_generation(parent, fence=parent_fence)
    if parent.plan_sealed_at is not None or parent.execution_id is not None:
        raise Conflict("an executed collection claim cannot retain delegated outcomes")
    if parent.output_collection_id is not None:
        raise InvalidState("outcome claim unexpectedly contains a direct output")
    if parent.outcome_state != "receiving":
        raise Conflict("processing outcome authority no longer accepts results")
    root = _collection_root(session, output_collection_id)
    try:
        identity = CollectionProcessingOutcomeIdentity(
            outcome_id=label,
            source_claim_id=source_claim.id,
            output_collection=root,
            derivation_sha256=derivation.sha256,
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    existing = session.get(
        CollectionProcessingOutcomeRecord,
        (parent.id, identity.outcome_id),
    )
    if existing is not None:
        if _outcome_identity(existing) != identity:
            raise Conflict("processing outcome identity is already bound differently")
        return
    source_conflict = session.scalar(
        select(CollectionProcessingOutcomeRecord).where(
            CollectionProcessingOutcomeRecord.claim_id == parent.id,
            CollectionProcessingOutcomeRecord.source_claim_id == source_claim.id,
        )
    )
    output_conflict = session.scalar(
        select(CollectionProcessingOutcomeRecord).where(
            CollectionProcessingOutcomeRecord.claim_id == parent.id,
            CollectionProcessingOutcomeRecord.collection_id == root.collection_id,
        )
    )
    if source_conflict is not None or output_conflict is not None:
        raise Conflict("processing outcome reuses a source claim or output collection")
    session.add(
        CollectionProcessingOutcomeRecord(
            claim_id=parent.id,
            outcome_id=identity.outcome_id,
            source_claim_id=identity.source_claim_id,
            collection_id=root.collection_id,
            archive_root_sha256=root.archive_root_sha256,
            content_identity=root.content_identity,
            derivation_sha256=identity.derivation_sha256,
            outcome_order=None,
            created_at=utc_timestamp_now(),
        )
    )
    parent.outcome_count += 1
    parent.updated_at = utc_timestamp_now()


def _advance_outcome_set(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> None:
    if claim.outcome_hash_state is None or claim.outcome_state != "sealing":
        raise RuntimeError("outcome sealing authority is unavailable")
    statement = select(CollectionProcessingOutcomeRecord).where(
        CollectionProcessingOutcomeRecord.claim_id == claim.id
    )
    if claim.outcome_validation_cursor is not None:
        statement = statement.where(
            CollectionProcessingOutcomeRecord.outcome_id > claim.outcome_validation_cursor
        )
    rows = list(
        session.scalars(
            statement.order_by(CollectionProcessingOutcomeRecord.outcome_id).limit(
                _DISPOSITION_VALIDATION_BATCH
            )
        )
    )
    if not rows:
        if claim.outcome_validation_count != claim.outcome_count:
            raise RuntimeError("outcome authority changed while sealing")
        claim.outcome_set_sha256 = CheckpointSHA256.from_state(claim.outcome_hash_state).hexdigest()
        claim.outcome_state = "sealed"
        claim.outcomes_sealed_at = utc_timestamp_now()
        claim.updated_at = claim.outcomes_sealed_at
        return
    digest = CheckpointSHA256.from_state(claim.outcome_hash_state)
    for row in rows:
        identity = _outcome_identity(row)
        if _collection_root(session, row.collection_id) != identity.output_collection:
            raise Conflict("processing outcome collection root changed")
        derivation = session.get(CollectionDerivationRecord, row.collection_id)
        if (
            derivation is None
            or derivation.claim_id != row.source_claim_id
            or derivation.document_sha256 != row.derivation_sha256
        ):
            raise Conflict("processing outcome derivation is unavailable")
        row.outcome_order = claim.outcome_validation_count
        _checkpoint_item(digest, identity.as_dict())
        claim.outcome_validation_count += 1
        claim.outcome_validation_cursor = row.outcome_id
    claim.outcome_hash_state = digest.export_state()
    claim.updated_at = utc_timestamp_now()


def _require_existing_outcome_binding(
    session: Session,
    *,
    source_claim: CollectionProcessingClaimRecord,
    outcome_claim_id: str | None,
    outcome_fence: int | None,
    outcome_id: str | None,
) -> None:
    binding = _outcome_binding_args(
        outcome_claim_id,
        outcome_fence,
        outcome_id,
    )
    rows = list(
        session.scalars(
            select(CollectionProcessingOutcomeRecord).where(
                CollectionProcessingOutcomeRecord.source_claim_id == source_claim.id
            )
        )
    )
    if binding is None:
        if rows:
            raise Conflict("collection work settlement has a processing outcome binding")
        return
    parent_id, parent_fence, label = binding
    parent = session.get(CollectionProcessingClaimRecord, parent_id)
    if parent is None or parent.fence != parent_fence:
        raise Conflict("processing outcome generation differs from settlement")
    if len(rows) != 1 or (
        rows[0].claim_id,
        rows[0].outcome_id,
    ) != (parent_id, label):
        raise Conflict("processing outcome binding differs from settlement")


def _outcome_identity(
    record: CollectionProcessingOutcomeRecord,
) -> CollectionProcessingOutcomeIdentity:
    return CollectionProcessingOutcomeIdentity(
        outcome_id=record.outcome_id,
        source_claim_id=record.source_claim_id,
        output_collection=CollectionRootIdentity(
            collection_id=record.collection_id,
            archive_root_sha256=record.archive_root_sha256,
            content_identity=record.content_identity,
        ),
        derivation_sha256=record.derivation_sha256,
    )


def _processing_outcomes(
    session: Session,
    claim_id: str,
) -> list[CollectionProcessingOutcomeIdentity]:
    return [
        _outcome_identity(item)
        for item in session.scalars(
            select(CollectionProcessingOutcomeRecord)
            .where(CollectionProcessingOutcomeRecord.claim_id == claim_id)
            .order_by(CollectionProcessingOutcomeRecord.outcome_id)
        )
    ]


def _canonical_outcomes(
    values: Sequence[CollectionProcessingOutcomeIdentity],
) -> tuple[CollectionProcessingOutcomeIdentity, ...]:
    outcomes = tuple(values)
    if not outcomes:
        raise BadRequest("outcome settlement requires verified collection outputs")
    if outcomes != tuple(sorted(outcomes)):
        raise BadRequest("processing outcomes must be canonically ordered")
    ids = [item.outcome_id for item in outcomes]
    claims = [item.source_claim_id for item in outcomes]
    outputs = [item.output_collection.collection_id for item in outcomes]
    if (
        len(ids) != len(set(ids))
        or len(claims) != len(set(claims))
        or len(outputs) != len(set(outputs))
    ):
        raise BadRequest("processing outcomes must be exact and unique")
    return outcomes


def _verify_dispositions(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    disposition_set: CollectionProcessingDispositionSetRecord,
    output: CollectionRecord,
) -> None:
    if disposition_set.disposition_count != claim.artifact_count:
        raise Conflict("derivation does not account for every input artifact exactly once")
    # A maintained derived-collection producer contributes exactly two Riverhog
    # control files: producer evidence and this small derivation summary. Every
    # other registered file was admitted only after its output-edge membership
    # existed, so the scalar count proves exact output coverage without loading
    # the collection or edge set.
    if output.file_count != disposition_set.output_artifact_count + 2:
        raise Conflict("derivation output paths do not match the derived collection artifacts")


def _verified_disposition_set(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    identity: ArtifactDispositionSetIdentity,
) -> CollectionProcessingDispositionSetRecord:
    record = session.get(CollectionProcessingDispositionSetRecord, claim.id)
    if (
        record is None
        or record.state != "sealed"
        or record.identity_sha256 != identity.sha256
        or record.disposition_count != identity.disposition_count
        or record.output_edge_count != identity.output_edge_count
        or record.output_artifact_count != identity.output_artifact_count
    ):
        raise Conflict("derivation disposition authority differs from the sealed claim")
    return record


def _require_outcome_retirement_coverage(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> None:
    if (
        session.scalar(
            select(CollectionProcessingOutcomeRecord.claim_id)
            .where(CollectionProcessingOutcomeRecord.claim_id == claim.id)
            .limit(1)
        )
        is None
    ):
        raise InvalidState("settled collection work has no verified outcomes")
    parent_input = aliased(CollectionProcessingClaimInputRecord)
    child_input = aliased(CollectionProcessingClaimInputRecord)
    safe = (
        select(CollectionProcessingDispositionRecord.claim_id)
        .join(
            CollectionProcessingOutcomeRecord,
            and_(
                CollectionProcessingOutcomeRecord.claim_id == claim.id,
                CollectionProcessingOutcomeRecord.source_claim_id
                == CollectionProcessingDispositionRecord.claim_id,
            ),
        )
        .join(
            child_input,
            and_(
                child_input.claim_id == CollectionProcessingDispositionRecord.claim_id,
                child_input.collection_id == CollectionProcessingDispositionRecord.collection_id,
            ),
        )
        .join(
            CollectionProcessingDispositionSetRecord,
            and_(
                CollectionProcessingDispositionSetRecord.claim_id
                == CollectionProcessingDispositionRecord.claim_id,
                CollectionProcessingDispositionSetRecord.state == "sealed",
            ),
        )
        .where(
            CollectionProcessingDispositionRecord.collection_id
            == CollectionFileRecord.collection_id,
            CollectionProcessingDispositionRecord.path == CollectionFileRecord.path,
            CollectionProcessingDispositionRecord.status.in_(("transformed", "preserved")),
            child_input.archive_root_sha256 == parent_input.archive_root_sha256,
            child_input.content_identity == parent_input.content_identity,
        )
        .correlate(CollectionFileRecord, parent_input)
        .exists()
    )
    missing = session.execute(
        select(CollectionFileRecord.collection_id, CollectionFileRecord.path)
        .join(
            parent_input,
            and_(
                parent_input.claim_id == claim.id,
                parent_input.collection_id == CollectionFileRecord.collection_id,
            ),
        )
        .where(
            ~CollectionFileRecord.path.startswith("riverhog/"),
            ~safe,
        )
        .order_by(CollectionFileRecord.collection_id, CollectionFileRecord.path_sort_key)
        .limit(1)
    ).first()
    if missing is not None:
        raise Conflict(
            "source retirement lacks a verified safe disposition for: "
            f"{missing.collection_id}::{missing.path}"
        )


def _canonical_artifacts(
    values: Sequence[CollectionArtifactIdentity],
) -> tuple[CollectionArtifactIdentity, ...]:
    artifacts = tuple(sorted(values))
    if not artifacts:
        raise BadRequest("exact artifact scope must not be empty")
    keys = [(item.collection.collection_id, item.path) for item in artifacts]
    if len(keys) != len(set(keys)):
        raise BadRequest("exact artifact scope must not repeat a collection file")
    return artifacts


def _validate_claim_artifacts(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    artifacts: Sequence[CollectionArtifactIdentity],
) -> None:
    roots = {
        item.collection_id: CollectionRootIdentity(
            collection_id=item.collection_id,
            archive_root_sha256=item.archive_root_sha256,
            content_identity=item.content_identity,
        )
        for item in _claim_inputs(session, claim.id)
    }
    for artifact in artifacts:
        root = roots.get(artifact.collection.collection_id)
        if root != artifact.collection:
            raise Conflict("artifact scope is outside the exact claim roots")
        current = session.get(
            CollectionFileRecord,
            (artifact.collection.collection_id, artifact.path),
        )
        if current is None or current.bytes != artifact.bytes or current.sha256 != artifact.sha256:
            raise Conflict("artifact scope differs from the immutable collection file")


def _claim_artifacts(
    session: Session,
    claim_id: str,
) -> list[CollectionProcessingClaimArtifactRecord]:
    return list(
        session.scalars(
            select(CollectionProcessingClaimArtifactRecord)
            .where(CollectionProcessingClaimArtifactRecord.claim_id == claim_id)
            .order_by(
                CollectionProcessingClaimArtifactRecord.collection_id,
                CollectionProcessingClaimArtifactRecord.path,
            )
        )
    )


def _claim_artifact_identities(
    session: Session,
    claim_id: str,
) -> tuple[CollectionArtifactIdentity, ...]:
    roots = {
        item.collection_id: CollectionRootIdentity(
            collection_id=item.collection_id,
            archive_root_sha256=item.archive_root_sha256,
            content_identity=item.content_identity,
        )
        for item in _claim_inputs(session, claim_id)
    }
    return tuple(
        CollectionArtifactIdentity(
            collection=roots[item.collection_id],
            path=item.path,
            bytes=item.bytes,
            sha256=item.sha256,
        )
        for item in _claim_artifacts(session, claim_id)
    )


def _collection_payload_paths(session: Session, collection_id: int) -> tuple[str, ...]:
    return tuple(
        path
        for path in session.scalars(
            select(CollectionFileRecord.path)
            .where(CollectionFileRecord.collection_id == collection_id)
            .order_by(CollectionFileRecord.path_sort_key)
        )
        if not path.startswith("riverhog/")
    )


def _collection_root(
    session: Session,
    collection_id: int,
    *,
    lock: bool = False,
) -> CollectionRootIdentity:
    statement = select(CollectionRecord).where(
        CollectionRecord.id == int(collection_id),
        CollectionRecord.is_published.is_(True),
    )
    if lock:
        statement = statement.with_for_update()
    collection = session.scalar(statement)
    if collection is None:
        raise NotFound(f"finalized collection not found: {collection_id}")
    if session.get(CollectionDeletionRecord, collection.id) is not None:
        raise Conflict(f"collection deletion is active: {collection.id}")
    roots = set(
        session.scalars(
            select(CollectionArchiveObjectRecord.sha256).where(
                CollectionArchiveObjectRecord.collection_id == collection.id,
                CollectionArchiveObjectRecord.object_id == "manifest",
                CollectionArchiveObjectRecord.verified_at.is_not(None),
            )
        )
    )
    roots.discard(None)
    if len(roots) != 1:
        raise InvalidState(
            f"collection has no unambiguous verified immutable root: {collection.id}"
        )
    return CollectionRootIdentity(
        collection_id=collection.id,
        archive_root_sha256=str(next(iter(roots))),
        content_identity=collection.content_identity,
    )


def _require_output_tags(session: Session, values: Sequence[str]) -> tuple[str, ...]:
    tags = tuple(str(value) for value in values)
    normalized = tuple(sorted(set(tags)))
    if not normalized or tags != normalized:
        raise BadRequest("output tags must be nonempty, unique, and canonical")
    found = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(normalized))))
    missing = sorted(set(normalized) - found)
    if missing:
        raise BadRequest("output tags do not exist: " + ", ".join(missing))
    return normalized


def _owned_claim(
    session: Session,
    claim_id: str,
    principal: ApplicationPrincipal,
    *,
    lock: bool = False,
) -> CollectionProcessingClaimRecord:
    statement = select(CollectionProcessingClaimRecord).where(
        CollectionProcessingClaimRecord.id == claim_id,
        CollectionProcessingClaimRecord.consumer_app == principal.app,
    )
    if lock:
        statement = statement.with_for_update()
    claim = session.scalar(statement)
    if claim is None:
        raise NotFound(f"collection processing claim not found: {claim_id}")
    return claim


def _claim_inputs(
    session: Session,
    claim_id: str,
) -> list[CollectionProcessingClaimInputRecord]:
    return list(
        session.scalars(
            select(CollectionProcessingClaimInputRecord)
            .where(CollectionProcessingClaimInputRecord.claim_id == claim_id)
            .order_by(CollectionProcessingClaimInputRecord.collection_order)
        )
    )


def _claim_payload(
    session: Session,
    claim: CollectionProcessingClaimRecord,
) -> dict[str, object]:
    plan: dict[str, object] | None = None
    if claim.plan_sealed_at is not None:
        _require_sealed_transform_plan(claim)
        assert claim.execution_id is not None
        assert claim.controller_evidence_json is not None
        assert claim.controller_evidence_sha256 is not None
        assert claim.operation_id is not None
        assert claim.operation_sha256 is not None
        assert claim.input_set_sha256 is not None
        assert claim.artifact_set_sha256 is not None
        assert claim.output_tag_set_sha256 is not None
        assert claim.retirement_policy is not None
        plan = {
            "execution_id": claim.execution_id,
            "controller_evidence": json.loads(claim.controller_evidence_json),
            "controller_evidence_sha256": claim.controller_evidence_sha256,
            "operation": {
                "id": claim.operation_id,
                "sha256": claim.operation_sha256,
            },
            "inputs": {
                "count": claim.input_count,
                "sha256": claim.input_set_sha256,
            },
            "artifacts": {
                "count": claim.artifact_count,
                "total_bytes": claim.artifact_bytes,
                "sha256": claim.artifact_set_sha256,
            },
            "output_tags": {
                "count": claim.output_tag_count,
                "sha256": claim.output_tag_set_sha256,
            },
            "retirement_policy": claim.retirement_policy,
            "retirement_grace_seconds": claim.retirement_grace_seconds,
            "sealed_at": claim.plan_sealed_at,
        }
    outcome_settlement = None
    if claim.plan_sealed_at is None and claim.settled_at is not None:
        if claim.retirement_policy is None or claim.outcome_set_sha256 is None:
            raise InvalidState("settled collection work has no exact outcome settlement")
        outcome_settlement = {
            "outcomes": {
                "count": claim.outcome_count,
                "sha256": claim.outcome_set_sha256,
            },
            "retirement_policy": claim.retirement_policy,
            "retirement_grace_seconds": claim.retirement_grace_seconds,
        }
    return {
        "format": "riverhog-processing-claim/v1",
        "id": claim.id,
        "work_id": claim.work_id,
        "consumer": {"app": claim.consumer_app, "key_id": claim.consumer_key_id},
        "purpose": claim.purpose,
        "state": claim.state,
        "fence": claim.fence,
        "expires_at": claim.expires_at,
        "created_at": claim.created_at,
        "updated_at": claim.updated_at,
        "settled_at": claim.settled_at,
        "abandoned_at": claim.abandoned_at,
        "abandonment_reason": claim.abandonment_reason,
        "released_at": claim.released_at,
        "output_collection_id": claim.output_collection_id,
        "work_document": json.loads(claim.work_document_json),
        "work_document_sha256": claim.work_document_sha256,
        "inputs": _input_set_payload(claim),
        "plan": plan,
        "outcomes": _outcome_set_payload(claim),
        "outcome_settlement": outcome_settlement,
    }


def _require_fence(claim: CollectionProcessingClaimRecord, fence: int) -> None:
    if claim.fence != int(fence):
        raise Conflict("collection processing claim fence is stale")


def _require_live_claim(claim: CollectionProcessingClaimRecord, *, fence: int) -> None:
    _require_fence(claim, fence)
    if claim.state != "active" or _expired(claim.expires_at):
        raise Conflict("collection processing claim is not active")


def _revoke_capabilities(session: Session, claim_id: str, *, now: str) -> None:
    rows = list(
        session.scalars(
            select(CollectionTransformCapabilityRecord).where(
                CollectionTransformCapabilityRecord.claim_id == claim_id,
                CollectionTransformCapabilityRecord.state == "active",
            )
        )
    )
    for capability in rows:
        capability.state = "revoked"
        capability.revoked_at = now


def _expired(value: str) -> bool:
    return parse_utc_timestamp(value) <= utc_now()


def _lease_seconds(value: int) -> int:
    if isinstance(value, bool):
        parsed = -1
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = -1
    if not _MIN_LEASE_SECONDS <= parsed <= _MAX_LEASE_SECONDS:
        raise BadRequest(
            f"lease seconds must be between {_MIN_LEASE_SECONDS} and {_MAX_LEASE_SECONDS}"
        )
    return parsed


def _sha256(value: str, label: str) -> str:
    normalized = str(value).casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise BadRequest(f"{label} must be a lowercase SHA-256")
    return normalized


def _visible(value: str, label: str, *, maximum: int) -> str:
    text = str(value)
    if not text or text != text.strip() or len(text) > maximum:
        raise BadRequest(f"{label} is invalid")
    return text


def processing_claim_blockers(
    session: Session,
    collection_id: int,
    *,
    exempt_claim_id: str | None = None,
    limit: int = COLLECTION_DELETION_BLOCKER_CATEGORY_SAMPLE_MAX,
) -> list[str]:
    """Return active workflow claims that must block collection deletion."""

    now = utc_timestamp_now()
    execution_app = literal("transform:") + CollectionProcessingClaimRecord.execution_id
    finalized_output_exists = (
        select(CollectionRecord.id).where(CollectionRecord.created_by_app == execution_app).exists()
    )
    output_upload_exists = (
        select(CollectionUploadRecord.collection_id)
        .where(CollectionUploadRecord.initiated_by_app == execution_app)
        .exists()
    )
    claimed_input = (
        select(CollectionProcessingClaimInputRecord.collection_id)
        .where(
            CollectionProcessingClaimInputRecord.claim_id == CollectionProcessingClaimRecord.id,
            CollectionProcessingClaimInputRecord.collection_id == collection_id,
        )
        .exists()
    )
    pending_output = (
        select(CollectionRecord.id)
        .where(
            CollectionRecord.id == collection_id,
            CollectionRecord.created_by_app == execution_app,
        )
        .exists()
    )
    retained_outcome = (
        select(CollectionProcessingOutcomeRecord.collection_id)
        .where(
            CollectionProcessingOutcomeRecord.claim_id == CollectionProcessingClaimRecord.id,
            CollectionProcessingOutcomeRecord.collection_id == collection_id,
        )
        .exists()
    )
    active_output_is_unsettled = or_(
        CollectionProcessingClaimRecord.execution_id.is_not(None)
        & or_(finalized_output_exists, output_upload_exists),
        retained_outcome,
    )
    statement = (
        select(
            CollectionProcessingClaimRecord.id,
            CollectionProcessingClaimRecord.state,
            CollectionProcessingClaimRecord.consumer_app,
        )
        .where(
            or_(
                claimed_input,
                CollectionProcessingClaimRecord.output_collection_id == collection_id,
                pending_output,
                retained_outcome,
            ),
            or_(
                CollectionProcessingClaimRecord.state.in_(("settled", "retiring")),
                (CollectionProcessingClaimRecord.state == "active")
                & or_(
                    CollectionProcessingClaimRecord.expires_at > now,
                    active_output_is_unsettled,
                ),
            ),
        )
        .order_by(CollectionProcessingClaimRecord.created_at)
        .limit(limit + 1)
    )
    if exempt_claim_id is not None:
        statement = statement.where(CollectionProcessingClaimRecord.id != exempt_claim_id)
    rows = list(session.execute(statement))
    result = [
        f"collection processing claim is {state}: {claim_id} ({consumer})"
        for claim_id, state, consumer in rows[:limit]
    ]
    if len(rows) > limit:
        result.append("additional collection processing claims exist; list claims for details")
    return result


def require_retirement_exemption(
    session: Session,
    *,
    claim_id: str,
    collection_id: int,
    principal: ApplicationPrincipal,
) -> dict[str, object]:
    claim = session.get(CollectionProcessingClaimRecord, claim_id)
    if claim is None or claim.consumer_app != principal.app or claim.state != "retiring":
        raise Forbidden("retirement claim does not authorize collection deletion")
    input_row = session.get(CollectionProcessingClaimInputRecord, (claim_id, collection_id))
    direct_output_ready = claim.output_collection_id is not None
    delegated_output_ready = (
        claim.outcome_state == "sealed"
        and claim.outcome_count > 0
        and claim.outcome_set_sha256 is not None
    )
    if input_row is None or not (direct_output_ready or delegated_output_ready):
        raise Forbidden("retirement claim does not authorize this input collection")
    outcome_set_sha256 = claim.outcome_set_sha256
    return RetirementClaimReferenceDocument(
        claim_id=claim.id,
        fence=claim.fence,
        work_id=claim.work_id,
        execution_id=claim.execution_id,
        output_collection_id=claim.output_collection_id,
        outcomes=(
            ExactSetAuthorityDocument(
                count=claim.outcome_count,
                sha256=cast(str, outcome_set_sha256),
            )
            if delegated_output_ready
            else None
        ),
    ).model_dump(mode="json")


__all__ = [
    "SqlAlchemyCollectionWorkflowService",
    "processing_claim_blockers",
    "require_retirement_exemption",
]
