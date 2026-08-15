"""Collection workflow claims, scoped capabilities, and derivation verification."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, cast

from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    RetirementPolicy,
    TransformIntent,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, InvalidState, NotFound
from sqlalchemy import asc, desc, func, or_, select
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
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
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
_CAPABILITY_ACTIONS = frozenset({"read-inputs", "write-output"})
_CLAIM_SORT_FIELDS = {
    "created_at": CollectionProcessingClaimRecord.created_at,
    "updated_at": CollectionProcessingClaimRecord.updated_at,
    "expires_at": CollectionProcessingClaimRecord.expires_at,
    "state": CollectionProcessingClaimRecord.state,
    "transform_id": CollectionProcessingClaimRecord.transform_id,
}


class SqlAlchemyCollectionWorkflowService:
    """Own generic Riverhog collection-processing coordination state."""

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
        input_collection_ids: Sequence[int],
        recipe_id: str,
        recipe_revision: int,
        recipe_sha256: str,
        operation_id: str,
        operation_sha256: str,
        effective_intent: Mapping[str, object],
        output_tags: Sequence[str],
        retirement_policy: RetirementPolicy,
        retirement_grace_seconds: int,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        purpose: str = "collection-transform/v1",
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        lease = _lease_seconds(lease_seconds)
        if not input_collection_ids:
            raise BadRequest("at least one finalized input collection is required")
        normalized_ids = tuple(sorted({int(value) for value in input_collection_ids}))
        if len(normalized_ids) != len(input_collection_ids):
            raise BadRequest("input collections must be unique")
        with session_scope(self._session_factory) as session:
            roots = tuple(_collection_root(session, collection_id) for collection_id in normalized_ids)
            _require_output_tags(session, output_tags)
            try:
                intent = TransformIntent.seal(
                    recipe=RecipeIdentity(recipe_id, recipe_revision, recipe_sha256),
                    operation=OperationIdentity(operation_id, operation_sha256),
                    inputs=roots,
                    effective_intent=effective_intent,
                    output_tags=output_tags,
                    retirement_policy=retirement_policy,
                    retirement_grace_seconds=retirement_grace_seconds,
                )
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            now = utc_timestamp_now()
            expires_at = format_utc_timestamp(utc_now() + timedelta(seconds=lease))
            claim = session.scalar(
                select(CollectionProcessingClaimRecord)
                .where(CollectionProcessingClaimRecord.id == intent.transform_id)
                .with_for_update()
            )
            intent_json = intent.to_json_bytes().decode("utf-8")
            if claim is not None:
                if claim.consumer_app != principal.app or claim.intent_json != intent_json:
                    raise Conflict("collection transform identity is already owned")
                if claim.state == "active" and _expired(claim.expires_at):
                    claim.fence += 1
                    claim.expires_at = expires_at
                    claim.updated_at = now
                    _revoke_capabilities(session, claim.id, now=now)
                elif claim.state == "active" and parse_utc_timestamp(expires_at) > parse_utc_timestamp(
                    claim.expires_at
                ):
                    claim.expires_at = expires_at
                    claim.updated_at = now
                return _claim_payload(session, claim)

            claim = CollectionProcessingClaimRecord(
                id=intent.transform_id,
                transform_id=intent.transform_id,
                consumer_app=principal.app,
                consumer_key_id=principal.key_id,
                purpose=_visible(purpose, "claim purpose", maximum=160),
                intent_json=intent_json,
                recipe_id=intent.recipe.id,
                recipe_revision=intent.recipe.revision,
                recipe_sha256=intent.recipe.sha256,
                operation_id=intent.operation.id,
                operation_sha256=intent.operation.sha256,
                output_tags_json=json.dumps(list(intent.output_tags), separators=(",", ":")),
                retirement_policy=intent.retirement_policy,
                retirement_grace_seconds=intent.retirement_grace_seconds,
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
                for index, root in enumerate(intent.inputs)
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
            claim = _owned_claim(session, claim_id, principal)
            return _claim_payload(session, claim)

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
        statement = select(CollectionProcessingClaimRecord).where(*filters).order_by(
            direction(_CLAIM_SORT_FIELDS[sort]),
            asc(CollectionProcessingClaimRecord.id),
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
                "pages": (1 if total else 0)
                if all_items
                else (total + per_page - 1) // per_page,
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
            if claim.state != "active" or _expired(claim.expires_at):
                raise Conflict("collection processing claim is not renewable")
            claim.expires_at = format_utc_timestamp(utc_now() + timedelta(seconds=lease))
            claim.updated_at = utc_timestamp_now()
            return _claim_payload(session, claim)

    def issue_capability(
        self,
        claim_id: str,
        *,
        fence: int,
        actions: Sequence[str],
        ttl_seconds: int,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        ttl = _lease_seconds(ttl_seconds)
        normalized_actions = tuple(sorted(set(str(item) for item in actions)))
        if not normalized_actions or not set(normalized_actions).issubset(_CAPABILITY_ACTIONS):
            raise BadRequest("transform capability actions are invalid")
        with session_scope(self._session_factory) as session:
            claim = _owned_claim(session, claim_id, principal, lock=True)
            _require_live_claim(claim, fence=fence)
            claim_expiry = parse_utc_timestamp(claim.expires_at)
            requested_expiry = utc_now() + timedelta(seconds=ttl)
            expiry = min(claim_expiry, requested_expiry)
            token = "rhc_" + secrets.token_urlsafe(32)
            now = utc_timestamp_now()
            capability = CollectionTransformCapabilityRecord(
                id=secrets.token_hex(16),
                claim_id=claim.id,
                fence=claim.fence,
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
                "actions": list(normalized_actions),
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
            actions = set(json.loads(capability.actions_json))
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
                for tag in json.loads(claim.output_tags_json):
                    grants.add(ApplicationAccess(COLLECTIONS_CREATE, tag_resource(str(tag))))
            return ApplicationPrincipal(
                app=f"transform:{claim.id}",
                key_id=capability.id,
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
            _require_live_claim(claim, fence=fence)
            if claim.id != document.claim_id or claim.transform_id != document.transform_id:
                raise Conflict("derivation does not bind the claimed transform")
            intent = TransformIntent.from_mapping(json.loads(claim.intent_json))
            if (
                document.fence != claim.fence
                or document.recipe != intent.recipe
                or document.operation != intent.operation
                or document.inputs != intent.inputs
                or document.output_tags != intent.output_tags
            ):
                raise Conflict("derivation differs from the claimed transform intent")
            output = session.get(CollectionRecord, int(output_collection_id))
            if output is None:
                raise NotFound(f"derived collection not found: {output_collection_id}")
            if output.created_by_app != f"transform:{claim.id}":
                raise Conflict("derived collection was not created by the claimed capability")
            actual_tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == output.id)
                    .order_by(CollectionTagRecord.tag_id)
                )
            )
            if actual_tags != intent.output_tags:
                raise Conflict("derived collection tags differ from the transform intent")
            evidence = session.get(
                CollectionFileRecord,
                (output.id, DERIVATION_EVIDENCE_PATH),
            )
            if evidence is None or evidence.sha256 != document.sha256:
                raise Conflict("derived collection does not contain its exact derivation evidence")
            expected_artifacts = {
                (item.collection_id, path)
                for item in intent.inputs
                for path in session.scalars(
                    select(CollectionFileRecord.path).where(
                        CollectionFileRecord.collection_id == item.collection_id,
                        ~CollectionFileRecord.path.in_(
                            (PRODUCER_EVIDENCE_PATH, DERIVATION_EVIDENCE_PATH)
                        ),
                    )
                )
            }
            actual_artifacts = {
                (item.input_collection_id, item.input_path) for item in document.dispositions
            }
            if actual_artifacts != expected_artifacts:
                raise Conflict("derivation does not account for every input artifact exactly once")
            _collection_root(session, output.id)
            existing = session.get(CollectionDerivationRecord, output.id)
            encoded = document.to_json_bytes().decode("utf-8")
            if existing is not None:
                if existing.document_json != encoded or existing.transform_id != claim.transform_id:
                    raise Conflict("derived collection already has different derivation evidence")
            else:
                session.add(
                    CollectionDerivationRecord(
                        collection_id=output.id,
                        transform_id=claim.transform_id,
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
            if claim.state != "settled" or claim.retirement_policy != "retire-after-verified-output":
                raise Conflict("collection processing claim is not eligible for retirement")
            if claim.settled_at is None or claim.output_collection_id is None:
                raise InvalidState("settled claim has no settlement identity")
            derivation_record = session.get(CollectionDerivationRecord, claim.output_collection_id)
            if derivation_record is None:
                raise InvalidState("settled claim has no verified derivation record")
            derivation = CollectionDerivation.from_mapping(json.loads(derivation_record.document_json))
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
                raise Conflict("collection processing claim retirement grace period has not elapsed")
            claim.state = "retiring"
            claim.updated_at = utc_timestamp_now()
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


def _collection_root(session: Session, collection_id: int) -> CollectionRootIdentity:
    collection = session.get(CollectionRecord, int(collection_id))
    if collection is None:
        raise NotFound(f"finalized collection not found: {collection_id}")
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


def _require_output_tags(session: Session, values: Sequence[str]) -> None:
    tags = tuple(sorted({str(value) for value in values}))
    if not tags or len(tags) != len(values):
        raise BadRequest("output tags must be nonempty and unique")
    found = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags))))
    missing = sorted(set(tags) - found)
    if missing:
        raise BadRequest("output tags do not exist: " + ", ".join(missing))


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
    return {
        "format": "riverhog-processing-claim/v1",
        "id": claim.id,
        "transform_id": claim.transform_id,
        "consumer": {"app": claim.consumer_app, "key_id": claim.consumer_key_id},
        "purpose": claim.purpose,
        "state": claim.state,
        "fence": claim.fence,
        "expires_at": claim.expires_at,
        "created_at": claim.created_at,
        "updated_at": claim.updated_at,
        "settled_at": claim.settled_at,
        "released_at": claim.released_at,
        "output_collection_id": claim.output_collection_id,
        "intent": json.loads(claim.intent_json),
        "inputs": [
            CollectionRootIdentity(
                collection_id=item.collection_id,
                manifest_sha256=item.manifest_sha256,
                content_etag=item.content_etag,
            ).as_dict()
            for item in _claim_inputs(session, claim.id)
        ],
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
    statement = (
        select(
            CollectionProcessingClaimRecord.id,
            CollectionProcessingClaimRecord.state,
            CollectionProcessingClaimRecord.consumer_app,
        )
        .join(
            CollectionProcessingClaimInputRecord,
            CollectionProcessingClaimInputRecord.claim_id == CollectionProcessingClaimRecord.id,
        )
        .where(
            CollectionProcessingClaimInputRecord.collection_id == collection_id,
            or_(
                CollectionProcessingClaimRecord.state.in_(("settled", "retiring")),
                (
                    CollectionProcessingClaimRecord.state == "active"
                )
                & (CollectionProcessingClaimRecord.expires_at > now),
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
        "transform_id": claim.transform_id,
        "output_collection_id": claim.output_collection_id,
    }


__all__ = [
    "SqlAlchemyCollectionWorkflowService",
    "processing_claim_blockers",
    "require_retirement_exemption",
]
