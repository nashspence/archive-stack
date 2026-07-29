from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    ArchiveCopyJobRecord,
    CollectionArchiveCopyRecord,
    CollectionProofMaturationRecord,
)
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_records import apply_archive_receipt
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService

from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    archive_receipt,
    as_archive_store,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy service\n", "notes.txt": b"small notes\n"}
INITIATOR = ApplicationPrincipal(
    app="operator",
    key_id="operator-key",
    access=frozenset(),
)


def _service(path: Path, *, source_ready: bool = True):
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
    destination = MemoryArchiveStore(backend="b2")
    service = SqlAlchemyArchiveCopyService(
        config,
        ArchiveStoreRegistry(
            {
                "deep": as_archive_store(source),
                "b2": as_archive_store(destination),
            },
        ),
        proof_verifier=FixtureProofVerifier(),
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
    assert destination.archive is not None
    assert destination.archive.manifest_bytes == archive.manifest_bytes
    assert [current.object_id for current in destination.archive.data_objects] == [
        current.object_id for current in archive.data_objects
    ]
    assert source.prepared == [("data-000000",)]
    assert source.cleaned == [("data-000000",)]
    assert destination.verified == [("data-000000", "manifest", "proof")]
    with session_scope(make_session_factory(config.database_url)) as session:
        copy = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "b2"))
        assert copy is not None
        assert [(current.kind, current.object_id) for current in copy.objects] == [
            ("pack", "data-000000"),
            ("manifest", "manifest"),
            ("proof", "proof"),
        ]
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
    assert source.prepared == [("data-000000",)]
    assert destination.archive is None


def test_startup_resumes_a_claimed_archive_copy(tmp_path: Path) -> None:
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
        job.state = "copying"

    assert service.requeue_interrupted_copies_for_startup() == 1
    with session_scope(factory) as session:
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None
        assert job.state == "requested"
        assert job.next_attempt_at is not None


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"sha256": "f" * 64}, "does not match its manifest"),
        ({"verified_at": None}, "is not verified"),
        ({"object_path": "archives/other/data.age"}, "outside its copy"),
    ],
)
def test_archive_receipt_requires_exact_verified_owned_objects(
    tmp_path: Path,
    replacement: dict[str, object],
    message: str,
) -> None:
    _config, archive = seed_archive_copy(
        tmp_path / "catalog.sqlite3",
        FILES,
    )
    receipt = archive_receipt(archive, prefix="archives/b2/opaque-copy")
    changed = replace(receipt.objects[0], **replacement)
    mismatched = replace(receipt, objects=(changed, *receipt.objects[1:]))
    copy = CollectionArchiveCopyRecord(collection_id=COLLECTION_ID, store="b2")

    with pytest.raises(ValueError, match=message):
        apply_archive_receipt(copy, mismatched, archive)

    assert copy.state is None or copy.state == "pending"
    assert copy.objects == []
