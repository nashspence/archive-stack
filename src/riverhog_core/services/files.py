from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.domain.errors import InvalidPath, NotFound
from riverhog_core.domain.file_paths import parse_logical_path
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.runtime_config import RuntimeConfig


def _read_collection_file_content(
    hot_store: HotStore,
    collection_id: str,
    path: str,
) -> bytes:
    try:
        return hot_store.get_collection_file(collection_id, path)
    except FileNotFoundError as exc:
        raise NotFound(f"file not found in hot store: {collection_id}/{path}") from exc


def _paginate_file_records(
    records: list[dict[str, object]],
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    total = len(records)
    pages = math.ceil(total / per_page) if total else 0
    start = (page - 1) * per_page
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "files": records[start : start + per_page],
    }


class SqlAlchemyFileService:
    def __init__(self, config: RuntimeConfig, hot_store: HotStore) -> None:
        self._config = config
        self._hot_store = hot_store
        self._session_factory = make_session_factory(config.database_url)

    def query_by_path(
        self,
        raw_path: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        logical_path = parse_logical_path(raw_path)

        with session_scope(self._session_factory) as session:
            all_files = session.scalars(
                select(CollectionFileRecord).options(
                    selectinload(CollectionFileRecord.collection),
                )
            ).all()

        result: list[dict[str, object]] = []
        for file_record in all_files:
            projected = f"{file_record.collection_id}/{file_record.path}"
            if logical_path.is_directory:
                if not projected.startswith(logical_path.canonical):
                    continue
            else:
                if projected != logical_path.canonical:
                    continue
            result.append(
                {
                    "logical_path": projected,
                    "collection_id": file_record.collection_id,
                    "collection_path": file_record.path,
                    "bytes": file_record.bytes,
                    "sha256": file_record.sha256,
                    "hot": file_record.hot,
                }
            )
        records = sorted(result, key=lambda record: str(record["logical_path"]))
        return {
            "path": logical_path.canonical,
            **_paginate_file_records(records, page=page, per_page=per_page),
        }

    def get_content(self, raw_path: str) -> bytes:
        logical_path = parse_logical_path(raw_path)
        if logical_path.is_directory:
            raise InvalidPath("a directory path does not identify one file")

        with session_scope(self._session_factory) as session:
            all_files = session.scalars(
                select(CollectionFileRecord).options(selectinload(CollectionFileRecord.collection))
            ).all()

            matching = [
                file
                for file in all_files
                if f"{file.collection_id}/{file.path}" == logical_path.canonical
            ]

            if not matching:
                raise NotFound(f"file not found: {raw_path}")

            file_record = matching[0]
            if not file_record.hot:
                raise NotFound(f"file is not hot: {raw_path}")

        return _read_collection_file_content(
            self._hot_store,
            file_record.collection_id,
            file_record.path,
        )
