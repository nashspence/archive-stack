from __future__ import annotations

from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    RecoveryDescriptor,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveAttestationRecord,
    CollectionArchiveObjectRecord,
)
from riverhog_core.pre_v1_encryption_cutover import PreV1EncryptionCutover
from riverhog_core.runtime_config import DEV_ARCHIVE_PASSPHRASE_ID
from sqlalchemy import select

from tests.unit.archive_object_fixtures import MemoryArchiveStore, seed_archive_copy


def test_pre_v1_cutover_publishes_descriptor_and_requeues_attestation(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.db", {"hello.txt": b"hello"})
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        descriptor = session.get(
            CollectionArchiveObjectRecord,
            (archive.collection_id, "deep", "recovery-descriptor"),
        )
        assert descriptor is not None
        session.delete(descriptor)
        session.add(
            CollectionArchiveAttestationRecord(
                collection_id=archive.collection_id,
                store="deep",
                state="matured",
                attempt_count=4,
                next_attempt_at="2026-08-01T00:00:00.000000Z",
                last_attempt_at="2026-08-01T00:00:00.000000Z",
                published_at="2026-08-01T00:00:00.000000Z",
                matured_at="2026-08-01T00:00:00.000000Z",
            )
        )

    memory = MemoryArchiveStore()
    cutover = PreV1EncryptionCutover(
        session_factory=factory,
        immutable_stores={"deep": memory},
    )
    plan = cutover.plan()
    assert [(item.collection_id, item.store) for item in plan] == [(archive.collection_id, "deep")]

    completed = cutover.execute()
    assert completed == plan
    content = memory.objects[plan[0].descriptor_path]
    descriptor = RecoveryDescriptor.from_json_bytes(content)
    assert descriptor.encryption.format == ARCHIVE_ENCRYPTION_FORMAT
    assert descriptor.encryption.passphrase_id == DEV_ARCHIVE_PASSPHRASE_ID
    assert descriptor.root.path == "manifest.json.age"
    assert descriptor.root.stored_bytes == len(archive.stored_objects["manifest.json.age"])

    with session_scope(factory) as session:
        rows = session.scalars(
            select(CollectionArchiveObjectRecord)
            .where(
                CollectionArchiveObjectRecord.collection_id == archive.collection_id,
                CollectionArchiveObjectRecord.store == "deep",
            )
            .order_by(CollectionArchiveObjectRecord.object_order)
        ).all()
        assert [row.object_id for row in rows][-3:] == [
            "manifest",
            "recovery-descriptor",
            "proof",
        ]
        attestation = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert attestation is not None
        assert attestation.state == "pending"
        assert attestation.attempt_count == 0
        assert attestation.published_at is None
        assert attestation.matured_at is None

    assert cutover.plan() == ()
    assert cutover.execute() == ()


def test_pre_v1_cutover_rejects_inconsistent_existing_descriptor(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "catalog.db", {"hello.txt": b"hello"})
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        descriptor = session.get(
            CollectionArchiveObjectRecord,
            (archive.collection_id, "deep", "recovery-descriptor"),
        )
        assert descriptor is not None
        descriptor.stored_sha256 = "0" * 64

    cutover = PreV1EncryptionCutover(session_factory=factory)
    try:
        cutover.plan()
    except RuntimeError as exc:
        assert "descriptor catalog identity is inconsistent" in str(exc)
    else:
        raise AssertionError("inconsistent descriptor must fail closed")
