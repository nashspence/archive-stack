from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, select, update
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyRetirementRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionProofMaturationRecord,
    CollectionRecord,
    RetrievalJobObjectRecord,
    RetrievalJobRecord,
)
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveObjectUploadReceipt
from riverhog_core.proofs import (
    CommandProofUpgrader,
    CommandProofVerifier,
    ProofUpgrader,
    ProofVerifier,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_mutations import require_collection_archive_idle

_LOG = logging.getLogger(__name__)


class SqlAlchemyProofMaturationService:
    def __init__(
        self,
        config: RuntimeConfig,
        archive_stores: ArchiveStoreRegistry,
        *,
        proof_upgrader: ProofUpgrader | None = None,
        proof_verifier: ProofVerifier | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._archive_stores = archive_stores
        self._proof_upgrader = proof_upgrader or CommandProofUpgrader(config.ots_upgrade_command)
        self._proof_verifier = proof_verifier or CommandProofVerifier(config.ots_verify_command)
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def requeue_interrupted_for_startup(self) -> int:
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(CollectionProofMaturationRecord)
                .where(CollectionProofMaturationRecord.state == "upgrading")
                .values(
                    state="pending",
                    next_attempt_at=now,
                    failure="proof maturation interrupted before completion",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def schedule_missing(self, *, limit: int = 1000) -> int:
        if limit < 1:
            return 0
        now = format_utc_timestamp(utc_now())
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(
                    CollectionArchiveCopyRecord.collection_id,
                    CollectionArchiveCopyRecord.store,
                )
                .outerjoin(
                    CollectionProofMaturationRecord,
                    and_(
                        CollectionProofMaturationRecord.collection_id
                        == CollectionArchiveCopyRecord.collection_id,
                        CollectionProofMaturationRecord.store == CollectionArchiveCopyRecord.store,
                    ),
                )
                .where(
                    CollectionArchiveCopyRecord.state == "uploaded",
                    CollectionProofMaturationRecord.collection_id.is_(None),
                )
                .order_by(
                    CollectionArchiveCopyRecord.collection_id,
                    CollectionArchiveCopyRecord.store,
                )
                .limit(limit)
            ).all()
            for collection_id, store in rows:
                session.add(
                    CollectionProofMaturationRecord(
                        collection_id=int(collection_id),
                        store=str(store),
                        state="pending",
                        attempt_count=0,
                        next_attempt_at=now,
                    )
                )
            return len(rows)

    def process_due(self, *, limit: int = 10) -> int:
        if limit < 1:
            return 0
        self.schedule_missing()
        processed = 0
        for _ in range(limit):
            claimed = self._claim_due()
            if claimed is None:
                break
            collection_id, store = claimed
            self._mature(collection_id=collection_id, store=store)
            processed += 1
        return processed

    def _claim_due(self) -> tuple[int, str] | None:
        now = format_utc_timestamp(utc_now())
        active_retrieval = (
            select(RetrievalJobRecord.id)
            .join(RetrievalJobObjectRecord)
            .where(
                RetrievalJobObjectRecord.collection_id
                == CollectionProofMaturationRecord.collection_id,
                RetrievalJobObjectRecord.source_store == CollectionProofMaturationRecord.store,
                RetrievalJobRecord.state.in_(("requested", "ready")),
            )
            .exists()
        )
        active_deletion = (
            select(CollectionDeletionRecord.collection_id)
            .where(
                CollectionDeletionRecord.collection_id
                == CollectionProofMaturationRecord.collection_id
            )
            .exists()
        )
        active_retirement = (
            select(ArchiveCopyRetirementRecord.collection_id)
            .where(
                ArchiveCopyRetirementRecord.collection_id
                == CollectionProofMaturationRecord.collection_id,
                ArchiveCopyRetirementRecord.store == CollectionProofMaturationRecord.store,
            )
            .exists()
        )
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionProofMaturationRecord)
                .where(
                    CollectionProofMaturationRecord.state.in_(("pending", "waiting", "retry_wait")),
                    CollectionProofMaturationRecord.next_attempt_at <= now,
                    ~active_retrieval,
                    ~active_deletion,
                    ~active_retirement,
                )
                .order_by(
                    CollectionProofMaturationRecord.next_attempt_at,
                    CollectionProofMaturationRecord.collection_id,
                    CollectionProofMaturationRecord.store,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if record is None:
                return None
            require_collection_archive_idle(session, record.collection_id)
            record.state = "upgrading"
            record.attempt_count += 1
            record.last_attempt_at = now
            record.failure = None
            return record.collection_id, record.store

    def _mature(self, *, collection_id: int, store: str) -> None:
        try:
            manifest_identity, proof_identity, passphrase_id = self._artifact_identities(
                collection_id=collection_id,
                store=store,
            )
            archive_store = self._archive_stores.require(store).store
            manifest = archive_store.read_archive_artifact(
                collection_id=collection_id,
                object=manifest_identity,
                passphrase_id=passphrase_id,
            )
            if not _receipt_matches_identity(manifest.receipt, manifest_identity):
                raise RuntimeError("archive manifest differs from its catalog record")
            proof = archive_store.read_archive_artifact(
                collection_id=collection_id,
                object=proof_identity,
                passphrase_id=passphrase_id,
            )
            self._proof_verifier.verify(
                manifest_bytes=manifest.content,
                proof_bytes=proof.content,
            )
            upgraded = self._proof_upgrader.upgrade(proof.content)
            if not upgraded.complete:
                self._record_waiting(collection_id=collection_id, store=store)
                return
            self._proof_verifier.verify(
                manifest_bytes=manifest.content,
                proof_bytes=upgraded.proof_bytes,
            )
            receipt = archive_store.replace_archive_proof(
                collection_id=collection_id,
                object=proof_identity,
                proof_bytes=upgraded.proof_bytes,
                passphrase_id=passphrase_id,
            )
            persisted = archive_store.read_archive_artifact(
                collection_id=collection_id,
                object=_identity_from_receipt(receipt),
                passphrase_id=passphrase_id,
            )
            if persisted.content != upgraded.proof_bytes:
                raise RuntimeError("persisted archive proof differs from the upgraded proof")
            if not _receipt_matches_identity(persisted.receipt, _identity_from_receipt(receipt)):
                raise RuntimeError("persisted archive proof metadata changed after replacement")
            self._proof_verifier.verify(
                manifest_bytes=manifest.content,
                proof_bytes=persisted.content,
            )
            self._record_matured(
                collection_id=collection_id,
                store=store,
                receipt=persisted.receipt,
            )
            _LOG.info(
                "collection archive proof matured: collection_id=%s store=%s",
                collection_id,
                store,
            )
        except Exception as exc:
            _LOG.exception(
                "collection archive proof maturation failed; retry scheduled: "
                "collection_id=%s store=%s",
                collection_id,
                store,
            )
            self._record_retry(collection_id=collection_id, store=store, exc=exc)

    def _artifact_identities(
        self,
        *,
        collection_id: int,
        store: str,
    ) -> tuple[ArchiveObjectIdentity, ArchiveObjectIdentity, str]:
        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, collection_id)
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if collection is None or copy is None or copy.state != "uploaded":
                raise RuntimeError("archive copy is not uploaded")
            records = {
                current.object_id: current
                for current in session.scalars(
                    select(CollectionArchiveObjectRecord).where(
                        CollectionArchiveObjectRecord.collection_id == collection_id,
                        CollectionArchiveObjectRecord.store == store,
                        CollectionArchiveObjectRecord.object_id.in_(("manifest", "proof")),
                    )
                )
            }
            try:
                manifest = records["manifest"]
                proof = records["proof"]
            except KeyError as exc:
                raise RuntimeError("archive copy has no manifest and proof pair") from exc
            return (
                _identity_from_record(manifest),
                _identity_from_record(proof),
                collection.passphrase_id,
            )

    def _record_waiting(self, *, collection_id: int, store: str) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionProofMaturationRecord, (collection_id, store))
            if record is None:
                return
            record.state = "waiting"
            record.next_attempt_at = _retry_at(self._config.proof_maturation_retry_delay)
            record.failure = None

    def _record_retry(self, *, collection_id: int, store: str, exc: Exception) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(CollectionProofMaturationRecord, (collection_id, store))
            if record is None:
                return
            record.state = "retry_wait"
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
            record = session.get(CollectionProofMaturationRecord, (collection_id, store))
            proof = session.get(CollectionArchiveObjectRecord, (collection_id, store, "proof"))
            copy = session.get(CollectionArchiveCopyRecord, (collection_id, store))
            if record is None or proof is None or copy is None:
                return
            proof.plaintext_bytes = receipt.plaintext_bytes
            proof.stored_bytes = receipt.stored_bytes
            proof.sha256 = receipt.sha256
            proof.stored_sha256 = receipt.stored_sha256
            proof.revision = receipt.revision
            proof.uploaded_at = receipt.uploaded_at
            proof.verified_at = receipt.verified_at
            copy.last_uploaded_at = max(
                copy.last_uploaded_at or receipt.uploaded_at,
                receipt.uploaded_at,
            )
            record.state = "matured"
            record.next_attempt_at = receipt.verified_at or receipt.uploaded_at
            record.matured_at = receipt.verified_at or receipt.uploaded_at
            record.failure = None


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


def _receipt_matches_identity(
    receipt: ArchiveObjectUploadReceipt,
    identity: ArchiveObjectIdentity,
) -> bool:
    return (
        receipt.object_id == identity.object_id
        and receipt.kind == identity.kind
        and receipt.object_path == identity.object_path
        and receipt.plaintext_bytes == identity.plaintext_bytes
        and receipt.stored_bytes == identity.stored_bytes
        and receipt.sha256 == identity.sha256
        and receipt.stored_sha256 == identity.stored_sha256
        and receipt.revision == identity.revision
    )


def _retry_at(delay: timedelta) -> str:
    return format_utc_timestamp(utc_now() + delay)
