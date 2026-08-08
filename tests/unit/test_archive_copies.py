from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_ingress_registry import ArchiveIngressStoreRegistry
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    ArchiveCopyObjectUploadRecord,
    CollectionArchiveCopyRecord,
    CollectionProofMaturationRecord,
)
from riverhog_core.ports.archive_ingress_store import MultipartPartReceipt, MultipartUpload
from riverhog_core.ports.archive_store import ArchiveObjectIdentity, ArchiveReadStatus
from riverhog_core.runtime_config import RetrievalCacheConfig, RuntimeConfig
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from sqlalchemy import select

from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    FixtureArchive,
    MemoryArchiveStore,
    as_archive_store,
    as_ingress_store,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy service\n", "notes.txt": b"small notes\n"}
INITIATOR = ApplicationPrincipal(
    app="operator",
    key_id="operator-key",
    access=frozenset(),
)


def _service(
    path: Path,
    *,
    source_ready: bool = True,
    destination: MemoryArchiveStore | None = None,
) -> tuple[
    RuntimeConfig,
    FixtureArchive,
    MemoryArchiveStore,
    MemoryArchiveStore,
    SqlAlchemyArchiveCopyService,
]:
    config, archive = seed_archive_copy(path, FILES)
    b2_config = replace(
        config.archive_store("deep"),
        name="b2",
        backend="b2",
        storage_class="STANDARD",
    )
    config = replace(
        config,
        archive_stores={"deep": config.archive_store("deep"), "b2": b2_config},
    )
    source = MemoryArchiveStore(archive, ready=source_ready)
    destination = destination or MemoryArchiveStore(backend="b2")
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "deep": as_archive_store(source),
                "b2": as_archive_store(destination),
            },
        ),
        ArchiveIngressStoreRegistry(
            {
                "deep": as_ingress_store(source),
                "b2": as_ingress_store(destination),
            }
        ),
    )
    return config, archive, source, destination, service


def test_archive_copy_preserves_the_independent_object_manifest(tmp_path: Path) -> None:
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
    prefix = "archives/b2/new-copy"
    assert destination.objects == {
        f"{prefix}/{relative_path}": content
        for relative_path, content in archive.stored_objects.items()
    }
    assert source.prepared == [("pack-000000000000",)]
    assert source.cleaned == [("pack-000000000000",)]
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


def test_archive_copy_to_restore_required_store_writes_final_custody(
    tmp_path: Path,
) -> None:
    config, archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        FILES,
        store="b2",
        backend="b2",
    )
    deep = replace(
        config.archive_store("b2"),
        name="deep",
        backend="aws",
        storage_class="DEEP_ARCHIVE",
        read_mode="restore_required",
    )
    config = replace(
        config,
        archive_stores={"b2": config.archive_store("b2"), "deep": deep},
        archive_read_order=("b2", "deep"),
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="https://cache.example",
            region="us-east-1",
            bucket="cache",
            access_key_id="key",
            secret_access_key="secret",
        ),
    )
    source = MemoryArchiveStore(archive, backend="b2")
    destination = MemoryArchiveStore(
        backend="aws",
        storage_class="DEEP_ARCHIVE",
        read_mode="restore_required",
    )
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "b2": as_archive_store(source),
                "deep": as_archive_store(destination),
            }
        ),
        ArchiveIngressStoreRegistry(
            {
                "b2": as_ingress_store(source),
                "deep": as_ingress_store(destination),
            }
        ),
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
        assert copy.storage_class == "DEEP_ARCHIVE"
        assert checkpoints == []
    assert set(destination.objects) == {
        "archives/aws/new-copy/volumes/pack-000000000000.tar.age",
        "archives/aws/new-copy/manifest.json.age",
        "archives/aws/new-copy/manifest.json.ots.age",
    }


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
    assert source.prepared == [("pack-000000000000",)]
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
    assert source.cleaned == [("pack-000000000000",)]
    assert destination.discarded_uploads == ["archives/b2/new-copy"]


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
    assert source.cleaned == [("pack-000000000000",)]
    assert destination.discarded_uploads == ["archives/b2/new-copy"]
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

    destination = BlockingDestination(backend="b2")
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
    assert destination.discarded_uploads == ["archives/b2/new-copy"]
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
