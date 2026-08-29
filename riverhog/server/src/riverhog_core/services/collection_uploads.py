from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from http_api_contracts import closed_literal_values
from riverhog_age import encrypt_age_scrypt
from riverhog_archive_contracts import CollectionEncryptionBinding
from riverhog_protocol import (
    CapturedFileProvenanceBinding,
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyMode,
    CollectionUploadCustodyObjectDocument,
    CollectionUploadFileBatchDocument,
    CollectionUploadFileIn,
    CollectionUploadRegistrationConstraintsDocument,
    CollectionUploadSort,
    CollectionUploadState,
    OmittedFileProvenanceBinding,
    PortableCollectionFile,
    PortableCollectionHeader,
    PortableCollectionIdentityBuilder,
    SortOrder,
    collection_upload_path_order_key,
    collection_upload_raw_digest_manifest,
    validate_collection_upload_batch_against_registration_constraints,
)
from riverhog_protocol.errors import BadRequest, Conflict, Forbidden, NotFound
from riverhog_protocol.manifest import collection_content_identity_ordered
from riverhog_protocol.paths import (
    normalize_collection_id,
    normalize_tag,
    relpath_search_key,
    relpath_sort_key,
    tag_set_identity,
    text_search_key,
)
from riverhog_protocol.raw_ingress import RawSourceDigestManifest, raw_volume_part_sha256s
from riverhog_protocol.transport import COLLECTION_UPLOAD_FILE_BATCH_MAX
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceArchive,
    ProvenanceValidationError,
    build_provenance_archive,
    reconstruct_provenance_archive_identity,
    validate_journal_chunks,
)
from sqlalchemy import asc, case, desc, exists, func, insert, literal, or_, select, true, update
from sqlalchemy.orm import Session, selectinload
from state_schema import read_snapshot
from time_formats import format_utc_timestamp, parse_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    ApplicationPrincipal,
)
from riverhog_core.archive_catalog import ArchiveVolumeProjection, build_archive_catalog_projection
from riverhog_core.archive_formats import ROOT_PROOF_STORAGE_FORMAT
from riverhog_core.archive_provenance import (
    ArchiveProvenancePublisher,
    SealedArchiveProvenance,
)
from riverhog_core.archive_recovery_descriptor import (
    ArchiveRecoveryDescriptorPublisher,
    SealedRecoveryDescriptor,
)
from riverhog_core.archive_root import ArchiveRootPublisher, SealedArchiveRoot
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import (
    begin_catalog_event,
    snapshot_catalog_event_collection_tags,
)
from riverhog_core.catalog_models import (
    CollectionArchiveAttestationRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionArchiveObjectUploadRecord,
    CollectionFileProvenanceRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionProofMaturationRecord,
    CollectionProvenanceJournalAgentRecord,
    CollectionProvenanceJournalChunkRecord,
    CollectionProvenanceJournalRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadProvenanceJournalChunkRecord,
    CollectionUploadProvenanceJournalRecord,
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import CollectionProcessingClaimRecord
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
from riverhog_core.ports.retrieval_cache import RetrievalCache, RetrievalCacheReceipt
from riverhog_core.proofs import ProofStamper
from riverhog_core.provenance_projection import (
    provenance_journal_projection,
)
from riverhog_core.raw_upload import RawUploadCheckpoint, RawVolumeUploader
from riverhog_core.raw_verification import verify_raw_file_from_part_manifest
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
_PROOF_RELATIVE_PATH = "manifest.json.ots.age"
_PROOF_CONTENT_TYPE = "application/vnd.riverhog.collection-manifest-proof+age"
_PROVENANCE_JOURNAL_CHUNK_BYTES = 1024 * 1024
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
    raw_manifest_json: str | None
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
        proof_stamper: ProofStamper,
        retrieval_cache: RetrievalCache | None = None,
        policy: CollectionVolumePolicy | None = None,
        session_factory: SessionFactory | None = None,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._retrieval_cache = retrieval_cache
        self._proof_stamper = proof_stamper
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
                        raw_part_plaintext_bytes=(
                            checkpoint.policy.raw_part_plaintext_bytes
                            if current["raw_manifest_json"] is not None
                            else None
                        ),
                        raw_digest_manifest_json=current["raw_manifest_json"],
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

    def put_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        content: bytes,
        sha256: str,
    ) -> dict[str, object]:
        return self.put_provenance_journal_chunks(
            collection_id,
            journal_id,
            chunks=lambda: iter((content,)),
            byte_count=len(content),
            sha256=sha256,
        )

    def put_provenance_journal_chunks(
        self,
        collection_id: int,
        journal_id: str,
        *,
        chunks: Callable[[], Iterable[bytes]],
        byte_count: int,
        sha256: str,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if byte_count < 1:
            raise BadRequest("provenance journal must not be empty")
        measured = hashlib.sha256()
        measured_bytes = 0

        def validated_chunks() -> Iterator[bytes]:
            nonlocal measured_bytes
            for source in chunks():
                chunk = bytes(source)
                if not chunk:
                    continue
                measured.update(chunk)
                measured_bytes += len(chunk)
                yield chunk

        try:
            summary = validate_journal_chunks(validated_chunks(), retain_frames=False)
        except ProvenanceValidationError as exc:
            raise BadRequest(str(exc)) from exc
        if measured_bytes != byte_count or measured.hexdigest() != sha256:
            raise BadRequest("provenance journal SHA-256 does not match its content")
        if summary.journal_id != journal_id:
            raise BadRequest("provenance journal path identity does not match its content")
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
                if existing.sha256 != sha256 or existing.bytes != byte_count:
                    raise Conflict("provenance journal already has different exact bytes")
                return _journal_payload(existing)
            record = CollectionUploadProvenanceJournalRecord(
                collection_id=normalized_id,
                journal_id=journal_id,
                bytes=byte_count,
                sha256=sha256,
                current_state_id=summary.current_state_id,
                current_path=summary.current_path,
                current_bytes=summary.current_bytes,
                current_sha256=summary.current_sha256,
            )
            session.add(record)
            session.flush()
            ordinal = 0
            for source in chunks():
                for offset in range(0, len(source), _PROVENANCE_JOURNAL_CHUNK_BYTES):
                    content = bytes(source[offset : offset + _PROVENANCE_JOURNAL_CHUNK_BYTES])
                    if not content:
                        continue
                    session.add(
                        CollectionUploadProvenanceJournalChunkRecord(
                            collection_id=normalized_id,
                            journal_id=journal_id,
                            ordinal=ordinal,
                            content=content,
                        )
                    )
                    ordinal += 1
            if ordinal == 0:
                raise BadRequest("provenance journal must not be empty")
            _touch_upload(upload, config=self._config)
            session.flush()
            return _journal_payload(record)

    def provenance_journal_metadata(
        self,
        collection_id: int,
        journal_id: str,
    ) -> tuple[int, str]:
        normalized_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            record = session.get(
                CollectionUploadProvenanceJournalRecord,
                (normalized_id, journal_id),
            )
            if record is None:
                raise NotFound(f"collection upload provenance journal not found: {journal_id}")
            return record.bytes, record.sha256

    def iter_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> Iterator[bytes]:
        """Yield exact staged journal chunks without materializing the value."""

        normalized_id = _collection_id(collection_id)
        with read_snapshot(self._session_factory) as session:
            if (
                session.get(
                    CollectionUploadProvenanceJournalRecord,
                    (normalized_id, journal_id),
                )
                is None
            ):
                raise NotFound(f"collection upload provenance journal not found: {journal_id}")
            statement = (
                select(CollectionUploadProvenanceJournalChunkRecord.content)
                .where(
                    CollectionUploadProvenanceJournalChunkRecord.collection_id == normalized_id,
                    CollectionUploadProvenanceJournalChunkRecord.journal_id == journal_id,
                )
                .order_by(CollectionUploadProvenanceJournalChunkRecord.ordinal)
                .execution_options(yield_per=16)
            )
            yield from session.scalars(statement)

    def complete(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_identity: str,
        provenance_identity: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if files_total < 1 or _SHA256_RE.fullmatch(content_identity) is None:
            raise BadRequest("collection upload completion identity is invalid")
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is not None:
                if (
                    collection.content_identity != content_identity
                    or collection.provenance_identity != provenance_identity
                ):
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
            actual_etag = collection_content_identity_ordered(
                (row.path, row.bytes, row.sha256)
                for batch in _upload_file_batches(session, normalized_id)
                for row in batch
            )
            if upload.file_count != files_total or actual_etag != content_identity:
                raise Conflict("collection upload registered manifest differs from completion")
            actual_provenance_identity = _upload_provenance_identity(
                session,
                upload,
            )
            if provenance_identity != actual_provenance_identity:
                raise Conflict("collection upload provenance identity differs from completion")
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
            upload.provenance_identity = actual_provenance_identity
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
        work = self.get_volume(collection_id, volume_id)
        units = work["units"]
        if not isinstance(units, list) or unit < 0 or unit >= len(units):
            raise NotFound(f"collection upload unit not found: {unit}")
        return dict(units[unit])

    def get(self, collection_id: int) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is not None:
                return _upload_payload(session, upload)
            collection = session.scalar(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.archive_copies))
                .where(CollectionRecord.id == normalized_id)
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
            if file is None or file.raw_digest_manifest_json is None:
                raise RuntimeError("raw volume source digest manifest is missing")
            manifest = RawSourceDigestManifest.from_json_bytes(file.raw_digest_manifest_json)
        return raw_volume_part_sha256s(
            manifest,
            file_offset=plan.file_offset,
            plaintext_bytes=plan.plaintext_bytes,
        )

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
                if not interrupted and not _ready_for_finalization(upload):
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
                if not _ready_for_finalization(upload):
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
            if not _ready_for_finalization(upload):
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
            upload.archive_attempt_count += 1
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
            if upload is None or not _ready_for_finalization(upload):
                return
            _mark_finalization_ready(upload, now=now)

    def _reconcile_sealed_volume_receipts(self, collection_id: int) -> None:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            store_name = upload.archive_store
            passphrase_id = upload.passphrase_id
            pending = [
                (
                    current.object_id,
                    current.kind,
                    current.plan_json,
                    current.checkpoint_json,
                )
                for current in upload.archive_objects
                if current.state == "sealed" and current.sealed_receipt_json is None
            ]
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
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                return
            files = [
                ArchiveFile(path=row.path, bytes=row.bytes, sha256=row.sha256)
                for row in sorted(upload.files, key=lambda item: item.file_order)
            ]
            plans = sorted(upload.archive_objects, key=lambda item: item.sequence)
            packs: list[tuple[PackVolumePlan, SealedPackVolume]] = []
            raw_volumes: list[SealedRawVolume] = []
            raw_manifests: dict[str, RawSourceDigestManifest] = {}
            for row in upload.files:
                if row.raw_digest_manifest_json:
                    raw_manifests[row.path] = RawSourceDigestManifest.from_json_bytes(
                        row.raw_digest_manifest_json
                    )
            for record in plans:
                if record.sealed_receipt_json is None:
                    raise RuntimeError("archive volume is not sealed")
                if record.kind == "pack":
                    packs.append(
                        (
                            parse_pack_volume_plan(record.plan_json),
                            _parse_sealed_pack(record.sealed_receipt_json),
                        )
                    )
                else:
                    raw_volumes.append(_parse_sealed_raw(record.sealed_receipt_json))
            verified_raw = [
                verify_raw_file_from_part_manifest(
                    file=file,
                    volumes=tuple(
                        current for current in raw_volumes if current.source_path == file.path
                    ),
                    manifest=raw_manifests[file.path],
                    verified_at=utc_timestamp_now(),
                )
                for file in files
                if file.path in raw_manifests
            ]
            store_name = upload.archive_store
            prefix = upload.archive_storage_prefix
            if not prefix:
                raise RuntimeError("collection archive storage prefix is missing")
            provenance = _upload_provenance_archive(session, upload)
            encryption = CollectionEncryptionBinding(
                format=upload.encryption_format,
                passphrase_id=upload.passphrase_id,
            )
            passphrase = self._config.archive_passphrase_for(upload.passphrase_id)

        archive_store = self._archive_stores.require(store_name)
        sealed_provenance = (
            ArchiveProvenancePublisher(
                object_store=archive_store.immutable_objects,
                passphrase=passphrase,
                scrypt_log_n=self._config.archive_scrypt_work_factor,
            ).publish(
                archive_storage_prefix=prefix,
                provenance=provenance,
            )
            if provenance is not None
            else None
        )
        root = ArchiveRootPublisher(
            object_store=archive_store.immutable_objects,
            passphrase=passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
        ).publish(
            archive_storage_prefix=prefix,
            files=files,
            packs=packs,
            raw_volumes=raw_volumes,
            verified_raw_files=verified_raw,
            provenance_identity=(
                sealed_provenance.identity if sealed_provenance is not None else None
            ),
            provenance_objects=(
                (*sealed_provenance.bundles, sealed_provenance.index)
                if sealed_provenance is not None
                else ()
            ),
        )
        recovery_descriptor = ArchiveRecoveryDescriptorPublisher(
            object_store=archive_store.immutable_objects
        ).publish(
            archive_storage_prefix=prefix,
            root=root,
            encryption=encryption,
        )
        proof_bytes = self._persisted_proof(collection_id, root.manifest_bytes)
        proof_ciphertext = encrypt_age_scrypt(
            proof_bytes,
            passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        proof_receipt = archive_store.immutable_objects.put_immutable_object(
            object_path=f"{prefix}/{_PROOF_RELATIVE_PATH}",
            content=proof_ciphertext,
            content_type=_PROOF_CONTENT_TYPE,
            required_identity_assertions={
                "riverhog-format": ROOT_PROOF_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(proof_bytes)),
                "riverhog-plaintext-sha256": hashlib.sha256(proof_bytes).hexdigest(),
                "riverhog-archive-root-sha256": root.plaintext_sha256,
            },
            placement="immediate",
        )
        projection = build_archive_catalog_projection(
            collection_id=collection_id,
            store=store_name,
            archive_storage_prefix=prefix,
            root=root,
            files=files,
            packs=packs,
            raw_volumes=raw_volumes,
            verified_raw_files=verified_raw,
            provenance_identity=(
                sealed_provenance.identity if sealed_provenance is not None else None
            ),
            provenance_objects=(
                (*sealed_provenance.bundles, sealed_provenance.index)
                if sealed_provenance is not None
                else ()
            ),
        )
        self._commit_finalized_collection(
            collection_id=collection_id,
            projection=projection,
            root=root,
            proof_bytes=proof_bytes,
            proof_receipt=proof_receipt,
            recovery_descriptor=recovery_descriptor,
            sealed_provenance=sealed_provenance,
        )

    def _persisted_proof(self, collection_id: int, manifest_bytes: bytes) -> bytes:
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                raise RuntimeError("collection upload disappeared before proof publication")
            existing = upload.collection_manifest_proof_bytes_b64
            if existing:
                return base64.b64decode(existing)
        with tempfile.TemporaryDirectory(prefix="riverhog-manifest-proof-") as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            proof_path = self._proof_stamper.stamp(manifest_path)
            proof_bytes = proof_path.read_bytes()
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is None:
                raise RuntimeError("collection upload disappeared while recording its proof")
            upload.collection_manifest_bytes_b64 = base64.b64encode(manifest_bytes).decode("ascii")
            upload.collection_manifest_proof_bytes_b64 = base64.b64encode(proof_bytes).decode(
                "ascii"
            )
        return proof_bytes

    def _commit_finalized_collection(
        self,
        *,
        collection_id: int,
        projection: object,
        root: SealedArchiveRoot,
        proof_bytes: bytes,
        proof_receipt: object,
        recovery_descriptor: SealedRecoveryDescriptor,
        sealed_provenance: SealedArchiveProvenance | None,
    ) -> None:
        from riverhog_core.archive_catalog import ArchiveCatalogProjection
        from riverhog_core.ports.archive_objects import ImmutableObjectReceipt

        if not isinstance(projection, ArchiveCatalogProjection) or not isinstance(
            proof_receipt, ImmutableObjectReceipt
        ):
            raise TypeError("archive finalization receipts are invalid")
        with session_scope(self._session_factory) as session:
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(CollectionUploadRecord.collection_id == collection_id)
                .with_for_update()
            )
            if upload is None:
                return
            if session.get(CollectionRecord, collection_id) is not None:
                session.delete(upload)
                return
            content_identity = collection_content_identity_ordered(
                (row.path, row.bytes, row.sha256)
                for batch in _upload_file_batches(session, collection_id)
                for row in batch
            )
            provenance_mode = _final_provenance_mode(
                session,
                collection_id,
                upload.provenance_mode,
            )
            now = utc_timestamp_now()
            inventory_builder = PortableCollectionIdentityBuilder(
                PortableCollectionHeader(
                    collection=collection_id,
                    content_identity=content_identity,
                    encryption_format=upload.encryption_format,
                    passphrase_id=upload.passphrase_id,
                    provenance_mode=provenance_mode,  # type: ignore[arg-type]
                    provenance_identity=upload.provenance_identity,
                )
            )
            for batch in _upload_file_path_batches(session, collection_id):
                for row in batch:
                    inventory_builder.add(
                        PortableCollectionFile(
                            path=row.path,
                            bytes=row.bytes,
                            sha256=row.sha256,
                        )
                    )
            inventory_identity = inventory_builder.identity
            if (
                inventory_builder.files != upload.file_count
                or inventory_builder.bytes != upload.file_bytes
            ):
                raise RuntimeError("collection upload file projections are inconsistent")
            collection = CollectionRecord(
                id=collection_id,
                creation_idempotency_key=upload.idempotency_key,
                creation_identity_sha256=upload.creation_identity_sha256,
                creation_custody_mode=upload.custody_mode,
                content_identity=content_identity,
                encryption_format=upload.encryption_format,
                passphrase_id=upload.passphrase_id,
                provenance_mode=provenance_mode,
                provenance_identity=upload.provenance_identity,
                inventory_identity=inventory_identity,
                metadata_revision=1,
                metadata_updated_at=now,
                ingest_source=upload.ingest_source,
                created_by_app=upload.initiated_by_app,
                created_by_key_id=upload.initiated_by_key_id,
                created_at=upload.opened_at or now,
                file_count=upload.file_count,
                file_bytes=upload.file_bytes,
            )
            session.add(collection)
            session.flush()
            for batch in _upload_file_batches(session, collection_id):
                session.execute(
                    insert(CollectionFileRecord),
                    [
                        {
                            "collection_id": collection_id,
                            "path": row.path,
                            "bytes": row.bytes,
                            "sha256": row.sha256,
                            "provenance_status": row.provenance_status,
                            "path_sort_key": relpath_sort_key(row.path),
                            "search_text": f"{collection_id}/{relpath_search_key(row.path)}",
                            "path_search_text": relpath_search_key(row.path),
                        }
                        for row in batch
                    ],
                )
            for journal in upload.provenance_journals:
                journal_projection = provenance_journal_projection(
                    collection_id=collection_id,
                    journal_id=journal.journal_id,
                    summary=validate_journal_chunks(
                        _iter_upload_journal_chunks(
                            session,
                            collection_id,
                            journal.journal_id,
                        )
                    ),
                )
                session.add(
                    CollectionProvenanceJournalRecord(
                        collection_id=collection_id,
                        journal_id=journal.journal_id,
                        bytes=journal.bytes,
                        sha256=journal.sha256,
                        entries=journal_projection.summary.entries,
                        agent_count=len(journal_projection.summary.agent_ids),
                        entity_counts_json=journal_projection.entity_counts_json,
                        current_state_id=journal.current_state_id,
                        current_path=journal.current_path,
                        current_bytes=journal.current_bytes,
                        current_sha256=journal.current_sha256,
                    )
                )
                session.flush()
                session.execute(
                    insert(CollectionProvenanceJournalChunkRecord).from_select(
                        ["collection_id", "journal_id", "ordinal", "content"],
                        select(
                            CollectionUploadProvenanceJournalChunkRecord.collection_id,
                            CollectionUploadProvenanceJournalChunkRecord.journal_id,
                            CollectionUploadProvenanceJournalChunkRecord.ordinal,
                            CollectionUploadProvenanceJournalChunkRecord.content,
                        ).where(
                            CollectionUploadProvenanceJournalChunkRecord.collection_id
                            == collection_id,
                            CollectionUploadProvenanceJournalChunkRecord.journal_id
                            == journal.journal_id,
                        ),
                    )
                )
                session.execute(
                    insert(CollectionProvenanceJournalAgentRecord),
                    [
                        {
                            "collection_id": collection_id,
                            "journal_id": journal.journal_id,
                            "agent_id": agent_id,
                        }
                        for agent_id in sorted(journal_projection.summary.agent_ids)
                    ],
                )
                session.add_all(journal_projection.entities)
                session.add_all(journal_projection.external_state_references)
                session.flush()
            for batch in _upload_file_batches(session, collection_id):
                session.execute(
                    insert(CollectionFileProvenanceRecord),
                    [
                        {
                            "collection_id": collection_id,
                            "path": row.path,
                            "status": row.provenance_status,
                            "journal_id": row.provenance_journal_id,
                            "current_state_id": row.provenance_current_state_id,
                            "omission_reason": row.provenance_omission_reason,
                        }
                        for row in batch
                    ],
                )
            session.execute(
                insert(CollectionTagRecord).from_select(
                    (
                        "collection_id",
                        "tag_id",
                        "assigned_by_app",
                        "assigned_by_key_id",
                        "assigned_at",
                    ),
                    select(
                        CollectionUploadTagRecord.collection_id,
                        CollectionUploadTagRecord.tag_id,
                        literal(upload.initiated_by_app),
                        literal(upload.initiated_by_key_id),
                        literal(now),
                    ).where(CollectionUploadTagRecord.collection_id == collection_id),
                )
            )
            tag_count = _upload_tag_count(session, collection_id)
            adjusted = session.execute(
                update(TagRecord)
                .where(
                    exists(
                        select(1).where(
                            CollectionUploadTagRecord.collection_id == collection_id,
                            CollectionUploadTagRecord.tag_id == TagRecord.id,
                        )
                    )
                )
                .values(collection_count=TagRecord.collection_count + 1)
            )
            if int(getattr(adjusted, "rowcount", 0) or 0) != tag_count:
                raise RuntimeError("collection upload tag projection is inconsistent")
            store_binding = self._archive_stores.require(upload.archive_store)
            copy = CollectionArchiveCopyRecord(
                collection_id=collection_id,
                store=upload.archive_store,
                state="uploaded",
                archive_storage_prefix=projection.root.archive_storage_prefix,
                last_uploaded_at=now,
                last_verified_at=now,
            )
            session.add(copy)
            session.flush()
            cache_receipts: list[tuple[ArchiveVolumeProjection, RetrievalCacheReceipt]] = []
            cache_required = (
                self._config.retrieval_cache_new_archive_enabled
                and self._retrieval_cache is not None
                and store_binding.store.read_mode() == "restore_required"
            )
            for volume in projection.volumes:
                if cache_required and volume.retrieval_cache is None:
                    raise RuntimeError(
                        "restore-required archive volume is missing its retrieval cache receipt"
                    )
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        object_id=volume.volume_id,
                        object_order=volume.sequence,
                        kind=volume.kind,
                        object_path=volume.object_path,
                        plaintext_bytes=volume.plaintext_bytes,
                        stored_bytes=volume.stored_bytes,
                        sha256=None,
                        stored_sha256=None,
                        revision=volume.revision,
                        age_state_json=volume.age_state_json,
                        archive_parts_json=volume.archive_parts_json,
                        plan_sha256=volume.plan_sha256,
                        index_sha256=volume.index_sha256,
                        uploaded_at=volume.completed_at,
                        verified_at=now,
                    )
                )
                if volume.retrieval_cache is not None:
                    cache_receipts.append((volume, volume.retrieval_cache))
            session.flush()
            cache_expires_at = format_utc_timestamp(
                utc_now() + self._config.retrieval_cache_new_archive_lease
            )
            cache_leases: list[RetrievalCacheLeaseRecord] = []
            for volume, receipt in cache_receipts:
                if receipt.stored_bytes != volume.stored_bytes or len(receipt.stored_sha256) != 64:
                    raise RuntimeError(
                        "retrieval cache receipt does not match its sealed archive volume"
                    )
                session.add(
                    RetrievalCacheObjectRecord(
                        source_store=upload.archive_store,
                        collection_id=collection_id,
                        object_id=volume.volume_id,
                        object_path=receipt.object_path,
                        revision=receipt.revision,
                        stored_bytes=receipt.stored_bytes,
                        stored_sha256=receipt.stored_sha256,
                        cached_at=receipt.cached_at,
                        verified_at=receipt.verified_at,
                        state="ready",
                    )
                )
                cache_leases.append(
                    RetrievalCacheLeaseRecord(
                        owner="new-archive",
                        source_store=upload.archive_store,
                        collection_id=collection_id,
                        object_id=volume.volume_id,
                        expires_at=cache_expires_at,
                    )
                )
            session.flush()
            session.add_all(cache_leases)
            artifact_order = len(projection.volumes)
            if sealed_provenance is not None:
                for current in (*sealed_provenance.bundles, sealed_provenance.index):
                    session.add(
                        CollectionArchiveObjectRecord(
                            collection_id=collection_id,
                            store=upload.archive_store,
                            object_id=current.object_id,
                            object_order=artifact_order,
                            kind=current.kind,
                            object_path=(
                                f"{projection.root.archive_storage_prefix}/{current.relative_path}"
                            ),
                            plaintext_bytes=current.plaintext_bytes,
                            stored_bytes=current.stored_bytes,
                            sha256=current.plaintext_sha256,
                            stored_sha256=current.stored_sha256,
                            revision=current.revision,
                            uploaded_at=current.completed_at,
                            verified_at=now,
                        )
                    )
                    artifact_order += 1
            session.add_all(
                (
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        object_id="manifest",
                        object_order=artifact_order,
                        kind="manifest",
                        object_path=root.object_path,
                        plaintext_bytes=root.plaintext_bytes,
                        stored_bytes=root.stored_bytes,
                        sha256=root.plaintext_sha256,
                        stored_sha256=root.stored_sha256,
                        revision=root.revision,
                        uploaded_at=root.completed_at,
                        verified_at=now,
                    ),
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        object_id="recovery-descriptor",
                        object_order=artifact_order + 1,
                        kind="recovery-descriptor",
                        object_path=recovery_descriptor.object_path,
                        plaintext_bytes=recovery_descriptor.bytes,
                        stored_bytes=recovery_descriptor.bytes,
                        sha256=recovery_descriptor.sha256,
                        stored_sha256=recovery_descriptor.sha256,
                        revision=recovery_descriptor.revision,
                        uploaded_at=recovery_descriptor.completed_at,
                        verified_at=now,
                    ),
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        object_id="proof",
                        object_order=artifact_order + 2,
                        kind="proof",
                        object_path=proof_receipt.object_path,
                        plaintext_bytes=len(proof_bytes),
                        stored_bytes=proof_receipt.stored_bytes,
                        sha256=hashlib.sha256(proof_bytes).hexdigest(),
                        stored_sha256=proof_receipt.stored_sha256,
                        revision=proof_receipt.revision,
                        uploaded_at=proof_receipt.completed_at,
                        verified_at=now,
                    ),
                )
            )
            session.flush()
            session.add_all(
                CollectionArchiveFileObjectRecord(
                    collection_id=collection_id,
                    store=upload.archive_store,
                    path=member.path,
                    sequence=0,
                    object_id=member.volume_id,
                    file_offset=0,
                    object_offset=member.data_offset,
                    bytes=member.bytes,
                    member=member.path,
                )
                for member in projection.pack_members
            )
            segment_orders: dict[str, int] = {}
            for segment in projection.segments:
                sequence = segment_orders.get(segment.path, 0)
                segment_orders[segment.path] = sequence + 1
                session.add(
                    CollectionArchiveFileObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        path=segment.path,
                        sequence=sequence,
                        object_id=segment.volume_id,
                        file_offset=segment.file_offset,
                        object_offset=0,
                        bytes=segment.bytes,
                        member=None,
                    )
                )
            session.add_all(
                (
                    CollectionMetadataPublicationRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        desired_revision=1,
                        state="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                    ),
                    CollectionProofMaturationRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        state="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                    ),
                    CollectionArchiveAttestationRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        state="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                    ),
                )
            )
            catalog_event = begin_catalog_event(
                session,
                change="created",
                collection_id=collection_id,
                occurred_at=now,
                inventory_identity=inventory_identity,
            )
            snapshot_catalog_event_collection_tags(
                session,
                event=catalog_event,
                phase="after",
                collection_id=collection_id,
            )
            self._events.emit_collection(
                type="collection.finalized",
                collection_id=collection_id,
                details={
                    "files_total": int(upload.file_count),
                    "bytes_total": int(upload.file_bytes),
                    "archive_store": upload.archive_store,
                    "archive_storage_prefix": projection.root.archive_storage_prefix,
                    "archive_objects": (
                        len(projection.volumes)
                        + (len(sealed_provenance.bundles) + 1 if sealed_provenance else 0)
                        + 3
                    ),
                },
                terminal=True,
                session=session,
            )
            session.delete(upload)


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
        or claim.output_tags_json is None
        or parse_utc_timestamp(claim.expires_at) <= utc_now()
        or initiator.key_id != claim.consumer_key_id
    ):
        raise Forbidden("transform output intent is not active")
    expected_tags = tuple(str(value) for value in json.loads(claim.output_tags_json))
    if (
        idempotency_key != execution_id
        or tag_set_identity(expected_tags) != tag_set_identity_sha256
        or ingest_source != f"transform:{execution_id}"
        or archive_store is not None
    ):
        raise Forbidden("collection upload differs from the sealed transform output intent")


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
) -> _RegisteredFile:
    path = value.path
    byte_count = value.bytes
    sha256 = value.sha256
    raw_manifest = collection_upload_raw_digest_manifest(value, constraints)
    raw_json = raw_manifest.to_json_bytes().decode("utf-8") if raw_manifest is not None else None
    raw_provenance = value.provenance
    provenance_journal_id: str | None = None
    provenance_current_state_id: str | None = None
    provenance_omission_reason: str | None = None
    if isinstance(raw_provenance, CapturedFileProvenanceBinding):
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
        "raw_manifest_json": raw_json,
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
        "raw_manifest_json": record.raw_digest_manifest_json,
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


def _upload_provenance_archive(
    session: Session,
    upload: CollectionUploadRecord,
) -> ProvenanceArchive | None:
    bindings = tuple(
        FileProvenanceBinding(
            path=row.path,
            bytes=row.bytes,
            sha256=row.sha256,
            status=row.provenance_status,  # type: ignore[arg-type]
            journal_id=row.provenance_journal_id,
            current_state_id=row.provenance_current_state_id,
            omission_reason=row.provenance_omission_reason,
        )
        for row in sorted(upload.files, key=lambda item: item.file_order)
    )
    journals = {
        row.journal_id: _upload_journal_bytes(session, upload.collection_id, row.journal_id)
        for row in upload.provenance_journals
    }
    if upload.provenance_mode == "omitted":
        if journals or any(item.status != "omitted" for item in bindings):
            raise Conflict("collection-wide provenance omission is not internally consistent")
        return None
    try:
        return build_provenance_archive(bindings=bindings, journals=journals)
    except ProvenanceValidationError as exc:
        raise Conflict(str(exc)) from exc


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


def _upload_provenance_identity(
    session: Session,
    upload: CollectionUploadRecord,
) -> str | None:
    collection_id = upload.collection_id
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
            raise Conflict("collection-wide provenance omission is not internally consistent")
        return None

    def bindings() -> Iterator[FileProvenanceBinding]:
        for batch in _upload_file_batches(session, collection_id):
            for row in batch:
                yield FileProvenanceBinding(
                    path=row.path,
                    bytes=row.bytes,
                    sha256=row.sha256,
                    status=row.provenance_status,
                    journal_id=row.provenance_journal_id,
                    current_state_id=row.provenance_current_state_id,
                    omission_reason=row.provenance_omission_reason,
                )

    def journals() -> Iterator[tuple[str, bytes]]:
        for journal_id in _iter_upload_journal_ids(session, collection_id):
            yield (
                journal_id,
                _upload_journal_bytes(
                    session,
                    collection_id,
                    journal_id,
                ),
            )

    try:
        return reconstruct_provenance_archive_identity(
            bindings=bindings,
            journals=journals,
        )
    except ProvenanceValidationError as exc:
        raise Conflict(str(exc)) from exc


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


def _ready_for_finalization(upload: CollectionUploadRecord) -> bool:
    checkpoint = _planner_checkpoint(upload)
    return bool(
        checkpoint.closed
        and upload.archive_objects
        and all(current.state == "sealed" for current in upload.archive_objects)
        and _has_complete_artifact_custody(upload)
    )


def _has_complete_artifact_custody(upload: CollectionUploadRecord) -> bool:
    return bool(upload.files) and all(
        current.custody_receipt_json is not None for current in upload.files
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


def _journal_payload(
    record: CollectionUploadProvenanceJournalRecord,
) -> dict[str, object]:
    return {
        "journal_id": record.journal_id,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "current_state_id": record.current_state_id,
        "current_path": record.current_path,
        "current_bytes": record.current_bytes,
        "current_sha256": record.current_sha256,
    }


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
