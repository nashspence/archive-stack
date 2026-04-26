from __future__ import annotations

import hashlib

from sqlalchemy import select

from arc_core.catalog_models import CollectionFileRecord, CollectionRecord
from arc_core.domain.errors import BadRequest, Conflict, NotFound
from arc_core.domain.models import CollectionSummary
from arc_core.domain.types import CollectionId, Sha256Hex
from arc_core.fs_paths import (
    PathNormalizationError,
    derive_collection_id_from_staging_path,
    find_collection_id_conflict,
    normalize_collection_id,
)
from arc_core.ports.hot_store import HotStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class StubCollectionService:
    def close(self, staging_path: str) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")

    def get(self, collection_id: str) -> object:
        raise NotImplementedError("StubCollectionService is not implemented yet")


class SqlAlchemyCollectionService:
    def __init__(self, config: RuntimeConfig, hot_store: HotStore) -> None:
        self._config = config
        self._hot_store = hot_store
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def close(self, staging_path: str) -> CollectionSummary:
        try:
            collection_id = derive_collection_id_from_staging_path(staging_path)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc

        entries = self._hot_store.list_collection_files(collection_id)
        if not entries:
            raise NotFound(f"no files found in hot store for collection: {collection_id}")

        with session_scope(self._session_factory) as session:
            existing_ids = session.scalars(select(CollectionRecord.id)).all()
            if collection_id in existing_ids:
                raise Conflict(f"collection already exists: {collection_id}")

            conflict = find_collection_id_conflict(existing_ids, collection_id)
            if conflict is not None:
                raise Conflict(f"collection id conflicts with existing collection: {conflict}")

            collection = CollectionRecord(id=collection_id, source_staging_path=staging_path)
            session.add(collection)
            for path, size in entries:
                content = self._hot_store.get_collection_file(collection_id, path)
                collection.files.append(
                    CollectionFileRecord(
                        collection_id=collection_id,
                        path=path,
                        bytes=size,
                        sha256=_sha256_hex(content),
                        hot=True,
                        archived=False,
                    )
                )
            session.flush()
            session.refresh(collection)
            return _summary_from_records(collection_id, collection.files)

    def get(self, collection_id: str) -> CollectionSummary:
        try:
            normalized_collection_id = normalize_collection_id(collection_id)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized_collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {normalized_collection_id}")
            return _summary_from_records(normalized_collection_id, collection.files)


def _sha256_hex(content: bytes) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(content).hexdigest())


def _summary_from_records(
    collection_id: str,
    file_records: list[CollectionFileRecord],
) -> CollectionSummary:
    return CollectionSummary(
        id=CollectionId(collection_id),
        files=len(file_records),
        bytes=sum(record.bytes for record in file_records),
        hot_bytes=sum(record.bytes for record in file_records if record.hot),
        archived_bytes=sum(record.bytes for record in file_records if record.archived),
    )
