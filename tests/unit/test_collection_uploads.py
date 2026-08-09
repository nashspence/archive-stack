from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_ingress_registry import ArchiveIngressStore, ArchiveIngressStoreRegistry
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionArchiveObjectRecord, TagRecord
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_protocol.manifest import collection_content_etag

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore, as_archive_store
from tests.unit.db_helpers import sqlite_url
from tests.unit.test_archive_root import MemoryImmutableStore
from tests.unit.test_pack_upload import MemoryMultipartStore

_CREATOR = ApplicationPrincipal(
    app="uploader",
    key_id="key-1",
    access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
)


class _UnusedRangeStore:
    def iter_object_range(self, **_: object):
        raise AssertionError("ingress does not read archive ranges")


def _service(tmp_path: Path) -> tuple[SqlAlchemyCollectionUploadService, RuntimeConfig]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-08-08T00:00:00.000000Z",
            )
        )
    archive_store = MemoryArchiveStore()
    ingress = ArchiveIngressStore(
        multipart=MemoryMultipartStore(),
        root=MemoryImmutableStore(),
        ranges=_UnusedRangeStore(),
    )
    return (
        SqlAlchemyCollectionUploadService(
            config,
            ArchiveStoreRegistry({"archive": as_archive_store(archive_store)}),
            ArchiveIngressStoreRegistry({"archive": ingress}),
            proof_stamper=FixtureProofStamper(),
        ),
        config,
    )


@pytest.mark.parametrize(
    "tags",
    [pytest.param(("docs",), id="tagged"), pytest.param((), id="untagged")],
)
def test_small_collection_moves_directly_from_source_unit_to_final_custody(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    service, config = _service(tmp_path)
    content = b"direct final archive\n"
    sha256 = hashlib.sha256(content).hexdigest()

    opened = service.create_or_resume(
        idempotency_key="upload-1",
        tags=tags,
        ingest_source="fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
    )
    assert opened["tags"] == list(tags)
    collection_id = int(opened["collection_id"])
    registered = service.register_files(
        collection_id,
        ({"path": "document.txt", "bytes": len(content), "sha256": sha256},),
    )
    assert registered["volumes"] == []

    closed = service.complete(
        collection_id,
        files_total=1,
        content_etag=collection_content_etag((("document.txt", len(content), sha256),)),
    )
    assert closed["state"] == "uploading"
    volume = service.list_volumes(collection_id)["volumes"][0]
    assert volume["kind"] == "pack"
    unit = volume["units"][0]
    assert unit["sources"] == [
        {
            "path": "document.txt",
            "offset": 0,
            "bytes": len(content),
            "sha256": sha256,
        }
    ]

    committed = service.upload_unit(
        collection_id,
        str(volume["volume_id"]),
        0,
        plan_sha256=str(volume["plan_sha256"]),
        content=content,
    )
    assert committed["state"] == "committed"
    finalized = service.get(collection_id)
    assert finalized["state"] == "finalized"
    assert finalized["tags"] == list(tags)
    assert finalized["uploaded_bytes"] == len(content)

    with session_scope(make_session_factory(config.database_url)) as session:
        objects = list(
            session.query(CollectionArchiveObjectRecord)
            .filter(CollectionArchiveObjectRecord.collection_id == collection_id)
            .order_by(CollectionArchiveObjectRecord.object_order)
        )
    assert [current.object_id for current in objects] == [
        "pack-000000000000",
        "manifest",
        "proof",
    ]
    assert objects[0].object_path.endswith("/volumes/pack-000000000000.tar.age")
    assert objects[1].object_path.endswith("/manifest.json.age")
    assert objects[2].object_path.endswith("/manifest.json.ots.age")
