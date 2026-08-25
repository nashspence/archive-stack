from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from riverhog_archive_contracts import (
    RECOVERY_DESCRIPTOR_PATH,
    CollectionEncryptionBinding,
)
from sqlalchemy import select
from time_formats import format_utc_timestamp, utc_now

from riverhog_core.archive_formats import RECOVERY_DESCRIPTOR_STORAGE_FORMAT
from riverhog_core.archive_recovery_descriptor import (
    RECOVERY_DESCRIPTOR_CONTENT_TYPE,
    build_recovery_descriptor,
)
from riverhog_core.catalog_db import SessionFactory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveAttestationRecord,
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionRecord,
)
from riverhog_core.ports.archive_objects import ImmutableArchiveObjectStore


@dataclass(frozen=True, slots=True)
class EncryptionCutoverItem:
    collection_id: int
    store: str
    archive_storage_prefix: str
    descriptor_path: str
    descriptor_bytes: bytes
    descriptor_sha256: str
    manifest_path: str
    manifest_stored_bytes: int
    manifest_stored_sha256: str
    encryption_format: str
    passphrase_id: str


class PreV1EncryptionCutover:
    """One-off pre-v1 descriptor backfill, separate from Riverhog startup."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        immutable_stores: Mapping[str, ImmutableArchiveObjectStore] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._immutable_stores = dict(immutable_stores or {})

    def plan(self) -> tuple[EncryptionCutoverItem, ...]:
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(CollectionRecord, CollectionArchiveCopyRecord)
                .join(
                    CollectionArchiveCopyRecord,
                    CollectionArchiveCopyRecord.collection_id == CollectionRecord.id,
                )
                .order_by(CollectionRecord.id, CollectionArchiveCopyRecord.store)
            ).all()
            planned: list[EncryptionCutoverItem] = []
            for collection, copy in rows:
                if copy.state != "uploaded" or not copy.archive_storage_prefix:
                    raise RuntimeError(
                        "pre-v1 encryption cutover requires an uploaded archive copy: "
                        f"collection={collection.id} store={copy.store}"
                    )
                objects = session.scalars(
                    select(CollectionArchiveObjectRecord).where(
                        CollectionArchiveObjectRecord.collection_id == collection.id,
                        CollectionArchiveObjectRecord.store == copy.store,
                    )
                ).all()
                manifests = [current for current in objects if current.kind == "manifest"]
                if len(manifests) != 1:
                    raise RuntimeError(
                        "pre-v1 encryption cutover requires exactly one encrypted root: "
                        f"collection={collection.id} store={copy.store}"
                    )
                manifest = manifests[0]
                item = _cutover_item(collection=collection, copy=copy, manifest=manifest)
                descriptors = [
                    current for current in objects if current.kind == "recovery-descriptor"
                ]
                if descriptors:
                    if len(descriptors) != 1 or not _descriptor_matches(descriptors[0], item):
                        raise RuntimeError(
                            "existing recovery descriptor catalog identity is inconsistent: "
                            f"collection={collection.id} store={copy.store}"
                        )
                    continue
                planned.append(item)
            return tuple(planned)

    def execute(self) -> tuple[EncryptionCutoverItem, ...]:
        completed: list[EncryptionCutoverItem] = []
        for item in self.plan():
            try:
                object_store = self._immutable_stores[item.store]
            except KeyError as exc:
                raise RuntimeError(
                    f"no immutable archive store is configured for {item.store}"
                ) from exc
            receipt = object_store.put_immutable_object(
                object_path=item.descriptor_path,
                content=item.descriptor_bytes,
                content_type=RECOVERY_DESCRIPTOR_CONTENT_TYPE,
                identity_metadata={
                    "riverhog-format": RECOVERY_DESCRIPTOR_STORAGE_FORMAT,
                    "riverhog-sha256": item.descriptor_sha256,
                    "riverhog-root-stored-sha256": item.manifest_stored_sha256,
                },
                placement="immediate",
            )
            if (
                receipt.object_path != item.descriptor_path
                or receipt.stored_bytes != len(item.descriptor_bytes)
                or receipt.stored_sha256 != item.descriptor_sha256
            ):
                raise RuntimeError(
                    "immutable archive store changed the recovery descriptor: "
                    f"collection={item.collection_id} store={item.store}"
                )
            with session_scope(self._session_factory) as session:
                collection = session.get(CollectionRecord, item.collection_id)
                copy = session.get(
                    CollectionArchiveCopyRecord,
                    (item.collection_id, item.store),
                )
                manifest = session.get(
                    CollectionArchiveObjectRecord,
                    (item.collection_id, item.store, "manifest"),
                )
                if collection is None or copy is None or manifest is None:
                    raise RuntimeError("archive catalog changed while applying encryption cutover")
                current = _cutover_item(
                    collection=collection,
                    copy=copy,
                    manifest=manifest,
                )
                if current != item:
                    raise RuntimeError("archive catalog changed while applying encryption cutover")
                existing = session.get(
                    CollectionArchiveObjectRecord,
                    (item.collection_id, item.store, "recovery-descriptor"),
                )
                if existing is not None:
                    if not _descriptor_matches(existing, item):
                        raise RuntimeError("recovery descriptor was concurrently changed")
                    continue
                later = session.scalars(
                    select(CollectionArchiveObjectRecord).where(
                        CollectionArchiveObjectRecord.collection_id == item.collection_id,
                        CollectionArchiveObjectRecord.store == item.store,
                        CollectionArchiveObjectRecord.object_order > manifest.object_order,
                    )
                ).all()
                for current_object in later:
                    current_object.object_order += 1
                now = format_utc_timestamp(utc_now())
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=item.collection_id,
                        store=item.store,
                        object_id="recovery-descriptor",
                        object_order=manifest.object_order + 1,
                        kind="recovery-descriptor",
                        object_path=receipt.object_path,
                        plaintext_bytes=receipt.stored_bytes,
                        stored_bytes=receipt.stored_bytes,
                        sha256=receipt.stored_sha256,
                        stored_sha256=receipt.stored_sha256,
                        revision=receipt.revision,
                        uploaded_at=receipt.completed_at,
                        verified_at=now,
                    )
                )
                attestation = session.get(
                    CollectionArchiveAttestationRecord,
                    (item.collection_id, item.store),
                )
                if attestation is not None:
                    attestation.state = "pending"
                    attestation.attempt_count = 0
                    attestation.next_attempt_at = now
                    attestation.last_attempt_at = None
                    attestation.published_at = None
                    attestation.matured_at = None
                    attestation.failure = None
            completed.append(item)
        return tuple(completed)


def _cutover_item(
    *,
    collection: CollectionRecord,
    copy: CollectionArchiveCopyRecord,
    manifest: CollectionArchiveObjectRecord,
) -> EncryptionCutoverItem:
    if manifest.object_id != "manifest":
        raise RuntimeError("encrypted root object identity is not canonical")
    prefix = (copy.archive_storage_prefix or "").strip("/")
    if not prefix or not manifest.object_path.startswith(f"{prefix}/"):
        raise RuntimeError("encrypted root is outside its archive copy")
    if not manifest.stored_sha256 or len(manifest.stored_sha256) != 64:
        raise RuntimeError("encrypted root has no stored SHA-256")
    root_relative_path = manifest.object_path.removeprefix(f"{prefix}/")
    encryption = CollectionEncryptionBinding(
        format=collection.encryption_format,
        passphrase_id=collection.passphrase_id,
    )
    descriptor = build_recovery_descriptor(
        encryption=encryption,
        root_relative_path=root_relative_path,
        root_stored_bytes=manifest.stored_bytes,
        root_stored_sha256=manifest.stored_sha256,
    )
    return EncryptionCutoverItem(
        collection_id=collection.id,
        store=copy.store,
        archive_storage_prefix=prefix,
        descriptor_path=f"{prefix}/{RECOVERY_DESCRIPTOR_PATH}",
        descriptor_bytes=descriptor,
        descriptor_sha256=hashlib.sha256(descriptor).hexdigest(),
        manifest_path=manifest.object_path,
        manifest_stored_bytes=manifest.stored_bytes,
        manifest_stored_sha256=manifest.stored_sha256,
        encryption_format=encryption.format,
        passphrase_id=encryption.passphrase_id,
    )


def _descriptor_matches(
    record: CollectionArchiveObjectRecord,
    item: EncryptionCutoverItem,
) -> bool:
    return (
        record.object_id == "recovery-descriptor"
        and record.object_path == item.descriptor_path
        and record.plaintext_bytes == len(item.descriptor_bytes)
        and record.stored_bytes == len(item.descriptor_bytes)
        and record.sha256 == item.descriptor_sha256
        and record.stored_sha256 == item.descriptor_sha256
    )


__all__ = ["EncryptionCutoverItem", "PreV1EncryptionCutover"]
