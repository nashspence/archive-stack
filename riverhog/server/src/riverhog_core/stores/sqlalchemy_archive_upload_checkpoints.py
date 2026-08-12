from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from sqlalchemy import select

from riverhog_core.catalog_db import SessionFactory, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionArchiveObjectUploadRecord
from riverhog_core.domain.archive import StoredPartReceipt
from riverhog_core.pack_upload import PackUploadCheckpoint, merge_pack_upload_checkpoints
from riverhog_core.raw_upload import RawUploadCheckpoint, merge_raw_upload_checkpoints
from riverhog_core.runtime_config import RuntimeConfig


class _ArchiveUploadCheckpoint(Protocol):
    @property
    def parts(self) -> Sequence[StoredPartReceipt]: ...

    @property
    def completed(self) -> object | None: ...

    def to_json(self) -> str: ...


_CheckpointT = TypeVar("_CheckpointT", bound=_ArchiveUploadCheckpoint)


class SqlAlchemyArchiveUploadCheckpointStore:
    """Persist multipart progress in the upload row that owns an immutable volume plan."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or make_session_factory(config.database_url)

    def load_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None:
        return self._load(collection_id=collection_id, volume_id=volume_id, kind="pack")

    def merge_pack_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str:
        return self._merge(
            collection_id=collection_id,
            volume_id=volume_id,
            kind="pack",
            checkpoint_json=checkpoint_json,
            parse=PackUploadCheckpoint.from_json,
            merge=merge_pack_upload_checkpoints,
        )

    def delete_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None:
        self._delete(collection_id=collection_id, volume_id=volume_id, kind="pack")

    def load_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None:
        return self._load(collection_id=collection_id, volume_id=volume_id, kind="segment")

    def merge_raw_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str:
        return self._merge(
            collection_id=collection_id,
            volume_id=volume_id,
            kind="segment",
            checkpoint_json=checkpoint_json,
            parse=RawUploadCheckpoint.from_json,
            merge=merge_raw_upload_checkpoints,
        )

    def delete_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None:
        self._delete(collection_id=collection_id, volume_id=volume_id, kind="segment")

    def _load(self, *, collection_id: int, volume_id: str, kind: str) -> str | None:
        with session_scope(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (collection_id, volume_id),
            )
            if record is None or record.kind != kind:
                return None
            return record.checkpoint_json

    def _merge(
        self,
        *,
        collection_id: int,
        volume_id: str,
        kind: str,
        checkpoint_json: str,
        parse: Callable[[str], _CheckpointT],
        merge: Callable[[_CheckpointT, _CheckpointT], _CheckpointT],
    ) -> str:
        incoming = parse(checkpoint_json)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(CollectionArchiveObjectUploadRecord)
                .where(
                    CollectionArchiveObjectUploadRecord.collection_id == collection_id,
                    CollectionArchiveObjectUploadRecord.object_id == volume_id,
                )
                .with_for_update()
            )
            if record is None or record.kind != kind:
                raise LookupError(f"archive upload volume not found: {volume_id}")
            current = parse(record.checkpoint_json) if record.checkpoint_json else incoming
            merged = merge(current, incoming)
            encoded = merged.to_json()
            record.checkpoint_json = encoded
            record.uploaded_bytes = sum(item.stored_bytes for item in merged.parts)
            record.uploaded_parts = len(merged.parts)
            record.state = "sealed" if merged.completed is not None else "uploading"
            return encoded

    def _delete(self, *, collection_id: int, volume_id: str, kind: str) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(
                CollectionArchiveObjectUploadRecord,
                (collection_id, volume_id),
            )
            if record is not None and record.kind == kind:
                record.checkpoint_json = None
                record.uploaded_bytes = 0
                record.uploaded_parts = 0
                record.state = "planned"
