from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta
from itertools import zip_longest
from typing import Any, Literal, TypedDict, cast

from http_api_contracts import closed_literal_values
from riverhog_archive_contracts import (
    CollectionArchiveTerminalDocument,
    CollectionArchiveVolumeDocument,
    CollectionEncryptionBinding,
    format_archive_sequence,
    update_archive_sequence_commitment,
)
from riverhog_protocol import (
    CapturedFileProvenanceBinding,
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyMode,
    CollectionUploadCustodyObjectDocument,
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadProvenanceJournalCreateDocument,
    CollectionUploadProvenanceJournalStatusDocument,
    CollectionUploadRawDigestBatchDocument,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    OmittedFileProvenanceBinding,
    PortableCollectionFile,
    PortableCollectionHeader,
    SortOrder,
    collection_upload_path_order_key,
    collection_upload_raw_digest_summary,
    validate_collection_upload_batch_against_registration_constraints,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, NotFound
from riverhog_protocol.manifest import collection_content_identity_ordered
from riverhog_protocol.pack_ingress import canonical_json_bytes
from riverhog_protocol.paths import (
    normalize_collection_id,
    normalize_tag,
    relpath_search_key,
    relpath_sort_key,
    tag_set_identity,
    text_search_key,
)
from riverhog_protocol.raw_ingress import (
    RawSourceDigestSummary,
    advance_raw_part_commitment,
    raw_volume_part_span,
)
from riverhog_protocol.transport import (
    COLLECTION_UPLOAD_FILE_BATCH_MAX,
    COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX,
)
from riverhog_provenance import (
    PROVENANCE_BINDING_SEGMENT_FILES_MAX,
    PROVENANCE_JOURNAL_ENTRY_BYTES_MAX,
    PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
    DerivativeJournalSeed,
    ExternalStateReference,
    FileProvenanceBinding,
    ProvenancePayloadIdentity,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceValidationError,
    ProvenanceVolumeDocument,
    bounded_binding_segment_bytes,
    create_derivative_journal_seed,
    create_derivative_source_entry,
    format_provenance_sequence,
    update_ordered_volume_commitment,
)
from riverhog_provenance.journal import (
    resolve_incremental_journal_current_state,
    validate_incremental_journal_entry,
)
from sqlalchemy import asc, case, desc, exists, func, insert, or_, select, true, update
from sqlalchemy.orm import Session, selectinload
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    ApplicationPrincipal,
)
from riverhog_core.archive_manifest import (
    build_collection_archive_root_manifest,
    build_collection_archive_terminal_document,
    build_collection_archive_volume_document,
)
from riverhog_core.archive_provenance import (
    ArchiveProvenancePublisher,
    SealedArchiveProvenance,
)
from riverhog_core.archive_recovery_descriptor import (
    ArchiveRecoveryDescriptorPublisher,
)
from riverhog_core.archive_root import (
    ArchiveRootPublisher,
    SealedArchiveVolumeMetadata,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    snapshot_catalog_event_collection_tags,
)
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProvenanceEntityRecord,
    CollectionProvenanceExternalStateReferenceRecord,
    CollectionProvenanceJournalAgentRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadProvenanceArchiveVolumeRecord,
    CollectionUploadProvenanceJournalChunkRecord,
    CollectionUploadProvenanceJournalRecord,
    CollectionUploadProvenanceReachabilityRecord,
    CollectionUploadProvenanceSourceRecord,
    CollectionUploadProvenanceValidationFactRecord,
    CollectionUploadRawPartDigestRecord,
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    RetrievalCacheLeaseRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import (
    CollectionProcessingClaimRecord,
    CollectionProcessingDispositionOutputRecord,
    CollectionProcessingDispositionSetRecord,
)
from riverhog_core.checkpoint_sha256 import CheckpointSHA256
from riverhog_core.collection_access import (
    collection_ids,
    permission_resources,
    require_collection_create_access,
    tag_ids,
)
from riverhog_core.collection_creation_identity import (
    CollectionUploadCreationIdentityDocument,
    CollectionUploadCreationIdentityPayload,
)
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    RawVolumePlan,
    SealedPackVolume,
    SealedRawVolume,
    StoredArchivePart,
)
from riverhog_core.incremental_plan import (
    OrderedArchiveFile,
    advance_incremental_volume_plan,
    incremental_volume_planner_checkpoint_bytes,
    new_incremental_volume_planner,
    parse_incremental_volume_planner_checkpoint,
)
from riverhog_core.pack_upload import PackUploadCheckpoint, PackVolumeUploader
from riverhog_core.pack_volume import (
    pack_unit_descriptors,
    pack_volume_plan_bytes,
    parse_pack_volume_plan,
)
from riverhog_core.ports.archive_objects import ArchiveResumableObjectStore
from riverhog_core.ports.retrieval_cache import RetrievalCache
from riverhog_core.raw_upload import RawUploadCheckpoint, RawVolumeUploader
from riverhog_core.raw_volume import parse_raw_volume_plan, raw_volume_plan_bytes
from riverhog_core.retrieval_cache_receipts import (
    parse_retrieval_cache_receipt,
    retrieval_cache_receipt_payload,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
)
from riverhog_core.services.operation_plans import (
    PLAN_TTL,
    challenge_expiry,
    challenge_has_shape,
    plan_challenge,
)
from riverhog_core.services.retrieval_cache import register_cache_ready
from riverhog_core.stores.mirrored_archive_resumable_object_store import (
    MirroredArchiveResumableObjectStore,
)
from riverhog_core.stores.sqlalchemy_archive_upload_checkpoints import (
    SqlAlchemyArchiveUploadCheckpointStore,
)
from riverhog_core.streaming_age import ResumableAgeSessionCache
from riverhog_core.throughput import (
    ArchiveThroughputTuning,
    ArchiveTransferResources,
    log_transfer_timing,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROVENANCE_JOURNAL_CHUNK_BYTES = 1024 * 1024
_FINALIZATION_FILE_BATCH = 1024
_LOG = logging.getLogger("riverhog_core.collection_uploads")
_DISCARD_CHALLENGE_PREFIX = "discard-upload"
_UPLOAD_SORT_FIELDS = closed_literal_values(CollectionUploadSort)
_UPLOAD_STATES = closed_literal_values(CollectionUploadState)
_SORT_ORDERS = closed_literal_values(SortOrder)
_CUSTODY_LOSS_WARNING = (
    "This permanently destroys Riverhog-custodied artifacts from an incomplete upload session."
)


class _RegisteredFile(TypedDict):
    path: str
    bytes: int
    sha256: str
    raw_part_plaintext_bytes: int | None
    raw_part_count: int | None
    raw_part_ordered_sha256: str | None
    provenance_status: str
    provenance_journal_id: str | None
    provenance_current_state_id: str | None
    provenance_omission_reason: str | None


class SqlAlchemyCollectionUploadService:
    """Own direct-to-final collection ingress and its final catalog transaction."""

    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        retrieval_cache: RetrievalCache | None = None,
        policy: CollectionVolumePolicy | None = None,
        session_factory: SessionFactory | None = None,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._retrieval_cache = retrieval_cache
        self._policy = policy or CollectionVolumePolicy.from_env(os.environ)
        self._session_factory = session_factory or make_session_factory(config.database_url)
        self._checkpoints = SqlAlchemyArchiveUploadCheckpointStore(
            config,
            session_factory=self._session_factory,
        )
        self._events = SqlAlchemyLifecycleEventService(
            config,
            session_factory=self._session_factory,
        )
        tuning = throughput_tuning or ArchiveThroughputTuning.from_env(os.environ)
        self._throughput = tuning
        self._resources = transfer_resources or ArchiveTransferResources.from_tuning(tuning)
        self._age_sessions = {
            passphrase_id: ResumableAgeSessionCache(
                passphrase,
                max_entries=tuning.age_session_cache_entries,
                derivation_gate=self._resources.age_derivations,
            )
            for passphrase_id, passphrase in config.archive_passphrases.items()
        }

    def create_or_resume(
        self,
        *,
        idempotency_key: str,
        initial_tag: str | None,
        tag_set_identity_sha256: str,
        ingest_source: str | None,
        archive_store: str | None,
        initiator: ApplicationPrincipal,
        event_context: Mapping[str, object] | None,
        provenance_mode: str = "captured",
        provenance_omission_reason: str | None = None,
        custody_mode: str = "producer-retained",
    ) -> dict[str, object]:
        key = _normalize_idempotency_key(idempotency_key)
        normalized_initial_tag = normalize_tag(initial_tag) if initial_tag is not None else None
        if normalized_initial_tag != initial_tag:
            raise BadRequest("initial collection tag must be canonical")
        if _SHA256_RE.fullmatch(tag_set_identity_sha256) is None:
            raise BadRequest("collection tag-set identity is invalid")
        store_name = archive_store or self._config.archive_write_store
        try:
            archive_binding = self._archive_stores.require(store_name)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        require_collection_create_access(
            initiator,
            COLLECTIONS_CREATE,
            (() if normalized_initial_tag is None else (normalized_initial_tag,)),
        )
        context_json = event_context_json(event_context)
        normalized_provenance_mode, normalized_omission_reason = _normalize_provenance_mode(
            provenance_mode,
            provenance_omission_reason,
        )
        normalized_custody_mode = _normalize_custody_mode(custody_mode)
        creation_identity = _collection_upload_creation_identity(
            tag_set_identity_sha256=tag_set_identity_sha256,
            ingest_source=ingest_source,
            archive_store=store_name,
            event_context_json=context_json,
            provenance_mode=normalized_provenance_mode,
            provenance_omission_reason=normalized_omission_reason,
            custody_mode=normalized_custody_mode,
        )

        with session_scope(self._session_factory) as session:
            _require_transform_output_intent(
                session,
                initiator=initiator,
                idempotency_key=key,
                tag_set_identity_sha256=tag_set_identity_sha256,
                ingest_source=ingest_source,
                archive_store=archive_store,
            )
            collection = session.scalar(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.archive_copies))
                .where(
                    CollectionRecord.created_by_app == initiator.app,
                    CollectionRecord.creation_idempotency_key == key,
                    CollectionRecord.is_published.is_(True),
                )
            )
            if collection is not None:
                if collection.creation_identity_sha256 != (
                    creation_identity.creation_identity_sha256
                ):
                    raise Conflict("collection upload idempotency identity changed")
                return _finalized_payload(
                    session,
                    collection,
                    store_name=store_name,
                    resumed=True,
                )
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(
                    CollectionUploadRecord.initiated_by_app == initiator.app,
                    CollectionUploadRecord.idempotency_key == key,
                )
                .with_for_update()
            )
            if upload is not None:
                if upload.creation_identity_sha256 != creation_identity.creation_identity_sha256:
                    raise Conflict("collection upload idempotency identity changed")
                if upload.state == "orphaned":
                    checkpoint = _planner_checkpoint(upload)
                    upload.state = "closing" if checkpoint.closed else "open"
                    upload.orphaned_at = None
                    resumed_at = utc_timestamp_now()
                    _touch_upload(upload, config=self._config, now=resumed_at)
                    upload.archive_phase = (
                        "uploading" if checkpoint.closed or upload.archive_objects else "planning"
                    )
                    upload.archive_phase_updated_at = resumed_at
                    upload.archive_failure = None
                    upload.archive_next_attempt_at = None
                elif upload.state == "discarding":
                    raise Conflict("collection upload discard is in progress")
                return _upload_payload(session, upload, resumed=True)

            if normalized_initial_tag is not None:
                _require_tags(session, (normalized_initial_tag,))
            now = utc_timestamp_now()
            checkpoint = new_incremental_volume_planner(policy=self._policy)
            upload = CollectionUploadRecord(
                idempotency_key=key,
                creation_identity_sha256=creation_identity.creation_identity_sha256,
                archive_generation=secrets.token_hex(32),
                tag_set_identity=tag_set_identity_sha256,
                ingest_source=ingest_source,
                search_text=text_search_key(ingest_source or ""),
                provenance_mode=normalized_provenance_mode,
                provenance_omission_reason=normalized_omission_reason,
                encryption_format=self._config.archive_active_encryption.format,
                passphrase_id=self._config.archive_active_encryption.passphrase_id,
                initiated_by_app=initiator.app,
                initiated_by_key_id=initiator.key_id,
                event_context_json=context_json,
                state="open",
                custody_mode=normalized_custody_mode,
                lease_expires_at=(
                    _custody_lease_expiry(self._config)
                    if normalized_custody_mode == "custody-transfer"
                    else None
                ),
                archive_store=store_name,
                opened_at=now,
                last_activity_at=now,
                archive_phase="planning",
                archive_phase_updated_at=now,
                archive_storage_prefix=(
                    archive_binding.store.new_collection_archive_storage_prefix()
                ),
                planner_checkpoint_json=(
                    incremental_volume_planner_checkpoint_bytes(checkpoint).decode("utf-8")
                ),
                derivative_provenance_state=(
                    "discovering" if initiator.app.startswith("transform:") else "not-required"
                ),
            )
            session.add(upload)
            session.flush()
            if normalized_initial_tag is not None:
                session.add(
                    CollectionUploadTagRecord(
                        collection_id=upload.collection_id,
                        tag_id=normalized_initial_tag,
                    )
                )
            session.flush()
            return _upload_payload(session, upload, resumed=False)

    def require_access(self, collection_id: int, principal: ApplicationPrincipal) -> None:
        normalized = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized)
            if upload is not None:
                if upload.initiated_by_app != principal.app:
                    raise NotFound(f"collection upload not found: {normalized}")
                _require_upload_tag_access(
                    session,
                    principal,
                    normalized,
                )
                return
            collection = session.get(CollectionRecord, normalized)
            if collection is None or collection.created_by_app != principal.app:
                raise NotFound(f"collection upload not found: {normalized}")
            _require_collection_tag_create_access(session, principal, normalized)

    def require_read_access(self, collection_id: int, principal: ApplicationPrincipal) -> None:
        """Allow the owning producer or a collection-scoped deletion operator to inspect."""

        normalized = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized)
            if upload is not None:
                if upload.initiated_by_app == principal.app:
                    _require_upload_tag_access(
                        session,
                        principal,
                        normalized,
                    )
                    return
                if _upload_visible_to_deleter(session, upload, principal):
                    return
                raise NotFound(f"collection upload not found: {normalized}")
            collection = session.get(CollectionRecord, normalized)
            if collection is None:
                raise NotFound(f"collection upload not found: {normalized}")
            if collection.created_by_app == principal.app:
                _require_collection_tag_create_access(session, principal, normalized)
                return
            resources = permission_resources(principal, COLLECTIONS_DELETE)
            allowed_tags = tag_ids(resources)
            if (
                ALL_RESOURCES in resources
                or normalized in collection_ids(resources)
                or (
                    allowed_tags
                    and session.scalar(
                        select(CollectionTagRecord.collection_id)
                        .where(CollectionTagRecord.collection_id == normalized)
                        .where(CollectionTagRecord.tag_id.in_(allowed_tags))
                        .limit(1)
                    )
                    is not None
                )
            ):
                return
            raise NotFound(f"collection upload not found: {normalized}")

    def list_tags(
        self,
        collection_id: int,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("invalid collection upload tag pagination")
        with read_snapshot(self._session_factory) as session:
            if session.get(CollectionUploadRecord, normalized_id) is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            total = _upload_tag_count(session, normalized_id)
            rows = list(
                session.scalars(
                    select(CollectionUploadTagRecord.tag_id)
                    .where(CollectionUploadTagRecord.collection_id == normalized_id)
                    .order_by(CollectionUploadTagRecord.tag_id)
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                )
            )
            return {
                "collection_id": normalized_id,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
                "tags": [{"tag": value} for value in rows],
            }

    def iter_tags(self, collection_id: int) -> Iterator[dict[str, object]]:
        normalized_id = _collection_id(collection_id)

        def generate() -> Iterator[dict[str, object]]:
            with read_snapshot(self._session_factory) as session:
                if session.get(CollectionUploadRecord, normalized_id) is None:
                    raise NotFound(f"collection upload session not found: {normalized_id}")
                result = session.scalars(
                    select(CollectionUploadTagRecord.tag_id)
                    .where(CollectionUploadTagRecord.collection_id == normalized_id)
                    .order_by(CollectionUploadTagRecord.tag_id)
                ).yield_per(100)
                for value in result:
                    yield {"tag": value}

        return generate()

    def add_tag(
        self,
        collection_id: int,
        tag: str,
        *,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        normalized_tag = normalize_tag(tag)
        if normalized_tag != tag:
            raise BadRequest("collection upload tag must be canonical")
        require_collection_create_access(principal, COLLECTIONS_CREATE, (normalized_tag,))
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state != "open":
                raise Conflict("collection upload tags are sealed")
            _require_tags(session, (normalized_tag,))
            existing = session.get(CollectionUploadTagRecord, (normalized_id, normalized_tag))
            if existing is None:
                session.add(
                    CollectionUploadTagRecord(
                        collection_id=normalized_id,
                        tag_id=normalized_tag,
                    )
                )
                _touch_upload(upload, config=self._config)
                session.flush()
            return {
                "collection_id": normalized_id,
                "tag_count": _upload_tag_count(session, normalized_id),
            }

    def remove_tag(self, collection_id: int, tag: str) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        normalized_tag = normalize_tag(tag)
        if normalized_tag != tag:
            raise BadRequest("collection upload tag must be canonical")
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state != "open":
                raise Conflict("collection upload tags are sealed")
            record = session.get(CollectionUploadTagRecord, (normalized_id, normalized_tag))
            if record is not None:
                session.delete(record)
                _touch_upload(upload, config=self._config)
                session.flush()
            return {
                "collection_id": normalized_id,
                "tag_count": _upload_tag_count(session, normalized_id),
            }

    def require_discard_access(
        self,
        collection_id: int,
        principal: ApplicationPrincipal,
    ) -> None:
        """Require deletion authority scoped to this upload identity or one of its tags."""

        normalized = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized)
            if upload is None or not _upload_visible_to_deleter(session, upload, principal):
                raise NotFound(f"collection upload not found: {normalized}")

    def register_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if not files or len(files) > COLLECTION_UPLOAD_FILE_BATCH_MAX:
            raise BadRequest(
                "collection upload file batch must contain "
                f"1 to {COLLECTION_UPLOAD_FILE_BATCH_MAX} files"
            )

        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state != "open":
                raise Conflict(f"collection upload session is not open: {normalized_id}")
            checkpoint = _planner_checkpoint(upload)
            request_files: list[dict[str, object]] = []
            for value in files:
                current_payload = dict(value)
                if "provenance" not in current_payload and upload.provenance_mode == "omitted":
                    current_payload["provenance"] = {
                        "status": "omitted",
                        "omission_reason": upload.provenance_omission_reason,
                    }
                request_files.append(current_payload)
            try:
                batch_document = CollectionUploadFileBatchDocument.model_validate(
                    {"files": request_files}
                )
                constraints_document = (
                    CollectionUploadRegistrationConstraintsDocument.model_validate(
                        _registration_constraints_payload(checkpoint.policy)
                    )
                )
                validate_collection_upload_batch_against_registration_constraints(
                    batch_document,
                    constraints_document,
                )
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            normalized_files = tuple(
                _normalize_file(
                    value,
                    provenance_mode=upload.provenance_mode,
                    constraints=constraints_document,
                    allow_server_derived=upload.initiated_by_app.startswith("transform:"),
                )
                for value in batch_document.files
            )
            requested_paths = tuple(current["path"] for current in normalized_files)
            existing = {
                row.path: row
                for row in session.scalars(
                    select(CollectionUploadFileRecord).where(
                        CollectionUploadFileRecord.collection_id == normalized_id,
                        CollectionUploadFileRecord.path.in_(requested_paths),
                    )
                )
            }
            last_registered = session.scalar(
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == normalized_id)
                .order_by(CollectionUploadFileRecord.file_order.desc())
                .limit(1)
            )
            new_files: list[_RegisteredFile] = []
            for current in normalized_files:
                row = existing.get(current["path"])
                if row is not None:
                    if _registered_file_identity(row) != current:
                        raise Conflict(
                            "collection upload file already has different metadata: "
                            f"{current['path']}"
                        )
                    continue
                new_files.append(current)
            if new_files:
                last_path = last_registered.path if last_registered is not None else None
                if last_path is not None and collection_upload_path_order_key(
                    new_files[0]["path"]
                ) <= collection_upload_path_order_key(last_path):
                    raise Conflict("collection upload file registration is not append-only")
                _require_transform_control_paths(upload, new_files)
            ordered: list[OrderedArchiveFile] = []
            next_order = checkpoint.next_file_order
            for current in new_files:
                session.add(
                    CollectionUploadFileRecord(
                        collection_id=normalized_id,
                        path=current["path"],
                        path_sort_key=relpath_sort_key(current["path"]),
                        file_order=next_order,
                        bytes=current["bytes"],
                        sha256=current["sha256"],
                        raw_part_plaintext_bytes=current["raw_part_plaintext_bytes"],
                        raw_part_count=current["raw_part_count"],
                        raw_part_ordered_sha256=current["raw_part_ordered_sha256"],
                        raw_parts_accepted=0,
                        raw_part_commitment_sha256=None,
                        provenance_status=current["provenance_status"],
                        provenance_journal_id=current["provenance_journal_id"],
                        provenance_current_state_id=current["provenance_current_state_id"],
                        provenance_omission_reason=current["provenance_omission_reason"],
                    )
                )
                ordered.append(
                    OrderedArchiveFile(
                        order=next_order,
                        file=ArchiveFile(
                            path=current["path"],
                            bytes=current["bytes"],
                            sha256=current["sha256"],
                        ),
                    )
                )
                next_order += 1
            upload.file_count += len(new_files)
            upload.file_bytes += sum(current["bytes"] for current in new_files)
            batch = advance_incremental_volume_plan(checkpoint, ordered)
            _persist_plan_batch(session, upload=upload, batch=batch)
            upload.planner_checkpoint_json = incremental_volume_planner_checkpoint_bytes(
                batch.checkpoint
            ).decode("utf-8")
            _touch_upload(upload, config=self._config)
            upload.archive_phase = "uploading" if upload.archive_objects else "planning"
            upload.archive_phase_updated_at = upload.last_activity_at
            session.flush()
            records = [
                session.get(CollectionUploadFileRecord, (normalized_id, current["path"]))
                for current in normalized_files
            ]
            return {
                "collection_id": normalized_id,
                "ingest_source": upload.ingest_source,
                "archive_store": upload.archive_store,
                "encryption_format": upload.encryption_format,
                "passphrase_id": upload.passphrase_id,
                "state": upload.state,
                "files": [_file_payload(row) for row in records if row is not None],
                "volumes": [_volume_summary(row) for row in batch.volumes],
            }

    def register_raw_part_digests(
        self,
        collection_id: int,
        batch: CollectionUploadRawDigestBatchDocument,
    ) -> dict[str, object]:
        """Append one bounded exact slice to a registered raw-source authority."""

        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state != "open":
                raise Conflict("collection upload no longer accepts raw source digests")
            file = session.scalar(
                select(CollectionUploadFileRecord)
                .where(
                    CollectionUploadFileRecord.collection_id == normalized_id,
                    CollectionUploadFileRecord.path == batch.path,
                )
                .with_for_update()
            )
            if file is None or file.raw_part_count is None or file.raw_part_ordered_sha256 is None:
                raise NotFound(f"registered raw upload file not found: {batch.path}")
            accepted = int(file.raw_parts_accepted)
            end = batch.first_part + len(batch.sha256s)
            if end > file.raw_part_count:
                raise BadRequest("raw source digest batch exceeds its registered part count")
            if batch.first_part < accepted:
                if end > accepted:
                    raise Conflict("raw source digest batch overlaps committed progress")
                existing = tuple(
                    session.scalars(
                        select(CollectionUploadRawPartDigestRecord.sha256)
                        .where(
                            CollectionUploadRawPartDigestRecord.collection_id == normalized_id,
                            CollectionUploadRawPartDigestRecord.path == batch.path,
                            CollectionUploadRawPartDigestRecord.part_number >= batch.first_part,
                            CollectionUploadRawPartDigestRecord.part_number < end,
                        )
                        .order_by(CollectionUploadRawPartDigestRecord.part_number)
                    )
                )
                if existing != tuple(batch.sha256s):
                    raise Conflict("raw source digest retry differs from committed bytes")
                return _raw_digest_progress(file)
            if batch.first_part != accepted:
                raise Conflict(
                    f"raw source digest offset differs: expected {accepted}, "
                    f"received {batch.first_part}"
                )
            next_part, commitment = advance_raw_part_commitment(
                file.raw_part_commitment_sha256,
                first_part=batch.first_part,
                part_sha256s=batch.sha256s,
            )
            for offset, sha256 in enumerate(batch.sha256s):
                session.add(
                    CollectionUploadRawPartDigestRecord(
                        collection_id=normalized_id,
                        path=batch.path,
                        part_number=batch.first_part + offset,
                        sha256=sha256,
                    )
                )
            file.raw_parts_accepted = next_part
            file.raw_part_commitment_sha256 = commitment
            if next_part == file.raw_part_count and commitment != file.raw_part_ordered_sha256:
                raise BadRequest("raw source digest sequence differs from its registered authority")
            _touch_upload(upload, config=self._config)
            session.flush()
            return _raw_digest_progress(file)

    def create_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        authority: CollectionUploadProvenanceJournalCreateDocument,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state != "open" or upload.provenance_mode == "omitted":
                raise Conflict("collection upload does not accept provenance journals")
            existing = session.get(
                CollectionUploadProvenanceJournalRecord,
                (normalized_id, journal_id),
            )
            if existing is not None:
                if existing.sha256 != authority.sha256 or existing.bytes != authority.bytes:
                    raise Conflict("provenance journal authority already differs")
                return _journal_payload(existing)
            record = CollectionUploadProvenanceJournalRecord(
                collection_id=normalized_id,
                journal_id=journal_id,
                bytes=authority.bytes,
                sha256=authority.sha256,
                state="accepting",
                accepted_bytes=0,
                content_hash_state=CheckpointSHA256().export_state(),
                validation_byte_offset=0,
                validation_sequence=0,
            )
            session.add(record)
            _touch_upload(upload, config=self._config)
            session.flush()
            return _journal_payload(record)

    def append_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        offset: int,
        content: bytes,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        chunk = bytes(content)
        if offset < 0 or not chunk or len(chunk) > COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX:
            raise BadRequest("provenance append is outside its bounded transport contract")
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            record = session.scalar(
                select(CollectionUploadProvenanceJournalRecord)
                .where(
                    CollectionUploadProvenanceJournalRecord.collection_id == normalized_id,
                    CollectionUploadProvenanceJournalRecord.journal_id == journal_id,
                )
                .with_for_update()
            )
            if upload is None or record is None:
                raise NotFound(f"collection upload provenance journal not found: {journal_id}")
            if record.state != "accepting":
                raise Conflict(f"provenance journal is {record.state}")
            if offset < record.accepted_bytes:
                existing = session.scalar(
                    select(CollectionUploadProvenanceJournalChunkRecord).where(
                        CollectionUploadProvenanceJournalChunkRecord.collection_id == normalized_id,
                        CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
                        CollectionUploadProvenanceJournalChunkRecord.byte_offset == offset,
                    )
                )
                if existing is None or existing.content != chunk:
                    raise Conflict("provenance append retry differs from committed bytes")
                return _journal_payload(record)
            if offset != record.accepted_bytes:
                raise Conflict(
                    f"provenance append offset differs: expected {record.accepted_bytes}, "
                    f"received {offset}"
                )
            if offset + len(chunk) > record.bytes:
                raise BadRequest("provenance append exceeds its declared authority")
            if (
                offset + len(chunk) < record.bytes
                and len(chunk) != COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX
            ):
                raise BadRequest(
                    "every non-final provenance append must fill one transport segment"
                )
            ordinal = int(
                session.scalar(
                    select(func.count(CollectionUploadProvenanceJournalChunkRecord.ordinal)).where(
                        CollectionUploadProvenanceJournalChunkRecord.collection_id == normalized_id,
                        CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
                    )
                )
                or 0
            )
            digest = CheckpointSHA256.from_state(record.content_hash_state)
            digest.update(chunk)
            session.add(
                CollectionUploadProvenanceJournalChunkRecord(
                    collection_id=normalized_id,
                    journal_id=journal_id,
                    ordinal=ordinal,
                    byte_offset=offset,
                    content=chunk,
                )
            )
            record.accepted_bytes += len(chunk)
            record.content_hash_state = digest.export_state()
            _touch_upload(upload, config=self._config)
            return _journal_payload(record)

    def seal_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionUploadProvenanceJournalRecord)
                .where(
                    CollectionUploadProvenanceJournalRecord.collection_id == normalized_id,
                    CollectionUploadProvenanceJournalRecord.journal_id == journal_id,
                )
                .with_for_update()
            )
            if record is None:
                raise NotFound(f"collection upload provenance journal not found: {journal_id}")
            if record.state in {"sealed", "failed"}:
                return _journal_payload(record)
            if record.state == "accepting":
                digest = CheckpointSHA256.from_state(record.content_hash_state)
                if record.accepted_bytes != record.bytes or digest.hexdigest() != record.sha256:
                    raise Conflict("provenance journal content does not match its exact authority")
                record.state = "validating"
            try:
                with session.begin_nested():
                    _validate_next_upload_journal_entry(session, record)
            except (ProvenanceValidationError, ValueError) as exc:
                record.state = "failed"
                record.failure = str(exc)[:1000]
            return _journal_payload(record)

    def get_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> dict[str, object]:
        with read_snapshot(self._session_factory) as session:
            record = session.get(
                CollectionUploadProvenanceJournalRecord,
                (_collection_id(collection_id), journal_id),
            )
            if record is None:
                raise NotFound(f"collection upload provenance journal not found: {journal_id}")
            return _journal_payload(record)

    def process_due_provenance_journal_validations(self, *, limit: int = 1) -> int:
        processed = 0
        for _ in range(max(0, limit)):
            with session_scope(self._session_factory) as session:
                record = session.scalar(
                    select(CollectionUploadProvenanceJournalRecord)
                    .where(CollectionUploadProvenanceJournalRecord.state == "validating")
                    .order_by(
                        CollectionUploadProvenanceJournalRecord.collection_id,
                        CollectionUploadProvenanceJournalRecord.journal_id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if record is None:
                    break
                collection_id = record.collection_id
                try:
                    with session.begin_nested():
                        _validate_next_upload_journal_entry(session, record)
                except (ProvenanceValidationError, ValueError) as exc:
                    record.state = "failed"
                    record.failure = str(exc)[:1000]
            self._schedule_finalization_if_ready(collection_id)
            processed += 1
        return processed

    def complete(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_identity: str,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if files_total < 1 or _SHA256_RE.fullmatch(content_identity) is None:
            raise BadRequest("collection upload completion identity is invalid")
        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord).where(
                    CollectionRecord.id == normalized_id,
                    CollectionRecord.is_published.is_(True),
                )
            )
            if collection is not None:
                if collection.content_identity != content_identity:
                    raise Conflict("collection upload completion identity changed")
                return _finalized_payload(
                    session,
                    collection,
                    store_name=self._config.archive_write_store,
                )
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.state not in {"open", "closing", "uploading", "finalizing"}:
                raise Conflict(f"collection upload session is {upload.state}: {normalized_id}")
            if upload.state == "finalizing":
                return _upload_payload(session, upload)
            if _upload_tag_set_identity(session, normalized_id) != upload.tag_set_identity:
                raise Conflict("collection upload tag set differs from creation identity")
            incomplete_raw = session.scalar(
                select(CollectionUploadFileRecord.path)
                .where(
                    CollectionUploadFileRecord.collection_id == normalized_id,
                    CollectionUploadFileRecord.raw_part_count.is_not(None),
                    or_(
                        CollectionUploadFileRecord.raw_parts_accepted
                        != CollectionUploadFileRecord.raw_part_count,
                        CollectionUploadFileRecord.raw_part_commitment_sha256
                        != CollectionUploadFileRecord.raw_part_ordered_sha256,
                    ),
                )
                .limit(1)
            )
            if incomplete_raw is not None:
                raise Conflict(f"raw source digest sequence is incomplete: {incomplete_raw}")
            incomplete_provenance = session.scalar(
                select(CollectionUploadProvenanceJournalRecord.journal_id)
                .where(
                    CollectionUploadProvenanceJournalRecord.collection_id == normalized_id,
                    CollectionUploadProvenanceJournalRecord.state != "sealed",
                )
                .limit(1)
            )
            if incomplete_provenance is not None:
                raise Conflict(f"provenance journal is not sealed: {incomplete_provenance}")
            _require_transform_output_authority(session, upload)
            actual_etag = collection_content_identity_ordered(
                (row.path, row.bytes, row.sha256)
                for batch in _upload_file_batches(session, normalized_id)
                for row in batch
            )
            if upload.file_count != files_total or actual_etag != content_identity:
                raise Conflict("collection upload registered manifest differs from completion")
            checkpoint = _planner_checkpoint(upload)
            if not checkpoint.closed:
                batch = advance_incremental_volume_plan(checkpoint, (), final=True)
                _persist_plan_batch(session, upload=upload, batch=batch)
                upload.planner_checkpoint_json = incremental_volume_planner_checkpoint_bytes(
                    batch.checkpoint
                ).decode("utf-8")
                session.flush()
            checkpoint = _planner_checkpoint(upload)
            if (
                not checkpoint.closed
                or checkpoint.files_seen != upload.file_count
                or checkpoint.bytes_seen != upload.file_bytes
            ):
                raise Conflict("collection upload planner differs from registered files")
            registered = (
                (row.path, row.bytes, row.sha256)
                for batch in _upload_file_batches(session, normalized_id)
                for row in batch
            )
            sentinel = object()
            try:
                plans_differ = any(
                    expected is sentinel or planned is sentinel or expected != planned
                    for expected, planned in zip_longest(
                        registered,
                        _iter_planned_file_identities(session, normalized_id),
                        fillvalue=sentinel,
                    )
                )
            except ValueError as exc:
                raise Conflict(
                    "collection upload volume plans differ from registered files"
                ) from exc
            if plans_differ:
                raise Conflict("collection upload volume plans differ from registered files")
            custody_pending = (
                upload.custody_mode == "custody-transfer"
                and not _has_complete_artifact_custody(upload)
            )
            upload.state = "closing" if custody_pending else "uploading"
            if custody_pending:
                _touch_upload(upload, config=self._config)
            else:
                upload.lease_expires_at = None
            upload.provenance_identity = None
            upload.closed_at = utc_timestamp_now()
            upload.last_activity_at = upload.closed_at
            upload.archive_phase = "uploading"
            upload.archive_phase_updated_at = upload.closed_at
            upload.archive_next_attempt_at = None
            upload.archive_failure = None
            session.flush()
        self._schedule_finalization_if_ready(normalized_id)
        return self.get(normalized_id)

    def list_files(
        self,
        collection_id: int,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("invalid collection upload file pagination")
        with read_snapshot(self._session_factory) as session:
            if session.get(CollectionUploadRecord, normalized_id) is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            total = int(
                session.scalar(
                    select(func.count(CollectionUploadFileRecord.path)).where(
                        CollectionUploadFileRecord.collection_id == normalized_id
                    )
                )
                or 0
            )
            statement = (
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == normalized_id)
                .order_by(CollectionUploadFileRecord.file_order)
            )
            statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.scalars(statement))
            return {
                "collection_id": normalized_id,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
                "files": [_file_payload(row) for row in rows],
            }

    def iter_files(self, collection_id: int) -> Iterator[dict[str, object]]:
        normalized_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            if session.get(CollectionUploadRecord, normalized_id) is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            statement = (
                select(CollectionUploadFileRecord)
                .where(CollectionUploadFileRecord.collection_id == normalized_id)
                .order_by(CollectionUploadFileRecord.file_order)
                .execution_options(yield_per=100)
            )
            for row in session.scalars(statement):
                yield _file_payload(row)

    def list_volumes(self, collection_id: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is None:
                if session.get(CollectionRecord, normalized_id) is not None:
                    return {"collection_id": normalized_id, "volumes": []}
                raise NotFound(f"collection upload session not found: {normalized_id}")
            volumes = list(
                session.scalars(
                    select(CollectionArchiveObjectUploadRecord)
                    .where(CollectionArchiveObjectUploadRecord.collection_id == normalized_id)
                    .order_by(CollectionArchiveObjectUploadRecord.sequence)
                )
            )
            return {
                "collection_id": normalized_id,
                "volumes": [_volume_work_payload(row) for row in volumes],
            }

    def acquire_work(self, collection_id: int, *, limit: int) -> dict[str, object]:
        """Acquire one bounded unit per actionable volume without scanning sealed work."""

        normalized_id = _collection_id(collection_id)
        if limit < 1 or limit > 64:
            raise BadRequest("collection upload work limit must be between 1 and 64")
        with read_snapshot(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is None:
                if session.get(CollectionRecord, normalized_id) is not None:
                    return {
                        "collection_id": normalized_id,
                        "planning_complete": True,
                        "complete": True,
                        "committed_payload_bytes": 0,
                        "work": [],
                    }
                raise NotFound(f"collection upload session not found: {normalized_id}")
            planning_complete = bool(_planner_checkpoint(upload).closed)
            volumes = list(
                session.scalars(
                    select(CollectionArchiveObjectUploadRecord)
                    .where(
                        CollectionArchiveObjectUploadRecord.collection_id == normalized_id,
                        CollectionArchiveObjectUploadRecord.state != "sealed",
                        or_(
                            CollectionArchiveObjectUploadRecord.kind == "pack",
                            exists(
                                select(CollectionUploadFileRecord.path).where(
                                    CollectionUploadFileRecord.collection_id == normalized_id,
                                    CollectionUploadFileRecord.path
                                    == CollectionArchiveObjectUploadRecord.source_path,
                                    CollectionUploadFileRecord.raw_parts_accepted
                                    >= (
                                        CollectionArchiveObjectUploadRecord.source_first_part
                                        + CollectionArchiveObjectUploadRecord.source_part_count
                                    ),
                                )
                            ),
                        ),
                    )
                    .order_by(CollectionArchiveObjectUploadRecord.sequence)
                    .limit(limit)
                )
            )
            work = [_unit_assignment_payload(row) for row in volumes]
            return {
                "collection_id": normalized_id,
                "planning_complete": planning_complete,
                "complete": planning_complete and not work,
                "committed_payload_bytes": upload.uploaded_payload_bytes,
                "work": work,
            }

    def get_volume(self, collection_id: int, volume_id: str) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (normalized_id, volume_id),
            )
            if record is None:
                raise NotFound(f"collection upload volume not found: {volume_id}")
            return _volume_work_payload(record)

    def upload_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (normalized_id, volume_id),
            )
            if upload is None or record is None:
                raise NotFound(f"collection upload volume not found: {volume_id}")
            if upload.state not in {"open", "closing", "uploading"}:
                raise Conflict(f"collection upload session is {upload.state}: {normalized_id}")
            if plan_sha256 != record.plan_sha256:
                raise Conflict("archive upload unit plan identity changed")
            store_name = upload.archive_store
            passphrase_id = upload.passphrase_id
            kind = record.kind
            object_path = record.object_path
            relative_path = record.relative_path
            plan_json = record.plan_json
            unit_plaintext_bytes = record.unit_plaintext_bytes

        receipt: SealedPackVolume | SealedRawVolume | None
        try:
            if kind == "pack":
                pack_plan = parse_pack_volume_plan(plan_json)
                descriptor = pack_unit_descriptors(pack_plan)[unit]
                if len(content) != descriptor.payload_bytes:
                    raise ValueError("pack upload unit payload length mismatch")
                pack_uploader = self._pack_uploader(
                    self._volume_object_store(
                        store_name=store_name,
                        collection_id=normalized_id,
                        object_id=volume_id,
                    ),
                    passphrase_id=passphrase_id,
                )
                pack_checkpoint = pack_uploader.open(
                    collection_id=normalized_id,
                    plan=pack_plan,
                    object_path=object_path,
                    relative_path=relative_path,
                )
                pack_checkpoint = pack_uploader.upload_unit(
                    plan=pack_plan,
                    checkpoint=pack_checkpoint,
                    unit_number=unit,
                    payload_chunks=(content,),
                )
                receipt = (
                    pack_uploader.sealed_receipt(
                        plan=pack_plan,
                        checkpoint=pack_checkpoint,
                    )
                    if pack_checkpoint.completed is not None
                    else None
                )
            elif kind == "segment":
                raw_plan = parse_raw_volume_plan(plan_json)
                expected = self._raw_volume_digests(normalized_id, raw_plan)
                if unit < 0 or unit >= len(expected):
                    raise ValueError("raw upload unit number is outside the plan")
                expected_bytes = min(
                    unit_plaintext_bytes,
                    raw_plan.plaintext_bytes - unit * unit_plaintext_bytes,
                )
                if len(content) != expected_bytes:
                    raise ValueError("raw upload unit payload length mismatch")
                raw_uploader = self._raw_uploader(
                    self._volume_object_store(
                        store_name=store_name,
                        collection_id=normalized_id,
                        object_id=volume_id,
                    ),
                    passphrase_id=passphrase_id,
                )
                raw_checkpoint = raw_uploader.open(
                    collection_id=normalized_id,
                    plan=raw_plan,
                    object_path=object_path,
                    relative_path=relative_path,
                    target_part_plaintext_bytes=unit_plaintext_bytes,
                    expected_part_sha256s=expected,
                )
                raw_checkpoint = raw_uploader.upload_unit(
                    plan=raw_plan,
                    checkpoint=raw_checkpoint,
                    unit_number=unit + 1,
                    plaintext=content,
                )
                receipt = (
                    raw_uploader.sealed_receipt(raw_checkpoint)
                    if raw_checkpoint.completed
                    else None
                )
            else:
                raise RuntimeError(f"unsupported archive volume kind: {kind}")
        except (IndexError, ValueError) as exc:
            raise BadRequest(str(exc)) from exc

        if receipt is not None:
            self._record_sealed_volume(normalized_id, receipt)
        payload = self.get_unit(normalized_id, volume_id, unit)
        self._schedule_finalization_if_ready(normalized_id)
        return payload

    def get_unit(self, collection_id: int, volume_id: str, unit: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (normalized_id, volume_id),
            )
            if record is None or unit < 0 or unit >= record.total_units:
                raise NotFound(f"collection upload unit not found: {unit}")
            return _unit_work_payload(record, unit)

    def get(self, collection_id: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is not None:
                return _upload_payload(session, upload)
            collection = session.scalar(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.archive_copies))
                .where(
                    CollectionRecord.id == normalized_id,
                    CollectionRecord.is_published.is_(True),
                )
            )
            if collection is None:
                raise NotFound(f"collection upload not found: {normalized_id}")
            return _finalized_payload(
                session,
                collection,
                store_name=self._config.archive_write_store,
            )

    def heartbeat(self, collection_id: int) -> dict[str, object]:
        """Renew one active custody-transfer construction lease."""

        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if upload.custody_mode != "custody-transfer":
                raise Conflict("collection upload does not have a custody-transfer lease")
            if upload.state not in {"open", "closing"}:
                raise Conflict(f"collection upload session is {upload.state}: {normalized_id}")
            _touch_upload(upload, config=self._config)
            return _upload_payload(session, upload)

    def reap_expired_custody_transfers(self, *, limit: int = 100) -> int:
        """Retain expired transferred custody as visible, resumable orphan state."""

        if limit < 1:
            return 0
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(
                        CollectionUploadRecord.custody_mode == "custody-transfer",
                        CollectionUploadRecord.state.in_(("open", "closing")),
                        CollectionUploadRecord.lease_expires_at.is_not(None),
                        CollectionUploadRecord.lease_expires_at <= now,
                    )
                    .order_by(
                        CollectionUploadRecord.lease_expires_at,
                        CollectionUploadRecord.collection_id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for upload in uploads:
                upload.state = "orphaned"
                upload.orphaned_at = now
                upload.lease_expires_at = None
                upload.last_activity_at = now
                upload.archive_phase = "orphaned"
                upload.archive_phase_updated_at = now
            return len(uploads)

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        tag: str | None,
        state: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        _validate_upload_list(page=page, per_page=per_page, sort=sort, order=order)
        with read_snapshot(self._session_factory) as session:
            statement = _upload_list_statement(
                q=q, tag=tag, state=state, sort=sort, order=order, principal=principal
            )
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.execute(statement))
            return {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
                "sort": sort,
                "order": order,
                "query": q,
                "filters": {"tag": tag, "state": state},
                "uploads": [
                    _upload_list_payload(
                        session,
                        upload,
                        files=int(files or 0),
                        byte_count=int(byte_count or 0),
                    )
                    for upload, files, byte_count in rows
                ],
            }

    def iter_uploads(
        self,
        *,
        q: str | None,
        tag: str | None,
        state: str | None,
        sort: str,
        order: str,
        principal: ApplicationPrincipal,
    ) -> Iterator[dict[str, object]]:
        _validate_upload_list(page=1, per_page=100, sort=sort, order=order)
        statement = _upload_list_statement(
            q=q, tag=tag, state=state, sort=sort, order=order, principal=principal
        ).execution_options(yield_per=100)
        with read_snapshot(self._session_factory) as session:
            for upload, files, byte_count in session.execute(statement):
                yield _upload_list_payload(
                    session,
                    upload,
                    files=int(files or 0),
                    byte_count=int(byte_count or 0),
                )

    def cancel(self, collection_id: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            if session.get(CollectionRecord, normalized_id) is not None:
                raise Conflict(f"collection is already finalized: {normalized_id}")
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is None:
                raise NotFound(f"collection upload session not found: {normalized_id}")
            if any(current.state == "sealed" for current in upload.archive_objects):
                raise Conflict("collection upload with sealed archive volumes cannot be canceled")
            payload = _upload_payload(session, upload, state="canceled")
            payload.update(
                {
                    "latest_failure": None,
                    "archive_phase": "canceled",
                    "archive_phase_updated_at": utc_timestamp_now(),
                    "archive_next_attempt_at": None,
                }
            )
            store_name = upload.archive_store
            prefix = upload.archive_storage_prefix
            passphrase_id = upload.passphrase_id
            checkpoints = [
                (current.kind, current.checkpoint_json)
                for current in upload.archive_objects
                if current.checkpoint_json
            ]
        for kind, checkpoint_json in checkpoints:
            if checkpoint_json is None:
                continue
            if kind == "pack":
                pack_checkpoint = PackUploadCheckpoint.from_json(checkpoint_json)
                if pack_checkpoint.completed is None:
                    self._pack_uploader(
                        self._volume_object_store(
                            store_name=store_name,
                            collection_id=normalized_id,
                            object_id=pack_checkpoint.volume_id,
                        ),
                        passphrase_id=passphrase_id,
                    ).abort(pack_checkpoint)
            else:
                raw_checkpoint = RawUploadCheckpoint.from_json(checkpoint_json)
                if raw_checkpoint.completed is None:
                    self._raw_uploader(
                        self._volume_object_store(
                            store_name=store_name,
                            collection_id=normalized_id,
                            object_id=raw_checkpoint.volume_id,
                        ),
                        passphrase_id=passphrase_id,
                    ).abort(raw_checkpoint)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is not None:
                session.delete(upload)
        if prefix:
            self._archive_stores.require(store_name).store.discard_collection_archive_upload(
                archive_storage_prefix=prefix
            )
        return payload

    def plan_orphan_discard(self, collection_id: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        expires = (utc_now() + PLAN_TTL).replace(microsecond=0)
        with session_scope(self._session_factory) as session:
            plan = _orphan_discard_plan(
                session,
                collection_id=normalized_id,
                expires_at=format_utc_timestamp(expires),
            )
        plan["challenge"] = (
            None if plan["blockers"] else plan_challenge(_DISCARD_CHALLENGE_PREFIX, plan, expires)
        )
        return plan

    def discard_orphan(self, collection_id: int, *, challenge: str) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        supplied = challenge.strip()
        if not supplied:
            raise BadRequest("collection upload discard challenge is required")
        expires = challenge_expiry(
            supplied,
            prefix=_DISCARD_CHALLENGE_PREFIX,
            operation="collection upload discard",
        )
        if utc_now() > expires:
            raise Conflict("collection upload discard plan has expired; request a new plan")
        checkpoints: list[tuple[str, str]] = []
        store_name = ""
        prefix = ""
        passphrase_id = ""
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is None:
                if not challenge_has_shape(supplied, prefix=_DISCARD_CHALLENGE_PREFIX):
                    raise NotFound(f"collection upload session not found: {normalized_id}")
                return {
                    "status": "already_absent",
                    "collection_id": normalized_id,
                    "files": 0,
                    "bytes": 0,
                    "custody": {"state": "complete"},
                    "archive_objects": 0,
                }
            plan = _orphan_discard_plan(
                session,
                collection_id=normalized_id,
                expires_at=format_utc_timestamp(expires),
            )
            if not secrets.compare_digest(
                plan_challenge(_DISCARD_CHALLENGE_PREFIX, plan, expires),
                supplied,
            ):
                raise Conflict("collection upload discard plan changed; request a new plan")
            blockers_value = plan["blockers"]
            if not isinstance(blockers_value, list):
                raise RuntimeError("collection upload discard plan blockers are invalid")
            blockers = [str(value) for value in blockers_value]
            if blockers:
                raise Conflict("collection upload discard is blocked: " + "; ".join(blockers))
            checkpoints = [
                (current.kind, current.checkpoint_json)
                for current in upload.archive_objects
                if current.checkpoint_json is not None
            ]
            store_name = upload.archive_store
            prefix = upload.archive_storage_prefix
            passphrase_id = upload.passphrase_id
            result = {
                "status": "discarded",
                "collection_id": normalized_id,
                "files": plan["files"],
                "bytes": plan["bytes"],
                "custody": plan["custody"],
                "archive_objects": plan["archive_objects"],
            }
            now = utc_timestamp_now()
            upload.state = "discarding"
            upload.archive_phase = "discarding"
            upload.archive_phase_updated_at = now
            upload.last_activity_at = now
        try:
            for kind, checkpoint_json in checkpoints:
                if kind == "pack":
                    pack_checkpoint = PackUploadCheckpoint.from_json(checkpoint_json)
                    if pack_checkpoint.completed is None:
                        self._pack_uploader(
                            self._volume_object_store(
                                store_name=store_name,
                                collection_id=normalized_id,
                                object_id=pack_checkpoint.volume_id,
                            ),
                            passphrase_id=passphrase_id,
                        ).abort(pack_checkpoint)
                else:
                    raw_checkpoint = RawUploadCheckpoint.from_json(checkpoint_json)
                    if raw_checkpoint.completed is None:
                        self._raw_uploader(
                            self._volume_object_store(
                                store_name=store_name,
                                collection_id=normalized_id,
                                object_id=raw_checkpoint.volume_id,
                            ),
                            passphrase_id=passphrase_id,
                        ).abort(raw_checkpoint)
            self._archive_stores.require(store_name).store.discard_collection_archive_upload(
                archive_storage_prefix=prefix
            )
        except Exception as exc:
            with session_scope(self._session_factory) as session:
                upload = session.get(CollectionUploadRecord, normalized_id)
                if upload is not None and upload.state == "discarding":
                    now = utc_timestamp_now()
                    upload.state = "orphaned"
                    upload.orphaned_at = now
                    upload.archive_phase = "orphaned"
                    upload.archive_phase_updated_at = now
                    upload.archive_failure = str(exc)
            raise
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == normalized_id)
                .with_for_update()
            )
            if upload is not None:
                if upload.state != "discarding":
                    raise RuntimeError("collection upload discard state changed unexpectedly")
                session.delete(upload)
        return result

    def _pack_uploader(
        self,
        object_store: ArchiveResumableObjectStore,
        *,
        passphrase_id: str,
    ) -> PackVolumeUploader:
        passphrase = self._config.archive_passphrase_for(passphrase_id)
        return PackVolumeUploader(
            object_store=object_store,
            checkpoint_store=self._checkpoints,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
            source_read_chunk_bytes=self._throughput.source_read_chunk_bytes,
            resources=self._resources,
            timing_observer=log_transfer_timing,
            session_cache=self._age_sessions[passphrase_id],
        )

    def _volume_object_store(
        self,
        *,
        store_name: str,
        collection_id: int,
        object_id: str,
    ) -> ArchiveResumableObjectStore:
        binding = self._archive_stores.require(store_name)
        archive = binding.resumable_objects
        if (
            not self._config.retrieval_cache_new_archive_enabled
            or self._retrieval_cache is None
            or binding.store.read_mode() != "restore_required"
        ):
            return archive
        return MirroredArchiveResumableObjectStore(
            archive=archive,
            cache=self._retrieval_cache,
            source_store=store_name,
            collection_id=collection_id,
            object_id=object_id,
            owner=f"collection-upload:{collection_id}:{object_id}",
        )

    def _raw_uploader(
        self,
        object_store: ArchiveResumableObjectStore,
        *,
        passphrase_id: str,
    ) -> RawVolumeUploader:
        passphrase = self._config.archive_passphrase_for(passphrase_id)
        return RawVolumeUploader(
            object_store=object_store,
            checkpoint_store=self._checkpoints,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
            source_read_chunk_bytes=self._throughput.source_read_chunk_bytes,
            resources=self._resources,
            timing_observer=log_transfer_timing,
            session_cache=self._age_sessions[passphrase_id],
        )

    def _raw_volume_digests(
        self,
        collection_id: int,
        plan: RawVolumePlan,
    ) -> tuple[str, ...]:
        with session_scope(self._session_factory) as session:
            file = session.get(CollectionUploadFileRecord, (collection_id, plan.source_path))
            if (
                file is None
                or file.raw_part_plaintext_bytes is None
                or file.raw_part_count is None
                or file.raw_part_ordered_sha256 is None
            ):
                raise RuntimeError("raw volume source digest authority is missing")
            summary = RawSourceDigestSummary(
                path=file.path,
                bytes=file.bytes,
                sha256=file.sha256,
                part_plaintext_bytes=file.raw_part_plaintext_bytes,
                part_count=file.raw_part_count,
                ordered_part_sha256=file.raw_part_ordered_sha256,
            )
            first, count = raw_volume_part_span(
                summary,
                file_offset=plan.file_offset,
                plaintext_bytes=plan.plaintext_bytes,
            )
            values = tuple(
                session.scalars(
                    select(CollectionUploadRawPartDigestRecord.sha256)
                    .where(
                        CollectionUploadRawPartDigestRecord.collection_id == collection_id,
                        CollectionUploadRawPartDigestRecord.path == plan.source_path,
                        CollectionUploadRawPartDigestRecord.part_number >= first,
                        CollectionUploadRawPartDigestRecord.part_number < first + count,
                    )
                    .order_by(CollectionUploadRawPartDigestRecord.part_number)
                )
            )
        if len(values) != count:
            raise RuntimeError("raw volume source digest rows are incomplete")
        return values

    def _record_sealed_volume(
        self,
        collection_id: int,
        receipt: SealedPackVolume | SealedRawVolume,
    ) -> None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (collection_id, receipt.volume_id),
            )
            if record is None:
                raise RuntimeError("sealed archive volume plan disappeared")
            encoded = _sealed_volume_json(receipt)
            if record.sealed_receipt_json not in {None, encoded}:
                raise RuntimeError("sealed archive volume receipt changed")
            record.sealed_receipt_json = encoded
            record.state = "sealed"
            record.sealed_at = receipt.completed_at
            record.updated_at = now
            record.uploaded_units = len(receipt.parts)
            record.uploaded_bytes = receipt.stored_bytes
            if upload is not None:
                _touch_upload(upload, config=self._config, now=now)
                upload.archive_phase_updated_at = now
                _record_artifact_custody_receipts(session, upload, now=now)

    def requeue_interrupted_finalizations_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        now = utc_timestamp_now()
        requeued = 0
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.state.in_(("closing", "uploading", "finalizing")))
                    .order_by(
                        CollectionUploadRecord.archive_phase_updated_at,
                        CollectionUploadRecord.collection_id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for upload in uploads:
                interrupted = upload.state == "finalizing" and upload.archive_phase == "finalizing"
                if not interrupted and not _ready_for_finalization(session, upload):
                    continue
                upload.state = "finalizing"
                upload.archive_phase = "retry_wait" if interrupted else "finalization_queued"
                upload.archive_next_attempt_at = now
                upload.archive_phase_updated_at = now
                if interrupted:
                    upload.archive_failure = "archive finalization interrupted before completion"
                else:
                    upload.archive_failure = None
                requeued += 1
        return requeued

    def requeue_interrupted_orphan_discards_for_startup(self, *, limit: int = 100) -> int:
        """Return interrupted guarded cleanup to visible, retryable orphan state."""

        if limit < 1:
            return 0
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.state == "discarding")
                    .order_by(CollectionUploadRecord.collection_id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for upload in uploads:
                upload.state = "orphaned"
                upload.orphaned_at = now
                upload.archive_phase = "orphaned"
                upload.archive_phase_updated_at = now
                upload.archive_failure = "orphan discard interrupted before cleanup completed"
            return len(uploads)

    def process_due_finalizations(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0
        self._schedule_ready_finalizations(limit=max(100, limit))
        processed = 0
        for _ in range(limit):
            collection_id = self._claim_due_finalization()
            if collection_id is None:
                break
            try:
                self._reconcile_sealed_volume_receipts(collection_id)
                self._finalize(collection_id)
            except Exception as exc:
                self._record_finalization_retry(collection_id, exc)
                _LOG.exception(
                    "collection archive finalization failed; retry scheduled: collection_id=%s",
                    collection_id,
                )
            processed += 1
        return processed

    def _schedule_ready_finalizations(self, *, limit: int) -> int:
        now = utc_timestamp_now()
        scheduled = 0
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.state.in_(("closing", "uploading")))
                    .order_by(
                        CollectionUploadRecord.archive_phase_updated_at,
                        CollectionUploadRecord.collection_id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for upload in uploads:
                if not _ready_for_finalization(session, upload):
                    continue
                _mark_finalization_ready(upload, now=now)
                scheduled += 1
        return scheduled

    def _claim_due_finalization(self) -> int | None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(
                    CollectionUploadRecord.state == "finalizing",
                    CollectionUploadRecord.archive_phase.in_(("finalization_queued", "retry_wait")),
                    CollectionUploadRecord.archive_next_attempt_at.is_not(None),
                    CollectionUploadRecord.archive_next_attempt_at <= now,
                )
                .order_by(
                    CollectionUploadRecord.archive_next_attempt_at,
                    CollectionUploadRecord.collection_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if upload is None:
                return None
            if not _ready_for_finalization(session, upload):
                upload.state = "uploading"
                upload.archive_phase = "uploading"
                upload.archive_next_attempt_at = None
                upload.archive_phase_updated_at = now
                return None
            upload.state = "finalizing"
            upload.archive_phase = "finalizing"
            upload.archive_phase_updated_at = now
            upload.archive_last_attempt_at = now
            upload.archive_next_attempt_at = None
            upload.archive_failure = None
            return upload.collection_id

    def _schedule_finalization_if_ready(self, collection_id: int) -> None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None or not _ready_for_finalization(session, upload):
                return
            _mark_finalization_ready(upload, now=now)

    def _reconcile_sealed_volume_receipts(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            store_name = upload.archive_store
            passphrase_id = upload.passphrase_id
            pending = list(
                session.execute(
                    select(
                        CollectionArchiveObjectUploadRecord.object_id,
                        CollectionArchiveObjectUploadRecord.kind,
                        CollectionArchiveObjectUploadRecord.plan_json,
                        CollectionArchiveObjectUploadRecord.checkpoint_json,
                    )
                    .where(
                        CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                        CollectionArchiveObjectUploadRecord.state == "sealed",
                        CollectionArchiveObjectUploadRecord.sealed_receipt_json.is_(None),
                    )
                    .order_by(CollectionArchiveObjectUploadRecord.sequence)
                    .limit(64)
                )
            )
        for volume_id, kind, plan_json, checkpoint_json in pending:
            if checkpoint_json is None:
                raise RuntimeError(f"sealed archive volume has no checkpoint: {volume_id}")
            if kind == "pack":
                checkpoint = PackUploadCheckpoint.from_json(checkpoint_json)
                if checkpoint.completed is None:
                    raise RuntimeError(f"sealed pack volume is incomplete: {volume_id}")
                receipt: SealedPackVolume | SealedRawVolume = self._pack_uploader(
                    self._volume_object_store(
                        store_name=store_name,
                        collection_id=collection_id,
                        object_id=volume_id,
                    ),
                    passphrase_id=passphrase_id,
                ).sealed_receipt(
                    plan=parse_pack_volume_plan(plan_json),
                    checkpoint=checkpoint,
                )
            elif kind == "segment":
                raw_checkpoint = RawUploadCheckpoint.from_json(checkpoint_json)
                if not raw_checkpoint.completed:
                    raise RuntimeError(f"sealed raw volume is incomplete: {volume_id}")
                receipt = self._raw_uploader(
                    self._volume_object_store(
                        store_name=store_name,
                        collection_id=collection_id,
                        object_id=volume_id,
                    ),
                    passphrase_id=passphrase_id,
                ).sealed_receipt(raw_checkpoint)
            else:
                raise RuntimeError(f"unsupported archive volume kind: {kind}")
            self._record_sealed_volume(collection_id, receipt)

    def _record_finalization_retry(self, collection_id: int, exc: Exception) -> None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            delay = min(3600, 2 ** min(upload.archive_attempt_count, 10))
            upload.state = "finalizing"
            upload.archive_phase = "retry_wait"
            upload.archive_phase_updated_at = now
            upload.archive_next_attempt_at = format_utc_timestamp(
                utc_now() + timedelta(seconds=delay)
            )
            upload.archive_failure = f"{type(exc).__name__}: {exc}"[:1000]

    def _finalize(self, collection_id: int) -> None:
        if self._advance_derivative_provenance(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._advance_provenance_closure_validation(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._advance_archive_tree_checkpoint(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._publish_next_archive_volume_metadata(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._publish_next_provenance_archive_object(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._publish_final_authority(collection_id):
            self._requeue_finalization_step(collection_id)
            return
        if self._advance_catalog_projection(collection_id):
            self._requeue_finalization_step(collection_id)

    def _advance_derivative_provenance(self, collection_id: int) -> bool:
        """Advance one bounded step of server-owned transform provenance."""

        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None or upload.derivative_provenance_state in {
                "not-required",
                "complete",
            }:
                return False
            if upload.derivative_provenance_state == "failed":
                raise Conflict("server-generated derivative provenance failed")
            try:
                if upload.derivative_provenance_state == "discovering":
                    _advance_derivative_source_discovery(session, upload)
                elif upload.derivative_provenance_state == "copying":
                    _advance_derivative_source_closure(session, upload)
                elif upload.derivative_provenance_state == "generating":
                    _advance_derivative_output_journal(session, upload)
                else:  # pragma: no cover - constrained durable state
                    raise RuntimeError("derivative provenance state is invalid")
            except Exception:
                upload.derivative_provenance_state = "failed"
                raise
            return True

    def _publish_final_authority(self, collection_id: int) -> bool:
        """Publish the bounded immutable root once and persist its exact receipts."""

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.final_authority_json is not None:
                return False
            if (
                upload.archive_tree_sha256 is None
                or upload.archive_ordered_volume_sha256 is None
                or upload.archive_terminal_receipt_json is None
            ):
                raise RuntimeError("archive authority checkpoints are incomplete")
            store_name = upload.archive_store
            prefix = upload.archive_storage_prefix
            encryption_format = upload.encryption_format
            passphrase_id = upload.passphrase_id
            sealed_provenance = _sealed_upload_provenance(upload)
            manifest = build_collection_archive_root_manifest(
                archive_generation=upload.archive_generation,
                tree={
                    "files": int(upload.file_count),
                    "bytes": int(upload.file_bytes),
                    "sha256": upload.archive_tree_sha256,
                },
                ordered_volume_sha256=upload.archive_ordered_volume_sha256,
                provenance_identity=(sealed_provenance.identity if sealed_provenance else None),
                provenance_objects=((sealed_provenance.root,) if sealed_provenance else ()),
            )
        if not prefix:
            raise RuntimeError("collection archive storage prefix is missing")
        self._begin_final_publication_attempt(collection_id)
        passphrase = self._config.archive_passphrase_for(passphrase_id)
        archive_store = self._archive_stores.require(store_name)
        root = ArchiveRootPublisher(
            object_store=archive_store.immutable_objects,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
        ).publish_root_manifest(archive_storage_prefix=prefix, manifest=manifest)
        recovery = ArchiveRecoveryDescriptorPublisher(
            object_store=archive_store.immutable_objects
        ).publish(
            archive_storage_prefix=prefix,
            root=root,
            encryption=CollectionEncryptionBinding(
                format=encryption_format,
                passphrase_id=passphrase_id,
            ),
        )
        authority = {
            "root": {
                "object_path": root.object_path,
                "relative_path": root.relative_path,
                "revision": root.revision,
                "plaintext_bytes": root.plaintext_bytes,
                "plaintext_sha256": root.plaintext_sha256,
                "stored_bytes": root.stored_bytes,
                "stored_sha256": root.stored_sha256,
                "tree_sha256": root.tree_sha256,
                "files": root.files,
                "bytes": root.bytes,
                "completed_at": root.completed_at,
            },
            "recovery": {
                "object_path": recovery.object_path,
                "relative_path": recovery.relative_path,
                "revision": recovery.revision,
                "bytes": recovery.bytes,
                "sha256": recovery.sha256,
                "completed_at": recovery.completed_at,
            },
        }
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":"))
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is not None:
                if upload.final_authority_json not in {None, encoded}:
                    raise RuntimeError("final archive authority receipt changed")
                upload.final_authority_json = encoded
        return True

    def _advance_catalog_projection(self, collection_id: int) -> bool:
        """Advance one bounded durable catalog-projection transaction."""

        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None:
                return False
            phase = upload.catalog_phase
            if phase == "complete":
                return False
            if phase in {"content-identity", "inventory-identity"}:
                _advance_catalog_identity(session, upload)
            elif phase == "collection":
                self._create_catalog_collection(session, upload)
            elif phase == "files":
                _advance_catalog_files(session, upload)
            elif phase == "journals":
                _advance_catalog_journals(session, upload)
            elif phase == "provenance-relations":
                _advance_catalog_provenance_relations(session, upload)
            elif phase == "bindings":
                _advance_catalog_bindings(session, upload)
            elif phase == "tags":
                _advance_catalog_tags(session, upload)
            elif phase == "archive-objects":
                self._advance_catalog_archive_objects(session, upload)
            elif phase == "file-objects":
                _advance_catalog_file_objects(session, upload)
            elif phase == "terminal":
                self._publish_catalog_collection(session, upload)
            else:  # pragma: no cover - constrained durable state
                raise RuntimeError(f"unknown catalog finalization phase: {phase}")
            return True

    def _create_catalog_collection(
        self,
        session: Session,
        upload: CollectionUploadRecord,
    ) -> None:
        if upload.catalog_content_identity is None or upload.catalog_inventory_identity is None:
            raise RuntimeError("catalog identities are incomplete")
        if session.get(CollectionRecord, upload.collection_id) is None:
            now = utc_timestamp_now()
            provenance_mode = _final_provenance_mode(
                session,
                upload.collection_id,
                upload.provenance_mode,
            )
            session.add(
                CollectionRecord(
                    id=upload.collection_id,
                    creation_idempotency_key=upload.idempotency_key,
                    creation_identity_sha256=upload.creation_identity_sha256,
                    creation_custody_mode=upload.custody_mode,
                    archive_generation=upload.archive_generation,
                    content_identity=upload.catalog_content_identity,
                    tag_set_identity=upload.tag_set_identity,
                    encryption_format=upload.encryption_format,
                    passphrase_id=upload.passphrase_id,
                    provenance_mode=provenance_mode,
                    provenance_identity=upload.provenance_identity,
                    inventory_identity=upload.catalog_inventory_identity,
                    metadata_revision=1,
                    metadata_updated_at=now,
                    ingest_source=upload.ingest_source,
                    created_by_app=upload.initiated_by_app,
                    created_by_key_id=upload.initiated_by_key_id,
                    created_at=upload.opened_at or now,
                    is_published=False,
                    file_count=upload.file_count,
                    file_bytes=upload.file_bytes,
                )
            )
            session.flush()
            session.add(
                CollectionArchiveCopyRecord(
                    collection_id=upload.collection_id,
                    store=upload.archive_store,
                    state="uploaded",
                    archive_storage_prefix=upload.archive_storage_prefix,
                    last_uploaded_at=now,
                    last_verified_at=now,
                )
            )
        upload.catalog_phase = "files"
        upload.catalog_cursor_json = "{}"

    def _advance_catalog_archive_objects(
        self,
        session: Session,
        upload: CollectionUploadRecord,
    ) -> None:
        cursor = _catalog_cursor(upload)
        section = str(cursor.get("section", "volumes"))
        total_volumes = _planner_checkpoint(upload).next_sequence
        now = utc_timestamp_now()
        if section == "volumes":
            sequence = _cursor_nonnegative_int(cursor, "sequence")
            if sequence < total_volumes:
                record = session.scalar(
                    select(CollectionArchiveObjectUploadRecord).where(
                        CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id,
                        CollectionArchiveObjectUploadRecord.sequence == sequence,
                    )
                )
                if record is None or record.sealed_receipt_json is None:
                    raise RuntimeError("catalog archive volume receipt is unavailable")
                volume = (
                    _parse_sealed_pack(record.sealed_receipt_json)
                    if record.kind == "pack"
                    else _parse_sealed_raw(record.sealed_receipt_json)
                )
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=upload.collection_id,
                        store=upload.archive_store,
                        object_id=volume.volume_id,
                        object_order=sequence,
                        kind=record.kind,
                        object_path=f"{upload.archive_storage_prefix}/{volume.relative_path}",
                        plaintext_bytes=volume.plaintext_bytes,
                        stored_bytes=volume.stored_bytes,
                        sha256=None,
                        stored_sha256=None,
                        revision=volume.revision,
                        age_state_json=volume.age_state_json,
                        archive_parts_json=_catalog_archive_parts_json(volume.parts),
                        plan_sha256=(
                            volume.plan_sha256 if isinstance(volume, SealedPackVolume) else None
                        ),
                        index_sha256=(
                            volume.index_sha256 if isinstance(volume, SealedPackVolume) else None
                        ),
                        uploaded_at=volume.completed_at,
                        verified_at=now,
                    )
                )
                if record.metadata_receipt_json is None:
                    raise RuntimeError("catalog archive volume metadata receipt is unavailable")
                metadata = _parse_archive_volume_metadata_receipt(record.metadata_receipt_json)
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=upload.collection_id,
                        store=upload.archive_store,
                        object_id=f"volume-metadata-{format_archive_sequence(sequence)}",
                        object_order=total_volumes + sequence,
                        kind="volume-metadata",
                        object_path=metadata.object_path,
                        plaintext_bytes=metadata.plaintext_bytes,
                        stored_bytes=metadata.stored_bytes,
                        sha256=metadata.plaintext_sha256,
                        stored_sha256=metadata.stored_sha256,
                        revision=metadata.revision,
                        uploaded_at=metadata.completed_at,
                        verified_at=now,
                    )
                )
                if volume.retrieval_cache is not None:
                    receipt = volume.retrieval_cache
                    if receipt.stored_bytes != volume.stored_bytes or (
                        receipt.stored_sha256 is not None and len(receipt.stored_sha256) != 64
                    ):
                        raise RuntimeError(
                            "retrieval cache receipt does not match its sealed archive volume"
                        )
                    register_cache_ready(
                        session,
                        source_store=upload.archive_store,
                        collection_id=upload.collection_id,
                        object_id=volume.volume_id,
                        receipt=receipt,
                    )
                    session.flush()
                    session.add(
                        RetrievalCacheLeaseRecord(
                            owner="new-archive",
                            source_store=upload.archive_store,
                            collection_id=upload.collection_id,
                            object_id=volume.volume_id,
                            expires_at=format_utc_timestamp(
                                utc_now() + self._config.retrieval_cache_new_archive_lease
                            ),
                        )
                    )
                _set_catalog_cursor(upload, {"section": "volumes", "sequence": sequence + 1})
                return
            if upload.archive_terminal_receipt_json is None:
                raise RuntimeError("catalog archive terminal receipt is unavailable")
            metadata = _parse_archive_volume_metadata_receipt(upload.archive_terminal_receipt_json)
            session.add(
                CollectionArchiveObjectRecord(
                    collection_id=upload.collection_id,
                    store=upload.archive_store,
                    object_id=(f"volume-terminal-{format_archive_sequence(total_volumes)}"),
                    object_order=2 * total_volumes,
                    kind="volume-terminal",
                    object_path=metadata.object_path,
                    plaintext_bytes=metadata.plaintext_bytes,
                    stored_bytes=metadata.stored_bytes,
                    sha256=metadata.plaintext_sha256,
                    stored_sha256=metadata.stored_sha256,
                    revision=metadata.revision,
                    uploaded_at=metadata.completed_at,
                    verified_at=now,
                )
            )
            _set_catalog_cursor(upload, {"section": "provenance", "sequence": 0})
            return
        provenance_count = int(upload.provenance_archive_next_sequence)
        if section == "provenance":
            sequence = _cursor_nonnegative_int(cursor, "sequence")
            if sequence < provenance_count:
                row = session.get(
                    CollectionUploadProvenanceArchiveVolumeRecord,
                    (upload.collection_id, sequence),
                )
                if row is None:
                    raise RuntimeError("catalog provenance archive volume is unavailable")
                base_order = 2 * total_volumes + 1 + 2 * sequence
                for offset, current in enumerate(
                    (
                        _parse_sealed_provenance_object(row.payload_receipt_json),
                        _parse_sealed_provenance_object(row.metadata_receipt_json),
                    )
                ):
                    session.add(
                        _catalog_small_archive_object(
                            upload=upload,
                            current=current,
                            object_order=base_order + offset,
                            verified_at=now,
                        )
                    )
                _set_catalog_cursor(upload, {"section": "provenance", "sequence": sequence + 1})
                return
            if upload.provenance_mode != "omitted":
                if upload.provenance_archive_terminal_receipt_json is None:
                    raise RuntimeError("catalog provenance terminal receipt is unavailable")
                terminal = _parse_sealed_provenance_object(
                    upload.provenance_archive_terminal_receipt_json
                )
                session.add(
                    _catalog_small_archive_object(
                        upload=upload,
                        current=terminal,
                        object_order=2 * total_volumes + 1 + 2 * provenance_count,
                        verified_at=now,
                    )
                )
            _set_catalog_cursor(upload, {"section": "roots"})
            return
        if section == "roots":
            authority = _final_authority(upload)
            order = (
                2 * total_volumes
                + 1
                + 2 * provenance_count
                + int(upload.provenance_mode != "omitted")
            )
            if upload.provenance_mode != "omitted":
                sealed = _sealed_upload_provenance(upload)
                assert sealed is not None
                session.add(
                    _catalog_small_archive_object(
                        upload=upload,
                        current=sealed.root,
                        object_order=order,
                        verified_at=now,
                    )
                )
                order += 1
            for object_id, kind, value in (
                ("manifest", "manifest", authority["root"]),
                ("recovery-descriptor", "recovery-descriptor", authority["recovery"]),
            ):
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=upload.collection_id,
                        store=upload.archive_store,
                        object_id=object_id,
                        object_order=order,
                        kind=kind,
                        object_path=str(value["object_path"]),
                        plaintext_bytes=_mapping_nonnegative_int(
                            value, "plaintext_bytes", fallback="bytes"
                        ),
                        stored_bytes=_mapping_nonnegative_int(
                            value, "stored_bytes", fallback="bytes"
                        ),
                        sha256=str(value.get("plaintext_sha256", value.get("sha256"))),
                        stored_sha256=str(value.get("stored_sha256", value.get("sha256"))),
                        revision=(
                            str(value["revision"]) if value.get("revision") is not None else None
                        ),
                        uploaded_at=str(value["completed_at"]),
                        verified_at=now,
                    )
                )
                order += 1
            upload.catalog_phase = "file-objects"
            upload.catalog_cursor_json = "{}"
            return
        raise RuntimeError("catalog archive-object cursor section is invalid")

    def _publish_catalog_collection(
        self,
        session: Session,
        upload: CollectionUploadRecord,
    ) -> None:
        collection = session.get(CollectionRecord, upload.collection_id)
        if collection is None:
            raise RuntimeError("catalog collection projection is unavailable")
        now = utc_timestamp_now()
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=upload.collection_id,
                store=upload.archive_store,
                desired_revision=1,
                state="pending",
                attempt_count=0,
                next_attempt_at=now,
            )
        )
        catalog_event = begin_catalog_event(
            session,
            change="created",
            collection_id=upload.collection_id,
            occurred_at=now,
            inventory_identity=collection.inventory_identity,
        )
        snapshot_catalog_event_collection_tags(
            session,
            event=catalog_event,
            phase="after",
            collection_id=upload.collection_id,
        )
        authority = _final_authority(upload)
        self._events.emit_collection(
            type="collection.finalized",
            collection_id=upload.collection_id,
            details={
                "files_total": int(upload.file_count),
                "bytes_total": int(upload.file_bytes),
                "archive_root_sha256": str(authority["root"]["plaintext_sha256"]),
            },
            terminal=True,
            session=session,
        )
        collection.is_published = True
        upload.catalog_phase = "complete"
        session.delete(upload)

    def _advance_provenance_closure_validation(self, collection_id: int) -> bool:
        """Validate one bounded slice of the exact provenance closure."""

        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None or upload.provenance_closure_validated:
                return False
            if upload.provenance_mode == "omitted":
                inconsistent = session.scalar(
                    select(
                        exists().where(
                            CollectionUploadFileRecord.collection_id == collection_id,
                            CollectionUploadFileRecord.provenance_status != "omitted",
                        )
                        | exists().where(
                            CollectionUploadProvenanceJournalRecord.collection_id == collection_id
                        )
                    )
                )
                if inconsistent:
                    raise Conflict(
                        "collection-wide provenance omission is not internally consistent"
                    )
                upload.provenance_validation_next_file_order = upload.file_count
                upload.provenance_closure_validated = True
                return True
            if upload.provenance_validation_next_file_order < upload.file_count:
                rows = list(
                    session.scalars(
                        select(CollectionUploadFileRecord)
                        .where(
                            CollectionUploadFileRecord.collection_id == collection_id,
                            CollectionUploadFileRecord.file_order
                            >= upload.provenance_validation_next_file_order,
                        )
                        .order_by(CollectionUploadFileRecord.file_order)
                        .limit(_FINALIZATION_FILE_BATCH)
                    )
                )
                if not rows:
                    raise Conflict("provenance bindings do not cover the collection tree")
                expected_order = upload.provenance_validation_next_file_order
                for row in rows:
                    if row.file_order != expected_order:
                        raise Conflict("provenance binding order is not contiguous")
                    _validate_upload_file_provenance_binding(session, row)
                    if row.provenance_status == "captured":
                        assert row.provenance_journal_id is not None
                        key = (collection_id, row.provenance_journal_id)
                        if session.get(CollectionUploadProvenanceReachabilityRecord, key) is None:
                            session.add(
                                CollectionUploadProvenanceReachabilityRecord(
                                    collection_id=collection_id,
                                    journal_id=row.provenance_journal_id,
                                )
                            )
                    expected_order += 1
                upload.provenance_validation_next_file_order = expected_order
                return True

            reachable = session.scalar(
                select(CollectionUploadProvenanceReachabilityRecord)
                .where(
                    CollectionUploadProvenanceReachabilityRecord.collection_id == collection_id,
                    CollectionUploadProvenanceReachabilityRecord.expanded.is_(False),
                )
                .order_by(CollectionUploadProvenanceReachabilityRecord.journal_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if reachable is not None:
                statement = select(CollectionUploadProvenanceValidationFactRecord).where(
                    CollectionUploadProvenanceValidationFactRecord.collection_id == collection_id,
                    CollectionUploadProvenanceValidationFactRecord.journal_id
                    == reachable.journal_id,
                    CollectionUploadProvenanceValidationFactRecord.kind == "external-state",
                )
                if reachable.after_external_fact_key is not None:
                    statement = statement.where(
                        CollectionUploadProvenanceValidationFactRecord.fact_key
                        > reachable.after_external_fact_key
                    )
                facts = list(
                    session.scalars(
                        statement.order_by(
                            CollectionUploadProvenanceValidationFactRecord.fact_key
                        ).limit(_FINALIZATION_FILE_BATCH + 1)
                    )
                )
                for fact in facts[:_FINALIZATION_FILE_BATCH]:
                    reference = _validate_external_state_reference(session, fact)
                    key = (collection_id, reference)
                    if session.get(CollectionUploadProvenanceReachabilityRecord, key) is None:
                        session.add(
                            CollectionUploadProvenanceReachabilityRecord(
                                collection_id=collection_id,
                                journal_id=reference,
                            )
                        )
                    reachable.after_external_fact_key = fact.fact_key
                if len(facts) <= _FINALIZATION_FILE_BATCH:
                    reachable.expanded = True
                return True

            journal_count = int(
                session.scalar(
                    select(func.count(CollectionUploadProvenanceJournalRecord.journal_id)).where(
                        CollectionUploadProvenanceJournalRecord.collection_id == collection_id
                    )
                )
                or 0
            )
            reachable_count = int(
                session.scalar(
                    select(
                        func.count(CollectionUploadProvenanceReachabilityRecord.journal_id)
                    ).where(
                        CollectionUploadProvenanceReachabilityRecord.collection_id == collection_id
                    )
                )
                or 0
            )
            if journal_count != reachable_count:
                raise Conflict("provenance contains a journal outside the captured closure")
            upload.provenance_closure_validated = True
            return True

    def _publish_next_provenance_archive_object(self, collection_id: int) -> bool:
        """Publish one bounded provenance volume, checkpoint, or final root."""

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or not upload.provenance_closure_validated:
                return False
            if upload.provenance_mode == "omitted":
                return False
            if upload.archive_tree_sha256 is None:
                return False
            if upload.provenance_archive_root_receipt_json is not None:
                return False
            sequence = int(upload.provenance_archive_next_sequence)
            prefix = upload.archive_storage_prefix
            store_name = upload.archive_store
            archive_generation = upload.archive_generation
            passphrase = self._config.archive_passphrase_for(upload.passphrase_id)
            tree_sha256 = upload.archive_tree_sha256
            publish_terminal = False

            if upload.provenance_archive_next_file_order < upload.file_count:
                first_file_order = int(upload.provenance_archive_next_file_order)
                rows = list(
                    session.scalars(
                        select(CollectionUploadFileRecord)
                        .where(
                            CollectionUploadFileRecord.collection_id == collection_id,
                            CollectionUploadFileRecord.file_order >= first_file_order,
                        )
                        .order_by(CollectionUploadFileRecord.file_order)
                        .limit(PROVENANCE_BINDING_SEGMENT_FILES_MAX)
                    )
                )
                if not rows or rows[0].file_order != first_file_order:
                    raise RuntimeError("provenance binding publication is not contiguous")
                binding_rows: list[Mapping[str, object]] = [
                    _provenance_binding_row(row) for row in rows
                ]
                payload, used = bounded_binding_segment_bytes(
                    first_file_order=first_file_order,
                    files=binding_rows,
                )
                rows = rows[:used]
                document = _provenance_volume_document(
                    archive_generation=archive_generation,
                    tree_sha256=tree_sha256,
                    sequence=sequence,
                    payload=payload,
                    first_file_order=first_file_order,
                    file_count=len(rows),
                )
                next_file_order = first_file_order + len(rows)
                journal_id = None
                next_journal_offset = 0
            else:
                journal = _next_provenance_publication_journal(session, upload)
                if journal is None:
                    if upload.provenance_archive_terminal_receipt_json is None:
                        terminal_document = ProvenanceTerminalDocument(
                            archive_generation=archive_generation,
                            archive_tree_sha256=tree_sha256,
                            sequence=sequence,
                        )
                        publish_terminal = True
                        publish_root = False
                        root_document = None
                    else:
                        if upload.provenance_archive_ordered_sha256 is None:
                            raise RuntimeError("provenance terminal has no ordered commitment")
                        root_document = ProvenanceRootDocument(
                            archive_generation=archive_generation,
                            archive_tree_sha256=tree_sha256,
                            ordered_volume_sha256=upload.provenance_archive_ordered_sha256,
                        )
                        publish_root = True
                    document = None
                    payload = b""
                    next_file_order = int(upload.provenance_archive_next_file_order)
                    journal_id = None
                    next_journal_offset = 0
                else:
                    publish_root = False
                    offset = int(upload.provenance_archive_current_journal_offset)
                    payload = _upload_journal_range_bytes(
                        session,
                        collection_id,
                        journal.journal_id,
                        offset=offset,
                        size=min(
                            PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
                            int(journal.bytes) - offset,
                        ),
                    )
                    document = _provenance_volume_document(
                        archive_generation=archive_generation,
                        tree_sha256=tree_sha256,
                        sequence=sequence,
                        payload=payload,
                        journal=journal,
                        journal_offset=offset,
                    )
                    next_file_order = int(upload.provenance_archive_next_file_order)
                    journal_id = journal.journal_id
                    next_journal_offset = offset + len(payload)
            if upload.provenance_archive_next_file_order < upload.file_count:
                publish_root = False
                root_document = None

        publisher = ArchiveProvenancePublisher(
            object_store=self._archive_stores.require(store_name).immutable_objects,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
        )
        if publish_terminal:
            sealed_terminal = publisher.publish_terminal(
                archive_storage_prefix=prefix,
                terminal=terminal_document,
            )
            with session_scope(self._session_factory) as session:
                upload = session.scalar(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.collection_id == collection_id)
                    .with_for_update()
                )
                if upload is None:
                    return False
                if upload.provenance_archive_terminal_receipt_json is None:
                    digest = (
                        CheckpointSHA256.from_state(upload.provenance_archive_hash_state)
                        if upload.provenance_archive_hash_state is not None
                        else CheckpointSHA256()
                    )
                    update_ordered_volume_commitment(digest, terminal_document)
                    upload.provenance_archive_terminal_receipt_json = (
                        _sealed_provenance_object_json(sealed_terminal)
                    )
                    upload.provenance_archive_ordered_sha256 = digest.hexdigest()
                    upload.provenance_archive_hash_state = None
            return True
        if publish_root:
            assert root_document is not None
            sealed_root = publisher.publish_root(
                archive_storage_prefix=prefix,
                root=root_document,
            )
            with session_scope(self._session_factory) as session:
                upload = session.scalar(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.collection_id == collection_id)
                    .with_for_update()
                )
                if upload is None:
                    return False
                if upload.provenance_archive_root_receipt_json is None:
                    upload.provenance_identity = sealed_root.identity
                    upload.provenance_archive_root_receipt_json = _sealed_provenance_json(
                        sealed_root
                    )
            return True

        assert document is not None
        sealed = publisher.publish_volume(
            archive_storage_prefix=prefix,
            document=document,
            payload=payload,
        )
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None:
                return False
            if upload.provenance_archive_next_sequence != sequence:
                return True
            digest = (
                CheckpointSHA256.from_state(upload.provenance_archive_hash_state)
                if upload.provenance_archive_hash_state is not None
                else CheckpointSHA256()
            )
            update_ordered_volume_commitment(digest, document)
            document_bytes = document.to_json_bytes()
            session.add(
                CollectionUploadProvenanceArchiveVolumeRecord(
                    collection_id=collection_id,
                    sequence=sequence,
                    kind=document.payload.kind,
                    document_json=document_bytes.decode("utf-8"),
                    payload_receipt_json=_sealed_provenance_object_json(sealed.payload),
                    metadata_receipt_json=_sealed_provenance_object_json(sealed.metadata),
                )
            )
            upload.provenance_archive_next_sequence = sequence + 1
            upload.provenance_archive_hash_state = digest.export_state()
            upload.provenance_archive_next_file_order = next_file_order
            if journal_id is not None:
                if upload.provenance_archive_current_journal_id not in {None, journal_id}:
                    raise RuntimeError("provenance journal publication changed identity")
                if document.journal_bytes == next_journal_offset:
                    upload.provenance_archive_last_journal_id = journal_id
                    upload.provenance_archive_current_journal_id = None
                    upload.provenance_archive_current_journal_offset = 0
                else:
                    upload.provenance_archive_current_journal_id = journal_id
                    upload.provenance_archive_current_journal_offset = next_journal_offset
            return True

    def _advance_archive_tree_checkpoint(self, collection_id: int) -> bool:
        """Advance at most one bounded file batch; return whether work was performed."""

        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None or upload.archive_tree_sha256 is not None:
                return False
            digest = (
                CheckpointSHA256.from_state(upload.archive_tree_hash_state)
                if upload.archive_tree_hash_state is not None
                else CheckpointSHA256()
            )
            rows = list(
                session.scalars(
                    select(CollectionUploadFileRecord)
                    .where(
                        CollectionUploadFileRecord.collection_id == collection_id,
                        CollectionUploadFileRecord.file_order
                        >= upload.archive_tree_next_file_order,
                    )
                    .order_by(CollectionUploadFileRecord.file_order)
                    .limit(_FINALIZATION_FILE_BATCH)
                )
            )
            if not rows:
                if upload.archive_tree_next_file_order != upload.file_count:
                    raise RuntimeError("archive tree checkpoint does not cover registered files")
                upload.archive_tree_sha256 = digest.hexdigest()
                upload.archive_tree_hash_state = None
                return True
            expected = upload.archive_tree_next_file_order
            for row in rows:
                if row.file_order != expected:
                    raise RuntimeError("archive tree file order is not contiguous")
                digest.update(f"{row.path}\t{row.bytes}\t{row.sha256}\n".encode())
                expected += 1
            upload.archive_tree_next_file_order = expected
            if expected == upload.file_count:
                upload.archive_tree_sha256 = digest.hexdigest()
                upload.archive_tree_hash_state = None
            else:
                upload.archive_tree_hash_state = digest.export_state()
            return True

    def _publish_next_archive_volume_metadata(self, collection_id: int) -> bool:
        """Publish and checkpoint one bounded volume document in sequence order."""

        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None or upload.archive_tree_sha256 is None:
                return False
            total_volumes = _planner_checkpoint(upload).next_sequence
            sequence = upload.archive_volume_next_sequence
            if sequence >= total_volumes:
                if sequence != total_volumes:
                    raise RuntimeError("archive volume metadata checkpoint exceeds its authority")
                if upload.archive_terminal_receipt_json is not None:
                    if upload.archive_ordered_volume_sha256 is None:
                        raise RuntimeError("archive terminal has no ordered commitment")
                    return False
                prefix = upload.archive_storage_prefix
                store_name = upload.archive_store
                archive_generation = upload.archive_generation
                passphrase = self._config.archive_passphrase_for(upload.passphrase_id)
                tree_sha256 = upload.archive_tree_sha256
                terminal = build_collection_archive_terminal_document(
                    archive_generation=archive_generation,
                    tree_sha256=tree_sha256,
                    sequence=sequence,
                )
                terminal_mode = True
            else:
                terminal_mode = False
            if terminal_mode:
                record = None
            else:
                record = session.scalar(
                    select(CollectionArchiveObjectUploadRecord).where(
                        CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                        CollectionArchiveObjectUploadRecord.sequence == sequence,
                    )
                )
                if record is None or record.sealed_receipt_json is None:
                    raise RuntimeError("archive volume metadata source is not sealed")
                prefix = upload.archive_storage_prefix
                store_name = upload.archive_store
                archive_generation = upload.archive_generation
                passphrase = self._config.archive_passphrase_for(upload.passphrase_id)
                tree_sha256 = upload.archive_tree_sha256
                receipt: SealedPackVolume | SealedRawVolume
                plan: PackVolumePlan | None
                if record.kind == "pack":
                    plan = parse_pack_volume_plan(record.plan_json)
                    receipt = _parse_sealed_pack(record.sealed_receipt_json)
                elif record.kind == "segment":
                    plan = None
                    receipt = _parse_sealed_raw(record.sealed_receipt_json)
                else:
                    raise RuntimeError(f"unsupported archive volume kind: {record.kind}")
        publisher = ArchiveRootPublisher(
            object_store=self._archive_stores.require(store_name).immutable_objects,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
        )
        document: CollectionArchiveTerminalDocument | CollectionArchiveVolumeDocument
        if terminal_mode:
            document = terminal
            published = publisher.publish_terminal_metadata(
                archive_storage_prefix=prefix, document=terminal
            )
        else:
            volume_document = build_collection_archive_volume_document(
                archive_generation=archive_generation,
                tree_sha256=tree_sha256,
                plan=plan,
                receipt=receipt,
            )
            document = volume_document
            published = publisher.publish_volume_metadata(
                archive_storage_prefix=prefix,
                document=volume_document,
            )
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            record = (
                session.scalar(
                    select(CollectionArchiveObjectUploadRecord)
                    .where(
                        CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                        CollectionArchiveObjectUploadRecord.sequence == sequence,
                    )
                    .with_for_update()
                )
                if not terminal_mode
                else None
            )
            if upload is None or (not terminal_mode and record is None):
                return False
            if upload.archive_volume_next_sequence != sequence:
                return True
            digest = (
                CheckpointSHA256.from_state(upload.archive_volume_hash_state)
                if upload.archive_volume_hash_state is not None
                else CheckpointSHA256()
            )
            update_archive_sequence_commitment(digest, document)
            if terminal_mode:
                upload.archive_terminal_receipt_json = _archive_volume_metadata_receipt_json(
                    published
                )
                upload.archive_ordered_volume_sha256 = digest.hexdigest()
                upload.archive_volume_hash_state = None
            else:
                assert record is not None
                record.metadata_receipt_json = _archive_volume_metadata_receipt_json(published)
                upload.archive_volume_next_sequence = sequence + 1
                upload.archive_volume_hash_state = digest.export_state()
            return True

    def _requeue_finalization_step(self, collection_id: int) -> None:
        now = utc_timestamp_now()
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.state = "finalizing"
            upload.archive_phase = "finalization_queued"
            upload.archive_phase_updated_at = now
            upload.archive_next_attempt_at = now
            upload.archive_failure = None

    def _begin_final_publication_attempt(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            upload.archive_attempt_count += 1


def _catalog_cursor(upload: CollectionUploadRecord) -> dict[str, object]:
    try:
        value = json.loads(upload.catalog_cursor_json)
    except json.JSONDecodeError as exc:  # pragma: no cover - durable corruption
        raise RuntimeError("catalog finalization cursor is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("catalog finalization cursor is not an object")
    return value


def _set_catalog_cursor(upload: CollectionUploadRecord, value: Mapping[str, object]) -> None:
    upload.catalog_cursor_json = json.dumps(value, sort_keys=True, separators=(",", ":"))


def _cursor_nonnegative_int(cursor: Mapping[str, object], key: str) -> int:
    value = cursor.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"catalog cursor {key} is invalid")
    return value


def _mapping_nonnegative_int(
    value: Mapping[str, object],
    key: str,
    *,
    fallback: str,
) -> int:
    current = value.get(key, value.get(fallback, 0))
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise RuntimeError(f"archive authority {key} is invalid")
    return current


def _advance_catalog_identity(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    next_order = _cursor_nonnegative_int(cursor, "next_file_order")
    digest = (
        CheckpointSHA256.from_state(upload.catalog_hash_state)
        if upload.catalog_hash_state is not None
        else CheckpointSHA256()
    )
    if upload.catalog_phase == "content-identity" and next_order == 0:
        digest.update(b'{"files":[')
    if upload.catalog_phase == "inventory-identity" and next_order == 0:
        if upload.catalog_content_identity is None:
            raise RuntimeError("portable inventory has no content identity")
        provenance_mode = _final_provenance_mode(
            session,
            upload.collection_id,
            upload.provenance_mode,
        )
        header = PortableCollectionHeader(
            collection=upload.collection_id,
            content_identity=upload.catalog_content_identity,
            encryption_format=upload.encryption_format,
            passphrase_id=upload.passphrase_id,
            provenance_mode=provenance_mode,  # type: ignore[arg-type]
            provenance_identity=upload.provenance_identity,
        )
        digest.update(canonical_json_bytes(header.model_dump(mode="json")))
    rows = list(
        session.scalars(
            select(CollectionUploadFileRecord)
            .where(
                CollectionUploadFileRecord.collection_id == upload.collection_id,
                CollectionUploadFileRecord.file_order >= next_order,
            )
            .order_by(CollectionUploadFileRecord.file_order)
            .limit(_FINALIZATION_FILE_BATCH)
        )
    )
    if rows:
        expected = next_order
        for row in rows:
            if row.file_order != expected:
                raise RuntimeError("catalog identity file order is not contiguous")
            if upload.catalog_phase == "content-identity":
                if expected:
                    digest.update(b",")
                digest.update(
                    json.dumps(
                        {"path": row.path, "bytes": row.bytes, "sha256": row.sha256},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            else:
                encoded = canonical_json_bytes(
                    PortableCollectionFile(
                        path=row.path,
                        bytes=row.bytes,
                        sha256=row.sha256,
                    ).to_mapping()
                )
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            expected += 1
        upload.catalog_hash_state = digest.export_state()
        _set_catalog_cursor(upload, {"next_file_order": expected})
        return
    if next_order != upload.file_count:
        raise RuntimeError("catalog identity does not cover every registered file")
    if upload.catalog_phase == "content-identity":
        digest.update(b'],"format":"riverhog-collection-content/v1"}')
        upload.catalog_content_identity = digest.hexdigest()
        upload.catalog_phase = "inventory-identity"
    else:
        upload.catalog_inventory_identity = digest.hexdigest()
        upload.catalog_phase = "collection"
    upload.catalog_hash_state = None
    upload.catalog_cursor_json = "{}"


def _advance_catalog_files(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    next_order = _cursor_nonnegative_int(cursor, "next_file_order")
    rows = list(
        session.scalars(
            select(CollectionUploadFileRecord)
            .where(
                CollectionUploadFileRecord.collection_id == upload.collection_id,
                CollectionUploadFileRecord.file_order >= next_order,
            )
            .order_by(CollectionUploadFileRecord.file_order)
            .limit(_FINALIZATION_FILE_BATCH)
        )
    )
    if not rows:
        if next_order != upload.file_count:
            raise RuntimeError("catalog file projection is incomplete")
        upload.catalog_phase = "journals"
        upload.catalog_cursor_json = "{}"
        return
    expected = next_order
    values: list[dict[str, object]] = []
    for row in rows:
        if row.file_order != expected:
            raise RuntimeError("catalog file projection order is not contiguous")
        values.append(
            {
                "collection_id": upload.collection_id,
                "path": row.path,
                "bytes": row.bytes,
                "sha256": row.sha256,
                "provenance_status": row.provenance_status,
                "path_sort_key": relpath_sort_key(row.path),
                "search_text": f"{upload.collection_id}/{relpath_search_key(row.path)}",
                "path_search_text": relpath_search_key(row.path),
            }
        )
        expected += 1
    session.execute(insert(CollectionFileRecord), values)
    _set_catalog_cursor(upload, {"next_file_order": expected})


def _advance_catalog_journals(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    journal_id = cursor.get("journal_id")
    after_journal = cursor.get("after_journal_id")
    if journal_id is None:
        statement = select(CollectionUploadProvenanceJournalRecord).where(
            CollectionUploadProvenanceJournalRecord.collection_id == upload.collection_id
        )
        if isinstance(after_journal, str):
            statement = statement.where(
                CollectionUploadProvenanceJournalRecord.journal_id > after_journal
            )
        journal = session.scalar(
            statement.order_by(CollectionUploadProvenanceJournalRecord.journal_id).limit(1)
        )
        if journal is None:
            upload.catalog_phase = "provenance-relations"
            upload.catalog_cursor_json = "{}"
            return
        journal_id = journal.journal_id
        cursor = {
            "after_journal_id": after_journal,
            "journal_id": journal_id,
            "stage": "header",
        }
    else:
        journal = session.get(
            CollectionUploadProvenanceJournalRecord,
            (upload.collection_id, str(journal_id)),
        )
        if journal is None:
            raise RuntimeError("catalog provenance journal disappeared")
    stage = str(cursor.get("stage", "header"))
    if stage == "header":
        if (
            session.get(
                CollectionProvenanceJournalRecord,
                (upload.collection_id, journal.journal_id),
            )
            is None
        ):
            agent_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionUploadProvenanceValidationFactRecord)
                    .where(
                        CollectionUploadProvenanceValidationFactRecord.collection_id
                        == upload.collection_id,
                        CollectionUploadProvenanceValidationFactRecord.journal_id
                        == journal.journal_id,
                        CollectionUploadProvenanceValidationFactRecord.kind == "agent",
                    )
                )
                or 0
            )
            session.add(
                CollectionProvenanceJournalRecord(
                    collection_id=upload.collection_id,
                    journal_id=journal.journal_id,
                    bytes=journal.bytes,
                    sha256=journal.sha256,
                    entries=journal.validation_sequence,
                    agent_count=agent_count,
                    entity_counts_json=journal.entity_counts_json,
                    current_state_id=journal.current_state_id,
                    current_entry_id=journal.current_entry_id,
                    current_entry_json_sha256=journal.current_entry_json_sha256,
                    current_path=journal.current_path,
                    current_bytes=journal.current_bytes,
                    current_sha256=journal.current_sha256,
                )
            )
        cursor.update({"stage": "chunks", "next_ordinal": 0})
        _set_catalog_cursor(upload, cursor)
        return
    if stage == "chunks":
        next_ordinal = _cursor_nonnegative_int(cursor, "next_ordinal")
        rows = list(
            session.scalars(
                select(CollectionUploadProvenanceJournalChunkRecord)
                .where(
                    CollectionUploadProvenanceJournalChunkRecord.collection_id
                    == upload.collection_id,
                    CollectionUploadProvenanceJournalChunkRecord.journal_id == journal.journal_id,
                    CollectionUploadProvenanceJournalChunkRecord.ordinal >= next_ordinal,
                )
                .order_by(CollectionUploadProvenanceJournalChunkRecord.ordinal)
                .limit(16)
            )
        )
        if rows:
            session.execute(
                insert(CollectionProvenanceJournalChunkRecord),
                [
                    {
                        "collection_id": upload.collection_id,
                        "journal_id": journal.journal_id,
                        "ordinal": row.ordinal,
                        "byte_offset": row.byte_offset,
                        "content": row.content,
                    }
                    for row in rows
                ],
            )
            cursor["next_ordinal"] = int(rows[-1].ordinal) + 1
            _set_catalog_cursor(upload, cursor)
            return
        cursor.update({"stage": "agents", "after_fact_key": None})
        _set_catalog_cursor(upload, cursor)
        return
    if stage in {"agents", "entities"}:
        kind = "agent" if stage == "agents" else "entity"
        after_key = cursor.get("after_fact_key")
        fact_statement = select(CollectionUploadProvenanceValidationFactRecord).where(
            CollectionUploadProvenanceValidationFactRecord.collection_id == upload.collection_id,
            CollectionUploadProvenanceValidationFactRecord.journal_id == journal.journal_id,
            CollectionUploadProvenanceValidationFactRecord.kind == kind,
        )
        if isinstance(after_key, str):
            fact_statement = fact_statement.where(
                CollectionUploadProvenanceValidationFactRecord.fact_key > after_key
            )
        facts = list(
            session.scalars(
                fact_statement.order_by(
                    CollectionUploadProvenanceValidationFactRecord.fact_key
                ).limit(512)
            )
        )
        if facts:
            if kind == "agent":
                session.execute(
                    insert(CollectionProvenanceJournalAgentRecord),
                    [
                        {
                            "collection_id": upload.collection_id,
                            "journal_id": journal.journal_id,
                            "agent_id": fact.fact_key,
                        }
                        for fact in facts
                    ],
                )
            else:
                values: list[dict[str, object]] = []
                for fact in facts:
                    value = json.loads(fact.value_json)
                    values.append(
                        {
                            "collection_id": upload.collection_id,
                            "journal_id": journal.journal_id,
                            "entity_type": value["entity_type"],
                            "entity_id": value["entity_id"],
                            "entry_id": value["entry_id"],
                            "document_json": json.dumps(
                                value["document"], sort_keys=True, separators=(",", ":")
                            ),
                        }
                    )
                session.execute(insert(CollectionProvenanceEntityRecord), values)
            cursor["after_fact_key"] = facts[-1].fact_key
            _set_catalog_cursor(upload, cursor)
            return
        if stage == "agents":
            cursor.update({"stage": "entities", "after_fact_key": None})
        else:
            cursor = {"after_journal_id": journal.journal_id}
        _set_catalog_cursor(upload, cursor)
        return
    raise RuntimeError("catalog provenance journal stage is invalid")


def _advance_catalog_provenance_relations(
    session: Session,
    upload: CollectionUploadRecord,
) -> None:
    cursor = _catalog_cursor(upload)
    after_journal = cursor.get("journal_id")
    after_key = cursor.get("fact_key")
    statement = select(CollectionUploadProvenanceValidationFactRecord).where(
        CollectionUploadProvenanceValidationFactRecord.collection_id == upload.collection_id,
        CollectionUploadProvenanceValidationFactRecord.kind == "external-state",
    )
    if isinstance(after_journal, str) and isinstance(after_key, str):
        statement = statement.where(
            or_(
                CollectionUploadProvenanceValidationFactRecord.journal_id > after_journal,
                (CollectionUploadProvenanceValidationFactRecord.journal_id == after_journal)
                & (CollectionUploadProvenanceValidationFactRecord.fact_key > after_key),
            )
        )
    facts = list(
        session.scalars(
            statement.order_by(
                CollectionUploadProvenanceValidationFactRecord.journal_id,
                CollectionUploadProvenanceValidationFactRecord.fact_key,
            ).limit(512)
        )
    )
    if not facts:
        upload.catalog_phase = "bindings"
        upload.catalog_cursor_json = "{}"
        return
    values: list[dict[str, object]] = []
    for fact in facts:
        value = json.loads(fact.value_json)
        values.append(
            {
                "collection_id": upload.collection_id,
                "from_journal_id": fact.journal_id,
                "to_journal_id": value["journal_id"],
                "entry_id": value["entry_id"],
                "state_id": value["state_id"],
                "entry_json_sha256": value["entry_json_sha256"],
            }
        )
    session.execute(insert(CollectionProvenanceExternalStateReferenceRecord), values)
    _set_catalog_cursor(
        upload,
        {"journal_id": facts[-1].journal_id, "fact_key": facts[-1].fact_key},
    )


def _advance_catalog_bindings(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    next_order = _cursor_nonnegative_int(cursor, "next_file_order")
    rows = list(
        session.scalars(
            select(CollectionUploadFileRecord)
            .where(
                CollectionUploadFileRecord.collection_id == upload.collection_id,
                CollectionUploadFileRecord.file_order >= next_order,
            )
            .order_by(CollectionUploadFileRecord.file_order)
            .limit(_FINALIZATION_FILE_BATCH)
        )
    )
    if not rows:
        if next_order != upload.file_count:
            raise RuntimeError("catalog provenance bindings are incomplete")
        upload.catalog_phase = "tags"
        upload.catalog_cursor_json = "{}"
        return
    session.execute(
        insert(CollectionFileProvenanceRecord),
        [
            {
                "collection_id": upload.collection_id,
                "path": row.path,
                "status": row.provenance_status,
                "journal_id": row.provenance_journal_id,
                "current_state_id": row.provenance_current_state_id,
                "omission_reason": row.provenance_omission_reason,
            }
            for row in rows
        ],
    )
    _set_catalog_cursor(upload, {"next_file_order": int(rows[-1].file_order) + 1})


def _advance_catalog_tags(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    after = cursor.get("after_tag")
    statement = select(CollectionUploadTagRecord).where(
        CollectionUploadTagRecord.collection_id == upload.collection_id
    )
    if isinstance(after, str):
        statement = statement.where(CollectionUploadTagRecord.tag_id > after)
    rows = list(session.scalars(statement.order_by(CollectionUploadTagRecord.tag_id).limit(100)))
    if not rows:
        upload.catalog_phase = "archive-objects"
        upload.catalog_cursor_json = "{}"
        return
    now = utc_timestamp_now()
    session.execute(
        insert(CollectionTagRecord),
        [
            {
                "collection_id": upload.collection_id,
                "tag_id": row.tag_id,
                "assigned_by_app": upload.initiated_by_app,
                "assigned_by_key_id": upload.initiated_by_key_id,
                "assigned_at": now,
            }
            for row in rows
        ],
    )
    session.execute(
        update(TagRecord)
        .where(TagRecord.id.in_([row.tag_id for row in rows]))
        .values(collection_count=TagRecord.collection_count + 1)
    )
    _set_catalog_cursor(upload, {"after_tag": rows[-1].tag_id})


def _final_authority(upload: CollectionUploadRecord) -> dict[str, dict[str, object]]:
    if upload.final_authority_json is None:
        raise RuntimeError("final archive authority is unavailable")
    try:
        value = json.loads(upload.final_authority_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("final archive authority receipt is invalid") from exc
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), dict) for key in ("root", "recovery")
    ):
        raise RuntimeError("final archive authority receipt is incomplete")
    return cast(dict[str, dict[str, object]], value)


def _catalog_archive_parts_json(parts: Sequence[StoredArchivePart]) -> str:
    return canonical_json_bytes([_part_payload(part) for part in parts]).decode("utf-8")


def _catalog_small_archive_object(
    *,
    upload: CollectionUploadRecord,
    current: Any,
    object_order: int,
    verified_at: str,
) -> CollectionArchiveObjectRecord:
    return CollectionArchiveObjectRecord(
        collection_id=upload.collection_id,
        store=upload.archive_store,
        object_id=str(current.object_id),
        object_order=object_order,
        kind=str(current.kind),
        object_path=f"{upload.archive_storage_prefix}/{current.relative_path}",
        plaintext_bytes=int(current.plaintext_bytes),
        stored_bytes=int(current.stored_bytes),
        sha256=str(current.plaintext_sha256),
        stored_sha256=str(current.stored_sha256),
        revision=current.revision,
        uploaded_at=str(current.completed_at),
        verified_at=verified_at,
    )


def _advance_catalog_file_objects(session: Session, upload: CollectionUploadRecord) -> None:
    cursor = _catalog_cursor(upload)
    sequence = _cursor_nonnegative_int(cursor, "sequence")
    total = _planner_checkpoint(upload).next_sequence
    if sequence >= total:
        if sequence != total:
            raise RuntimeError("catalog file-object cursor exceeds archive authority")
        upload.catalog_phase = "terminal"
        upload.catalog_cursor_json = "{}"
        return
    record = session.scalar(
        select(CollectionArchiveObjectUploadRecord).where(
            CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id,
            CollectionArchiveObjectUploadRecord.sequence == sequence,
        )
    )
    if record is None or record.sealed_receipt_json is None:
        raise RuntimeError("catalog file-object source is unavailable")
    if record.kind == "pack":
        plan = parse_pack_volume_plan(record.plan_json)
        session.execute(
            insert(CollectionArchiveFileObjectRecord),
            [
                {
                    "collection_id": upload.collection_id,
                    "store": upload.archive_store,
                    "path": member.path,
                    "sequence": 0,
                    "object_id": plan.volume_id,
                    "file_offset": 0,
                    "object_offset": member.data_offset,
                    "bytes": member.bytes,
                    "member": member.path,
                }
                for member in plan.members
            ],
        )
    elif record.kind == "segment":
        volume = _parse_sealed_raw(record.sealed_receipt_json)
        if record.source_first_part is None:
            raise RuntimeError("raw archive segment has no source sequence")
        session.add(
            CollectionArchiveFileObjectRecord(
                collection_id=upload.collection_id,
                store=upload.archive_store,
                path=volume.source_path,
                sequence=int(record.source_first_part),
                object_id=volume.volume_id,
                file_offset=volume.file_offset,
                object_offset=0,
                bytes=volume.plaintext_bytes,
                member=None,
            )
        )
    else:
        raise RuntimeError("catalog file-object source kind is invalid")
    _set_catalog_cursor(upload, {"sequence": sequence + 1})


def _collection_id(value: int) -> int:
    try:
        return int(normalize_collection_id(value))
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _normalize_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > 200:
        raise BadRequest("idempotency_key is invalid")
    return normalized


def _collection_upload_creation_identity(
    *,
    tag_set_identity_sha256: str,
    ingest_source: str | None,
    archive_store: str,
    event_context_json: str | None,
    provenance_mode: Literal["captured", "omitted"],
    provenance_omission_reason: str | None,
    custody_mode: CollectionUploadCustodyMode,
) -> CollectionUploadCreationIdentityDocument:
    event_context = json.loads(event_context_json) if event_context_json is not None else None
    if event_context is not None and not isinstance(event_context, dict):  # pragma: no cover
        raise RuntimeError("normalized upload event context is not an object")
    return CollectionUploadCreationIdentityDocument.seal(
        CollectionUploadCreationIdentityPayload(
            tag_set_identity=tag_set_identity_sha256,
            ingest_source=ingest_source,
            archive_store=archive_store,
            event_context=event_context,
            provenance_mode=provenance_mode,
            provenance_omission_reason=provenance_omission_reason,
            custody_mode=custody_mode,
        )
    )


def _require_transform_output_intent(
    session: Session,
    *,
    initiator: ApplicationPrincipal,
    idempotency_key: str,
    tag_set_identity_sha256: str,
    ingest_source: str | None,
    archive_store: str | None,
) -> None:
    # The transform namespace is reserved for claim-scoped capability principals.
    prefix = "transform:"
    if not initiator.app.startswith(prefix):
        return
    execution_id = initiator.app.removeprefix(prefix)
    if _SHA256_RE.fullmatch(execution_id) is None:
        raise Forbidden("transform output collections require a scoped capability")
    claim = session.scalar(
        select(CollectionProcessingClaimRecord)
        .where(CollectionProcessingClaimRecord.execution_id == execution_id)
        .with_for_update()
    )
    if (
        claim is None
        or claim.state != "active"
        or claim.plan_sealed_at is None
        or claim.output_tag_set_sha256 is None
        or parse_utc_timestamp(claim.expires_at) <= utc_now()
        or initiator.key_id != claim.consumer_key_id
    ):
        raise Forbidden("transform output intent is not active")
    if (
        idempotency_key != execution_id
        or claim.output_tag_set_sha256 != tag_set_identity_sha256
        or ingest_source != f"transform:{execution_id}"
        or archive_store is not None
    ):
        raise Forbidden("collection upload differs from the sealed transform output intent")


def _require_transform_control_paths(
    upload: CollectionUploadRecord,
    files: Sequence[_RegisteredFile],
) -> None:
    if not upload.initiated_by_app.startswith("transform:"):
        return
    control_paths = {
        str(item["path"]) for item in files if str(item["path"]).startswith("riverhog/")
    }
    if control_paths - {PRODUCER_EVIDENCE_PATH, DERIVATION_EVIDENCE_PATH}:
        raise Conflict("transform output contains an unsupported Riverhog control file")


def _require_transform_output_authority(
    session: Session,
    upload: CollectionUploadRecord,
) -> None:
    """Bind every staged transform payload to the sealed generic authority.

    Target artifacts enter Riverhog custody incrementally before the producer
    can seal its complete production and disposition authorities.  Completion
    is the first boundary where Riverhog can require the exact bijection; file
    registration deliberately remains resumable construction state.
    """

    prefix = "transform:"
    if not upload.initiated_by_app.startswith(prefix):
        return
    execution_id = upload.initiated_by_app.removeprefix(prefix)
    claim = session.scalar(
        select(CollectionProcessingClaimRecord).where(
            CollectionProcessingClaimRecord.execution_id == execution_id
        )
    )
    if claim is None:
        raise Conflict("transform output claim is unavailable")
    disposition_set = session.get(CollectionProcessingDispositionSetRecord, claim.id)
    if disposition_set is None or disposition_set.state != "sealed":
        raise Conflict("transform completion requires a sealed disposition set")
    missing_edge = session.scalar(
        select(CollectionUploadFileRecord.path)
        .where(
            CollectionUploadFileRecord.collection_id == upload.collection_id,
            ~CollectionUploadFileRecord.path.startswith("riverhog/"),
            ~exists().where(
                CollectionProcessingDispositionOutputRecord.claim_id == claim.id,
                CollectionProcessingDispositionOutputRecord.output_path
                == CollectionUploadFileRecord.path,
            ),
        )
        .limit(1)
    )
    if missing_edge is not None:
        raise Conflict(f"transform output file has no exact disposition edge: {missing_edge}")
    missing_file = session.scalar(
        select(CollectionProcessingDispositionOutputRecord.output_path)
        .where(
            CollectionProcessingDispositionOutputRecord.claim_id == claim.id,
            ~exists().where(
                CollectionUploadFileRecord.collection_id == upload.collection_id,
                CollectionUploadFileRecord.path
                == CollectionProcessingDispositionOutputRecord.output_path,
            ),
        )
        .limit(1)
    )
    if missing_file is not None:
        raise Conflict(f"transform disposition output file is absent: {missing_file}")


def _require_tags(session: Session, tags: Sequence[str]) -> None:
    found = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags))))
    missing = sorted(set(tags) - found)
    if missing:
        raise BadRequest(f"collection tags do not exist: {', '.join(missing)}")


def _normalize_file(
    value: CollectionUploadFileIn,
    *,
    provenance_mode: str,
    constraints: CollectionUploadRegistrationConstraintsDocument,
    allow_server_derived: bool = False,
) -> _RegisteredFile:
    path = value.path
    byte_count = value.bytes
    sha256 = value.sha256
    raw_manifest = collection_upload_raw_digest_summary(value, constraints)
    raw_provenance = value.provenance
    provenance_journal_id: str | None = None
    provenance_current_state_id: str | None = None
    provenance_omission_reason: str | None = None
    if raw_provenance is None:
        if not allow_server_derived or provenance_mode != "captured":
            raise BadRequest("captured collection uploads require a provenance binding")
        status = "deriving"
    elif isinstance(raw_provenance, CapturedFileProvenanceBinding):
        if provenance_mode == "omitted":
            raise BadRequest("collection-wide provenance omission cannot contain journals")
        provenance_journal_id = raw_provenance.journal_id
        provenance_current_state_id = raw_provenance.current_state_id
        status = "captured"
    elif isinstance(raw_provenance, OmittedFileProvenanceBinding):
        provenance_omission_reason = raw_provenance.omission_reason
        status = "omitted"
    else:  # pragma: no cover - the discriminated public model is exhaustive
        raise TypeError("collection upload file provenance model is unknown")
    return {
        "path": path,
        "bytes": byte_count,
        "sha256": sha256,
        "raw_part_plaintext_bytes": (
            raw_manifest.part_plaintext_bytes if raw_manifest is not None else None
        ),
        "raw_part_count": raw_manifest.part_count if raw_manifest is not None else None,
        "raw_part_ordered_sha256": (
            raw_manifest.ordered_part_sha256 if raw_manifest is not None else None
        ),
        "provenance_status": str(status),
        "provenance_journal_id": provenance_journal_id,
        "provenance_current_state_id": provenance_current_state_id,
        "provenance_omission_reason": provenance_omission_reason,
    }


def _registered_file_identity(record: CollectionUploadFileRecord) -> _RegisteredFile:
    return {
        "path": record.path,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "raw_part_plaintext_bytes": record.raw_part_plaintext_bytes,
        "raw_part_count": record.raw_part_count,
        "raw_part_ordered_sha256": record.raw_part_ordered_sha256,
        "provenance_status": record.provenance_status,
        "provenance_journal_id": record.provenance_journal_id,
        "provenance_current_state_id": record.provenance_current_state_id,
        "provenance_omission_reason": record.provenance_omission_reason,
    }


def _normalize_provenance_mode(
    mode: str,
    omission_reason: str | None,
) -> tuple[Literal["captured", "omitted"], str | None]:
    if mode == "captured" and omission_reason is None:
        return "captured", None
    if mode == "omitted" and omission_reason:
        normalized = omission_reason.strip()
        if normalized == omission_reason:
            return "omitted", normalized
    raise BadRequest("provenance_mode must be captured, or omitted with provenance_omission_reason")


def _validate_upload_file_provenance_binding(
    session: Session,
    row: CollectionUploadFileRecord,
) -> None:
    if row.provenance_status == "omitted":
        if (
            row.provenance_journal_id is not None
            or row.provenance_current_state_id is not None
            or not row.provenance_omission_reason
            or row.provenance_omission_reason != row.provenance_omission_reason.strip()
        ):
            raise Conflict(f"omitted provenance binding is invalid: {row.path}")
        return
    if row.provenance_status != "captured" or row.provenance_journal_id is None:
        raise Conflict(f"captured provenance binding is incomplete: {row.path}")
    journal = session.get(
        CollectionUploadProvenanceJournalRecord,
        (row.collection_id, row.provenance_journal_id),
    )
    if (
        journal is None
        or journal.state != "sealed"
        or journal.current_state_id != row.provenance_current_state_id
        or journal.current_path != row.path
        or journal.current_bytes != row.bytes
        or journal.current_sha256 != row.sha256
    ):
        raise Conflict(f"captured file state is unresolved: {row.path}")


def _validate_external_state_reference(
    session: Session,
    fact: CollectionUploadProvenanceValidationFactRecord,
) -> str:
    value = json.loads(fact.value_json)
    journal_id = str(value.get("journal_id", ""))
    entry_id = str(value.get("entry_id", ""))
    state_id = str(value.get("state_id", ""))
    entry_sha256 = str(value.get("entry_json_sha256", ""))
    target = session.get(
        CollectionUploadProvenanceJournalRecord,
        (fact.collection_id, journal_id),
    )
    entry = session.get(
        CollectionUploadProvenanceValidationFactRecord,
        (fact.collection_id, journal_id, "entry", entry_id),
    )
    state = session.get(
        CollectionUploadProvenanceValidationFactRecord,
        (fact.collection_id, journal_id, "state", state_id),
    )
    entry_value = json.loads(entry.value_json) if entry is not None else None
    if (
        target is None
        or target.state != "sealed"
        or entry is None
        or state is None
        or not isinstance(entry_value, dict)
        or entry_value.get("sha256") != entry_sha256
    ):
        raise Conflict(f"provenance external state is unresolved: {journal_id}")
    return journal_id


def _provenance_binding_row(row: CollectionUploadFileRecord) -> dict[str, object]:
    binding = FileProvenanceBinding(
        path=row.path,
        bytes=row.bytes,
        sha256=row.sha256,
        status=cast(Any, row.provenance_status),
        journal_id=row.provenance_journal_id,
        current_state_id=row.provenance_current_state_id,
        omission_reason=row.provenance_omission_reason,
    )
    value: dict[str, object] = {
        "path": binding.path,
        "bytes": binding.bytes,
        "sha256": binding.sha256,
        "status": binding.status,
    }
    if binding.status == "captured":
        value.update(
            {
                "journal_id": binding.journal_id,
                "current_state_id": binding.current_state_id,
            }
        )
    else:
        value["omission_reason"] = binding.omission_reason
    return value


def _provenance_volume_document(
    *,
    archive_generation: str,
    tree_sha256: str,
    sequence: int,
    payload: bytes,
    first_file_order: int | None = None,
    file_count: int | None = None,
    journal: CollectionUploadProvenanceJournalRecord | None = None,
    journal_offset: int | None = None,
) -> ProvenanceVolumeDocument:
    kind = "journal" if journal is not None else "bindings"
    identity = ProvenancePayloadIdentity(
        kind=cast(Literal["bindings", "journal"], kind),
        path=(f"provenance/payloads/volume-{format_provenance_sequence(sequence)}.bin.age"),
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    if journal is None:
        return ProvenanceVolumeDocument(
            archive_generation=archive_generation,
            archive_tree_sha256=tree_sha256,
            sequence=sequence,
            payload=identity,
            first_file_order=first_file_order,
            file_count=file_count,
        )
    return ProvenanceVolumeDocument(
        archive_generation=archive_generation,
        archive_tree_sha256=tree_sha256,
        sequence=sequence,
        payload=identity,
        journal_id=journal.journal_id,
        journal_offset=journal_offset,
        journal_bytes=journal.bytes,
        journal_sha256=journal.sha256,
    )


def _next_provenance_publication_journal(
    session: Session,
    upload: CollectionUploadRecord,
) -> CollectionUploadProvenanceJournalRecord | None:
    if upload.provenance_archive_current_journal_id is not None:
        journal = session.get(
            CollectionUploadProvenanceJournalRecord,
            (upload.collection_id, upload.provenance_archive_current_journal_id),
        )
    else:
        statement = select(CollectionUploadProvenanceJournalRecord).where(
            CollectionUploadProvenanceJournalRecord.collection_id == upload.collection_id
        )
        if upload.provenance_archive_last_journal_id is not None:
            statement = statement.where(
                CollectionUploadProvenanceJournalRecord.journal_id
                > upload.provenance_archive_last_journal_id
            )
        journal = session.scalar(
            statement.order_by(CollectionUploadProvenanceJournalRecord.journal_id).limit(1)
        )
    if journal is not None and journal.state != "sealed":
        raise Conflict(f"provenance journal is not sealed: {journal.journal_id}")
    return journal


def _upload_journal_range_bytes(
    session: Session,
    collection_id: int,
    journal_id: str,
    *,
    offset: int,
    size: int,
) -> bytes:
    if size < 1 or size > PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX:
        raise RuntimeError("provenance journal publication range is invalid")
    rows = session.execute(
        select(
            CollectionUploadProvenanceJournalChunkRecord.byte_offset,
            CollectionUploadProvenanceJournalChunkRecord.content,
        )
        .where(
            CollectionUploadProvenanceJournalChunkRecord.collection_id == collection_id,
            CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
            CollectionUploadProvenanceJournalChunkRecord.byte_offset
            + func.length(CollectionUploadProvenanceJournalChunkRecord.content)
            > offset,
            CollectionUploadProvenanceJournalChunkRecord.byte_offset < offset + size,
        )
        .order_by(CollectionUploadProvenanceJournalChunkRecord.byte_offset)
    )
    content = bytearray()
    expected = offset
    for row in rows:
        raw = bytes(row.content)
        row_offset = int(row.byte_offset)
        start = max(0, expected - row_offset)
        if row_offset > expected or start >= len(raw):
            raise RuntimeError("provenance journal chunks are not contiguous")
        take = min(len(raw) - start, size - len(content))
        content.extend(raw[start : start + take])
        expected += take
        if len(content) == size:
            break
    if len(content) != size:
        raise RuntimeError("provenance journal publication range is unavailable")
    return bytes(content)


def _iter_upload_journal_chunks(
    session: Session,
    collection_id: int,
    journal_id: str,
) -> Iterator[bytes]:
    statement = (
        select(CollectionUploadProvenanceJournalChunkRecord.content)
        .where(
            CollectionUploadProvenanceJournalChunkRecord.collection_id == collection_id,
            CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
        )
        .order_by(CollectionUploadProvenanceJournalChunkRecord.ordinal)
        .execution_options(yield_per=16)
    )
    yield from session.scalars(statement)


def _iter_upload_journal_ids(session: Session, collection_id: int) -> Iterator[str]:
    after: str | None = None
    while True:
        statement = select(CollectionUploadProvenanceJournalRecord.journal_id).where(
            CollectionUploadProvenanceJournalRecord.collection_id == collection_id
        )
        if after is not None:
            statement = statement.where(CollectionUploadProvenanceJournalRecord.journal_id > after)
        batch = list(
            session.scalars(
                statement.order_by(CollectionUploadProvenanceJournalRecord.journal_id).limit(100)
            )
        )
        if not batch:
            return
        yield from batch
        after = batch[-1]


def _upload_journal_bytes(
    session: Session,
    collection_id: int,
    journal_id: str,
) -> bytes:
    return b"".join(_iter_upload_journal_chunks(session, collection_id, journal_id))


def _final_provenance_mode(
    session: Session,
    collection_id: int,
    upload_provenance_mode: str,
) -> str:
    if upload_provenance_mode == "omitted":
        return "omitted"
    has_omission = session.scalar(
        select(
            exists().where(
                CollectionUploadFileRecord.collection_id == collection_id,
                CollectionUploadFileRecord.provenance_status == "omitted",
            )
        )
    )
    return "mixed" if has_omission else "captured"


_DERIVATIVE_SOURCE_BATCH = 128
_DERIVATIVE_REFERENCE_BATCH = 64
_DERIVATIVE_JOURNAL_NAMESPACE = uuid.UUID("6c096a7c-8215-4c4d-9db0-22e11ca791ad")


def _derivative_claim(
    session: Session,
    upload: CollectionUploadRecord,
) -> CollectionProcessingClaimRecord:
    execution_id = upload.initiated_by_app.removeprefix("transform:")
    claim = session.scalar(
        select(CollectionProcessingClaimRecord).where(
            CollectionProcessingClaimRecord.execution_id == execution_id
        )
    )
    if claim is None or claim.plan_sealed_at is None:
        raise Conflict("transform provenance requires its sealed collection-work claim")
    authority = session.get(CollectionProcessingDispositionSetRecord, claim.id)
    if authority is None or authority.state != "sealed":
        raise Conflict("transform provenance requires its sealed disposition authority")
    return claim


def _derivative_cursor(upload: CollectionUploadRecord) -> dict[str, object]:
    value = json.loads(upload.derivative_provenance_cursor_json)
    if not isinstance(value, dict):
        raise RuntimeError("derivative provenance cursor is invalid")
    return value


def _set_derivative_cursor(
    upload: CollectionUploadRecord,
    value: Mapping[str, object],
) -> None:
    upload.derivative_provenance_cursor_json = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":")
    )


def _advance_derivative_source_discovery(
    session: Session,
    upload: CollectionUploadRecord,
) -> None:
    claim = _derivative_claim(session, upload)
    cursor = _derivative_cursor(upload)
    after_output = cursor.get("output_path")
    after_collection = cursor.get("input_collection_id")
    after_path = cursor.get("input_path")
    statement = select(
        CollectionProcessingDispositionOutputRecord.output_path,
        CollectionProcessingDispositionOutputRecord.input_collection_id,
        CollectionProcessingDispositionOutputRecord.input_path,
    ).where(CollectionProcessingDispositionOutputRecord.claim_id == claim.id)
    if (
        isinstance(after_output, str)
        and isinstance(after_collection, int)
        and isinstance(after_path, str)
    ):
        statement = statement.where(
            or_(
                CollectionProcessingDispositionOutputRecord.output_path > after_output,
                (CollectionProcessingDispositionOutputRecord.output_path == after_output)
                & (
                    CollectionProcessingDispositionOutputRecord.input_collection_id
                    > after_collection
                ),
                (CollectionProcessingDispositionOutputRecord.output_path == after_output)
                & (
                    CollectionProcessingDispositionOutputRecord.input_collection_id
                    == after_collection
                )
                & (CollectionProcessingDispositionOutputRecord.input_path > after_path),
            )
        )
    rows = list(
        session.execute(
            statement.order_by(
                CollectionProcessingDispositionOutputRecord.output_path,
                CollectionProcessingDispositionOutputRecord.input_collection_id,
                CollectionProcessingDispositionOutputRecord.input_path,
            ).limit(_DERIVATIVE_SOURCE_BATCH)
        )
    )
    if not rows:
        upload.derivative_provenance_state = "copying"
        _set_derivative_cursor(upload, {})
        return
    for output_path, source_collection_id, source_path in rows:
        if session.get(CollectionUploadFileRecord, (upload.collection_id, output_path)) is None:
            continue
        binding = session.get(
            CollectionFileProvenanceRecord,
            (source_collection_id, source_path),
        )
        if binding is None:
            raise Conflict("transform source provenance binding is unavailable")
        if binding.status == "captured":
            assert binding.journal_id is not None
            key = (upload.collection_id, source_collection_id, binding.journal_id)
            if session.get(CollectionUploadProvenanceSourceRecord, key) is None:
                session.add(
                    CollectionUploadProvenanceSourceRecord(
                        collection_id=upload.collection_id,
                        source_collection_id=source_collection_id,
                        journal_id=binding.journal_id,
                    )
                )
    last = rows[-1]
    _set_derivative_cursor(
        upload,
        {
            "output_path": str(last.output_path),
            "input_collection_id": int(last.input_collection_id),
            "input_path": str(last.input_path),
        },
    )


def _advance_derivative_source_closure(
    session: Session,
    upload: CollectionUploadRecord,
) -> None:
    source = session.scalar(
        select(CollectionUploadProvenanceSourceRecord)
        .where(
            CollectionUploadProvenanceSourceRecord.collection_id == upload.collection_id,
            CollectionUploadProvenanceSourceRecord.expanded.is_(False),
        )
        .order_by(
            CollectionUploadProvenanceSourceRecord.source_collection_id,
            CollectionUploadProvenanceSourceRecord.journal_id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if source is not None:
        statement = select(CollectionProvenanceExternalStateReferenceRecord).where(
            CollectionProvenanceExternalStateReferenceRecord.collection_id
            == source.source_collection_id,
            CollectionProvenanceExternalStateReferenceRecord.from_journal_id == source.journal_id,
        )
        if (
            source.after_to_journal_id is not None
            and source.after_entry_id is not None
            and source.after_state_id is not None
        ):
            statement = statement.where(
                or_(
                    CollectionProvenanceExternalStateReferenceRecord.to_journal_id
                    > source.after_to_journal_id,
                    (
                        CollectionProvenanceExternalStateReferenceRecord.to_journal_id
                        == source.after_to_journal_id
                    )
                    & (
                        CollectionProvenanceExternalStateReferenceRecord.entry_id
                        > source.after_entry_id
                    ),
                    (
                        CollectionProvenanceExternalStateReferenceRecord.to_journal_id
                        == source.after_to_journal_id
                    )
                    & (
                        CollectionProvenanceExternalStateReferenceRecord.entry_id
                        == source.after_entry_id
                    )
                    & (
                        CollectionProvenanceExternalStateReferenceRecord.state_id
                        > source.after_state_id
                    ),
                )
            )
        references = list(
            session.scalars(
                statement.order_by(
                    CollectionProvenanceExternalStateReferenceRecord.to_journal_id,
                    CollectionProvenanceExternalStateReferenceRecord.entry_id,
                    CollectionProvenanceExternalStateReferenceRecord.state_id,
                ).limit(_DERIVATIVE_SOURCE_BATCH + 1)
            )
        )
        for reference in references[:_DERIVATIVE_SOURCE_BATCH]:
            key = (
                upload.collection_id,
                source.source_collection_id,
                reference.to_journal_id,
            )
            if session.get(CollectionUploadProvenanceSourceRecord, key) is None:
                session.add(
                    CollectionUploadProvenanceSourceRecord(
                        collection_id=upload.collection_id,
                        source_collection_id=source.source_collection_id,
                        journal_id=reference.to_journal_id,
                    )
                )
            source.after_to_journal_id = reference.to_journal_id
            source.after_entry_id = reference.entry_id
            source.after_state_id = reference.state_id
        if len(references) <= _DERIVATIVE_SOURCE_BATCH:
            source.expanded = True
        return

    source = session.scalar(
        select(CollectionUploadProvenanceSourceRecord)
        .where(
            CollectionUploadProvenanceSourceRecord.collection_id == upload.collection_id,
            CollectionUploadProvenanceSourceRecord.copied.is_(False),
        )
        .order_by(
            CollectionUploadProvenanceSourceRecord.source_collection_id,
            CollectionUploadProvenanceSourceRecord.journal_id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if source is None:
        upload.derivative_provenance_state = "generating"
        _set_derivative_cursor(upload, {})
        return
    authoritative = session.get(
        CollectionProvenanceJournalRecord,
        (source.source_collection_id, source.journal_id),
    )
    if authoritative is None:
        raise Conflict("transform source provenance journal is unavailable")
    staged = session.get(
        CollectionUploadProvenanceJournalRecord,
        (upload.collection_id, source.journal_id),
    )
    if staged is None:
        staged = CollectionUploadProvenanceJournalRecord(
            collection_id=upload.collection_id,
            journal_id=source.journal_id,
            bytes=authoritative.bytes,
            sha256=authoritative.sha256,
            state="accepting",
            accepted_bytes=0,
            content_hash_state=CheckpointSHA256().export_state(),
        )
        session.add(staged)
        session.flush()
    elif staged.generated_output_path is not None or (
        staged.bytes != authoritative.bytes or staged.sha256 != authoritative.sha256
    ):
        raise Conflict("source provenance journal identity collides in derivative closure")
    if staged.state == "failed":
        raise Conflict("copied source provenance journal validation failed")
    if staged.state == "accepting":
        chunk = session.scalar(
            select(CollectionProvenanceJournalChunkRecord)
            .where(
                CollectionProvenanceJournalChunkRecord.collection_id == source.source_collection_id,
                CollectionProvenanceJournalChunkRecord.journal_id == source.journal_id,
                CollectionProvenanceJournalChunkRecord.byte_offset == source.copy_offset,
            )
            .order_by(CollectionProvenanceJournalChunkRecord.ordinal)
            .limit(1)
        )
        if chunk is None:
            raise Conflict("source provenance journal bytes are unavailable")
        content = bytes(chunk.content)
        digest = CheckpointSHA256.from_state(staged.content_hash_state)
        digest.update(content)
        next_ordinal = int(
            session.scalar(
                select(func.count())
                .select_from(CollectionUploadProvenanceJournalChunkRecord)
                .where(
                    CollectionUploadProvenanceJournalChunkRecord.collection_id
                    == upload.collection_id,
                    CollectionUploadProvenanceJournalChunkRecord.journal_id == source.journal_id,
                )
            )
            or 0
        )
        session.add(
            CollectionUploadProvenanceJournalChunkRecord(
                collection_id=upload.collection_id,
                journal_id=source.journal_id,
                ordinal=next_ordinal,
                byte_offset=source.copy_offset,
                content=content,
            )
        )
        source.copy_offset += len(content)
        staged.accepted_bytes = source.copy_offset
        staged.content_hash_state = digest.export_state()
        if source.copy_offset == staged.bytes:
            if digest.hexdigest() != staged.sha256:
                raise Conflict("copied source provenance journal digest changed")
            staged.state = "validating"
        elif source.copy_offset > staged.bytes:
            raise Conflict("copied source provenance journal exceeds its authority")
        return
    if staged.state == "validating":
        _validate_next_upload_journal_entry(session, staged)
        return
    if staged.state != "sealed" or (
        staged.current_state_id != authoritative.current_state_id
        or staged.current_entry_id != authoritative.current_entry_id
        or staged.current_entry_json_sha256 != authoritative.current_entry_json_sha256
        or staged.current_path != authoritative.current_path
        or staged.current_bytes != authoritative.current_bytes
        or staged.current_sha256 != authoritative.current_sha256
    ):
        raise Conflict("copied source provenance journal projection changed")
    source.copied = True


def _derivative_reference_rows(
    session: Session,
    *,
    claim_id: str,
    output_path: str,
    after_journal_id: str | None,
    after_state_id: str | None,
    limit: int,
) -> list[Any]:
    statement = (
        select(
            CollectionProvenanceJournalRecord.journal_id,
            CollectionProvenanceJournalRecord.current_entry_id,
            CollectionProvenanceJournalRecord.current_entry_json_sha256,
            CollectionProvenanceJournalRecord.current_state_id,
        )
        .join(
            CollectionFileProvenanceRecord,
            (
                CollectionFileProvenanceRecord.collection_id
                == CollectionProvenanceJournalRecord.collection_id
            )
            & (
                CollectionFileProvenanceRecord.journal_id
                == CollectionProvenanceJournalRecord.journal_id
            ),
        )
        .join(
            CollectionProcessingDispositionOutputRecord,
            (
                CollectionProcessingDispositionOutputRecord.input_collection_id
                == CollectionFileProvenanceRecord.collection_id
            )
            & (
                CollectionProcessingDispositionOutputRecord.input_path
                == CollectionFileProvenanceRecord.path
            ),
        )
        .where(
            CollectionProcessingDispositionOutputRecord.claim_id == claim_id,
            CollectionProcessingDispositionOutputRecord.output_path == output_path,
            CollectionFileProvenanceRecord.status == "captured",
        )
        .distinct()
    )
    if after_journal_id is not None and after_state_id is not None:
        statement = statement.where(
            or_(
                CollectionProvenanceJournalRecord.journal_id > after_journal_id,
                (CollectionProvenanceJournalRecord.journal_id == after_journal_id)
                & (CollectionProvenanceJournalRecord.current_state_id > after_state_id),
            )
        )
    return list(
        session.execute(
            statement.order_by(
                CollectionProvenanceJournalRecord.journal_id,
                CollectionProvenanceJournalRecord.current_state_id,
            ).limit(limit)
        )
    )


def _derivative_journal_id(execution_id: str, output_path: str) -> str:
    return f"urn:uuid:{uuid.uuid5(_DERIVATIVE_JOURNAL_NAMESPACE, f'{execution_id}:{output_path}')}"


def _derivative_seed(
    upload: CollectionUploadRecord,
    claim: CollectionProcessingClaimRecord,
    file: CollectionUploadFileRecord,
) -> tuple[bytes, DerivativeJournalSeed]:
    assert claim.operation_id is not None
    return create_derivative_journal_seed(
        relative_path=file.path,
        byte_count=file.bytes,
        sha256=file.sha256,
        agent_name="riverhog",
        agent_version="v1",
        event_label=claim.operation_id,
        started_at=upload.opened_at,
        ended_at=upload.closed_at or upload.last_activity_at,
        journal_id=_derivative_journal_id(cast(str, claim.execution_id), file.path),
    )


def _advance_derivative_output_journal(
    session: Session,
    upload: CollectionUploadRecord,
) -> None:
    claim = _derivative_claim(session, upload)
    file = session.scalar(
        select(CollectionUploadFileRecord)
        .where(
            CollectionUploadFileRecord.collection_id == upload.collection_id,
            CollectionUploadFileRecord.provenance_status == "deriving",
        )
        .order_by(CollectionUploadFileRecord.file_order)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if file is None:
        upload.derivative_provenance_state = "complete"
        _set_derivative_cursor(upload, {})
        return
    journal_id = _derivative_journal_id(cast(str, claim.execution_id), file.path)
    journal = session.get(
        CollectionUploadProvenanceJournalRecord,
        (upload.collection_id, journal_id),
    )
    if journal is None:
        first = _derivative_reference_rows(
            session,
            claim_id=claim.id,
            output_path=file.path,
            after_journal_id=None,
            after_state_id=None,
            limit=1,
        )
        if not first:
            file.provenance_status = "omitted"
            file.provenance_omission_reason = (
                "No contributing source artifact carried captured provenance."
            )
            return
        content, _seed = _derivative_seed(upload, claim, file)
        digest = CheckpointSHA256()
        digest.update(content)
        journal = CollectionUploadProvenanceJournalRecord(
            collection_id=upload.collection_id,
            journal_id=journal_id,
            bytes=len(content),
            sha256="0" * 64,
            state="generating",
            accepted_bytes=len(content),
            content_hash_state=digest.export_state(),
            generated_output_path=file.path,
        )
        session.add(journal)
        session.add(
            CollectionUploadProvenanceJournalChunkRecord(
                collection_id=upload.collection_id,
                journal_id=journal_id,
                ordinal=0,
                byte_offset=0,
                content=content,
            )
        )
        session.flush()
        _validate_next_upload_journal_entry(session, journal)
        return
    if journal.generated_output_path != file.path:
        raise Conflict("generated provenance journal binds another output artifact")
    if journal.state == "failed":
        raise Conflict("generated derivative provenance journal validation failed")
    if journal.state == "generating" and journal.validation_byte_offset < journal.bytes:
        _validate_next_upload_journal_entry(session, journal)
        return
    if journal.state == "generating":
        rows = _derivative_reference_rows(
            session,
            claim_id=claim.id,
            output_path=file.path,
            after_journal_id=journal.generation_after_journal_id,
            after_state_id=journal.generation_after_state_id,
            limit=_DERIVATIVE_REFERENCE_BATCH,
        )
        if rows:
            _prefix, seed = _derivative_seed(upload, claim, file)
            references = tuple(
                ExternalStateReference(
                    journal_id=str(row.journal_id),
                    entry_id=str(row.current_entry_id),
                    entry_json_sha256=str(row.current_entry_json_sha256),
                    state_id=str(row.current_state_id),
                )
                for row in rows
            )
            if journal.validation_previous_entry_id is None or (
                journal.validation_previous_json_sha256 is None
            ):
                raise RuntimeError("generated provenance predecessor is unavailable")
            content = create_derivative_source_entry(
                seed=seed,
                references=references,
                sequence=int(journal.validation_sequence),
                previous_entry_id=journal.validation_previous_entry_id,
                previous_entry_json_sha256=journal.validation_previous_json_sha256,
                recorded_at=upload.closed_at or upload.last_activity_at,
            )
            digest = CheckpointSHA256.from_state(journal.content_hash_state)
            digest.update(content)
            ordinal = int(
                session.scalar(
                    select(func.count())
                    .select_from(CollectionUploadProvenanceJournalChunkRecord)
                    .where(
                        CollectionUploadProvenanceJournalChunkRecord.collection_id
                        == upload.collection_id,
                        CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
                    )
                )
                or 0
            )
            session.add(
                CollectionUploadProvenanceJournalChunkRecord(
                    collection_id=upload.collection_id,
                    journal_id=journal_id,
                    ordinal=ordinal,
                    byte_offset=journal.bytes,
                    content=content,
                )
            )
            journal.bytes += len(content)
            journal.accepted_bytes = journal.bytes
            journal.content_hash_state = digest.export_state()
            last = rows[-1]
            journal.generation_after_journal_id = str(last.journal_id)
            journal.generation_after_state_id = str(last.current_state_id)
            return
        digest = CheckpointSHA256.from_state(journal.content_hash_state)
        journal.sha256 = digest.hexdigest()
        journal.state = "validating"
        _validate_next_upload_journal_entry(session, journal)
    if journal.state == "sealed":
        if (
            journal.current_path != file.path
            or journal.current_bytes != file.bytes
            or journal.current_sha256 != file.sha256
            or journal.current_state_id is None
        ):
            raise Conflict("generated derivative provenance changed its output identity")
        file.provenance_status = "captured"
        file.provenance_journal_id = journal.journal_id
        file.provenance_current_state_id = journal.current_state_id
        file.provenance_omission_reason = None


def _planner_checkpoint(upload: CollectionUploadRecord) -> Any:
    if not upload.planner_checkpoint_json:
        raise RuntimeError("collection upload planner checkpoint is missing")
    return parse_incremental_volume_planner_checkpoint(upload.planner_checkpoint_json)


def _persist_plan_batch(session: Session, *, upload: CollectionUploadRecord, batch: Any) -> None:
    if not upload.archive_storage_prefix:
        raise RuntimeError("collection archive storage prefix is missing")
    now = utc_timestamp_now()
    for plan in batch.packs:
        plan_json = pack_volume_plan_bytes(plan).decode("utf-8")
        relative = f"volumes/{plan.volume_id}.tar.age"
        upload.archive_objects.append(
            CollectionArchiveObjectUploadRecord(
                collection_id=upload.collection_id,
                object_id=plan.volume_id,
                sequence=plan.sequence,
                kind="pack",
                relative_path=relative,
                object_path=f"{upload.archive_storage_prefix}/{relative}",
                plaintext_bytes=plan.plaintext_bytes,
                source_bytes=sum(current.bytes for current in plan.members),
                unit_plaintext_bytes=plan.part_plaintext_bytes,
                plan_json=plan_json,
                plan_sha256=plan.plan_sha256,
                state="planned",
                uploaded_bytes=0,
                uploaded_units=0,
                total_units=len(plan.units),
                updated_at=now,
            )
        )
    for plan in batch.raw_volumes:
        plan_json = raw_volume_plan_bytes(plan).decode("utf-8")
        relative = f"volumes/{plan.volume_id}.bin.age"
        upload.archive_objects.append(
            CollectionArchiveObjectUploadRecord(
                collection_id=upload.collection_id,
                object_id=plan.volume_id,
                sequence=plan.sequence,
                kind="segment",
                relative_path=relative,
                object_path=f"{upload.archive_storage_prefix}/{relative}",
                plaintext_bytes=plan.plaintext_bytes,
                source_bytes=plan.plaintext_bytes,
                source_path=plan.source_path,
                source_first_part=(
                    plan.file_offset // batch.checkpoint.policy.raw_part_plaintext_bytes
                ),
                source_part_count=max(
                    1,
                    (plan.plaintext_bytes + batch.checkpoint.policy.raw_part_plaintext_bytes - 1)
                    // batch.checkpoint.policy.raw_part_plaintext_bytes,
                ),
                unit_plaintext_bytes=batch.checkpoint.policy.raw_part_plaintext_bytes,
                plan_json=plan_json,
                plan_sha256=hashlib.sha256(plan_json.encode()).hexdigest(),
                state="planned",
                uploaded_bytes=0,
                uploaded_units=0,
                total_units=max(
                    1,
                    (plan.plaintext_bytes + batch.checkpoint.policy.raw_part_plaintext_bytes - 1)
                    // batch.checkpoint.policy.raw_part_plaintext_bytes,
                ),
                updated_at=now,
            )
        )
    session.flush()


def _upload_file_batches(
    session: Session,
    collection_id: int,
) -> Iterator[list[Any]]:
    """Read a finalized upload inventory with bounded keyset batches."""

    after = -1
    while True:
        rows = list(
            session.execute(
                select(
                    CollectionUploadFileRecord.file_order,
                    CollectionUploadFileRecord.path,
                    CollectionUploadFileRecord.bytes,
                    CollectionUploadFileRecord.sha256,
                    CollectionUploadFileRecord.provenance_status,
                    CollectionUploadFileRecord.provenance_journal_id,
                    CollectionUploadFileRecord.provenance_current_state_id,
                    CollectionUploadFileRecord.provenance_omission_reason,
                )
                .where(
                    CollectionUploadFileRecord.collection_id == collection_id,
                    CollectionUploadFileRecord.file_order > after,
                )
                .order_by(CollectionUploadFileRecord.file_order)
                .limit(COLLECTION_UPLOAD_FILE_BATCH_MAX)
            )
        )
        if not rows:
            return
        yield rows
        after = int(rows[-1].file_order)


def _upload_archive_object_batches(
    session: Session,
    collection_id: int,
) -> Iterator[list[CollectionArchiveObjectUploadRecord]]:
    """Read planned archive objects in bounded contiguous sequence batches."""

    after = -1
    while True:
        rows = list(
            session.scalars(
                select(CollectionArchiveObjectUploadRecord)
                .where(
                    CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                    CollectionArchiveObjectUploadRecord.sequence > after,
                )
                .order_by(CollectionArchiveObjectUploadRecord.sequence)
                .limit(64)
            )
        )
        if not rows:
            return
        yield rows
        after = int(rows[-1].sequence)


def _upload_provenance_archive_volume_batches(
    session: Session,
    collection_id: int,
) -> Iterator[list[CollectionUploadProvenanceArchiveVolumeRecord]]:
    """Read bounded provenance archive receipts in exact sequence order."""

    after = -1
    while True:
        rows = list(
            session.scalars(
                select(CollectionUploadProvenanceArchiveVolumeRecord)
                .where(
                    CollectionUploadProvenanceArchiveVolumeRecord.collection_id == collection_id,
                    CollectionUploadProvenanceArchiveVolumeRecord.sequence > after,
                )
                .order_by(CollectionUploadProvenanceArchiveVolumeRecord.sequence)
                .limit(64)
            )
        )
        if not rows:
            return
        yield rows
        after = int(rows[-1].sequence)


def _upload_file_path_batches(
    session: Session,
    collection_id: int,
) -> Iterator[list[Any]]:
    """Read an upload inventory in portable UTF-8 path order with bounded keysets."""

    after = b""
    while True:
        rows = list(
            session.execute(
                select(
                    CollectionUploadFileRecord.file_order,
                    CollectionUploadFileRecord.path,
                    CollectionUploadFileRecord.bytes,
                    CollectionUploadFileRecord.sha256,
                    CollectionUploadFileRecord.provenance_status,
                    CollectionUploadFileRecord.provenance_journal_id,
                    CollectionUploadFileRecord.provenance_current_state_id,
                    CollectionUploadFileRecord.provenance_omission_reason,
                    CollectionUploadFileRecord.path_sort_key,
                )
                .where(
                    CollectionUploadFileRecord.collection_id == collection_id,
                    CollectionUploadFileRecord.path_sort_key > after,
                )
                .order_by(CollectionUploadFileRecord.path_sort_key)
                .limit(COLLECTION_UPLOAD_FILE_BATCH_MAX)
            )
        )
        if not rows:
            return
        yield rows
        after = bytes(rows[-1].path_sort_key)


def _iter_planned_file_identities(
    session: Session,
    collection_id: int,
) -> Iterator[tuple[str, int, str]]:
    """Project the persisted volume plan back to its ordered file identities."""

    rows = session.scalars(
        select(CollectionArchiveObjectUploadRecord)
        .where(CollectionArchiveObjectUploadRecord.collection_id == collection_id)
        .order_by(CollectionArchiveObjectUploadRecord.sequence)
    ).yield_per(1)
    raw_path: str | None = None
    raw_bytes = 0
    raw_file_bytes = 0
    raw_sha256 = ""

    def finish_raw() -> tuple[str, int, str] | None:
        nonlocal raw_path, raw_bytes, raw_file_bytes, raw_sha256
        if raw_path is None:
            return None
        if raw_bytes != raw_file_bytes:
            raise ValueError(f"raw volume plan does not cover its file: {raw_path}")
        result = (raw_path, raw_file_bytes, raw_sha256)
        raw_path = None
        raw_bytes = 0
        raw_file_bytes = 0
        raw_sha256 = ""
        return result

    for record in rows:
        if record.kind == "pack":
            completed_raw = finish_raw()
            if completed_raw is not None:
                yield completed_raw
            # Pack layout owns bytewise tar-member ordering.  Upload registration
            # additionally keeps terminal derivation evidence last, so reconstruct
            # that collection-level order within each bounded pack before comparing
            # it with the append-only inventory.
            members = sorted(
                parse_pack_volume_plan(record.plan_json).members,
                key=lambda member: collection_upload_path_order_key(member.path),
            )
            for member in members:
                yield member.path, member.bytes, member.sha256
            continue
        plan = parse_raw_volume_plan(record.plan_json)
        if raw_path != plan.source_path:
            completed_raw = finish_raw()
            if completed_raw is not None:
                yield completed_raw
            if plan.file_offset != 0:
                raise ValueError("raw volume plan does not begin at file offset zero")
            raw_path = plan.source_path
            raw_file_bytes = plan.file_bytes
            raw_sha256 = plan.file_sha256
        elif (
            plan.file_offset != raw_bytes
            or plan.file_bytes != raw_file_bytes
            or plan.file_sha256 != raw_sha256
        ):
            raise ValueError(f"raw volume plan is not contiguous: {plan.source_path}")
        raw_bytes += plan.plaintext_bytes
    completed_raw = finish_raw()
    if completed_raw is not None:
        yield completed_raw


def _upload_tag_count(session: Session, collection_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(CollectionUploadTagRecord)
            .where(CollectionUploadTagRecord.collection_id == collection_id)
        )
        or 0
    )


def _upload_tag_set_identity(session: Session, collection_id: int) -> str:
    tags = session.scalars(
        select(CollectionUploadTagRecord.tag_id)
        .where(CollectionUploadTagRecord.collection_id == collection_id)
        .order_by(CollectionUploadTagRecord.tag_id)
    ).yield_per(100)
    return tag_set_identity(tags)


def _require_upload_tag_access(
    session: Session,
    principal: ApplicationPrincipal,
    collection_id: int,
) -> None:
    resources = permission_resources(principal, COLLECTIONS_CREATE)
    if ALL_RESOURCES in resources:
        return
    allowed_tags = tag_ids(resources)
    if not allowed_tags:
        raise NotFound("collection tags are not available")
    unauthorized = session.scalar(
        select(CollectionUploadTagRecord.tag_id)
        .where(CollectionUploadTagRecord.collection_id == collection_id)
        .where(CollectionUploadTagRecord.tag_id.not_in(allowed_tags))
        .limit(1)
    )
    if unauthorized is not None:
        raise NotFound("collection tags are not available")


def _require_collection_tag_create_access(
    session: Session,
    principal: ApplicationPrincipal,
    collection_id: int,
) -> None:
    resources = permission_resources(principal, COLLECTIONS_CREATE)
    if ALL_RESOURCES in resources:
        return
    allowed_tags = tag_ids(resources)
    if not allowed_tags:
        raise NotFound("collection tags are not available")
    unauthorized = session.scalar(
        select(CollectionTagRecord.tag_id)
        .where(CollectionTagRecord.collection_id == collection_id)
        .where(CollectionTagRecord.tag_id.not_in(allowed_tags))
        .limit(1)
    )
    if unauthorized is not None:
        raise NotFound("collection tags are not available")


def _ready_for_finalization(session: Session, upload: CollectionUploadRecord) -> bool:
    checkpoint = _planner_checkpoint(upload)
    sealed = int(
        session.scalar(
            select(func.count(CollectionArchiveObjectUploadRecord.object_id)).where(
                CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id,
                CollectionArchiveObjectUploadRecord.state == "sealed",
            )
        )
        or 0
    )
    return bool(
        checkpoint.closed
        and checkpoint.next_sequence > 0
        and sealed == checkpoint.next_sequence
        and _has_complete_artifact_custody(upload)
    )


def _has_complete_artifact_custody(upload: CollectionUploadRecord) -> bool:
    return bool(
        upload.file_count > 0
        and upload.custodied_file_count == upload.file_count
        and upload.custodied_file_bytes == upload.file_bytes
    )


def _mark_finalization_ready(upload: CollectionUploadRecord, *, now: str) -> None:
    upload.state = "finalizing"
    upload.lease_expires_at = None
    upload.archive_phase = "finalization_queued"
    upload.archive_phase_updated_at = now
    upload.archive_next_attempt_at = now
    upload.archive_failure = None


def _registration_constraints_payload(policy: CollectionVolumePolicy) -> dict[str, int]:
    return {
        "pack_member_bytes": policy.pack_member_bytes,
        "raw_part_plaintext_bytes": policy.raw_part_plaintext_bytes,
    }


def _file_payload(record: CollectionUploadFileRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "provenance": (
            {
                "status": "captured",
                "journal_id": record.provenance_journal_id,
                "current_state_id": record.provenance_current_state_id,
            }
            if record.provenance_status == "captured"
            else None
            if record.provenance_status == "deriving"
            else {
                "status": "omitted",
                "omission_reason": record.provenance_omission_reason,
            }
        ),
        "custody_receipt": (
            CollectionUploadArtifactCustodyReceiptDocument.model_validate_json(
                record.custody_receipt_json
            )
            if record.custody_receipt_json is not None
            else None
        ),
    }


def _raw_digest_progress(record: CollectionUploadFileRecord) -> dict[str, object]:
    if record.raw_part_count is None:
        raise TypeError("raw digest progress requires a raw upload file")
    accepted = int(record.raw_parts_accepted)
    expected = int(record.raw_part_count)
    return {
        "path": record.path,
        "accepted_parts": accepted,
        "expected_parts": expected,
        "complete": accepted == expected,
    }


def _journal_payload(
    record: CollectionUploadProvenanceJournalRecord,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "journal_id": record.journal_id,
        "state": record.state,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "accepted_bytes": record.accepted_bytes,
        "failure": record.failure,
    }
    if record.state == "sealed":
        payload.update(
            {
                "current_state_id": record.current_state_id,
                "current_path": record.current_path,
                "current_bytes": record.current_bytes,
                "current_sha256": record.current_sha256,
            }
        )
    return CollectionUploadProvenanceJournalStatusDocument.model_validate(payload).model_dump(
        mode="json"
    )


def _validate_next_upload_journal_entry(
    session: Session,
    record: CollectionUploadProvenanceJournalRecord,
) -> None:
    if record.validation_byte_offset == record.bytes:
        if record.state == "validating":
            _seal_validated_upload_journal(session, record)
        return
    encoded = _next_upload_journal_entry_bytes(session, record)
    projected = validate_incremental_journal_entry(
        encoded,
        sequence=record.validation_sequence,
        journal_id=record.journal_id,
        previous_entry_id=record.validation_previous_entry_id,
        previous_json_sha256=record.validation_previous_json_sha256,
    )
    entry_id = str(projected.frame.document["id"])
    _insert_validation_fact(
        session,
        record,
        kind="entry",
        key=entry_id,
        value={"sequence": projected.frame.sequence, "sha256": projected.frame.sha256},
        allow_identical=False,
    )
    if projected.primary_lineage_id is not None:
        if record.primary_lineage_id is not None:
            raise ProvenanceValidationError("journal repeats its initialization policy")
        record.primary_lineage_id = projected.primary_lineage_id
    for agent_id in projected.agents:
        _insert_validation_fact(
            session, record, kind="agent", key=agent_id, value={}, allow_identical=True
        )
    for event_id in projected.events:
        _insert_validation_fact(
            session, record, kind="event", key=event_id, value={}, allow_identical=True
        )
    for state_id, state_json in projected.states:
        _insert_validation_fact(
            session,
            record,
            kind="state",
            key=state_id,
            value=json.loads(state_json),
            allow_identical=True,
        )
    counts = json.loads(record.entity_counts_json)
    if not isinstance(counts, dict):
        raise ProvenanceValidationError("journal entity-count checkpoint is invalid")
    for entity_type, count in projected.entity_counts:
        counts[entity_type] = int(counts.get(entity_type, 0)) + count
    record.entity_counts_json = json.dumps(counts, sort_keys=True, separators=(",", ":"))
    for entity_type, entity_id, document_json in projected.entities:
        _insert_validation_fact(
            session,
            record,
            kind="entity",
            key=f"{entity_type}\x00{entity_id}",
            value={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entry_id": entry_id,
                "document": json.loads(document_json),
            },
            allow_identical=False,
            replace=True,
        )
    session.flush()
    for role, operation, binding_json in projected.bindings:
        binding = json.loads(binding_json)
        existing = session.get(
            CollectionUploadProvenanceValidationFactRecord,
            (record.collection_id, record.journal_id, "binding", role),
        )
        if operation == "unbind":
            if existing is not None:
                session.delete(existing)
            continue
        established_by = binding.get("established_by_capture_id") or binding.get(
            "established_by_activity_id"
        )
        if (
            not isinstance(established_by, str)
            or session.get(
                CollectionUploadProvenanceValidationFactRecord,
                (record.collection_id, record.journal_id, "event", established_by),
            )
            is None
        ):
            raise ProvenanceValidationError(
                "payload binding references an absent capture or activity"
            )
        state_ref = binding.get("state")
        binding_state_id = state_ref.get("id") if isinstance(state_ref, dict) else None
        if (
            not isinstance(state_ref, dict)
            or state_ref.get("scope") != "local"
            or not isinstance(binding_state_id, str)
            or session.get(
                CollectionUploadProvenanceValidationFactRecord,
                (record.collection_id, record.journal_id, "state", binding_state_id),
            )
            is None
        ):
            raise ProvenanceValidationError("payload binding references an absent local state")
        canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"))
        if existing is None:
            session.add(
                CollectionUploadProvenanceValidationFactRecord(
                    collection_id=record.collection_id,
                    journal_id=record.journal_id,
                    kind="binding",
                    fact_key=role,
                    value_json=canonical,
                )
            )
        else:
            existing.value_json = canonical
        session.flush()
    for reference in projected.external_states:
        value = {
            "journal_id": reference.journal_id,
            "entry_id": reference.entry_id,
            "entry_json_sha256": reference.entry_json_sha256,
            "state_id": reference.state_id,
        }
        key = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        _insert_validation_fact(
            session,
            record,
            kind="external-state",
            key=key,
            value=value,
            allow_identical=True,
        )
    record.validation_previous_entry_id = entry_id
    record.validation_previous_json_sha256 = projected.frame.sha256
    record.validation_sequence += 1
    record.validation_byte_offset += len(encoded)
    if record.validation_byte_offset == record.bytes and record.state == "validating":
        _seal_validated_upload_journal(session, record)


def _next_upload_journal_entry_bytes(
    session: Session,
    record: CollectionUploadProvenanceJournalRecord,
) -> bytes:
    offset = int(record.validation_byte_offset)
    rows = session.execute(
        select(
            CollectionUploadProvenanceJournalChunkRecord.byte_offset,
            CollectionUploadProvenanceJournalChunkRecord.content,
        )
        .where(
            CollectionUploadProvenanceJournalChunkRecord.collection_id == record.collection_id,
            CollectionUploadProvenanceJournalChunkRecord.journal_id == record.journal_id,
            CollectionUploadProvenanceJournalChunkRecord.byte_offset
            + func.length(CollectionUploadProvenanceJournalChunkRecord.content)
            > offset,
        )
        .order_by(CollectionUploadProvenanceJournalChunkRecord.byte_offset)
        .limit(
            PROVENANCE_JOURNAL_ENTRY_BYTES_MAX // COLLECTION_UPLOAD_PROVENANCE_APPEND_BYTES_MAX + 2
        )
    )
    if not rows:
        raise ProvenanceValidationError("provenance journal content is unavailable")
    content = bytearray()
    expected_offset = offset
    found_separator = False
    for row in rows:
        row_offset = int(row.byte_offset)
        raw = bytes(row.content)
        start = max(0, expected_offset - row_offset)
        if row_offset > expected_offset or start >= len(raw):
            raise ProvenanceValidationError("provenance journal chunks are not contiguous")
        content.extend(raw[start:])
        expected_offset = row_offset + len(raw)
        separator = content.find(b"\x1e", 1)
        if separator >= 0:
            del content[separator:]
            found_separator = True
            break
        if len(content) > PROVENANCE_JOURNAL_ENTRY_BYTES_MAX:
            raise ProvenanceValidationError("provenance journal entry exceeds its bounded limit")
        if expected_offset >= record.bytes:
            break
    if not content or (expected_offset < record.bytes and not found_separator):
        raise ProvenanceValidationError("provenance journal entry boundary is unavailable")
    if len(content) > PROVENANCE_JOURNAL_ENTRY_BYTES_MAX:
        raise ProvenanceValidationError("provenance journal entry exceeds its bounded limit")
    return bytes(content)


def _insert_validation_fact(
    session: Session,
    record: CollectionUploadProvenanceJournalRecord,
    *,
    kind: str,
    key: str,
    value: object,
    allow_identical: bool,
    replace: bool = False,
) -> None:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    existing = session.get(
        CollectionUploadProvenanceValidationFactRecord,
        (record.collection_id, record.journal_id, kind, key),
    )
    if existing is not None:
        if replace:
            existing.value_json = canonical
            return
        if allow_identical and existing.value_json == canonical:
            return
        raise ProvenanceValidationError(f"journal repeats or redefines {kind} identity {key}")
    session.add(
        CollectionUploadProvenanceValidationFactRecord(
            collection_id=record.collection_id,
            journal_id=record.journal_id,
            kind=kind,
            fact_key=key,
            value_json=canonical,
        )
    )


def _seal_validated_upload_journal(
    session: Session,
    record: CollectionUploadProvenanceJournalRecord,
) -> None:
    if record.validation_sequence < 1 or record.primary_lineage_id is None:
        raise ProvenanceValidationError("journal must contain at least one entry")
    binding = session.get(
        CollectionUploadProvenanceValidationFactRecord,
        (
            record.collection_id,
            record.journal_id,
            "binding",
            "co_resident_primary_payload",
        ),
    )
    if binding is None:
        raise ProvenanceValidationError("journal has no current primary payload binding")
    binding_value = json.loads(binding.value_json)
    state_ref = binding_value.get("state") if isinstance(binding_value, dict) else None
    state_id = state_ref.get("id") if isinstance(state_ref, dict) else None
    state = (
        session.get(
            CollectionUploadProvenanceValidationFactRecord,
            (record.collection_id, record.journal_id, "state", state_id),
        )
        if isinstance(state_id, str)
        else None
    )
    if state is None:
        raise ProvenanceValidationError("current primary payload state is not asserted")
    state_entity = session.get(
        CollectionUploadProvenanceValidationFactRecord,
        (
            record.collection_id,
            record.journal_id,
            "entity",
            f"states\x00{state_id}",
        ),
    )
    state_entity_value = json.loads(state_entity.value_json) if state_entity is not None else None
    current_entry_id = (
        state_entity_value.get("entry_id") if isinstance(state_entity_value, dict) else None
    )
    entry = (
        session.get(
            CollectionUploadProvenanceValidationFactRecord,
            (record.collection_id, record.journal_id, "entry", current_entry_id),
        )
        if isinstance(current_entry_id, str)
        else None
    )
    entry_value = json.loads(entry.value_json) if entry is not None else None
    current_entry_json_sha256 = entry_value.get("sha256") if isinstance(entry_value, dict) else None
    if not isinstance(current_entry_id, str) or not isinstance(current_entry_json_sha256, str):
        raise ProvenanceValidationError("current primary state has no exact asserting entry")
    current_state_id, current_path, current_bytes, current_sha256 = (
        resolve_incremental_journal_current_state(
            primary_lineage_id=record.primary_lineage_id,
            binding_json=binding.value_json,
            state_json=state.value_json,
        )
    )
    record.current_state_id = current_state_id
    record.current_entry_id = current_entry_id
    record.current_entry_json_sha256 = current_entry_json_sha256
    record.current_path = current_path
    record.current_bytes = current_bytes
    record.current_sha256 = current_sha256
    record.state = "sealed"
    record.failure = None


def _volume_summary(plan: PackVolumePlan | RawVolumePlan) -> dict[str, object]:
    return {
        "volume_id": plan.volume_id,
        "sequence": plan.sequence,
        "kind": "pack" if isinstance(plan, PackVolumePlan) else "segment",
    }


def _part_payload(part: StoredArchivePart) -> dict[str, object]:
    return {
        "number": part.number,
        "plaintext_start": part.plaintext_start,
        "plaintext_bytes": part.plaintext_bytes,
        "plaintext_sha256": part.plaintext_sha256,
        "stored_bytes": part.stored_bytes,
        "stored_sha256": part.stored_sha256,
    }


def _unit_states(record: CollectionArchiveObjectUploadRecord) -> set[int]:
    if not record.checkpoint_json:
        return set()
    checkpoint = (
        PackUploadCheckpoint.from_json(record.checkpoint_json)
        if record.kind == "pack"
        else RawUploadCheckpoint.from_json(record.checkpoint_json)
    )
    return {current.number - 1 for current in checkpoint.archive_parts}


def _volume_work_payload(record: CollectionArchiveObjectUploadRecord) -> dict[str, object]:
    committed = _unit_states(record)
    if record.kind == "pack":
        pack_plan = parse_pack_volume_plan(record.plan_json)
        units = [
            {
                "unit": current.unit,
                "payload_bytes": current.payload_bytes,
                "plaintext_bytes": current.plaintext_bytes,
                "sources": [
                    {
                        "path": source.path,
                        "offset": 0,
                        "bytes": source.bytes,
                        "artifact_sha256": source.sha256,
                    }
                    for source in current.sources
                ],
                "state": "committed" if current.unit in committed else "pending",
            }
            for current in pack_unit_descriptors(pack_plan)
        ]
    else:
        raw_plan = parse_raw_volume_plan(record.plan_json)
        raw_part_bytes = record.unit_plaintext_bytes
        units = []
        for unit in range(record.total_units):
            byte_count = min(
                raw_part_bytes,
                raw_plan.plaintext_bytes - unit * raw_part_bytes,
            )
            units.append(
                {
                    "unit": unit,
                    "payload_bytes": byte_count,
                    "plaintext_bytes": byte_count,
                    "sources": [
                        {
                            "path": raw_plan.source_path,
                            "offset": raw_plan.file_offset + unit * raw_part_bytes,
                            "bytes": byte_count,
                            "artifact_sha256": raw_plan.file_sha256,
                        }
                    ],
                    "state": "committed" if unit in committed else "pending",
                }
            )
    return {
        "volume_id": record.object_id,
        "sequence": record.sequence,
        "kind": record.kind,
        "state": record.state,
        "plan_sha256": record.plan_sha256,
        "plaintext_bytes": record.plaintext_bytes,
        "source_bytes": record.source_bytes,
        "units": units,
    }


def _unit_work_payload(
    record: CollectionArchiveObjectUploadRecord,
    unit: int,
) -> dict[str, object]:
    committed = unit < record.uploaded_units or record.state == "sealed"
    if record.kind == "pack":
        descriptors = pack_unit_descriptors(parse_pack_volume_plan(record.plan_json))
        if unit < 0 or unit >= len(descriptors):
            raise NotFound(f"collection upload unit not found: {unit}")
        current = descriptors[unit]
        return {
            "unit": current.unit,
            "payload_bytes": current.payload_bytes,
            "plaintext_bytes": current.plaintext_bytes,
            "sources": [
                {
                    "path": source.path,
                    "offset": 0,
                    "bytes": source.bytes,
                    "artifact_sha256": source.sha256,
                }
                for source in current.sources
            ],
            "state": "committed" if committed else "pending",
        }
    if record.kind == "segment":
        plan = parse_raw_volume_plan(record.plan_json)
        if unit < 0 or unit >= record.total_units:
            raise NotFound(f"collection upload unit not found: {unit}")
        byte_count = min(
            record.unit_plaintext_bytes,
            plan.plaintext_bytes - unit * record.unit_plaintext_bytes,
        )
        return {
            "unit": unit,
            "payload_bytes": byte_count,
            "plaintext_bytes": byte_count,
            "sources": [
                {
                    "path": plan.source_path,
                    "offset": plan.file_offset + unit * record.unit_plaintext_bytes,
                    "bytes": byte_count,
                    "artifact_sha256": plan.file_sha256,
                }
            ],
            "state": "committed" if committed else "pending",
        }
    raise RuntimeError(f"unsupported archive volume kind: {record.kind}")


def _unit_assignment_payload(record: CollectionArchiveObjectUploadRecord) -> dict[str, object]:
    if record.uploaded_units >= record.total_units:
        raise RuntimeError("unsealed archive volume has no actionable upload unit")
    return {
        "volume": {
            "volume_id": record.object_id,
            "sequence": record.sequence,
            "kind": record.kind,
        },
        "plan_sha256": record.plan_sha256,
        "unit": _unit_work_payload(record, record.uploaded_units),
    }


def _sealed_volume_json(receipt: SealedPackVolume | SealedRawVolume) -> str:
    common: dict[str, object] = {
        "volume_id": receipt.volume_id,
        "sequence": receipt.sequence,
        "relative_path": receipt.relative_path,
        "plaintext_bytes": receipt.plaintext_bytes,
        "age_state": json.loads(receipt.age_state_json),
        "parts": [_part_payload(current) for current in receipt.parts],
        "revision": receipt.revision,
        "completed_at": receipt.completed_at,
        "retrieval_cache": retrieval_cache_receipt_payload(receipt.retrieval_cache),
    }
    if isinstance(receipt, SealedPackVolume):
        common.update(
            {
                "kind": "pack",
                "files": receipt.files,
                "source_bytes": receipt.source_bytes,
                "index_sha256": receipt.index_sha256,
                "plan_sha256": receipt.plan_sha256,
            }
        )
    else:
        common.update(
            {
                "kind": "segment",
                "source_path": receipt.source_path,
                "file_offset": receipt.file_offset,
                "file_bytes": receipt.file_bytes,
                "file_sha256": receipt.file_sha256,
            }
        )
    return json.dumps(common, sort_keys=True, separators=(",", ":"))


def _archive_volume_metadata_receipt_json(
    receipt: SealedArchiveVolumeMetadata,
) -> str:
    return json.dumps(
        {
            "sequence": receipt.sequence,
            "object_path": receipt.object_path,
            "relative_path": receipt.relative_path,
            "revision": receipt.revision,
            "plaintext_bytes": receipt.plaintext_bytes,
            "plaintext_sha256": receipt.plaintext_sha256,
            "stored_bytes": receipt.stored_bytes,
            "stored_sha256": receipt.stored_sha256,
            "completed_at": receipt.completed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_archive_volume_metadata_receipt(
    content: str,
) -> SealedArchiveVolumeMetadata:
    value = json.loads(content)
    return SealedArchiveVolumeMetadata(
        sequence=_stored_int(value["sequence"], "metadata sequence"),
        object_path=str(value["object_path"]),
        relative_path=str(value["relative_path"]),
        revision=str(value["revision"]) if value["revision"] is not None else None,
        plaintext_bytes=_stored_int(value["plaintext_bytes"], "metadata plaintext bytes"),
        plaintext_sha256=str(value["plaintext_sha256"]),
        stored_bytes=_stored_int(value["stored_bytes"], "metadata stored bytes"),
        stored_sha256=str(value["stored_sha256"]),
        completed_at=str(value["completed_at"]),
    )


def _sealed_provenance_object_json(receipt: Any) -> str:
    return json.dumps(
        {
            "object_id": receipt.object_id,
            "kind": receipt.kind,
            "relative_path": receipt.relative_path,
            "plaintext_bytes": receipt.plaintext_bytes,
            "plaintext_sha256": receipt.plaintext_sha256,
            "stored_bytes": receipt.stored_bytes,
            "stored_sha256": receipt.stored_sha256,
            "revision": receipt.revision,
            "completed_at": receipt.completed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_sealed_provenance_object(content: str) -> Any:
    from riverhog_core.domain.archive import SealedProvenanceObject

    value = json.loads(content)
    return SealedProvenanceObject(
        object_id=str(value["object_id"]),
        kind=str(value["kind"]),
        relative_path=str(value["relative_path"]),
        plaintext_bytes=_stored_int(value["plaintext_bytes"], "provenance plaintext bytes"),
        plaintext_sha256=str(value["plaintext_sha256"]),
        stored_bytes=_stored_int(value["stored_bytes"], "provenance stored bytes"),
        stored_sha256=str(value["stored_sha256"]),
        revision=str(value["revision"]) if value["revision"] is not None else None,
        completed_at=str(value["completed_at"]),
    )


def _sealed_provenance_json(receipt: SealedArchiveProvenance) -> str:
    return json.dumps(
        {
            "identity": receipt.identity,
            "root": json.loads(_sealed_provenance_object_json(receipt.root)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sealed_upload_provenance(
    upload: CollectionUploadRecord,
) -> SealedArchiveProvenance | None:
    if upload.provenance_mode == "omitted":
        if upload.provenance_archive_root_receipt_json is not None:
            raise RuntimeError("omitted provenance has an archive root")
        return None
    if upload.provenance_archive_root_receipt_json is None:
        raise RuntimeError("captured provenance root is not published")
    value = json.loads(upload.provenance_archive_root_receipt_json)
    root = _parse_sealed_provenance_object(
        json.dumps(value["root"], sort_keys=True, separators=(",", ":"))
    )
    receipt = SealedArchiveProvenance(identity=str(value["identity"]), root=root)
    if receipt.identity != upload.provenance_identity:
        raise RuntimeError("provenance root identity differs from upload state")
    return receipt


def _parse_parts(values: Sequence[Mapping[str, object]]) -> tuple[StoredArchivePart, ...]:
    return tuple(
        StoredArchivePart(
            number=_stored_int(value["number"], "part number"),
            plaintext_start=_stored_int(value["plaintext_start"], "part plaintext start"),
            plaintext_bytes=_stored_int(value["plaintext_bytes"], "part plaintext bytes"),
            plaintext_sha256=str(value["plaintext_sha256"]),
            stored_bytes=_stored_int(value["stored_bytes"], "part stored bytes"),
            stored_sha256=str(value["stored_sha256"]),
        )
        for value in values
    )


def _stored_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _parse_sealed_pack(content: str) -> SealedPackVolume:
    value = json.loads(content)
    return SealedPackVolume(
        volume_id=str(value["volume_id"]),
        sequence=int(value["sequence"]),
        relative_path=str(value["relative_path"]),
        files=int(value["files"]),
        source_bytes=int(value["source_bytes"]),
        plaintext_bytes=int(value["plaintext_bytes"]),
        age_state_json=json.dumps(value["age_state"], sort_keys=True, separators=(",", ":")),
        index_sha256=str(value["index_sha256"]),
        plan_sha256=str(value["plan_sha256"]),
        parts=_parse_parts(value["parts"]),
        revision=str(value["revision"]) if value["revision"] is not None else None,
        completed_at=str(value["completed_at"]),
        retrieval_cache=parse_retrieval_cache_receipt(value.get("retrieval_cache")),
    )


def _parse_sealed_raw(content: str) -> SealedRawVolume:
    value = json.loads(content)
    return SealedRawVolume(
        volume_id=str(value["volume_id"]),
        sequence=int(value["sequence"]),
        relative_path=str(value["relative_path"]),
        source_path=str(value["source_path"]),
        file_offset=int(value["file_offset"]),
        plaintext_bytes=int(value["plaintext_bytes"]),
        file_bytes=int(value["file_bytes"]),
        file_sha256=str(value["file_sha256"]),
        age_state_json=json.dumps(value["age_state"], sort_keys=True, separators=(",", ":")),
        parts=_parse_parts(value["parts"]),
        revision=str(value["revision"]) if value["revision"] is not None else None,
        completed_at=str(value["completed_at"]),
        retrieval_cache=parse_retrieval_cache_receipt(value.get("retrieval_cache")),
    )


def _custody_stats(session: Session, collection_id: int) -> tuple[int, int]:
    upload = session.get(CollectionUploadRecord, collection_id)
    if upload is None:
        return 0, 0
    return int(upload.custodied_file_count), int(upload.custodied_file_bytes)


def _custody_payload(
    *,
    files: int,
    byte_count: int,
    custodied_files: int,
    custodied_bytes: int,
) -> dict[str, object]:
    """Project exact completion separately from non-authoritative progress counters."""

    if (
        custodied_files < 0
        or custodied_bytes < 0
        or custodied_files > files
        or custodied_bytes > byte_count
        or (custodied_files == 0 and custodied_bytes != 0)
    ):
        raise RuntimeError("collection upload custody projection is inconsistent")
    if (custodied_files, custodied_bytes) == (files, byte_count):
        return {"state": "complete"}
    return {
        "state": "pending",
        "files": custodied_files,
        "bytes": custodied_bytes,
    }


def _validate_upload_list(*, page: int, per_page: int, sort: str, order: str) -> None:
    if page < 1 or per_page < 1 or per_page > 100:
        raise BadRequest("invalid collection upload pagination")
    if sort not in _UPLOAD_SORT_FIELDS:
        raise BadRequest("invalid collection upload sort")
    if order not in _SORT_ORDERS:
        raise BadRequest("collection upload order must be asc or desc")


def _upload_list_statement(
    *,
    q: str | None,
    tag: str | None,
    state: str | None,
    sort: str,
    order: str,
    principal: ApplicationPrincipal,
) -> Any:
    if state is not None and state not in _UPLOAD_STATES:
        raise BadRequest("invalid collection upload state")
    filters: list[Any] = [_upload_read_filter(principal)]
    if q:
        pattern = f"%{text_search_key(q)}%"
        matching_ids = (
            select(CollectionUploadRecord.collection_id)
            .where(CollectionUploadRecord.search_text.like(pattern))
            .union(
                select(CollectionUploadTagRecord.collection_id).where(
                    CollectionUploadTagRecord.tag_id.like(pattern)
                )
            )
        )
        filters.append(CollectionUploadRecord.collection_id.in_(matching_ids))
    if tag:
        filters.append(
            exists(
                select(1).where(
                    CollectionUploadTagRecord.collection_id == CollectionUploadRecord.collection_id,
                    CollectionUploadTagRecord.tag_id == tag,
                )
            )
        )
    if state:
        filters.append(CollectionUploadRecord.state == state)
    statement = select(
        CollectionUploadRecord,
        CollectionUploadRecord.file_count.label("files"),
        CollectionUploadRecord.file_bytes.label("bytes"),
    ).where(*filters)
    direction = asc if order == "asc" else desc
    sort_column = {
        "id": CollectionUploadRecord.collection_id,
        "created_at": CollectionUploadRecord.opened_at,
        "state": CollectionUploadRecord.state,
        "bytes": CollectionUploadRecord.file_bytes,
        "files": CollectionUploadRecord.file_count,
    }[sort]
    return statement.order_by(
        direction(sort_column),
        direction(CollectionUploadRecord.collection_id),
    )


def _upload_list_payload(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    files: int,
    byte_count: int,
) -> dict[str, object]:
    custodied_files, custodied_bytes = _custody_stats(session, upload.collection_id)
    return {
        "collection_id": upload.collection_id,
        "created_at": upload.opened_at,
        "tag_count": _upload_tag_count(session, upload.collection_id),
        "ingest_source": upload.ingest_source,
        "archive_store": upload.archive_store,
        "encryption_format": upload.encryption_format,
        "passphrase_id": upload.passphrase_id,
        "state": upload.state,
        "custody_mode": upload.custody_mode,
        "files": files,
        "bytes": byte_count,
        "custody": _custody_payload(
            files=files,
            byte_count=byte_count,
            custodied_files=custodied_files,
            custodied_bytes=custodied_bytes,
        ),
        "upload_state_expires_at": upload.lease_expires_at,
        "orphaned_at": upload.orphaned_at,
    }


def _normalize_custody_mode(value: str) -> CollectionUploadCustodyMode:
    normalized = str(value or "")
    if normalized not in {"producer-retained", "custody-transfer"}:
        raise BadRequest("collection upload custody mode is invalid")
    return cast(CollectionUploadCustodyMode, normalized)


def _upload_visible_to_deleter(
    session: Session,
    upload: CollectionUploadRecord,
    principal: ApplicationPrincipal,
) -> bool:
    if principal.allows_collection(COLLECTIONS_DELETE, upload.collection_id):
        return True
    allowed_tags = tag_ids(permission_resources(principal, COLLECTIONS_DELETE))
    if not allowed_tags:
        return False
    return (
        session.scalar(
            select(CollectionUploadTagRecord.tag_id)
            .where(
                CollectionUploadTagRecord.collection_id == upload.collection_id,
                CollectionUploadTagRecord.tag_id.in_(allowed_tags),
            )
            .limit(1)
        )
        is not None
    )


def _upload_read_filter(principal: ApplicationPrincipal) -> Any:
    owner = CollectionUploadRecord.initiated_by_app == principal.app
    resources = permission_resources(principal, COLLECTIONS_DELETE)
    if ALL_RESOURCES in resources:
        return true()
    filters = [owner]
    allowed_collections = collection_ids(resources)
    if allowed_collections:
        filters.append(CollectionUploadRecord.collection_id.in_(allowed_collections))
    allowed_tags = tag_ids(resources)
    if allowed_tags:
        filters.append(
            exists(
                select(1).where(
                    CollectionUploadTagRecord.collection_id == CollectionUploadRecord.collection_id,
                    CollectionUploadTagRecord.tag_id.in_(allowed_tags),
                )
            )
        )
    return or_(*filters)


def _orphan_discard_plan(
    session: Session,
    *,
    collection_id: int,
    expires_at: str,
) -> dict[str, object]:
    upload = session.get(CollectionUploadRecord, collection_id)
    if upload is None:
        raise NotFound(f"collection upload session not found: {collection_id}")
    files = upload.file_count
    byte_count = upload.file_bytes
    custodied_files, custodied_bytes = _custody_stats(session, collection_id)
    archive_objects = int(
        session.scalar(
            select(func.count(CollectionArchiveObjectUploadRecord.object_id)).where(
                CollectionArchiveObjectUploadRecord.collection_id == collection_id
            )
        )
        or 0
    )
    blockers = [] if upload.state == "orphaned" else [f"upload session is {upload.state}"]
    transform_prefix = "transform:"
    if upload.initiated_by_app.startswith(transform_prefix):
        execution_id = upload.initiated_by_app.removeprefix(transform_prefix)
        claim = session.scalar(
            select(CollectionProcessingClaimRecord).where(
                CollectionProcessingClaimRecord.execution_id == execution_id
            )
        )
        if (
            claim is not None
            and claim.state == "active"
            and parse_utc_timestamp(claim.expires_at) > utc_now()
        ):
            blockers.append("owning processing claim remains active until " + claim.expires_at)
    return {
        "status": "blocked" if blockers else "ready",
        "collection_id": collection_id,
        "warning": _CUSTODY_LOSS_WARNING,
        "expires_at": expires_at,
        "state": upload.state,
        "files": int(files),
        "bytes": int(byte_count),
        "custody": _custody_payload(
            files=int(files),
            byte_count=int(byte_count),
            custodied_files=custodied_files,
            custodied_bytes=custodied_bytes,
        ),
        "archive_objects": archive_objects,
        "blockers": blockers,
    }


def _custody_lease_expiry(config: RuntimeConfig, *, now: str | None = None) -> str:
    current = parse_utc_timestamp(now) if now is not None else utc_now()
    return format_utc_timestamp(current + config.collection_upload_custody_lease)


def _touch_upload(
    upload: CollectionUploadRecord,
    *,
    config: RuntimeConfig,
    now: str | None = None,
) -> None:
    current = now or utc_timestamp_now()
    upload.last_activity_at = current
    if upload.custody_mode == "custody-transfer" and upload.state in {"open", "closing"}:
        upload.lease_expires_at = _custody_lease_expiry(config, now=current)


def _archive_object_source_paths(record: CollectionArchiveObjectUploadRecord) -> tuple[str, ...]:
    if record.kind == "pack":
        return tuple(member.path for member in parse_pack_volume_plan(record.plan_json).members)
    if record.kind == "segment":
        return (parse_raw_volume_plan(record.plan_json).source_path,)
    raise RuntimeError(f"unsupported archive volume kind: {record.kind}")


def _record_artifact_custody_receipts(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    now: str,
) -> None:
    """Persist exact safe-release evidence once every covering object is sealed."""

    volumes_by_path: dict[str, list[CollectionArchiveObjectUploadRecord]] = {}
    for volume in upload.archive_objects:
        for path in _archive_object_source_paths(volume):
            volumes_by_path.setdefault(path, []).append(volume)
    newly_custodied_files = 0
    newly_custodied_bytes = 0
    for artifact in upload.files:
        if artifact.custody_receipt_json is not None:
            continue
        volumes = volumes_by_path.get(artifact.path, [])
        if not volumes or any(
            volume.state != "sealed" or volume.sealed_receipt_json is None for volume in volumes
        ):
            continue
        objects = tuple(
            CollectionUploadCustodyObjectDocument(
                volume_id=volume.object_id,
                sealed_receipt_sha256=hashlib.sha256(
                    str(volume.sealed_receipt_json).encode("utf-8")
                ).hexdigest(),
            )
            for volume in sorted(volumes, key=lambda current: current.object_id)
        )
        receipt = CollectionUploadArtifactCustodyReceiptDocument.seal(
            collection_id=upload.collection_id,
            path=artifact.path,
            bytes=artifact.bytes,
            sha256=artifact.sha256,
            archive_objects=objects,
        )
        artifact.custodied_at = now
        artifact.custody_receipt_json = receipt.model_dump_json(
            exclude_none=True,
            by_alias=True,
        )
        newly_custodied_files += 1
        newly_custodied_bytes += artifact.bytes
    upload.custodied_file_count += newly_custodied_files
    upload.custodied_file_bytes += newly_custodied_bytes


def _upload_payload(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    state: str | None = None,
    resumed: bool | None = None,
) -> dict[str, object]:
    files_total = upload.file_count
    bytes_total = upload.file_bytes
    custodied_files, custodied_bytes = _custody_stats(session, upload.collection_id)
    archive_progress = session.execute(
        select(
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_bytes), 0),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_units), 0),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.total_units), 0),
        ).where(CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id)
    ).one()
    payload: dict[str, object] = {
        "collection_id": upload.collection_id,
        "created_at": upload.opened_at,
        "tag_count": _upload_tag_count(session, upload.collection_id),
        "ingest_source": upload.ingest_source,
        "provenance_mode": upload.provenance_mode,
        "provenance_identity": None,
        "archive_store": upload.archive_store,
        "encryption_format": upload.encryption_format,
        "passphrase_id": upload.passphrase_id,
        "state": state or upload.state,
        "custody_mode": upload.custody_mode,
        "registration_constraints": _registration_constraints_payload(
            _planner_checkpoint(upload).policy
        ),
        "files_total": int(files_total),
        "bytes_total": int(bytes_total),
        "upload_state_expires_at": None if state == "canceled" else upload.lease_expires_at,
        "custody": _custody_payload(
            files=int(files_total),
            byte_count=int(bytes_total),
            custodied_files=custodied_files,
            custodied_bytes=custodied_bytes,
        ),
        "orphaned_at": None if state == "canceled" else upload.orphaned_at,
        "latest_failure": upload.archive_failure,
        "archive_phase": upload.archive_phase,
        "archive_phase_updated_at": upload.archive_phase_updated_at,
        "archive_next_attempt_at": upload.archive_next_attempt_at,
        "archive_storage_prefix": upload.archive_storage_prefix,
        "archive_uploaded_bytes": int(archive_progress[0]),
        "archive_total_bytes": None,
        "archive_uploaded_units": int(archive_progress[1]),
        "archive_total_units": int(archive_progress[2]),
        "collection": None,
    }
    if resumed is not None:
        payload["resumed"] = resumed
    return payload


def _finalized_payload(
    session: Session,
    collection: CollectionRecord,
    *,
    store_name: str,
    resumed: bool | None = None,
) -> dict[str, object]:
    copy = session.scalar(
        select(CollectionArchiveCopyRecord)
        .where(CollectionArchiveCopyRecord.collection_id == collection.id)
        .order_by(
            case((CollectionArchiveCopyRecord.store == store_name, 0), else_=1),
            CollectionArchiveCopyRecord.store,
        )
        .limit(1)
    )
    tag_count = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionTagRecord)
            .where(CollectionTagRecord.collection_id == collection.id)
        )
        or 0
    )
    archive_copy_count = int(
        session.scalar(
            select(func.count())
            .select_from(CollectionArchiveCopyRecord)
            .where(CollectionArchiveCopyRecord.collection_id == collection.id)
        )
        or 0
    )
    stored_bytes = int(
        session.scalar(
            select(func.coalesce(func.sum(CollectionArchiveObjectRecord.stored_bytes), 0)).where(
                CollectionArchiveObjectRecord.collection_id == collection.id,
                CollectionArchiveObjectRecord.store == (copy.store if copy else store_name),
            )
        )
        or 0
    )
    manifest_sha256 = session.scalar(
        select(CollectionArchiveObjectRecord.sha256).where(
            CollectionArchiveObjectRecord.collection_id == collection.id,
            CollectionArchiveObjectRecord.store == (copy.store if copy else store_name),
            CollectionArchiveObjectRecord.object_id == "manifest",
        )
    )
    if manifest_sha256 is None:
        raise RuntimeError("finalized collection has no immutable archive-root identity")
    summary = {
        "id": collection.id,
        "created_at": collection.created_at,
        "tag_count": tag_count,
        "content_identity": collection.content_identity,
        "tag_set_identity": collection.tag_set_identity,
        "archive_root_sha256": manifest_sha256,
        "encryption_format": collection.encryption_format,
        "passphrase_id": collection.passphrase_id,
        "files": int(collection.file_count),
        "bytes": int(collection.file_bytes),
        "remote_storage_bytes": stored_bytes,
        "archive_copy_count": archive_copy_count,
    }
    payload: dict[str, object] = {
        "collection_id": collection.id,
        "created_at": collection.created_at,
        "tag_count": tag_count,
        "ingest_source": collection.ingest_source,
        "provenance_mode": collection.provenance_mode,
        "provenance_identity": collection.provenance_identity,
        "content_identity": collection.content_identity,
        "archive_root_sha256": manifest_sha256,
        "archive_store": copy.store if copy else store_name,
        "encryption_format": collection.encryption_format,
        "passphrase_id": collection.passphrase_id,
        "state": "finalized",
        "custody_mode": collection.creation_custody_mode,
        "registration_constraints": None,
        "files_total": summary["files"],
        "bytes_total": summary["bytes"],
        "upload_state_expires_at": None,
        "custody": {"state": "complete"},
        "orphaned_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_phase_updated_at": copy.last_verified_at if copy else None,
        "archive_next_attempt_at": None,
        "archive_storage_prefix": copy.archive_storage_prefix if copy else None,
        "archive_uploaded_bytes": stored_bytes,
        "archive_total_bytes": stored_bytes,
        "archive_uploaded_units": None,
        "archive_total_units": None,
        "collection": summary,
    }
    if resumed is not None:
        payload["resumed"] = resumed
    return payload
