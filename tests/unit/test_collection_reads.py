from __future__ import annotations

from pathlib import Path

from riverhog_core.catalog_db import (
    create_catalog_engine,
    initialize_db,
    session_scope,
)
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collections import SqlAlchemyCollectionService
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from tests.unit.db_helpers import sqlite_url

NOW = "2026-01-01T00:00:00.000000Z"


def _seed_collections(database: Path, *, count: int) -> tuple[RuntimeConfig, Engine]:
    database_url = sqlite_url(database)
    initialize_db(database_url)
    engine = create_catalog_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_scope(factory) as session:
        for collection_id in range(1, count + 1):
            session.add(
                CollectionRecord(
                    id=collection_id,
                    creation_idempotency_key=f"fixture-{collection_id}",
                    content_etag=f"{collection_id:064x}",
                    provenance_mode="omitted",
                    provenance_etag=None,
                    record_etag=f"{collection_id:064x}",
                    metadata_revision=1,
                    metadata_updated_at=NOW,
                    created_by_app="fixture",
                    created_at=NOW,
                )
            )
            session.add(
                CollectionArchiveCopyRecord(
                    collection_id=collection_id,
                    store="archive",
                    state="uploaded",
                    archive_storage_prefix=f"archives/{collection_id}",
                    last_uploaded_at=NOW,
                    last_verified_at=NOW,
                )
            )
            for order, object_id in enumerate(
                ("manifest", "proof", *(f"segment-{index}" for index in range(8)))
            ):
                session.add(
                    CollectionArchiveObjectRecord(
                        collection_id=collection_id,
                        store="archive",
                        object_id=object_id,
                        object_order=order,
                        kind=object_id if object_id in {"manifest", "proof"} else "segment",
                        object_path=f"archives/{collection_id}/{object_id}",
                        plaintext_bytes=1,
                        stored_bytes=1,
                        sha256="a" * 64,
                        stored_sha256="b" * 64,
                        version_id=f"version-{collection_id}-{object_id}",
                        uploaded_at=NOW,
                        verified_at=NOW,
                    )
                )
    return RuntimeConfig(database_url=database_url), engine


def test_collection_list_query_count_is_independent_of_page_rows(tmp_path: Path) -> None:
    config, engine = _seed_collections(tmp_path / "catalog.sqlite3", count=12)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = SqlAlchemyCollectionService(config, session_factory=factory)
    select_count = 0

    @event.listens_for(engine, "before_cursor_execute")
    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    service.list(page=1, per_page=1, q=None)
    one_row_selects = select_count
    select_count = 0

    loaded_object_ids: list[str] = []

    def record_loaded_object(target: CollectionArchiveObjectRecord, _context: object) -> None:
        loaded_object_ids.append(target.object_id)

    event.listen(CollectionArchiveObjectRecord, "load", record_loaded_object)
    try:
        page = service.list(page=1, per_page=12, q=None)
    finally:
        event.remove(CollectionArchiveObjectRecord, "load", record_loaded_object)
    twelve_row_selects = select_count

    assert len(page.collections) == 12
    assert len(loaded_object_ids) == 24
    assert set(loaded_object_ids) == {"manifest", "proof"}
    assert all(
        current.archive_copies[0].collection_manifest.proof_state == "uploaded"
        for current in page.collections
    )
    assert all(current.archive_copies[0].object_count == 10 for current in page.collections)
    assert twelve_row_selects == one_row_selects
    engine.dispose()
