from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from arc_core.catalog_models import CollectionFileRecord, CollectionRecord
from arc_core.domain.errors import BadRequest, InvalidTarget, NotFound
from arc_core.domain.selectors import parse_target
from arc_core.fs_paths import PathNormalizationError, normalize_collection_id
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class StubFileService:
    def list_collection_files(self, collection_id: str) -> list[dict[str, object]]:
        raise NotImplementedError("StubFileService is not implemented yet")

    def query_by_target(self, raw_target: str) -> list[dict[str, object]]:
        raise NotImplementedError("StubFileService is not implemented yet")

    def get_content(self, raw_target: str) -> bytes:
        raise NotImplementedError("StubFileService is not implemented yet")


class SqlAlchemyFileService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def list_collection_files(self, collection_id: str) -> list[dict[str, object]]:
        try:
            normalized = normalize_collection_id(collection_id)
        except PathNormalizationError as exc:
            raise BadRequest(str(exc)) from exc

        with session_scope(self._session_factory) as session:
            collection = session.get(CollectionRecord, normalized)
            if collection is None:
                raise NotFound(f"collection not found: {normalized}")
            return sorted(
                [
                    {
                        "path": f.path,
                        "bytes": f.bytes,
                        "hot": f.hot,
                        "archived": f.archived,
                    }
                    for f in collection.files
                ],
                key=lambda r: str(r["path"]),
            )

    def query_by_target(self, raw_target: str) -> list[dict[str, object]]:
        target = parse_target(raw_target)

        with session_scope(self._session_factory) as session:
            all_files = session.scalars(
                select(CollectionFileRecord).options(
                    selectinload(CollectionFileRecord.collection)
                )
            ).all()

        result: list[dict[str, object]] = []
        for file_record in all_files:
            projected = f"{file_record.collection_id}/{file_record.path}"
            if target.is_dir:
                if not projected.startswith(target.canonical):
                    continue
            else:
                if projected != target.canonical:
                    continue
            result.append(
                {
                    "target": projected,
                    "collection": file_record.collection_id,
                    "path": file_record.path,
                    "bytes": file_record.bytes,
                    "sha256": file_record.sha256,
                    "hot": file_record.hot,
                    "archived": file_record.archived,
                }
            )
        return sorted(result, key=lambda r: str(r["target"]))

    def get_content(self, raw_target: str) -> bytes:
        target = parse_target(raw_target)
        if target.is_dir:
            raise InvalidTarget("directory selectors are not supported for content download")

        with session_scope(self._session_factory) as session:
            all_files = session.scalars(
                select(CollectionFileRecord).options(
                    selectinload(CollectionFileRecord.collection)
                )
            ).all()

            matching = [
                f for f in all_files if f"{f.collection_id}/{f.path}" == target.canonical
            ]

            if not matching:
                raise NotFound(f"file not found: {raw_target}")

            file_record = matching[0]
            if not file_record.hot:
                raise NotFound(f"file is not hot: {raw_target}")

            collection = session.get(CollectionRecord, file_record.collection_id)
            if collection is None:
                raise NotFound(f"collection not found: {file_record.collection_id}")

            dir_path = self._config.resolve_staging_path(collection.source_staging_path)
            return (dir_path / file_record.path).read_bytes()
