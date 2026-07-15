from __future__ import annotations

import math

from sqlalchemy import func, select

from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionFileRecord
from riverhog_core.domain.errors import BadRequest, InvalidPath, NotFound
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


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _file_payload(file_record: CollectionFileRecord) -> dict[str, object]:
    return {
        "logical_path": f"{file_record.collection_id}/{file_record.path}",
        "collection_id": file_record.collection_id,
        "collection_path": file_record.path,
        "bytes": int(file_record.bytes),
        "sha256": file_record.sha256,
        "hot": bool(file_record.hot),
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
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        logical_path = parse_logical_path(raw_path)
        parts = logical_path.path.parts
        stmt = select(CollectionFileRecord)
        if len(parts) == 1:
            stmt = stmt.where(
                CollectionFileRecord.collection_id.like(
                    _like_prefix(f"{parts[0]}/"),
                    escape="\\",
                )
            )
        else:
            collection_id = f"{parts[0]}/{parts[1]}"
            stmt = stmt.where(CollectionFileRecord.collection_id == collection_id)
            if len(parts) > 2:
                collection_path = "/".join(parts[2:])
                if logical_path.is_directory:
                    stmt = stmt.where(
                        CollectionFileRecord.path.like(
                            _like_prefix(f"{collection_path}/"),
                            escape="\\",
                        )
                    )
                else:
                    stmt = stmt.where(CollectionFileRecord.path == collection_path)

        with session_scope(self._session_factory) as session:
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            files = session.scalars(
                stmt.order_by(
                    CollectionFileRecord.collection_id,
                    CollectionFileRecord.path,
                )
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
        return {
            "path": logical_path.canonical,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": math.ceil(total / per_page) if total else 0,
            "files": [_file_payload(file_record) for file_record in files],
        }

    def get_content(self, raw_path: str) -> bytes:
        logical_path = parse_logical_path(raw_path)
        if logical_path.is_directory:
            raise InvalidPath("a directory path does not identify one file")
        parts = logical_path.path.parts
        collection_id = f"{parts[0]}/{parts[1]}"
        collection_path = "/".join(parts[2:])

        with session_scope(self._session_factory) as session:
            file_record = session.get(
                CollectionFileRecord,
                {"collection_id": collection_id, "path": collection_path},
            )
            if file_record is None:
                raise NotFound(f"file not found: {raw_path}")
            if not file_record.hot:
                raise NotFound(f"file is not hot: {raw_path}")

        return _read_collection_file_content(
            self._hot_store,
            file_record.collection_id,
            file_record.path,
        )
