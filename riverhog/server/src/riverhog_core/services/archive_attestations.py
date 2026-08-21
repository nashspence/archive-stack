from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, select, update
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_attestations import (
    AttestationSigner,
    AttestationVerifier,
    CommandAttestationSigner,
    CommandAttestationVerifier,
    archive_copy_checksums,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionArchiveAttestationRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionProofMaturationRecord,
    RetrievalJobObjectRecord,
    RetrievalJobRecord,
)
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveObjectUploadReceipt,
    CollectionArchiveUploadReceipt,
)
from riverhog_core.proofs import (
    CommandProofStamper,
    CommandProofUpgrader,
    CommandProofVerifier,
    ProofStamper,
    ProofUpgrader,
    ProofVerifier,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_mutations import require_collection_archive_idle

_LOG = logging.getLogger(__name__)


class SqlAlchemyArchiveAttestationService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        signer: AttestationSigner | None = None,
        signature_verifier: AttestationVerifier | None = None,
        proof_stamper: ProofStamper | None = None,
        proof_upgrader: ProofUpgrader | None = None,
        proof_verifier: ProofVerifier | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        configured = (
            config.attestation_secret_key_file is not None
            and config.attestation_public_key_file is not None
        )
        self._enabled = configured or (signer is not None and signature_verifier is not None)
        self._signer: AttestationSigner | None
        if signer is not None:
            self._signer = signer
        elif configured:
            assert config.attestation_secret_key_file is not None
            self._signer = CommandAttestationSigner(config.attestation_secret_key_file)
        else:
            self._signer = None
        self._signature_verifier: AttestationVerifier | None
        if signature_verifier is not None:
            self._signature_verifier = signature_verifier
        elif configured:
            assert config.attestation_public_key_file is not None
            self._signature_verifier = CommandAttestationVerifier(
                config.attestation_public_key_file
            )
        else:
            self._signature_verifier = None
        self._proof_stamper = proof_stamper or CommandProofStamper(config.ots_stamp_command)
        self._proof_upgrader = proof_upgrader or CommandProofUpgrader(config.ots_upgrade_command)
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def requeue_interrupted_for_startup(self) -> int:
        if not self._enabled:
            return 0
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            publishing = session.execute(
                update(CollectionArchiveAttestationRecord)
                .where(CollectionArchiveAttestationRecord.state == "publishing")
                .values(
                    state="pending",
                    next_attempt_at=now,
                    failure="archive attestation publication interrupted before completion",
                )
            )
            upgrading = session.execute(
                update(CollectionArchiveAttestationRecord)
                .where(CollectionArchiveAttestationRecord.state == "upgrading")
                .values(
                    state="waiting",
                    next_attempt_at=now,
                    failure="archive attestation proof upgrade interrupted before completion",
                )
            )
            return int(getattr(publishing, "rowcount", 0) or 0) + int(
                getattr(upgrading, "rowcount", 0) or 0
            )

    def schedule_missing(self, *, limit: int = 1000) -> int:
        if not self._enabled or limit < 1:
            return 0
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(
                    CollectionArchiveCopyRecord.collection_id,
                    CollectionArchiveCopyRecord.store,
                )
                .join(
                    CollectionProofMaturationRecord,
                    and_(
                        CollectionProofMaturationRecord.collection_id
                        == CollectionArchiveCopyRecord.collection_id,
                        CollectionProofMaturationRecord.store == CollectionArchiveCopyRecord.store,
                    ),
                )
                .outerjoin(
                    CollectionArchiveAttestationRecord,
                    and_(
                        CollectionArchiveAttestationRecord.collection_id
                        == CollectionArchiveCopyRecord.collection_id,
                        CollectionArchiveAttestationRecord.store
                        == CollectionArchiveCopyRecord.store,
                    ),
                )
                .where(
                    CollectionArchiveCopyRecord.state == "uploaded",
                    CollectionProofMaturationRecord.state == "matured",
                    CollectionArchiveAttestationRecord.collection_id.is_(None),
                )
                .order_by(
                    CollectionArchiveCopyRecord.collection_id,
                    CollectionArchiveCopyRecord.store,
                )
                .limit(limit)
            ).all()
            for collection_id, store in rows:
                session.add(
                    CollectionArchiveAttestationRecord(
                        collection_id=int(collection_id),
                        store=str(store),
                        state="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                    )
                )
            return len(rows)

    def process_due(self, *, limit: int = 10) -> int:
        if not self._enabled or limit < 1:
            return 0
        self.schedule_missing()
        processed = 0
        for _ in range(limit):
            claimed = self._claim_due()
            if claimed is None:
                break
            collection_id, store, operation = claimed
            if operation == "publish":
                self._publish(collection_id=collection_id, store=store)
            else:
                self._mature(collection_id=collection_id, store=store)
            processed += 1
        return processed

    def _claim_due(self) -> tuple[int, str, str] | None:
        now = format_utc_timestamp(utc_now())
        active_retrieval = (
            select(RetrievalJobRecord.id)
            .join(RetrievalJobObjectRecord)
            .where(
                RetrievalJobObjectRecord.collection_id
                == CollectionArchiveAttestationRecord.collection_id,
                RetrievalJobObjectRecord.source_store == CollectionArchiveAttestationRecord.store,
                RetrievalJobRecord.state.in_(("requested", "ready")),
            )
            .exists()
        )
        active_deletion = (
            select(CollectionDeletionRecord.collection_id)
            .where(
                CollectionDeletionRecord.collection_id
                == CollectionArchiveAttestationRecord.collection_id
            )
            .exists()
        )
        active_retirement = (
            select(ArchiveCopyRetirementRecord.collection_id)
            .where(
                ArchiveCopyRetirementRecord.collection_id
                == CollectionArchiveAttestationRecord.collection_id,
                ArchiveCopyRetirementRecord.store == CollectionArchiveAttestationRecord.store,
            )
            .exists()
        )
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionArchiveAttestationRecord)
                .where(
                    CollectionArchiveAttestationRecord.state.in_(
                        ("pending", "waiting", "publish_retry", "upgrade_retry")
                    ),
                    CollectionArchiveAttestationRecord.next_attempt_at <= now,
                    ~active_retrieval,
                    ~active_deletion,
                    ~active_retirement,
                )
                .order_by(
                    CollectionArchiveAttestationRecord.next_attempt_at,
                    CollectionArchiveAttestationRecord.collection_id,
                    CollectionArchiveAttestationRecord.store,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if record is None:
                return None
            require_collection_archive_idle(session, record.collection_id)
            operation = "publish" if record.state in {"pending", "publish_retry"} else "upgrade"
            record.state = "publishing" if operation == "publish" else "upgrading"
            record.attempt_count += 1
            record.last_attempt_at = now
            record.failure = None
            return record.collection_id, record.store, operation

    def _publish(self, *, collection_id: int, store: str) -> None:
        try:
            archive_store = self._archive_stores.require(store).store
            copy, objects = self._copy_objects(collection_id=collection_id, store=store)
            for current in objects:
                if current.kind not in {
                    "pack",
                    "file",
                    "segment",
                    "provenance-bundle",
                    "provenance-index",
                    "manifest",
                    "proof",
                }:
                    continue
                if current.stored_sha256:
                    continue
                _LOG.info(
                    "hashing existing archive ciphertext: collection_id=%s store=%s object=%s "
                    "bytes=%s",
                    collection_id,
                    store,
                    current.object_id,
                    current.stored_bytes,
                )
                digest = archive_store.stored_archive_object_sha256(
                    collection_id=collection_id,
                    object=current,
                )
                self._record_stored_sha256(
                    collection_id=collection_id,
                    store=store,
                    object_id=current.object_id,
                    stored_sha256=digest,
                )
            copy, objects = self._copy_objects(collection_id=collection_id, store=store)
            checksums = archive_copy_checksums(
                archive_storage_prefix=copy.archive_storage_prefix or "",
                objects=objects,
            )
            assert self._signer is not None
            assert self._signature_verifier is not None
            signature = self._signer.sign(checksums)
            self._signature_verifier.verify(checksums=checksums, signature=signature)
            proof = self._stamp_signature(signature)
            receipt = archive_store.publish_archive_attestation(
                collection_id=collection_id,
                archive_storage_prefix=copy.archive_storage_prefix or "",
                checksums=checksums,
                signature=signature,
                proof=proof,
            )
            persisted = {
                current.object_id: archive_store.read_archive_attestation_artifact(
                    collection_id=collection_id,
                    object=_identity_from_receipt(current),
                )
                for current in receipt.objects
            }
            if persisted["checksums"].content != checksums:
                raise RuntimeError("persisted archive checksums differ from their input")
            if persisted["signature"].content != signature:
                raise RuntimeError("persisted archive signature differs from its input")
            self._signature_verifier.verify(
                checksums=persisted["checksums"].content,
                signature=persisted["signature"].content,
            )
            self._record_published(
                collection_id=collection_id,
                store=store,
                receipt=CollectionArchiveUploadReceipt(
                    objects=tuple(current.receipt for current in persisted.values())
                ),
            )
            _LOG.info(
                "collection archive attestation published: collection_id=%s store=%s",
                collection_id,
                store,
            )
        except Exception as exc:
            _LOG.exception(
                "collection archive attestation publication failed; retry scheduled: "
                "collection_id=%s store=%s",
                collection_id,
                store,
            )
            self._record_retry(
                collection_id=collection_id,
                store=store,
                state="publish_retry",
                exc=exc,
            )

    def _mature(self, *, collection_id: int, store: str) -> None:
        try:
            archive_store = self._archive_stores.require(store).store
            copy, objects = self._copy_objects(collection_id=collection_id, store=store)
            by_id = {current.object_id: current for current in objects}
            checksums = archive_store.read_archive_attestation_artifact(
                collection_id=collection_id,
                object=by_id["checksums"],
            )
            signature = archive_store.read_archive_attestation_artifact(
                collection_id=collection_id,
                object=by_id["signature"],
            )
            proof = archive_store.read_archive_attestation_artifact(
                collection_id=collection_id,
                object=by_id["signature-proof"],
            )
            expected = archive_copy_checksums(
                archive_storage_prefix=copy.archive_storage_prefix or "",
                objects=objects,
            )
            if checksums.content != expected:
                raise RuntimeError("archive checksums no longer match their copy")
            assert self._signature_verifier is not None
            self._signature_verifier.verify(
                checksums=checksums.content,
                signature=signature.content,
            )
            upgraded = self._proof_upgrader.upgrade(proof.content)
            if not upgraded.complete:
                self._record_waiting(collection_id=collection_id, store=store)
                return
            self._proof_verifier.verify(
                manifest_bytes=signature.content,
                proof_bytes=upgraded.proof_bytes,
            )
            receipt = archive_store.replace_archive_attestation_proof(
                collection_id=collection_id,
                object=by_id["signature-proof"],
                proof_bytes=upgraded.proof_bytes,
            )
            persisted = archive_store.read_archive_attestation_artifact(
                collection_id=collection_id,
                object=_identity_from_receipt(receipt),
            )
            if persisted.content != upgraded.proof_bytes:
                raise RuntimeError("persisted archive attestation proof differs from its input")
            self._proof_verifier.verify(
                manifest_bytes=signature.content,
                proof_bytes=persisted.content,
            )
            self._record_matured(
                collection_id=collection_id,
                store=store,
                receipt=persisted.receipt,
            )
            _LOG.info(
                "collection archive attestation matured: collection_id=%s store=%s",
                collection_id,
                store,
            )
        except Exception as exc:
            _LOG.exception(
                "collection archive attestation maturation failed; retry scheduled: "
                "collection_id=%s store=%s",
                collection_id,
                store,
            )
            self._record_retry(
                collection_id=collection_id,
                store=store,
                state="upgrade_retry",
                exc=exc,
            )

    def _copy_objects(
        self,
        *,
        collection_id: int,
        store: str,
    ) -> tuple[CollectionArchiveCopyRecord, tuple[ArchiveObjectIdentity, ...]]:
        with session_scope(self._session_factory) as session:
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if copy is None or copy.state != "uploaded" or not copy.archive_storage_prefix:
                raise RuntimeError("archive copy is not uploaded")
            rows = session.scalars(
                select(CollectionArchiveObjectRecord)
                .where(
                    CollectionArchiveObjectRecord.collection_id == collection_id,
                    CollectionArchiveObjectRecord.store == store,
                )
                .order_by(CollectionArchiveObjectRecord.object_order)
            ).all()
            return copy, tuple(_identity_from_record(current) for current in rows)

    def _stamp_signature(self, signature: bytes) -> bytes:
        with tempfile.TemporaryDirectory(prefix="riverhog-attestation-stamp-") as tmp:
            signature_path = Path(tmp) / "SHA256SUMS.minisig"
            signature_path.write_bytes(signature)
            return self._proof_stamper.stamp(signature_path).read_bytes()

    def _record_stored_sha256(
        self,
        *,
        collection_id: int,
        store: str,
        object_id: str,
        stored_sha256: str,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectRecord,
                (collection_id, store, object_id),
            )
            if record is None:
                raise RuntimeError("archive object disappeared while hashing")
            record.stored_sha256 = stored_sha256

    def _record_published(
        self,
        *,
        collection_id: int,
        store: str,
        receipt: CollectionArchiveUploadReceipt,
    ) -> None:
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionArchiveAttestationRecord, (collection_id, store))
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if record is None or copy is None:
                return
            existing = {current.object_id: current for current in copy.objects}
            next_order = max((current.object_order for current in copy.objects), default=-1) + 1
            for offset, current in enumerate(receipt.objects):
                object_record = existing.get(current.object_id)
                if object_record is None:
                    object_record = CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store=store,
                        object_id=current.object_id,
                        object_order=next_order + offset,
                    )
                    copy.objects.append(object_record)
                _apply_receipt(object_record, current)
            record.state = "waiting"
            record.next_attempt_at = _retry_at(self._config.proof_maturation_retry_delay)
            record.published_at = now
            record.failure = None
            copy.last_uploaded_at = max(
                copy.last_uploaded_at or now,
                *(current.uploaded_at for current in receipt.objects),
            )

    def _record_waiting(self, *, collection_id: int, store: str) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionArchiveAttestationRecord, (collection_id, store))
            if record is None:
                return
            record.state = "waiting"
            record.next_attempt_at = _retry_at(self._config.proof_maturation_retry_delay)
            record.failure = None

    def _record_retry(
        self,
        *,
        collection_id: int,
        store: str,
        state: str,
        exc: Exception,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionArchiveAttestationRecord, (collection_id, store))
            if record is None:
                return
            record.state = state
            record.next_attempt_at = _retry_at(self._config.proof_maturation_retry_delay)
            record.failure = f"{type(exc).__name__}: {exc}"[:1000]

    def _record_matured(
        self,
        *,
        collection_id: int,
        store: str,
        receipt: ArchiveObjectUploadReceipt,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionArchiveAttestationRecord, (collection_id, store))
            proof = session.get(
                CollectionArchiveObjectRecord,
                (collection_id, store, "signature-proof"),
            )
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if record is None or proof is None or copy is None:
                return
            _apply_receipt(proof, receipt)
            record.state = "matured"
            record.next_attempt_at = receipt.verified_at or receipt.uploaded_at
            record.matured_at = receipt.verified_at or receipt.uploaded_at
            record.failure = None
            copy.last_uploaded_at = max(
                copy.last_uploaded_at or receipt.uploaded_at,
                receipt.uploaded_at,
            )


def _identity_from_record(record: CollectionArchiveObjectRecord) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=record.object_id,
        kind=record.kind,
        object_path=record.object_path,
        plaintext_bytes=record.plaintext_bytes,
        stored_bytes=record.stored_bytes,
        sha256=record.sha256,
        stored_sha256=record.stored_sha256,
        revision=record.revision,
    )


def _identity_from_receipt(receipt: ArchiveObjectUploadReceipt) -> ArchiveObjectIdentity:
    return ArchiveObjectIdentity(
        object_id=receipt.object_id,
        kind=receipt.kind,
        object_path=receipt.object_path,
        plaintext_bytes=receipt.plaintext_bytes,
        stored_bytes=receipt.stored_bytes,
        sha256=receipt.sha256,
        stored_sha256=receipt.stored_sha256,
        revision=receipt.revision,
    )


def _apply_receipt(
    record: CollectionArchiveObjectRecord,
    receipt: ArchiveObjectUploadReceipt,
) -> None:
    record.kind = receipt.kind
    record.object_path = receipt.object_path
    record.plaintext_bytes = receipt.plaintext_bytes
    record.stored_bytes = receipt.stored_bytes
    record.sha256 = receipt.sha256
    record.stored_sha256 = receipt.stored_sha256
    record.revision = receipt.revision
    record.uploaded_at = receipt.uploaded_at
    record.verified_at = receipt.verified_at


def _retry_at(delay: timedelta) -> str:
    return format_utc_timestamp(utc_now() + delay)


__all__ = ["SqlAlchemyArchiveAttestationService"]
