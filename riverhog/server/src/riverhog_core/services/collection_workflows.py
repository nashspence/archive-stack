"""Generic collection work claims, scoped capabilities, and derivation verification."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import cast

from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    canonical_json_bytes,
    canonical_json_sha256,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, InvalidState, NotFound
from sqlalchemy import asc, desc, func, literal, or_, select
from sqlalchemy.orm import Session
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_CREATE,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
    collection_resource,
    tag_resource,
)
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import (
    CollectionDerivationRecord,
    CollectionProcessingClaimInputRecord,
    CollectionProcessingClaimRecord,
    CollectionTransformCapabilityRecord,
)
from riverhog_core.runtime_config import RuntimeConfig

_MIN_LEASE_SECONDS = 30
_MAX_LEASE_SECONDS = 24 * 60 * 60
_DEFAULT_LEASE_SECONDS = 30 * 60
_MAX_WORK_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_CONTROLLER_EVIDENCE_BYTES = 16 * 1024 * 1024
_CAPABILITY_ACTIONS = frozenset({"read-inputs", "write-output"})
_CAPABILITY_AUDIENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,299}$", re.ASCII)
_RETIREMENT_POLICIES = frozenset({"retain", "retire-after-verified-output"})
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
        inputs: Sequence[CollectionRootIdentity],
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
            maximum_bytes=_MAX_WORK_DOCUMENT_BYTES,
        )
        normalized_work_sha256 = _sha256(
            work_document_sha256,
            "work document identity",
        )
        if hashlib.sha256(encoded_work).hexdigest() != normalized_work_sha256:
            raise BadRequest("work document identity does not match its canonical JSON")
        normalized_inputs = _canonical_roots(inputs)
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
            for expected in normalized_inputs:
                if _collection_root(session, expected.collection_id, lock=True) != expected:
                    raise Conflict(
                        f"input collection root differs from the claimed identity: "
                        f"{expected.collection_id}"
                    )
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
                    inputs=normalized_inputs,
                    principal=principal,
                )
                if claim.state == "active" and _expired(claim.expires_at):
                    _require_no_execution_output(session, claim)
                    claim.fence += 1
                    claim.expires_at = expires_at
                    claim.updated_at = now
                    _clear_plan(claim)
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
            session.add(claim)
            session.flush()
            session.add_all(
                CollectionProcessingClaimInputRecord(
                    claim_id=claim.id,
                    collection_id=root.collection_id,
                    collection_order=index,
                    manifest_sha256=root.manifest_sha256,
                    content_etag=root.content_etag,
                )
                for index, root in enumerate(normalized_inputs)
            )
            session.flush()
            return _claim_payload(session, claim)

    def get_claim(
        self,
        claim_id: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            return _claim_payload(session, _owned_claim(session, claim_id, principal))

    def list_claims(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        state: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        all_items: bool = False,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("claim pagination is invalid")
        if sort not in _CLAIM_SORT_FIELDS or order not in {"asc", "desc"}:
            raise BadRequest("claim sorting is invalid")
        filters = [CollectionProcessingClaimRecord.consumer_app == principal.app]
        if state:
            filters.append(CollectionProcessingClaimRecord.state == state)
        direction = desc if order == "desc" else asc
        statement = (
            select(CollectionProcessingClaimRecord)
            .where(*filters)
            .order_by(
                direction(_CLAIM_SORT_FIELDS[sort]),
                asc(CollectionProcessingClaimRecord.id),
            )
        )
        with session_scope(self._session_factory) as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(
                        select(CollectionProcessingClaimRecord.id).where(*filters).subquery()
                    )
                )
                or 0
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.scalars(statement))
            return {
                "page": 1 if all_items else page,
                "per_page": total if all_items else per_page,
                "total": total,
                "pages": (1 if total else 0) if all_items else (total + per_page - 1) // per_page,
                "sort": sort,
                "order": order,
                "filters": {"state": state},
                "claims": [_claim_payload(session, current) for current in rows],
            }

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
            _clear_plan(claim)
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
        output_tags: Sequence[str],
        retirement_policy: str,
        retirement_grace_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_execution_id = _sha256(execution_id, "execution identity")
        evidence_bytes, evidence = _json_document(
            controller_evidence,
            label="controller evidence",
            maximum_bytes=_MAX_CONTROLLER_EVIDENCE_BYTES,
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
        policy = str(retirement_policy)
        if policy not in _RETIREMENT_POLICIES:
            raise BadRequest("retirement policy is invalid")
        if isinstance(retirement_grace_seconds, bool) or retirement_grace_seconds < 0:
            raise BadRequest("retirement grace seconds must be non-negative")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            tags = _require_output_tags(session, output_tags)
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
                    json.dumps(list(tags), separators=(",", ":")),
                    policy,
                    int(retirement_grace_seconds),
                )
                actual = (
                    claim.execution_id,
                    claim.controller_evidence_json,
                    claim.controller_evidence_sha256,
                    claim.operation_id,
                    claim.operation_sha256,
                    claim.output_tags_json,
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
            claim.output_tags_json = json.dumps(list(tags), separators=(",", ":"))
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
                state="active",
                expires_at=format_utc_timestamp(expiry),
                created_at=now,
            )
            session.add(capability)
            claim.updated_at = now
            session.flush()
            return {
                "format": "riverhog-transform-capability/v1",
                "id": capability.id,
                "claim_id": claim.id,
                "fence": claim.fence,
                "audience": capability.audience,
                "actions": list(normalized_actions),
                "principal_app": _capability_app(claim, normalized_actions),
                "expires_at": capability.expires_at,
                "token": token,
            }

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
            if "read-inputs" in actions:
                for item in _claim_inputs(session, claim.id):
                    resource = collection_resource(item.collection_id)
                    grants.update(
                        {
                            ApplicationAccess(CATALOG_READ, resource),
                            ApplicationAccess(RETRIEVAL_MANAGE, resource),
                            ApplicationAccess(PROVENANCE_READ, resource),
                            ApplicationAccess(PROVENANCE_EXPORT, resource),
                        }
                    )
            if "write-output" in actions:
                assert claim.output_tags_json is not None
                for tag in json.loads(claim.output_tags_json):
                    grants.add(ApplicationAccess(COLLECTIONS_CREATE, tag_resource(str(tag))))
            app = _capability_app(claim, actions)
            return ApplicationPrincipal(
                app=app,
                # Preserve the initiating key for download-budget attribution and
                # revocation while the synthetic app keeps claim-scoped ownership
                # stable across capability refreshes.
                key_id=claim.consumer_key_id,
                access=frozenset(grants),
            )

    def settle_claim(
        self,
        claim_id: str,
        *,
        fence: int,
        output_collection_id: int,
        derivation: Mapping[str, object],
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        try:
            document = CollectionDerivation.from_mapping(derivation)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_fence(claim, fence)
            _require_sealed_plan(claim)
            if claim.state in {"settled", "retiring", "released"}:
                _require_existing_settlement(
                    session,
                    claim,
                    output_collection_id=output_collection_id,
                    document=document,
                )
                return _claim_payload(session, claim)
            _require_active_generation(claim, fence=fence)
            assert claim.execution_id is not None
            assert claim.controller_evidence_json is not None
            assert claim.controller_evidence_sha256 is not None
            assert claim.operation_id is not None
            assert claim.operation_sha256 is not None
            assert claim.output_tags_json is not None
            inputs = tuple(
                CollectionRootIdentity(
                    collection_id=item.collection_id,
                    manifest_sha256=item.manifest_sha256,
                    content_etag=item.content_etag,
                )
                for item in _claim_inputs(session, claim.id)
            )
            expected_evidence = cast(dict[str, object], json.loads(claim.controller_evidence_json))
            if (
                document.claim_id != claim.id
                or document.execution_id != claim.execution_id
                or document.fence != claim.fence
                or document.inputs != inputs
                or document.operation
                != OperationIdentity(claim.operation_id, claim.operation_sha256)
                or document.output_tags
                != tuple(str(item) for item in json.loads(claim.output_tags_json))
                or document.execution_envelope_sha256 != claim.execution_id
                or document.controller_evidence != expected_evidence
                or document.controller_evidence_sha256 != claim.controller_evidence_sha256
            ):
                raise Conflict("derivation differs from the sealed collection work plan")
            output = session.scalar(
                select(CollectionRecord)
                .where(CollectionRecord.id == int(output_collection_id))
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
            actual_tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == output.id)
                    .order_by(CollectionTagRecord.tag_id)
                )
            )
            if actual_tags != document.output_tags:
                raise Conflict("derived collection tags differ from the sealed work plan")
            evidence = session.get(
                CollectionFileRecord,
                (output.id, DERIVATION_EVIDENCE_PATH),
            )
            if evidence is None or evidence.sha256 != document.sha256:
                raise Conflict("derived collection does not contain its exact derivation evidence")
            _verify_dispositions(session, document, output.id)
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
            now = utc_timestamp_now()
            claim.state = "settled"
            claim.output_collection_id = output.id
            claim.settled_at = claim.settled_at or now
            claim.updated_at = now
            _revoke_capabilities(session, claim.id, now=now)
            session.flush()
            return _claim_payload(session, claim)

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
            if claim.settled_at is None or claim.output_collection_id is None:
                raise InvalidState("settled claim has no settlement identity")
            derivation_record = session.get(CollectionDerivationRecord, claim.output_collection_id)
            if derivation_record is None:
                raise InvalidState("settled claim has no verified derivation record")
            derivation = CollectionDerivation.from_mapping(
                json.loads(derivation_record.document_json)
            )
            unsafe = [
                item.input_path
                for item in derivation.dispositions
                if item.status not in {"transformed", "preserved"}
            ]
            if unsafe:
                raise Conflict(
                    "source retirement is not authorized for omitted or rejected artifacts: "
                    + ", ".join(unsafe[:10])
                )
            _collection_root(session, claim.output_collection_id)
            eligible_at = parse_utc_timestamp(claim.settled_at) + timedelta(
                seconds=claim.retirement_grace_seconds
            )
            if utc_now() < eligible_at:
                raise Conflict(
                    "collection processing claim retirement grace period has not elapsed"
                )
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
            _require_no_execution_output(session, claim)
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
    inputs: tuple[CollectionRootIdentity, ...],
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
    stored = tuple(
        CollectionRootIdentity(
            collection_id=item.collection_id,
            manifest_sha256=item.manifest_sha256,
            content_etag=item.content_etag,
        )
        for item in _claim_inputs(session, claim.id)
    )
    if stored != inputs:
        raise Conflict("collection work identity was reused with different input roots")


def _clear_plan(claim: CollectionProcessingClaimRecord) -> None:
    claim.execution_id = None
    claim.controller_evidence_json = None
    claim.controller_evidence_sha256 = None
    claim.operation_id = None
    claim.operation_sha256 = None
    claim.output_tags_json = None
    claim.retirement_policy = None
    claim.retirement_grace_seconds = 0
    claim.plan_sealed_at = None


def _execution_output_exists(
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


def _capability_app(
    claim: CollectionProcessingClaimRecord,
    actions: Sequence[str],
) -> str:
    if "write-output" in actions:
        if claim.execution_id is None:
            raise InvalidState("write capability has no sealed execution identity")
        return f"transform:{claim.execution_id}"
    return f"claim:{claim.id}"


def _require_sealed_plan(claim: CollectionProcessingClaimRecord) -> None:
    if any(
        value is None
        for value in (
            claim.plan_sealed_at,
            claim.execution_id,
            claim.controller_evidence_json,
            claim.controller_evidence_sha256,
            claim.operation_id,
            claim.operation_sha256,
            claim.output_tags_json,
            claim.retirement_policy,
        )
    ):
        raise InvalidState("collection processing claim has no sealed execution plan")


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


def _require_existing_settlement(
    session: Session,
    claim: CollectionProcessingClaimRecord,
    *,
    output_collection_id: int,
    document: CollectionDerivation,
) -> None:
    """Accept only an exact replay of an already committed settlement.

    stove0 may crash after Riverhog commits settlement but before its local work
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


def _verify_dispositions(
    session: Session,
    document: CollectionDerivation,
    output_collection_id: int,
) -> None:
    expected_artifacts = {
        (root.collection_id, path)
        for root in document.inputs
        for path in _collection_payload_paths(session, root.collection_id)
    }
    actual_artifacts = {
        (item.input_collection_id, item.input_path) for item in document.dispositions
    }
    if actual_artifacts != expected_artifacts or len(actual_artifacts) != len(
        document.dispositions
    ):
        raise Conflict("derivation does not account for every input artifact exactly once")
    output_artifacts = set(_collection_payload_paths(session, output_collection_id))
    derived_artifacts = {path for item in document.dispositions for path in item.outputs}
    if derived_artifacts != output_artifacts:
        raise Conflict("derivation output paths do not match the derived collection artifacts")


def _collection_payload_paths(session: Session, collection_id: int) -> tuple[str, ...]:
    return tuple(
        path
        for path in session.scalars(
            select(CollectionFileRecord.path)
            .where(CollectionFileRecord.collection_id == collection_id)
            .order_by(CollectionFileRecord.path)
        )
        if not path.startswith("riverhog/")
    )


def _collection_root(
    session: Session,
    collection_id: int,
    *,
    lock: bool = False,
) -> CollectionRootIdentity:
    statement = select(CollectionRecord).where(CollectionRecord.id == int(collection_id))
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
        manifest_sha256=str(next(iter(roots))),
        content_etag=collection.content_etag,
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
        _require_sealed_plan(claim)
        assert claim.execution_id is not None
        assert claim.controller_evidence_json is not None
        assert claim.controller_evidence_sha256 is not None
        assert claim.operation_id is not None
        assert claim.operation_sha256 is not None
        assert claim.output_tags_json is not None
        assert claim.retirement_policy is not None
        plan = {
            "execution_id": claim.execution_id,
            "controller_evidence": json.loads(claim.controller_evidence_json),
            "controller_evidence_sha256": claim.controller_evidence_sha256,
            "operation": {
                "id": claim.operation_id,
                "sha256": claim.operation_sha256,
            },
            "output_tags": json.loads(claim.output_tags_json),
            "retirement_policy": claim.retirement_policy,
            "retirement_grace_seconds": claim.retirement_grace_seconds,
            "sealed_at": claim.plan_sealed_at,
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
        "inputs": [
            CollectionRootIdentity(
                collection_id=item.collection_id,
                manifest_sha256=item.manifest_sha256,
                content_etag=item.content_etag,
            ).as_dict()
            for item in _claim_inputs(session, claim.id)
        ],
        "plan": plan,
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
    limit: int = 10,
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
    active_execution_is_unsettled = CollectionProcessingClaimRecord.execution_id.is_not(None) & or_(
        finalized_output_exists, output_upload_exists
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
            ),
            or_(
                CollectionProcessingClaimRecord.state.in_(("settled", "retiring")),
                (CollectionProcessingClaimRecord.state == "active")
                & or_(
                    CollectionProcessingClaimRecord.expires_at > now,
                    active_execution_is_unsettled,
                ),
            ),
        )
        .order_by(CollectionProcessingClaimRecord.created_at)
        .limit(limit)
    )
    if exempt_claim_id is not None:
        statement = statement.where(CollectionProcessingClaimRecord.id != exempt_claim_id)
    return [
        f"collection processing claim is {state}: {claim_id} ({consumer})"
        for claim_id, state, consumer in session.execute(statement)
    ]


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
    if input_row is None or claim.output_collection_id is None:
        raise Forbidden("retirement claim does not authorize this input collection")
    return {
        "claim_id": claim.id,
        "fence": claim.fence,
        "work_id": claim.work_id,
        "execution_id": claim.execution_id,
        "output_collection_id": claim.output_collection_id,
    }


__all__ = [
    "SqlAlchemyCollectionWorkflowService",
    "processing_claim_blockers",
    "require_retirement_exemption",
]
