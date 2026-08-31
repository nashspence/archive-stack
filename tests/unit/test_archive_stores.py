from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_api.mappers import map_archive_store
from riverhog_api.schemas.archive_stores import ArchiveStoreOut
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_protocol.paths import tag_set_identity

from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url


def _config(path: Path) -> RuntimeConfig:
    config = RuntimeConfig(database_url=sqlite_url(path))
    deep = replace(
        config.archive_store("archive"),
        name="deep",
        base_url="http://127.0.0.1/deep",
    )
    return replace(
        config,
        archive_stores={"deep": deep},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )


def _seed(path: Path) -> None:
    factory = make_session_factory(sqlite_url(path))
    with session_scope(factory) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="fixture-1",
                creation_identity_sha256="e" * 64,
                creation_custody_mode="producer-retained",
                content_identity="0" * 64,
                tag_set_identity=tag_set_identity(()),
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity="1" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=1,
                path="readme.txt",
                bytes=12,
                sha256="a" * 64,
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=1,
            store="deep",
            state="uploaded",
            archive_storage_prefix="collections/1",
            last_uploaded_at="2026-01-01T00:00:00.000000Z",
            last_verified_at="2026-01-01T00:00:00.000000Z",
        )
        session.add(copy)
        for order, (object_id, kind, size) in enumerate(
            (("data-000000", "pack", 20), ("manifest", "manifest", 10))
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=copy.collection_id,
                    store=copy.store,
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"collections/1/{object_id}.age",
                    plaintext_bytes=size,
                    stored_bytes=size,
                    sha256=chr(ord("a") + order) * 64,
                    stored_sha256=chr(ord("a") + order) * 64,
                    uploaded_at="2026-01-01T00:00:00.000000Z",
                    verified_at="2026-01-01T00:00:00.000000Z",
                )
            )
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=1,
                store="deep",
                desired_revision=1,
                published_revision=1,
                state="published",
                attempt_count=1,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
                object_path="collections/1/metadata.json.age",
                stored_bytes=7,
                stored_sha256="d" * 64,
                published_at="2026-01-01T00:00:00.000000Z",
            )
        )


def _stores(*, include_b2: bool = False) -> ArchiveStoreRegistry:
    stores = {
        "deep": archive_store_binding(MemoryArchiveStore(read_mode="restore_required")),
    }
    if include_b2:
        stores["b2"] = archive_store_binding(MemoryArchiveStore(read_mode="immediate"))
    return ArchiveStoreRegistry(stores)


def test_archive_store_summary_uses_database_aggregates_and_validates_api_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(path))
    _seed(path)

    summary = SqlAlchemyArchiveStoreService(_config(path), _stores()).get("deep")
    response = ArchiveStoreOut.model_validate(map_archive_store(summary))

    assert response.store == "deep"
    assert response.collections == 1
    assert response.objects == 3
    assert response.stored_bytes == 37
    assert response.read_priority == 1
    assert response.write_target is True


def test_archive_store_list_is_bounded_filterable_and_sorted(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    config = _config(path)
    archive = config.archive_store("deep")
    config = replace(
        config,
        archive_stores={
            "deep": archive,
            "b2": replace(
                archive,
                name="b2",
                base_url="http://127.0.0.1/b2",
            ),
        },
        archive_write_store="deep",
        archive_read_order=("deep", "b2"),
    )
    initialize_db(config.database_url)
    _seed(path)

    service = SqlAlchemyArchiveStoreService(config, _stores(include_b2=True))
    page = service.list(
        page_size=1,
        position=None,
        q=None,
        sort="stored_bytes",
        order="desc",
    )
    filtered = service.list(
        page_size=25,
        position=None,
        q="immediate",
        sort="store",
        order="asc",
    )
    by_read_priority = service.list(
        page_size=25,
        position=None,
        q=None,
        sort="read_priority",
        order="asc",
    )

    assert page.next_position is not None
    assert [store.store for store in page.stores] == ["deep"]
    assert [store.store for store in filtered.stores] == ["b2"]
    assert [(store.store, store.read_priority) for store in by_read_priority.stores] == [
        ("deep", 1),
        ("b2", 2),
    ]


def test_archive_store_summary_includes_download_allowance(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    config = _config(path)
    config = replace(
        config,
        archive_stores={
            "deep": replace(
                config.archive_store("deep"),
                monthly_download_allowance_bytes=1_000,
                download_safety_buffer_bytes=100,
            )
        },
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    initialize_db(config.database_url)
    allowance = SqlAlchemyDownloadAllowance(config)
    assert b"".join(
        allowance.track(
            store="deep",
            expected_bytes=125,
            content=iter((b"x" * 125,)),
        )
    )

    summary = SqlAlchemyArchiveStoreService(
        config,
        _stores(),
        download_allowance=allowance,
    ).get("deep")

    assert summary.download_allowance is not None
    assert summary.download_allowance.allowance_bytes == 1_000
    assert summary.download_allowance.accounted_bytes == 125
    assert summary.download_allowance.remaining_bytes == 775
