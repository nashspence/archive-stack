from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, TypedDict

from riverhog_age import encrypt_age_scrypt
from riverhog_protocol.errors import BadRequest, Conflict, NotFound
from riverhog_protocol.manifest import collection_content_etag_ordered
from riverhog_protocol.paths import (
    PathNormalizationError,
    normalize_collection_id,
    normalize_relpath,
)
from riverhog_protocol.raw_ingress import RawSourceDigestManifest, raw_volume_part_sha256s
from riverhog_provenance import (
    FileProvenanceBinding,
    ProvenanceArchive,
    ProvenanceValidationError,
    build_provenance_archive,
    validate_journal,
)
from sqlalchemy import asc, desc, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload
from time_formats import format_utc_timestamp, utc_now, utc_timestamp_now

from riverhog_core.app_permissions import COLLECTIONS_CREATE, ApplicationPrincipal
from riverhog_core.archive_catalog import build_archive_catalog_projection
from riverhog_core.archive_formats import ROOT_PROOF_STORAGE_FORMAT
from riverhog_core.archive_manifest import validate_collection_archive_plan
from riverhog_core.archive_provenance import (
    ArchiveProvenancePublisher,
    SealedArchiveProvenance,
)
from riverhog_core.archive_root import ArchiveRootPublisher, SealedArchiveRoot
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_events import record_catalog_event
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
    CollectionProvenanceJournalRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadFileRecord,
    CollectionUploadProvenanceJournalRecord,
    CollectionUploadRecord,
    CollectionUploadTagRecord,
    TagRecord,
)
from riverhog_core.collection_access import require_collection_create_access
from riverhog_core.collection_metadata import collection_record_manifest
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.domain.archive import (
    ArchiveFile,
    PackVolumePlan,
    RawVolumePlan,
    SealedPackVolume,
    SealedRawVolume,
    StoredPartReceipt,
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
from riverhog_core.proofs import ProofStamper
from riverhog_core.provenance_projection import (
    ProvenanceJournalProjection,
    provenance_journal_projection,
)
from riverhog_core.raw_upload import RawUploadCheckpoint, RawVolumeUploader
from riverhog_core.raw_verification import verify_raw_file_from_part_manifest
from riverhog_core.raw_volume import parse_raw_volume_plan, raw_volume_plan_bytes
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.lifecycle_events import (
    SqlAlchemyLifecycleEventService,
    event_context_json,
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
_LOG = logging.getLogger("riverhog_core.collection_uploads")


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
        policy: CollectionVolumePolicy | None = None,
        session_factory: SessionFactory | None = None,
        throughput_tuning: ArchiveThroughputTuning | None = None,
        transfer_resources: ArchiveTransferResources | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
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
        self._age_sessions = ResumableAgeSessionCache(
            config.archive_passphrase,
            max_entries=tuning.age_session_cache_entries,
            derivation_gate=self._resources.age_derivations,
        )

    def create_or_resume(
        self,
        *,
        idempotency_key: str,
        tags: Sequence[str],
        ingest_source: str | None,
        archive_store: str | None,
        initiator: ApplicationPrincipal,
        event_context: Mapping[str, object] | None,
        provenance_mode: str = "captured",
        provenance_omission_reason: str | None = None,
    ) -> dict[str, object]:
        key = _normalize_idempotency_key(idempotency_key)
        normalized_tags = _normalize_tags(tags)
        store_name = archive_store or self._config.archive_write_store
        try:
            archive_binding = self._archive_stores.require(store_name)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        require_collection_create_access(initiator, COLLECTIONS_CREATE, normalized_tags)
        context_json = event_context_json(event_context)
        normalized_provenance_mode, normalized_omission_reason = _normalize_provenance_mode(
            provenance_mode,
            provenance_omission_reason,
        )

        with session_scope(self._session_factory) as session:
            collection = session.scalar(
                select(CollectionRecord)
                .options(selectinload(CollectionRecord.archive_copies))
                .where(
                    CollectionRecord.created_by_app == initiator.app,
                    CollectionRecord.creation_idempotency_key == key,
                )
            )
            if collection is not None:
                collection_tags = tuple(sorted(current.tag_id for current in collection.tags))
                if collection_tags != normalized_tags:
                    raise Conflict("collection upload idempotency identity changed")
                if (
                    normalized_provenance_mode == "omitted"
                    and collection.provenance_mode != "omitted"
                ) or (
                    normalized_provenance_mode == "captured"
                    and collection.provenance_mode == "omitted"
                ):
                    raise Conflict("collection upload provenance identity changed")
                return _finalized_payload(session, collection, store_name=store_name)
            upload = session.scalar(
                select(CollectionUploadRecord)
                .where(
                    CollectionUploadRecord.initiated_by_app == initiator.app,
                    CollectionUploadRecord.idempotency_key == key,
                )
                .with_for_update()
            )
            if upload is not None:
                if (
                    _upload_tags(upload) != normalized_tags
                    or upload.archive_store != store_name
                    or upload.event_context_json != context_json
                    or upload.provenance_mode != normalized_provenance_mode
                    or upload.provenance_omission_reason != normalized_omission_reason
                ):
                    raise Conflict("collection upload idempotency identity changed")
                return _upload_payload(session, upload)

            _require_tags(session, normalized_tags)
            now = utc_timestamp_now()
            checkpoint = new_incremental_volume_planner(policy=self._policy)
            upload = CollectionUploadRecord(
                idempotency_key=key,
                ingest_source=ingest_source,
                provenance_mode=normalized_provenance_mode,
                provenance_omission_reason=normalized_omission_reason,
                initiated_by_app=initiator.app,
                initiated_by_key_id=initiator.key_id,
                event_context_json=context_json,
                state="open",
                archive_store=store_name,
                opened_at=now,
                last_activity_at=now,
                archive_phase="planning",
                archive_phase_updated_at=now,
                archive_storage_prefix=archive_binding.store.new_collection_archive_storage_prefix(),
                planner_checkpoint_json=(
                    incremental_volume_planner_checkpoint_bytes(checkpoint).decode("utf-8")
                ),
            )
            session.add(upload)
            session.flush()
            session.add_all(
                CollectionUploadTagRecord(collection_id=upload.collection_id, tag_id=tag)
                for tag in normalized_tags
            )
            session.flush()
            return _upload_payload(session, upload)

    def require_access(self, collection_id: int, principal: ApplicationPrincipal) -> None:
        normalized = _collection_id(collection_id)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized)
            if upload is not None:
                if upload.initiated_by_app != principal.app:
                    raise NotFound(f"collection upload not found: {normalized}")
                require_collection_create_access(
                    principal,
                    COLLECTIONS_CREATE,
                    _upload_tags(upload),
                )
                return
            collection = session.get(CollectionRecord, normalized)
            if collection is None or collection.created_by_app != principal.app:
                raise NotFound(f"collection upload not found: {normalized}")
            tags = tuple(
                session.scalars(
                    select(CollectionTagRecord.tag_id)
                    .where(CollectionTagRecord.collection_id == normalized)
                    .order_by(CollectionTagRecord.tag_id)
                )
            )
            require_collection_create_access(principal, COLLECTIONS_CREATE, tags)

    def register_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if not files or len(files) > 100:
            raise BadRequest("collection upload file batch must contain 1 to 100 files")

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
            normalized_files = tuple(
                _normalize_file(
                    value,
                    policy=checkpoint.policy,
                    provenance_mode=upload.provenance_mode,
                    collection_omission_reason=upload.provenance_omission_reason,
                )
                for value in files
            )
            if list(normalized_files) != sorted(normalized_files, key=lambda item: item["path"]):
                raise BadRequest("collection upload files must be in canonical path order")
            existing = {
                row.path: row
                for row in session.scalars(
                    select(CollectionUploadFileRecord).where(
                        CollectionUploadFileRecord.collection_id == normalized_id
                    )
                )
            }
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
                last_path = max(existing) if existing else None
                if last_path is not None and new_files[0]["path"].encode() <= last_path.encode():
                    raise Conflict("collection upload file registration is not append-only")
            ordered: list[OrderedArchiveFile] = []
            next_order = checkpoint.next_file_order
            for current in new_files:
                session.add(
                    CollectionUploadFileRecord(
                        collection_id=normalized_id,
                        path=current["path"],
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
            batch = advance_incremental_volume_plan(checkpoint, ordered)
            _persist_plan_batch(session, upload=upload, batch=batch)
            upload.planner_checkpoint_json = incremental_volume_planner_checkpoint_bytes(
                batch.checkpoint
            ).decode("utf-8")
            upload.last_activity_at = utc_timestamp_now()
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
        normalized_id = _collection_id(collection_id)
        if len(content) > 64 * 1024 * 1024:
            raise BadRequest("provenance journal exceeds the 64 MiB per-journal limit")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise BadRequest("provenance journal SHA-256 does not match its content")
        try:
            summary = validate_journal(content)
        except ProvenanceValidationError as exc:
            raise BadRequest(str(exc)) from exc
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
                if existing.sha256 != sha256 or existing.journal_bytes != content:
                    raise Conflict("provenance journal already has different exact bytes")
                return _journal_payload(existing)
            record = CollectionUploadProvenanceJournalRecord(
                collection_id=normalized_id,
                journal_id=journal_id,
                journal_bytes=content,
                bytes=len(content),
                sha256=sha256,
                current_state_id=summary.current_state_id,
                current_path=summary.current_path,
                current_bytes=summary.current_bytes,
                current_sha256=summary.current_sha256,
            )
            session.add(record)
            upload.last_activity_at = utc_timestamp_now()
            session.flush()
            return _journal_payload(record)

    def complete(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_etag: str,
        provenance_etag: str | None = None,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if files_total < 1 or _SHA256_RE.fullmatch(content_etag) is None:
            raise BadRequest("collection upload completion identity is invalid")
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_id)
            if collection is not None:
                if (
                    collection.content_etag != content_etag
                    or collection.provenance_etag != provenance_etag
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
            if upload.state not in {"open", "uploading", "finalizing", "failed"}:
                raise Conflict(f"collection upload session is {upload.state}: {normalized_id}")
            if upload.state == "finalizing":
                return _upload_payload(session, upload)
            rows = list(
                session.scalars(
                    select(CollectionUploadFileRecord)
                    .where(CollectionUploadFileRecord.collection_id == normalized_id)
                    .order_by(CollectionUploadFileRecord.file_order)
                )
            )
            actual_etag = collection_content_etag_ordered(
                (row.path, row.bytes, row.sha256) for row in rows
            )
            if len(rows) != files_total or actual_etag != content_etag:
                raise Conflict("collection upload registered manifest differs from completion")
            provenance = _upload_provenance_archive(upload)
            actual_provenance_etag = provenance.identity if provenance is not None else None
            if provenance_etag != actual_provenance_etag:
                raise Conflict("collection upload provenance identity differs from completion")
            checkpoint = _planner_checkpoint(upload)
            if not checkpoint.closed:
                batch = advance_incremental_volume_plan(checkpoint, (), final=True)
                _persist_plan_batch(session, upload=upload, batch=batch)
                upload.planner_checkpoint_json = incremental_volume_planner_checkpoint_bytes(
                    batch.checkpoint
                ).decode("utf-8")
            try:
                pack_plans: list[PackVolumePlan] = []
                raw_plans: list[RawVolumePlan] = []
                for record in sorted(upload.archive_objects, key=lambda item: item.sequence):
                    if record.kind == "pack":
                        pack_plans.append(parse_pack_volume_plan(record.plan_json))
                    else:
                        raw_plans.append(parse_raw_volume_plan(record.plan_json))
                validate_collection_archive_plan(
                    files=tuple(
                        ArchiveFile(path=row.path, bytes=row.bytes, sha256=row.sha256)
                        for row in rows
                    ),
                    packs=pack_plans,
                    raw_volumes=raw_plans,
                )
            except ValueError as exc:
                raise Conflict(
                    "collection upload volume plans differ from registered files"
                ) from exc
            upload.state = "uploading"
            upload.provenance_etag = actual_provenance_etag
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
        all_items: bool,
    ) -> dict[str, object]:
        normalized_id = _collection_id(collection_id)
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("invalid collection upload file pagination")
        with session_scope(self._session_factory) as session:
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
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.scalars(statement))
            return {
                "page": 1 if all_items else page,
                "per_page": total if all_items and total else per_page,
                "total": total,
                "pages": 1 if all_items and total else (total + per_page - 1) // per_page,
                "files": [_file_payload(row) for row in rows],
            }

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
            if upload.state not in {"open", "uploading", "failed"}:
                raise Conflict(f"collection upload session is {upload.state}: {normalized_id}")
            if plan_sha256 != record.plan_sha256:
                raise Conflict("archive upload unit plan identity changed")
            store_name = upload.archive_store
            kind = record.kind
            object_path = record.object_path
            relative_path = record.relative_path
            plan_json = record.plan_json
            unit_plaintext_bytes = record.unit_plaintext_bytes

        store = self._archive_stores.require(store_name)
        receipt: SealedPackVolume | SealedRawVolume | None
        try:
            if kind == "pack":
                pack_plan = parse_pack_volume_plan(plan_json)
                descriptor = pack_unit_descriptors(pack_plan)[unit]
                if len(content) != descriptor.payload_bytes:
                    raise ValueError("pack upload unit payload length mismatch")
                pack_uploader = self._pack_uploader(store.multipart_objects)
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
                raw_uploader = self._raw_uploader(store.multipart_objects)
                raw_checkpoint = raw_uploader.open(
                    collection_id=normalized_id,
                    plan=raw_plan,
                    object_path=object_path,
                    relative_path=relative_path,
                    target_part_plaintext_bytes=unit_plaintext_bytes,
                    expected_part_sha256s=expected,
                )
                raw_checkpoint = raw_uploader.upload_part(
                    plan=raw_plan,
                    checkpoint=raw_checkpoint,
                    part_number=unit + 1,
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
        all_items: bool,
        principal: ApplicationPrincipal,
    ) -> dict[str, object]:
        if page < 1 or per_page < 1 or per_page > 100:
            raise BadRequest("invalid collection upload pagination")
        if sort not in {"id", "created_at", "state", "bytes", "files"}:
            raise BadRequest("invalid collection upload sort")
        if order not in {"asc", "desc"}:
            raise BadRequest("collection upload order must be asc or desc")
        with session_scope(self._session_factory) as session:
            filters: list[Any] = [CollectionUploadRecord.initiated_by_app == principal.app]
            if q:
                pattern = f"%{q.casefold()}%"
                filters.append(
                    or_(
                        func.lower(func.coalesce(CollectionUploadRecord.ingest_source, "")).like(
                            pattern
                        ),
                        exists(
                            select(1).where(
                                CollectionUploadTagRecord.collection_id
                                == CollectionUploadRecord.collection_id,
                                func.lower(CollectionUploadTagRecord.tag_id).like(pattern),
                            )
                        ),
                    )
                )
            if tag:
                filters.append(
                    exists(
                        select(1).where(
                            CollectionUploadTagRecord.collection_id
                            == CollectionUploadRecord.collection_id,
                            CollectionUploadTagRecord.tag_id == tag,
                        )
                    )
                )
            if state:
                filters.append(CollectionUploadRecord.state == state)
            stats = (
                select(
                    CollectionUploadFileRecord.collection_id.label("collection_id"),
                    func.count(CollectionUploadFileRecord.path).label("files"),
                    func.coalesce(func.sum(CollectionUploadFileRecord.bytes), 0).label("bytes"),
                )
                .group_by(CollectionUploadFileRecord.collection_id)
                .subquery()
            )
            statement = (
                select(CollectionUploadRecord, stats.c.files, stats.c.bytes)
                .outerjoin(
                    stats,
                    stats.c.collection_id == CollectionUploadRecord.collection_id,
                )
                .where(*filters)
            )
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            direction = asc if order == "asc" else desc
            sort_column = {
                "id": CollectionUploadRecord.collection_id,
                "created_at": CollectionUploadRecord.opened_at,
                "state": CollectionUploadRecord.state,
                "bytes": stats.c.bytes,
                "files": stats.c.files,
            }[sort]
            statement = statement.order_by(
                direction(sort_column),
                asc(CollectionUploadRecord.collection_id),
            )
            if not all_items:
                statement = statement.offset((page - 1) * per_page).limit(per_page)
            rows = list(session.execute(statement))
            return {
                "page": 1 if all_items else page,
                "per_page": total if all_items else per_page,
                "total": total,
                "pages": 1 if all_items and total else (total + per_page - 1) // per_page,
                "sort": sort,
                "order": order,
                "query": q,
                "filters": {"tag": tag, "state": state},
                "uploads": [
                    {
                        "collection_id": upload.collection_id,
                        "created_at": upload.opened_at,
                        "tags": list(_upload_tags(upload)),
                        "ingest_source": upload.ingest_source,
                        "archive_store": upload.archive_store,
                        "state": upload.state,
                        "files": int(files or 0),
                        "bytes": int(byte_count or 0),
                        "uploaded_bytes": _custodied_bytes(session, upload.collection_id),
                    }
                    for upload, files, byte_count in rows
                ],
            }

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
            store_name = upload.archive_store
            prefix = upload.archive_storage_prefix
            checkpoints = [
                (current.kind, current.checkpoint_json)
                for current in upload.archive_objects
                if current.checkpoint_json
            ]
        store = self._archive_stores.require(store_name)
        for kind, checkpoint_json in checkpoints:
            if checkpoint_json is None:
                continue
            if kind == "pack":
                pack_checkpoint = PackUploadCheckpoint.from_json(checkpoint_json)
                if pack_checkpoint.completed is None:
                    self._pack_uploader(store.multipart_objects).abort(pack_checkpoint)
            else:
                raw_checkpoint = RawUploadCheckpoint.from_json(checkpoint_json)
                if raw_checkpoint.completed is None:
                    self._raw_uploader(store.multipart_objects).abort(raw_checkpoint)
        with session_scope(self._session_factory) as session:
            upload = session.get(CollectionUploadRecord, normalized_id)
            if upload is not None:
                session.delete(upload)
        if prefix:
            self._archive_stores.require(store_name).store.discard_collection_archive_upload(
                archive_storage_prefix=prefix
            )
        return payload

    def _pack_uploader(self, object_store: object) -> PackVolumeUploader:
        return PackVolumeUploader(
            object_store=object_store,  # type: ignore[arg-type]
            checkpoint_store=self._checkpoints,
            passphrase=self._config.archive_passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
            source_read_chunk_bytes=self._throughput.source_read_chunk_bytes,
            resources=self._resources,
            timing_observer=log_transfer_timing,
            session_cache=self._age_sessions,
        )

    def _raw_uploader(self, object_store: object) -> RawVolumeUploader:
        return RawVolumeUploader(
            object_store=object_store,  # type: ignore[arg-type]
            checkpoint_store=self._checkpoints,
            passphrase=self._config.archive_passphrase,
            scrypt_log_n=self._config.archive_scrypt_work_factor,
            source_read_chunk_bytes=self._throughput.source_read_chunk_bytes,
            resources=self._resources,
            timing_observer=log_transfer_timing,
            session_cache=self._age_sessions,
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
            record.uploaded_parts = len(receipt.parts)
            record.uploaded_bytes = receipt.stored_bytes
            upload = session.get(CollectionUploadRecord, collection_id)
            if upload is not None:
                upload.last_activity_at = now
                upload.archive_phase_updated_at = now

    def requeue_interrupted_finalizations_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        now = utc_timestamp_now()
        requeued = 0
        with session_scope(self._session_factory) as session:
            uploads = list(
                session.scalars(
                    select(CollectionUploadRecord)
                    .where(CollectionUploadRecord.state.in_(("uploading", "finalizing", "failed")))
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
                upload.archive_phase = "retry_wait"
                upload.archive_next_attempt_at = now
                upload.archive_phase_updated_at = now
                if interrupted:
                    upload.archive_failure = "archive finalization interrupted before completion"
                requeued += 1
        return requeued

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
                    .where(CollectionUploadRecord.state.in_(("uploading", "failed")))
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
                    CollectionUploadRecord.archive_phase == "retry_wait",
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
        store = self._archive_stores.require(store_name)
        for volume_id, kind, plan_json, checkpoint_json in pending:
            if checkpoint_json is None:
                raise RuntimeError(f"sealed archive volume has no checkpoint: {volume_id}")
            if kind == "pack":
                checkpoint = PackUploadCheckpoint.from_json(checkpoint_json)
                if checkpoint.completed is None:
                    raise RuntimeError(f"sealed pack volume is incomplete: {volume_id}")
                receipt: SealedPackVolume | SealedRawVolume = self._pack_uploader(
                    store.multipart_objects
                ).sealed_receipt(
                    plan=parse_pack_volume_plan(plan_json),
                    checkpoint=checkpoint,
                )
            elif kind == "segment":
                raw_checkpoint = RawUploadCheckpoint.from_json(checkpoint_json)
                if not raw_checkpoint.completed:
                    raise RuntimeError(f"sealed raw volume is incomplete: {volume_id}")
                receipt = self._raw_uploader(store.multipart_objects).sealed_receipt(raw_checkpoint)
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
            provenance = _upload_provenance_archive(upload)

        archive_store = self._archive_stores.require(store_name)
        sealed_provenance = (
            ArchiveProvenancePublisher(
                object_store=archive_store.immutable_objects,
                passphrase=self._config.archive_passphrase,
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
            passphrase=self._config.archive_passphrase,
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
        proof_bytes = self._persisted_proof(collection_id, root.manifest_bytes)
        proof_ciphertext = encrypt_age_scrypt(
            proof_bytes,
            self._config.archive_passphrase,
            log_n=self._config.archive_scrypt_work_factor,
        )
        proof_receipt = archive_store.immutable_objects.put_immutable_object(
            object_path=f"{prefix}/{_PROOF_RELATIVE_PATH}",
            content=proof_ciphertext,
            content_type=_PROOF_CONTENT_TYPE,
            identity_metadata={
                "riverhog-format": ROOT_PROOF_STORAGE_FORMAT,
                "riverhog-plaintext-bytes": str(len(proof_bytes)),
                "riverhog-plaintext-sha256": hashlib.sha256(proof_bytes).hexdigest(),
                "riverhog-manifest-sha256": root.plaintext_sha256,
            },
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
            rows = sorted(upload.files, key=lambda item: item.file_order)
            file_entries = [(row.path, row.bytes, row.sha256) for row in rows]
            content_etag = collection_content_etag_ordered(file_entries)
            tags = _upload_tags(upload)
            now = utc_timestamp_now()
            _, record_etag = collection_record_manifest(
                collection_id=collection_id,
                content_etag=content_etag,
                provenance_mode=_final_provenance_mode(upload),
                provenance_etag=upload.provenance_etag,
                metadata_revision=1,
                tags=tags,
                files=file_entries,
            )
            collection = CollectionRecord(
                id=collection_id,
                creation_idempotency_key=upload.idempotency_key,
                content_etag=content_etag,
                provenance_mode=_final_provenance_mode(upload),
                provenance_etag=upload.provenance_etag,
                record_etag=record_etag,
                metadata_revision=1,
                metadata_updated_at=now,
                ingest_source=upload.ingest_source,
                created_by_app=upload.initiated_by_app,
                created_by_key_id=upload.initiated_by_key_id,
                created_at=upload.opened_at or now,
            )
            session.add(collection)
            session.flush()
            collection.files.extend(
                CollectionFileRecord(
                    collection_id=collection_id,
                    path=row.path,
                    bytes=row.bytes,
                    sha256=row.sha256,
                )
                for row in rows
            )
            journal_projections: dict[str, ProvenanceJournalProjection] = {}
            for journal in upload.provenance_journals:
                journal_projection = provenance_journal_projection(
                    collection_id=collection_id,
                    journal_id=journal.journal_id,
                    summary=validate_journal(journal.journal_bytes),
                )
                journal_projections[journal.journal_id] = journal_projection
                session.add(
                    CollectionProvenanceJournalRecord(
                        collection_id=collection_id,
                        journal_id=journal.journal_id,
                        journal_bytes=journal.journal_bytes,
                        bytes=journal.bytes,
                        sha256=journal.sha256,
                        entries=len(journal_projection.summary.frames),
                        agent_ids_json=json.dumps(
                            sorted(journal_projection.summary.agent_ids), separators=(",", ":")
                        ),
                        entity_counts_json=journal_projection.entity_counts_json,
                        current_state_id=journal.current_state_id,
                        current_path=journal.current_path,
                        current_bytes=journal.current_bytes,
                        current_sha256=journal.current_sha256,
                    )
                )
            session.flush()
            session.add_all(
                CollectionFileProvenanceRecord(
                    collection_id=collection_id,
                    path=row.path,
                    status=row.provenance_status,
                    journal_id=row.provenance_journal_id,
                    current_state_id=row.provenance_current_state_id,
                    omission_reason=row.provenance_omission_reason,
                )
                for row in rows
            )
            for journal in upload.provenance_journals:
                journal_projection = journal_projections[journal.journal_id]
                session.add_all(journal_projection.entities)
                session.add_all(journal_projection.lineage_edges)
            collection.tags.extend(
                CollectionTagRecord(
                    collection_id=collection_id,
                    tag_id=tag,
                    assigned_by_app=upload.initiated_by_app,
                    assigned_by_key_id=upload.initiated_by_key_id,
                    assigned_at=now,
                )
                for tag in tags
            )
            store_config = self._config.archive_store(upload.archive_store)
            copy = CollectionArchiveCopyRecord(
                collection_id=collection_id,
                store=upload.archive_store,
                state="uploaded",
                archive_storage_prefix=projection.root.archive_storage_prefix,
                backend=store_config.backend,
                storage_class=store_config.storage_class,
                last_uploaded_at=now,
                last_verified_at=now,
            )
            session.add(copy)
            session.flush()
            for volume in projection.volumes:
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
                        version_id=volume.version_id,
                        age_state_json=volume.age_state_json,
                        part_receipts_json=volume.part_receipts_json,
                        plan_sha256=volume.plan_sha256,
                        index_sha256=volume.index_sha256,
                        backend=store_config.backend,
                        storage_class=store_config.storage_class,
                        uploaded_at=volume.completed_at,
                        verified_at=now,
                    )
                )
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
                            object_path=f"{projection.root.archive_storage_prefix}/{current.relative_path}",
                            plaintext_bytes=current.plaintext_bytes,
                            stored_bytes=current.stored_bytes,
                            sha256=current.plaintext_sha256,
                            stored_sha256=current.stored_sha256,
                            version_id=current.version_id,
                            backend=store_config.backend,
                            storage_class=store_config.storage_class,
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
                        version_id=root.version_id,
                        backend=store_config.backend,
                        storage_class=store_config.storage_class,
                        uploaded_at=root.completed_at,
                        verified_at=now,
                    ),
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=upload.archive_store,
                        object_id="proof",
                        object_order=artifact_order + 1,
                        kind="proof",
                        object_path=proof_receipt.object_path,
                        plaintext_bytes=len(proof_bytes),
                        stored_bytes=proof_receipt.stored_bytes,
                        sha256=hashlib.sha256(proof_bytes).hexdigest(),
                        stored_sha256=proof_receipt.stored_sha256,
                        version_id=proof_receipt.version_id,
                        backend=store_config.backend,
                        storage_class=store_config.storage_class,
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
            record_catalog_event(
                session,
                change="created",
                collection_id=collection_id,
                occurred_at=now,
                record_etag=record_etag,
                before_tags=(),
                after_tags=tags,
            )
            self._events.emit_collection(
                type="collection.finalized",
                collection_id=collection_id,
                details={
                    "files_total": len(rows),
                    "bytes_total": sum(row.bytes for row in rows),
                    "archive_store": upload.archive_store,
                    "archive_storage_prefix": projection.root.archive_storage_prefix,
                    "archive_objects": (
                        len(projection.volumes)
                        + (len(sealed_provenance.bundles) + 1 if sealed_provenance else 0)
                        + 2
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


def _normalize_tags(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(value.strip().casefold() for value in values))
    if any(not value for value in normalized):
        raise BadRequest("collection tags must not be empty")
    if len(set(normalized)) != len(normalized):
        raise BadRequest("collection tags must not contain duplicates")
    return normalized


def _require_tags(session: Session, tags: Sequence[str]) -> None:
    found = set(session.scalars(select(TagRecord.id).where(TagRecord.id.in_(tags))))
    missing = sorted(set(tags) - found)
    if missing:
        raise BadRequest(f"collection tags do not exist: {', '.join(missing)}")


def _normalize_file(
    value: Mapping[str, object],
    *,
    policy: CollectionVolumePolicy,
    provenance_mode: str,
    collection_omission_reason: str | None,
) -> _RegisteredFile:
    try:
        path = normalize_relpath(str(value.get("path", "")))
    except PathNormalizationError as exc:
        raise BadRequest(str(exc)) from exc
    byte_count = value.get("bytes")
    sha256 = str(value.get("sha256", ""))
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or _SHA256_RE.fullmatch(sha256) is None
    ):
        raise BadRequest(f"collection upload file identity is invalid: {path}")
    raw = value.get("raw_parts")
    raw_json: str | None = None
    if byte_count >= policy.pack_member_bytes:
        if not isinstance(raw, Mapping):
            raise BadRequest(f"raw part digests are required for large file: {path}")
        part_bytes = raw.get("part_plaintext_bytes")
        digests = raw.get("sha256s")
        if part_bytes != policy.raw_part_plaintext_bytes or not isinstance(digests, list):
            raise BadRequest(f"raw part digest policy does not match the session: {path}")
        try:
            raw_json = (
                RawSourceDigestManifest(
                    path=path,
                    bytes=byte_count,
                    sha256=sha256,
                    part_plaintext_bytes=part_bytes,
                    part_sha256s=tuple(str(current) for current in digests),
                )
                .to_json_bytes()
                .decode("utf-8")
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
    elif raw is not None:
        raise BadRequest(f"raw part digests are only valid for large files: {path}")
    raw_provenance = value.get("provenance")
    if raw_provenance is None and provenance_mode == "omitted":
        raw_provenance = {
            "status": "omitted",
            "omission_reason": collection_omission_reason,
        }
    if not isinstance(raw_provenance, Mapping):
        raise BadRequest(f"collection upload file provenance is required: {path}")
    status = raw_provenance.get("status")
    provenance_journal_id: str | None = None
    provenance_current_state_id: str | None = None
    provenance_omission_reason: str | None = None
    if status == "captured":
        if provenance_mode == "omitted":
            raise BadRequest("collection-wide provenance omission cannot contain journals")
        if set(raw_provenance) != {"status", "journal_id", "current_state_id"}:
            raise BadRequest(f"captured file provenance is invalid: {path}")
        provenance_journal_id = str(raw_provenance.get("journal_id", ""))
        provenance_current_state_id = str(raw_provenance.get("current_state_id", ""))
        if not provenance_journal_id or not provenance_current_state_id:
            raise BadRequest(f"captured file provenance identity is invalid: {path}")
    elif status == "omitted":
        if set(raw_provenance) != {"status", "omission_reason"}:
            raise BadRequest(f"omitted file provenance is invalid: {path}")
        provenance_omission_reason = str(raw_provenance.get("omission_reason", ""))
        if (
            not provenance_omission_reason
            or provenance_omission_reason != provenance_omission_reason.strip()
        ):
            raise BadRequest(f"file provenance omission requires a visible reason: {path}")
    else:
        raise BadRequest(f"collection upload file provenance status is invalid: {path}")
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
) -> tuple[str, str | None]:
    if mode == "captured" and omission_reason is None:
        return mode, None
    if mode == "omitted" and omission_reason:
        normalized = omission_reason.strip()
        if normalized == omission_reason:
            return mode, normalized
    raise BadRequest("provenance_mode must be captured, or omitted with provenance_omission_reason")


def _upload_provenance_archive(
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
    journals = {row.journal_id: row.journal_bytes for row in upload.provenance_journals}
    if upload.provenance_mode == "omitted":
        if journals or any(item.status != "omitted" for item in bindings):
            raise Conflict("collection-wide provenance omission is not internally consistent")
        return None
    try:
        return build_provenance_archive(bindings=bindings, journals=journals)
    except ProvenanceValidationError as exc:
        raise Conflict(str(exc)) from exc


def _final_provenance_mode(upload: CollectionUploadRecord) -> str:
    if upload.provenance_mode == "omitted":
        return "omitted"
    return (
        "mixed" if any(item.provenance_status == "omitted" for item in upload.files) else "captured"
    )


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
                uploaded_parts=0,
                total_parts=len(plan.units),
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
                uploaded_parts=0,
                total_parts=max(
                    1,
                    (plan.plaintext_bytes + batch.checkpoint.policy.raw_part_plaintext_bytes - 1)
                    // batch.checkpoint.policy.raw_part_plaintext_bytes,
                ),
                updated_at=now,
            )
        )
    session.flush()


def _upload_tags(upload: CollectionUploadRecord) -> tuple[str, ...]:
    return tuple(sorted(current.tag_id for current in upload.tags))


def _ready_for_finalization(upload: CollectionUploadRecord) -> bool:
    checkpoint = _planner_checkpoint(upload)
    return bool(
        checkpoint.closed
        and upload.archive_objects
        and all(current.state == "sealed" for current in upload.archive_objects)
    )


def _mark_finalization_ready(upload: CollectionUploadRecord, *, now: str) -> None:
    upload.state = "finalizing"
    upload.archive_phase = "retry_wait"
    upload.archive_phase_updated_at = now
    upload.archive_next_attempt_at = now


def _layout_payload(policy: CollectionVolumePolicy) -> dict[str, int]:
    return {
        "pack_source_bytes": policy.pack_source_bytes,
        "pack_files": policy.pack_files,
        "pack_member_bytes": policy.pack_member_bytes,
        "pack_part_plaintext_bytes": policy.pack_part_plaintext_bytes,
        "raw_volume_plaintext_bytes": policy.raw_volume_plaintext_bytes,
        "raw_part_plaintext_bytes": policy.raw_part_plaintext_bytes,
    }


def _file_payload(record: CollectionUploadFileRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "bytes": record.bytes,
        "sha256": record.sha256,
        "upload_state": "registered",
        "uploaded_bytes": 0,
        "upload_state_expires_at": None,
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


def _part_payload(part: StoredPartReceipt) -> dict[str, object]:
    return {
        "number": part.number,
        "plaintext_start": part.plaintext_start,
        "plaintext_bytes": part.plaintext_bytes,
        "plaintext_sha256": part.plaintext_sha256,
        "stored_bytes": part.stored_bytes,
        "stored_sha256": part.stored_sha256,
        "etag": part.etag,
    }


def _unit_states(record: CollectionArchiveObjectUploadRecord) -> set[int]:
    if not record.checkpoint_json:
        return set()
    checkpoint = (
        PackUploadCheckpoint.from_json(record.checkpoint_json)
        if record.kind == "pack"
        else RawUploadCheckpoint.from_json(record.checkpoint_json)
    )
    return {current.number - 1 for current in checkpoint.parts}


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
                        "sha256": source.sha256,
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
        for unit in range(record.total_parts):
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
                            "sha256": raw_plan.file_sha256,
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
        "version_id": receipt.version_id,
        "completed_at": receipt.completed_at,
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


def _parse_parts(values: Sequence[Mapping[str, object]]) -> tuple[StoredPartReceipt, ...]:
    return tuple(
        StoredPartReceipt(
            number=_stored_int(value["number"], "part number"),
            plaintext_start=_stored_int(value["plaintext_start"], "part plaintext start"),
            plaintext_bytes=_stored_int(value["plaintext_bytes"], "part plaintext bytes"),
            plaintext_sha256=str(value["plaintext_sha256"]),
            stored_bytes=_stored_int(value["stored_bytes"], "part stored bytes"),
            stored_sha256=str(value["stored_sha256"]),
            etag=str(value["etag"]),
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
        version_id=str(value["version_id"]) if value["version_id"] is not None else None,
        completed_at=str(value["completed_at"]),
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
        version_id=str(value["version_id"]) if value["version_id"] is not None else None,
        completed_at=str(value["completed_at"]),
    )


def _custodied_bytes(session: Session, collection_id: int) -> int:
    return int(
        session.scalar(
            select(
                func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.source_bytes), 0)
            ).where(
                CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                CollectionArchiveObjectUploadRecord.state == "sealed",
            )
        )
        or 0
    )


def _upload_payload(
    session: Session,
    upload: CollectionUploadRecord,
    *,
    state: str | None = None,
) -> dict[str, object]:
    files_total, bytes_total = session.execute(
        select(
            func.count(CollectionUploadFileRecord.path),
            func.coalesce(func.sum(CollectionUploadFileRecord.bytes), 0),
        ).where(CollectionUploadFileRecord.collection_id == upload.collection_id)
    ).one()
    uploaded_bytes = _custodied_bytes(session, upload.collection_id)
    archive_progress = session.execute(
        select(
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_bytes), 0),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.uploaded_parts), 0),
            func.coalesce(func.sum(CollectionArchiveObjectUploadRecord.total_parts), 0),
        ).where(CollectionArchiveObjectUploadRecord.collection_id == upload.collection_id)
    ).one()
    return {
        "collection_id": upload.collection_id,
        "created_at": upload.opened_at,
        "tags": list(_upload_tags(upload)),
        "ingest_source": upload.ingest_source,
        "provenance_mode": upload.provenance_mode,
        "provenance_etag": upload.provenance_etag,
        "archive_store": upload.archive_store,
        "state": state or upload.state,
        "layout": _layout_payload(_planner_checkpoint(upload).policy),
        "files_total": int(files_total),
        "files_pending": int(files_total) if uploaded_bytes == 0 else 0,
        "files_partial": 0,
        "files_uploaded": int(files_total) if upload.state == "finalized" else 0,
        "bytes_total": int(bytes_total),
        "uploaded_bytes": min(int(bytes_total), uploaded_bytes),
        "missing_bytes": max(0, int(bytes_total) - uploaded_bytes),
        "upload_state_expires_at": None,
        "latest_failure": upload.archive_failure,
        "archive_phase": upload.archive_phase,
        "archive_phase_updated_at": upload.archive_phase_updated_at,
        "archive_storage_prefix": upload.archive_storage_prefix,
        "archive_uploaded_bytes": int(archive_progress[0]),
        "archive_total_bytes": None,
        "archive_uploaded_parts": int(archive_progress[1]),
        "archive_total_parts": int(archive_progress[2]),
        "collection": None,
    }


def _finalized_payload(
    session: Session,
    collection: CollectionRecord,
    *,
    store_name: str,
) -> dict[str, object]:
    copy = next(
        (current for current in collection.archive_copies if current.store == store_name),
        collection.archive_copies[0] if collection.archive_copies else None,
    )
    tags = sorted(current.tag_id for current in collection.tags)
    stored_bytes = sum(current.stored_bytes for current in copy.objects) if copy else 0
    summary = {
        "id": collection.id,
        "created_at": collection.created_at,
        "tags": tags,
        "files": len(collection.files),
        "bytes": sum(current.bytes for current in collection.files),
        "remote_storage_bytes": stored_bytes,
        "archive_copies": [],
    }
    return {
        "collection_id": collection.id,
        "created_at": collection.created_at,
        "tags": tags,
        "ingest_source": collection.ingest_source,
        "provenance_mode": collection.provenance_mode,
        "provenance_etag": collection.provenance_etag,
        "archive_store": copy.store if copy else store_name,
        "state": "finalized",
        "layout": None,
        "files_total": summary["files"],
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": summary["files"],
        "bytes_total": summary["bytes"],
        "uploaded_bytes": summary["bytes"],
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "latest_failure": None,
        "archive_phase": "completed",
        "archive_phase_updated_at": copy.last_verified_at if copy else None,
        "archive_storage_prefix": copy.archive_storage_prefix if copy else None,
        "archive_uploaded_bytes": stored_bytes,
        "archive_total_bytes": stored_bytes,
        "archive_uploaded_parts": None,
        "archive_total_parts": None,
        "collection": summary,
    }
