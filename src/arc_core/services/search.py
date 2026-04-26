from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from arc_core.catalog_models import CollectionFileRecord, CollectionRecord
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class StubSearchService:
    def search(self, query: str, limit: int) -> list[object]:
        raise NotImplementedError("StubSearchService is not implemented yet")


class SqlAlchemySearchService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def search(self, query: str, limit: int) -> list[object]:
        needle = query.casefold()
        results: list[dict[str, object]] = []

        with session_scope(self._session_factory) as session:
            collections = session.scalars(
                select(CollectionRecord).options(
                    selectinload(CollectionRecord.files).selectinload(CollectionFileRecord.copies)
                )
            ).all()

        for collection in sorted(collections, key=lambda item: item.id):
            if needle in collection.id.casefold():
                results.append(_collection_summary_payload(collection))

        for collection in sorted(collections, key=lambda item: item.id):
            for file_record in sorted(collection.files, key=lambda item: item.path):
                target = f"{collection.id}/{file_record.path}"
                if needle not in target.casefold():
                    continue
                results.append(
                    {
                        "kind": "file",
                        "target": target,
                        "collection": collection.id,
                        "path": f"/{file_record.path}",
                        "bytes": file_record.bytes,
                        "hot": file_record.hot,
                        "copies": [
                            {
                                "id": copy.copy_id,
                                "volume_id": copy.volume_id,
                                "location": copy.location,
                            }
                            for copy in sorted(
                                file_record.copies,
                                key=lambda item: (item.volume_id, item.copy_id, item.location),
                            )
                        ],
                    }
                )

        results.sort(key=lambda item: (str(item["kind"]), str(item["target"])))
        return results[:limit]


def _collection_summary_payload(collection: CollectionRecord) -> dict[str, object]:
    files = collection.files
    bytes_total = sum(record.bytes for record in files)
    hot_bytes = sum(record.bytes for record in files if record.hot)
    archived_bytes = sum(record.bytes for record in files if record.archived)
    return {
        "kind": "collection",
        "target": f"{collection.id}/",
        "collection": collection.id,
        "files": len(files),
        "bytes": bytes_total,
        "hot_bytes": hot_bytes,
        "archived_bytes": archived_bytes,
        "pending_bytes": bytes_total - archived_bytes,
    }
