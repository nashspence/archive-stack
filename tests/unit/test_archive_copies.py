from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyObjectUploadRecord,
    CollectionArchiveCopyRecord,
    CollectionProofMaturationRecord,
    RetrievalCacheLeaseRecord,
    RetrievalCacheObjectRecord,
)
from riverhog_core.ports.archive_objects import (
    CompletedObjectReceipt,
    MultipartPartReceipt,
    MultipartUpload,
)
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveReadStatus
from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt
from riverhog_core.runtime_config import RetrievalCacheConfig, RuntimeConfig
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from sqlalchemy import select

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    FixtureArchive,
    MemoryArchiveStore,
    archive_store_binding,
    make_archive,
    make_captured_provenance_archive,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy service\n", "notes.txt": b"small notes\n"}
INITIATOR = ApplicationPrincipal(
    app="operator",
    key_id="operator-key",
    access=frozenset(),
)


class _ArchiveCopyCache:
    def __init__(self) -> None:
        self.store = MemoryArchiveStore()

    def multipart_object_store(self, **_: object) -> MemoryArchiveStore:
        return self.store

    def verify_multipart_object(
        self,
        *,
        completed: CompletedObjectReceipt,
        parts: tuple[MultipartPartReceipt, ...] = (),
    ) -> RetrievalCacheReceipt:
        assert parts
        content = self.store.objects[completed.object_path]
        return RetrievalCacheReceipt(
            object_path=completed.object_path,
            revision=completed.revision,
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            cached_at=completed.completed_at,
            verified_at=completed.completed_at,
        )


def _multipart_archive(files: dict[str, bytes], *, parts: int = 4) -> FixtureArchive:
    archive = make_archive(files)
    ciphertext = archive.stored_objects[f"volumes/{archive.pack_plan.volume_id}.tar.age"]
    plaintext = archive.pack_plaintext

    def split(content: bytes) -> list[bytes]:
        return [
            content[len(content) * index // parts : len(content) * (index + 1) // parts]
            for index in range(parts)
        ]

    plaintext_parts = split(plaintext)
    stored_parts = split(ciphertext)
    plaintext_start = 0
    receipts: list[dict[str, object]] = []
    for number, (plain, stored) in enumerate(
        zip(plaintext_parts, stored_parts, strict=True),
        start=1,
    ):
        receipts.append(
            {
                "number": number,
                "plaintext_start": plaintext_start,
                "plaintext_bytes": len(plain),
                "plaintext_sha256": hashlib.sha256(plain).hexdigest(),
                "stored_bytes": len(stored),
                "stored_sha256": hashlib.sha256(stored).hexdigest(),
                "part_token": f"source-part-{number}",
            }
        )
        plaintext_start += len(plain)
    return replace(
        archive,
        pack_parts_json=json.dumps(receipts, sort_keys=True, separators=(",", ":")),
    )


def _service(
    path: Path,
    *,
    source_ready: bool = True,
    destination: MemoryArchiveStore | None = None,
    archive: FixtureArchive | None = None,
) -> tuple[
    RuntimeConfig,
    FixtureArchive,
    MemoryArchiveStore,
    MemoryArchiveStore,
    SqlAlchemyArchiveCopyService,
]:
    config, archive = seed_archive_copy(path, FILES, archive=archive)
    b2_config = replace(
        config.archive_store("deep"),
        name="b2",
    )
    config = replace(
        config,
        archive_stores={"deep": config.archive_store("deep"), "b2": b2_config},
    )
    source = MemoryArchiveStore(archive, ready=source_ready)
    destination = destination or MemoryArchiveStore()
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "deep": archive_store_binding(source),
                "b2": archive_store_binding(destination),
            },
        ),
    )
    return config, archive, source, destination, service


def test_archive_copy_preserves_the_independent_object_manifest(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="riverhog.transfer")
    config, archive, source, destination, service = _service(tmp_path / "catalog.sqlite3")

    requested = service.create_or_resume(
        COLLECTION_ID,
        source_store="deep",
        destination_store="b2",
        initiator=INITIATOR,
        event_context={"workflow": "promotion"},
    )
    assert service.process_due(limit=1) == 1

    assert requested["state"] == "requested"
    prefix = "archives/archive/new-copy"
    assert destination.objects == {
        f"{prefix}/{relative_path}": content
        for relative_path, content in archive.stored_objects.items()
    }
    assert source.prepared == [("pack-000000000000", "manifest", "proof")]
    assert source.cleaned == [("pack-000000000000", "manifest", "proof")]
    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2"))
        assert copy is not None
        assert [(current.kind, current.object_id) for current in copy.objects] == [
            ("pack", "pack-000000000000"),
            ("manifest", "manifest"),
            ("proof", "proof"),
        ]
        pack = copy.objects[0]
        assert [current.path for current in pack.placements] == sorted(FILES)
        assert pack.plan_sha256 == archive.pack_plan_sha256
        assert pack.index_sha256 == archive.pack_index_sha256
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None
        assert job.state == "completed"
        assert job.completed_at is not None
        maturation = session.get(CollectionProofMaturationRecord, (COLLECTION_ID, "b2"))
        assert maturation is not None and maturation.state == "pending"
    shown = service.get(COLLECTION_ID, destination_store="b2")
    listed = service.list(
        page=1,
        per_page=25,
        q="b2",
        sort="requested_at",
        order="desc",
        all_items=False,
    )
    assert shown["state"] == "completed"
    assert shown["initiated_by_app"] == "operator"
    assert listed["copies"] == [shown]
    events = (
        SqlAlchemyLifecycleEventService(config)
        .page(
            owner_app="operator",
            after=None,
            limit=100,
        )
        .events
    )
    assert [event.type.rsplit(".", 1)[-1] for event in events] == [
        "requested",
        "completed",
    ]
    assert events[-1].data["context"] == {"workflow": "promotion"}
    transfer_messages = [message for message in caplog.messages if "transfer operation=" in message]
    assert any("operation=archive_copy_part" in message for message in transfer_messages)
    assert sum("operation=archive_copy_object" in message for message in transfer_messages) == 2
    assert all("integrity_seconds=" in message for message in transfer_messages)
    assert all("pack-000000000000" not in message for message in transfer_messages)


def test_archive_copy_pipelines_source_parts_into_parallel_destination_requests(
    tmp_path: Path,
) -> None:
    archive = _multipart_archive(FILES)

    class ConcurrentDestination(MemoryArchiveStore):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.rendezvous = threading.Barrier(2)
            self.active = 0
            self.maximum_active = 0

        def upload_part(
            self,
            *,
            upload: MultipartUpload,
            number: int,
            content: bytes,
        ) -> MultipartPartReceipt:
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            if number <= 2:
                self.rendezvous.wait(timeout=2)
            try:
                return super().upload_part(upload=upload, number=number, content=content)
            finally:
                with self.lock:
                    self.active -= 1

    destination = ConcurrentDestination()
    _config, archive, _source, destination, service = _service(
        tmp_path / "catalog.sqlite3",
        destination=destination,
        archive=archive,
    )
    service.create_or_resume(
        COLLECTION_ID,
        source_store="deep",
        destination_store="b2",
        initiator=INITIATOR,
    )

    assert service.process_due(limit=1) == 1
    assert destination.maximum_active >= 2
    assert (
        destination.objects[f"archives/archive/new-copy/volumes/{archive.pack_plan.volume_id}.tar.age"]
        == archive.stored_objects[f"volumes/{archive.pack_plan.volume_id}.tar.age"]
    )


def test_archive_copy_preserves_immutable_provenance_objects(tmp_path: Path) -> None:
    archive = make_captured_provenance_archive(FILES, tmp_path / "source")
    config, archive, source, destination, service = _service(
        tmp_path / "catalog.sqlite3",
        archive=archive,
    )

    service.create_or_resume(
        COLLECTION_ID,
        source_store="deep",
        destination_store="b2",
        initiator=INITIATOR,
    )
    assert service.process_due(limit=1) == 1
    assert archive.provenance is not None

    expected_ids = (
        "pack-000000000000",
        *(bundle.bundle_id for bundle in archive.provenance.bundles),
        "provenance-index",
        "manifest",
        "proof",
    )
    prefix = "archives/archive/new-copy"
    assert destination.objects == {
        f"{prefix}/{relative_path}": content
        for relative_path, content in archive.stored_objects.items()
    }
    assert source.prepared == [expected_ids]
    assert source.cleaned == [expected_ids]
    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2"))
        assert copy is not None
        assert [(current.kind, current.object_id) for current in copy.objects] == [
            ("pack", "pack-000000000000"),
            *(("provenance-bundle", bundle.bundle_id) for bundle in archive.provenance.bundles),
            ("provenance-index", "provenance-index"),
            ("manifest", "manifest"),
            ("proof", "proof"),
        ]


def test_archive_copy_to_restore_required_store_writes_final_custody(
    tmp_path: Path,
) -> None:
    config, archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        FILES,
        store="b2",
    )
    deep = replace(
        config.archive_store("b2"),
        name="deep",
    )
    config = replace(
        config,
        archive_stores={"b2": config.archive_store("b2"), "deep": deep},
        archive_read_order=("b2", "deep"),
        retrieval_cache=RetrievalCacheConfig(storage_adapter="archive"),
    )
    source = MemoryArchiveStore(archive)
    destination = MemoryArchiveStore(
        read_mode="restore_required",
    )
    cache = _ArchiveCopyCache()
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "b2": archive_store_binding(source),
                "deep": archive_store_binding(destination),
            }
        ),
        retrieval_cache=cache,  # type: ignore[arg-type]
    )
    service.create_or_resume(
        COLLECTION_ID,
        source_store="b2",
        destination_store="deep",
        initiator=INITIATOR,
    )

    assert service.process_due(limit=1) == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep"))
        checkpoints = session.scalars(select(ArchiveCopyObjectUploadRecord)).all()
        assert copy is not None
        assert copy.storage_adapter == "archive"
        assert copy.read_mode == "restore_required"
        assert checkpoints == []
        cached = session.get(
            RetrievalCacheObjectRecord,
            ("deep", COLLECTION_ID, "pack-000000000000"),
        )
        lease = session.get(
            RetrievalCacheLeaseRecord,
            ("new-archive", "deep", COLLECTION_ID, "pack-000000000000"),
        )
        assert cached is not None
        assert lease is not None
    assert set(destination.objects) == {
        "archives/archive/new-copy/volumes/pack-000000000000.tar.age",
        "archives/archive/new-copy/manifest.json.age",
        "archives/archive/new-copy/manifest.json.ots.age",
    }
    pack_path = "archives/archive/new-copy/volumes/pack-000000000000.tar.age"
    assert cache.store.objects[pack_path] == destination.objects[pack_path]


def test_restore_required_copy_uses_archive_only_when_new_archive_cache_is_disabled(
    tmp_path: Path,
) -> None:
    config, archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        FILES,
        store="b2",
    )
    deep = replace(
        config.archive_store("b2"),
        name="deep",
    )
    config = replace(
        config,
        archive_stores={"b2": config.archive_store("b2"), "deep": deep},
        retrieval_cache=RetrievalCacheConfig(storage_adapter="archive"),
        retrieval_cache_new_archive_enabled=False,
    )
    source = MemoryArchiveStore(archive)
    destination = MemoryArchiveStore(read_mode="restore_required")
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "b2": archive_store_binding(source),
                "deep": archive_store_binding(destination),
            }
        ),
        retrieval_cache=_ArchiveCopyCache(),  # type: ignore[arg-type]
    )

    selected = service._volume_object_store(
        store_name="deep",
        collection_id=COLLECTION_ID,
        object_id="pack-000000000000",
    )

    assert selected is destination


def test_archive_copy_waits_for_selected_source_objects(tmp_path: Path) -> None:
    config, _archive, source, destination, service = _service(
        tmp_path / "catalog.sqlite3", source_ready=False
    )
    service.create_or_resume(
        COLLECTION_ID,
        destination_store="b2",
        initiator=INITIATOR,
    )

    assert service.process_due(limit=1) == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None and job.state == "waiting"
    assert source.prepared == [("pack-000000000000", "manifest", "proof")]
    assert destination.objects == {}


def test_archive_copy_checks_remote_source_outside_its_catalog_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _archive, source, _destination, service = _service(
        tmp_path / "catalog.sqlite3",
        source_ready=False,
    )
    service.create_or_resume(COLLECTION_ID, destination_store="b2", initiator=INITIATOR)
    original_prepare = source.prepare_archive_objects_read

    def inspect_claim(
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **kwargs: object,
    ) -> ArchiveReadStatus:
        with session_scope(make_session_factory(config.database_url)) as session:
            job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
            assert job is not None and job.state == "checking"
        return original_prepare(objects=objects, **kwargs)

    monkeypatch.setattr(source, "prepare_archive_objects_read", inspect_claim)

    assert service.process_due(limit=1) == 1
    assert service.get(COLLECTION_ID, destination_store="b2")["state"] == "waiting"


def test_archive_copy_canceled_during_source_check_cleans_the_requested_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, _archive, source, destination, service = _service(
        tmp_path / "catalog.sqlite3",
        source_ready=False,
    )
    service.create_or_resume(COLLECTION_ID, destination_store="b2", initiator=INITIATOR)
    original_prepare = source.prepare_archive_objects_read

    def cancel_during_check(
        *,
        objects: Sequence[ArchiveObjectIdentity],
        **kwargs: object,
    ) -> ArchiveReadStatus:
        canceling = service.cancel(COLLECTION_ID, destination_store="b2")
        assert canceling["state"] == "canceling"
        return original_prepare(objects=objects, **kwargs)

    monkeypatch.setattr(source, "prepare_archive_objects_read", cancel_during_check)

    assert service.process_due(limit=1) == 1
    assert service.get(COLLECTION_ID, destination_store="b2")["state"] == "canceled"
    assert source.cleaned == [("pack-000000000000", "manifest", "proof")]
    assert destination.discarded_uploads == ["archives/archive/new-copy"]


def test_archive_copy_cancellation_closes_waiting_job_and_discards_prefix(
    tmp_path: Path,
) -> None:
    config, _archive, source, destination, service = _service(
        tmp_path / "catalog.sqlite3", source_ready=False
    )
    service.create_or_resume(COLLECTION_ID, destination_store="b2", initiator=INITIATOR)
    service.process_due(limit=1)

    canceled = service.cancel(COLLECTION_ID, destination_store="b2")

    assert canceled["state"] == "canceled"
    assert canceled["completed_at"] is not None
    assert source.cleaned == [("pack-000000000000", "manifest", "proof")]
    assert destination.discarded_uploads == ["archives/archive/new-copy"]
    filtered = service.list(
        page=1,
        per_page=25,
        q=None,
        state="canceled",
        sort="requested_at",
        order="desc",
        all_items=False,
    )
    assert filtered["filters"] == {"state": "canceled"}
    assert filtered["copies"] == [canceled]
    events = (
        SqlAlchemyLifecycleEventService(config)
        .page(
            owner_app="operator",
            after=None,
            limit=100,
        )
        .events
    )
    assert [event.type.rsplit(".", 1)[-1] for event in events] == [
        "requested",
        "canceled",
    ]

    restarted = service.create_or_resume(
        COLLECTION_ID,
        destination_store="b2",
        initiator=INITIATOR,
    )
    assert restarted["state"] == "requested"


def test_archive_copy_cancellation_stops_an_active_transfer_before_commit(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingDestination(MemoryArchiveStore):
        def upload_part(
            self,
            *,
            upload: MultipartUpload,
            number: int,
            content: bytes,
        ) -> MultipartPartReceipt:
            started.set()
            assert release.wait(timeout=5)
            return super().upload_part(upload=upload, number=number, content=content)

    destination = BlockingDestination()
    config, _archive, _source, _destination, service = _service(
        tmp_path / "catalog.sqlite3",
        destination=destination,
    )
    service.create_or_resume(COLLECTION_ID, destination_store="b2", initiator=INITIATOR)
    worker = threading.Thread(target=service.process_due, daemon=True)
    worker.start()
    assert started.wait(timeout=5)

    canceling = service.cancel(COLLECTION_ID, destination_store="b2")
    assert canceling["state"] == "canceling"
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    canceled = service.get(COLLECTION_ID, destination_store="b2")
    assert canceled["state"] == "canceled"
    assert canceled["completed_at"] is not None
    assert destination.objects == {}
    assert destination.discarded_uploads == ["archives/archive/new-copy"]
    with session_scope(make_session_factory(config.database_url)) as session:
        assert session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2")) is None


@pytest.mark.parametrize("interrupted_state", ["checking", "copying"])
def test_startup_resumes_a_claimed_archive_copy(
    tmp_path: Path,
    interrupted_state: str,
) -> None:
    config, _archive, _source, _destination, service = _service(tmp_path / "catalog.sqlite3")
    service.create_or_resume(
        COLLECTION_ID,
        destination_store="b2",
        initiator=INITIATOR,
    )
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None
        job.state = interrupted_state

    assert service.requeue_interrupted_copies_for_startup() == 1
    with session_scope(factory) as session:
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None
        assert job.state == "requested"
        assert job.next_attempt_at is not None
