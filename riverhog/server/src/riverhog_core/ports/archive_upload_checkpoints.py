from __future__ import annotations

from typing import Protocol


class PackUploadCheckpointStore(Protocol):
    def load_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None: ...

    def merge_pack_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str: ...

    def delete_pack_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None: ...


class RawUploadCheckpointStore(Protocol):
    def load_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> str | None: ...

    def merge_raw_upload_checkpoint(
        self, *, collection_id: int, volume_id: str, checkpoint_json: str
    ) -> str: ...

    def delete_raw_upload_checkpoint(self, *, collection_id: int, volume_id: str) -> None: ...
