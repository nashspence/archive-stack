from __future__ import annotations

from typing import Literal

from riverhog_api.schemas.common import RiverhogModel


class ArchiveOut(RiverhogModel):
    state: Literal["pending", "uploading", "uploaded", "retrying", "failed"]
    object_path: str | None
    stored_bytes: int | None
    backend: str | None
    storage_class: str | None
    last_uploaded_at: str | None
    last_verified_at: str | None
    failure: str | None


class CollectionManifestOut(RiverhogModel):
    object_path: str | None = None
    sha256: str | None = None
    ots_object_path: str | None = None
    ots_state: Literal["pending", "uploaded", "failed"] = "pending"
