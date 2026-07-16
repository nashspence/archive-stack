from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import ArchiveCopyJobRecord, CollectionArchiveCopyRecord
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    COLLECTION_ID,
    MemoryArchiveStore,
    as_archive_store,
    seed_archive_copy,
)

FILES = {"document.txt": b"archive copy service\n", "notes.txt": b"small notes\n"}


def _service(path: Path, *, source_ready: bool = True):
    config, archive = seed_archive_copy(path, FILES, hot=False)
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
        assert session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2")) is None


def test_archive_copy_waits_for_selected_source_objects(tmp_path: Path) -> None:
    config, _archive, source, destination, service = _service(
        tmp_path / "catalog.sqlite3", source_ready=False
    )
    service.create_or_resume(COLLECTION_ID, destination_store="b2")

    assert service.process_due(limit=1) == 1

    with session_scope(make_session_factory(config.database_url)) as session:
        job = session.get(ArchiveCopyJobRecord, (COLLECTION_ID, "b2"))
        assert job is not None and job.state == "waiting"
    assert source.prepared == [("data-000000",)]
    assert destination.archive is None
